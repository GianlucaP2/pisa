"""OpenStreetMap: place search (Photon, or Nominatim), places nearby (Overpass), free and without a key. Data
© OpenStreetMap contributors, ODbL: every result says so (`ProviderPlace.attribution`).

The public instances are run by volunteers, so their etiquette is enforced here, not left to the caller:

- **One request a second per backend**, process-wide (`Limiter`): every caller of the same base URL waits its
  turn, whichever adapter instance it holds.
- **An honest User-Agent** (`user_agent`): `PISA/<version>`, and the operator's project URL when set. Nothing
  about the user: no name, no address, no email.
- **A cache** (`TtlCache`): bounded, in memory, keyed by the normalized query and the rounded area, so asking
  again doesn't ask the service again.

What leaves the server: the query text (search), a kind from a fixed list (nearby), and a point rounded to 3
decimals (~100 m): a search's bias and a nearby search's centre. Never an id of the user's. Every base URL is
the operator's (`OsmConfig`), so self-hosted backends work; no redirect is followed (a 3xx is a provider
error), so a call never ends up at another host.

Answers are untrusted: names, addresses, phones and websites are cleaned (no control, zero-width or
bidirectional characters, lengths clipped), coordinates checked, counts capped, the body's size capped.
"""

import asyncio
import json
import math
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

import httpx

from pisa import __version__
from pisa.connectors.door import guard
from pisa.connectors.openrouteservice import OpenRouteService, not_configured
from pisa.domain.places import OSM_ATTRIBUTION, OSM_ATTRIBUTION_URL, Mode, osm_hours
from pisa.failures import external_failure
from pisa.logging import get_logger
from pisa.ports.places import (
    Coord,
    Meter,
    PlaceProviderError,
    ProviderPlace,
    TravelEstimate,
    Usage,
)

ATTRIBUTION = OSM_ATTRIBUTION
ATTRIBUTION_URL = OSM_ATTRIBUTION_URL
PHOTON_URL = "https://photon.komoot.io"
# the languages photon.komoot.io takes: any other `lang` (it) is HTTP 400 "Language is not supported.
# Supported are: default, de, en, fr" (v0.1.5 in production: every Italian place search failed)
PHOTON_LANGS = frozenset({"de", "en", "fr"})
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

MIN_INTERVAL_S = 1.0  # the public instances' rule: at most one request a second
CACHE_TTL_S = 6 * 3600.0
CACHE_MAX = 512
BIAS_DECIMALS = 3  # ~100 m: enough to rank results near the user, not where they stand
CACHE_AREA_DECIMALS = 2  # ~1 km: two searches this close share their answer
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
OVERPASS_TIMEOUT_S = 8  # the server's own budget for the query ([timeout:…])
OVERPASS_HTTP_TIMEOUT = httpx.Timeout(12.0, connect=4.0)
BODY_MAX = 2 * 1024 * 1024  # bytes of an answer read at most
SEARCH_MAX = 10
NEARBY_MAX = 20
RADIUS_MIN_M, RADIUS_MAX_M = 100, 5_000
NAME_MAX, ADDRESS_MAX, PHONE_MAX, URL_MAX = 120, 200, 40, 300
HOURS_MAX = 255  # OSM's own limit for a tag's value
QUERY_MAX = 200

# What "near a point" can ask Overpass for: a fixed list, so no text of the user's is written into the query.
NEARBY_KINDS: dict[str, tuple[str, str]] = {
    "pharmacy": ("amenity", "pharmacy"),
    "fuel": ("amenity", "fuel"),
    "atm": ("amenity", "atm"),
    "bank": ("amenity", "bank"),
    "hospital": ("amenity", "hospital"),
    "doctors": ("amenity", "doctors"),
    "police": ("amenity", "police"),
    "post_office": ("amenity", "post_office"),
    "toilets": ("amenity", "toilets"),
    "drinking_water": ("amenity", "drinking_water"),
    "parking": ("amenity", "parking"),
    "charging_station": ("amenity", "charging_station"),
    "bicycle_parking": ("amenity", "bicycle_parking"),
    "cafe": ("amenity", "cafe"),
    "restaurant": ("amenity", "restaurant"),
    "bar": ("amenity", "bar"),
    "fast_food": ("amenity", "fast_food"),
    "supermarket": ("shop", "supermarket"),
    "convenience": ("shop", "convenience"),
    "bakery": ("shop", "bakery"),
    "tobacco": ("shop", "tobacco"),
    "hotel": ("tourism", "hotel"),
}

Backend = Literal["photon", "nominatim"]
_log = get_logger("pisa.places.osm")


@dataclass(frozen=True)
class OsmConfig:
    """The operator's choices (PISA_OSM_*): which geocoder, each backend's base URL, the project URL that goes
    in the User-Agent."""

    geocoder: Backend = "photon"
    photon_url: str = PHOTON_URL
    nominatim_url: str = NOMINATIM_URL
    overpass_url: str = OVERPASS_URL
    project_url: str = ""

    @classmethod
    def from_settings(cls, settings: Any) -> "OsmConfig":
        return cls(
            geocoder=settings.osm_geocoder,
            photon_url=settings.osm_photon_url,
            nominatim_url=settings.osm_nominatim_url,
            overpass_url=settings.osm_overpass_url,
            project_url=settings.osm_project_url,
        )


def user_agent(project_url: str = "") -> str:
    """PISA and its version, and where to read about it: never anything of the user's."""
    where = f"+{project_url}" if project_url else "self-hosted personal assistant"
    return f"PISA/{__version__} ({where})"


# --- etiquette: the limiter and the cache ----------------------------------------------------------------


class Limiter:
    """At most one request every `interval` seconds. Each caller takes the next free slot before it waits
    (no await in between), so callers in parallel are spaced out, never let through together."""

    def __init__(
        self,
        interval: float = MIN_INTERVAL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next = -math.inf

    async def wait(self) -> None:
        now = self._clock()
        slot = max(now, self._next)
        self._next = slot + self.interval
        if slot > now:
            await self._sleep(slot - now)


class TtlCache:
    """A bounded in-memory cache: entries live `ttl` seconds; past `size` entries the oldest used goes."""

    def __init__(
        self, ttl: float = CACHE_TTL_S, size: int = CACHE_MAX, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._size = size
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[float, list[ProviderPlace]]] = OrderedDict()

    def get(self, key: Hashable) -> list[ProviderPlace] | None:
        hit = self._items.get(key)
        if hit is None:
            return None
        if hit[0] <= self._clock():
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return [replace(p) for p in hit[1]]  # copies: a caller may change what it gets

    def put(self, key: Hashable, value: list[ProviderPlace]) -> None:
        self._items[key] = (self._clock() + self._ttl, [replace(p) for p in value])
        self._items.move_to_end(key)
        while len(self._items) > self._size:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


# process-wide: every adapter built for the same base URL shares its limiter and its cache
_LIMITERS: dict[str, Limiter] = {}
_CACHES: dict[str, TtlCache] = {}


def limiter_for(base: str) -> Limiter:
    return _LIMITERS.setdefault(base, Limiter())


def cache_for(base: str) -> TtlCache:
    return _CACHES.setdefault(base, TtlCache())


def reset() -> None:
    """Forget every limiter and cache (tests)."""
    _LIMITERS.clear()
    _CACHES.clear()


# --- untrusted answers --------------------------------------------------------------------------------------


def clean(value: Any, limit: int) -> str | None:
    """A provider's string fit to show: no control, format (zero-width, bidirectional), private or unassigned
    characters, whitespace folded, at most `limit` characters. None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value[: limit * 4])
    kept = "".join(
        " " if c.isspace() else c for c in text if c.isspace() or unicodedata.category(c)[0] != "C"
    )
    out = " ".join(kept.split())[:limit].strip()
    return out or None


def clean_url(value: Any) -> str | None:
    url = clean(value, URL_MAX)
    if url is None or " " in url or not re.match(r"(?i)https?://[^/\s]+", url):
        return None
    return url


def coordinate(lat: Any, lon: Any) -> Coord | None:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(la) and math.isfinite(lo) and -90 <= la <= 90 and -180 <= lo <= 180):
        return None
    return la, lo


def rounded(point: Coord, decimals: int = BIAS_DECIMALS) -> Coord:
    return round(point[0], decimals), round(point[1], decimals)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _category(value: Any) -> str | None:
    text = clean(value, 40)
    return text if text and re.fullmatch(r"[a-z0-9_]+", text) else None


def _osm_id(kind: Any, ident: Any) -> str | None:
    """`node/123`, `way/45`, `relation/6`: the element's own id (Photon writes N, W, R)."""
    kinds = {"n": "node", "w": "way", "r": "relation", "node": "node", "way": "way", "relation": "relation"}
    name = kinds.get(str(kind).lower()) if kind is not None else None
    if name is None or isinstance(ident, bool):
        return None
    try:
        number = int(ident)
    except (TypeError, ValueError):
        return None
    return f"{name}/{number}" if number > 0 else None


def _join(*parts: Any) -> str | None:
    words = [p for p in (clean(x, ADDRESS_MAX) for x in parts) if p]
    return clean(", ".join(words), ADDRESS_MAX) if words else None


def _hours(value: Any) -> dict[str, Any]:
    """The place's `opening_hours`: a week of periods when the rule is a simple one (`osm_hours`), else the
    raw text as the hours note (never guessed); nothing when there is no tag."""
    raw = clean(value, HOURS_MAX)
    if raw is None:
        return {}
    weekly = osm_hours(raw)
    return {"weekly": weekly} if weekly is not None else {"hours_note": raw}


def _osm_place(
    provider_id: str, name: str, point: Coord, category: str | None, address: str | None, **more: Any
) -> ProviderPlace:
    return ProviderPlace(
        provider="osm", provider_id=provider_id, name=name, lat=point[0], lon=point[1], category=category,
        address=address, attribution=ATTRIBUTION, **more,
    )  # fmt: skip


# --- one HTTP call ------------------------------------------------------------------------------------------


class _Backend:
    """One OSM service at the operator's base URL: its limiter, its cache, its failures."""

    provider: str  # the name in `external_failure` lines
    label: str  # the name in errors

    def __init__(self, http: httpx.AsyncClient, meter: Meter, base: str, project_url: str) -> None:
        self._http = http
        self._meter = meter
        self.base = base.rstrip("/")
        self._agent = user_agent(project_url)
        self.limiter = limiter_for(self.base)
        self.cache = cache_for(self.base)

    async def call(self, method: str, url: str, sku: str, *, limit_s: httpx.Timeout, **kwargs: Any) -> Any:
        try:
            return await self._call_once(method, url, sku, limit_s=limit_s, **kwargs)
        except PlaceProviderError as err:  # status and kind only: an answer may echo the query
            external_failure(self.provider, f"{method} {httpx.URL(url).path}", err.status, err)
            raise

    async def _call_once(
        self, method: str, url: str, sku: str, *, limit_s: httpx.Timeout, **kwargs: Any
    ) -> Any:
        await self.limiter.wait()
        headers = {"User-Agent": self._agent, "Accept": "application/json"}
        try:
            async with self._http.stream(
                method, url, headers=headers, timeout=limit_s, follow_redirects=False, **kwargs
            ) as response:
                body = bytearray()
                if response.status_code < 300:
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > BODY_MAX:
                            await self._meter(Usage(sku), False)
                            raise PlaceProviderError(f"{self.label} sent an answer too large to read.")
        except httpx.HTTPError as exc:
            await self._meter(Usage(sku), False)
            raise PlaceProviderError(f"{self.label} is unreachable ({exc.__class__.__name__}).") from None
        status = response.status_code
        await self._meter(Usage(sku), status < 300)
        if status == 429:
            raise PlaceProviderError(
                f"{self.label} refused the call: too many requests (HTTP 429).", 429, quota=True
            )
        if status >= 300:  # a redirect is never followed: it could lead to another host
            raise PlaceProviderError(f"{self.label} refused the call (HTTP {status}).", status)
        try:
            return json.loads(bytes(body))
        except ValueError:
            raise PlaceProviderError(f"{self.label} returned a response that is not JSON.") from None


def _search_key(
    op: str, query: str, near: Coord | None, limit: int, lang: str, within_m: int | None = None
) -> tuple[Any, ...]:
    # a bounded search is kept by the box it sent (E1 BX-1): two points of one cache area send different
    # boxes, and one's places are not the other's
    area: Any = None
    if near is not None:
        area = box(near, within_m) if within_m is not None else rounded(near, CACHE_AREA_DECIMALS)
    return op, " ".join(query.casefold().split()), area, limit, lang[:2].lower(), within_m


def box(near: Coord, within_m: int) -> tuple[float, float, float, float]:
    """(min lon, min lat, max lon, max lat) of a square `within_m` each way around the rounded point: a
    bound, not a bias (§31 p1: a biased "ristorante" brought places 146-201 km away first)."""
    lat, lon = rounded(near)
    dlat = within_m / 111_320
    dlon = within_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    return round(lon - dlon, 3), round(lat - dlat, 3), round(lon + dlon, 3), round(lat + dlat, 3)


Parsed = tuple[ProviderPlace, bool]  # a place, and whether it names a street (an address to go to)


class _Geocoder(_Backend):
    """A search backend: the query and the rounded bias go out; the answer is cleaned, capped and kept."""

    async def search(
        self,
        query: str,
        *,
        near: Coord | None,
        limit: int,
        lang: str,
        streets: bool = False,
        within_m: int | None = None,
    ) -> list[ProviderPlace]:
        """At most `limit` places; `streets`: only those that name a street (a geocode); `within_m`: only
        within that distance of `near` (a bound box), else `near` is a bias."""
        key = _search_key("address" if streets else "search", query, near, limit, lang, within_m)
        if (hit := self.cache.get(key)) is not None:
            return hit
        bound = box(near, within_m) if near is not None and within_m is not None else None
        found = self._parse(await self._ask(query[:QUERY_MAX], near, limit, lang[:2].lower(), bound))
        places = [place for place, street in found if street or not streets][:limit]
        self.cache.put(key, places)
        return places

    async def _ask(
        self, query: str, near: Coord | None, limit: int, lang: str, bound: tuple[float, ...] | None = None
    ) -> Any:
        raise NotImplementedError

    def _parse(self, data: Any) -> list[Parsed]:
        raise NotImplementedError


class Photon(_Geocoder):
    """komoot's Photon: `/api` (search) answers GeoJSON features with OSM's own properties."""

    provider = "osm_photon"
    label = "Photon (OpenStreetMap)"
    sku = "osm.photon"

    async def _ask(
        self, query: str, near: Coord | None, limit: int, lang: str, bound: tuple[float, ...] | None = None
    ) -> Any:
        params: dict[str, str] = {"q": query, "limit": str(limit)}
        if lang in PHOTON_LANGS:  # else its default names (the local ones: Italian in Italy)
            params["lang"] = lang
        if near is not None:
            lat, lon = rounded(near)
            params |= {"lat": f"{lat:.{BIAS_DECIMALS}f}", "lon": f"{lon:.{BIAS_DECIMALS}f}"}
        if bound is not None:  # minLon,minLat,maxLon,maxLat: only places inside
            params["bbox"] = ",".join(f"{v:.3f}" for v in bound)
        return await self.call("GET", f"{self.base}/api", self.sku, limit_s=TIMEOUT, params=params)

    def _parse(self, data: Any) -> list[Parsed]:
        features = data.get("features") if isinstance(data, dict) else None
        return [p for p in map(_photon_place, features if isinstance(features, list) else []) if p]


def _photon_place(feature: Any) -> Parsed | None:
    if not isinstance(feature, dict):
        return None
    props, geometry = feature.get("properties"), feature.get("geometry")
    if not isinstance(props, dict) or not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) != 2:
        return None
    point = coordinate(coords[1], coords[0])  # GeoJSON: [lon, lat]
    ident = _osm_id(props.get("osm_type"), props.get("osm_id"))
    road = clean(props.get("street"), 100)
    street = " ".join(x for x in (road, clean(props.get("housenumber"), 20)) if x)
    town = " ".join(x for x in (clean(props.get("postcode"), 20), clean(props.get("city"), 80)) if x)
    address = _join(street, town, props.get("country"))
    name = clean(props.get("name"), NAME_MAX) or clean(street, NAME_MAX)
    if point is None or ident is None or name is None:
        return None
    return _osm_place(ident, name, point, _category(props.get("osm_value")), address), road is not None


class Nominatim(_Geocoder):
    """OSM's own Nominatim: `/search` with `format=jsonv2`. A bias is a viewbox around the point (not a
    bound: results outside it still come). `extratags=1`: each place's other OSM tags come too (nothing more
    is sent), its `opening_hours` among them."""

    provider = "osm_nominatim"
    label = "Nominatim (OpenStreetMap)"
    sku = "osm.nominatim"
    VIEWBOX_DEG = 0.25  # ~25 km each way

    async def _ask(
        self, query: str, near: Coord | None, limit: int, lang: str, bound: tuple[float, ...] | None = None
    ) -> Any:
        params: dict[str, str] = {
            "q": query, "format": "jsonv2", "limit": str(limit), "addressdetails": "1", "extratags": "1",
            "accept-language": lang,
        }  # fmt: skip
        if bound is not None:  # a bound: only places inside the box
            min_lon, min_lat, max_lon, max_lat = bound
            params |= {"viewbox": f"{min_lon:.3f},{max_lat:.3f},{max_lon:.3f},{min_lat:.3f}", "bounded": "1"}
        elif near is not None:
            lat, lon = rounded(near)
            d = self.VIEWBOX_DEG
            params["viewbox"] = f"{lon - d:.3f},{lat + d:.3f},{lon + d:.3f},{lat - d:.3f}"
        return await self.call("GET", f"{self.base}/search", self.sku, limit_s=TIMEOUT, params=params)

    def _parse(self, data: Any) -> list[Parsed]:
        return [p for p in map(_nominatim_place, data if isinstance(data, list) else []) if p]


def _nominatim_place(item: Any) -> Parsed | None:
    if not isinstance(item, dict):
        return None
    point = coordinate(item.get("lat"), item.get("lon"))
    ident = _osm_id(item.get("osm_type"), item.get("osm_id"))
    address = _dict(item.get("address"))
    road = clean(address.get("road"), 100)
    street = " ".join(x for x in (road, clean(address.get("house_number"), 20)) if x)
    town = address.get("city") or address.get("town") or address.get("village")
    where = _join(street, " ".join(x for x in (clean(address.get("postcode"), 20), clean(town, 80)) if x),
                  address.get("country"))  # fmt: skip
    name = clean(item.get("name"), NAME_MAX) or clean(street, NAME_MAX)
    if point is None or ident is None or name is None:
        return None
    where = where or clean(item.get("display_name"), ADDRESS_MAX)
    hours = _hours(_dict(item.get("extratags")).get("opening_hours"))
    return _osm_place(ident, name, point, _category(item.get("type")), where, **hours), road is not None


class Overpass(_Backend):
    """Overpass: the elements of one kind (`NEARBY_KINDS`) around a point."""

    provider = "osm_overpass"
    label = "Overpass (OpenStreetMap)"
    sku = "osm.overpass"

    async def nearby(self, kind: str, *, near: Coord, radius_m: int, limit: int) -> list[ProviderPlace]:
        tag = NEARBY_KINDS.get(kind)
        if tag is None:
            raise PlaceProviderError(f"OpenStreetMap can't look for {kind[:40]!r} nearby.")
        radius = max(RADIUS_MIN_M, min(int(radius_m), RADIUS_MAX_M))
        limit = max(1, min(limit, NEARBY_MAX))
        lat, lon = rounded(near)
        key = ("nearby", kind, (lat, lon), radius, limit)
        if (hit := self.cache.get(key)) is not None:
            return hit
        query = (
            f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];"
            f'nwr["{tag[0]}"="{tag[1]}"](around:{radius},{lat:.{BIAS_DECIMALS}f},{lon:.{BIAS_DECIMALS}f});'
            f"out center tags {limit};"
        )
        data = await self.call(
            "POST", self.base, self.sku, limit_s=OVERPASS_HTTP_TIMEOUT, data={"data": query}
        )
        elements = data.get("elements") if isinstance(data, dict) else None
        places = [p for p in map(_overpass_place, elements if isinstance(elements, list) else []) if p][
            :limit
        ]
        self.cache.put(key, places)
        return places


def _overpass_place(element: Any) -> ProviderPlace | None:
    if not isinstance(element, dict):
        return None
    tags = _dict(element.get("tags"))
    center = _dict(element.get("center")) or element
    point = coordinate(center.get("lat"), center.get("lon"))
    ident = _osm_id(element.get("type"), element.get("id"))
    name = (
        clean(tags.get("name"), NAME_MAX)
        or clean(tags.get("brand"), NAME_MAX)
        or clean(tags.get("operator"), NAME_MAX)
    )
    if point is None or ident is None or name is None:
        return None
    street = " ".join(
        x for x in (clean(tags.get("addr:street"), 100), clean(tags.get("addr:housenumber"), 20)) if x
    )
    town = " ".join(x for x in (clean(tags.get("addr:postcode"), 20), clean(tags.get("addr:city"), 80)) if x)
    category = next((_category(tags.get(k)) for k in ("amenity", "shop", "tourism") if tags.get(k)), None)
    return _osm_place(
        ident, name, point, category, _join(street, town),
        phone=clean(tags.get("phone") or tags.get("contact:phone"), PHONE_MAX),
        website=clean_url(tags.get("website") or tags.get("contact:website")),
    )  # fmt: skip


# --- the adapter -------------------------------------------------------------------------------------------


class OsmPlaces:
    """The places port over OpenStreetMap: search (Photon or Nominatim), nearby (Overpass) and, with the
    user's OpenRouteService key, travel times. No key of its own, no bill (its usage is metered, never
    priced)."""

    id = "osm"
    attribution = ATTRIBUTION
    attribution_url = ATTRIBUTION_URL
    # it can't filter by open now; Nominatim's places carry their `opening_hours` when simple (`osm_hours`),
    # and the search says open or closed from them (Photon's public answer has no tags beyond its own)
    can_filter_open_now = False
    can_search_rich = False
    can_bound = True  # a search can be bounded to a distance of a point (`within_m`): Photon's bbox

    def __init__(
        self,
        http: httpx.AsyncClient,
        meter: Meter,
        *,
        config: OsmConfig | None = None,
        routes: OpenRouteService | None = None,
    ) -> None:
        config = config or OsmConfig()
        backend = Nominatim if config.geocoder == "nominatim" else Photon
        base = config.nominatim_url if config.geocoder == "nominatim" else config.photon_url
        self._geocoder: Photon | Nominatim = backend(http, meter, base, config.project_url)
        self._overpass = Overpass(http, meter, config.overpass_url, config.project_url)
        self._routes = routes

    async def test(self) -> str:
        await self._geocoder.search("Roma", near=None, limit=1, lang="en")
        return f"{self._geocoder.label} answered a search."

    # --- search --------------------------------------------------------------------------------------------

    def usage_for_search(self, rich: bool = False) -> Usage:  # `rich` is never asked (can_search_rich)
        return Usage(self._geocoder.sku)

    async def search(
        self,
        query: str,
        *,
        near: Coord | None,
        open_now: bool,
        limit: int,
        lang: str,
        rich: bool = False,
        within_m: int | None = None,
    ) -> list[ProviderPlace]:
        """`open_now` and `rich` are never asked of OSM (`can_filter_open_now`, `can_search_rich`): its places
        carry their hours when Nominatim read them; `within_m` (`can_bound`): only places that close to
        `near`."""
        guard("a map search", query, provider="osm")  # RAIL: their values never leave
        return await self._geocoder.search(
            query, near=near, limit=max(1, min(limit, SEARCH_MAX)), lang=lang, within_m=within_m
        )

    async def geocode(self, text: str, *, near: Coord | None, lang: str) -> list[ProviderPlace]:
        """Street addresses for a text ("Via Esempio 1, Milano"): the results that name a street (a whole
        city or region is too vague to go to)."""
        guard("a map search", text, provider="osm")  # RAIL: never their values
        return await self._geocoder.search(text, near=near, limit=5, lang=lang, streets=True)

    # --- nearby --------------------------------------------------------------------------------------------

    def can_find_nearby(self, kind: str) -> bool:
        return kind in NEARBY_KINDS

    def usage_for_nearby(self) -> Usage:
        return Usage(self._overpass.sku)

    async def nearby(self, kind: str, *, near: Coord, radius_m: int, limit: int) -> list[ProviderPlace]:
        """Places of one kind (`NEARBY_KINDS`: pharmacy, fuel, atm…) within `radius_m` (100 m to 5 km) of a
        point, at most `limit` (20)."""
        return await self._overpass.nearby(kind, near=near, radius_m=radius_m, limit=limit)

    # --- travel times (OpenRouteService, the user's key) ----------------------------------------------------

    def can_travel(self, mode: Mode, *, arrive_by: bool) -> bool:
        return self._routes is not None and self._routes.can_travel(mode, arrive_by=arrive_by)

    def usage_for_travel(self, mode: Mode, destinations: int) -> Usage:
        if self._routes is None:
            return Usage("ors.matrix", 0)
        return self._routes.usage_for_travel(mode, destinations)

    async def travel(
        self,
        origin: Coord,
        destinations: list[Coord],
        mode: Mode,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> list[TravelEstimate | None]:
        if self._routes is None:
            raise not_configured()  # no key: not configured, never a failure
        return await self._routes.travel(origin, destinations, mode, depart_at=depart_at, arrive_by=arrive_by)
