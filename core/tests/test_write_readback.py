"""Every write returns what is now there, read back from the store
after the write, never echoed from the call; a partial write says what did not change; a draft waiting for
their yes is never done; and Pisa's confirmation is said from the stored value. Every word here is
synthetic."""

import json
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall

from pisa.brain.task_tools import _bulk_said
from pisa.domain import results
from pisa.domain.tasks import state_line
from tests.assistant_env import add_task, assistant_data, rome
from tests.conftest import pair
from tests.owner import owner_at
from tests.test_brain_probes import _device, turn_with
from tests.test_intentions import call, rows, rt, tools
from tests.test_place_tools import data_of
from tests.test_rituals import _settings
from tests.test_task_control import NOW, TZ, row, talk


@pytest.fixture
def device(db: psycopg.Connection) -> uuid.UUID:
    return uuid.UUID(pair(db)[0])


@pytest.fixture
def owner(db: psycopg.Connection) -> None:
    _settings(db)


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


@pytest.fixture
def google(http_handler, db, secrets_key):  # type: ignore[no-untyped-def]
    from tests.actions_fake import ActionsGoogle, connect
    from tests.assistant_env import set_settings

    fake = ActionsGoogle()
    http_handler["handler"] = fake
    set_settings(db, {"language": "it-IT"})
    connect(db, secrets_key)
    return fake


def stored_of(result: str) -> dict[str, Any]:
    """Pisa's own result line inside a tool's words."""
    line = next(x for x in result.split("\n") if x.startswith('{"status"'))
    return json.loads(line)


# --- 1. tasks: the task as stored, not the call's values --------------------------------------------------


def test_a_task_write_says_the_task_as_the_store_normalised_it(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    task = add_task(db, "Ritirare il pacco")
    said = data_of(talk(db, secrets_key, device, "rename_task",
                        {"task_ref": "pacco", "title": "  Ritirare   il pacco  in posta "}))  # fmt: skip
    assert said == "Renamed. Now: «Ritirare il pacco in posta», open."  # the store's spaces
    said = data_of(talk(db, secrets_key, device, "set_task_deadline",
                        {"task_ref": "pacco", "deadline": "2026-10-24", "why": "lo dice lui"}))  # fmt: skip
    assert said.endswith("Now: «Ritirare il pacco in posta», open, by Sat 24 Oct.")  # a day: "by", no 00:00
    said = data_of(talk(db, secrets_key, device, "set_task_repeat",
                        {"task_ref": "pacco", "every": "month", "interval": 1}))  # fmt: skip
    assert "repeats every month" in said and "(s)" not in said  # the repeat as stored, not the call's words
    assert row(db, task)[7] == {"every": "month", "interval": 1}


def test_completing_a_repeating_task_names_the_next_one_the_store_made(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    task = add_task(db, "Innaffiare le piante", NOW.date())
    db.execute("UPDATE tasks SET repeat = %s WHERE id = %s", ('{"every": "week", "interval": 1}', task))
    said = data_of(talk(db, secrets_key, device, "complete_task", {"task_ref": "piante"}, "fatto"))
    first = said.split("\n")[0]
    assert first.startswith("Marked done: «Innaffiare le piante», done, by Thu 22 Oct, repeats every week.")
    assert "Next one: «Innaffiare le piante», open, by Thu 29 Oct, repeats every week." in first


def test_state_line_is_compact_and_in_his_zone() -> None:
    task = SimpleNamespace(
        title="Chiamare il notaio", status="open", due_at=rome(2026, 10, 23, 23, 30).astimezone(),
        due_kind="datetime", expected_from=None, duration_min=None, priority=2, place_text=None,
        country=None, repeat=None, notes="x" * 200,
    )  # fmt: skip
    line = state_line(task, TZ)  # type: ignore[arg-type]
    assert line.startswith("«Chiamare il notaio», open, due Fri 23 Oct 23:30, notes «")
    assert len(line) < 160  # one sentence's worth: a long note is cut


# --- 2. a partial write says what did not change -----------------------------------------------------------


def test_a_partial_bulk_change_says_which_tasks_did_not_change() -> None:
    a = SimpleNamespace(id="a", title="Pagare la bolletta")
    b = SimpleNamespace(id="b", title="Ritirare le camicie")
    partial = _bulk_said([a, b], {"count": 1, "changed": ["a"]}, "Done: 1 tasks (done).", "Say it.")  # type: ignore[list-item]
    assert "NOT changed (1, no longer open): Ritirare le camicie." in partial
    assert "Pagare la bolletta" not in partial.split("NOT changed")[1]
    none = _bulk_said([a, b], {"count": 0, "changed": []}, "Done: 0 tasks (done).", "Say it.")  # type: ignore[list-item]
    assert "Not changed: none of them is open any more" in none and "Done" not in none
    whole = _bulk_said([a], {"count": 1, "changed": ["a"]}, "Done: 1 tasks (done).", "Say it.")  # type: ignore[list-item]
    assert whole.startswith("Done: 1 tasks (done).") and "NOT changed" not in whole


def test_the_bulk_store_returns_which_ids_it_changed(client, db) -> None:  # type: ignore[no-untyped-def]
    still = add_task(db, "Comprare il latte")
    closed = add_task(db, "Comprare il pane")
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (closed,))
    bulk = rt(client).tasks_bulk

    async def go() -> dict[str, Any]:
        return await bulk.apply([still, closed], "done", origin="voice")

    out = client.portal.call(go)
    assert out["count"] == 1 and out["changed"] == [still]


# --- 3. Pisa's promises, intentions and rituals
# ---------------------------------------------------------------


def test_remember_to_says_the_time_as_stored_rolled_to_tomorrow(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    said = talk(db, secrets_key, device, "remember_to",
                {"what": "chiamare la mamma", "condition": "time", "when": "09:30"})  # fmt: skip
    # at 10:00 a 09:30 is tomorrow's: the store's moment, not the call's "09:30"
    assert said.startswith("Saved: Pisa brings up «chiamare la mamma» at Fri 23 Oct 09:30.")
    (due,) = db.execute("SELECT due_at FROM pisa_promises").fetchone()
    assert due == rome(2026, 10, 23, 9, 30)


def test_keep_intention_reads_back_its_cue(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    words = "ricordami di pagare il bollo domani alle 10"
    out = call(client, tools(client, words)["keep_intention"], what="pagare il bollo", cue="time",
               when="domani alle 10")  # fmt: skip
    (kept,) = rows(db)
    assert f"Kept (id {kept['id']})." in out and "Now: Cosa: pagare il bollo" in out
    task = db.execute("SELECT due_at FROM tasks WHERE id = %s", (kept["task_id"],)).fetchone()[0]
    assert f"{task.astimezone(__import__('zoneinfo').ZoneInfo(TZ)):%Y-%m-%d %H:%M}" in out  # the task's time


def test_set_ritual_reads_the_ritual_back(client, db) -> None:  # type: ignore[no-untyped-def]
    rituals = rt(client).rituals

    async def go() -> str:
        await rituals.put({"closing": {"at": "21:00"}})
        return await rituals.switch("closing", True, "accendi la chiusura della sera")

    assert client.portal.call(go) == "closing is now on at 21:00. Say it in a few words."  # the stored time


# --- 4. the calendar: the approved act and the undo, read back ---------------------------------------------


def test_an_approved_proposal_says_the_event_as_google_holds_it(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = rome(2026, 10, 20, 13)
    dev, a = _device(db), client.app.state.runtime.actions
    turn_with(
        client, a.create_event, "Corsa", "domani alle 6", said="metti la corsa domani alle 6", device=dev
    )
    (rid,) = db.execute("SELECT id::text FROM approvals").fetchone()
    google.normalise = 15  # this calendar keeps starts on the quarter hour: 06:00 stays, 06:10 would not
    out = turn_with(client, a.answer_proposal, rid[:8], True, "sì, la corsa", said="sì, la corsa",
                    device=dev, asked=frozenset({rid}))  # fmt: skip
    result = stored_of(out)
    assert result["status"] == results.DONE
    assert result["stored"] == {
        "title": "Corsa",
        "start": "Wed 2026-10-21 06:00",
        "end": "Wed 2026-10-21 07:00",
    }


def test_undo_says_what_the_calendar_holds_after_it(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = rome(2026, 10, 20, 13)
    dev, a = _device(db), client.app.state.runtime.actions
    from tests.actions_fake import CALENDAR, cached_event

    event = google.event("ev1", "Palestra", rome(2026, 10, 21, 10), rome(2026, 10, 21, 11))
    cached_event(db, event, CALENDAR)
    # the syncs after the move and the undo list ev1, as Google does: a read-back with no row is foreign
    google.listed = True
    words = "sposta la palestra domani alle 11"
    moved = turn_with(
        client, a.move_event, "palestra", "domani alle 11", said=words, device=dev, owner_said=words
    )
    assert stored_of(moved)["stored"]["start"] == "Wed 2026-10-21 11:00"
    undone = turn_with(client, a.undo_last_change, said="annulla", device=dev)
    # back at 10:00: the calendar's read-back after the undo, never the move's 11:00
    assert stored_of(undone)["stored"] == {
        "title": "Palestra", "start": "Wed 2026-10-21 10:00", "end": "Wed 2026-10-21 11:00",
    }  # fmt: skip


def test_a_store_that_normalises_the_time_makes_the_result_differ_from_the_request(  # type: ignore[no-untyped-def]
    client, db, google, clock
) -> None:
    clock.now = rome(2026, 10, 20, 13)
    dev, a = _device(db), client.app.state.runtime.actions
    google.normalise = 30  # the store rounds a start up to the half hour
    out = turn_with(client, a.create_event, "Dentista", "domani alle 18:10",
                    said="metti il dentista domani alle 18:10", device=dev,
                    owner_said="metti il dentista domani alle 18:10")  # fmt: skip
    result = stored_of(out)
    assert result["stored"]["start"] == "Wed 2026-10-21 18:30"  # what is there, not the 18:10 asked
    assert result.get("verified") is False  # and the read-back didn't confirm the request: no "fatto"


# --- 5. a draft waits for their yes, with what it will do
# ----------------------------------------------------


def test_a_draft_is_never_done_before_his_yes(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]
    from tests.test_approvals import run, thread

    thread(google, db)
    said = run(client, client.app.state.runtime.actions.draft_reply, "Claudio cover", "Ciao, ce l'ho.")
    first = said.split("\n")[0]
    assert first.startswith("Draft saved in Gmail, in the thread: NOT sent, waiting for their yes.")
    assert "Their yes sends it, as stored:" in said and "Text: Ciao, ce l'ho." in said


# --- 6. end to end on the stub: Pisa's confirmation carries the stored value
# -----------------------------------


def _confirms_from_the_result(tool: str, args: dict[str, Any]) -> Any:
    """A model that calls the write, then confirms in one sentence from what the result says is stored (the
    skills' rule), never from its own call."""

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == tool
        ]
        if not returns:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}
            return
        at = re.search(r"at (\w{3} \d{2} \w{3} \d{2}:\d{2})", str(returns[-1].content))
        yield f"Te lo ricordo {at.group(1) if at else '?'}."

    return reply


def test_pisa_confirms_with_the_stored_time_not_the_one_asked(db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    args = {"what": "chiamare la mamma", "condition": "time", "when": "09:30"}
    model = _confirms_from_the_result("remember_to", args)
    reply = talk(db, secrets_key, device, "remember_to", args, "ricordamelo alle nove e mezza", model=model)
    assert reply == "Te lo ricordo Fri 23 Oct 09:30."  # tomorrow, as stored at 10:00; never today's 09:30
    assert NOW + timedelta(hours=23) < db.execute("SELECT due_at FROM pisa_promises").fetchone()[0]


# --- review fixes -----------------------------------------------------------------------------------------


def test_the_next_one_is_the_one_this_completion_made_not_another_series(db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from pisa.brain.conversation import run_change
    from pisa.brain.follow_tools import Change
    from pisa.services.tasks import TasksService
    from tests.test_proactivity import _attend

    task = add_task(db, "Innaffiare le piante", NOW.date())
    other = add_task(db, "Innaffiare le piante", NOW.date() + timedelta(days=3))  # another series, same title
    for one in (task, other):
        db.execute("UPDATE tasks SET repeat = %s WHERE id = %s", ('{"every": "week", "interval": 1}', one))

    async def go() -> str:
        async with _attend(db, NOW) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            data = assistant_data(env.pool, tasks, clock=env.clock)
            return await run_change(data, Change("complete_task", {"task_id": task}))

    said = asyncio.run(go())
    assert "Next one: «Innaffiare le piante», open, by Thu 29 Oct" in said  # the one made, not Sun 25 Oct's


def test_a_counted_task_closed_before_his_yes_is_said_not_changed(db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pisa.brain.task_tools import add_task_control_tools
    from pisa.domain import decisions
    from pisa.services.tasks import TasksService
    from tests.test_proactivity import _attend

    ids = [add_task(db, f"Scaduto {n}", rome(2026, 10, 20, 9)) for n in range(3)]

    async def go() -> str:
        async with _attend(db, rome(2026, 10, 29, 12)) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            data = assistant_data(env.pool, tasks, clock=env.clock)
            agent: Agent[None, str] = Agent(TestModel())
            add_task_control_tools(agent, data, gate=None, lang=lambda: "it-IT")
            tool: Any = agent._function_toolset.tools["tasks_bulk"].function  # pyright: ignore[reportPrivateUsage]
            with decisions.collecting("togli tutti i task scaduti"):
                await tool(action="dismiss", filter="overdue")
            db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (ids[0],))  # done meanwhile
            with decisions.collecting("sì"):
                return await tool(action="dismiss", filter="overdue", confirm_count=3)

    said = asyncio.run(go())
    assert "Done: 2 tasks (dismiss). NOT changed (1 of the 3 counted, no longer open)." in said
