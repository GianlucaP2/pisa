"""The OpenStreetMap fallback against recorded answers (httpx.MockTransport), no network: Photon, Nominatim
and Overpass parsing, the one-request-a-second limiter, the cache, untrusted answers cleaned and capped, error
mapping to the place jobs' reason codes, OpenRouteService travel times with and without the user's key, an
honest User-Agent, and no query, place or coordinate in any log line."""

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from pisa import __version__
from pisa.connectors import osm
from pisa.connectors.openrouteservice import OpenRouteService
from pisa.connectors.osm import Limiter, OsmConfig, OsmPlaces, TtlCache, clean, user_agent
from pisa.domain.errors import PisaError
from pisa.ports.places import PlaceProviderError, ProviderPlace, TravelEstimate, Usage
from pisa.services.place_providers import _job_reason  # pyright: ignore[reportPrivateUsage]
from pisa.settings import Settings
from tests.support.fake_secrets import openrouteservice_key

HERE = (45 + 0.464231, 9 + 0.189512)  # synthetic, built here (a literal has at most 3 decimals)
MARKER = "quokkafarmacia"  # an unusual word: a match anywhere in a log is unambiguous
PHOTON = "https://photon.example.test"
NOMINATIM = "https://nominatim.example.test"
OVERPASS = "https://overpass.example.test/api/interpreter"
ORS = "https://ors.example.test"
CONFIG = OsmConfig("photon", PHOTON, NOMINATIM, OVERPASS, "https://example.org/pisa")


class Meter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, bool]] = []

    async def __call__(self, usage: Usage, ok: bool) -> None:
        self.calls.append((usage.sku, usage.units, ok))


class FakeTime:
    """A monotonic clock that the limiter's sleep moves forward."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


Handler = Any


class Fake:
    """Answers by URL path; records each request and when (on the fake clock) it went out."""

    def __init__(self, time: FakeTime, answers: dict[str, Handler]) -> None:
        self.time = time
        self.answers = answers
        self.requests: list[httpx.Request] = []
        self.at: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.at.append(self.time.now)
        answer = self.answers[request.url.path]
        return answer(request) if callable(answer) else answer  # pyright: ignore[reportReturnType]


def json_answer(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def setup(
    answers: dict[str, Handler], config: OsmConfig = CONFIG, routes: str | None = None
) -> tuple[OsmPlaces, Fake, Meter, FakeTime]:
    """An adapter over a fake transport, with fresh process-wide limiters on the fake clock."""
    osm.reset()
    time = FakeTime()
    for base in (config.photon_url, config.nominatim_url, config.overpass_url):
        osm._LIMITERS[base] = Limiter(clock=time, sleep=time.sleep)  # pyright: ignore[reportPrivateUsage]
        osm._CACHES[base] = TtlCache(clock=time)  # pyright: ignore[reportPrivateUsage]
    fake = Fake(time, answers)
    meter = Meter()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    ors = OpenRouteService(http, routes, meter, base_url=ORS, clock=time) if routes is not None else None
    return OsmPlaces(http, meter, config=config, routes=ors), fake, meter, time


def photon_feature(osm_id: int, name: str, lon: float, lat: float, **props: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"osm_type": "N", "osm_id": osm_id, "osm_key": "amenity", "osm_value": "pharmacy",
                       "name": name, **props},
    }  # fmt: skip


CENTRALE = photon_feature(
    101, "Farmacia Centrale", 9.191, 45.465,
    street="Via Esempio", housenumber="1", postcode="20121", city="Milano", country="Italia",
)  # fmt: skip
SQUARE = {
    **photon_feature(202, "Piazza", 9.19, 45.46),
    "properties": {"osm_type": "W", "osm_id": 202, "osm_value": "square", "name": "Piazza"},
}
PHOTON_ANSWER = {"type": "FeatureCollection", "features": [CENTRALE, SQUARE]}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- parsing and what is sent -----------------------------------------------------------------------------


def test_photon_search_parses_osm_places_and_sends_only_the_query_and_a_rounded_point() -> None:
    adapter, fake, meter, _ = setup({"/api": json_answer(PHOTON_ANSWER)})
    found = run(adapter.search(f"farmacia {MARKER}", near=HERE, open_now=False, limit=5, lang="it-IT"))
    request = fake.requests[0]
    assert request.method == "GET" and request.url.host == "photon.example.test"
    assert dict(request.url.params) == {
        "q": f"farmacia {MARKER}", "limit": "5", "lang": "it", "lat": "45.464", "lon": "9.190",
    }  # fmt: skip
    assert request.headers["user-agent"] == f"PISA/{__version__} (+https://example.org/pisa)"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    first = found[0]
    assert (first.provider, first.provider_id, first.name) == ("osm", "node/101", "Farmacia Centrale")
    assert (first.lat, first.lon) == (45.465, 9.191)  # GeoJSON is [lon, lat]
    assert first.address == "Via Esempio 1, 20121 Milano, Italia" and first.category == "pharmacy"
    assert first.attribution == "© OpenStreetMap contributors"
    assert found[1].provider_id == "way/202" and found[1].category == "square"
    addresses = run(adapter.geocode(f"via esempio {MARKER}", near=HERE, lang="it"))
    assert [p.provider_id for p in addresses] == ["node/101"]  # the square names no street
    assert meter.calls == [("osm.photon", 1, True)] * 2
    assert adapter.usage_for_search() == Usage("osm.photon")
    assert not adapter.can_filter_open_now and not adapter.can_search_rich


def test_nominatim_is_the_alternative_geocoder_with_a_viewbox_bias() -> None:
    answer = [
        {"osm_type": "node", "osm_id": 7, "lat": "45.4650", "lon": "9.1900", "name": "Farmacia Due",
         "type": "pharmacy", "address": {"road": "Via Prova", "house_number": "9", "city": "Milano"}},
        {"osm_type": "relation", "osm_id": 8, "lat": "not a number", "lon": "9.19", "name": "Broken"},
    ]  # fmt: skip
    config = OsmConfig("nominatim", PHOTON, NOMINATIM, OVERPASS, "https://example.org/pisa")
    adapter, fake, meter, _ = setup({"/search": json_answer(answer)}, config)
    found = run(adapter.search("farmacia", near=HERE, open_now=False, limit=3, lang="en-US"))
    params = dict(fake.requests[0].url.params)
    assert fake.requests[0].url.host == "nominatim.example.test"
    assert params["format"] == "jsonv2" and params["accept-language"] == "en" and params["limit"] == "3"
    assert params["viewbox"] == "8.940,45.714,9.440,45.214"  # around the point rounded to 3 decimals
    assert "email" not in params
    assert [(p.provider_id, p.name, p.address) for p in found] == [
        ("node/7", "Farmacia Due", "Via Prova 9, Milano")
    ]
    assert fake.requests[0].headers["user-agent"] == f"PISA/{__version__} (+https://example.org/pisa)"
    assert meter.calls == [("osm.nominatim", 1, True)]


def test_overpass_nearby_asks_a_fixed_kind_in_a_bounded_radius_and_caps_the_results() -> None:
    one = {
        "type": "node", "id": 1, "lat": 45.464, "lon": 9.19,
        "tags": {"amenity": "pharmacy", "name": "Farmacia Uno", "phone": "+39 02 0000 0000",
                 "website": "https://farmacia.example.org", "addr:street": "Via Prova",
                 "addr:housenumber": "3"},
    }  # fmt: skip
    two = {
        "type": "way", "id": 2, "center": {"lat": 45.466, "lon": 9.191},
        "tags": {"amenity": "pharmacy", "brand": "Catena", "website": "javascript:alert(1)"},
    }  # fmt: skip
    nameless = {"type": "node", "id": 3, "lat": 45.0, "lon": 9.0, "tags": {"amenity": "pharmacy"}}
    more = [
        {"type": "node", "id": 100 + i, "lat": 45.46, "lon": 9.19, "tags": {"name": f"P{i}"}}
        for i in range(30)
    ]
    elements = [one, two, nameless, *more]
    adapter, fake, meter, _ = setup({"/api/interpreter": json_answer({"elements": elements})})
    found = run(adapter.nearby("pharmacy", near=HERE, radius_m=50_000, limit=99))
    request = fake.requests[0]
    assert request.method == "POST"
    query = parse_qs(request.content.decode())["data"][0]
    assert query == (
        '[out:json][timeout:8];nwr["amenity"="pharmacy"](around:5000,45.464,9.190);out center tags 20;'
    )
    assert len(found) == 20  # the cap, whatever was asked or sent
    one, two = found[0], found[1]
    assert (one.provider_id, one.phone, one.website, one.address) == (
        "node/1",
        "+39 02 0000 0000",
        "https://farmacia.example.org",
        "Via Prova 3",
    )
    assert (two.provider_id, two.name, two.lat, two.website) == ("way/2", "Catena", 45.466, None)
    assert meter.calls == [("osm.overpass", 1, True)] and adapter.usage_for_nearby() == Usage("osm.overpass")
    with pytest.raises(PlaceProviderError):  # a kind outside the list never reaches the query
        run(adapter.nearby('pharmacy"](around:1,0,0);out;', near=HERE, radius_m=500, limit=5))
    assert len(fake.requests) == 1 and adapter.can_find_nearby("fuel") and not adapter.can_find_nearby("x")


def test_untrusted_answers_are_cleaned_clipped_and_capped() -> None:
    sneaky = "Farm‮acia​ \x07Bella⁦" + "x" * 500
    features = [photon_feature(i + 1, sneaky, 9.19, 45.46) for i in range(25)]
    features.append(photon_feature(999, "Off the map", 9.19, 123.0))  # an impossible latitude: dropped
    adapter, _, _, _ = setup({"/api": json_answer({"features": features})})
    found = run(adapter.search("farmacia", near=None, open_now=False, limit=50, lang="en"))
    assert len(found) == 10  # the search cap
    name = found[0].name
    assert name.startswith("Farmacia Bellaxxx") and len(name) == 120
    assert not any(c in name for c in "‮​\x07⁦")
    assert clean("  a\tb\n\nc  ", 10) == "a b c" and clean(42, 10) is None and clean("​", 5) is None


# --- etiquette: the limiter and the cache -------------------------------------------------------------------


def test_the_limiter_spaces_parallel_requests_one_second_apart_per_backend() -> None:
    adapter, fake, _, time = setup(
        {"/api": json_answer(PHOTON_ANSWER), "/api/interpreter": json_answer({"elements": []})}
    )

    async def many() -> None:
        await asyncio.gather(
            *(adapter.search(f"q{i}", near=None, open_now=False, limit=1, lang="en") for i in range(3)),
            adapter.nearby("atm", near=HERE, radius_m=500, limit=5),
        )

    run(many())
    photon = [at for at, r in zip(fake.at, fake.requests, strict=True) if r.url.host == "photon.example.test"]
    assert photon == [1000.0, 1001.0, 1002.0]  # one a second, never two together
    # only the 2nd and 3rd searches waited for their turn; Overpass, another backend, didn't wait
    assert len(time.slept) == 2 and len(fake.requests) == 4
    limiter = Limiter(clock=time, sleep=time.sleep)
    time.now += 10
    run(limiter.wait())
    run(limiter.wait())
    assert time.slept[-1] == 1.0


def test_a_repeated_search_is_answered_from_the_cache() -> None:
    adapter, fake, meter, time = setup({"/api": json_answer(PHOTON_ANSWER)})

    def search(query: str, near: tuple[float, float] | None, lang: str = "it") -> list[ProviderPlace]:
        return run(adapter.search(query, near=near, open_now=False, limit=5, lang=lang))

    first = search("Farmacia Centrale", HERE)
    first[0].name = "changed by a caller"
    nearby = (HERE[0] - 0.0003, HERE[1] + 0.0017)
    again = search("  farmacia   CENTRALE ", nearby)  # the same words, within the same ~1 km
    assert len(fake.requests) == 1 and again[0].name == "Farmacia Centrale"
    search("Farmacia Centrale", HERE, lang="en")  # another language: asked again
    search("Farmacia Centrale", (45.6, 9.3))  # elsewhere: asked again
    assert len(fake.requests) == 3 and len(meter.calls) == 3  # a hit is no call, so nothing metered
    time.now += osm.CACHE_TTL_S + 1
    search("Farmacia Centrale", HERE)
    assert len(fake.requests) == 4  # expired
    cache = TtlCache(ttl=60, size=2, clock=time)
    for key in ("a", "b", "c"):
        cache.put(key, [])
    assert len(cache) == 2 and cache.get("a") is None and cache.get("c") == []


def test_a_repeated_nearby_search_is_answered_from_the_cache() -> None:
    adapter, fake, _, _ = setup({"/api/interpreter": json_answer({"elements": []})})
    run(adapter.nearby("fuel", near=HERE, radius_m=800, limit=5))
    same = (HERE[0] - 0.00004, HERE[1] + 0.0001)  # the same rounded point
    run(adapter.nearby("fuel", near=same, radius_m=800, limit=5))
    assert len(fake.requests) == 1


# --- errors ------------------------------------------------------------------------------------------------


def _reason(adapter: OsmPlaces) -> tuple[str, PlaceProviderError]:
    with pytest.raises(PlaceProviderError) as caught:
        run(adapter.search("farmacia", near=None, open_now=False, limit=5, lang="en"))
    return _job_reason([caught.value], [], capped=False), caught.value


def _raises(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx.Response(429, text="slow down"), "quota"),
        (httpx.Response(503, text=f"no results for {MARKER}"), "provider_error:503"),
        (
            httpx.Response(302, headers={"location": "https://elsewhere.example.test/api"}),
            "provider_error:302",
        ),
        (_raises(httpx.ReadTimeout("slow")), "timeout"),
        (_raises(httpx.ConnectError("refused")), "unreachable"),
        (httpx.Response(200, text="<html>not json</html>"), "provider_error"),
        (httpx.Response(200, content=b"[" + b"0," * osm.BODY_MAX + b"0]"), "provider_error"),
    ],
)
def test_failures_map_to_the_place_jobs_reason_codes_and_follow_no_redirect(
    answer: Handler, reason: str
) -> None:
    adapter, fake, meter, _ = setup({"/api": answer})
    code, err = _reason(adapter)
    assert code == reason and (err.quota is (reason == "quota"))
    assert len(fake.requests) == 1  # a redirect is never followed
    assert meter.calls[-1][2] is (reason == "provider_error" and "not JSON" in err.message)


# --- OpenRouteService ------------------------------------------------------------------------------------


def test_without_an_openrouteservice_key_travel_is_not_configured_never_a_failure() -> None:
    adapter, fake, _, _ = setup({})
    assert not adapter.can_travel("walk", arrive_by=False)
    with pytest.raises(PisaError) as caught:
        run(adapter.travel(HERE, [(45.47, 9.2)], "walk"))
    assert caught.value.code == "place_provider_not_configured"
    assert caught.value.detail == "provider_not_configured"
    blank = OpenRouteService(httpx.AsyncClient(transport=httpx.MockTransport(fake)), "  ", Meter())
    assert not blank.can_travel("drive", arrive_by=False)
    with pytest.raises(PisaError) as again:
        run(blank.travel(HERE, [(45.47, 9.2)], "drive"))
    assert again.value.detail == "provider_not_configured" and fake.requests == []


def test_openrouteservice_matrix_with_the_key_in_a_header_and_no_transit() -> None:
    key = openrouteservice_key("a1b2c3d4e5f60718293a4b5c6d7e8f90")
    answer = {"durations": [[600.2, None, 59.0]], "distances": [[1234.4, None, 80.0]]}
    adapter, fake, meter, _ = setup({"/v2/matrix/foot-walking": json_answer(answer)}, routes=key)
    dests = [(45.47, 9.2), (45.48, 9.21), (45.465, 9.19)]
    assert adapter.can_travel("walk", arrive_by=False) and adapter.can_travel("bike", arrive_by=False)
    assert not adapter.can_travel("transit", arrive_by=False)  # ORS has no public transport: unsupported
    assert not adapter.can_travel("drive", arrive_by=True)
    got = run(adapter.travel(HERE, dests, "walk"))
    assert got == [TravelEstimate(11, None, 1234), None, TravelEstimate(1, None, 80)]
    request = fake.requests[0]
    assert request.headers["authorization"] == key and key not in str(request.url)
    body = json.loads(request.content)
    assert body["locations"][0] == [round(HERE[1], 5), round(HERE[0], 5)] and body["sources"] == [0]
    assert body["destinations"] == [1, 2, 3]
    assert meter.calls == [("ors.matrix", 1, True)]
    assert adapter.usage_for_travel("walk", 30) == Usage("ors.matrix", 2)
    with pytest.raises(PisaError) as caught:
        run(adapter.travel(HERE, dests, "transit"))
    assert caught.value.detail == "unsupported" and len(fake.requests) == 1


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "provider_error:401"), (403, "provider_error:403"), (429, "quota"), (500, "provider_error:500")],
)
def test_openrouteservice_errors_map_to_reason_codes(status: int, reason: str) -> None:
    key = openrouteservice_key("0" * 32)
    adapter, fake, _, _ = setup({"/v2/matrix/driving-car": httpx.Response(status, text=key)}, routes=key)
    with pytest.raises(PlaceProviderError) as caught:
        run(adapter.travel(HERE, [(45.47, 9.2)], "drive"))
    assert _job_reason([caught.value], [], capped=False) == reason and key not in caught.value.message
    if status == 429:  # backing off: the next call doesn't go out
        with pytest.raises(PlaceProviderError):
            run(adapter.travel(HERE, [(45.47, 9.2)], "drive"))
        assert len(fake.requests) == 1


# --- privacy ---------------------------------------------------------------------------------------------


def test_the_user_agent_names_pisa_and_its_version_and_nothing_personal() -> None:
    assert user_agent() == f"PISA/{__version__} (self-hosted personal assistant)"
    assert user_agent("https://example.org/pisa") == f"PISA/{__version__} (+https://example.org/pisa)"
    settings = Settings(timezone="Europe/Rome")  # pyright: ignore[reportCallIssue]
    assert (settings.osm_enabled, settings.osm_geocoder, settings.osm_project_url) == (True, "photon", "")
    assert settings.osm_photon_url == osm.PHOTON_URL and settings.osm_overpass_url == osm.OVERPASS_URL
    assert OsmConfig.from_settings(settings) == OsmConfig()
    for bad in ("mailto:someone@example.org", "https://example.org/?who=me", "https://user@example.org"):
        with pytest.raises(ValidationError):
            Settings(timezone="Europe/Rome", osm_project_url=bad)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        Settings(timezone="Europe/Rome", osm_photon_url="ftp://photon.lan")  # pyright: ignore[reportCallIssue]
    own = Settings(timezone="Europe/Rome", osm_photon_url="http://photon.lan:2322/")  # pyright: ignore[reportCallIssue]
    assert own.osm_photon_url == "http://photon.lan:2322"  # self-hosted on the operator's network


def test_no_query_place_coordinate_or_key_in_any_log_line() -> None:
    from tests.test_logs_hold_no_user_text import debug_logs

    key = openrouteservice_key("feedfacefeedfacefeedfacefeedface")
    echo = httpx.Response(500, json={"error": f"nothing for {MARKER} at {HERE[0]},{HERE[1]}", "key": key})
    with debug_logs() as (raw, out):
        adapter, _, _, _ = setup(
            {"/api": json_answer(PHOTON_ANSWER), "/v2/matrix/driving-car": echo}, routes=key
        )
        run(adapter.search(f"farmacia {MARKER}", near=HERE, open_now=False, limit=5, lang="it"))
        run(adapter.search(f"farmacia {MARKER}", near=HERE, open_now=False, limit=5, lang="it"))  # cached
        failing, _, _, _ = setup({"/api": echo})
        with pytest.raises(PlaceProviderError):
            run(failing.search(f"farmacia {MARKER}", near=HERE, open_now=False, limit=5, lang="it"))
        with pytest.raises(PlaceProviderError):
            run(adapter.travel(HERE, [(45.47, 9.2)], "drive"))
    written = out.getvalue()
    everything = " ".join(raw) + written
    for private in (MARKER, "Farmacia", "Via Esempio", "45.46", "9.18", "9.19", key):
        assert private not in everything, private
    lines = [json.loads(line) for line in written.splitlines() if '"external_failure"' in line]
    assert [(e["provider"], e["op"], e["status"]) for e in lines] == [
        ("osm_photon", "GET /api", 500),
        ("openrouteservice", "POST /v2/matrix/driving-car", 500),
    ]


def test_a_bounded_search_sends_a_box_to_photon_and_a_bounded_viewbox_to_nominatim() -> None:
    """§31 p1: "near here" is a bound, not a bias. `within_m` sends Photon's bbox (W,S,E,N)
    around the rounded point; Nominatim gets its viewbox with bounded=1. Without it, nothing changes."""
    adapter, fake, _, _ = setup({"/api": json_answer(PHOTON_ANSWER)})
    run(adapter.search("ristorante", near=HERE, open_now=False, limit=5, lang="it", within_m=5_000))
    params = dict(fake.requests[0].url.params)
    west, south, east, north = (float(v) for v in params["bbox"].split(","))
    assert west < 9.190 < east and south < 45.464 < north
    assert round(north - south, 2) == 0.09  # 5 km each way: about 0.045° of latitude
    run(adapter.search("ristorante", near=HERE, open_now=False, limit=5, lang="it"))
    assert "bbox" not in dict(fake.requests[1].url.params)
    config = OsmConfig("nominatim", PHOTON, NOMINATIM, OVERPASS, "https://example.org/pisa")
    adapter, fake, _, _ = setup({"/search": json_answer([])}, config)
    run(adapter.search("ristorante", near=HERE, open_now=False, limit=5, lang="it", within_m=25_000))
    params = dict(fake.requests[0].url.params)
    assert params["bounded"] == "1" and len(params["viewbox"].split(",")) == 4


def test_a_bounded_search_is_cached_by_its_box() -> None:
    """E1 BX-1: two points of one ~1 km cache area send different boxes: each is asked, never the other's
    places; the same box again is the cached answer."""
    adapter, fake, _, _ = setup({"/api": json_answer(PHOTON_ANSWER)})
    there = (HERE[0] - 0.004, HERE[1])  # same 2-decimal area, a box ~450 m south
    assert osm.rounded(there, osm.CACHE_AREA_DECIMALS) == osm.rounded(HERE, osm.CACHE_AREA_DECIMALS)
    assert osm.box(there, 5_000) != osm.box(HERE, 5_000)
    for near in (HERE, there, HERE):
        run(adapter.search("ristorante", near=near, open_now=False, limit=5, lang="it", within_m=5_000))
    assert len(fake.requests) == 2
