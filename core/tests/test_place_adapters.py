"""Place provider adapters against recorded responses (httpx.MockTransport): Apple's token exchange and
refresh, 429 and back-off, travel modes; Google's field mask per call kind, hours, reviews, errors; Tavily's
requests and credits. No network."""

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest

from pisa.connectors import google_maps
from pisa.connectors.apple_maps import AppleMaps, MapsKey
from pisa.connectors.google_maps import ATTRIBUTES, GoogleMaps
from pisa.connectors.tavily import Tavily
from pisa.domain.places import Period
from pisa.ports.places import PlaceProviderError, Usage
from tests.conftest import Clock
from tests.places_env import HERE_MILAN
from tests.places_fake import FakePlaces
from tests.test_push import p8

MARIO = (45.465, 9.094)


class Meter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, bool]] = []

    async def __call__(self, usage: Usage, ok: bool) -> None:
        self.calls.append((usage.sku, usage.units, ok))


def apple(fake: FakePlaces, clock: Clock | None = None) -> tuple[AppleMaps, Meter, Any]:
    pem, public = p8()
    meter = Meter()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return (
        AppleMaps(
            http, MapsKey("MAPSKEY123", "EXAMPLETEAM", pem), meter, clock=clock or Clock(1_800_000_000.0)
        ),
        meter,
        public,
    )


def google(fake: FakePlaces) -> tuple[GoogleMaps, Meter]:
    meter = Meter()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return GoogleMaps(http, "AIza-test", meter, clock=lambda: datetime(2026, 10, 21, 8, tzinfo=UTC)), meter


# --- Apple ------------------------------------------------------------------------------------------------


def test_apple_token_exchange_and_its_jwt() -> None:
    fake = FakePlaces()
    client, meter, public = apple(fake)
    asyncio.run(client.access_token())
    request = fake.last("apple.token")
    token = request.headers["authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": "MAPSKEY123", "typ": "JWT"}
    claims = jwt.decode(
        token, public, algorithms=["ES256"], options={"verify_iat": False, "verify_exp": False}
    )
    assert claims == {"iss": "EXAMPLETEAM", "iat": 1_800_000_000, "exp": 1_800_001_800, "scope": "server_api"}
    assert meter.calls == [("apple.token", 1, True)]
    assert "private_key_pem" not in repr(client._key)


def test_apple_access_token_is_cached_and_renewed() -> None:
    fake = FakePlaces()
    clock = Clock(1_800_000_000.0)
    client, _, _ = apple(fake, clock)

    async def three_searches() -> None:
        await client.search("trattoria", near=HERE_MILAN, open_now=False, limit=5, lang="it-IT")
        clock.now += 1800 - 61  # still valid (renewed a minute early)
        await client.search("trattoria", near=HERE_MILAN, open_now=False, limit=5, lang="it-IT")
        clock.now += 2
        await client.search("trattoria", near=HERE_MILAN, open_now=False, limit=5, lang="it-IT")

    asyncio.run(three_searches())
    assert fake.routes() == ["apple.token", "apple.search", "apple.search", "apple.token", "apple.search"]


def test_apple_401_renews_the_token_once() -> None:
    fake = FakePlaces()
    client, _, _ = apple(fake)

    async def go() -> None:
        await client.access_token()
        fake.apple_tokens += 1  # Apple revoked the cached token: the next call is a 401
        await client.search("trattoria", near=None, open_now=False, limit=1, lang="it-IT")

    asyncio.run(go())
    assert fake.routes() == ["apple.token", "apple.search", "apple.token", "apple.search"]


def test_apple_429_backs_off() -> None:
    fake = FakePlaces()
    clock = Clock(1_800_000_000.0)
    client, _, _ = apple(fake, clock)
    fake.fail["apple.search"] = (429, {"error": {"message": "Too Many Requests", "details": []}})

    async def go() -> list[str]:
        out = []
        for _ in range(2):
            try:
                await client.search("x", near=None, open_now=False, limit=1, lang="it-IT")
            except PlaceProviderError as err:
                out.append(err.message)
        clock.now += 61
        await client.search("x", near=None, open_now=False, limit=1, lang="it-IT")
        return out

    errors = asyncio.run(go())
    assert errors[0] == "Apple Maps refused the call: the daily quota of 25,000 calls is used up (HTTP 429)."
    assert errors[1].startswith("Apple Maps is backing off")
    assert fake.routes() == ["apple.token", "apple.search", "apple.search"]  # no call while backing off


@pytest.mark.parametrize(("mode", "transport"), [("drive", "Automobile"), ("transit", "Transit"),
                                                  ("walk", "Walking"), ("bike", "Cycling")])  # fmt: skip
def test_apple_travel_modes_and_dates(mode: str, transport: str) -> None:
    fake = FakePlaces()
    client, _, _ = apple(fake)
    depart = datetime(2026, 10, 21, 10, 30, tzinfo=UTC)
    [estimate] = asyncio.run(client.travel(HERE_MILAN, [MARIO], mode, depart_at=depart))  # type: ignore[arg-type]
    q = parse_qs(urlparse(str(fake.last("apple.etas").url)).query)
    assert q["transportType"] == [transport] and q["departureDate"] == ["2026-10-21T10:30:00Z"]
    assert estimate is not None and estimate.distance_m and estimate.minutes >= 1
    assert (estimate.traffic_minutes is not None) == (mode == "drive")  # traffic: driving only
    assert client.can_travel(mode, arrive_by=True)  # type: ignore[arg-type]


def test_apple_travel_in_chunks_of_10_with_missing_routes() -> None:
    fake = FakePlaces()
    client, _, _ = apple(fake)
    destinations = [(45.46 + i / 1000, 9.19) for i in range(12)]
    fake.no_route.add(destinations[3])
    estimates = asyncio.run(client.travel(HERE_MILAN, destinations, "walk"))
    assert len(estimates) == 12 and estimates[3] is None and all(e for i, e in enumerate(estimates) if i != 3)
    assert fake.routes("apple.etas") == ["apple.etas", "apple.etas"]
    assert client.usage_for_travel("walk", 12) == Usage("apple.etas", 2)


def test_apple_search_maps_the_results() -> None:
    fake = FakePlaces()
    client, _, _ = apple(fake)
    results = asyncio.run(client.search("trattoria", near=HERE_MILAN, open_now=False, limit=1, lang="it-IT"))
    [mario] = results
    assert (mario.provider, mario.provider_id, mario.name, mario.category) == (
        "apple", "I7C6F6E2B1A0F9D3E", "Trattoria da Mario", "restaurant"
    )  # fmt: skip
    assert mario.address == "Via Immaginaria 1, 20121 Milano MI, Italia" and (mario.lat, mario.lon) == (
        45.465,
        9.094,
    )
    q = parse_qs(urlparse(str(fake.last("apple.search").url)).query)
    assert q["searchLocation"] == ["45.465000,9.092000"] and q["lang"] == ["it-IT"]
    assert not client.can_filter_open_now  # Apple places have no hours
    assert not client.can_search_rich  # nor a richer search: the caller asks neither of Apple


# --- Google -----------------------------------------------------------------------------------------------


def test_google_field_mask_per_call_kind() -> None:
    fake = FakePlaces()
    client, meter = google(fake)

    async def every_kind() -> None:
        await client.search("trattoria", near=HERE_MILAN, open_now=False, limit=5, lang="it-IT")
        await client.find_id("Trattoria da Mario, Via Immaginaria 1", near=MARIO, lang="it-IT")
        await client.details("ChIJmarioTrattoria0001", reviews=False, lang="it-IT")
        await client.details("ChIJmarioTrattoria0001", reviews=True, lang="it-IT")
        await client.travel(HERE_MILAN, [MARIO], "drive")
        await client.travel(HERE_MILAN, [MARIO], "walk")

    asyncio.run(every_kind())
    masks = [(r, req.headers["x-goog-fieldmask"]) for r, req in fake.requests]
    listing = "id,displayName,formattedAddress,location,primaryType"
    details = (
        listing + ",nationalPhoneNumber,internationalPhoneNumber,websiteUri,regularOpeningHours,"
        "currentOpeningHours,rating,userRatingCount,priceLevel"
    )
    matrix = "originIndex,destinationIndex,status,condition,distanceMeters,duration,staticDuration"
    assert masks == [
        ("google.search", ",".join(f"places.{f}" for f in listing.split(","))),
        ("google.search", "places.id,places.location"),
        ("google.details", details),
        (
            "google.details",
            details
            + ",reviews,editorialSummary,photos,reviewSummary,generativeSummary,"
            + ",".join(ATTRIBUTES),
        ),
        ("google.matrix", matrix),
        ("google.matrix", matrix),
    ]
    assert all(req.headers["x-goog-api-key"] == "AIza-test" for _, req in fake.requests)
    assert [sku for sku, _, _ in meter.calls] == [
        "google.text_search_pro",
        "google.text_search_pro",
        "google.place_details_enterprise",
        "google.place_details_enterprise_atmosphere",
        "google.route_matrix_pro",
        "google.route_matrix_essentials",
    ]
    drive, walk = (json.loads(req.content) for r, req in fake.requests if r == "google.matrix")
    assert drive["routingPreference"] == "TRAFFIC_AWARE" and "routingPreference" not in walk
    assert walk["travelMode"] == "WALK"


def test_google_search_body() -> None:
    fake = FakePlaces()
    client, _ = google(fake)
    results = asyncio.run(client.search("Milano", near=HERE_MILAN, open_now=True, limit=2, lang="it-IT"))
    body = json.loads(fake.last("google.search").content)
    assert body == {
        "textQuery": "Milano", "pageSize": 2, "languageCode": "it", "openNow": True,
        "locationBias": {"circle": {"center": {"latitude": 45.465, "longitude": 9.092}, "radius": 50000.0}},
    }  # fmt: skip
    assert [r.open_now for r in results] == [True, True]  # Google filtered on it
    assert results[1].category == "post_office" and results[0].rating is None  # no rating in a list (cost)


def test_google_details_hours_reviews_and_price() -> None:
    fake = FakePlaces()
    client, _ = google(fake)
    place = asyncio.run(client.details("ChIJmarioTrattoria0001", reviews=True, lang="it-IT"))
    assert place.phone == "+39 02 0000 4567" and place.website == "https://trattoriadamario.example.it/"
    assert (place.rating, place.rating_count, place.price_level) == (4.5, 1240, 2)
    assert place.weekly[0] == Period(1, "12:00", "15:00")
    assert Period(5, "19:00", "01:00") in place.weekly and Period(6, "19:00", "01:00") in place.weekly
    assert not [p for p in place.weekly if p.day == 7]  # closed on Sunday
    assert place.current[0] == Period(3, "12:00", "15:00", date(2026, 10, 21))
    assert place.closed_dates == [date(2026, 10, 26)] and place.hours_note == "Special hours on 2026-10-26."
    assert place.open_now is True and place.summary == "Trattoria milanese con cucina tradizionale."
    assert [(r.author, r.rating, r.source) for r in place.reviews] == [
        ("Reviewer One", 5, "google"), ("Reviewer Two", 3, "google")
    ]  # fmt: skip


def test_google_details_without_reviews_has_none() -> None:
    fake = FakePlaces()
    client, _ = google(fake)
    place = asyncio.run(client.details("ChIJmarioTrattoria0001", reviews=False, lang="it-IT"))
    assert place.reviews == [] and place.summary is None and place.phone


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                         "status": "INVALID_ARGUMENT"}},
         "Google rejected the Maps key (HTTP 400: API key not valid. Please pass a valid API key.)."),
        (403, {"error": {"code": 403, "message": "Places API (New) has not been used in project 1 before",
                         "status": "PERMISSION_DENIED"}},
         "Google Maps refused the call (HTTP 403: Places API (New) has not been used in project 1 before)."),
        (429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
         "Google Maps refused the call: a quota is used up (HTTP 429: Quota exceeded)."),
    ],
)  # fmt: skip
def test_google_errors_name_the_cause(status: int, body: Any, message: str) -> None:
    fake = FakePlaces()
    client, meter = google(fake)
    fake.fail["google.search"] = (status, body)
    with pytest.raises(PlaceProviderError) as err:
        asyncio.run(client.search("x", near=None, open_now=False, limit=1, lang="it-IT"))
    assert err.value.message == message
    assert meter.calls == [("google.text_search_pro", 1, False)]


def test_google_arrival_time_only_for_transit() -> None:
    client, _ = google(FakePlaces())
    assert client.can_travel("transit", arrive_by=True) and not client.can_travel("drive", arrive_by=True)


def test_google_place_ids_are_checked() -> None:
    client, _ = google(FakePlaces())
    with pytest.raises(PlaceProviderError):
        asyncio.run(client.details("../../etc", reviews=False, lang="it-IT"))
    assert google_maps.PLACES.startswith("https://places.googleapis.com/")


# --- Tavily -----------------------------------------------------------------------------------------------


def test_tavily_search_and_extract_credits() -> None:
    fake = FakePlaces()
    meter = Meter()
    client = Tavily(httpx.AsyncClient(transport=httpx.MockTransport(fake)), "tvly-test", meter)

    async def go() -> Any:
        found = await client.search("Trattoria da Mario Milano", max_results=4)
        pages = await client.extract([r.url for r in found])
        return found, pages

    found, pages = asyncio.run(go())
    assert [r.url for r in found][:2] == [
        "https://food.example.com/trattoria-da-mario",
        "https://trattoriadamario.example.it/menu",
    ]
    assert len(pages) == 3  # the forum page failed: no text, no credit
    search_body = json.loads(fake.last("tavily.search").content)
    assert search_body == {
        "query": "Trattoria da Mario Milano",
        "search_depth": "basic",
        "max_results": 4,
        "include_usage": True,
        "include_answer": False,
    }
    extract_body = json.loads(fake.last("tavily.extract").content)
    assert extract_body["extract_depth"] == "basic" and len(extract_body["urls"]) == 4
    assert fake.last("tavily.search").headers["authorization"] == "Bearer tvly-test"
    assert meter.calls == [("tavily.credit", 1, True), ("tavily.credit", 1, True)]
    assert client.usage_for_extract(4) == Usage("tavily.credit", 1) and client.usage_for_extract(6).units == 2


def test_tavily_limits() -> None:
    fake = FakePlaces()
    meter = Meter()
    client = Tavily(httpx.AsyncClient(transport=httpx.MockTransport(fake)), "tvly-test", meter)
    fake.fail["tavily.search"] = (
        432,
        {"detail": {"error": "This request exceeds your plan's set usage limit."}},
    )
    with pytest.raises(PlaceProviderError) as err:
        asyncio.run(client.search("x", max_results=1))
    assert err.value.quota and "HTTP 432" in err.value.message and "usage limit" in err.value.message
    assert meter.calls == [("tavily.credit", 0, False)]
    fake.fail["tavily.search"] = (401, {"detail": {"error": "Unauthorized: missing or invalid API key."}})
    with pytest.raises(PlaceProviderError) as err:
        asyncio.run(client.search("x", max_results=1))
    assert err.value.message == "Tavily rejected the key (HTTP 401)."
