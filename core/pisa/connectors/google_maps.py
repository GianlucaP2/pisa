"""Google Places API (New) and Routes API. The key goes in X-Goog-Api-Key, and every call sends a
field mask: Google bills a call at the most expensive field asked for, so each kind of call asks only for
what it needs.

| call | mask | SKU |
|---|---|---|
| search (lists) | id, name, address, location, primary type | Text Search Pro |
| rich search (map_search) | + rating, price, open now, attributes | Text Search Enterprise + Atmosphere |
| find_id | id + location (to check it is the same spot) | Text Search Pro |
| details (card without reviews) | + phone, website, hours, rating, price level | Place Details Enterprise |
| with reviews (place_details) | + reviews, editorial summary, attributes | Details Enterprise + Atmosphere |
| travel | route matrix elements | Route Matrix Pro (driving, TRAFFIC_AWARE), else Essentials |
| photo | getMedia, skipHttpRedirect: a short-lived URL | Place Photo |
"""

import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx

from pisa.connectors.door import guard
from pisa.domain.places import Mode, Period, category_key
from pisa.failures import external_failure
from pisa.ports.places import (
    Coord,
    Meter,
    Photo,
    PlaceProviderError,
    ProviderPlace,
    Review,
    TravelEstimate,
    Usage,
)

PLACES = "https://places.googleapis.com/v1"
ROUTES = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
SEARCH_RADIUS_M = 50_000.0  # the largest location bias Google accepts

_LIST = ["id", "displayName", "formattedAddress", "location", "primaryType"]
SEARCH_MASK = ",".join(f"places.{f}" for f in _LIST)
IDS_MASK = "places.id"  # the free SKU (Text Search Essentials, IDs only): the provider test
FIND_MASK = "places.id,places.location"
DETAILS_MASK = ",".join(
    [*_LIST, "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri", "regularOpeningHours",
     "currentOpeningHours", "rating", "userRatingCount", "priceLevel"]
)  # fmt: skip
# What the place offers, as Google says it (Atmosphere fields; absent when Google doesn't know).
ATTRIBUTES = (
    "outdoorSeating", "servesCoffee", "servesBreakfast", "servesBrunch", "servesLunch", "servesDinner",
    "servesVegetarianFood", "servesDessert", "servesBeer", "servesWine", "servesCocktails", "liveMusic",
    "goodForGroups", "goodForChildren", "allowsDogs", "reservable", "takeout", "dineIn", "delivery",
)  # fmt: skip
# the photos (names and their authors), Google's review summary and its generative summary.
DETAILS_REVIEWS_MASK = (
    DETAILS_MASK + ",reviews,editorialSummary,photos,reviewSummary,generativeSummary," + ",".join(ATTRIBUTES)
)
RICH_SEARCH_MASK = (
    SEARCH_MASK
    + ","
    + ",".join(
        f"places.{f}"
        for f in (
            "rating",
            "userRatingCount",
            "priceLevel",
            "currentOpeningHours.openNow",
            "photos",
            *ATTRIBUTES,
        )
    )
)
PHOTOS_MAX = 10
MATRIX_MASK = "originIndex,destinationIndex,status,condition,distanceMeters,duration,staticDuration"
TRAVEL_MODE: dict[str, str] = {"drive": "DRIVE", "transit": "TRANSIT", "walk": "WALK", "bike": "BICYCLE"}
PRICE_LEVEL = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


class GoogleMaps:
    id = "google"
    can_filter_open_now = True
    can_search_rich = True  # rating, price, open now and attributes, at the richer price

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        meter: Meter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._key = api_key
        self._meter = meter
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _call(
        self, method: str, url: str, mask: str | None, usage: Usage, *, body: Any = None, params: Any = None
    ) -> Any:
        try:
            return await self._call_once(method, url, mask, usage, body=body, params=params)
        except PlaceProviderError as err:  # with Google's own reason, never the key
            external_failure("google_maps", f"{method} {httpx.URL(url).path}", err.status, err, self._key)
            raise

    async def _call_once(
        self, method: str, url: str, mask: str | None, usage: Usage, *, body: Any = None, params: Any = None
    ) -> Any:
        headers = {"X-Goog-Api-Key": self._key}
        if mask is not None:  # getMedia (a photo) takes no field mask
            headers["X-Goog-FieldMask"] = mask
        try:
            response = await self._http.request(
                method, url, json=body, params=params, headers=headers, timeout=10
            )
        except httpx.HTTPError as exc:
            await self._meter(usage, False)
            raise PlaceProviderError(f"Google Maps is unreachable ({exc.__class__.__name__}).") from None
        await self._meter(usage, response.status_code < 400)
        if response.status_code >= 400:
            reason = _reason(response)
            detail = f": {reason}" if reason else ""
            if response.status_code == 429:
                raise PlaceProviderError(
                    f"Google Maps refused the call: a quota is used up (HTTP 429{detail}).", 429, quota=True
                )
            if response.status_code in (400, 403) and "API key" in reason:
                raise PlaceProviderError(
                    f"Google rejected the Maps key (HTTP {response.status_code}{detail}).",
                    response.status_code,
                )
            raise PlaceProviderError(
                f"Google Maps refused the call (HTTP {response.status_code}{detail}).", response.status_code
            )
        try:
            return response.json()
        except ValueError:
            raise PlaceProviderError("Google Maps returned a response that is not JSON.") from None

    async def test(self) -> str:
        """The cheapest real call: an ids-only Text Search (a free SKU)."""
        await self._call(
            "POST", f"{PLACES}/places:searchText", IDS_MASK, Usage("google.text_search_ids"),
            body={"textQuery": "Eiffel Tower", "pageSize": 1},
        )  # fmt: skip
        return "Google accepted the key: a Text Search (ids only, free) worked."

    # --- travel times ------------------------------------------------------------------------------------

    def can_travel(self, mode: Mode, *, arrive_by: bool) -> bool:
        return mode in TRAVEL_MODE and (not arrive_by or mode == "transit")  # arrivalTime: transit only

    def usage_for_travel(self, mode: Mode, destinations: int) -> Usage:
        sku = "google.route_matrix_pro" if mode == "drive" else "google.route_matrix_essentials"
        return Usage(sku, destinations)

    async def travel(
        self,
        origin: Coord,
        destinations: list[Coord],
        mode: Mode,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> list[TravelEstimate | None]:
        body: dict[str, Any] = {
            "origins": [{"waypoint": _waypoint(origin)}],
            "destinations": [{"waypoint": _waypoint(d)} for d in destinations],
            "travelMode": TRAVEL_MODE[mode],
        }
        if mode == "drive":
            body["routingPreference"] = "TRAFFIC_AWARE"
        if depart_at is not None and depart_at > self._clock():
            body["departureTime"] = _rfc3339(depart_at)
        if arrive_by is not None:
            body["arrivalTime"] = _rfc3339(arrive_by)
        elements = await self._call(
            "POST", ROUTES, MATRIX_MASK, self.usage_for_travel(mode, len(destinations)), body=body
        )
        out: list[TravelEstimate | None] = [None] * len(destinations)
        for element in elements if isinstance(elements, list) else []:
            index = element.get("destinationIndex", 0)
            if element.get("condition") != "ROUTE_EXISTS" or (element.get("status") or {}).get("code"):
                continue
            duration, static = _seconds(element.get("duration")), _seconds(element.get("staticDuration"))
            if duration is None or not 0 <= index < len(out):
                continue
            base = static if static is not None else duration
            out[index] = TravelEstimate(  # minutes rounded up: never late
                math.ceil(base / 60),
                math.ceil(duration / 60) if mode == "drive" else None,
                element.get("distanceMeters"),
            )
        return out

    # --- places -----------------------------------------------------------------------------------------

    def usage_for_search(self, rich: bool = False) -> Usage:
        return Usage("google.text_search_enterprise_atmosphere" if rich else "google.text_search_pro")

    async def search(
        self, query: str, *, near: Coord | None, open_now: bool, limit: int, lang: str, rich: bool = False
    ) -> list[ProviderPlace]:
        guard("a map search", query, provider="google")  # RAIL: their values never leave
        body: dict[str, Any] = {"textQuery": query, "pageSize": limit, "languageCode": lang[:2]}
        if near is not None:
            body["locationBias"] = {"circle": {"center": _latlng(near), "radius": SEARCH_RADIUS_M}}
        if open_now:
            body["openNow"] = True
        mask = RICH_SEARCH_MASK if rich else SEARCH_MASK
        data = await self._call(
            "POST", f"{PLACES}/places:searchText", mask, self.usage_for_search(rich), body=body
        )
        places = [_place(p) for p in data.get("places") or []]
        for place in places:
            if open_now:
                place.open_now = True  # Google filtered on it
        return places[:limit]

    def usage_for_photo(self) -> Usage:
        return Usage("google.place_photo")

    async def photo_uri(self, name: str, *, max_width: int) -> str:
        """Place Photos (New) getMedia with skipHttpRedirect: Google answers the photo's short-lived URL."""
        if not re.fullmatch(r"places/[A-Za-z0-9_-]{4,300}/photos/[A-Za-z0-9_-]{4,1000}", name):
            raise PlaceProviderError(f"{name[:60]!r} is not a Google photo name.")
        data = await self._call(
            "GET", f"{PLACES}/{name}/media", None, self.usage_for_photo(),
            params={"maxWidthPx": str(max(1, min(max_width, 4800))), "skipHttpRedirect": "true"},
        )  # fmt: skip
        uri = data.get("photoUri")
        if not isinstance(uri, str) or not uri.startswith("https://"):
            raise PlaceProviderError("Google sent no photo URL.")
        return uri

    async def find_id(self, query: str, *, near: Coord | None, lang: str) -> tuple[str, Coord | None] | None:
        body: dict[str, Any] = {"textQuery": query, "pageSize": 1, "languageCode": lang[:2]}
        if near is not None:
            body["locationBias"] = {"circle": {"center": _latlng(near), "radius": 500.0}}
        data = await self._call(
            "POST", f"{PLACES}/places:searchText", FIND_MASK, Usage("google.text_search_pro"), body=body
        )
        found = data.get("places") or []
        if not found or not found[0].get("id"):
            return None
        location = found[0].get("location") or {}
        lat, lon = location.get("latitude"), location.get("longitude")
        return str(found[0]["id"]), (lat, lon) if lat is not None and lon is not None else None

    def usage_for_details(self, reviews: bool) -> Usage:
        return Usage(
            "google.place_details_enterprise_atmosphere" if reviews else "google.place_details_enterprise"
        )

    async def details(self, provider_id: str, *, reviews: bool, lang: str) -> ProviderPlace:
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,300}", provider_id):
            raise PlaceProviderError(f"{provider_id[:40]!r} is not a Google place id.")
        mask = DETAILS_REVIEWS_MASK if reviews else DETAILS_MASK
        data = await self._call(
            "GET", f"{PLACES}/places/{provider_id}", mask, self.usage_for_details(reviews),
            params={"languageCode": lang[:2]},
        )  # fmt: skip
        return _place(data)


def _reason(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return ""
    message = error.get("message") if isinstance(error, dict) else None
    return str(message) if message else ""


def _latlng(c: Coord) -> dict[str, float]:
    return {"latitude": c[0], "longitude": c[1]}


def _waypoint(c: Coord) -> dict[str, Any]:
    return {"location": {"latLng": _latlng(c)}}


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(value: Any) -> int | None:
    """Google durations are strings like "754s"."""
    if isinstance(value, str) and value.endswith("s"):
        try:
            return round(float(value[:-1]))
        except ValueError:
            return None
    return None


def _day(google_day: int) -> int:
    return 7 if google_day == 0 else google_day  # Google: 0 = Sunday; Pisa: 1 = Monday … 7 = Sunday


def _hhmm(point: dict[str, Any]) -> str:
    return f"{int(point.get('hour', 0)):02d}:{int(point.get('minute', 0)):02d}"


def _periods(hours: dict[str, Any] | None, *, dated: bool) -> list[Period]:
    out: list[Period] = []
    for period in (hours or {}).get("periods") or []:
        opens, closes = period.get("open") or {}, period.get("close")
        when: date | None = None
        if dated:
            d = opens.get("date")
            if not d:
                continue
            when = date(int(d["year"]), int(d["month"]), int(d["day"]))
        out.append(
            Period(
                day=_day(int(opens.get("day", 0))) if when is None else when.isoweekday(),
                open=_hhmm(opens),
                close=_hhmm(closes) if closes else None,  # no close: open around the clock
                date=when,
            )
        )
    return out


def _place(data: dict[str, Any]) -> ProviderPlace:
    location = data.get("location") or {}
    current = data.get("currentOpeningHours")
    current_periods = _periods(current, dated=True)
    special = [
        date(int(d["date"]["year"]), int(d["date"]["month"]), int(d["date"]["day"]))
        for d in (current or {}).get("specialDays") or []
        if d.get("date")
    ]
    opening_dates = {p.date for p in current_periods}
    reviews = [
        Review(
            author=(r.get("authorAttribution") or {}).get("displayName"),
            rating=r.get("rating"),
            text=((r.get("text") or r.get("originalText") or {}).get("text") or ""),
            published_at=r.get("publishTime"),
            source="google",
            relative_time=r.get("relativePublishTimeDescription"),
            author_uri=(r.get("authorAttribution") or {}).get("uri"),
        )
        for r in data.get("reviews") or []
    ]
    return ProviderPlace(
        provider="google",
        provider_id=str(data.get("id", "")),
        name=(data.get("displayName") or {}).get("text") or "",
        lat=location.get("latitude"),
        lon=location.get("longitude"),
        category=category_key(data.get("primaryType")),
        address=data.get("formattedAddress"),
        phone=data.get("internationalPhoneNumber") or data.get("nationalPhoneNumber"),
        website=data.get("websiteUri"),
        weekly=_periods(data.get("regularOpeningHours"), dated=False),
        current=current_periods,
        closed_dates=[d for d in special if d not in opening_dates],
        hours_note=("Special hours on " + ", ".join(d.isoformat() for d in sorted(special)) + ".")
        if special
        else None,
        open_now=(current or {}).get("openNow") if current else None,
        rating=data.get("rating"),
        rating_count=data.get("userRatingCount"),
        price_level=PRICE_LEVEL.get(str(data.get("priceLevel"))),
        summary=(data.get("editorialSummary") or {}).get("text"),
        reviews=[r for r in reviews if r.text],
        attributes={a: data[a] for a in ATTRIBUTES if isinstance(data.get(a), bool)},
        photos=_photos(data.get("photos")),
        review_summary=_text(data, "reviewSummary", "text", "text"),
        review_summary_disclosure=_text(data, "reviewSummary", "disclosureText", "text"),
        review_summary_flag_uri=_text(data, "reviewSummary", "flagContentUri"),
        reviews_uri=_text(data, "reviewSummary", "reviewsUri"),
        description=_text(data, "generativeSummary", "overview", "text"),
        description_disclosure=_text(data, "generativeSummary", "disclosureText", "text"),
        description_flag_uri=_text(data, "generativeSummary", "overviewFlagContentUri"),
    )


def _photos(items: Any) -> list[Photo]:
    out = []
    for item in (items if isinstance(items, list) else [])[:PHOTOS_MAX]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        author = next(iter(item.get("authorAttributions") or []), {}) or {}
        out.append(
            Photo(
                item["name"],
                author.get("displayName"),
                author.get("uri"),
                item.get("widthPx"),
                item.get("heightPx"),
            )
        )
    return out


def _text(data: dict[str, Any], *path: str) -> str | None:
    """A string deep in Google's answer (its AI summaries, their disclosures and links), else None."""
    value: Any = data
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None
