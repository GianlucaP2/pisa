# ruff: noqa: E501
"""Corollary discharge: Pisa must not hear its own writes as the user's or the world's. One test per behaviour;
each fails with its fix reverted.
Each reader test writes one Pisa item and one owner item and asserts only their comes back as evidence."""

import asyncio
import uuid
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pisa.domain import understand
from pisa.domain.actions import grounded_change
from tests.conftest import Clock, dsn
from tests.native import native
from tests.owner import owner_at
from tests.test_approvals import auth, google  # noqa: F401  # pyright: ignore[reportUnusedImport]  (the fixtures)
from tests.test_research_eval import NOW
from tests.test_understanding_service import turn, week  # noqa: F401  (the fixture)


@pytest.fixture(autouse=True)
def model_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from pisa.brain import understand as small

    monkeypatch.setattr(small, "UNDERSTAND_S", 5.0)


def _pool_run(db: psycopg.Connection, action: Any) -> Any:
    async def go() -> Any:
        from pisa.db.pool import open_pool

        pool = await open_pool(dsn(db), max_size=2)
        await pool.wait()
        try:
            return await action(pool)
        finally:
            await pool.close()

    return asyncio.run(go())


# --- Row 1: their raw words are what they said ---------------------------------------------------------------------


def test_a_changed_turn_stores_his_words_and_js_meant_apart(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    t = turn(db, secrets_key, "Ehm, chiama nino ferry", confirm="same")
    assert t.understood and t.understood[0]["meant"] == "chiama Nino Ferri"
    content, meant, fixes = db.execute(
        "SELECT content, meant, corrections FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT 1"
    ).fetchone()  # type: ignore[misc]
    assert content == "Ehm, chiama nino ferry"  # their recognised words, as they said them
    assert meant == "chiama Nino Ferri"  # Pisa's understanding, beside them
    assert fixes == [{"from": "nino ferry", "to": "Nino Ferri", "via": "model"}]
    # what the model reads in history does not change: the marked form, rebuilt byte for byte
    step = understand.Result(
        meant="chiama Nino Ferri",
        changed=True,
        corrections=[understand.Correction("nino ferry", "Nino Ferri", 1.0, "model")],
    )
    old = understand.marked(step, "Ehm, chiama nino ferry", "it-IT")

    async def read(pool: Any) -> Any:
        from pisa.services.conversation import _recent_turns

        async with pool.connection() as conn:
            cid = (await (await conn.execute("SELECT id FROM conversations LIMIT 1")).fetchone())[0]  # type: ignore[index]
            return await _recent_turns(conn, cid)

    turns = _pool_run(db, read)
    user = next(x for x in turns if x.role == "user")
    assert user.content == old
    assert (
        user.said == "Ehm, chiama Nino Ferri"
    )  # their words, the correction applied: never Pisa's paraphrase


def test_a_time_only_in_js_meant_does_not_ground_a_change(db, week) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    """Earlier owner words ground a done in a later turn of the talk. Pisa's `meant` carries a claim
    and a time their words never said (nor a correction): neither grounds."""
    conversation = uuid.uuid4()
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()[0]  # type: ignore[index]
    db.execute(
        "INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation, device, NOW, NOW),
    )
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at, meant, corrections) "
        "VALUES (%s, 'user', 'ehm la spesa', 'it-IT', %s, 'ho fatto la spesa alle 21', '[]')",
        (conversation, NOW),
    )

    async def read(pool: Any) -> Any:
        from pisa.services.conversation import _recent_turns

        async with pool.connection() as conn:
            return await _recent_turns(conn, conversation)

    (user,) = _pool_run(db, read)
    assert "21" not in (user.said or "") and "fatto" not in (user.said or "")
    args: dict[str, str] = {}
    assert not grounded_change("mark_done", args, user.said or "", date(2026, 10, 20), title="la spesa")
    assert grounded_change(
        "mark_done", args, "ho fatto la spesa alle 21", date(2026, 10, 20), title="la spesa"
    )  # Pisa's words would


def test_his_earlier_words_in_the_talk_are_his_not_js(db, secrets_key, week, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    """The earlier turns of the talk reach the change tools (ProfileTurn.earlier) as their words with
    the step's corrections, never the marked form with Pisa's paraphrase."""
    from pisa.services import conversation

    seen: list[tuple[str, ...]] = []
    real = conversation.ProfileTurn

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(tuple(kwargs.get("earlier", ())))
        return real(*args, **kwargs)

    monkeypatch.setattr(conversation, "ProfileTurn", spy)
    first = turn(db, secrets_key, "Ehm, chiama nino ferry", confirm="same")
    turn(db, secrets_key, "e poi?", device=first.device)
    assert seen[-1] == ("Ehm, chiama Nino Ferri",)


def test_a_personal_eval_has_his_words_as_his_turn(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    t = turn(db, secrets_key, "Ehm, chiama nino ferry", confirm="same")
    device = t.device
    assert device is not None

    async def heard(pool: Any) -> Any:
        from pisa.services.personal_evals import PersonalEvals

        evals = PersonalEvals(pool, owner=owner_at(), clock=Clock(NOW + timedelta(minutes=1)))
        return await evals.heard(device, "No, hai sbagliato", "it-IT")

    assert _pool_run(db, heard)
    (scenario,) = db.execute("SELECT scenario FROM personal_evals").fetchone()  # type: ignore[misc]
    assert scenario["turns"][0]["owner"] == "Ehm, chiama nino ferry"
    assert "understanding" not in str(scenario["turns"])


# --- Row 2: Pisa's sent mail is Pisa's -------------------------------------------------------------------------------


def _mail(
    db: psycopg.Connection,
    mid: str,
    body: str,
    *,
    thread: str | None = None,
    subject: str = "Re: contratto",
    sender: str = "owner@example.org",
    labels: tuple[str, ...] = ("SENT",),
    to: str = "Marco Rossi <marco@example.invalid>",
    pending: bool = False,
    j: bool = False,
) -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, to_addresses, subject, sent_at, labels, "
        "snippet, body, intake_status, intake_category) VALUES (%s, %s, 'Owner', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            mid,
            thread or f"t-{mid}",
            sender,
            [to],
            subject,
            NOW - timedelta(hours=2),
            list(labels),
            body[:100],
            body,
            "pending" if pending else "done",
            None if pending else "other",
        ),
    )
    if j:  # what `_do_send` keeps: the id Gmail's send returned
        db.execute(
            "INSERT INTO pisa_sent_mail (gmail_message_id, draft_id, sent_at) VALUES (%s, 'd1', %s)",
            (mid, NOW),
        )


def test_a_send_keeps_the_id_gmail_returned(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.test_approvals import decide, pending, run, thread

    thread(google, db)
    actions = client.app.state.runtime.actions
    run(client, actions.draft_reply, "Claudio cover", "Ciao, la porto sabato.")
    (approval,) = pending(client, auth)
    assert decide(client, auth, approval["id"], how="voice").json()["status"] == "done"
    (draft_id,) = google.sent
    assert db.execute("SELECT gmail_message_id, draft_id FROM pisa_sent_mail").fetchall() == [
        ("m-sent", draft_id)
    ]


def test_js_sent_promise_is_no_task_his_identical_mail_is(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.actions_fake import connect
    from tests.assistant_env import set_settings

    set_settings(db, {"language": "it-IT"})
    connect(db, secrets_key)
    words = "Buongiorno,\nLe mando il contratto entro venerdì.\nCordiali saluti"
    _mail(db, "j1", words, j=True)  # Pisa drafted it, they said yes, Gmail sent it
    _mail(db, "o1", words)  # their own, the same words
    client.portal.call(client.app.state.runtime.money_back.scan)
    refs = [r[0] for r in db.execute("SELECT source_ref FROM intentions").fetchall()]  # Proposed
    assert refs and set(refs) == {"o1"}


def test_his_style_reads_only_his_own_sent_bodies(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.people_offers import PeopleOffers
    from tests.test_people import person

    pid = person(db, "Marco Rossi", ["marco@example.invalid"])
    _mail(db, "j1", "Gentile Marco,\nin allegato il file.\nCordiali saluti, Pisa", j=True)
    _mail(db, "o1", "Ciao Marco!\nci vediamo.\nUn abbraccio, R.")

    async def style(pool: Any) -> Any:
        async with pool.connection() as conn:
            return await PeopleOffers(pool, owner=owner_at())._style(conn, pid, "it-IT")

    got = _pool_run(db, style)
    assert got.seen == 1 and got.greeting == "Ciao"  # one body: theirs, never Pisa's "Gentile"


def test_js_sent_mail_is_no_receipt(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.money import MoneyService
    from tests.assistant_env import set_settings

    set_settings(db, {"language": "it-IT"})
    body = "Pagamento ricevuto: 12,99 € grazie"
    _mail(
        db, "j3", body, subject="Ricevuta di pagamento", labels=("SENT",), j=True
    )  # Pisa's accountant package
    _mail(db, "o3", body, subject="Ricevuta di pagamento", labels=("INBOX",), sender="cassa@negozio.example")

    async def scan(pool: Any) -> int:
        return await MoneyService(pool, owner=owner_at(), clock=Clock(NOW)).scan()

    assert _pool_run(db, scan) == 1
    assert db.execute("SELECT email_id, kind FROM money_seen").fetchall() == [("o3", "receipt")]


# --- Row 3: Pisa's events are Pisa's ---------------------------------------------------------------------------------

PISA_EVENT = uuid.UUID("5f0c4b8e-0a7e-4d2b-9a11-2f3c4d5e6f70")


def test_the_sync_reads_js_tag_on_a_create_only(db) -> None:  # type: ignore[no-untyped-def]
    from zoneinfo import ZoneInfo

    from pisa.connectors.google.calendar import parse_event
    from pisa.services.google_sync import _upsert_event

    tz = ZoneInfo("Europe/Rome")
    base = {"updated": "2026-10-20T10:00:00Z", "start": {"dateTime": "2026-10-21T20:00:00+02:00"},
            "end": {"dateTime": "2026-10-21T22:00:00+02:00"}, "summary": "Cena"}  # fmt: skip
    tag = {"private": {"pisa": "1", "pisa_action": str(PISA_EVENT)}}
    made = parse_event("primary", {**base, "id": PISA_EVENT.hex, "extendedProperties": tag}, tz)
    moved = parse_event(
        "primary", {**base, "id": "hisownevent1", "extendedProperties": tag}, tz
    )  # Pisa moved it
    his = parse_event("primary", {**base, "id": "hisownevent2"}, tz)
    assert (made.made_by_pisa, moved.made_by_pisa, his.made_by_pisa) == (True, False, False)
    db.execute("INSERT INTO calendars (id, name) VALUES ('primary', 'P')")

    async def store(pool: Any) -> None:
        async with pool.connection() as conn:
            for e in (made, moved, his):
                await _upsert_event(conn, e)

    _pool_run(db, store)
    rows = dict(db.execute("SELECT id, made_by_pisa FROM calendar_events").fetchall())
    assert rows == {PISA_EVENT.hex: True, "hisownevent1": False, "hisownevent2": False}
    # synced again (a later change in Google, the tag not in that answer): once Pisa's, Pisa's
    again = parse_event("primary", {**base, "id": PISA_EVENT.hex, "summary": "Cena alle 21"}, tz)

    async def resync(pool: Any) -> None:
        async with pool.connection() as conn:
            await _upsert_event(conn, again)

    _pool_run(db, resync)
    assert db.execute(
        "SELECT title, made_by_pisa FROM calendar_events WHERE id = %s", (PISA_EVENT.hex,)
    ).fetchone() == ("Cena alle 21", True)


def test_js_event_abroad_adds_no_residency_evidence_his_does(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    clock.now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    db.execute("INSERT INTO calendars (id, name) VALUES ('c', 'P') ON CONFLICT DO NOTHING")
    for eid, day, mine in (("j1", 2, True), ("o1", 3, False)):
        db.execute(
            "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, location, "
            "made_by_pisa) VALUES ('c', %s, 'Cena', %s, %s, false, 'confirmed', 'Times Square, New York', %s)",
            (eid, datetime(2026, 1, day, 12, 0, tzinfo=ny), datetime(2026, 1, day, 15, 0, tzinfo=ny), mine),
        )
    report = client.portal.call(client.app.state.runtime.residency.report, 2026)
    by_day = {e["day"]: (e["country"], e["source"]) for e in report["evidence"]}
    assert by_day.get("2026-01-03") == ("US", "calendar"), report["evidence"]  # their own event there
    assert "2026-01-02" not in by_day  # Pisa's: no evidence of where they were


def test_arriving_late_to_js_event_writes_no_lateness_evidence(db) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_event
    from tests.conftest import pair
    from tests.test_learning import at, learning, stay
    from tests.test_learning_fixes import MONDAY, place

    device = pair(db)[0]
    trattoria = place(db, "Trattoria Milanese", "restaurant", (45.47, 9.20))
    for i in range(6):
        day = MONDAY + timedelta(days=i)
        event = add_event(db, "Cena", at(day, 10), minutes=60)
        db.execute("UPDATE calendar_events SET place_id = %s, accepted = true, made_by_pisa = %s WHERE id = %s",
                   (trattoria, i % 2 == 0, event))  # fmt: skip
        stay(db, device, at(day, 10, 12), at(day, 11), (45.47, 9.20), trattoria)
    learning(db, at(date(2026, 10, 25), 3, 30), lambda s: s.nightly())
    rows = db.execute("SELECT summary FROM profile_evidence WHERE signal = 'S2'").fetchall()
    assert len(rows) == 3  # their three dinners only: Pisa's three are no habit of theirs


def test_js_event_titles_teach_no_lexicon_word_or_place(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.lexicon import LexiconService
    from tests.assistant_env import add_event
    from tests.test_learning import place

    for title, where, mine in (("Cena da Ottavio Brambilla", "Trattoria Zaffiro", True),
                               ("Pranzo con Ferdinando Zucchi", "Osteria Smeraldo", False)):  # fmt: skip
        pid = place(db, where, (45.47, 9.20), "Via Immaginaria 1, 20121 Milano, Italy")
        event = add_event(db, title, NOW + timedelta(hours=3))
        db.execute(
            "UPDATE calendar_events SET place_id = %s, made_by_pisa = %s WHERE id = %s", (pid, mine, event)
        )

    async def names(pool: Any) -> set[str]:
        _, entries, _ = await LexiconService(pool, owner=owner_at(), clock=Clock(NOW)).current()
        return {e.name for e in entries}

    got = _pool_run(db, names)
    assert {"Pranzo con Ferdinando Zucchi", "Osteria Smeraldo"} <= got
    assert not {"Cena da Ottavio Brambilla", "Trattoria Zaffiro"} & got


def test_the_dead_setting_is_gone_and_js_events_on_his_yes_still_count_as_his_day(db) -> None:  # type: ignore[no-untyped-def]
    """The ruling: streaks (and the meeting hold, the week poster) describe their day: an event Pisa made
    on their yes counts there; only the learning and evidence readers skip `made_by_pisa`. The retired
    plan_calendar_id setting is read nowhere: a stale one filters nothing."""
    from pisa.services import streaks
    from tests.assistant_env import add_event

    for mine in (True, False):
        event = add_event(db, "Palestra", NOW - timedelta(days=1 + int(mine)), calendar="jplan")
        db.execute("UPDATE calendar_events SET made_by_pisa = %s WHERE id = %s", (mine, event))
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('plan_calendar_id', '\"jplan\"', %s)",
        (NOW,),
    )

    async def go(pool: Any) -> Any:
        async with pool.connection() as conn:
            return await streaks.sessions(conn, NOW.date(), "Europe/Rome", NOW)

    got = _pool_run(db, go)
    assert sorted(d for _, d in got) == [(NOW - timedelta(days=2)).date(), (NOW - timedelta(days=1)).date()]
    import pisa.services.actions as a
    import pisa.services.notifications as n
    import pisa.services.rituals as r

    assert not any(
        hasattr(m, "PLAN_CALENDAR_SETTING") for m in (a, n, r, streaks)
    )  # read nowhere, written nowhere


def test_js_own_event_made_after_outside_text_stays_foreign(db) -> None:  # type: ignore[no-untyped-def]
    """the tell gate keeps its meaning: `made_by_pisa` says who made it, never that it is clean."""
    from pisa.services import foreign

    db.execute("INSERT INTO calendars (id, name, access_role) VALUES ('primary', 'P', 'owner')")
    for eid, outside in (("a" * 32, True), ("b" * 32, False)):
        db.execute(
            "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, made_by_pisa) "
            "VALUES ('primary', %s, 'Cena', %s, %s, false, 'confirmed', true)",
            (eid, NOW, NOW + timedelta(hours=1)),
        )
        db.execute(
            "INSERT INTO approvals (id, kind, status, summary, details, payload, created_at, expires_at, lang) "
            "VALUES (%s, 'calendar_create', 'done', 's', '[]', %s, %s, %s, 'it-IT')",
            (uuid.UUID(eid), Jsonb({"body": {"id": eid}, "after_outside": outside}), NOW, NOW),
        )
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
        "VALUES ('primary', 'his', 'Pranzo', %s, %s, false, 'confirmed')",
        (NOW, NOW + timedelta(hours=1)),
    )
    native(db, "his")  # Made in their own Google Calendar
    # The mark is on the row (set at write time and by the sync), never a join on the approvals
    db.execute("UPDATE calendar_events SET outside = true WHERE id = %s", ("a" * 32,))
    foreign_ids = {
        r[0]
        for r in db.execute(f"SELECT e.id FROM {foreign.EVENTS} WHERE {foreign.FOREIGN_EVENT}").fetchall()
    }
    assert foreign_ids == {"a" * 32}


# --- The review -----------------------------------------------------------------------------


def test_js_event_stays_js_after_js_move_and_the_daily_full_resync(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    """Create → sync → Pisa moves it (the move's approval rewrites `pisa_action`) → incremental → full resync: Pisa's."""
    from datetime import UTC, datetime

    from pisa.domain.calendar import CalendarInfo, EventChanges
    from pisa.services.google_sync import sync_calendar
    from tests.gateway_env import run as run_async
    from tests.google_fake import FakeGoogle
    from tests.test_google import google_env

    move = uuid.uuid4()
    db.execute(
        "INSERT INTO approvals (id, kind, status, summary, details, payload, created_at, expires_at, lang) "
        "VALUES (%s, 'calendar_create', 'done', 's', '[]', %s, %s, %s, 'it-IT')",
        (PISA_EVENT, Jsonb({"body": {"id": PISA_EVENT.hex}}), NOW, NOW),
    )

    def item(action: uuid.UUID, start: str) -> dict[str, Any]:
        return {"id": PISA_EVENT.hex, "summary": "Cena", "updated": "2026-10-20T10:00:00Z",
                "start": {"dateTime": f"2026-10-21T{start}:00+02:00"}, "end": {"dateTime": "2026-10-21T23:00:00+02:00"},
                "extendedProperties": {"private": {"pisa": "1", "pisa_action": str(action)}}}  # fmt: skip

    class Source:
        def __init__(self) -> None:
            self.items = [item(PISA_EVENT, "20:00")]

        async def calendars(self) -> list[CalendarInfo]:
            return [CalendarInfo("primary", "Personale", "owner")]

        async def events(
            self, calendar_id: str, *, sync_token: str | None, time_min: Any, time_max: Any
        ) -> EventChanges:
            from zoneinfo import ZoneInfo

            from pisa.connectors.google.calendar import parse_event

            parsed = tuple(parse_event(calendar_id, i, ZoneInfo("Europe/Rome")) for i in self.items)
            return EventChanges(parsed, "token")

    source = Source()

    async def sync(now: datetime) -> Any:
        async with google_env(db, secrets_key, FakeGoogle(), clock) as (service, pool):
            return await sync_calendar(pool, service, timezone="Europe/Rome", source=source, now=now)

    def mine() -> Any:
        return db.execute(
            "SELECT made_by_pisa FROM calendar_events WHERE id = %s", (PISA_EVENT.hex,)
        ).fetchone()

    day = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run_async(lambda: sync(day))
    assert mine() == (True,)
    source.items = [item(move, "21:00")]  # Pisa moved it: Google's tag now names the move's approval
    run_async(lambda: sync(day + timedelta(hours=1)))  # incremental
    assert mine() == (True,)
    run_async(lambda: sync(datetime(2026, 10, 20, 22, 30, tzinfo=UTC)))  # a new day: the full resync
    assert mine() == (True,)
    # and a create whose first sync was missed, already moved by Pisa when the sync comes: Pisa's too
    db.execute("DELETE FROM calendar_events")
    run_async(lambda: sync(datetime(2026, 10, 21, 22, 30, tzinfo=UTC)))
    assert mine() == (True,)


def test_the_model_reads_the_marked_form_this_turn(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    t = turn(db, secrets_key, "Ehm, chiama nino ferry", confirm="same")
    step = understand.Result(
        meant="chiama Nino Ferri",
        changed=True,
        corrections=[understand.Correction("nino ferry", "Nino Ferri", 1.0, "model")],
    )
    assert t.prompt == understand.marked(step, "Ehm, chiama nino ferry", "it-IT")
    plain = turn(db, secrets_key, "Che ore sono?")
    assert plain.prompt == "Che ore sono?"  # unchanged words: their words alone


def test_the_vault_redaction_covers_js_meant_and_the_corrections(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import vault

    conversation = uuid.uuid4()
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    if device is None:
        from tests.conftest import pair

        pair(db)
        device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    db.execute(
        "INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation, device[0], NOW, NOW),  # type: ignore[index]
    )
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at, meant, corrections) VALUES "
        "(%s, 'user', 'ehm la carta è CA12345AB', 'it-IT', %s, 'la carta è CA12345AB', %s), "
        "(%s, 'user', 'chiama nino ferry', 'it-IT', %s, 'chiama Nino Ferri', %s)",
        (conversation, NOW, Jsonb([{"from": "CA12345", "to": "CA12345AB", "via": "rules"}]),
         conversation, NOW, Jsonb([{"from": "nino ferry", "to": "Nino Ferri", "via": "model"}])),
    )  # fmt: skip
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, %s)",
        (vault.REDACT_KEY, Jsonb(NOW.isoformat()), NOW),
    )
    assert _pool_run(db, lambda pool: vault.redact_turns(pool, NOW + timedelta(minutes=1))) == 1
    rows = db.execute("SELECT content, meant, corrections FROM messages ORDER BY id").fetchall()
    assert "CA12345AB" not in rows[0][0] and "CA12345AB" not in rows[0][1] and rows[0][2] is None
    assert rows[1] == (
        "chiama nino ferry",
        "chiama Nino Ferri",
        [{"from": "nino ferry", "to": "Nino Ferri", "via": "model"}],
    )
