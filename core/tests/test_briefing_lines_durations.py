"""Review fixes outside the planner itself: the briefing's first-task line and word cut,
durations, and the retention of travel cells and plans. The planner's own fixes are in
test_planner.py and test_planner_solver.py; N1 is in test_conversation_tools.py."""

import asyncio
import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pisa.brain import briefing as brain
from pisa.services import maintenance
from tests.assistant_env import rome
from tests.test_google_terms_and_etas import data

# --- N2 ---------------------------------------------------------------------------------------------------


def test_a_first_task_whose_leave_time_has_passed_says_now() -> None:
    task = brain.FirstTask("Ritirare il pacco", rome(2026, 10, 20, 9, 10), rome(2026, 10, 20, 8, 45))
    assert "Prima attività: Ritirare il pacco alle 09:10 (esci ora)." in brain.fallback(data(first_task=task))
    english = brain.fallback(data(lang="en-US", first_task=task))
    assert "First task: Ritirare il pacco at 09:10 (leave now)." in english
    _, facts = brain.prompt(data(first_task=task))
    assert "First task in today's plan: Ritirare il pacco at 09:10 (leave now)." in facts
    later = brain.FirstTask("Ritirare il pacco", rome(2026, 10, 20, 9, 30), rome(2026, 10, 20, 9, 5))
    assert "(esci alle 09:05)" in brain.fallback(data(first_task=later))


def test_a_first_sentence_over_the_limit_is_cut_to_it() -> None:
    title = " ".join(["Riunione"] * 30)
    text = brain.fallback(data(events=[brain.Event(rome(2026, 10, 20, 9), title, None, False)]))
    assert len(text.split()) == 20 and text.endswith("…")


# --- L6 ---------------------------------------------------------------------------------------------------


def test_durations_outside_5_to_480_are_refused(db: psycopg.Connection) -> None:
    """A task's duration is 5 to 480 minutes, or none."""
    insert = (
        "INSERT INTO tasks (id, title, due_kind, source_kind, title_key, duration_min, created_at, "
        "updated_at) VALUES (%s, %s, 'none', 'voice', %s, %s, now(), now())"
    )
    for title, minutes in (("Breve", 5), ("Lungo", 480), ("Giusto", 45), ("Ignoto", None)):
        db.execute(insert, (uuid.uuid4(), title, title.lower(), minutes))
    rows = dict(db.execute("SELECT title, duration_min FROM tasks").fetchall())
    assert rows == {"Breve": 5, "Lungo": 480, "Giusto": 45, "Ignoto": None}
    for minutes in (4, 481):
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(insert, (uuid.uuid4(), "Fuori", f"fuori{minutes}", minutes))


# --- L3 ---------------------------------------------------------------------------------------------------


def test_travel_cells_go_after_a_day_and_plans_are_kept(db: psycopg.Connection, app_settings) -> None:  # type: ignore[no-untyped-def]
    now = rome(2026, 10, 21, 3, 30)
    for i, age in enumerate((timedelta(hours=25), timedelta(hours=23))):
        db.execute(
            "INSERT INTO travel_cache (from_key, to_key, mode, slot, minutes, provider, created_at) "
            "VALUES (%s, 'b', 'transit', %s, 10, 'google', %s)",
            (f"a{i}", now - age, now - age),
        )
    for age in (timedelta(days=91), timedelta(days=89)):
        db.execute(
            "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (%s, %s, %s, 'x', "
            "'f', %s)",
            (uuid.uuid4(), (now - age).date(), now - age, Jsonb({})),
        )
    places = asyncio.run(maintenance.prune_places(app_settings, now))
    history = asyncio.run(maintenance.prune_history(app_settings, now))
    assert places["travel"] == 1 and "plans" not in history  # Their plans are kept
    assert db.execute("SELECT from_key FROM travel_cache").fetchall() == [("a1",)]
    assert db.execute("SELECT count(*) FROM plans").fetchone() == (2,)
