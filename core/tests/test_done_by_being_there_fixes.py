"""The must-fix rows, each with a test that fails
without its fix."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
from pydantic import SecretStr

from pisa.db.pool import open_pool
from pisa.infra.keys import KeyStore
from pisa.ports.push import OutgoingNotification
from pisa.services.notifications import Dispatcher, enqueue
from pisa.services.push import PushService, set_apns_key
from tests.conftest import Clock, dsn, pair
from tests.live_app import live, message_of
from tests.owner import owner_at
from tests.test_gateway_presence import wait_for
from tests.test_push import KEY_ID, TEAM_ID, TOPIC, Apple, p8, with_token
from tests.test_understanding_service import week  # noqa: F401  (a fixture: the user's week)

CLIENT_NOW = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)  # the `client` fixture's clock
DONE: dict[str, object] = {
    "task_id": "00000000-0000-4000-8000-000000000541",
    "title": "BOBBY'S Bitterballen",
    "place": "BOBBY'S Bitterballen",
    "undo_until": "2026-10-21T13:00:00+02:00",
}


def store_done(db: psycopg.Connection, now: datetime) -> uuid.UUID:
    async def go() -> uuid.UUID:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            nid, _ = await enqueue(
                conn, kind="done", text="Fatto: BOBBY'S Bitterballen ✓", lang="it-IT", now=now,
                timezone="Europe/Rome", done=DONE,
            )  # fmt: skip
            return nid

    return asyncio.run(go())


# --- H-1: the done notify reaches a connected app; one failing channel never stops the others ---------------


def test_the_done_notify_reaches_the_live_socket(client, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        store_done(db, CLIENT_NOW)
        # Delivered on the socket (never left pending for a retry): the notify came before the pong.
        sent = "SELECT delivered_via FROM notifications WHERE delivered_via IS NOT NULL"
        wait_for(lambda: db.execute(sent).fetchall())
        (message,) = app.ping()
        app.check()
    assert db.execute(sent).fetchall() == [("ws",)]
    message_of(json.dumps(message))
    assert message["type"] == "notify" and message["kind"] == "done"
    assert {k: message[k] for k in ("task_id", "title", "place")} == {
        k: DONE[k] for k in ("task_id", "title", "place")
    }
    assert datetime.fromisoformat(message["undo_until"]) == datetime.fromisoformat(str(DONE["undo_until"]))


class Broken:
    """A socket channel that fails for one device."""

    def __init__(self, broken: str) -> None:
        self.broken = broken

    async def notify(self, device_id: object, notification: OutgoingNotification) -> bool:
        if str(device_id) == self.broken:
            raise ValueError("the socket refused it")
        return False

    async def approval(self, device_id: object, approval: Any) -> bool:
        return False

    async def action_done(self, device_id: object, done: Any) -> bool:
        return False


def test_a_failing_socket_never_stops_the_push_to_the_others(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    first, _ = pair(db, "iPad")
    second, _ = pair(db, "iPhone")
    with_token(db, second)
    apple = Apple()
    keys = KeyStore(SecretStr(secrets_key))
    nid = store_done(db, CLIENT_NOW)

    async def go() -> str | None:
        pool = await open_pool(dsn(db), max_size=3)
        await pool.wait()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(apple)) as http:
                push = PushService(pool, keys, http, team_id=TEAM_ID, topic=TOPIC)
                async with pool.connection() as conn:
                    await set_apns_key(conn, keys, KEY_ID, p8()[0])
                return await Dispatcher(
                    pool, Broken(first), push, clock=Clock(CLIENT_NOW), owner=owner_at()
                ).deliver(nid)
        finally:
            await pool.close()

    assert asyncio.run(go()) == "apns"
    assert len(apple.requests) == 1
    body = json.loads(apple.requests[0].content)
    assert body["kind"] == "done" and body["undo_until"] == DONE["undo_until"]
    assert db.execute("SELECT delivered_via FROM notifications").fetchone() == ("apns",)


# --- done by being there ------------------------------------------------------------------------------------

import pytest  # noqa: E402

from pisa.domain.stays import Fix  # noqa: E402
from tests.assistant_env import add_event, add_task, rome, set_settings  # noqa: E402
from tests.test_done import BOBBYS, HOTEL, TRATTORIA, done_notes, run, tick  # noqa: E402
from tests.test_follow import fixes, track  # noqa: E402
from tests.test_planner import place, with_place  # noqa: E402


@pytest.fixture
def owner(db: psycopg.Connection) -> str:
    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    return pair(db)[0]


def test_an_event_in_two_calendars_is_closed_once_and_undone_whole(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    trattoria = place(db, "Trattoria", TRATTORIA)
    for calendar in ("primary", "work"):  # a shared invite: the same event in two calendars
        add_event(db, "Cena", rome(2026, 10, 21, 19), minutes=120, calendar=calendar, event_id="ev-cena")
    db.execute("UPDATE calendar_events SET place_id = %s", (trattoria,))
    track(db, owner, fixes(TRATTORIA, rome(2026, 10, 21, 19), rome(2026, 10, 21, 19, 20)))
    assert tick(db, secrets_key, rome(2026, 10, 21, 19, 20)) == 1
    assert tick(db, secrets_key, rome(2026, 10, 21, 19, 21)) == 0
    assert len(done_notes(db)) == 1
    assert db.execute("SELECT count(*) FROM calendar_events WHERE attended_at IS NOT NULL").fetchone() == (2,)
    run(db, secrets_key, rome(2026, 10, 21, 19, 30), lambda done, f, env: done.undo_event("ev-cena"))
    assert db.execute("SELECT count(*) FROM calendar_events WHERE attended_at IS NOT NULL").fetchone() == (0,)


def test_one_stay_closes_one_occurrence_of_an_overdue_repeating_task(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    gym = place(db, "Palestra", TRATTORIA)
    task = add_task(db, "Allenamento", rome(2026, 10, 19).date())  # overdue: due two days ago
    with_place(db, "tasks", task, gym)
    db.execute("""UPDATE tasks SET repeat = '{"every": "day", "interval": 1}' WHERE id = %s""", (task,))
    track(db, owner, fixes(TRATTORIA, rome(2026, 10, 21, 9), rome(2026, 10, 21, 9, 30)))
    for minute in (30, 31, 32):
        tick(db, secrets_key, rome(2026, 10, 21, 9, minute))
        # The next occurrence gets its place again (the place job resolves it from its place text).
        db.execute("UPDATE tasks SET place_id = %s WHERE status = 'open'", (gym,))
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'done'").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'open'").fetchone() == (1,)  # the next one
    assert len(done_notes(db)) == 1


def test_a_stay_at_another_known_place_nearby_never_closes_it(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    shop = place(db, "Ferramenta", TRATTORIA)
    cafe = (TRATTORIA[0] + 0.0009, TRATTORIA[1])  # ~100 m north
    place(db, "Caffè accanto", cafe)
    task = add_task(db, "Comprare le viti")
    with_place(db, "tasks", task, shop)
    # A coarse location (120 m): the shop is within its reach, but the stay is linked to the café.
    coarse = [
        Fix(cafe[0], cafe[1], 120, rome(2026, 10, 21, 10) + timedelta(minutes=m)) for m in range(0, 31, 2)
    ]
    track(db, owner, coarse)
    linked = db.execute("SELECT p.name FROM stays s JOIN places p ON p.id = s.place_id").fetchall()
    assert linked == [("Caffè accanto",)]
    assert tick(db, secrets_key, rome(2026, 10, 21, 10, 30)) == 0
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)


def test_attended_survives_the_daily_full_resync(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.google_sync import sync_calendar
    from tests.gateway_env import run as run_async
    from tests.google_fake import FakeGoogle
    from tests.test_google import T0, google_env

    fake = FakeGoogle()

    async def sync(now: datetime) -> Any:
        async with google_env(db, secrets_key, fake, clock) as (google, pool):
            return await sync_calendar(pool, google, timezone="Europe/Rome", now=now)

    run_async(lambda: sync(T0))
    db.execute("UPDATE calendar_events SET attended_at = %s WHERE id = 'ev-lunch'", (T0,))
    run_async(lambda: sync(datetime(2026, 10, 20, 22, 30, tzinfo=UTC)))  # a new day: a full resync
    assert db.execute("SELECT attended_at FROM calendar_events WHERE id = 'ev-lunch'").fetchone() == (T0,)


def test_a_voice_close_names_no_place_replans_and_an_undo_holds_for_the_day(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    bobbys = place(db, "BOBBY'S Bitterballen", BOBBYS)
    task = add_task(db, "BOBBY'S Bitterballen")
    with_place(db, "tasks", task, bobbys)
    replanned: list[str] = []

    async def say_it(done: Any, f: Any, env: Any) -> Any:
        async def current(day: Any = None) -> dict[str, Any]:
            return {"items": [{"kind": "task", "task_id": task}]}

        async def plan(reason: str = "") -> dict[str, Any]:
            replanned.append(reason)
            return {}

        done._planner.current, done._planner.plan = current, plan
        return await done.mark("BOBBY'S Bitterballen")

    out = run(db, secrets_key, rome(2026, 10, 24, 12, 40), say_it)
    assert out["done"][0]["task_id"] == task and replanned  # L-5: the plan follows a voice close too
    ((_, payload),) = done_notes(db)
    assert payload["place"] is None  # L-4: the user's words name no place
    summary = db.execute("SELECT summary FROM notifications WHERE kind = 'done'").fetchone()
    assert summary == (
        "Fatto: BOBBY'S Bitterballen ✓ (annulla dall'app)",
    )  # Q3: held in quiet, the way to undo
    run(db, secrets_key, rome(2026, 10, 24, 12, 41), lambda done, f, env: done.undo_task(task))
    # The user reopened it: the stay that is still going on doesn't close it again today.
    track(db, owner, fixes(BOBBYS, rome(2026, 10, 24, 12, 30), rome(2026, 10, 24, 12, 50)))
    assert tick(db, secrets_key, rome(2026, 10, 24, 12, 50)) == 0
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)


def test_a_task_already_closed_isnt_closed_again(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    bobbys = place(db, "BOBBY'S Bitterballen", BOBBYS)
    task = add_task(db, "BOBBY'S Bitterballen")
    with_place(db, "tasks", task, bobbys)

    async def both(done: Any, f: Any, env: Any) -> list[int]:
        target = {"task_id": task}
        args = ("BOBBY'S Bitterballen", bobbys, "BOBBY'S Bitterballen", None)
        now = rome(2026, 10, 24, 12, 45)
        first = await done._close(target, *args, "voice", None, now)
        second = await done._close(target, *args, "stay", now, now)
        return [first, second]

    assert run(db, secrets_key, rome(2026, 10, 24, 12, 45), both) == [1, 0]
    assert len(done_notes(db)) == 1


def test_u4_a_soft_event_pisa_moved_is_attended_at_its_new_time(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    trattoria = place(db, "Trattoria", TRATTORIA)
    coffee = add_event(db, "Caffè con Anna", rome(2026, 10, 21, 10), minutes=30)
    with_place(db, "calendar_events", coffee, trattoria)
    # Pisa moved it to 14:00 in the plan; the user was there 14:00-14:20, far from its calendar time ± 1 h.
    track(db, owner, fixes(HOTEL, rome(2026, 10, 21, 9), rome(2026, 10, 21, 13, 50)))
    track(db, owner, fixes(TRATTORIA, rome(2026, 10, 21, 14), rome(2026, 10, 21, 14, 20)))

    async def go(done: Any, f: Any, env: Any) -> int:
        async def current(day: Any = None) -> dict[str, Any]:
            return {"items": [{
                "kind": "event", "event_id": coffee, "moved_from": rome(2026, 10, 21, 10).isoformat(),
                "start": rome(2026, 10, 21, 14).isoformat(), "end": rome(2026, 10, 21, 14, 30).isoformat(),
            }]}  # fmt: skip

        done._planner.current = current
        return await done.tick()

    assert run(db, secrets_key, rome(2026, 10, 21, 14, 20), go) == 1


# --- M-4: BOBBY'S by voice, task and lunch ------------------------------------------------------------------


def test_a_task_and_an_event_at_the_same_place_close_together_in_one_line(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_research_eval import research

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    bobbys = place(db, "BOBBY'S Bitterballen", BOBBYS)
    task = add_task(db, "BOBBY'S Bitterballen")
    with_place(db, "tasks", task, bobbys)
    lunch = add_event(db, "Pranzo", rome(2026, 10, 20, 13), minutes=60)
    with_place(db, "calendar_events", lunch, bobbys)
    said: list[Any] = [("mark_done", {"what": "BOBBY'S"})]
    t = research(db, secrets_key, "ci sono stato, mi è piaciuto", said, "Fatto.", with_data=True)
    assert "Done: BOBBY'S Bitterballen and Pranzo." in t.result("mark_done")
    assert db.execute("SELECT status FROM tasks").fetchone() == ("done",)
    assert db.execute("SELECT attended_at IS NOT NULL FROM calendar_events").fetchone() == (True,)
    assert [text for text, _ in done_notes(db)] == ["Fatto: BOBBY'S Bitterballen ✓"]


def test_otherwise_the_answer_picks_by_number_or_title(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_research_eval import research

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)  # one test transaction: now() would tie them all
    add_task(db, "Comprare il pane", created=at)
    add_task(db, "Comprare il latte", created=at + timedelta(minutes=1))
    add_task(db, "Comprare il pane integrale", created=at + timedelta(minutes=2))
    asked = research(db, secrets_key, "ho comprato", [("mark_done", {"what": "comprare"})], with_data=True)
    assert "1. Comprare il pane; 2. Comprare il latte; 3. Comprare il pane integrale" in asked.result(
        "mark_done"
    )
    picked: list[Any] = [("mark_done", {"what": "comprare", "pick": 2})]
    t = research(db, secrets_key, "il secondo", picked, with_data=True, device=asked.device)
    assert "Done: Comprare il latte." in t.result("mark_done")
    by_title: list[Any] = [("mark_done", {"what": "Comprare il pane"})]
    t = research(db, secrets_key, "il pane", by_title, with_data=True, device=asked.device)
    assert "Done: Comprare il pane." in t.result("mark_done")


def test_the_list_asked_and_the_pick_keep_one_order(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The rows' storage order is not the tasks' order: with no ORDER BY, "il secondo" could name another task
    than the one Pisa listed second (CI flake). Stored newest first here, listed oldest first."""
    from tests.test_research_eval import research

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    add_task(db, "Comprare il pane integrale", created=at + timedelta(minutes=2))
    add_task(db, "Comprare il latte", created=at + timedelta(minutes=1))
    add_task(db, "Comprare il pane", created=at)
    asked = research(db, secrets_key, "ho comprato", [("mark_done", {"what": "comprare"})], with_data=True)
    assert "1. Comprare il pane; 2. Comprare il latte; 3. Comprare il pane integrale" in asked.result(
        "mark_done"
    )


# --- M-5: a person writing is never a low notice, whatever the email's text makes the model think ----------


# --- Priority, dismiss and reschedule wait for the user's yes after outside text ----------------


def test_u1_task_changes_after_outside_text_wait_and_are_no_label(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_rain_links_web_yes import READ_WEB, l3_turn

    task = add_task(db, "Pagare la bolletta")
    lower: list[Any] = [READ_WEB, ("set_task_priority", {"task_ref": "bolletta", "priority": 3})]
    t = l3_turn(db, secrets_key, "cerca il traghetto", lower)
    assert t.result("set_task_priority").startswith("Not done yet: this turn read text from outside")
    assert t.reply.endswith("Posso mettere «Pagare la bolletta» in priorità 3?")
    assert db.execute("SELECT priority FROM tasks").fetchone() == (2,)
    yes = l3_turn(db, secrets_key, "sì", [("confirm_remember", {})], device=t.device)
    assert "Now: «Pagare la bolletta», open, priority low." in yes.result("confirm_remember")
    assert db.execute("SELECT priority, priority_source FROM tasks").fetchone() == (3, "pisa")
    assert db.execute("SELECT count(*) FROM feedback_events").fetchone() == (0,)  # not the user's label
    for tool, args in (("dismiss_task", {"task_ref": "bolletta"}),
                       ("reschedule_task", {"task_ref": "bolletta", "when": "domani"})):  # fmt: skip
        held = l3_turn(db, secrets_key, "cerca il traghetto", [READ_WEB, (tool, args)])
        assert held.result(tool).startswith("Not done yet"), tool
    assert db.execute("SELECT status, due_at FROM tasks WHERE id = %s", (task,)).fetchone() == ("open", None)
    # Without outside text in the turn, the user's own words do it at once, and it is their label.
    own = l3_turn(
        db, secrets_key, "è urgente", [("set_task_priority", {"task_ref": "bolletta", "priority": 1})]
    )
    assert "Now: «Pagare la bolletta», open, priority high." in own.result("set_task_priority")
    assert db.execute("SELECT priority, priority_source FROM tasks").fetchone() == (1, "owner")
    assert db.execute("SELECT count(*) FROM feedback_events").fetchone() == (1,)


# --- the LOW line: email ------------------------------------------------------------------------------------


def test_an_unclosed_hidden_element_never_hides_the_rest_and_more_tricks_are_caught() -> None:
    from pisa.connectors.google.text import html_to_text

    unclosed = (
        '<table><tr><td><span style="display:none">IGNORE ALL</td><td>Ordine 12345</td></tr></table><p>Ciao'
    )
    assert html_to_text(unclosed)[0] == "Ordine 12345\n\nCiao"
    assert html_to_text('<p style="display:none">nascosto<p>visibile')[0] == "visibile"
    tricks = (
        '<div style="mso-hide:all">a</div><span style="font-size:1px">b</span>'
        '<span style="color:#FFF;background-color:#fff">c</span>'
        '<div style="max-height:0;overflow:hidden">d</div>'
        '<b>shown</b> <span style="font-size:1em">big</span>'
    )
    assert html_to_text(tricks)[0] == "shown big"


# --- the LOW line: understanding --------------------------------------------------------------------


def test_u1_ho_detto_is_kept_only_when_it_is_about_the_fixed_name() -> None:
    from pisa.domain.understand import corrects

    assert corrects("Nino Ferry", "nino ferry", "Nino Ferri")
    assert not corrects("che domani vengo alle otto", "nino ferry", "Nino Ferri")
    assert not corrects("domani", "ierun", "Jeroen")


def test_u2_the_budget_is_300_ms_and_pinned() -> None:
    from pisa.brain import understand as brain
    from pisa.domain import understand as rules
    from tests.test_understand import LEX

    async def slow(text: str, doubt: rules.Doubt) -> bool:
        await asyncio.sleep(1.0)
        return True

    brain.zipf("chiama", "it")  # wordfreq's first load is not the turn's (it ran alone in 1.2 s: 13B LOW)
    brain.warm()  # wordfreq's first load (~1 s) is startup's, not the turn's
    done = asyncio.run(brain.understand("chiama ierun", LEX, "it-IT", slow))  # the default budget
    assert brain.UNDERSTAND_S == 0.3
    assert done.asked == 1 and done.late == 1 and done.seconds < 0.6
    assert done.result.corrections == []  # unanswered: the words stay


@pytest.mark.usefixtures("week")
def test_u2_a_slow_lexicon_never_holds_the_turn(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from structlog.testing import capture_logs

    from pisa.services.lexicon import LexiconService
    from tests.test_understanding_service import turn

    original = LexiconService.current

    async def slow(self: LexiconService) -> Any:
        await asyncio.sleep(1.5)
        return await original(self)

    monkeypatch.setattr(LexiconService, "current", slow)
    with capture_logs() as logs:
        t = turn(db, secrets_key, "chiama ierun", confirm="same")
    assert t.understood == []  # no lexicon yet: the words go on as said
    assert any(e["event"] == "understand_lexicon_slow" and e["stale"] is False for e in logs)


def test_u5_a_model_too_slow_for_the_budget_stops_being_asked() -> None:
    from collections import deque

    from pisa.brain.understand import Understood
    from pisa.domain import understand as rules
    from pisa.services import conversation as service

    convo = service.ConversationService.__new__(service.ConversationService)
    convo._late, convo._slow_until = deque(maxlen=service.SLOW_WINDOW), None
    convo._clock = lambda: datetime(2026, 10, 26, 9, tzinfo=UTC)
    nothing = rules.Result("x")
    for _ in range(service.SLOW_LATE):
        convo._note_late(Understood(nothing, 1, 0.3, late=1), "t")
    assert convo._slow_until == datetime(2026, 10, 26, 9, tzinfo=UTC) + service.SLOW_PAUSE


@pytest.mark.usefixtures("week")
def test_u1_an_unrelated_ho_detto_is_no_correction(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain import understand
    from tests.test_understanding_service import turn

    monkeypatch.setattr(understand, "UNDERSTAND_S", 5.0)  # the fake answers at once; give it time
    first = turn(db, secrets_key, "chiama nino ferry", confirm="same")
    assert first.understood and first.understood[0]["corrections"]
    turn(db, secrets_key, "No, ho detto che domani vengo alle otto e mezza", device=first.device)
    assert db.execute("SELECT count(*) FROM understand_corrections").fetchone() == (0,)
