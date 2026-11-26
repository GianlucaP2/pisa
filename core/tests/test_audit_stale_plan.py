"""Release audit §10: today's plan read after their tasks changed.

The audit driver deletes each row's tasks and seeds the next row's a second before the turn. The stored plan
was made from the earlier tasks, and the minute's re-plan had not run yet:
- f4-*-2: `what_now` read the plan's to-do (the tasks with no time), which still named "Renew the car
  insurance" from row f2-*-3, and never the row's own electricity bill and plumber. The first fix checked only
  the timed items for a gone task, and nothing checked for a task added since.
- f1-en-*: the turn's context ("Today in Pisa's plan … Today's tasks with no time: «Chiamare la banca» …,
  you may suggest one of them") carried the previous rep's Italian tasks, and the model added the English
  request under that title.

A plan is now stored with the open tasks it was made from (each with its last change); a plan of today read
after one was added, closed, deleted or changed is made again first (`PlannerService.current`), and a reader
that cannot make one reads none (`plan_store.stored_current`). Synthetic tasks only.
"""

from datetime import date, timedelta
from typing import Any

import pytest

from pisa.brain.plan_tools import now_and_next
from pisa.services import plan_store
from tests.assistant_env import add_task
from tests.replay.scenarios import c

TODAY = date(2026, 10, 20)  # the suite's clock: 20 October, 13:00 in Rome
N = "2026-11-19T15:39"  # the replay's clock, as in the audit's run


def rt(client: Any) -> Any:
    return client.app.state.runtime


def titles(plan: dict[str, Any]) -> set[str]:
    return {
        str(i.get("title")) for i in [*plan.get("items", []), *(plan.get("todo") or [])] if i.get("title")
    }


def made(client: Any) -> dict[str, Any]:
    """Today's plan as the planner makes and stores it now."""
    return client.portal.call(lambda: rt(client).planner.plan(reason="test"))


def read(client: Any) -> dict[str, Any]:
    """Today's plan as every reader gets it (GET /v1/plan, `what_now`, `get_plan`, `mark_done`'s targets)."""
    return client.portal.call(lambda: rt(client).planner.get())


# --- the planner's own check ------------------------------------------------------------------------------


def test_a_task_added_after_the_plan_is_in_the_plan_read(client, db) -> None:  # type: ignore[no-untyped-def]
    add_task(db, "Pay the electricity bill", TODAY)
    assert "Pay the electricity bill" in titles(made(client))
    add_task(db, "Call the plumber", TODAY)
    assert {"Pay the electricity bill", "Call the plumber"} <= titles(read(client))


def test_a_to_do_task_deleted_after_the_plan_is_never_read(client, db) -> None:  # type: ignore[no-untyped-def]
    """f4-*-2: the gone task was in the to-do (no time), never in the timed items."""
    gone = add_task(db, "Renew the car insurance", TODAY)
    plan = made(client)
    assert "Renew the car insurance" in {t["title"] for t in plan.get("todo") or []}
    db.execute("DELETE FROM tasks WHERE id = %s", (gone,))
    add_task(db, "Pay the electricity bill", TODAY)
    now = read(client)
    assert "Renew the car insurance" not in titles(now) and "Pay the electricity bill" in titles(now)


@pytest.mark.parametrize("change", ["done", "dismissed", "renamed", "due later"])
def test_a_task_closed_or_changed_after_the_plan_makes_it_again(client, db, change) -> None:  # type: ignore[no-untyped-def]
    task = add_task(db, "Pay the electricity bill", TODAY)
    made(client)
    if change == "renamed":
        db.execute("UPDATE tasks SET title = 'Pay the gas bill', updated_at = now() WHERE id = %s", (task,))
    elif change == "due later":
        db.execute(
            "UPDATE tasks SET due_at = due_at + interval '9 days', updated_at = now() WHERE id = %s", (task,)
        )
    else:
        db.execute("UPDATE tasks SET status = %s, updated_at = now() WHERE id = %s", (change, task))
    now = read(client)
    if change != "due later":  # a task due in 9 days may still head the to-do: made again all the same
        assert "Pay the electricity bill" not in titles(now), now
    plans = db.execute("SELECT reason FROM plans ORDER BY generated_at DESC LIMIT 1").fetchone()
    assert plans == ("tasks changed since it was made",)


def test_the_plan_read_as_made_is_made_once(client, db) -> None:  # type: ignore[no-untyped-def]
    add_task(db, "Pay the electricity bill", TODAY)
    made(client)
    before = db.execute("SELECT count(*) FROM plans").fetchone()
    read(client)
    read(client)
    assert db.execute("SELECT count(*) FROM plans").fetchone() == before  # nothing changed: no new plan


def test_the_plan_keeps_the_open_tasks_it_was_made_from(client, db) -> None:  # type: ignore[no-untyped-def]
    one = add_task(db, "Pay the electricity bill", TODAY)
    add_task(db, "Buy milk", status="done")
    made(client)
    (kept,) = db.execute("SELECT open_tasks FROM plans ORDER BY generated_at DESC LIMIT 1").fetchone()
    assert set(kept) == {one}


def test_a_plan_stored_without_its_open_tasks_is_read_as_it_is(client, db) -> None:  # type: ignore[no-untyped-def]
    """Plans stored before the open tasks were kept (and a test's own) are never made again on read."""
    from psycopg.types.json import Jsonb

    add_task(db, "Pay the electricity bill", TODAY)
    kept = {"date": TODAY.isoformat(), "items": [], "todo": [{"title": "Kept as it is"}]}
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) "
        "VALUES (gen_random_uuid(), %s, now(), 'old', 'x', %s)",
        (TODAY, Jsonb(kept)),
    )
    assert titles(read(client)) == {"Kept as it is"}


def test_a_reader_that_cannot_make_a_plan_reads_none_of_gone_tasks(client, db) -> None:  # type: ignore[no-untyped-def]
    gone = add_task(db, "Renew the car insurance", TODAY)
    made(client)

    async def stored() -> Any:
        async with rt(client).pool.connection() as conn:
            return await plan_store.stored_current(conn, TODAY)

    assert stored_titles(client.portal.call(stored)) == {"Renew the car insurance"}
    db.execute("DELETE FROM tasks WHERE id = %s", (gone,))
    assert client.portal.call(stored) is None


def stored_titles(plan: dict[str, Any] | None) -> set[str]:
    return titles(plan or {})


# --- what the tools and the turn's context read ------------------------------------------------------------


def test_what_now_and_today_seen_read_the_tasks_open_now(client, db) -> None:  # type: ignore[no-untyped-def]
    """Seed, plan, delete, add, then at once `what_now` and the turn's context (`today_seen`)."""
    from zoneinfo import ZoneInfo

    data = rt(client).conversations.deps.tools.data
    gone = add_task(db, "Renew the car insurance", TODAY)
    made(client)
    db.execute("DELETE FROM tasks WHERE id = %s", (gone,))
    add_task(db, "Pay the electricity bill", TODAY)
    said = now_and_next(client.portal.call(data.plan), data.now(), ZoneInfo(data.timezone))
    seen = client.portal.call(data.today_seen) or ""
    for text in (said, seen):
        assert "car insurance" not in text and "electricity bill" in text, text


def test_mark_done_after_seed_delete_add_closes_the_new_task(client, db) -> None:  # type: ignore[no-untyped-def]
    """f2-en-1: mark_done finds the task in the store itself, never through the plan: a stale plan cannot
    leave it open (this held on 968316cf0 too; the run's open bill had another cause)."""
    data = rt(client).conversations.deps.tools.data
    gone = add_task(db, "Pay the rent", TODAY)
    made(client)
    db.execute("DELETE FROM tasks WHERE id = %s", (gone,))
    bill = add_task(db, "Pay the electricity bill", TODAY + timedelta(days=2))
    out = client.portal.call(lambda: data.mark_done("electricity bill"))
    assert [d["title"] for d in out["done"]] == ["Pay the electricity bill"]
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (bill,)).fetchone() == ("done",)


# --- the audit's rows, replayed with the scripted model ----------------------------------------------------

GONE_EN = [("Renew the car insurance", None, None), ("Wash the car", None, None)]
GONE_IT = [("Rinnovare l'assicurazione dell'auto", None, None), ("Lavare l'auto", None, None)]


def _scenario(lang: str, words: str, world: dict[str, Any], script: list[Any], sid: str) -> dict[str, Any]:
    return {
        "id": sid, "now": N, "lang": lang, "max_questions": 0, "world": world,
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip


def _turn(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          scenario: dict[str, Any]) -> Any:  # fmt: skip
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    return _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]


@pytest.mark.parametrize(
    ("lang", "words", "today", "gone"),
    [
        ("en-US", "What should I do now?", ["Pay the electricity bill", "Call the plumber"], GONE_EN),
        ("it-IT", "Cosa faccio adesso?", ["Pagare la bolletta della luce", "Chiamare l'idraulico"], GONE_IT),
    ],
    ids=["f4-en-2", "f4-it-2"],
)
def test_what_now_reads_the_row_s_own_tasks_not_the_to_do_of_gone_ones(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, words, today, gone
) -> None:
    right, wrong = f"Now: {today[0]}.", f"Now: {gone[0][0]}."
    step = {"seen": today[0], "then": right, "else": wrong}
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks_today": today, "plan_gone": gone}, [[c("what_now")], step],
                           "audit-what-now-todo"))  # fmt: skip
    assert turn.reply.strip().endswith(right), turn.reply


@pytest.mark.parametrize(
    ("words", "gone", "english"),
    [
        ("Add a task to renew my passport by next Friday", "Rinnovare il passaporto", "Renew passport"),
        ("Add 'call the bank' for tomorrow, it's urgent", "Chiamare la banca", "Call the bank"),
    ],
    ids=["f1-en-1", "f1-en-2"],
)
def test_an_english_request_never_sees_the_italian_to_do_of_gone_tasks(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, words, gone, english
) -> None:
    """Rep 3: the context's "Today's tasks with no time: «Chiamare la banca» …; you may suggest one of
    them" came from a plan of the previous rep's Italian tasks. A model that takes the title it sees there
    (the reply of f1-en-2 echoed that line: "It has no set time, so tell me when you want it and I'll put it
    in your plan") now sees none."""
    pool = [(gone, None, None), ("Restituire i libri in biblioteca", None, None)]
    add = {"hint": f"«{gone}»", "then": [c("add_task", title=gone)], "else": [c("add_task", title=english)]}
    scenario = _scenario("en-US", words, {"plan_gone": pool}, [add, "Added."], "audit-title-todo")
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert turn.calls and turn.calls[0][0] == "add_task", turn.calls
    assert [r[0] for r in db.execute("SELECT title FROM tasks WHERE status = 'open'").fetchall()] == [english]
