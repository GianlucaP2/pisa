"""Places in the order phone → Google (the user's key) → OpenStreetMap, server side (v0.1.4, contract 2.3).

- the phone's delegate (`pisa.maps`): a request in the turn's run with a fresh requestId, the phone's answer
  matched by it on the same session and device; an answer that is not ok, none in time, one for another id or
  from another device: the next provider is asked; an answer out of the contract's bounds is `invalid_frame`;
- the order, each step present or absent: the phone (a turn on an app that granted it), the user's order
  (Google with their key), OpenStreetMap (no key; PISA_OSM_ENABLED, review mode), nothing set up;
- travel times: phone → Google → OpenRouteService (the user's key), transit only from the phone or Google;
- OpenStreetMap's places keep their id (`places.osm_id`) and carry their licence's credit to the client.

Synthetic data only: Testville, made-up names, the 555 number range."""

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from pisa_conformance.validate import client_frame_errors
from pydantic import SecretStr

from pisa.api.live import HelloFrame, negotiate
from pisa.api.runs import HTTP_NEGOTIATED
from pisa.api.server import features, served
from pisa.connectors import osm
from pisa.connectors.osm import OsmConfig
from pisa.db.pool import open_pool
from pisa.domain import phone_maps as rules
from pisa.domain.errors import PisaError
from pisa.domain.places import OSM_ATTRIBUTION, OSM_ATTRIBUTION_URL
from pisa.infra.keys import KeyStore
from pisa.ports.places import PlaceProviderError, TravelEstimate
from pisa.services import a2ui
from pisa.services import phone_maps as delegate_module
from pisa.services.phone_maps import PhoneMaps, PhoneMapsAdapter, using
from pisa.services.place_providers import PlaceProviderService
from pisa.services.places import PlacesService
from pisa.services.session import FrameError, Negotiated
from tests.conftest import Clock, dsn
from tests.owner import owner_at
from tests.places_fake import FakePlaces
from tests.support.fake_secrets import google_api_key, openrouteservice_key
from tests.test_live_v2_frames import FULL, checked, hello, session

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "v2" / "fixtures" / "gaps"
OTHER_DEVICE = "00000000-0000-4000-8000-0000000000d2"
TESTVILLE = (45.001, 9.003)  # synthetic
FARMACIA = {
    "id": "I5E0A1B2C3D4F5061", "name": "Farmacia Esempio", "address": "Via Esempio 1, Testville",
    "lat": 45.001, "lon": 9.003, "kind": "pharmacy", "distanceMeters": 420, "openNow": True,
    "phone": "+15550100100", "url": "https://pharmacy.example.org",
}  # fmt: skip
CLOSED = {"id": "I00000000000000AA", "name": "Farmacia Chiusa", "lat": 45.003, "lon": 9.001, "openNow": False}
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
TZ = "Europe/Rome"
PHOTON = "https://photon.w2a.example.test"
ORS = "https://ors.w2a.example.test"
OSM_CONFIG = OsmConfig("photon", PHOTON, "https://nominatim.w2a.example.test",
                       "https://overpass.w2a.example.test/api/interpreter", "")  # fmt: skip
PHOTON_ANSWER = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [9.011, 45.011]},
        "properties": {"osm_type": "N", "osm_id": 4242, "osm_key": "amenity", "osm_value": "pharmacy",
                       "name": "Farmacia Aperta", "street": "Via Prova", "housenumber": "2",
                       "city": "Testville"},
    }],
}  # fmt: skip


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the contract's bounds ----------------------------------------------------------------------------------


def _fixtures(kind: str) -> list[Path]:
    return sorted((FIXTURES / kind).glob("maps_result_*.json"))


@pytest.mark.parametrize("path", _fixtures("valid"), ids=lambda p: p.name)
def test_a_result_the_contract_allows_is_taken(path: Path) -> None:
    value = json.loads(path.read_text())["frame"]["value"]
    got = rules.parse_result(value)
    assert got.request_id == value["requestId"].lower() and got.ok is value["ok"]


@pytest.mark.parametrize("path", _fixtures("invalid"), ids=lambda p: p.name)
def test_a_result_out_of_the_contract_s_bounds_is_refused_whole(path: Path) -> None:
    frame = json.loads(path.read_text())["frame"]
    assert client_frame_errors(frame)  # the kit refuses it too
    with pytest.raises(rules.Refused) as refused:
        rules.parse_result(frame["value"])
    for quoted in ("Farmacia", "Esempio", "45.", "9.0", "http://"):  # a refusal never quotes the value
        assert quoted not in str(refused.value)


def test_more_bounds_the_server_checks_itself() -> None:
    ok = {"requestId": str(uuid.uuid4()), "ok": True}
    for bad in (
        ok | {"places": [FARMACIA | {"lat": True}]},  # a boolean is no number
        ok | {"places": [FARMACIA | {"lat": float("nan")}]},
        ok | {"places": [FARMACIA | {"id": "I5E0\n"}]},  # `$` that lets a trailing newline through: not here
        ok | {"places": [FARMACIA | {"phone": "+15550100100\n"}]},
        ok | {"places": [FARMACIA | {"address": "Via‮Esempio"}]},  # a bidi override
        ok | {"places": [FARMACIA | {"distanceMeters": 1.5}]},
        ok | {"eta": {"seconds": 60, "distanceMeters": 10, "mode": "walk", "departAt": "domani"}},
        ok | {"places": [FARMACIA], "eta": {"seconds": 60, "distanceMeters": 10, "mode": "walk"}},
        {"requestId": "not-a-uuid", "ok": False, "reason": "denied"},
    ):
        with pytest.raises(rules.Refused):
            rules.parse_result(bad)
    eta = rules.parse_result(ok | {"eta": {"seconds": 61, "distanceMeters": 90, "mode": "walk",
                                            "departAt": "2026-11-24T10:00:00Z"}})  # fmt: skip
    assert eta.eta == rules.MapsEta(61, 90, "walk", "2026-11-24T10:00:00Z")


def test_what_pisa_asks_is_clean_bounded_and_a_valid_server_event() -> None:
    value = rules.search_value(str(uuid.uuid4()), "  farmacia‮\n aperta  " + "x" * 300, TESTVILLE, 25)
    assert value is not None and value["limit"] == rules.PLACES_MAX and len(value["query"]) == rules.QUERY_MAX
    assert value["query"].startswith("farmacia aperta ") and value["near"] == {"lat": 45.001, "lon": 9.003}
    assert rules.search_value(str(uuid.uuid4()), "⁦\n", None, 5) is None


# --- the delegate on a session ------------------------------------------------------------------------------


# How long a request waits for the phone here. A test whose phone answers never reaches its wait (the answer
# comes when the test sends it), so the wait is long: a short one ran out on a loaded machine (`-n 10`) while
# the kit checked the stream between the request and the answer. Only a test that expects no answer is brief.
ANSWER_WAIT_S = 60.0
NO_ANSWER_WAIT_S = 0.2


def _session(
    negotiated: Negotiated = FULL, device: str | None = None, *, wait_s: float = ANSWER_WAIT_S
) -> Any:
    s = session(negotiated)
    if device is not None:  # another paired phone's socket
        s.device = dataclasses.replace(s.device, id=uuid.UUID(device))
        s.phone_maps = PhoneMaps(device, s._maps_request)  # pyright: ignore[reportPrivateUsage]
    s.live.attach(lambda _text: None)
    s.phone_maps._wait_s = wait_s  # pyright: ignore[reportPrivateUsage]
    return s


def _asked(s: Any, name: str = rules.SEARCH) -> dict[str, Any]:
    """The latest request the session sent, after the kit's checks on its whole stream."""
    frames = checked(s.live.kept(), 1)
    return [f["event"]["value"] for f in frames if f["event"].get("name") == name][-1]


async def _sent(s: Any, name: str = rules.SEARCH) -> dict[str, Any]:
    """The next request once it is out (the delegate's task runs up to its wait)."""

    def count() -> int:
        return sum(f.get("event", {}).get("name") == name for f in s.live.kept())

    before = count()
    for _ in range(50):
        await asyncio.sleep(0)
        if count() > before:
            break
    return _asked(s, name)


async def _search(s: Any, answer: Any = None, *, to: Any = None) -> Any:
    """Ask the phone; `answer(requestId)` is the phone's result (sent to `to`, else to the same session)."""
    task = asyncio.create_task(s.phone_maps.search("farmacia", TESTVILLE, 5))
    asked = await _sent(s)
    if answer is not None:
        await (to or s).custom(rules.RESULT, answer(asked["requestId"]))
    return await task, asked


def test_the_delegate_goes_only_to_a_socket_that_granted_pisa_maps() -> None:
    assert session().turn_senders(None)["phone_maps"] is not None
    bare = Negotiated([a2ui.CATALOG_ID], [0, 1], False, {"pisa.notice": 1})
    assert session(bare).turn_senders(None)["phone_maps"] is None  # not granted
    assert session(HTTP_NEGOTIATED).turn_senders(None)["phone_maps"] is None  # a REST-only turn
    plain = HelloFrame.model_validate(hello(extensions={"pisa.maps": 1, "pisa.notice": 1}))
    on, off = negotiate(plain), negotiate(plain, phone_maps=False)
    assert isinstance(on, Negotiated) and on.extensions == {"pisa.maps": 1, "pisa.notice": 1}
    assert isinstance(off, Negotiated) and off.extensions == {"pisa.notice": 1}  # PISA_PHONE_MAPS_ENABLED=0
    ids = [f["id"] for f in features(frozenset(), phone_maps=False)]
    assert "maps.phone" in [f["id"] for f in served()] and "maps.phone" not in ids


def test_the_phone_s_places_answer_its_own_request() -> None:
    s = _session()
    got, asked = run(_search(s, lambda rid: {"requestId": rid.upper(), "ok": True, "places": [FARMACIA]}))
    assert uuid.UUID(asked["requestId"]) and asked["query"] == "farmacia" and asked["limit"] == 5
    assert [p.name for p in got] == ["Farmacia Esempio"] and s.phone_maps.pending == 0
    second = run(_search(s, lambda rid: {"requestId": rid, "ok": False, "reason": "denied"}))[1]
    assert second["requestId"] != asked["requestId"]  # a fresh id each time


@pytest.mark.parametrize("reason", rules.REASONS)
def test_a_phone_that_says_no_gives_its_reason(reason: str) -> None:
    s = _session()
    got, _ = run(_search(s, lambda rid: {"requestId": rid, "ok": False, "reason": reason}))
    assert got == reason


def test_no_answer_in_time_is_a_timeout_and_a_late_one_is_dropped() -> None:
    s = _session(wait_s=NO_ANSWER_WAIT_S)

    async def late() -> tuple[Any, bool]:
        got, asked = await _search(s)
        result = rules.parse_result({"requestId": asked["requestId"], "ok": True, "places": [FARMACIA]})
        return got, s.phone_maps.answer(result, str(s.device.id))

    got, taken = run(late())
    assert got == "timeout" and taken is False and s.phone_maps.pending == 0


def test_an_answer_for_another_request_or_from_another_device_is_dropped() -> None:
    s = _session(wait_s=NO_ANSWER_WAIT_S)
    got, _ = run(_search(s, lambda _rid: {"requestId": str(uuid.uuid4()), "ok": True, "places": [FARMACIA]}))
    assert got == "timeout"  # the unknown id answered nothing
    other = _session(device=OTHER_DEVICE, wait_s=NO_ANSWER_WAIT_S)
    got, _ = run(_search(s, lambda rid: {"requestId": rid, "ok": True, "places": [FARMACIA]}, to=other))
    assert got == "timeout"  # the other device's socket has no such request

    async def direct() -> bool:  # and one handed to this delegate with another device's id
        task = asyncio.create_task(s.phone_maps.search("farmacia", None, 5))
        rid = (await _sent(s))["requestId"]
        taken = s.phone_maps.answer(rules.parse_result({"requestId": rid, "ok": True, "places": [FARMACIA]}),
                                    OTHER_DEVICE)  # fmt: skip
        assert await task == "timeout"
        return taken

    assert run(direct()) is False


def test_an_out_of_bounds_answer_is_an_invalid_frame_and_needs_the_extension() -> None:
    s = _session()
    with pytest.raises(FrameError):
        run(
            s.custom(
                rules.RESULT, {"requestId": str(uuid.uuid4()), "ok": True, "places": [FARMACIA | {"x": 1}]}
            )
        )
    bare = _session(Negotiated([a2ui.CATALOG_ID], [0, 1], False, {"pisa.notice": 1}))
    with pytest.raises(FrameError):
        run(bare.custom(rules.RESULT, {"requestId": str(uuid.uuid4()), "ok": False, "reason": "denied"}))


def test_places_for_an_eta_is_no_eta_and_an_eta_is_minutes_rounded_up() -> None:
    s = _session()
    adapter = PhoneMapsAdapter(s.phone_maps, lambda: NOW)

    async def eta(answer: Any) -> Any:
        task = asyncio.create_task(adapter.travel(TESTVILLE, [(45.01, 9.01)], "walk"))
        asked = await _sent(s, rules.ETA)
        assert asked["mode"] == "walk" and asked["to"] == {"lat": 45.01, "lon": 9.01}
        await s.custom(rules.RESULT, answer(asked["requestId"]))
        return await task

    got = run(eta(lambda rid: {"requestId": rid, "ok": True,
                               "eta": {"seconds": 61, "distanceMeters": 900, "mode": "walk"}}))  # fmt: skip
    assert got == [TravelEstimate(2, None, 900)]
    with pytest.raises(PlaceProviderError) as failed:
        run(eta(lambda rid: {"requestId": rid, "ok": True, "places": [FARMACIA]}))
    assert failed.value.message == "the phone's maps: unsupported"
    assert not adapter.can_travel("walk", arrive_by=True) and adapter.can_travel("transit", arrive_by=False)


def test_the_adapter_keeps_only_open_places_when_asked_and_no_results_is_empty() -> None:
    s = _session()
    adapter = PhoneMapsAdapter(s.phone_maps)

    async def search(answer: Any, open_now: bool) -> Any:
        task = asyncio.create_task(
            adapter.search("farmacia", near=None, open_now=open_now, limit=5, lang="it")
        )
        await s.custom(rules.RESULT, answer((await _sent(s))["requestId"]))
        return await task

    found = run(search(lambda rid: {"requestId": rid, "ok": True, "places": [FARMACIA, CLOSED]}, True))
    assert [(p.provider, p.provider_id, p.category, p.website) for p in found] == [
        ("phone", "I5E0A1B2C3D4F5061", "pharmacy", "https://pharmacy.example.org")
    ]
    assert run(search(lambda rid: {"requestId": rid, "ok": False, "reason": "no_results"}, False)) == []


# --- the order ----------------------------------------------------------------------------------------------


class Phone(PhoneMaps):
    """The phone, scripted: its answer to every search and ETA (a reason, or what it found)."""

    def __init__(self, places: list[dict[str, Any]] | str, eta: int | str = "denied") -> None:
        super().__init__(str(uuid.uuid4()), lambda _n, _v: True)
        self.places, self.eta_s, self.asked = places, eta, 0

    async def search(self, query: str, near: Any, limit: int) -> Any:
        self.asked += 1
        return self.places if isinstance(self.places, str) else [rules._place(p) for p in self.places]  # pyright: ignore[reportPrivateUsage]

    async def eta(self, origin: Any, destination: Any, mode: str) -> Any:
        self.asked += 1
        return self.eta_s if isinstance(self.eta_s, str) else rules.MapsEta(self.eta_s, 1000, mode)


def _web(fake: FakePlaces, seen: list[str], nominatim: list[dict[str, Any]] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "nominatim.w2a.example.test" and nominatim is not None:
            return httpx.Response(200, json=nominatim)
        if request.url.host == "photon.w2a.example.test":
            bbox = request.url.params.get("bbox")  # a bound, as Photon does: only features inside
            if bbox:
                west, south, east, north = (float(v) for v in bbox.split(","))
                inside = [f for f in PHOTON_ANSWER["features"]
                          if west <= f["geometry"]["coordinates"][0] <= east
                          and south <= f["geometry"]["coordinates"][1] <= north]  # fmt: skip
                return httpx.Response(200, json={**PHOTON_ANSWER, "features": inside})
            return httpx.Response(200, json=PHOTON_ANSWER)
        if request.url.host == "ors.w2a.example.test":
            return httpx.Response(200, json={"durations": [[600.0]], "distances": [[800.0]]})
        return fake(request)

    return handler


@asynccontextmanager
async def maps_env(
    db: psycopg.Connection, secrets_key: str, *, google: bool = False, ors: bool = False, on: bool = True,
    disabled: bool = False, nominatim: list[dict[str, Any]] | None = None,
) -> AsyncIterator[tuple[PlaceProviderService, PlacesService, list[str]]]:  # fmt: skip
    """The provider service over fakes: Google (FakePlaces), Photon and OpenRouteService; `on`: OSM;
    `nominatim`: OSM searches Nominatim instead of Photon, which answers that."""
    osm.reset()
    pool = await open_pool(dsn(db), max_size=4)
    await pool.wait()
    seen: list[str] = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(_web(FakePlaces(), seen, nominatim)))
    config = dataclasses.replace(OSM_CONFIG, geocoder="nominatim") if nominatim is not None else OSM_CONFIG
    try:
        clock = Clock(NOW)
        providers = PlaceProviderService(
            pool, KeyStore(SecretStr(secrets_key)), http, team_id="EXAMPLETEAM", owner=owner_at(TZ),
            clock=clock, disabled=disabled, osm=config if on else None, ors_url=ORS,
        )  # fmt: skip
        for provider in ("google", "openrouteservice"):  # what an earlier env of the same test stored
            await providers.delete_key(provider)
        if google:
            await providers.set_key("google", google_api_key("-test-w2a-0000"))
        if ors:
            await providers.set_key("openrouteservice", openrouteservice_key("00112233445566778899aabbccdd"))
        places = PlacesService(pool, providers, object(), owner=owner_at(TZ), clock=clock)  # type: ignore[arg-type]
        yield providers, places, seen
    finally:
        await http.aclose()
        await pool.close()


def test_the_order_is_phone_then_the_user_s_then_the_free_fallback(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with maps_env(db, secrets_key) as (providers, _, _):
            assert providers.order_for("search", ["apple", "google"]) == ["apple", "google", "osm"]
            assert providers.order_for("routes", ["google"]) == ["google", "openrouteservice"]
            with using(Phone([])):
                assert providers.order_for("search", ["apple", "google"])[0] == "phone"
                assert providers.order_for("routes", ["google"]) == ["phone", "google", "openrouteservice"]
                assert providers.order_for("details", ["google"]) == ["google"]  # the phone: search and ETA
        async with maps_env(db, secrets_key, on=False) as (providers, _, _):
            assert providers.order_for("search", ["google"]) == ["google"]  # PISA_OSM_ENABLED=0
        async with maps_env(db, secrets_key, disabled=True) as (providers, _, _):
            with using(Phone([])):
                assert providers.order_for("search", ["google"])[:1] == ["google"]  # review mode: no phone
                assert await providers.adapter("osm") is None and await providers.adapter("phone") is None

    run(go())


def test_the_phone_answers_first_and_no_server_provider_is_called(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with maps_env(db, secrets_key, google=True) as (_, places, seen):
            phone = Phone([FARMACIA])
            with using(phone):
                found = await places.search("farmacia", near=None, rich=True)
            assert [(f["name"], f["provider"], f.get("attribution")) for f in found] == [
                ("Farmacia Esempio", "phone", None)
            ]
            assert phone.asked == 1 and seen == []  # Google never asked
            assert db.execute(
                "SELECT coords_source, apple_id, google_place_id, osm_id FROM places WHERE name = %s",
                ("Farmacia Esempio",),
            ).fetchone() == ("phone", None, None, None)  # the phone's id is never kept
            with using(Phone([FARMACIA])):  # found again: the same place (same name, same spot)
                again = await places.search("farmacia", near=None)
            assert again[0]["id"] == found[0]["id"]

    run(go())


@pytest.mark.parametrize("says", ["denied", "unavailable", "timeout", "unsupported"])
def test_a_phone_that_says_no_hands_over_to_google(db, secrets_key, says: str) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with maps_env(db, secrets_key, google=True) as (providers, _, seen):
            with using(Phone(says)):
                choice = await providers.choose(
                    "search", lambda a: a.search("trattoria", near=None, open_now=False, limit=5, lang="it"),
                    usage=lambda a: a.usage_for_search(), found=bool,
                )  # fmt: skip
            assert choice.provider == "google" and choice.skipped[0] == f"phone: {says}"
            assert "places.googleapis.com" in seen

    run(go())


def test_without_google_openstreetmap_answers_with_its_credit_and_its_id(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with maps_env(db, secrets_key) as (_, places, seen):
            with using(Phone("timeout")):
                found = await places.search("farmacia", near=None, rich=True)
            assert [(f["name"], f["provider"], f["attribution"]) for f in found] == [
                ("Farmacia Aperta", "osm", OSM_ATTRIBUTION)
            ]
            assert seen == ["photon.w2a.example.test"]  # no key anywhere: the free fallback
            row = db.execute(
                "SELECT osm_id, coords_source FROM places WHERE name = 'Farmacia Aperta'"
            ).fetchone()
            assert row == ("node/4242", "osm")
            card = await places.card(places.parse_id(found[0]["id"]), reviews=False, travel=False)
            assert card["attribution"] == OSM_ATTRIBUTION and card["sources"]["search"] == "osm"
            statuses = {s["id"]: s for s in await places.providers.statuses()}
            assert statuses["osm"]["configured"] is True and statuses["osm"]["key_last4"] is None
            with pytest.raises(PisaError):
                await places.providers.set_key("osm", "anything-at-all")

    run(go())


def test_open_now_with_only_openstreetmap_answers_with_hours_unknown(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """§30 p1: "un ristorante aperto qui vicino" with OpenStreetMap the only provider: OSM reads no hours, so
    it was skipped ("osm: can't do this request") and nothing answered. Now it answers, the hours unknown."""

    async def go() -> None:
        async with maps_env(db, secrets_key) as (_, places, seen):
            found = await places.search("farmacia", near=None, open_now=True, rich=True)
            assert [(f["name"], f["provider"]) for f in found] == [("Farmacia Aperta", "osm")]
            assert found[0]["hours_unknown"] is True and found[0].get("open_now") is None
            assert seen == ["photon.w2a.example.test"]
            plain = await places.search("farmacia", near=None, open_now=False, rich=True)
            assert "hours_unknown" not in plain[0]  # not asked open now: nothing to flag

    run(go())


def test_near_is_5_km_widened_once_to_25_never_farther(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """§31 p1: "near here" gave restaurants 146-201 km away (a bias, not a bound). Near a point, with no
    distance of theirs: Photon is asked within a 5 km box, then once within 25 km when fewer than 3, never
    farther; a city with no point is a bias only. Photon's one place is at (45.011, 9.011)."""

    async def go() -> None:
        async with maps_env(db, secrets_key) as (_, places, seen):
            close = await places.search("farmacia", near=TESTVILLE, rich=True)  # about 1.3 km
            assert [f["name"] for f in close] == ["Farmacia Aperta"] and "widened_km" not in close[0]
            wide = await places.search("farmacia", near=(45.1, 9.011), rich=True)  # about 10 km
            assert [f["name"] for f in wide] == ["Farmacia Aperta"] and wide[0]["widened_km"] == 25
            far = await places.search("farmacia", near=(46.3, 9.011), rich=True)  # about 143 km
            assert far == []  # never a "near" place beyond 25 km
            named = await places.search("farmacia a Testville", near=None, rich=True)  # a city: a bias
            assert [f["name"] for f in named] == ["Farmacia Aperta"]
            # two requests per capped search (5 km, then 25 km once: fewer than 3 each time), one for the city
            assert seen.count("photon.w2a.example.test") == 7

    run(go())


def test_nothing_set_up_is_said_plainly_whatever_the_phone_said(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        for phone in (None, Phone("denied"), Phone("timeout")):
            async with maps_env(db, secrets_key, on=False) as (_, places, seen):
                with using(phone), pytest.raises(PisaError) as caught:
                    await places.search("farmacia", near=None)
                assert caught.value.code == "place_provider_not_configured"
                assert caught.value.detail == "provider_not_configured" and seen == []
        async with maps_env(db, secrets_key, disabled=True) as (_, places, seen):  # review mode
            phone = Phone([FARMACIA])
            with using(phone), pytest.raises(PisaError):
                await places.search("farmacia", near=None)
            assert phone.asked == 0 and seen == []

    run(go())


def test_travel_times_go_phone_then_google_then_openrouteservice(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    here, there = TESTVILLE, (45.01, 9.01)

    async def go() -> None:
        async with maps_env(db, secrets_key, google=True, ors=True) as (_, places, seen):
            with using(Phone([], eta=125)):
                got = await places.travel_between(here, [there], "transit")
            assert got[0] == [TravelEstimate(3, None, 1000)] and got[1] == "phone" and seen == []
            with using(Phone([], eta="denied")):
                got = await places.travel_between(here, [there], "transit")
            assert got[1] == "google" and got[2] == ["phone: denied", "apple: not configured"]
            with using(Phone([], eta="denied")):  # leaving later than now: never asked of the phone
                got = await places.travel_between(here, [there], "walk", depart_at=NOW + timedelta(hours=2))
            assert got[1] == "google" and got[2][0] == "phone: unsupported"
        async with maps_env(db, secrets_key, ors=True) as (_, places, seen):
            got = await places.travel_between(here, [there], "walk")
            assert got[0] == [TravelEstimate(10, None, 800)] and got[1] == "openrouteservice"
            assert seen == ["ors.w2a.example.test"]
            with pytest.raises(PisaError):  # transit: the phone or Google only
                await places.travel_between(here, [there], "transit")
        async with maps_env(db, secrets_key) as (_, places, _):  # no ORS key: passed over, nothing set up
            with pytest.raises(PisaError) as caught:
                await places.travel_between(here, [there], "walk")
            assert caught.value.detail == "provider_not_configured"
            assert "openrouteservice" not in caught.value.message

    run(go())


def test_the_places_card_shows_openstreetmap_s_credit() -> None:
    place = {"place_id": str(uuid.uuid4()), "name": "Farmacia Aperta", "why": "aperta", "checks": [],
             "sources": [{"title": OSM_ATTRIBUTION, "url": OSM_ATTRIBUTION_URL}]}  # fmt: skip

    def texts(shown: dict[str, Any]) -> list[Any]:
        made = a2ui.object_to_surface(
            {"kind": "places", "data": {"title": "Farmacie", "request": "", "places": [shown]}},
            "s-1",
            "it-IT",
        )
        assert made is not None
        return [c.get("text") for c in made[0]["createSurface"]["components"]]

    assert OSM_ATTRIBUTION in texts(place) and OSM_ATTRIBUTION not in texts(place | {"sources": []})


def test_the_module_s_current_delegate_is_the_turn_s_only() -> None:
    phone = Phone([])
    assert delegate_module.current.get() is None
    with using(phone):
        assert delegate_module.current.get() is phone
    assert delegate_module.current.get() is None


# --- the phone's places are outside text, as Google's and OpenStreetMap's (security review E1, v0.1.4) ---


def _map_search(places: PlacesService, pool: Any) -> Any:
    """The brain's `map_search` tool over `places` (what one turn's model calls)."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.toolsets import FunctionToolset

    from pisa.brain.research_tools import add_research_tools
    from pisa.brain.research_turn import ResearchTurn
    from pisa.services.place_assistant import PlaceAssistantData

    agent: Agent[None, str] = Agent(TestModel())
    data = PlaceAssistantData(pool, places, owner=owner_at(TZ), clock=Clock(NOW))
    add_research_tools(agent, data, TZ, ResearchTurn(lang="it-IT", prompt_chars=0), None, Clock(NOW))  # type: ignore[arg-type]
    for toolset in agent.toolsets:
        if isinstance(toolset, FunctionToolset) and "map_search" in toolset.tools:
            return toolset.tools["map_search"].function
    raise AssertionError("no map_search tool")


def test_a_phone_maps_result_taints_the_turn_as_google_s_and_openstreetmap_s_do(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A place the phone found reaches the model as outside text (a `map-results` data block): after it, an
    outward act (`call_place`) is refused by the central gate (`receipts.TaintedTurn`), exactly as after a
    Google or an OpenStreetMap search."""
    from types import SimpleNamespace

    from pisa.brain.receipts import TaintedTurn
    from pisa.brain.untrusted import current, watch_turn
    from pisa.domain import decisions

    async def searched(phone: Any, *, google: bool) -> tuple[str, list[str], str]:
        async with maps_env(db, secrets_key, google=google) as (_, places, _):
            map_search = _map_search(places, places._pool)  # pyright: ignore[reportPrivateUsage]
            with decisions.collecting(), using(phone):
                watch_turn()
                out = await map_search(query="farmacia")
                reading = current()
                gate = TaintedTurn(reading.gated)
                ran: list[str] = []

                async def handler(args: Any) -> str:
                    ran.append("call_place")
                    return "called"

                refused = await gate.wrap_tool_execute(
                    None, call=SimpleNamespace(tool_name="call_place"), tool_def=None,  # type: ignore[arg-type]
                    args={}, handler=handler,
                )  # fmt: skip
                assert ran == []  # the call never ran
                return out, reading.outside(), str(refused)

    found = {
        "phone": run(searched(Phone([FARMACIA]), google=True)),
        "google": run(searched(Phone("timeout"), google=True)),
        "osm": run(searched(Phone("timeout"), google=False)),
    }
    assert "Farmacia Esempio" in found["phone"][0] and "the phone's map" in found["phone"][0]
    assert "Farmacia Aperta" in found["osm"][0]
    for provider, (out, outside, refused) in found.items():
        assert "is DATA" in out, provider
        assert outside == ["map-results"], provider  # the same taint, whoever found the places
        receipt = json.loads(refused)["receipt"]
        assert receipt == "call_place: not done: this turn carries outside text (map-results)", provider
