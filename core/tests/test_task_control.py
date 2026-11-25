"""Full control of tasks (contract Q): delete, repeat, manual moves and skips from the app, and the voice
tools (rename, reschedule, notes, priority, place, repeat, dismiss, restore, and delete asked first and done
only on the user's yes in the next turn). A reference matching several tasks is asked about, never
guessed."""

import asyncio
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from pisa.domain.errors import PisaError
from pisa.domain.tasks import next_due, parse_when
from pisa.services.settings import SettingsService
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, add_task, assistant_data, conversation, rome, set_settings
from tests.conftest import Clock, FakeModels, dsn, pair
from tests.owner import owner_at
from tests.places_env import place_env
from tests.places_fake import FakePlaces
from tests.test_place_tools import calls_tool, data_of

TZ = "Europe/Rome"
NOW = rome(2026, 10, 22, 10)


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


# --- the rules (pure) -------------------------------------------------------------------------------------


def test_next_due_and_when() -> None:
    week = {"every": "week", "interval": 1}
    due, kind = next_due(rome(2026, 10, 22, 23, 59), "date", week, NOW, TZ)
    assert (due, kind) == (rome(2026, 10, 29, 23, 59), "date")
    month = {"every": "month", "interval": 1}
    assert next_due(rome(2027, 1, 31, 23, 59), "date", month, NOW, TZ)[0] == rome(2027, 2, 28, 23, 59)
    two_days = {"every": "day", "interval": 2}
    assert next_due(rome(2026, 10, 22, 9, 30), "datetime", two_days, NOW, TZ) == (
        rome(2026, 10, 24, 9, 30),
        "datetime",
    )
    assert next_due(None, "none", week, NOW, TZ) == (rome(2026, 10, 29, 23, 59), "date")  # from the day done
    thursday = date(2026, 10, 22)
    assert parse_when("domani alle 15:30", thursday) == ("2026-10-23T15:30", "datetime")
    assert parse_when("venerdì", thursday) == ("2026-10-23", "date")
    assert parse_when("giovedì", thursday) == ("2026-10-29", "date")  # the next one, not today
    assert parse_when("Monday", thursday) == ("2026-10-26", "date")
    assert parse_when("2026-11-03", thursday) == ("2026-11-03", "date")
    assert parse_when("nessuna", thursday) == (None, "none")
    with pytest.raises(PisaError):
        parse_when("prima o poi", thursday)


# --- REST --------------------------------------------------------------------------------------------------


def test_delete_is_permanent(client, auth) -> None:  # type: ignore[no-untyped-def]
    task = client.post("/v1/tasks", headers=auth, json={"title": "Comprare il pane"}).json()
    assert client.delete(f"/v1/tasks/{task['id']}", headers=auth).status_code == 204
    assert client.get("/v1/tasks", headers=auth, params={"status": "all"}).json() == {"tasks": []}
    again = client.delete(f"/v1/tasks/{task['id']}", headers=auth)
    assert again.status_code == 404 and again.json()["error"]["code"] == "not_found"


def test_repeating_tasks(client, db, clock, auth) -> None:  # type: ignore[no-untyped-def]
    clock.now = NOW
    body = {
        "title": "Innaffiare le piante", "due_at": "2026-10-22", "due_kind": "date", "notes": "Il basilico",
        "place_text": "Casa", "duration_min": 15, "priority": 1, "repeat": {"every": "week", "interval": 1},
    }  # fmt: skip
    first = client.post("/v1/tasks", headers=auth, json=body).json()
    assert first["repeat"] == {"every": "week", "interval": 1}
    done = client.patch(f"/v1/tasks/{first['id']}", headers=auth, json={"status": "done"})
    assert done.status_code == 200
    (second,) = client.get("/v1/tasks", headers=auth).json()["tasks"]
    assert second["id"] != first["id"] and second["due_at"] == "2026-10-29T23:59:00+01:00"
    same = ("title", "notes", "place_text", "duration_min", "priority", "repeat")
    assert {k: second[k] for k in same} == {k: first[k] for k in same}
    client.patch(f"/v1/tasks/{second['id']}", headers=auth, json={"status": "dismissed"})
    # H8: dismissing ends only this occurrence: the series goes on.
    (after,) = client.get("/v1/tasks", headers=auth).json()["tasks"]
    assert after["due_at"] == "2026-11-05T23:59:00+01:00" and after["title"] == first["title"]
    client.delete(f"/v1/tasks/{after['id']}", headers=auth)
    third = client.post("/v1/tasks", headers=auth, json=body | {"title": "Pagare l'affitto"}).json()
    stopped = client.patch(f"/v1/tasks/{third['id']}", headers=auth, json={"repeat": None}).json()
    assert stopped["repeat"] is None
    client.patch(f"/v1/tasks/{third['id']}", headers=auth, json={"status": "done"})
    assert client.get("/v1/tasks", headers=auth).json()["tasks"] == []  # the series stopped
    bad = client.post(
        "/v1/tasks", headers=auth, json={"title": "Ogni tanto", "repeat": {"every": "year", "interval": 1}}
    )  # fmt: skip
    assert bad.status_code == 400
    too_far = client.post(
        "/v1/tasks", headers=auth, json={"title": "Ogni tanto", "repeat": {"every": "day", "interval": 13}}
    )
    assert too_far.status_code == 400


@pytest.mark.usefixtures("owner_plans_every_task")
def test_moves_and_skips_from_the_app(client, db, clock, http_handler, auth) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    set_settings(db, {"quiet": {"hours": None}})
    task = add_task(db, "Ritirare il pacco", NOW.date())
    db.execute("UPDATE tasks SET duration_min = 45 WHERE id = %s", (task,))
    event = add_event(db, "Riunione", rome(2026, 10, 22, 11), minutes=90)
    move = {"task_id": task, "action": "move", "start": "2026-10-22T16:00:00+02:00"}
    moved = client.post("/v1/plan/adjustments", headers=auth, json=move)
    assert moved.status_code == 201, moved.json()
    item = next(i for i in moved.json()["items"] if i.get("task_id") == task)
    assert (item["start"], item["end"], item["adjusted"]) == (
        "2026-10-22T16:00:00+02:00", "2026-10-22T16:45:00+02:00", True,
    )  # fmt: skip
    skipped = client.post("/v1/plan/adjustments", headers=auth, json={"event_id": event, "action": "skip"})
    assert next(i for i in skipped.json()["items"] if i.get("event_id") == event)["status"] == "skipped"
    rows = db.execute("SELECT action, source FROM plan_adjustments ORDER BY seq").fetchall()
    assert rows == [("move", "app"), ("skip", "app")]
    no_time = client.post("/v1/plan/adjustments", headers=auth, json={"task_id": task, "action": "move"})
    assert (
        no_time.status_code == 400 and no_time.json()["error"]["message"] == "A move needs start (RFC 3339)."
    )
    both = client.post(
        "/v1/plan/adjustments", headers=auth, json={"task_id": task, "event_id": event, "action": "skip"}
    )
    assert both.status_code == 400
    # M17: outside today's planner window (08:00-21:00), or in the past (now is 10:00): refused.
    for start, message in (
        (
            "2026-10-22T03:00:00+02:00",
            "The start must be inside today's plan, from 08:00 to 21:00 (2026-10-22).",
        ),
        (
            "2026-10-23T10:00:00+02:00",
            "The start must be inside today's plan, from 08:00 to 21:00 (2026-10-22).",
        ),
        ("2026-10-22T09:30:00+02:00", "The start 09:30 is in the past."),
        # The task lasts 45 minutes: from 20:30 it would end at 21:15 .
        ("2026-10-22T20:30:00+02:00", "It would end at 21:15, after today's plan ends (21:00)."),
    ):
        refused = client.post("/v1/plan/adjustments", headers=auth, json=move | {"start": start})
        assert refused.status_code == 400 and refused.json()["error"]["message"] == message, start
    assert db.execute("SELECT count(*) FROM plan_adjustments").fetchone() == (2,)


# --- by voice ---------------------------------------------------------------------------------------------


def talk(
    db: psycopg.Connection, secrets_key: str, device: uuid.UUID, tool: str, args: dict[str, Any],
    text: str = "sì", at: datetime = NOW, model: Any = None,
) -> str:  # fmt: skip
    """One turn (the user says `text`) whose model calls `tool(args)`; the tool's result (as the model said
    it)."""
    models = FakeModels()
    models.default = model or calls_tool(tool, args)

    async def go() -> str:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(at), models, keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)

            async def brief() -> str:
                return ""

            data = assistant_data(
                env.pool, tasks, clock=env.clock, places=env.places,
                settings=SettingsService(env.pool, owner=owner_at()), brief=brief,
            )  # fmt: skip
            service = conversation(
                env.pool, env.places._gateway, owner=owner_at(TZ), clock=env.clock, data=data
            )

            async def emit(delta: str, final: bool) -> None:
                pass

            return await service.respond(device, text, "it-IT", emit, trace_id="t")

    return asyncio.run(go())


def row(db: psycopg.Connection, task: str) -> Any:
    return db.execute(
        "SELECT title, status, due_at, due_kind, notes, priority, place_text, repeat FROM tasks WHERE id = "
        "%s",
        (task,),
    ).fetchone()


@pytest.fixture
def device(db: psycopg.Connection) -> uuid.UUID:
    return uuid.UUID(pair(db)[0])


def test_rename_reschedule_notes_priority_place_repeat(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    task = add_task(db, "Ritirare il pacco", NOW.date())
    said = data_of(
        talk(
            db, secrets_key, device, "rename_task", {"task_ref": "pacco", "title": "Ritirare il pacco Amazon"}
        )
    )
    assert said == "Renamed. Now: «Ritirare il pacco Amazon», open, by Thu 22 Oct."
    said = data_of(
        talk(db, secrets_key, device, "reschedule_task", {"task_ref": "pacco", "when": "domani alle 15:30"})
    )
    assert said == "Deadline set. Now: «Ritirare il pacco Amazon», open, due Fri 23 Oct 15:30."
    # Each write says the task as stored (the notes and the place were never in the result)
    said = data_of(
        talk(db, secrets_key, device, "set_task_notes", {"task_ref": "pacco", "notes": "  Codice 4521 "})
    )
    assert said.endswith("notes «Codice 4521».")  # stripped by the store: the stored value, not the call's
    talk(db, secrets_key, device, "set_task_priority", {"task_ref": "pacco", "priority": 1})
    said = data_of(
        talk(
            db,
            secrets_key,
            device,
            "set_task_place",
            {"task_ref": "pacco", "place": "Poste, Via Immaginaria 1"},
        )
    )
    assert "place «Poste, Via Immaginaria 1»" in said
    said = data_of(
        talk(
            db, secrets_key, device, "set_task_repeat", {"task_ref": "pacco", "every": "week", "interval": 2}
        )
    )
    assert said == (
        "Repeat set. Now: «Ritirare il pacco Amazon», open, due Fri 23 Oct 15:30, priority high, "
        "place «Poste, Via Immaginaria 1», repeats every 2 weeks, notes «Codice 4521»."
    )
    title, status, due, kind, notes, priority, place, repeat = row(db, task)
    assert (title, status, kind, notes, priority, place) == (
        "Ritirare il pacco Amazon", "open", "datetime", "Codice 4521", 1, "Poste, Via Immaginaria 1",
    )  # fmt: skip
    assert due == rome(2026, 10, 23, 15, 30) and repeat == {"every": "week", "interval": 2}
    said = data_of(talk(db, secrets_key, device, "reschedule_task", {"task_ref": "pacco", "when": "nessuna"}))
    assert said.startswith("No due date now. Now: «Ritirare il pacco Amazon», open, priority high,")


def test_dismiss_and_restore(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    task = add_task(db, "Chiamare il dentista")
    assert data_of(talk(db, secrets_key, device, "dismiss_task", {"task_ref": "dentista"})) == (
        "Dismissed. Now: «Chiamare il dentista», dismissed."
    )
    assert row(db, task)[1] == "dismissed"
    assert data_of(talk(db, secrets_key, device, "restore_task", {})) == (
        "Restored. Now: «Chiamare il dentista», open."
    )
    assert row(db, task)[1] == "open"


def test_several_matches_are_asked_about(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    add_task(db, "Pagare la bolletta della luce")
    add_task(db, "Pagare la bolletta del gas")
    said = data_of(
        talk(db, secrets_key, device, "set_task_priority", {"task_ref": "bolletta", "priority": 1})
    )
    assert said.startswith("Several tasks match: ") and "don't guess" in said
    assert db.execute("SELECT count(*) FROM tasks WHERE priority = 1").fetchone() == (0,)


def test_delete_acts_at_once_and_restore_brings_it_back(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    """The user's words delete it at once, softly (no ask-then-confirm);
    restore_task brings it back within 24 hours (test_acting: the purge after)."""
    task = add_task(db, "Comprare un regalo")
    said = talk(db, secrets_key, device, "delete_task", {"task_ref": "regalo"}, "cancella il regalo")
    # The result says done (its line, which the model reads and doesn't say), and Undo
    assert "Deleted: Comprare un regalo" in said and "restore_task" in said
    assert row(db, task)[1] == "deleted"
    talk(db, secrets_key, device, "restore_task", {"task_ref": "regalo"}, "rimettilo")
    assert row(db, task)[1] == "open"


def test_the_prompt_lists_the_task_tools() -> None:
    from pisa.brain.conversation import system_prompt
    from pisa.brain.skills import load

    prompt = " ".join(
        (system_prompt(lang="it-IT") + "".join(s.body for s in load())).split()
    )  # the prompt, the skills
    for name in ("rename_task", "reschedule_task", "set_task_notes", "set_task_priority", "set_task_place",
                 "set_task_repeat", "dismiss_task", "restore_task", "delete_task"):  # fmt: skip
        assert name in prompt
    assert "confirm_delete_task" not in prompt and "deleted at once" in prompt


def test_a_repeating_task_never_makes_two_next_ones(client, db, clock, auth) -> None:  # type: ignore[no-untyped-def]
    """M11: done → undo → done, a restore, or two completions at once make one next occurrence; reopening
    removes the next one it made unless the user already changed it."""
    clock.now = NOW
    body = {
        "title": "Bollette",
        "due_at": "2026-10-22",
        "due_kind": "date",
        "repeat": {"every": "day", "interval": 1},
    }
    task = client.post("/v1/tasks", headers=auth, json=body).json()
    url = f"/v1/tasks/{task['id']}"

    def open_titles() -> list[str]:
        return [t["due_at"][:10] for t in client.get("/v1/tasks", headers=auth).json()["tasks"]]

    client.patch(url, headers=auth, json={"status": "done"})
    assert open_titles() == ["2026-10-23"]
    client.patch(url, headers=auth, json={"status": "open"})  # undo: the untouched next one goes
    assert open_titles() == ["2026-10-22"]
    client.patch(url, headers=auth, json={"status": "done"})
    client.patch(url, headers=auth, json={"status": "done"})  # twice: one next
    (nxt,) = client.get("/v1/tasks", headers=auth).json()["tasks"]
    clock.now = NOW + timedelta(minutes=5)
    client.patch(f"/v1/tasks/{nxt['id']}", headers=auth, json={"notes": "la luce"})  # the user changed it
    client.patch(url, headers=auth, json={"status": "open"})  # restore: a changed next one stays
    client.patch(url, headers=auth, json={"status": "done"})  # and no second one is made
    assert open_titles() == ["2026-10-23"]


def test_two_completions_at_once_make_one_next(db, clock) -> None:  # type: ignore[no-untyped-def]
    """M11: the status change applies once (AND status <> 'done'); the index allows one next per task."""
    from pisa.db.pool import open_pool

    async def go() -> int:
        pool = await open_pool(dsn(db), max_size=4)
        await pool.wait()
        try:
            service = TasksService(pool, owner=owner_at(), clock=lambda: NOW)
            task = await service.create(
                title="Bollette", source_kind="manual", due_at="2026-10-22", due_kind="date",
                repeat={"every": "day", "interval": 1},
            )  # fmt: skip
            await asyncio.gather(*(service.update(task.id, {"status": "done"}) for _ in range(4)))
            return len(await service.list("open"))
        finally:
            await pool.close()

    assert asyncio.run(go()) == 1


def test_short_yes() -> None:
    from pisa.domain.tasks import is_short_yes

    for yes in ("sì", "Si!", "ok", "OK, cancellalo", "yes please", "va bene", "certo", "confermo"):
        assert is_short_yes(yes), yes
    for no in ("Pisa?", "no", "sì ma no", "ok leggi la mail e poi cancella tutto", "", "bene", "forse sì"):
        assert not is_short_yes(no), no


def test_a_task_from_a_places_card_is_at_that_place(client, db, clock, auth) -> None:  # type: ignore[no-untyped-def]
    """ "Plan it" sends the card's place_id; the task is there, named after it."""
    clock.now = NOW
    place_id = db.execute(
        "INSERT INTO places (id, name, lat, lon, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Osteria sul Ponte', 45.451, 9.08, now(), now()) RETURNING id::text"
    ).fetchone()[0]  # type: ignore[index]
    task = client.post("/v1/tasks", headers=auth, json={"title": "Cena", "place_id": place_id}).json()
    assert (task["place_id"], task["place_text"]) == (place_id, "Osteria sul Ponte")
    named = client.post(
        "/v1/tasks",
        headers=auth,
        json={"title": "Aperitivo", "place_id": place_id, "place_text": "sul ponte"},
    ).json()
    assert (named["place_id"], named["place_text"]) == (place_id, "sul ponte")
    missing = client.post("/v1/tasks", headers=auth, json={"title": "X", "place_id": "nope"})
    assert (
        missing.status_code == 404
        and missing.json()["error"]["message"] == "There is no place with id 'nope'."
    )
    assert db.execute("SELECT count(*) FROM tasks WHERE title = 'X'").fetchone() == (0,)
