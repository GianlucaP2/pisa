# ruff: noqa: E501
"""The day as a story: rituals and place notes. Each behaviour's test fails when its fix is reverted."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pisa.domain import rituals as rules
from tests.conftest import PROTOCOL_DIR, pair
from tests.owner import owner_at

TZ = ZoneInfo("Europe/Rome")
WED_22 = datetime(2026, 11, 18, 22, 0, tzinfo=TZ)  # Wednesday; tomorrow is Thursday 19 November
CAL = "owner@example.invalid"


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _calendar(db: Any) -> None:
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES (%s, 'Personale', 'owner') ON CONFLICT DO NOTHING",
        (CAL,),
    )


def _event(db: Any, eid: str, title: str, start: str, end: str, attendees: int = 0) -> None:
    _calendar(db)
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, attendees) "
        "VALUES (%s, %s, %s, %s, %s, false, 'confirmed', %s)",
        (CAL, eid, title, local(start), local(end), attendees),
    )


def _task(db: Any, title: str, *, due: datetime | None = None, kind: str = "datetime", status: str = "open",
          updated: datetime | None = None, place_id: uuid.UUID | None = None) -> str:  # fmt: skip
    task_id = uuid.uuid4()
    now = updated or datetime.now(UTC)
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at, due_at, due_kind, "
        "place_id) VALUES (%s, %s, %s, %s, 'manual', %s, %s, %s, %s, %s)",
        (task_id, title, title.casefold(), status, now, now, due, kind if due else "none", place_id),
    )
    return str(task_id)


def _settings(db: Any, **patch: Any) -> None:
    from psycopg.types.json import Jsonb

    from pisa.domain import settings as setting_rules

    row = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
    value = setting_rules.apply_patch(setting_rules.with_defaults(row[0] if row else None, language="it-IT"),
                                      {"language": "it-IT", "quiet": {"hours": None}, **patch})  # fmt: skip
    db.execute("INSERT INTO app_settings (key, value) VALUES ('settings', %s) ON CONFLICT (key) DO UPDATE SET "
               "value = EXCLUDED.value", (Jsonb(value),))  # fmt: skip


def _hdr(db: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


# --- the contract -----------------------------------------------------------------------------------------


def test_rituals_get_put_and_the_rest_fixture(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.api import schemas

    hdr = _hdr(db)
    got = client.get("/v1/rituals", headers=hdr).json()
    assert got == json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "rituals.json").read_text())
    schemas.Rituals.model_validate(got)
    put = client.put(
        "/v1/rituals", headers=hdr, json={"ghost_run": {"on": False}, "closing": {"at": "21:00"}}
    )
    assert put.status_code == 200 and put.json()["ghost_run"] == {"on": False, "at": "22:00"}
    assert put.json()["closing"]["at"] == "21:00" and put.json()["one_thing"]["on"] is True
    bad = client.put("/v1/rituals", headers=hdr, json={"closing": {"at": "25:00"}})
    assert bad.status_code == 400 and "closing.at" in bad.json()["error"]["message"]
    assert (
        client.put("/v1/rituals", headers=hdr, json={"something_different": {"per_week": 9}}).status_code
        == 400
    )


def test_place_notes_rest_and_fixture(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.api import schemas

    schemas.PlaceNotes.model_validate(
        json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "place_notes.json").read_text())
    )
    hdr = _hdr(db)
    made = client.post("/v1/places/notes", headers=hdr, json={"text": "comprare il bagnoschiuma", "lat": 45.46,
                                                               "lon": 9.19, "place_name": "Esselunga"})  # fmt: skip
    assert made.status_code == 201 and made.json()["radius_m"] == 150
    assert (
        client.post(
            "/v1/places/notes", headers=hdr, json={"text": "x", "lat": 1, "lon": 1, "radius_m": 20}
        ).status_code
        == 400
    )
    listed = client.get("/v1/places/notes", headers=hdr).json()["notes"]
    assert [n["text"] for n in listed] == ["comprare il bagnoschiuma"]
    assert client.delete(f"/v1/places/notes/{listed[0]['id']}", headers=hdr).status_code == 204
    assert client.delete(f"/v1/places/notes/{listed[0]['id']}", headers=hdr).status_code == 404


def test_place_enter_over_the_websocket_fires_the_note(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.live_app import live

    _settings(db)
    clock.now = WED_22.replace(hour=18).astimezone(UTC)
    device_id, token = pair(db)
    note = client.portal.call(
        lambda: client.app.state.runtime.place_notes.create(
            "comprare il bagnoschiuma", 45.46, 9.19, place_name="Esselunga"
        )
    )
    with live(client, token, device_id) as app:
        app.custom("pisa.place_enter", {"noteIds": [note["id"]]})
        assert {m["type"] for m in app.ping()} <= {"notify"}
        app.check()
    row = db.execute("SELECT kind, text FROM notifications").fetchone()
    assert row == ("reminder", "Sei da Esselunga: comprare il bagnoschiuma.")


# --- row 1: Ghost Run -------------------------------------------------------------------------------------


def _ghost(client: Any, clock: Any, when: datetime = WED_22) -> bool:
    clock.now = when.astimezone(UTC)
    return client.portal.call(client.app.state.runtime.rituals.ghost_run)


def test_the_ghost_run_finds_one_problem_and_asks_one_fix(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "call", "Call con Marco", "2026-11-19T18:30", "2026-11-19T19:30", attendees=1)
    _event(db, "gym", "Palestra", "2026-11-19T18:00", "2026-11-19T19:00")
    assert _ghost(client, clock) is True
    text, ritual, sheet_id, ask_id = db.execute(
        "SELECT text, ritual, sheet_id, ask_id FROM notifications"
    ).fetchone()
    assert ritual == "ghost_run" and text.startswith("Domani, in 20 secondi:")
    assert "Sposto palestra alle 19:30?" in text  # the movable one (their own, nobody invited) moves
    ask = db.execute("SELECT kind, status, payload FROM ritual_asks WHERE id = %s", (ask_id,)).fetchone()
    assert ask[0] == "ghost_fix" and ask[1] == "open" and ask[2]["ref"] == "gym"
    data = db.execute("SELECT data FROM ritual_sheets WHERE id = %s", (sheet_id,)).fetchone()[0]
    assert data["blocks"][0]["type"] == "table" and [a["id"] for a in data["actions"]] == ["a1", "a2"]
    assert _ghost(client, clock) is False  # once a night
    # Nothing changed without their yes: the event is where it was.
    assert db.execute("SELECT starts_at FROM calendar_events WHERE id = 'gym'").fetchone()[0] == local(
        "2026-11-19T18:00"
    )


def test_no_push_when_tomorrow_is_empty_or_fine(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    assert _ghost(client, clock) is False  # empty
    _event(db, "a", "Dentista", "2026-11-19T09:00", "2026-11-19T10:00")
    db.execute("DELETE FROM app_settings WHERE key = 'rituals_ghost_run_ran'")  # rehearse again (M5 aside)
    assert _ghost(client, clock) is False  # nothing worth saying
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_the_fix_s_yes_by_tap_gives_the_task_its_block(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    task_id = _task(db, "Preparare le slide", due=local("2026-11-19T00:00"), kind="date")
    assert _ghost(client, clock) is True
    sheet_id, text = db.execute("SELECT sheet_id, text FROM notifications").fetchone()
    assert "Preparare le slide scade domani" in text and "Ti tengo preparare le slide alle 8?" in text
    hdr = _hdr(db)
    sheet = client.get(f"/v1/sheets/sheet:{sheet_id}", headers=hdr).json()
    assert sheet["kind"] == "sheet" and sheet["data"]["title"] == "Domani"
    done = client.post(f"/v1/sheets/sheet:{sheet_id}/actions/a1", headers=hdr)
    assert done.status_code == 200 and done.json()["said"] == "Fatto: Preparare le slide alle 08:00."
    start = db.execute("SELECT expected_from FROM tasks WHERE id = %s", (task_id,)).fetchone()[0]
    assert start == local("2026-11-19T08:00")
    again = client.post(f"/v1/sheets/sheet:{sheet_id}/actions/a2", headers=hdr)
    assert again.status_code == 409  # one answer


def test_quiet_hours_send_nothing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, quiet={"hours": {"start": "21:30", "end": "07:00"}})
    _task(db, "Preparare le slide", due=local("2026-11-19T00:00"), kind="date")
    clock.now = WED_22.astimezone(UTC)
    assert client.portal.call(client.app.state.runtime.rituals.tick) == []
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_the_problem_rules() -> None:
    def b(ref: str, start: str, end: str, movable: bool = True, place: str | None = None) -> rules.Block:
        return rules.Block("event", ref, ref.title(), local(start), local(end), place, movable=movable)

    clash = [
        b("call", "2026-11-19T18:30", "2026-11-19T19:30", movable=False),
        b("gym", "2026-11-19T18:00", "2026-11-19T19:00"),
    ]
    problem = rules.find_problem(clash, travel={}, due=[], rain_at=None, bike=False, lang="it-IT")
    assert problem is not None and problem.kind == "clash" and problem.fix is not None
    assert problem.fix.ref == "gym" and problem.fix.start == local("2026-11-19T19:30")
    both = [
        b("a", "2026-11-19T10:00", "2026-11-19T11:00", False),
        b("c", "2026-11-19T10:30", "2026-11-19T11:30", False),
    ]
    said = rules.find_problem(both, travel={}, due=[], rain_at=None, bike=False, lang="it-IT")
    assert said is not None and said.fix is None and said.question is None  # said, no fix: nobody's alone
    far = [
        b("a", "2026-11-19T10:00", "2026-11-19T11:00", place="p1"),
        b("b", "2026-11-19T11:10", "2026-11-19T12:00", place="p2"),
    ]
    transfer = rules.find_problem(
        far, travel={("a", "b"): 40}, due=[], rain_at=None, bike=False, lang="it-IT"
    )
    assert transfer is not None and transfer.kind == "transfer" and transfer.fix is not None
    assert transfer.fix.start == local("2026-11-19T11:45")  # after the arrival (11:40), on a quarter
    rain = rules.find_problem(
        [], travel={}, due=[], rain_at=local("2026-11-19T08:00"), bike=True, lang="it-IT"
    )
    assert rain is not None and rain.kind == "rain" and rain.fix is None


# --- row 2: one thing -------------------------------------------------------------------------------------


def test_the_morning_briefing_is_one_thing(client, db, clock, fake_models) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.settings import load

    _settings(db)
    clock.now = local("2026-11-18T08:00").astimezone(UTC)
    _task(db, "Pagare il bollo", due=local("2026-11-18T18:00"))
    _task(db, "Comprare il latte")
    _event(db, "d", "Dentista", "2026-11-18T10:00", "2026-11-18T11:00")
    runtime = client.app.state.runtime

    async def compose(trigger: str) -> str:
        async with runtime.pool.connection() as conn:
            settings = await load(conn, language="it-IT")
        return await runtime.briefings._compose(settings, clock.now, trigger)

    text = client.portal.call(compose, "time")
    assert text == "Una cosa sola oggi: pagare il bollo, scade oggi entro le 18."
    assert not fake_models.calls  # by code, no model
    client.put("/v1/rituals", headers=_hdr(db), json={"one_thing": {"on": False}})
    assert client.portal.call(compose, "time") != text  # off: the whole briefing again


def test_one_thing_rules() -> None:
    t = rules.Thing
    assert rules.one_thing(
        "it-IT", overdue=[t("Rinnovare il passaporto")], due_today=[t("x")], events=[], todo=[]
    ) == ("Una cosa sola oggi: rinnovare il passaporto, è già in ritardo.")
    assert rules.one_thing(
        "it-IT", overdue=[], due_today=[], events=[t("Dentista", local("2026-11-18T09:30"))], todo=[]
    ) == ("Una cosa sola oggi: dentista alle 9:30.")
    assert rules.one_thing("en-US", overdue=[], due_today=[], events=[], todo=[]) is None


# --- row 3: Chiudiamo ---------------------------------------------------------------------------------------


def test_closing_recaps_the_day_with_undo_per_line(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    clock.now = local("2026-11-18T21:30").astimezone(UTC)
    done = _task(db, "Comprare il latte", status="done", updated=local("2026-11-18T12:00"))
    _task(db, "Chiamare il commercialista", due=local("2026-11-19T10:00"))
    assert client.portal.call(client.app.state.runtime.rituals.tick) == ["closing"]
    text, ritual, sheet_id = db.execute("SELECT text, ritual, sheet_id FROM notifications").fetchone()
    assert ritual == "closing" and text.startswith("Chiudiamo?")
    data = db.execute("SELECT data FROM ritual_sheets WHERE id = %s", (sheet_id,)).fetchone()[0]
    items = [b.get("items") for b in data["blocks"] if b["type"] == "list"]
    assert items == [["✓ Comprare il latte"], ["Chiamare il commercialista"]]
    assert data["actions"][0]["style"] == "undo"
    hdr = _hdr(db)
    undone = client.post(f"/v1/sheets/sheet:{sheet_id}/actions/a1", headers=hdr)
    assert undone.status_code == 200 and undone.json()["said"] == "Annullato."
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (done,)).fetchone()[0] == "open"


def test_nothing_on_an_empty_day(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    clock.now = local("2026-11-18T21:30").astimezone(UTC)
    assert client.portal.call(client.app.state.runtime.rituals.tick) == []


# --- row 4: the week's shape --------------------------------------------------------------------------------


def test_the_week_poster_on_sunday_with_code_s_numbers(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    for n, day in enumerate(("16", "18", "20")):
        _event(db, f"g{n}", "Palestra", f"2026-11-{day}T18:00", f"2026-11-{day}T19:30")
    _event(db, "m", "Riunione budget", "2026-11-17T10:00", "2026-11-17T12:00")
    _event(db, "old", "Palestra", "2026-11-04T18:00", "2026-11-04T19:00")  # 2 weeks ago: the streak
    _task(db, "Pagare il bollo", status="done", updated=local("2026-11-19T12:00"))
    clock.now = local("2026-11-22T19:00").astimezone(UTC)  # Sunday
    assert client.portal.call(client.app.state.runtime.rituals.tick) == ["week_poster"]
    data = db.execute("SELECT data FROM ritual_sheets WHERE ritual = 'week_poster'").fetchone()[0]
    stats = {b["label"]: b["value"] for b in data["blocks"] if b["type"] == "stat"}
    assert stats["Palestra"] in ("4.5", "2 su 4")  # the hours and the streak share the label
    hours = [b for b in data["blocks"] if b["type"] == "stat" and b.get("unit") == "h"]
    assert {b["label"]: b["value"] for b in hours} == {"Riunioni": "2", "Palestra": "4.5", "Spostamenti": "0",
                                                       "Altro": "0", "Libero": "105.5"}  # fmt: skip
    streak = [b for b in data["blocks"] if b["type"] == "stat" and b.get("unit") == "sett."]
    assert streak[0]["value"] == "2 su 4" and stats["Fatti"] == "1"


# --- row 5: place memory ------------------------------------------------------------------------------------


def test_a_note_fires_once_a_visit_and_a_task_with_a_place_too(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    clock.now = local("2026-11-18T18:00").astimezone(UTC)
    place = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, lat, lon, created_at, updated_at) VALUES (%s, 'Lavanderia', 45.1, 9.1, now(), now())",
        (place,),
    )
    task_id = _task(db, "ritirare le camicie", place_id=place)
    hdr = _hdr(db)
    note = client.post("/v1/places/notes", headers=hdr, json={"text": "comprare il bagnoschiuma", "lat": 45.46,
                                                               "lon": 9.19, "place_name": "Esselunga"}).json()  # fmt: skip
    ids = [n["id"] for n in client.get("/v1/places/notes", headers=hdr).json()["notes"]]
    assert ids == [note["id"], f"task:{task_id}"]
    first = client.post("/v1/places/enter", headers=hdr, json={"note_ids": ids}).json()
    assert first["fired"] == ids
    assert (
        first["text"] == "Sei da Esselunga: comprare il bagnoschiuma. Ritirare le camicie: è qui, 2 minuti?"
    )
    clock.now += timedelta(hours=2)
    assert (
        client.post("/v1/places/enter", headers=hdr, json={"note_ids": ids}).json()["fired"] == []
    )  # one visit
    clock.now += timedelta(hours=5)
    assert client.post("/v1/places/enter", headers=hdr, json={"note_ids": [note["id"]]}).json()["fired"] == [
        note["id"]
    ]


def test_remember_at_place_takes_only_the_owner_s_words() -> None:
    assert rules.in_owner_words(
        "comprare il bagnoschiuma", "ricordami quando sono da Esselunga di comprare il bagnoschiuma"
    )
    assert not rules.in_owner_words(
        "comprare i biglietti per il concerto",
        "ricordami quando sono da Esselunga di comprare il bagnoschiuma",
    )


# --- row 6: something different -----------------------------------------------------------------------------


def _ideas(found: list[dict[str, str]]) -> Any:
    async def ideas(declined: set[str], used: set[str]) -> list[dict[str, str]]:
        return found

    return ideas


def test_an_idea_as_a_question_never_twice_a_no_remembered(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    runtime = client.app.state.runtime
    runtime.rituals._ideas = _ideas([
        {"kind": "restaurant", "key": "restaurant:sushi ko", "text": "Sushi Ko, via Immaginaria 3"},
        {"kind": "restaurant", "key": "restaurant:trattoria", "text": "Trattoria Da Gino"},
        {"kind": "museum", "key": "museum:mudec", "text": "il Mudec"},
    ])  # fmt: skip
    clock.now = local("2026-11-18T15:00").astimezone(UTC)  # a weekday afternoon: work hours, no
    assert client.portal.call(runtime.rituals.something_different) is False
    clock.now = local("2026-11-18T19:00").astimezone(UTC)
    _event(db, "w", "Cena con Anna", "2026-11-18T20:00", "2026-11-18T22:00")  # booked within 3 h: no
    assert client.portal.call(runtime.rituals.something_different) is False
    db.execute("DELETE FROM calendar_events")
    assert client.portal.call(runtime.rituals.something_different) is True
    text, ask_id = db.execute("SELECT text, ask_id FROM notifications").fetchone()
    assert text == "Vuoi provare qualcosa di diverso oggi? Avevo in mente: Sushi Ko, via Immaginaria 3."
    assert client.portal.call(runtime.rituals.something_different) is False  # one a day
    assert client.portal.call(runtime.rituals.answer, ask_id, False, "voice") == (
        "Va bene, per questo genere non te lo chiedo più."
    )
    clock.now = local("2026-11-21T11:00").astimezone(UTC)  # Saturday morning
    assert client.portal.call(runtime.rituals.something_different) is True
    second = db.execute("SELECT text FROM notifications ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    assert "il Mudec" in second  # never the same idea, never the kind they said no to
    clock.now = local("2026-11-22T11:00").astimezone(UTC)
    assert client.portal.call(runtime.rituals.something_different) is False  # 2 a week


# --- row 7: hunches -----------------------------------------------------------------------------------------


def _say(client: Any, device: str, text: str) -> str:
    runtime = client.app.state.runtime

    async def emit(delta: str, final: bool) -> None:
        return None

    return client.portal.call(lambda: runtime.conversations.respond(uuid.UUID(device), text, "it-IT", emit,
                                                                    trace_id=uuid.uuid4().hex))  # fmt: skip


async def _in_a_turn(client: Any, device: str, guess: str, evidence: list[str]) -> str:
    """note_hunch as the brain calls it: inside the device's turn (the turn that asks it)."""
    from pisa.services.conversation import TurnState, current_turn

    runtime = client.app.state.runtime
    turn = TurnState(uuid.uuid4(), "stasera sushi")
    runtime.conversations._last_turn[uuid.UUID(device)] = turn
    token = current_turn.set(turn)
    try:
        said = await runtime.rituals.note_hunch(guess, evidence, uuid.UUID(device))
    finally:
        current_turn.reset(token)
        turn.asked = runtime.rituals.unasked.mark()
    if said.startswith("Stored"):  # Pisa's reply asks it (M4: only a question Pisa said binds a yes)
        await runtime.conversations.remember(uuid.UUID(device), "stasera sushi", "it-IT", guess, "it-IT")
    return said


def test_a_hunch_is_asked_once_and_his_yes_confirms_it(client, db, clock, fake_models) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    device, _ = pair(db)
    runtime = client.app.state.runtime
    said = client.portal.call(
        _in_a_turn, client, device, "ti piace il sushi il venerdì?", ["3 venerdì su 4 sushi"]
    )
    assert said.startswith("Stored as a hunch") and "ti piace il sushi il venerdì?" in said
    assert _say(client, device, "sì") == "Segnato, grazie."
    assert not fake_models.calls  # by code
    assert db.execute("SELECT status FROM hunches").fetchone()[0] == "confirmed"
    claim = db.execute(
        "SELECT c.provenance, c.statement, (SELECT count(*) FROM profile_evidence e WHERE e.claim_id = c.id "
        "AND e.signal = 'hunch') FROM hunches h JOIN profile_claims c ON c.id = h.claim_id"
    ).fetchone()
    assert claim == ("confirmed", "ti piace il sushi il venerdì", 1)  # a claim, its evidence linked
    again = client.portal.call(_in_a_turn, client, device, "Ti piace il sushi il venerdì", ["x"])
    assert again.startswith("Already confirmed")  # never asked twice
    assert client.portal.call(runtime.rituals.hunches) == ["ti piace il sushi il venerdì?"]


def test_a_yes_after_something_newer_never_answers_the_hunch(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    device, _ = pair(db)
    runtime = client.app.state.runtime
    client.portal.call(_in_a_turn, client, device, "ti piace correre la mattina?", ["corse alle 7"])
    latest = runtime.conversations._last_turn[uuid.UUID(device)]
    runtime.rituals.unasked.said(
        uuid.UUID(device), None
    )  # Pisa said something else unasked since: newest wins
    assert client.portal.call(runtime.rituals.answer_newest, uuid.UUID(device), True, latest) is None
    other = object()  # or a later turn of Pisa's came in between
    assert client.portal.call(runtime.rituals.answer_newest, uuid.UUID(device), True, other) is None
    assert db.execute("SELECT status FROM hunches").fetchone()[0] == "asked"


def test_a_ghost_run_notice_is_not_answered_after_a_newer_reply() -> None:
    from pisa.services.rituals import RitualsService
    from pisa.services.unasked import Unasked

    unasked = Unasked()
    rituals = RitualsService(None, owner=owner_at(), unasked=unasked)  # type: ignore[arg-type]
    device, ask = uuid.uuid4(), uuid.uuid4()
    unasked.said(device, f"ask:{ask}")

    class Turn:
        asked: int | None = None

    before = Turn()
    before.asked = unasked.mark() - 5  # a turn of Pisa's before the notice
    assert rituals.newest_ask(device, before) == ask
    after = Turn()
    after.asked = unasked.mark()  # Pisa replied after the notice: its own question is the newest
    assert rituals.newest_ask(device, after) is None


def test_switch_only_on_his_words() -> None:
    assert rules.said_switch("spegni il Ghost Run", "ghost_run", False)
    assert not rules.said_switch("spegni la luce", "ghost_run", False)
    assert rules.said_switch("riattiva chiudiamo", "closing", True)


def test_a_move_is_in_one_list_with_its_undo() -> None:
    line = rules.DoneLine("Palestra spostata alle 19.", {"do": "undo_approval", "ref": "x"})
    made = rules.closing_sheet("it-IT", [], [line], [])
    assert made is not None
    data, actions = made
    assert [b["type"] for b in data["blocks"]] == ["section", "list"]
    assert actions == [
        {
            "id": "a1",
            "label": "Annulla: Palestra spostata alle 19.",
            "style": "undo",
            "do": "undo_approval",
            "ref": "x",
        }
    ]


def test_a_sheet_button_is_single_use_and_ends(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    clock.now = local("2026-11-18T21:30").astimezone(UTC)
    _task(db, "Comprare il latte", status="done", updated=local("2026-11-18T12:00"))
    _task(db, "Comprare il pane", status="done", updated=local("2026-11-18T13:00"))
    assert client.portal.call(client.app.state.runtime.rituals.tick) == ["closing"]
    (sheet_id,) = db.execute("SELECT sheet_id FROM notifications").fetchone()
    hdr = _hdr(db)
    before = client.get(f"/v1/sheets/sheet:{sheet_id}", headers=hdr).json()
    assert all(set(a) == {"id", "label", "style"} for a in before["data"]["actions"])  # effects stay on Pisa
    assert client.post(f"/v1/sheets/sheet:{sheet_id}/actions/a1", headers=hdr).status_code == 200
    assert client.post(f"/v1/sheets/sheet:{sheet_id}/actions/a1", headers=hdr).status_code == 409  # once
    after = client.get(f"/v1/sheets/sheet:{sheet_id}", headers=hdr).json()
    assert [a["id"] for a in after["data"]["actions"]] == ["a2"]  # M2: a used button leaves the sheet
    assert after["revision"] > before["revision"]
    clock.now += timedelta(hours=25)
    late = client.post(f"/v1/sheets/sheet:{sheet_id}/actions/a2", headers=hdr)
    assert late.status_code == 409 and late.json()["error"]["code"] == "approval_expired"
    assert "actions" not in client.get(f"/v1/sheets/sheet:{sheet_id}", headers=hdr).json()["data"]


def test_a_ghost_run_due_in_quiet_hours_goes_at_his_first_minute_if_still_relevant(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, quiet={"hours": {"start": "21:30", "end": "07:00"}})
    _event(db, "call", "Call con Marco", "2026-11-19T18:30", "2026-11-19T19:30", attendees=1)
    _event(db, "gym", "Palestra", "2026-11-19T18:00", "2026-11-19T19:00")
    rituals = client.app.state.runtime.rituals
    clock.now = WED_22.astimezone(UTC)
    assert client.portal.call(rituals.tick) == []  # quiet: nothing now
    clock.now = local("2026-11-19T07:00").astimezone(UTC)
    assert "ghost_run" in client.portal.call(rituals.tick)  # their first minute: the fix is still ahead
    assert "Sposto palestra alle 19:30?" in db.execute("SELECT text FROM notifications").fetchone()[0]
    assert client.portal.call(rituals.tick) == []  # once


def test_a_deferred_ghost_run_no_longer_relevant_is_dropped(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, quiet={"hours": {"start": "21:30", "end": "07:00"}})
    _event(db, "call", "Call con Marco", "2026-11-19T07:00", "2026-11-19T08:00", attendees=1)
    _event(db, "gym", "Colazione con Anna", "2026-11-19T06:30", "2026-11-19T07:30", attendees=1)  # no fix
    rituals = client.app.state.runtime.rituals
    clock.now = WED_22.astimezone(UTC)
    client.portal.call(rituals.tick)
    clock.now = local("2026-11-19T07:00").astimezone(UTC)
    assert client.portal.call(rituals.tick) == []  # no fix and the day has begun: dropped
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


# --- review fixes -------------------------------------------------------------------------------------


class _Agent:
    """Enough of an agent to call the ritual tools directly."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool_plain(self, fn: Any) -> Any:
        self.tools[fn.__name__] = fn
        return fn

    def instructions(self, fn: Any) -> Any:
        return fn


def _tools(client: Any, words: str, device: str) -> dict[str, Any]:
    from pisa.brain.profile_tools import ProfileTurn
    from pisa.brain.ritual_tools import add_ritual_tools

    runtime = client.app.state.runtime
    agent = _Agent()
    add_ritual_tools(agent, runtime.rituals, runtime.place_notes, None, lambda: None,  # type: ignore[arg-type]
                     ProfileTurn(words=words, device=uuid.UUID(device), lang="it-IT"))  # fmt: skip
    return agent.tools


async def _tool_in_turn(client: Any, device: str, tool: Any, before: Any, outside: bool, *args: Any) -> Any:
    from pisa.brain.untrusted import watch_turn
    from pisa.services.conversation import TurnState, current_turn

    turn = TurnState(uuid.uuid4(), "sì")
    turn.before = before
    token = current_turn.set(turn)
    seen = watch_turn()
    if outside:
        seen.read("email")  # this turn read an email
    try:
        return await tool(*args)
    finally:
        current_turn.reset(token)


def _deadline_ghost(client: Any, db: Any, clock: Any) -> uuid.UUID:
    _settings(db)
    _task(db, "Preparare le slide", due=local("2026-11-19T00:00"), kind="date")
    assert _ghost(client, clock) is True
    return db.execute("SELECT ask_id FROM notifications").fetchone()[0]


def test_answer_my_question_never_answers_an_older_notice(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.conversation import TurnState

    ask = _deadline_ghost(client, db, clock)
    device, _ = pair(db)
    runtime = client.app.state.runtime
    runtime.rituals.unasked.said(uuid.UUID(device), f"ask:{ask}")  # the notice reached the phone
    newer = TurnState(uuid.uuid4(), "e domani?")
    newer.asked = runtime.rituals.unasked.mark()  # then Pisa replied with its own question
    tool = _tools(client, "sì", device)["answer_my_question"]
    said = client.portal.call(_tool_in_turn, client, device, tool, newer, False, True)
    assert said == "Nothing of Pisa's is waiting for that answer."
    assert db.execute("SELECT status FROM ritual_asks WHERE id = %s", (ask,)).fetchone()[0] == "open"
    older = TurnState(uuid.uuid4(), "ciao")
    older.asked = 0  # a turn before the notice: the notice is the newest
    refused = client.portal.call(_tool_in_turn, client, device, tool, older, True, True)
    assert refused.startswith("Not answered")  # nothing Pisa read answers it
    said = client.portal.call(_tool_in_turn, client, device, tool, older, False, True)
    assert said == "Fatto: Preparare le slide alle 08:00."


def test_a_fix_on_a_changed_day_is_refused(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "call", "Call con Marco", "2026-11-19T18:30", "2026-11-19T19:30", attendees=1)
    _event(db, "gym", "Palestra", "2026-11-19T18:00", "2026-11-19T19:00")
    assert _ghost(client, clock) is True
    ask = db.execute("SELECT ask_id FROM notifications").fetchone()[0]
    runtime = client.app.state.runtime

    class Moves:
        async def move_on_yes(self, *args: Any) -> tuple[bool, str]:
            return True, "Spostata."  # the calendar write itself would succeed

    runtime.rituals._actions = Moves()
    db.execute("UPDATE calendar_events SET starts_at = %s, ends_at = %s WHERE id = 'gym'",
               (local("2026-11-19T20:00"), local("2026-11-19T21:00")))  # fmt: skip  # they moved it themselves
    said = client.portal.call(client.app.state.runtime.rituals.answer, ask, True, "tap")
    assert said == "Non ci sono riuscito: è cambiato qualcosa."
    assert db.execute("SELECT starts_at FROM calendar_events WHERE id = 'gym'").fetchone()[0] == local(
        "2026-11-19T20:00"
    )


def test_a_fix_whose_slot_was_taken_is_refused(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    ask = _deadline_ghost(client, db, clock)
    _event(db, "new", "Colazione con Anna", "2026-11-19T08:00", "2026-11-19T09:00", attendees=1)
    said = client.portal.call(client.app.state.runtime.rituals.answer, ask, True, "tap")
    assert said == "Non ci sono riuscito: è cambiato qualcosa."
    assert db.execute("SELECT expected_from FROM tasks").fetchone()[0] is None


def test_the_recap_lists_tasks_closed_today_and_undoes_only_within_24h(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    old = _task(db, "Pagare il bollo", status="done", updated=local("2026-11-10T12:00"))
    db.execute(
        "UPDATE tasks SET notes = 'x', updated_at = %s WHERE id = %s", (local("2026-11-18T12:00"), old)
    )
    done = _task(db, "Comprare il latte", status="done", updated=local("2026-11-18T12:00"))
    clock.now = local("2026-11-18T21:30").astimezone(UTC)
    made = client.portal.call(client.app.state.runtime.rituals.closing_sheet)
    assert made is not None and made[0]["blocks"][1]["items"] == [
        "✓ Comprare il latte"
    ]  # edited today ≠ done today
    assert client.portal.call(client.app.state.runtime.rituals.tick) == ["closing"]
    (sheet_id,) = db.execute("SELECT sheet_id FROM notifications").fetchone()
    db.execute(
        "UPDATE tasks SET status = 'open', updated_at = now() WHERE id = %s", (done,)
    )  # reopened already
    tap = client.post(f"/v1/sheets/sheet:{sheet_id}/actions/a1", headers=_hdr(db))
    assert tap.status_code == 409 and tap.json()["error"]["code"] == "undo_expired"


def test_a_hunch_is_a_candidate_claim_and_a_no_retires_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    device, _ = pair(db)
    runtime = client.app.state.runtime
    client.portal.call(_in_a_turn, client, device, "ti piace correre la mattina?", ["corse alle 7, 3 volte"])
    state = db.execute(
        "SELECT c.provenance, c.state FROM hunches h JOIN profile_claims c ON c.id = h.claim_id"
    ).fetchone()
    assert state == ("inferred", "candidate")  # in About you, never in a prompt
    assert "correre" not in " ".join(client.portal.call(runtime.profile.core_profile))
    assert _say(client, device, "no") == "Ok, me lo tolgo dalla testa."
    after = db.execute(
        "SELECT c.state, c.retired_by, c.against FROM hunches h JOIN profile_claims c ON c.id = h.claim_id"
    ).fetchone()
    assert after == ("retired", "owner", 3.0)
    runtime.rituals._profile = None  # the rituals' own check, not only the profile's
    refused = client.portal.call(_in_a_turn, client, device, "hai problemi di salute col cuore?", ["x"])
    assert refused.startswith("Not stored") and db.execute("SELECT count(*) FROM hunches").fetchone()[0] == 1


def test_a_hunch_pisa_never_said_is_not_answered(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.conversation import TurnState, current_turn

    _settings(db)
    device, _ = pair(db)
    runtime = client.app.state.runtime

    async def noted_but_not_said() -> None:
        turn = TurnState(uuid.uuid4(), "stasera sushi")
        runtime.conversations._last_turn[uuid.UUID(device)] = turn
        token = current_turn.set(turn)
        try:
            await runtime.rituals.note_hunch("ti piace il sushi?", ["sushi"], uuid.UUID(device))
        finally:
            current_turn.reset(token)
            turn.asked = runtime.rituals.unasked.mark()
        await runtime.conversations.remember(
            uuid.UUID(device), "stasera sushi", "it-IT", "Buon appetito.", "it-IT"
        )

    client.portal.call(noted_but_not_said)
    latest = runtime.conversations._last_turn[uuid.UUID(device)]
    assert client.portal.call(runtime.rituals.answer_newest, uuid.UUID(device), True, latest) is None
    assert db.execute("SELECT status FROM ritual_asks").fetchone()[0] == "lapsed"


def test_the_ghost_run_rehearses_once_a_day_whatever_it_found(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "a", "Dentista", "2026-11-19T09:00", "2026-11-19T10:00")
    assert _ghost(client, clock) is False  # fine: nothing said
    _event(db, "b", "Call", "2026-11-19T09:30", "2026-11-19T10:30", attendees=1)
    assert _ghost(client, clock, WED_22 + timedelta(minutes=1)) is False  # never again in its window


def test_rails_with_their_own_tests(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    ask = _deadline_ghost(client, db, clock)
    clock.now = local("2026-11-19T08:00").astimezone(UTC)  # the fix's start: the question has lapsed
    assert client.portal.call(runtime.rituals.answer, ask, True, "voice") is None
    device, _ = pair(db)
    db.execute("INSERT INTO places (id, name, lat, lon, created_at, updated_at) VALUES (gen_random_uuid(), "
               "'Esselunga', 45.4, 9.1, now(), now())")  # fmt: skip
    words = "ricordami quando sono da Esselunga di comprare il bagnoschiuma"
    tools = _tools(client, words, device)
    from_email = client.portal.call(
        _tool_in_turn,
        client,
        device,
        tools["remember_at_place"],
        None,
        True,
        "comprare il bagnoschiuma",
        "Esselunga",
    )
    # their own ask this turn, nothing in it that only an email holds: a read email alone no longer refuses
    assert from_email.startswith("Saved")
    hunch = client.portal.call(
        _tool_in_turn, client, device, tools["note_hunch"], None, True, "ti piace Esselunga?", ["x"]
    )
    assert hunch.startswith("Not stored")


def test_ideas_at_per_week_and_d042_s_switch(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    runtime = client.app.state.runtime
    runtime.rituals._ideas = _ideas(
        [{"kind": "a", "key": "a:1", "text": "uno"}, {"kind": "b", "key": "b:1", "text": "due"}]
    )
    client.put("/v1/rituals", headers=_hdr(db), json={"something_different": {"per_week": 1}})
    clock.now = local("2026-11-18T19:00").astimezone(UTC)
    assert client.portal.call(runtime.rituals.something_different) is True
    clock.now = local("2026-11-21T11:00").astimezone(UTC)
    assert client.portal.call(runtime.rituals.something_different) is False  # 1 a week
    _settings(db, profile={"something_different": False})
    client.put("/v1/rituals", headers=_hdr(db), json={"something_different": {"per_week": 7}})
    assert client.portal.call(runtime.rituals.something_different) is False  # About you's own switch


def test_place_enter_in_quiet_hours_fires_nothing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, quiet={"hours": {"start": "22:30", "end": "07:00"}})
    clock.now = local("2026-11-18T23:00").astimezone(UTC)
    hdr = _hdr(db)
    note = client.post("/v1/places/notes", headers=hdr, json={"text": "pane", "lat": 45.4, "lon": 9.1}).json()
    assert client.post("/v1/places/enter", headers=hdr, json={"note_ids": [note["id"]]}).json() == {
        "fired": [],
        "text": None,
    }
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_the_place_is_in_his_words_and_like_is_escaped(client, db) -> None:  # type: ignore[no-untyped-def]
    assert rules.place_in_words("Esselunga", "ricordami quando sono da Esselunga di comprare il pane")
    assert not rules.place_in_words("Coop Lorenteggio", "ricordami quando sono lì di comprare il pane")
    db.execute(
        "INSERT INTO places (id, name, lat, lon, created_at, updated_at) VALUES (gen_random_uuid(), 'Esselunga', 45.4, 9.1, now(), now())"
    )
    known = client.app.state.runtime.place_notes.known_place
    assert client.portal.call(known, "Esse_unga") is None and client.portal.call(known, "%") is None
    assert client.portal.call(known, "esselunga") is not None
    device, _ = pair(db)
    tools = _tools(client, "ricordami quando sono lì di comprare il pane", device)
    said = client.portal.call(
        _tool_in_turn,
        client,
        device,
        tools["remember_at_place"],
        None,
        False,
        "comprare il pane",
        "Esselunga",
    )
    assert said.startswith("Not saved: the place must be one the user named")


def test_an_idea_is_never_repeated_after_the_asks_are_pruned(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    runtime = client.app.state.runtime
    runtime.rituals._ideas = _ideas([{"kind": "a", "key": "a:1", "text": "uno"}])
    clock.now = local("2026-11-18T19:00").astimezone(UTC)
    assert client.portal.call(runtime.rituals.something_different) is True
    db.execute("DELETE FROM notifications")
    db.execute("DELETE FROM ritual_asks")  # 90 days later, pruned
    clock.now = local("2026-11-25T19:00").astimezone(UTC)
    assert client.portal.call(runtime.rituals.something_different) is False


def test_l8(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, quiet={"hours": {"start": "22:30", "end": "07:00"}})
    _event(db, "call", "Call con Marco", "2026-11-19T15:30", "2026-11-19T16:30", attendees=1)
    _event(db, "gym", "Palestra", "2026-11-19T15:00", "2026-11-19T16:00")
    assert _ghost(client, clock) is True
    expires = db.execute("SELECT expires_at FROM ritual_asks").fetchone()[0]
    assert expires == local("2026-11-19T15:00")  # L4: lapses at the event's own start, the earlier
    hdr = _hdr(db)
    bad = client.put("/v1/rituals", headers=hdr, json={"closing": {"at": "23:00"}})  # quiet from 22:30
    assert bad.status_code == 400 and "quiet hours" in bad.json()["error"]["message"]  # L5
    runtime = client.app.state.runtime
    clock.now = local("2026-11-18T21:30").astimezone(UTC)
    _task(db, "Comprare\x07 il latte", status="done", updated=local("2026-11-18T12:00"))
    assert client.portal.call(runtime.rituals.closing) is True
    (pushed,) = db.execute("SELECT sheet_id FROM notifications WHERE ritual = 'closing'").fetchone()
    sheet_id, data = client.portal.call(runtime.rituals.closing_now)
    assert sheet_id == str(pushed)  # L7: the push's sheet still opens
    assert client.get(f"/v1/sheets/sheet:{pushed}", headers=hdr).status_code == 200
    assert "\x07" not in data["actions"][0]["label"]  # L8


# --- the merge with the rituals' later rows -----------------------------------------------------------------------------


def test_chiudiamo_reads_phase_3_s_ledger_with_its_undo(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    clock.now = local("2026-11-18T21:30").astimezone(UTC)
    task = _task(db, "Comprare il latte", status="done", updated=local("2026-11-18T12:00"))
    db.execute(
        "INSERT INTO pisa_decisions (id, at, move, tool, ref, provenance, outcome) VALUES (gen_random_uuid(), %s, "
        "'act', 'complete_task', %s, 'owner_words', 'done')",
        (local("2026-11-18T12:00"), task),
    )
    made = client.portal.call(client.app.state.runtime.rituals.closing_sheet)
    assert made is not None
    data, actions = made
    assert data["blocks"][1]["items"] == ["✓ Comprare il latte"]  # once: the ledger's line, not twice
    assert all(a["do"] == "undo_ledger" for a in actions)  # its own Undo, when it has one


def test_ferma_tutto_pauses_the_rituals() -> None:
    from pisa.services.worker_steps import INITIATIVE_STEPS

    assert "rituals" in INITIATIVE_STEPS  # Pisa's own initiative waits while they said "ferma tutto"
