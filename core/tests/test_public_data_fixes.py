# ruff: noqa: E501
"""The review of a342456 (M1-M2, L3-L11) and the ruling on scheduled public skills
(a-e). One focused test per fix, each failing when the fix is reverted (the handoff's table)."""

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from pisa.domain import gtfs_rt, transit, wishes
from pisa.domain.skill_starters import named
from tests.public_fake import DATA, PENN, TERMINI, FakePublic
from tests.test_public_data_service import AT_ROME, _at, _home, _place, _ready, _run, _task

NY_POINT = (40.741, -73.966)


# --- M1: errand combos have their switch ---------------------------------------------------------------------


def test_errand_combos_off_by_his_switch(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _task(db, "Ritirare le camicie", _place(db, "Lavanderia Rossi", (41.865, 12.523)))
    _task(db, "Comprare il pane", _place(db, "Forno Bianchi", (41.865, 12.528)))
    runtime = client.app.state.runtime
    clock.now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert _run(client, runtime.rituals.get)["errands"] == {"on": True}
    _run(client, runtime.rituals.put, {"errands": {"on": False}})
    assert _run(client, runtime.errands.tick) is False
    assert "is now on" in _run(client, runtime.rituals.switch, "errands", True, "accendi le commissioni")
    assert _run(client, runtime.errands.tick) is True  # back on by their words


# --- M2: each source its own cap; the reader's own limits ----------------------------------------------------


def test_each_source_s_cap_reaches_the_connector(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.public_sources import FEED_MAX, SOURCES, STATIC_MAX

    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    seen: list[tuple[str, int]] = []
    real = runtime.public._fetch

    class Recording:
        async def get(self, url: str, **kw: Any) -> bytes:
            seen.append((url.rsplit("/", 1)[-1], kw["cap"]))
            return await real.get(url, **kw)

    runtime.public._fetch = Recording()
    _run(client, runtime.public.read, "transit_alerts", {})
    _run(client, runtime.public.read, "next_departures", {})
    _run(client, runtime.public.tick)
    caps = dict(seen)
    assert caps["rome_rtgtfs_service_alerts_feed.pb"] == FEED_MAX == 8 * 1024 * 1024
    assert caps["rome_static_gtfs.zip"] == STATIC_MAX
    assert SOURCES["nws_alerts"].cap == FEED_MAX and SOURCES["nyc_static"].cap == STATIC_MAX


def test_the_reader_caps_entities_and_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    entity = gtfs_rt.put(1, "e") + gtfs_rt.put(3, gtfs_rt.put(1, gtfs_rt.put(1, "t") + gtfs_rt.put(5, "64")))
    feed = gtfs_rt.put(1, gtfs_rt.put(3, 1)) + gtfs_rt.put(2, entity) * 12
    assert len(gtfs_rt.parse(feed).trips) == 12
    monkeypatch.setattr(gtfs_rt, "MAX_ENTITIES", 5)
    assert len(gtfs_rt.parse(feed).trips) == 5
    monkeypatch.setattr(gtfs_rt, "MAX_FIELDS", 8)
    with pytest.raises(gtfs_rt.FeedError, match="too many fields"):
        gtfs_rt.parse(feed)


# --- L3: parsing off the event loop -------------------------------------------------------------------------


def test_feeds_and_static_sets_are_parsed_off_the_event_loop(
    client, db, clock, http_handler, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import public_data

    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    threads: list[str] = []
    parse, reduce = gtfs_rt.parse, public_data.stops_json

    def watched_parse(*a: Any, **kw: Any) -> Any:
        threads.append(threading.current_thread().name)
        return parse(*a, **kw)

    def watched_reduce(raw: bytes) -> bytes:
        threads.append(threading.current_thread().name)
        return reduce(raw)

    monkeypatch.setattr(public_data.gtfs_rt, "parse", watched_parse)
    monkeypatch.setattr(public_data, "stops_json", watched_reduce)
    runtime = client.app.state.runtime
    _run(client, runtime.public.read, "next_departures", {})
    _run(client, runtime.public.tick)
    _run(client, runtime.public.read, "next_departures", {})
    loop = client.portal.call(lambda: _thread())
    assert len(threads) >= 2 and loop not in threads


async def _thread() -> str:
    return threading.current_thread().name


# --- L4: alerts' fields coerced; a bad alert only drops the problems line -------------------------------------


def test_nyc_alerts_coerced_and_printable() -> None:
    body = {"entity": [
        {"id": "a1", "alert": {"active_period": [{"start": "100", "end": 300}], "informed_entity": [{"route_id": "A"}],
                               "header_text": {"translation": [{"language": "en", "text": "A trains\x00 delayed\n now"}]},
                               "transit_realtime.mercury_alert": {"alert_type": "Delays"}}},
        {"id": "a2", "alert": {"active_period": [{"start": "soon"}], "header_text": "oops"}},
    ]}  # fmt: skip
    first, second = transit.nyc_alerts(json.dumps(body))
    assert first.periods == ((100, 300),) and first.header == "A trains delayed now"
    assert transit.problems([first], 200, {"A"}) == [first]
    assert second.periods == ((None, None),) and second.header == ""


def test_a_malformed_alerts_feed_only_drops_the_airport_s_problems(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    import httpx

    fake = _ready(client, http_handler, clock)
    _at(db, PENN, AT_ROME)
    leave = AT_ROME + timedelta(days=1)
    db.execute(
        "INSERT INTO trips (id, title, depart_at, arrive_at, legs, created_at, updated_at) VALUES (%s, 'Roma', %s, %s, %s, %s, %s)",
        (uuid.uuid4(), leave, leave + timedelta(hours=9),
         Jsonb([{"kind": "flight", "at": leave.isoformat(), "ref": "AZ5288", "place": "JFK → FCO"}]), AT_ROME, AT_ROME),
    )  # fmt: skip

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("subway-alerts.json"):
            return httpx.Response(200, json={"entity": 5})  # not a list: a TypeError while reading it
        return fake(request)

    http_handler["handler"] = broken
    a = _run(client, client.app.state.runtime.public.read, "airport_departure", {})
    assert a["ok"] and a["airport"] == "JFK" and a["problems"] == []


# --- L5: the NWS's error body (it repeats their point) is never logged -------------------------------------------


def test_the_nws_error_is_logged_by_its_status_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import httpx

    from pisa.connectors import public_feeds
    from pisa.ports.public_data import PublicDataError

    logged: list[tuple[Any, ...]] = []
    monkeypatch.setattr(public_feeds, "external_failure", lambda *a: logged.append(a))

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=f"No data for point {request.url.params['point']}")

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            for url in ("https://api.weather.gov/alerts/active", "https://dati.lazio.it/x.csv"):
                with pytest.raises(PublicDataError):
                    await public_feeds.PublicFeeds(http).get(url, params={"point": "40.71,-74.01"})

    asyncio.run(go())
    assert logged[0][1:] == ("api.weather.gov", 404, None)
    assert b"40.71" in logged[1][3]  # another host's body is kept (no place of theirs in it)


# --- L6: the NWS's per-point copies don't pile up -----------------------------------------------------------


def test_old_nws_point_copies_are_pruned(app_settings, db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from pisa.services import maintenance

    now = datetime.now(UTC)
    for key, age in (("nws_alerts:40.71,-74.01", timedelta(hours=3)), ("nws_alerts:41.90,12.50", timedelta(minutes=5)),
                     ("rome_alerts", timedelta(days=9))):  # fmt: skip
        db.execute(
            "INSERT INTO public_cache (key, body, fetched_at) VALUES (%s, '\\x00', %s)", (key, now - age)
        )
    counts = asyncio.run(maintenance.prune_history(app_settings, now))
    left = {r[0] for r in db.execute("SELECT key FROM public_cache").fetchall()}
    assert counts["public_cache"] == 1 and left == {"nws_alerts:41.90,12.50", "rome_alerts"}


# --- L7, L11: keeping a starter -----------------------------------------------------------------------------


def _offered(db: Any, text: str = 'Vuoi che la tenga come skill "problemi metro"?') -> None:
    conv = uuid.uuid4()
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()[0]
    db.execute("INSERT INTO conversations (id, device_id) VALUES (%s, %s)", (conv, device))
    db.execute("INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'assistant', %s, 'it-IT', %s)",
               (conv, text, AT_ROME))  # fmt: skip


def test_keep_refuses_in_a_turn_that_read_outside_text(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    _offered(db)
    starters = client.app.state.runtime.conversations.deps.tools.public[1]
    said, sheet = _run(
        client, starters.keep, "problemi metro", "sì, tienila", outside=True, turn_id=None, device_id=None
    )
    assert (
        sheet is None
        and said.startswith("Not kept")
        and db.execute("SELECT count(*) FROM pisa_skills").fetchone()[0] == 0
    )


def test_keep_skill_without_a_screen_gives_its_lines_as_data(
    client, db, clock, http_handler, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    from tests.conftest import set_key

    set_key(db, secrets_key)
    _ready(client, http_handler, clock)
    device = uuid.UUID(_at(db, TERMINI, AT_ROME))
    _offered(db)
    seen: list[str] = []

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        returns = [
            p
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart) and p.tool_name == "keep_skill"
        ]
        if not returns:
            if "keep_skill" not in [t.name for t in info.function_tools]:
                yield {
                    0: DeltaToolCall(name="search_tools", json_args=json.dumps({"queries": ["keep_skill"]}))
                }
                return
            yield {0: DeltaToolCall(name="keep_skill", json_args=json.dumps({"name": "problemi metro"}))}
            return
        seen.append(str(returns[-1].content))
        yield "La accendo?"

    fake_models.default = reply

    async def emit(delta: str, final: bool) -> None:
        pass

    _run(
        client,
        client.app.state.runtime.conversations.respond,
        device,
        "sì, tienila",
        "it-IT",
        emit,
        trace_id=uuid.uuid4().hex,
    )
    assert seen and "<public-data-" in seen[0] and "Metro A" in seen[0]


# --- L8: streaks never fail the poster; nothing later today counts ---------------------------------------------


def test_a_failing_streak_step_leaves_the_poster(client, db, clock, http_handler, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import rituals

    _ready(client, http_handler, clock)
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES ('me@example.invalid', 'P', 'owner') ON CONFLICT DO NOTHING"
    )
    sunday = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
    db.execute("INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, attendees) "
               "VALUES ('me@example.invalid', 'e', 'Riunione', %s, %s, false, 'confirmed', 0)",
               (sunday - timedelta(days=2), sunday - timedelta(days=2, hours=-1)))  # fmt: skip

    async def broken(*a: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(rituals, "streak_sessions", broken)
    clock.now = sunday
    assert _run(client, client.app.state.runtime.rituals.week_poster, True) is True


def test_a_session_later_today_isn_t_counted_yet(client, db) -> None:  # type: ignore[no-untyped-def]
    from zoneinfo import ZoneInfo

    from pisa.services.streaks import sessions

    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES ('me@example.invalid', 'P', 'owner') ON CONFLICT DO NOTHING"
    )
    now = datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo("Europe/Rome"))
    for eid, at in (("done", now - timedelta(hours=2)), ("later", now + timedelta(hours=8))):
        db.execute("INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, attendees) "
                   "VALUES ('me@example.invalid', %s, 'Palestra', %s, %s, false, 'confirmed', 0)",
                   (eid, at, at + timedelta(hours=1)))  # fmt: skip

    async def go() -> list[tuple[str, Any]]:
        pool = client.app.state.runtime.pool
        async with pool.connection() as conn:
            return await sessions(conn, now.date(), "Europe/Rome", now)

    assert len(client.portal.call(go)) == 1


# --- L9: the briefing's skip is named for what it is ---------------------------------------------------------


def test_a_slow_public_part_is_logged_as_the_briefing_s_public_skip(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from structlog.testing import capture_logs

    from pisa.services import briefing

    monkeypatch.setattr(briefing, "PLACE_NOTES_S", 0.05)
    runtime = client.app.state.runtime

    async def slow() -> list[str]:
        await asyncio.sleep(1)
        return ["x"]

    runtime.briefings.public = slow
    with capture_logs() as logs:
        _run(
            client,
            runtime.briefings.gather,
            {"language": "it-IT", "briefing": {"sections": [], "max_words": 60}},
            AT_ROME,
        )
    assert any(e["event"] == "briefing_public_skipped" for e in logs)


# --- L10: an old location isn't "where they are" ------------------------------------------------------------


def test_after_twelve_hours_home_and_said_so(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _at(db, PENN, AT_ROME - timedelta(hours=13))  # yesterday's New York
    _home(db, TERMINI)
    runtime = client.app.state.runtime
    found = _run(client, runtime.public.read, "transit_alerts", {})
    assert found["city"] == "rome" and "their latest location is more than 12 hours old" in found["summary"]
    db.execute("DELETE FROM app_settings WHERE key = 'profile_home'")
    found = _run(client, runtime.public.read, "transit_alerts", {})
    assert found["city"] == "nyc" and "their location of 13 hours ago" in found["summary"]


# --- the ruling: public skills on their schedule (a-e) -----------------------------------------------------------


def _skill(db: Any, name: str, recipe: dict[str, Any], *, last_used: datetime | None = None) -> None:
    db.execute(
        "INSERT INTO pisa_skills (id, name, name_key, kind, status, made_from, checks, recipe, tests_passed, digest, "
        "enabled_at, last_used_at, created_at, updated_at) VALUES (gen_random_uuid(), %s, %s, 'view', 'on', %s, %s, %s, "
        "2, 'seed', %s, %s, %s, %s)",
        (name, name, f"vorrei che tu potessi dirmi {name}", Jsonb([{"input": name, "shows": "text"}] * 2),
         Jsonb(recipe), AT_ROME - timedelta(days=1), last_used, AT_ROME, AT_ROME),
    )  # fmt: skip


def _schedule(client: Any, name: str, words: str = "dimmelo ogni mattina alle 8") -> str:
    return _run(
        client,
        client.app.state.runtime.autopilot.skills.set_schedule,
        name,
        "08:00",
        "daily",
        words,
        outside=False,
    )


def _at_eight(clock: Any) -> None:
    clock.now = datetime(2026, 11, 4, 7, 1, tzinfo=UTC)  # 08:01 in Rome


def test_ruling_a_only_when_every_read_is_public() -> None:
    metro = [{"tool": "transit_alerts", "args": {}}]
    assert not wishes.reads_outside(metro)
    assert not wishes.reads_outside([*metro, {"tool": "weather_alerts", "args": {}}])
    assert wishes.reads_outside([*metro, {"tool": "get_current_time", "args": {}}])  # not every read
    assert not wishes.reads_outside([{"tool": "get_current_time", "args": {}}])  # Pisa's own: as before
    assert wishes.reads_outside([*metro, {"tool": "search_my_email", "args": {}}])


def test_ruling_b_the_push_is_the_lines_verbatim_with_the_credit(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    _skill(db, "problemi metro", named("problemi metro", "IT").recipe)  # type: ignore[union-attr]
    assert "Not scheduled" not in _schedule(client, "problemi metro")
    _at_eight(clock)
    clock.now = datetime.fromtimestamp(1793706988 + 60, UTC)  # the alert's own window, 12:57 in Rome
    db.execute("UPDATE pisa_skills SET schedule = %s", (Jsonb({"at": "12:00", "days": "daily"}),))
    assert _run(client, client.app.state.runtime.autopilot.skills.tick) == 1
    (text,) = [r[0] for r in db.execute("SELECT text FROM notifications").fetchall()]
    assert text.startswith("problemi metro: 1 active alert(s) on the metro: Metro A: rallentamenti")
    assert text.endswith("(Dati: Roma Servizi per la Mobilità (CC BY-SA))")


def test_ruling_c_his_words_quiet_hours_per_hour_withering_and_a_stop(
    client, db, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import worker_steps

    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    recipe = named("ztl adesso", "IT").recipe  # type: ignore[union-attr]
    for n in range(6):
        _skill(db, f"ztl {n}", recipe)
    assert _schedule(client, "ztl 0", "va bene").startswith(
        "Not scheduled"
    )  # only their words give it a time
    for n in range(6):
        assert "Not scheduled" not in _schedule(client, f"ztl {n}")
    db.execute(
        "UPDATE pisa_skills SET last_used_at = %s WHERE name = 'ztl 5'", (AT_ROME - timedelta(days=40),)
    )
    db.execute("UPDATE pisa_skills SET enabled_at = %s WHERE name = 'ztl 5'", (AT_ROME - timedelta(days=40),))
    _at_eight(clock)
    runtime = client.app.state.runtime
    assert _run(client, runtime.autopilot.skills.tick) == 4  # SCHEDULED_PER_HOUR
    assert db.execute("SELECT status FROM pisa_skills WHERE name = 'ztl 5'").fetchone()[0] == "withered"
    assert "skills" in worker_steps.INITIATIVE_STEPS  # a stop pauses it
    db.execute("DELETE FROM notifications")
    db.execute(
        "UPDATE pisa_skills SET last_run_on = NULL, status = 'on', last_used_at = now() WHERE name = 'ztl 0'"
    )
    db.execute("DELETE FROM pisa_skill_uses")
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settings', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb({"quiet": {"until": "2026-11-04T23:00:00+01:00"}}),),
    )
    _run(client, runtime.autopilot.skills.tick)
    rows = db.execute("SELECT delivered_via FROM notifications").fetchall()
    assert rows and all(r[0] == "held" for r in rows)  # quiet hours hold it


def test_ruling_d_only_the_rounded_point_leaves_on_a_schedule(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock)
    _at(db, NY_POINT, AT_ROME)
    _skill(db, "allerte meteo", named("allerte meteo", "IT").recipe)  # type: ignore[union-attr]
    _schedule(client, "allerte meteo")
    _at_eight(clock)
    _run(client, client.app.state.runtime.autopilot.skills.tick)
    asked = [r for r in fake.requests if r.url.host == "api.weather.gov"]
    assert asked and all(r.url.params["point"] == "40.74,-73.97" for r in asked)
    assert all("40.741" not in str(r.url) for r in fake.requests)


def test_ruling_e_a_failed_read_says_so_and_an_old_copy_is_said_old(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock, FakePublic())
    _at(db, TERMINI, AT_ROME)
    _skill(db, "allerte meteo", named("allerte meteo", "IT").recipe)  # type: ignore[union-attr]
    _schedule(client, "allerte meteo")
    fake.down.add("raw.githubusercontent.com")
    _at_eight(clock)
    runtime = client.app.state.runtime
    _run(client, runtime.autopilot.skills.tick)
    (first,) = [r[0] for r in db.execute("SELECT text FROM notifications").fetchall()]
    assert "can't be read now" in first and "allerta" not in first.split(":", 1)[1].split("(")[0]
    # a copy kept from earlier, the source down again: the push says it is older
    fake.down.clear()
    clock.now += timedelta(minutes=10)
    assert _run(client, runtime.public.read, "weather_alerts", {})["ok"]
    fake.down.add("raw.githubusercontent.com")
    clock.now += timedelta(hours=1, minutes=5)
    old = _run(client, runtime.public.read, "weather_alerts", {})
    assert (
        old["old"] is True and "An older copy: DPC, bollettino couldn't be read just now." in old["summary"]
    )
    _ = DATA
