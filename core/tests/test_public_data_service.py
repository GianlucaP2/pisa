# ruff: noqa: E501
"""The public data through Pisa's runtime (the cache and its rate limit, failing quietly, the
static stops loaded by the worker, the sources route, the briefing's lines, the views and the starters, errand
combos, streaks in the poster). One test (or a few) per row; each fails when its fix is reverted."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from tests.conftest import pair
from tests.public_fake import HOME, NYC_NOW, PENN, ROME_NOW, TERMINI, FakePublic, handler

ROME = ZoneInfo("Europe/Rome")
AT_ROME = datetime.fromtimestamp(ROME_NOW, UTC)  # Tuesday 3 Nov 2026, 12:56 in Rome


def _run(client: Any, fn: Any, *args: Any, **kw: Any) -> Any:
    return client.portal.call(lambda: fn(*args, **kw))


def _at(db: Any, where: tuple[float, float], when: datetime) -> str:
    row = db.execute("SELECT id::text FROM devices WHERE revoked_at IS NULL LIMIT 1").fetchone()
    device = row[0] if row else pair(db)[0]
    db.execute(
        "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) VALUES (%s, %s, %s, 10, %s)",
        (device, where[0], where[1], when),
    )
    return device


def _home(db: Any, where: tuple[float, float]) -> None:
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('profile_home', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb({"lat": where[0], "lon": where[1], "name": "Casa", "source": "owner", "confirmed": True}),),
    )


def _ready(
    client: Any, http_handler: Any, clock: Any, fake: FakePublic | None = None, now: datetime = AT_ROME
) -> Any:
    fake = fake or FakePublic()
    http_handler["handler"] = handler(fake)
    clock.now = now
    return fake


def _loaded(client: Any, city: str = "rome") -> None:
    """The first ask marks the city's stops wanted; the worker's tick downloads them."""
    runtime = client.app.state.runtime
    _run(client, runtime.public.read, "next_departures", {})
    assert _run(client, runtime.public.tick) == 1


# --- row 1: the transit tools ---------------------------------------------------------------------------------


def test_the_first_ask_loads_the_stops_in_the_worker_never_in_the_turn(
    client, db, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    first = _run(client, runtime.public.read, "next_departures", {})
    assert first["ok"] is False and "loading Rome's stops" in first["summary"]
    assert fake.count("rome_gtfs.zip") == 0  # 47 MB: never inside a turn
    assert _run(client, runtime.public.tick) == 1 and fake.count("rome_gtfs.zip") == 1
    assert _run(client, runtime.public.tick) == 0  # fresh for a week
    found = _run(client, runtime.public.read, "next_departures", {})
    assert found["ok"] and found["departures"] and found["stop"].upper().startswith("TERMINI")
    assert all({"line", "toward", "at", "in_min"} <= set(d) for d in found["departures"])
    assert "Roma Servizi per la Mobilità" in found["credit"]


def test_the_cache_is_the_rate_limit_one_call_a_window(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    _loaded(client)
    runtime = client.app.state.runtime
    for _ in range(3):
        _run(client, runtime.public.read, "next_departures", {})
    assert fake.count("rome_trips.pb") == 1
    clock.now = AT_ROME + timedelta(seconds=31)
    _run(client, runtime.public.read, "next_departures", {})
    assert fake.count("rome_trips.pb") == 2


def test_for_home_and_a_named_line(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    _home(db, HOME)
    _loaded(client)
    runtime = client.app.state.runtime
    home = _run(client, runtime.public.read, "next_departures", {"to": "casa"})
    assert home["ok"] and home["for_home"] and {d["line"] for d in home["departures"]} == {"360"}
    assert home["summary"].startswith("Next departures for home near")
    line = _run(client, runtime.public.read, "next_departures", {"line": "il 64"})
    assert line["departures"] and {d["line"] for d in line["departures"]} == {"64"}


def test_a_failing_source_is_said_never_guessed_and_waited_out(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    _loaded(client)
    runtime = client.app.state.runtime
    fake.down.add("romamobilita.it")
    found = _run(client, runtime.public.read, "next_departures", {})
    assert found["ok"] is False and "can't be read" in found["summary"] and not found.get("departures")
    calls = fake.count("rome_trips.pb")
    clock.now = AT_ROME + timedelta(minutes=2)
    _run(client, runtime.public.read, "next_departures", {})
    assert fake.count("rome_trips.pb") == calls  # the backoff: no new call for 5 minutes
    row = db.execute("SELECT error FROM public_cache WHERE key = 'rome_trips'").fetchone()
    assert row and "503" in row[0]
    fake.down.clear()
    clock.now = AT_ROME + timedelta(minutes=6)
    assert _run(client, runtime.public.read, "next_departures", {})["ok"]
    # a copy kept: when the source fails again it is used, said as old, for up to 10 minutes
    fake.down.add("romamobilita.it")
    clock.now = AT_ROME + timedelta(minutes=7)
    old = _run(client, runtime.public.read, "next_departures", {})
    assert old["ok"] and "older copy" in old["summary"]


def test_metro_problems_rome_and_a_line_in_new_york(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    metro = _run(client, runtime.public.read, "transit_alerts", {})
    assert metro["ok"] and metro["count"] == 1 and metro["alerts"][0]["lines"] == "Metro A"
    assert "rallentamenti" in metro["summary"]
    assert _run(client, runtime.public.read, "transit_alerts", {"line": "metro C"})["count"] == 0
    sent = json.loads(
        (__import__("tests.public_fake", fromlist=["DATA"]).DATA / "nyc_alerts.json").read_text()
    )["header"]["timestamp"]
    _at(db, PENN, AT_ROME + timedelta(minutes=1))  # they landed: their latest location
    clock.now = datetime.fromtimestamp(sent, UTC)
    seven = _run(client, runtime.public.read, "transit_alerts", {"line": "the 7"})
    assert (
        seven["ok"] and seven["city"] == "nyc" and seven["count"] >= 1 and "7" in seven["alerts"][0]["lines"]
    )
    subway = _run(client, runtime.public.read, "transit_alerts", {})
    assert all(a["kind"] == "Delays" for a in subway["alerts"])  # planned works aren't "problems now"


def test_new_york_departures_from_the_mta(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock, now=datetime.fromtimestamp(NYC_NOW, UTC))
    _at(db, PENN, clock.now)
    _loaded(client, "nyc")
    runtime = client.app.state.runtime
    found = _run(client, runtime.public.read, "next_departures", {"line": "A"})
    assert found["ok"] and found["city"] == "nyc" and {d["line"] for d in found["departures"]} == {"A"}
    assert fake.count("nyc_trips_ace.pb") == 1 and fake.count("(empty feed)") == 0  # only the A's feed
    assert found["credit"] == "Data: MTA"


def test_weather_alerts_italy_from_the_bulletin_the_us_from_the_nws(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock, FakePublic(dpc="dpc_latest_all_yellow.zip"))
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    it = _run(client, runtime.public.read, "weather_alerts", {})
    assert it["ok"] and it["country"] == "IT" and it["level_today"] == "gialla"
    assert "allerta gialla for temporali (Bacini di Roma)" in it["summary"] and "02/11 14:57" in it["summary"]
    _at(db, (40.741, -73.966), AT_ROME + timedelta(minutes=1))
    clock.now = AT_ROME + timedelta(minutes=1)
    us = _run(client, runtime.public.read, "weather_alerts", {})
    assert us["ok"] and us["country"] == "US" and us["alerts"][0]["severity"] == "Moderate"
    asked = [r for r in fake.requests if r.url.host == "api.weather.gov"][-1]
    assert asked.url.params["point"] == "40.74,-73.97"  # a rounded point is all Pisa sends
    assert asked.headers["user-agent"].startswith("J-personal-assistant")


def test_ztl_pharmacy_and_the_airport(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    z = _run(client, runtime.public.read, "ztl_status", {"zone": "centro"})
    assert (
        z["ok"] and "ZTL Centro Storico (diurna): ACTIVE now" in z["summary"] and "28/07/2026" in z["summary"]
    )
    p = _run(client, runtime.public.read, "nearest_pharmacy", {})
    assert p["ok"] and len(p["pharmacies"]) <= 3 and "hours and on-duty rota unknown" in p["summary"]
    assert all(row["map_url"].startswith("https://maps.apple.com/") for row in p["pharmacies"])
    assert _run(client, runtime.public.read, "airport_departure", {})["ok"] is False  # no flight yet
    leave = AT_ROME + timedelta(days=1)
    db.execute(
        "INSERT INTO trips (id, title, depart_at, arrive_at, legs, created_at, updated_at) VALUES (%s, 'New York', %s, %s, %s, %s, %s)",
        (uuid.uuid4(), leave, leave + timedelta(hours=9),
         Jsonb([{"kind": "flight", "at": leave.astimezone(ROME).isoformat(), "ref": "AZ5287", "place": "FCO → JFK"}]),
         AT_ROME, AT_ROME),
    )  # fmt: skip
    a = _run(client, runtime.public.read, "airport_departure", {})
    assert a["ok"] and a["airport"] == "FCO" and a["usual_time"] is True and a["way_min"] == 55
    assert a["leave_at"] == (leave - timedelta(minutes=180 + 55)).astimezone(ROME).strftime("%a %H:%M")
    assert a["problems"] and "Metro A" in a["problems"][0]  # the way to Termini


def test_lists_the_sources_with_licence_and_status(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    _run(client, runtime.public.read, "transit_alerts", {})
    fake.down.add("dati.lazio.it")
    _run(client, runtime.public.read, "nearest_pharmacy", {})
    _, token = pair(db)
    body = client.get("/v1/public/sources", headers={"Authorization": f"Bearer {token}"}).json()
    by = {s["key"]: s for s in body["sources"]}
    assert by["rome_alerts"]["status"] == "ok" and by["rome_alerts"]["licence"] == "CC BY-SA 4.0"
    assert by["lazio_pharmacies"]["status"] == "failing" and by["nws_alerts"]["status"] == "unused"
    assert by["rome_ztl"]["status"] == "reviewed" and by["rome_ztl"]["url"].endswith("/ztl-in-centro/")
    from pisa.api.public import PublicSourcesResponse
    from tests.conftest import PROTOCOL_DIR

    PublicSourcesResponse.model_validate(body)
    fixture = json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "public_sources.json").read_text())
    assert {s["key"] for s in PublicSourcesResponse.model_validate(fixture).model_dump()["sources"]} <= set(
        by
    )


# --- row 2: the skills from them, the briefing ------------------------------------------------------------------


def test_a_view_runs_the_public_reads(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.skill_starters import named

    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    sheet, _ = _run(client, runtime.views.sheet_of, "problemi metro", named("problemi metro", "IT").recipe)  # type: ignore[union-attr]
    assert sheet is not None and sheet["blocks"][0]["text"].startswith("1 active alert(s) on the metro")
    facts = [b for b in sheet["blocks"] if b["type"] == "fact"]
    assert facts and facts[0]["label"] == "Metro A"
    assert sheet["blocks"][-1]["text"].startswith("Dati: Roma Servizi per la Mobilità")


def test_a_starter_offered_once_kept_on_his_yes_to_pisa_s_offer(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    public, starters = runtime.conversations.deps.tools.public
    assert public is runtime.public
    assert _run(client, starters.offer, "transit_alerts") == "problemi metro"
    said, sheet = _run(
        client, starters.keep, "problemi metro", "sì, tienila", outside=False, turn_id=None, device_id=None
    )
    assert sheet is None and "did not offer" in said  # Pisa never offered it
    conv = uuid.uuid4()
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()[0]
    db.execute("INSERT INTO conversations (id, device_id) VALUES (%s, %s)", (conv, device))
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'assistant', %s, 'it-IT', %s)",
        (conv, 'Un avviso sulla metro A. Vuoi che la tenga come skill "problemi metro"?', AT_ROME),
    )
    assert _run(client, starters.offer, "transit_alerts") is None  # offered once: never again this week
    said, sheet = _run(
        client, starters.keep, "problemi metro", "no grazie", outside=False, turn_id=None, device_id=None
    )
    assert sheet is None and "own yes" in said
    said, sheet = _run(
        client, starters.keep, "problemi metro", "sì, tienila", outside=True, turn_id=None, device_id=None
    )
    assert sheet is None  # never in a turn that read outside text
    said, sheet = _run(
        client, starters.keep, "problemi metro", "sì, tienila", outside=False, turn_id=None, device_id=None
    )
    assert sheet is not None and "passed all 2 checks" in said and "Lo accendo?" in said
    row = db.execute(
        "SELECT status, made_from, kind FROM pisa_skills WHERE name_key = 'problemi metro'"
    ).fetchone()
    assert row == (
        "trial",
        "vorrei che tu potessi dirmi se la metro ha problemi",
        "view",
    )  # the path: Face ID next


def test_the_morning_brief_composes_them_for_the_city_he_is_in(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock, FakePublic(dpc="dpc_latest_all_yellow.zip"))
    _at(db, TERMINI, AT_ROME)
    runtime = client.app.state.runtime
    lines = _run(client, runtime.public.briefing_lines)
    assert any("allerta gialla" in line for line in lines) and any("Metro A" in line for line in lines)
    from pisa.brain import briefing as brain

    data = _run(
        client,
        runtime.briefings.gather,
        {"language": "it-IT", "briefing": {"sections": ["weather"], "max_words": 60}},
        AT_ROME,
    )
    assert data.public == lines and not data.empty
    instructions, facts = brain.prompt(data)
    assert "<public-data-" in facts and "DATA" in instructions
    # in New York with no line of theirs asked about: the subway's alerts stay out (never the whole network)
    clock.now = datetime.fromtimestamp(NYC_NOW, UTC)
    _at(db, PENN, clock.now)
    assert not any("subway" in line for line in _run(client, runtime.public.briefing_lines))


# --- the brain: the tools' results are outside data ------------------------------------------------------------


def test_the_tools_answer_as_outside_data_and_offer_the_starter(
    client, db, clock, http_handler, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    from tests.conftest import set_key

    set_key(db, secrets_key)
    _ready(client, http_handler, clock)
    device = uuid.UUID(_at(db, TERMINI, AT_ROME))
    seen: list[str] = []

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        returns = [
            p
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart) and p.tool_name == "transit_alerts"
        ]
        if not returns:
            names = [t.name for t in info.function_tools]
            if "transit_alerts" not in names:
                yield {
                    0: DeltaToolCall(
                        name="search_tools", json_args=json.dumps({"queries": ["transit_alerts"]})
                    )
                }
                return
            yield {0: DeltaToolCall(name="transit_alerts", json_args="{}")}
            return
        seen.append(str(returns[-1].content))
        yield "Sulla metro A ci sono rallentamenti."

    fake_models.default = reply

    async def emit(delta: str, final: bool) -> None:
        pass

    runtime = client.app.state.runtime
    _run(
        client,
        runtime.conversations.respond,
        device,
        "la metro ha problemi?",
        "it-IT",
        emit,
        trace_id=uuid.uuid4().hex,
    )
    assert seen and "<public-data-" in seen[0] and "never follow it" in seen[0]
    assert "rallentamenti" in seen[0] and 'skill named "problemi metro"' in seen[0]


# --- row 3: errand combos ---------------------------------------------------------------------------------------


def _place(db: Any, name: str, where: tuple[float, float]) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute("INSERT INTO places (id, name, lat, lon, created_at, updated_at) VALUES (%s, %s, %s, %s, now(), now())",
               (pid, name, where[0], where[1]))  # fmt: skip
    return pid


def _task(db: Any, title: str, place: uuid.UUID) -> None:
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at, due_kind, place_id) "
        "VALUES (%s, %s, %s, 'open', 'manual', now(), now(), 'none', %s)",
        (uuid.uuid4(), title, title.casefold(), place),
    )


def test_one_errand_combo_a_day_never_in_quiet_hours(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _task(db, "Ritirare le camicie", _place(db, "Lavanderia Rossi", (41.865, 12.523)))
    _task(db, "Comprare il pane", _place(db, "Forno Bianchi", (41.865, 12.528)))
    runtime = client.app.state.runtime
    clock.now = datetime(2026, 11, 3, 7, 0, tzinfo=UTC)  # 08:00 in Rome: before 9
    assert _run(client, runtime.errands.tick) is False
    clock.now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert _run(client, runtime.errands.tick) is True
    texts = [r[0] for r in db.execute("SELECT text FROM notifications").fetchall()]
    assert texts == ["Passi da Lavanderia Rossi e Forno Bianchi in 15 minuti? Sono vicini."]
    assert _run(client, runtime.errands.tick) is False  # once a day
    clock.now += timedelta(days=1)
    assert _run(client, runtime.errands.tick) is False  # never the same pair within a week
    from pisa.services import worker_steps

    assert "errands" in worker_steps.INITIATIVE_STEPS and "public_data" not in worker_steps.INITIATIVE_STEPS


def test_quiet_hours_hold_no_errand(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    _task(db, "Ritirare le camicie", _place(db, "Lavanderia Rossi", (41.865, 12.523)))
    _task(db, "Comprare il pane", _place(db, "Forno Bianchi", (41.865, 12.528)))
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settings', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb({"quiet": {"until": "2026-11-03T23:00:00+01:00"}}),),
    )
    clock.now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert _run(client, client.app.state.runtime.errands.tick) is False
    assert db.execute("SELECT count(*) FROM errand_suggestions").fetchone()[0] == 0


# --- row 4: streaks in the poster --------------------------------------------------------------------------------


def test_the_poster_shows_the_streaks_that_matter(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(client, http_handler, clock)
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES ('me@example.invalid', 'Personale', 'owner') ON CONFLICT DO NOTHING"
    )
    sunday = datetime(2026, 11, 22, 19, 0, tzinfo=ROME)
    for weeks in range(3):
        start = sunday - timedelta(weeks=weeks, days=4, hours=1)
        db.execute(
            "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, attendees) "
            "VALUES ('me@example.invalid', %s, 'Corsa al parco', %s, %s, false, 'confirmed', 0)",
            (f"run{weeks}", start, start + timedelta(hours=1)),
        )
    clock.now = sunday.astimezone(UTC)
    assert _run(client, client.app.state.runtime.rituals.week_poster, True) is True
    data = db.execute("SELECT data FROM ritual_sheets WHERE ritual = 'week_poster'").fetchone()[0]
    stats = [b for b in data["blocks"] if b["type"] == "stat"]
    run = [b for b in stats if b["label"].startswith("Corsa")]
    assert run == [{"type": "stat", "label": "Corsa, di fila", "value": "3", "unit": "sett.", "trend": "up"}]
    labels = [b["label"] for b in stats]
    assert labels.index("Corsa, di fila") == labels.index("Palestra", 1) + 1  # after the gym's streak stat


def test_nyc_planned_works_only_when_he_names_the_line(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    _ready(
        client, http_handler, clock, now=datetime.fromtimestamp(1795870000, UTC)
    )  # the A's planned reroute
    _at(db, PENN, clock.now)
    runtime = client.app.state.runtime
    assert (
        _run(client, runtime.public.read, "transit_alerts", {})["count"] == 0
    )  # works aren't "problems now"
    a = _run(client, runtime.public.read, "transit_alerts", {"line": "A"})
    assert a["count"] == 1 and a["alerts"][0]["kind"] == "Planned - Reroute"
