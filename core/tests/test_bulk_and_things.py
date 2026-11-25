"""the rows, the bulk tool, tasks that move on, things v1, smaller turns, the evals'
own budget and the reads IOS needs."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from pisa.domain import attend as A
from pisa.infra import hard_limit
from tests.assistant_env import assistant_data
from tests.conftest import pair
from tests.owner import owner_at
from tests.test_acting import google as google  # the fixture
from tests.test_proactivity import T0, _attend, _propose

# --- review rows ---------------------------------------------------------------------------------------


def test_floors_go_by_the_rules_right_after_reminders_and_the_rest_waits(db) -> None:  # type: ignore[no-untyped-def]
    asked: list[int] = []

    async def decide(candidates: list[A.Candidate], moment: Any) -> list[A.Decision]:
        asked.append(len(candidates))
        return []

    async def go() -> Any:
        async with _attend(db, T0, decide) as env:
            await _propose(env, "f", "Esci ora: tram 13 alle 9:12, 20 minuti.", "leave", kind="reminder",
                           urgency="now", floor=True, deadline=T0 + timedelta(minutes=12))  # fmt: skip
            await _propose(env, "p", "Il pacco.")
            env.clock.now += timedelta(minutes=5)  # the promise's window is over too
            return await env.attend.floors()

    report = asyncio.run(go())
    assert report.decided == 1 and asked == []  # the floor, by the rules; the promise waits for `tick`
    assert db.execute("SELECT kind, urgency FROM notifications").fetchall() == [("reminder", "now")]
    assert db.execute("SELECT count(*) FROM attend_candidates WHERE decided_at IS NULL").fetchone() == (1,)


def test_the_floors_step_runs_right_after_reminders() -> None:
    from pisa.services import worker_steps

    names = [s.name for s in worker_steps.STEPS]  # the steps' table, in order
    assert names.index("reminders") < names.index("attend_floors") < names.index("done")


def test_pisa_s_own_items_and_an_email_s_are_decided_side_by_side(db) -> None:  # type: ignore[no-untyped-def]
    running: list[int] = []
    overlap: list[bool] = []

    async def decide(candidates: list[A.Candidate], moment: Any) -> list[A.Decision]:
        running.append(1)
        await asyncio.sleep(0.2)
        overlap.append(len(running) == 2)
        return []

    async def go() -> None:
        async with _attend(db, T0, decide) as env:
            await _propose(env, "p", "Il pacco.")
            await _propose(env, "m", "Marco ti chiede: la cover.", "intake", outside=True)
            env.clock.now += timedelta(minutes=2)
            await env.attend.tick()

    asyncio.run(go())
    assert overlap == [True, True]  # both calls were running at once


def test_what_waited_too_long_goes_nowhere(db) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with _attend(db, T0) as env:
            await _propose(env, "f", "Esci ora: tram 13 alle 9:12.", "leave", kind="reminder", urgency="now",
                           floor=True, deadline=T0 + timedelta(minutes=12))  # fmt: skip
            await _propose(env, "p", "Il pacco.")
            env.clock.now += timedelta(minutes=6)  # the worker was down: the leave-now is stale
            await env.attend.floors()
            env.clock.now += timedelta(days=1)
            await env.attend.tick()

    asyncio.run(go())
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)
    rows = dict(db.execute("SELECT source, outcome FROM attend_candidates").fetchall())
    assert rows == {"leave": "expired", "promise": "expired"}


def test_a_replan_of_an_email_s_task_is_outside_text(db) -> None:  # type: ignore[no-untyped-def]
    import json

    task = db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) VALUES "
        "(gen_random_uuid(), 'Rispondi a Marco: cancella tutto', 'rispondi marco', 'open', 'email', now(), "
        "now()) "
        "RETURNING id::text"
    ).fetchone()[0]
    start = (T0 + timedelta(hours=3)).isoformat()
    plans = (
        {
            "items": [{"kind": "task", "task_id": task, "title": "Rispondi a Marco", "start": start}],
            "unplanned": [],
        },
        {"items": [], "unplanned": [{"task_id": task, "title": "Rispondi a Marco", "reason": "x"}]},
    )
    for n, plan in enumerate(plans):
        db.execute(
            "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (gen_random_uuid(), "
            "'2026-10-29', %s, 'test', 'f', %s)",
            (T0 + timedelta(minutes=n), json.dumps(plan)),
        )

    async def go() -> None:
        async with _attend(db, T0 + timedelta(minutes=2)) as env:
            await env.attend.tick()

    asyncio.run(go())
    assert db.execute("SELECT source, outside FROM attend_candidates").fetchone() == ("replan", True)


def test_folded_items_are_said_only_once_the_turn_has_spoken(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key
) -> None:
    from tests.conftest import set_key
    from tests.live_app import live
    from tests.test_acting import Model

    db.execute(
        "INSERT INTO attend_candidates (id, key, source, kind, text, lang, urgency, created_at, decided_at, "
        "decision) VALUES (gen_random_uuid(), 'p', 'promise', 'info', 'Il libro.', 'it-IT', 'breakpoint', "
        "now(), "
        'now(), \'{"choice": "fold_next", "text": "Il libro."}\')'
    )

    async def broken(messages, info):  # type: ignore[no-untyped-def]
        raise RuntimeError("the provider is down")
        yield  # pragma: no cover

    set_key(db, secrets_key)
    device_id, token = pair(db)
    for model in (broken, Model("(never these words)", [])):
        fake_models.default = model
        with live(client, token, device_id) as app:
            app.say("ciao", lang="it-IT", source="earbuds_mic")
            while (m := app.next())["type"] not in ("error",) and not (m["type"] == "say" and m["final"]):
                pass
        said = db.execute("SELECT outcome FROM attend_candidates").fetchone()[0]
        assert said == (None if model is broken else "said_in_talk")  # a failed turn keeps it for the next


def test_the_eval_decides_as_the_service_does() -> None:
    from pisa_tools import attend_eval as E

    calls: list[set[bool]] = []

    async def record(case: E.Case, members: list[A.Candidate], moment: Any) -> list[A.Decision]:
        assert not any(c.floor for c in members)  # floors go by the rules
        calls.append({c.outside for c in members})
        return []

    asyncio.run(E.evaluate(record, E.select(None)))
    assert calls and all(len(kinds) == 1 for kinds in calls)  # an email never shares a call with Pisa's own
    assert E.quiet_at(T0.replace(hour=23)) and not E.quiet_at(T0.replace(hour=12))


def test_a_dismissed_notice_is_the_owner_s_dismissed(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with _attend(db, T0) as env:
            await _propose(env, "p", "Il pacco.")
            env.clock.now += timedelta(minutes=2)
            await env.attend.tick()

    asyncio.run(go())
    (notification_id,) = db.execute("SELECT id FROM notifications").fetchone()
    _, token = pair(db)
    clock.now = datetime.fromisoformat(
        "2026-10-29T09:04:00+01:00"
    )  # the server's now: the swipe is a minute old
    event = {"event": "notification_dismissed", "at": "2026-10-29T09:03:00+01:00",
             "notification_id": str(notification_id), "site": "Banner"}  # fmt: skip
    nameless = {"event": "notification_dismissed", "at": "2026-10-29T09:03:00+01:00"}
    response = client.post("/v1/devices/me/diagnostics", headers={"Authorization": f"Bearer {token}"},
                           json={"appEvents": [event, nameless]})  # fmt: skip
    assert response.status_code == 204
    assert db.execute("SELECT outcome FROM attend_candidates").fetchone() == ("dismissed",)
    assert db.execute("SELECT to_value FROM feedback_events WHERE kind = 'attend'").fetchone() == (
        "dismissed",
    )
    stored = db.execute("SELECT event, notification_id FROM device_app_events").fetchall()
    assert stored == [("notification_dismissed", notification_id)]  # the one without an id said nothing


# --- The bulk tool and Today's edit mode -------------------------------------------------------------------


def test_the_closed_filters_name_only_open_tasks(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import tasks_bulk as B
    from pisa.domain.tasks import Task
    from tests.assistant_env import TZ, rome

    def task(n: int, **kw: Any) -> Task:
        base = {
            "id": f"t{n}", "title": f"T{n}", "notes": None, "status": "open", "due_at": None,
            "due_kind": "none", "duration_min": None, "place_text": None, "priority": 2,
            "source_kind": "manual", "source_ref": None, "source_label": None, "confidence": None,
            "created_at": rome(2026, 10, 1), "updated_at": rome(2026, 10, 1),
        }  # fmt: skip
        return Task(**(base | kw))

    now = rome(2026, 10, 29, 12)
    tasks = [
        task(1, due_at=rome(2026, 10, 28, 9), due_kind="datetime"), task(2, priority=3),
        task(3, source_kind="email"), task(4, created_at=rome(2026, 10, 28)), task(5, status="done"),
    ]  # fmt: skip

    def ids(which: Any, **kw: Any) -> list[str]:
        return [t.id for t in B.select(tasks, which, now=now, timezone=TZ, **kw)]

    assert ids("all") == ["t1", "t2", "t3", "t4"]  # never a done one
    assert (
        ids("overdue") == ["t1"] and ids("low_priority") == ["t2"] and ids("source", source="email") == ["t3"]
    )
    assert ids("older_than", days=7) == ["t1", "t2", "t3"] and ids("older_than", days=0) == []
    assert ids("from_sender", sender="amazon", senders={"t3": "Amazon.it ship@amazon.it"}) == ["t3"]
    assert ids("from_sender", sender="a", senders={"t3": "Amazon"}) == []  # too short to name anyone
    assert ids("ids", ids=["t2", "t5", "nope"]) == ["t2"]
    assert B.count_text(23, "dismiss", "it-IT") == "Sono 23: li tolgo tutti?"


def test_today_s_edit_mode_changes_the_set_with_one_undo(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task

    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    one, two, closed = add_task(db, "Uno"), add_task(db, "Due"), add_task(db, "Tre", status="done")
    out = client.post("/v1/tasks/bulk", headers=auth, json={"ids": [one, two, closed], "action": "delete"})
    assert out.status_code == 200 and out.json()["count"] == 2  # the one already done isn't touched
    rows = dict(db.execute("SELECT title, status FROM tasks").fetchall())
    assert rows == {"Uno": "deleted", "Due": "deleted", "Tre": "done"}
    undo = client.post(f"/v1/tasks/bulk/{out.json()['bulk_id']}/undo", headers=auth)
    assert undo.status_code == 200 and undo.json()["count"] == 2
    assert dict(db.execute("SELECT title, status FROM tasks").fetchall())["Uno"] == "open"
    assert (
        client.post(f"/v1/tasks/bulk/{out.json()['bulk_id']}/undo", headers=auth).status_code == 404
    )  # once
    bad = client.post("/v1/tasks/bulk", headers=auth, json={"ids": [one], "action": "explode"})
    assert bad.status_code == 400


def test_pisa_says_the_count_acts_on_one_yes_and_never_on_more(db) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pisa.brain.task_tools import add_task_control_tools
    from pisa.domain import decisions
    from pisa.services.tasks import TasksService
    from tests.assistant_env import TZ, add_task, rome

    for n in range(3):
        add_task(db, f"Scaduto {n}", rome(2026, 10, 20, 9))
    add_task(db, "Futuro", rome(2026, 11, 20, 9))

    async def go() -> list[str]:
        async with _attend(db, rome(2026, 10, 29, 12)) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            data = assistant_data(env.pool, tasks, clock=env.clock)
            agent: Agent[None, str] = Agent(TestModel())
            add_task_control_tools(agent, data, gate=None, lang=lambda: "it-IT")
            tool: Any = agent._function_toolset.tools["tasks_bulk"].function
            said = []
            with decisions.collecting("togli tutti i task scaduti"):
                said.append(await tool(action="dismiss", filter="overdue"))
                said.append(await tool(action="dismiss", filter="overdue", confirm_count=3))  # no yes yet
            add_task(db, "Scaduto 3", rome(2026, 10, 21, 9))
            with decisions.collecting("sì, toglili tutti"):
                said.append(await tool(action="dismiss", filter="overdue", confirm_count=3))  # now 4: more
            with decisions.collecting("sì"):  # The yes to the new count, in the next message
                said.append(await tool(action="dismiss", filter="overdue", confirm_count=4))
            with decisions.collecting("annulla"):
                said.append(await tool(action="undo"))
            return said

    preview, no_yes, grew, done, undone = asyncio.run(go())
    assert "3 open tasks match" in preview and "Sono 3: li tolgo tutti?" in preview
    assert no_yes.startswith("Not changed") and grew.startswith("Not changed: now 4 match, not 3")
    assert done.startswith("Done: 4 tasks") and undone.startswith("Restored 4 tasks")
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'open'").fetchone() == (5,)


# --- Tasks that move on -------------------------------------------------------------------------------------


def test_an_aged_task_leaves_the_plan_is_asked_once_and_not_yet_keeps_it(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome, set_settings
    from tests.test_follow import run

    set_settings(db, {"quiet": {"hours": None}})
    old = add_task(db, "Pagare F24", rome(2026, 10, 20, 9))
    recent = add_task(db, "Chiamare Nino", rome(2026, 10, 28, 9))
    now = rome(2026, 10, 29, 12)

    async def first(follow: Any, env: Any) -> Any:
        aged = await follow._tasks.aged_ids()
        await follow._planner.plan(reason="test")
        asked = await follow.aging()
        again = await follow.aging()  # one question per task, not every minute
        return aged, asked, again

    aged, asked, again = run(db, secrets_key, now, first)
    assert aged == {old} and asked == 1 and again == 0
    (plan,) = db.execute("SELECT plan FROM plans ORDER BY generated_at DESC LIMIT 1").fetchone()
    planned = {i.get("task_id") for i in plan["items"]} | {u["task_id"] for u in plan.get("unplanned") or []}
    planned |= {t["task_id"] for t in plan["todo"]}  # Today's tasks with no time
    assert old not in planned and recent in planned  # aged: out of the plan, still in the list
    check_in, text, purpose = db.execute("SELECT id, text, purpose FROM check_ins").fetchone()
    assert (text, purpose) == ("«Pagare F24» è scaduto da 9 giorni: è ancora da fare?", "aging")

    async def keep(follow: Any, env: Any) -> Any:
        await follow.answer(str(check_in), "not_done")
        return await follow._tasks.aged_ids()

    assert run(db, secrets_key, now + timedelta(minutes=5), keep) == set()  # kept: back in the plan
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (old,)).fetchone() == ("open",)


def test_a_week_without_an_answer_and_the_task_goes_with_its_reason(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome
    from tests.test_follow import run

    now = rome(2026, 10, 29, 12)
    task = add_task(db, "Rinnovare il dominio", rome(2026, 10, 10, 9))
    from tests.test_changes_feed import aging_asked

    aging_asked(db, task, now - timedelta(days=8))  # A question that reached the user

    async def tick(follow: Any, env: Any) -> int:
        return await follow.aging()

    assert run(db, secrets_key, now, tick) == 1
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("dismissed",)
    move = db.execute("SELECT move, tool, ref, purpose FROM pisa_decisions").fetchone()
    assert move[:3] == ("act", "aging", task) and move[3].startswith("Scaduto e senza risposta da 7 giorni")


def test_a_follow_up_in_the_thread_closes_its_task_with_undo(db) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from pisa.brain.intake import Triage
    from pisa.services.intake import IntakeService

    thread = "thread-1"
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels) "
        "VALUES ('m1', %s, 'GLS', 'gls@x.example', 'Il tuo pacco', now(), ARRAY['INBOX'])",
        (thread,),
    )
    task = db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, source_ref, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Ritirare il pacco', 'ritirare pacco', 'open', 'email', 'm1', now(), "
        "now()) "
        "RETURNING id::text"
    ).fetchone()[0]
    other = db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) VALUES "
        "(gen_random_uuid(), 'Pagare la luce', 'pagare luce', 'open', 'manual', now(), now()) "
        "RETURNING id::text"
    ).fetchone()[0]

    def verdict(done: bool) -> Triage:
        return Triage(category="delivery", automated=True, asks_owner_to_act=False, already_done=done,
                      reason="ritirato", notice="none")  # fmt: skip

    async def go(done: bool) -> int:
        async with _attend(db, T0) as env:
            intake = IntakeService(env.pool, None, owner=owner_at(), clock=env.clock, attend=env.attend)  # type: ignore[arg-type]

            async def triage(*args: Any, **kwargs: Any) -> Triage:
                return verdict(done)

            intake._triage = triage  # type: ignore[method-assign]
            return await intake._follow_up(SimpleNamespace(id="m2", thread_id=thread), T0)  # type: ignore[arg-type]

    assert asyncio.run(go(False)) == 0  # "in consegna domani": nothing closes
    assert asyncio.run(go(True)) == 1
    rows = dict(db.execute("SELECT id::text, status FROM tasks").fetchall())
    assert rows == {task: "done", other: "open"}  # only Pisa's own task from that thread
    assert db.execute("SELECT source, task_id::text FROM done_by_presence").fetchone() == ("email", task)
    assert db.execute("SELECT source, kind, alone, outside FROM attend_candidates").fetchone() == (
        "stay",
        "done",
        True,
        True,  # Its words are the email's task's title: outside text
    )


# --- Things v1 ---------------------------------------------------------------------------------------------


def test_only_numbers_that_name_one_thing_are_hard_keys() -> None:
    from datetime import date

    from pisa.domain import things as T

    keys = [{"kind": "order", "value": "402-1234567"}, {"kind": "amount", "value": "€ 49,90"},
            {"kind": "date", "value": "2026-11-06"}, {"kind": "tracking", "value": "1z999aa1"},
            {"kind": "order", "value": "402-1234567"}, {"kind": "reference", "value": "abc"}]  # fmt: skip
    # never an amount, a date or a short code
    assert T.typed_keys(keys) == [("order", "402-1234567"), ("tracking", "1Z999AA1")]
    assert T.dates([{"kind": "date", "value": "venerdì 6/11/2026 alle 21"}]) == [date(2026, 11, 6)]
    assert T.same_venue(T.venue_words("Teatro Ombrellai, Milano"), "Concerto", "Teatro degli Ombrellai")


def test_emails_join_a_thing_by_number_thread_or_date_and_place_and_never_merge(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import things

    def mail(mid: str, thread: str) -> None:
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels) "
            "VALUES (%s, %s, 'Zalando', 'z@x.example', %s, now(), ARRAY['INBOX'])",
            (mid, thread, f"Ordine {mid}"),
        )

    for mid, thread in (("a", "t1"), ("b", "t2"), ("c", "t1"), ("d", "t3"), ("e", "t4")):
        mail(mid, thread)
    from tests.assistant_env import add_event, rome

    event = add_event(db, "Concerto", rome(2026, 11, 6, 20), location="Teatro degli Ombrellai")
    order = [{"kind": "order", "value": "402-1234567"}]

    async def go() -> list[Any]:
        async with _attend(db, T0) as env, env.pool.connection() as conn:
            link = things.link_email
            first = await link(
                conn, message_id="a", thread_id="t1", keys=order, subject="Ordine", gist=None, now=T0
            )
            by_key = await link(
                conn, message_id="b", thread_id="t2", keys=order, subject="Spedito", gist=None, now=T0
            )
            by_thread = await link(
                conn, message_id="c", thread_id="t1", keys=[], subject="Re", gist=None, now=T0
            )
            nothing = await link(
                conn, message_id="d", thread_id="t3", keys=[], subject="Ciao", gist=None, now=T0
            )
            tickets = [{"kind": "order", "value": "402-7654321"}, {"kind": "date", "value": "2026-11-06"}]
            other = await link(conn, message_id="e", thread_id="t4", keys=tickets, subject="Biglietti",
                               gist={"where": "Teatro Ombrellai"}, now=T0)  # fmt: skip
            return [first, by_key, by_thread, nothing, other]

    first, by_key, by_thread, nothing, other = asyncio.run(go())
    assert first is not None and first == by_key == by_thread and nothing is None
    assert other is not None and other != first  # another order of the same shop: another thing
    items = db.execute("SELECT item_kind, item_ref, linked_by FROM thing_items ORDER BY item_ref").fetchall()
    assert ("event", event, "date_place") in items and ("email", "b", "key") in items


def test_a_second_email_about_the_same_order_updates_its_task_never_adds_one(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.intake_fake import IntakeModel, task
    from tests.test_intake import add_email, intake, tasks

    add_email(db, "m-1", "Ordine confermato", "Il tuo ordine n. 402-1234567: ritiralo al punto Fermopoint.")
    add_email(db, "m-2", "Ordine pronto", "Ordine n. 402-1234567 pronto al ritiro entro il 6 novembre.")
    db.execute("UPDATE email_messages SET thread_id = id")  # two threads: only the number joins them
    fake = IntakeModel()
    fake.triage = {"Ordine confermato": "delivery", "Ordine pronto": "delivery"}
    fake.extract["Ordine confermato"] = {"tasks": [task("Ritirare l'ordine", priority=2)]}
    fake.extract["Ordine pronto"] = {"tasks": [task("Ritirare il pacco Zalando", priority=2,
                                                    due_at="2026-11-06", due_kind="date")]}  # fmt: skip
    intake(db, secrets_key, fake)
    rows = tasks(db)
    assert [r[0] for r in rows] == ["Ritirare l'ordine"] and rows[0][1] is not None  # one task, its new date
    assert db.execute("SELECT count(*) FROM things").fetchone() == (1,)
    linked = db.execute(
        "SELECT item_kind, count(*) FROM thing_items GROUP BY item_kind ORDER BY 1"
    ).fetchall()
    assert linked == [("email", 2), ("task", 1)]


def test_pisa_reads_a_thing_whole(db) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pisa.brain.task_tools import add_task_control_tools
    from pisa.services import things
    from pisa.services.tasks import TasksService

    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "intake_gist) "
        "VALUES ('z1', 't', 'Zalando', 'z@x.example', 'Spedito', '2026-10-28 10:00+01', ARRAY['INBOX'], "
        '\'{"what": "Il tuo ordine è stato spedito", "when": "venerdì"}\')'
    )

    async def go() -> str:
        async with _attend(db, T0) as env:
            async with env.pool.connection() as conn:
                key = [{"kind": "order", "value": "ZX-88812"}]
                await things.link_email(conn, message_id="z1", thread_id="t", subject="Ordine Zalando",
                                        keys=key,
                                        gist=None, now=T0)  # fmt: skip
            tasks = TasksService(env.pool, owner=owner_at(), clock=env.clock)
            data = assistant_data(env.pool, tasks, clock=env.clock)
            agent: Agent[None, str] = Agent(TestModel())
            add_task_control_tools(agent, data, gate=None, lang=lambda: "it-IT")
            tool: Any = agent._function_toolset.tools["thing"].function
            return (
                f"{await tool(ref='zx-88812')}\n{await tool(ref='ordine zalando')}\n{await tool(ref='volo')}"
            )

    said = asyncio.run(go())
    assert said.count("Il tuo ordine è stato spedito (when: venerdì)") == 2  # by its number, and by its words
    assert said.rstrip().endswith("No thing matches that: try the task or the email.")
    assert "DATA" in said  # the emails' words go as data


# --- 5. Smaller turns: the task list in summary, the trim's own lists last ----------------------------------


def test_a_long_task_list_comes_as_a_summary_and_a_group_a_page_at_a_time(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from datetime import datetime

    from tests.test_conversation_tools import NOW, add_task, ask, data_of

    for n in range(3):
        add_task(db, f"Scaduto numero {n}", NOW - timedelta(days=1 + n), "date")
    add_task(db, "Oggi da fare", NOW + timedelta(hours=2), "datetime")
    for n in range(12):
        add_task(db, f"Senza data numero {n}")
    add_task(db, "Lontano nel tempo", datetime(2026, 12, 1, 9, tzinfo=NOW.tzinfo), "date")
    summary = data_of(ask(db, secrets_key, "get_tasks", {}))
    assert summary.startswith("17 open tasks. By group: 3 overdue, 1 today, 1 later, 12 undated.")
    assert "Scaduto numero 2" in summary and "Senza data" not in summary  # the most urgent few, not the list
    assert summary.count(";") == 4  # five named
    overdue = data_of(ask(db, secrets_key, "get_tasks", {"group": "overdue"}))
    assert overdue.startswith("3 open tasks (overdue): ") and "and" not in overdue.split(":", 1)[1]
    undated = data_of(ask(db, secrets_key, "get_tasks", {"group": "undated"}))
    assert undated.endswith("; and 4 more (skip=8).")
    rest = data_of(ask(db, secrets_key, "get_tasks", {"group": "undated", "skip": 8}))
    assert rest.count("Senza data") == 4 and "more" not in rest


def test_over_budget_the_trim_cuts_outside_results_first_then_pisa_s_own_lists() -> None:
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart

    from pisa.brain.history import BUDGET_TOKENS, tokens, trim
    from pisa.brain.untrusted import data_block

    def wrapped(kind: str, text: str) -> str:
        block = data_block(kind, text, what=kind)
        return f"{block.rule}\n{block.text}"

    rounds = [
        [("get_tasks", wrapped("tasks", "Pagare la bolletta; " * 3000)),  # ~60k chars of Pisa's own list
         ("read_page", wrapped("page", "una pagina " * 400))],  # ~4.4k chars from outside
        [("get_current_time", "Friday 2026-10-31 09:00")],  # the latest round
    ]  # fmt: skip
    messages: list[Any] = [ModelRequest(parts=[UserPromptPart(content="che compiti ho?")])]
    for n, results in enumerate(rounds):
        calls = [ToolCallPart(tool, {}, tool_call_id=f"c{n}{i}") for i, (tool, _) in enumerate(results)]
        messages += [
            ModelResponse(parts=calls),
            ModelRequest(parts=[ToolReturnPart(t, text, tool_call_id=f"c{n}{i}")
                                for i, (t, text) in enumerate(results)]),
        ]  # fmt: skip
    assert tokens(messages) > BUDGET_TOKENS
    kept = trim(messages)
    assert tokens(kept) <= BUDGET_TOKENS
    out = {p.tool_call_id: str(p.content) for m in kept for p in m.parts if isinstance(p, ToolReturnPart)}
    assert "trimmed" in out["c01"]  # the outside page went first
    assert "trimmed" in out["c00"]  # still over: Pisa's own list too, rather than a 52k-token call
    assert out["c00"].count("<tasks-") == out["c00"].count("</tasks-")  # closed again


# --- 6. The evals on their own budget; the guard said, never denied ---------------------------------------


def test_an_eval_spends_its_own_budget_never_the_owner_s(db) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime
    from decimal import Decimal

    import pytest

    from pisa.domain.errors import PisaError
    from pisa.infra import ledger, spend
    from tests.conftest import dsn

    now = datetime(2026, 10, 29, 10, tzinfo=UTC)
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, 0.98, true, "
        "'t', %s)",
        (now,),
    )  # the user's day: €0.98 of €1.00

    async def go() -> list[object]:
        import psycopg

        out: list[object] = []
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:

            async def reserve(eur: str) -> object:
                try:
                    return await ledger.reserve(
                        conn, slot="chat.conversation", model_ref="openai:x", estimate_eur=Decimal(eur),
                        price_known=True, trace_id="e", now=now, tz="Europe/Rome", hard=hard_limit.DEFAULT,
                    )  # fmt: skip
                except PisaError as err:
                    return err.message

            with ledger.eval_budget():
                out.append(await reserve("0.30"))  # the user's cap is nearly spent: the eval's is not
                out.append(await reserve("0.30"))  # €0.60 would pass the eval's €0.50
            out.append(await reserve("0.05"))  # the user's own call: their cap, untouched by the eval
            day = spend.period_starts(now, "Europe/Rome")[0]
            out.append(await ledger.spent_since(conn, day))
            out.append((await ledger.spend_summary(conn, now, "Europe/Rome")).today_eur)
        return out

    import asyncio

    first, second, owner, spent, summary = asyncio.run(go())
    assert not isinstance(first, str)
    assert isinstance(second, str) and "eval cap" in second and "user's budget is untouched" in second
    assert isinstance(owner, str) and owner.startswith("Daily AI spend cap reached: €0.98")
    assert spent == summary == Decimal("0.98")  # the user's spend, the eval's €0.30 not in it
    rows = db.execute("SELECT budget, count(*) FROM llm_calls GROUP BY 1 ORDER BY 1").fetchall()
    assert rows == [("eval", 1), ("owner", 1)]
    with pytest.raises(AssertionError):
        assert ledger.budget() == "eval"  # outside the block: the user's again


def test_the_guard_is_a_line_pisa_says_plainly_and_an_eval_never_latches_it(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models, clock
) -> None:
    from typing import cast

    from pisa.services.conversation import GUARD_LINE
    from tests.conftest import seed_catalog
    from tests.test_acting import said, socket_turns

    if db.execute("SELECT count(*) FROM model_catalog WHERE ref = 'openai:cheap-model'").fetchone() == (0,):
        seed_catalog(db, [{"ref": "openai:cheap-model", "input": 0.1, "output": 0.2}])
    db.execute("UPDATE model_slots SET fallback_ref = 'openai:cheap-model' WHERE id = 'chat.conversation'")
    seen: list[str] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        seen.append(info.instructions or "")
        yield "Ok."

    fake_models.default = model
    fake_models.replies["cheap-model"] = model
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at, budget) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, 0.45, "
        "true, 't', %s, 'eval')",
        (clock.now,),
    )  # the evals spent €0.45 today: not the user's
    socket_turns(client, db, secrets_key, cast(Any, None), "come va?")
    assert "Budget guard" not in seen[-1]  # the user's day is untouched: the usual model, no line
    assert db.execute("SELECT 1 FROM app_settings WHERE key = 'budget_guard_said'").fetchone() is None
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, 0.71, true, "
        "'t', %s)",
        (clock.now,),
    )
    first, second = socket_turns(client, db, secrets_key, cast(Any, None), "perché parli così?", "e poi?")
    line = GUARD_LINE.format(period="day's", period_it="del giorno", share=50)
    assert (
        line.strip() in seen[-2] and line.strip() in seen[-1]
    )  # every turn while it holds, not the first only
    assert "abbiamo superato il 50% del budget del giorno" in seen[-1] and "never deny it" in seen[-1]
    assert said(first).strip().endswith("Per oggi uso il modello più economico.")
    assert said(second).strip() == "Ok."


def test_a_fresh_start_is_not_a_contradiction() -> None:
    from pisa.domain import talk
    from pisa_tools.talk_eval import CASES

    case = next(c for c in CASES if c.name == "fresh-start")
    assert case.check == "fresh" and case.words == "non serve mantenere le cose vecchie"
    assert talk.violations("Ok, ripartiamo da zero.", "it-IT", check="fresh") == []
    assert talk.violations("Ma prima mi avevi chiesto di tenerle.", "it-IT", check="fresh") == [
        "pointed out a change of mind"
    ]
    assert talk.violations("Sei sicuro? Le tengo comunque.", "it-IT", check="fresh") == [
        "pointed out a change of mind"
    ]


# --- 7. The reads IOS needs: options by id, a draft and an email by ref, a day's agenda ---------------------


def test_the_phone_reads_options_by_id_while_they_last(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair
    from tests.places_fake import FakePlaces
    from tests.test_plan_options import NOW
    from tests.test_plan_options import make_trip_day as live

    lunch = live(db)["lunch"]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    made = client.post("/v1/plan/options", headers=auth, json={"target": {"event_id": lunch}}).json()
    read = client.get(f"/v1/plan/options/{made['options_id']}", headers=auth)
    assert read.status_code == 200 and read.json() == made  # as they were made (contract N)
    clock.now = NOW + timedelta(minutes=16)
    gone = client.get(f"/v1/plan/options/{made['options_id']}", headers=auth)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "options_expired"
    bad = client.get("/v1/plan/options/not-an-id", headers=auth)
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "options_expired"


def test_the_phone_reads_a_draft_and_an_email_by_ref_never_a_body_or_spam(client, db) -> None:  # type: ignore[no-untyped-def]
    import uuid
    from datetime import UTC, datetime

    from tests.conftest import pair

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    at = datetime(2026, 10, 29, 9, tzinfo=UTC)
    rows = (("m-quiet", ["INBOX"], "notification"), ("m-spam", ["SPAM"], "notification"),
            ("m-task", ["INBOX"], "action"))  # fmt: skip
    for mid, labels, category in rows:
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
            "snippet, body, intake_status, intake_category, intake_reason, intake_gist) VALUES (%s, %s, "
            "'Anna', 'anna@example.com', 'Il pacco', %s, %s, 's', 'IL CORPO SEGRETO', 'done', %s, "
            "'Solo un avviso.', "
            '\'{"what": "Avviso di consegna"}\')',
            (mid, f"t-{mid}", at, labels, category),
        )
    task = str(uuid.uuid4())
    db.execute(
        "INSERT INTO tasks (id, title, title_key, source_kind, source_ref, created_at, updated_at) "
        "VALUES (%s, 'Ritirare il pacco', 'ritirare pacco', 'email', 'm-task', %s, %s)",
        (task, at, at),
    )
    quiet = client.get("/v1/emails/m-quiet", headers=auth).json()
    assert quiet | {"why": "x", "kind": "x"} == {
        "id": "m-quiet", "subject": "Il pacco", "sender": "Anna", "sender_address": "anna@example.com",
        "sent_at": "2026-10-29T10:00:00+01:00", "gist": "Avviso di consegna", "task_id": None, "kind": "x",
        "why": "x", "detail": "Solo un avviso.",
    }  # fmt: skip
    assert quiet["why"] and "CORPO" not in str(quiet)  # never the body
    tasked = client.get("/v1/emails/m-task", headers=auth).json()
    assert (tasked["task_id"], tasked["why"], tasked["kind"]) == (task, None, None)
    assert client.get("/v1/emails/m-spam", headers=auth).status_code == 404  # spam is never read
    assert client.get("/v1/emails/nope", headers=auth).status_code == 404
    draft = uuid.uuid4()
    db.execute(
        "INSERT INTO email_drafts (id, gmail_draft_id, thread_id, reply_to_id, in_reply_to, to_addresses, "
        "outsiders, subject, body, created_at, updated_at) VALUES (%s, 'g1', 't', 'm-task', '<x>', "
        "ARRAY['anna@example.com', 'nino@example.com'], ARRAY['nino@example.com'], 'Re: Il pacco', "
        "'Ciao Anna, passo domani.', %s, %s)",
        (draft, at, at),
    )
    read = client.get(f"/v1/drafts/{draft}", headers=auth).json()
    assert read == {
        "id": str(draft), "subject": "Re: Il pacco", "to": ["anna@example.com", "nino@example.com"], "cc": [],
        "outsiders": ["nino@example.com"], "body": "Ciao Anna, passo domani.", "version": 1,
        "updated_at": "2026-10-29T10:00:00+01:00", "sent_at": None, "approval_id": None,
    }  # fmt: skip
    assert client.get(f"/v1/drafts/{uuid.uuid4()}", headers=auth).status_code == 404
    assert client.get("/v1/drafts/x", headers=auth).status_code == 404


def test_a_day_without_a_stored_plan_shows_its_agenda(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    import json

    from pisa.services.session_messages import Present
    from tests.test_interface_tools import _calls, _phone, _returned

    fake_models.default = _calls([("show_plan", {"day": "2026-11-03", "purpose": "il martedì"})], "Eccolo.")
    _, (frames,) = _phone(client, db, secrets_key, ["cosa ho martedì?"])
    (shown,) = [f for f in frames if f["type"] == "present"]
    # Not sent until the phone decodes it; the phone shows the agenda itself (no stored plan)
    assert shown["object"]["ref"] == {"date": "2026-11-03", "options_id": None}
    Present.model_validate_json(
        json.dumps(shown)
    )  # the protocol's own model accepts it (the schema: fixture)
    assert "its agenda is shown" in _returned(fake_models.default.seen[-1])
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (gen_random_uuid(), "
        "'2026-11-03', now(), 'ask', "
        "'f', '{}')"
    )
    fake_models.default = _calls([("show_plan", {"day": "2026-11-03", "purpose": "il martedì"})], "Eccolo.")
    _, (frames,) = _phone(client, db, secrets_key, ["e adesso?"])
    (shown,) = [f for f in frames if f["type"] == "present"]
    assert shown["object"]["ref"] == {"date": "2026-11-03", "options_id": None}  # a stored plan: the plan
