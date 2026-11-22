"""Done by being there: 10 minutes at a task's or today's event's place close it, planned or not
(a lunch stay at BOBBY'S Bitterballen, the task unplanned), with one line and Undo for 24
hours; by voice (`mark_done`, "ci sono stato, mi è piaciuto"), gated like the other changes; a sensitive place
closes the item and learns nothing; the `done` notification in the push."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from pisa.domain.follow import Item, Seen, status
from pisa.services.done import DoneService
from pisa.services.follow import FollowService
from pisa.services.notifications import _category, _push_data
from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, add_task, rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.places_env import PlaceEnv, place_env
from tests.places_fake import FakePlaces
from tests.test_follow import fixes, track
from tests.test_planner import place, with_place

TZ = "Europe/Rome"
BOBBYS = (52.359, 4.862)
HOTEL = (52.330, 4.877)  # the hotel, an invented point ~3.4 km away
TRATTORIA = (52.357, 4.868)


def run(
    db: psycopg.Connection,
    secrets_key: str,
    now: datetime,
    action: Callable[[DoneService, FollowService, PlaceEnv], Awaitable[Any]],
) -> Any:
    async def go() -> Any:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock)
            options = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(TZ), clock=env.clock)
            follow = FollowService(
                env.pool, planner, options, tasks, env.places, owner=owner_at(TZ), clock=env.clock
            )
            done = DoneService(env.pool, tasks, planner, owner=owner_at(TZ), clock=env.clock)
            return await action(done, follow, env)

    return asyncio.run(go())


def tick(db: psycopg.Connection, secrets_key: str, now: datetime) -> int:
    return run(db, secrets_key, now, lambda done, follow, env: done.tick())


def done_notes(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT text, done FROM notifications WHERE kind = 'done' ORDER BY created_at"
    ).fetchall()


@pytest.fixture
def owner(db: psycopg.Connection) -> str:
    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    device, _ = pair(db)
    return device


def test_the_snack_bar_stay_closes_the_unplanned_task(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    """A trip where the task was unplanned: lunch at BOBBY'S Bitterballen; the task was in "not fit for
    today", so no plan item and no visit detection saw it. Now 10 minutes there close it, planned or not."""
    bobbys = place(db, "BOBBY'S Bitterballen", BOBBYS)
    task = add_task(db, "BOBBY'S Bitterballen")
    with_place(db, "tasks", task, bobbys)
    track(db, owner, fixes(HOTEL, rome(2026, 10, 24, 11, 30), rome(2026, 10, 24, 12, 10)))
    # 8 minutes at the counter: not yet.
    track(db, owner, fixes(BOBBYS, rome(2026, 10, 24, 12, 30), rome(2026, 10, 24, 12, 38)))
    assert tick(db, secrets_key, rome(2026, 10, 24, 12, 39)) == 0
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)
    # Still there at 12:42: 12 minutes.
    track(db, owner, fixes(BOBBYS, rome(2026, 10, 24, 12, 40), rome(2026, 10, 24, 12, 42)))
    assert tick(db, secrets_key, rome(2026, 10, 24, 12, 42)) == 1
    assert db.execute("SELECT count(*) FROM plans").fetchone() == (0,)  # never planned
    assert db.execute("SELECT status FROM tasks").fetchone() == ("done",)
    ((text, payload),) = done_notes(db)
    assert text == "Fatto: BOBBY'S Bitterballen ✓"
    assert payload == {
        "task_id": task,
        "title": "BOBBY'S Bitterballen",
        "place": "BOBBY'S Bitterballen",
        "undo_until": rome(2026, 10, 25, 12, 42).isoformat(),
    }
    assert db.execute("SELECT source, learn, place_id::text FROM done_by_presence").fetchone() == (
        "stay", True, bobbys,
    )  # fmt: skip
    # Once: the next minute closes nothing more and says nothing more.
    assert tick(db, secrets_key, rome(2026, 10, 24, 12, 43)) == 0
    assert len(done_notes(db)) == 1


def test_no_confirm_check_in_while_the_owner_is_still_there() -> None:
    """Pisa never asks "did you do it?" while the 10 minutes can still close it by themselves."""
    errand = Item("task", rome(2026, 10, 21, 16), rome(2026, 10, 21, 16, 30), "p", TRATTORIA)
    staying = [Seen(rome(2026, 10, 21, 11), None, *TRATTORIA, 15, "p")]
    at_noon = rome(2026, 10, 21, 11, 6)
    day = rome(2026, 10, 21)
    assert status(errand, staying, now=at_noon, here=TRATTORIA, trip_min=0, day_start=day) == "planned"
    left = [Seen(rome(2026, 10, 21, 11), rome(2026, 10, 21, 11, 6), *TRATTORIA, 15, "p")]
    assert status(errand, left, now=at_noon, here=HOTEL, trip_min=9, day_start=day) == "visited"


def test_an_event_is_attended_within_its_time_plus_or_minus_an_hour(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    trattoria = place(db, "Trattoria", TRATTORIA)
    dinner = add_event(db, "Cena con Marco", rome(2026, 10, 21, 19), minutes=120)
    with_place(db, "calendar_events", dinner, trattoria)
    # There 17:30-17:55: before the hour ahead of it.
    track(db, owner, fixes(TRATTORIA, rome(2026, 10, 21, 17, 30), rome(2026, 10, 21, 17, 55)))
    track(db, owner, fixes(HOTEL, rome(2026, 10, 21, 17, 59), rome(2026, 10, 21, 18, 5)))
    assert tick(db, secrets_key, rome(2026, 10, 21, 18, 5)) == 0
    # Back 18:10-18:22: inside the window.
    track(db, owner, fixes(TRATTORIA, rome(2026, 10, 21, 18, 10), rome(2026, 10, 21, 18, 22)))
    assert tick(db, secrets_key, rome(2026, 10, 21, 18, 22)) == 1
    row = db.execute("SELECT attended_at FROM calendar_events WHERE id = %s", (dinner,)).fetchone()
    assert row is not None and row[0] == rome(2026, 10, 21, 18, 22)
    ((text, payload),) = done_notes(db)
    assert text == "Fatto: Cena con Marco ✓" and payload["event_id"] == dinner and "task_id" not in payload
    event = run(db, secrets_key, rome(2026, 10, 21, 18, 23), lambda done, f, env: f._planner.event(dinner))
    assert event["attended_at"] == rome(2026, 10, 21, 18, 22).isoformat()
    # Attended is done: the plan says so, and no check-in asks about it.
    plan = {
        "date": "2026-10-21",
        "items": [{
            "kind": "event", "event_id": dinner, "title": "Cena con Marco", "place_id": trattoria,
            "start": rome(2026, 10, 21, 19).isoformat(), "end": rome(2026, 10, 21, 21).isoformat(),
        }],
    }  # fmt: skip
    found = run(db, secrets_key, rome(2026, 10, 21, 18, 23), lambda done, f, env: f.statuses(plan))
    assert found == {dinner: "done"}


def test_a_task_due_on_a_later_day_waits_for_its_day(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    poste = place(db, "Poste Italiane", TRATTORIA)
    task = add_task(db, "Ritirare il pacco", rome(2026, 10, 22).date())
    with_place(db, "tasks", task, poste)
    track(db, owner, fixes(TRATTORIA, rome(2026, 10, 21, 10), rome(2026, 10, 21, 10, 20)))
    assert tick(db, secrets_key, rome(2026, 10, 21, 10, 20)) == 0
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)


def test_a_sensitive_place_closes_the_item_and_learns_nothing(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    hospital = place(db, "Ospedale Sant'Andrea", TRATTORIA)
    db.execute("UPDATE places SET category = 'hospital' WHERE id = %s", (hospital,))
    task = add_task(db, "Visita di controllo")
    with_place(db, "tasks", task, hospital)
    track(db, owner, fixes(TRATTORIA, rome(2026, 10, 21, 9), rome(2026, 10, 21, 9, 30)))
    assert tick(db, secrets_key, rome(2026, 10, 21, 9, 30)) == 1
    assert db.execute("SELECT status FROM tasks").fetchone() == ("done",)
    assert db.execute("SELECT learn, place_id FROM done_by_presence").fetchone() == (False, None)
    ((text, payload),) = done_notes(db)
    assert text == "Fatto: Visita di controllo ✓" and payload["place"] is None
    # The undo works, and isn't a label either.
    run(db, secrets_key, rome(2026, 10, 21, 10), lambda done, f, env: done.undo_task(task))
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)
    assert db.execute("SELECT count(*) FROM feedback_events").fetchone() == (0,)


def test_undo_within_24_hours_reopens_it_once_and_is_a_label(db, secrets_key, owner) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.errors import PisaError

    bobbys = place(db, "BOBBY'S Bitterballen", BOBBYS)
    task = add_task(db, "BOBBY'S Bitterballen")
    with_place(db, "tasks", task, bobbys)
    track(db, owner, fixes(BOBBYS, rome(2026, 10, 24, 12, 30), rome(2026, 10, 24, 12, 45)))
    assert tick(db, secrets_key, rome(2026, 10, 24, 12, 45)) == 1
    run(db, secrets_key, rome(2026, 10, 24, 13), lambda done, f, env: done.undo_task(task))
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)
    assert db.execute("SELECT kind, subject, from_value, to_value FROM feedback_events").fetchall() == [
        ("done_undone", f"task:{task}", "done", "open")
    ]
    # The same visit doesn't close it again today.
    track(db, owner, fixes(BOBBYS, rome(2026, 10, 24, 12, 46), rome(2026, 10, 24, 13, 10)))
    assert tick(db, secrets_key, rome(2026, 10, 24, 13, 10)) == 0
    with pytest.raises(PisaError) as nothing:
        run(db, secrets_key, rome(2026, 10, 24, 13, 11), lambda done, f, env: done.undo_task(task))
    assert nothing.value.code == "not_found"
    # Another day, another visit: closed again; its undo ends after 24 hours.
    track(db, owner, fixes(BOBBYS, rome(2026, 10, 25, 12, 30), rome(2026, 10, 25, 12, 45)))
    assert tick(db, secrets_key, rome(2026, 10, 25, 12, 45)) == 1
    with pytest.raises(PisaError) as late:
        run(db, secrets_key, rome(2026, 10, 26, 12, 46), lambda done, f, env: done.undo_task(task))
    assert late.value.code == "undo_expired" and late.value.http_status == 409


def test_the_undo_endpoints(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    now = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)  # the client's clock
    task = add_task(db, "BOBBY'S Bitterballen", status="done")
    event = add_event(db, "Cena", rome(2026, 10, 20, 12), minutes=60)
    db.execute("UPDATE calendar_events SET attended_at = %s", (now - timedelta(hours=1),))
    for target in ({"task_id": task}, {"calendar_id": "primary", "event_id": event}):
        db.execute(
            "INSERT INTO done_by_presence (id, task_id, calendar_id, event_id, day, learn, title, source, "
            "done_at, undo_until) VALUES (%s, %s, %s, %s, %s, true, 'x', 'stay', %s, %s)",
            (uuid.uuid4(), target.get("task_id"), target.get("calendar_id"), target.get("event_id"),
             now.date(), now - timedelta(hours=1), now + timedelta(hours=23)),
        )  # fmt: skip
    reopened = client.post(f"/v1/tasks/{task}/undo-done", headers=auth)
    assert reopened.status_code == 200 and reopened.json()["status"] == "open"
    assert client.post(f"/v1/tasks/{task}/undo-done", headers=auth).status_code == 404
    unattended = client.post(f"/v1/events/{event}/undo-attended", headers=auth)
    assert unattended.status_code == 200 and "attended_at" not in unattended.json()
    assert db.execute("SELECT attended_at FROM calendar_events").fetchone() == (None,)
    assert [k for (k,) in db.execute("SELECT kind FROM feedback_events ORDER BY kind").fetchall()] == [
        "attended_undone", "done_undone",
    ]  # fmt: skip
    assert client.post(f"/v1/tasks/{uuid.uuid4()}/undo-done", headers=auth).status_code == 404
    assert client.post("/v1/tasks/nope/undo-done", headers=auth).status_code == 404


def test_the_done_push_carries_the_undo() -> None:
    nid = uuid.uuid4()
    done = {
        "task_id": "t-1",
        "title": "BOBBY'S Bitterballen",
        "place": None,
        "undo_until": "2026-10-25T12:42:00+02:00",
    }
    assert _push_data("done", nid, None, None, None, done) == {
        "kind": "done", "notification_id": str(nid), "task_id": "t-1", "title": "BOBBY'S Bitterballen",
        "undo_until": "2026-10-25T12:42:00+02:00",
    }  # fmt: skip
    assert _category("done", None) == "DONE"


# --- by voice -----------------------------------------------------------------------------------------------


def test_voice_ci_sono_stato_mi_e_piaciuto(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_research_eval import research

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    task = add_task(db, "BOBBY'S Bitterballen")
    with_place(db, "tasks", task, place(db, "BOBBY'S Bitterballen", BOBBYS))
    said: list[Any] = [("mark_done", {"what": "BOBBY'S Bitterballen"})]
    t = research(db, secrets_key, "ci sono stato, mi è piaciuto", said,
                 "Fatto.", with_data=True)  # fmt: skip
    assert "Done: BOBBY'S Bitterballen. The user can undo it for 24 hours." in t.result("mark_done")
    assert db.execute("SELECT status FROM tasks").fetchone() == ("done",)
    assert db.execute("SELECT source, arrived_at FROM done_by_presence").fetchone() == ("voice", None)
    assert [text for text, _ in done_notes(db)] == ["Fatto: BOBBY'S Bitterballen ✓"]


def test_voice_two_matches_ask_one_question_and_none_matches_nothing(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_research_eval import research

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    add_task(db, "Comprare il pane")
    add_task(db, "Comprare il latte")
    t = research(db, secrets_key, "fatto, ho comprato", [("mark_done", {"what": "comprare"})], with_data=True)
    assert "Several match: " in t.result("mark_done") and "Ask which one" in t.result("mark_done")
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'done'").fetchone() == (0,)
    t = research(
        db, secrets_key, "il latte l'ho comprato", [("mark_done", {"what": "latte"})], with_data=True
    )
    assert "Done: Comprare il latte." in t.result("mark_done")
    t = research(
        db, secrets_key, "sono stato in palestra", [("mark_done", {"what": "palestra"})], with_data=True
    )
    assert "Nothing open matches" in t.result("mark_done")


def test_voice_mark_done_after_reading_outside_text_waits_for_yes(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """L3 for the new change tool: never from an email or a page. The
    user's own "fatto" acts even after the web; here a page says it, not the user, so it waits."""
    from tests.test_rain_links_web_yes import READ_WEB, l3_turn

    task = add_task(db, "Comprare i biglietti del traghetto")
    t = l3_turn(
        db, secrets_key, "il sito del traghetto cosa dice?",
        [READ_WEB, ("mark_done", {"what": "biglietti traghetto"})],
    )  # fmt: skip
    assert t.result("mark_done").startswith("Not done yet: this turn read text from outside")
    assert t.reply.endswith("Posso segnare «biglietti traghetto» come fatto?")
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("open",)
    yes = l3_turn(db, secrets_key, "sì", [("confirm_remember", {})], device=t.device)
    assert "Done: Comprare i biglietti del traghetto." in yes.result("confirm_remember")
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("done",)
