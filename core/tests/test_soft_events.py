"""Pisa may move a soft event (one the user made in Pisa) within its day when a stop
needs the time, and says so; never a fixed event, nor a window event's end. The calendar keeps its time
."""

from datetime import timedelta
from typing import Any

import psycopg
import pytest

from tests.assistant_env import add_event, rome, set_settings
from tests.places_env import at_location
from tests.test_plan_insert import BOBBYS, HERE_AMS, NOW, at, items, service
from tests.test_planner import place, with_place

# The user asks Pisa to plan every task (Pisa never times one by itself)
pytestmark = pytest.mark.usefixtures("owner_plans_every_task")


def day(db: psycopg.Connection, kind: str) -> dict[str, str]:
    """11:30: a coffee with Marta at 12:00 (the user's kind for it), a meeting at 13:00, both near
    Centraal."""
    set_settings(db, {"language": "it-IT", "places": {"default_mode": "walk"}, "planner": {"buffer_min": 5}})
    coffee = add_event(db, "Caffè con Marta", rome(2026, 10, 24, 12), minutes=45)
    db.execute("UPDATE calendar_events SET kind_owner = %s WHERE id = %s", (kind, coffee))
    with_place(db, "calendar_events", coffee, place(db, "Bar Centraal", HERE_AMS))
    meeting = add_event(db, "Riunione", rome(2026, 10, 24, 13), minutes=60)
    with_place(db, "calendar_events", meeting, place(db, "Ufficio", HERE_AMS))
    at_location(db, HERE_AMS, NOW - timedelta(minutes=1))
    return {"coffee": coffee, "meeting": meeting, "bobbys": place(db, "BOBBY'S Bitterballen", BOBBYS)}


def lunch_before_the_meeting(db: psycopg.Connection, secrets_key: str, ids: dict[str, str]) -> dict[str, Any]:
    return service(db, secrets_key, lambda p, o: o.insert(
        task_id=None, place_id=ids["bobbys"], when="before", relative_to=ids["meeting"], duration_min=60,
    ))  # fmt: skip


def test_a_soft_event_moves_within_its_day_for_a_stop_and_says_so(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    ids = day(db, "soft")
    plan = lunch_before_the_meeting(db, secrets_key, ids)
    assert "options_id" not in plan
    (lunch,) = items(plan, "task")
    coffee = next(i for i in items(plan, "event") if i["title"] == "Caffè con Marta")
    meeting = next(i for i in items(plan, "event") if i["title"] == "Riunione")
    assert at(coffee["moved_from"]) == rome(2026, 10, 24, 12)
    assert at(coffee["start"]) != rome(2026, 10, 24, 12)
    assert at(coffee["end"]) - at(coffee["start"]) == timedelta(minutes=45)  # the same length
    assert at(coffee["start"]).date() == rome(2026, 10, 24, 12).date()  # the same day
    assert coffee["why"].startswith("Spostato dalle 12:00 perché ci stia la tappa")
    assert at(meeting["start"]) == rome(2026, 10, 24, 13) and "moved_from" not in meeting  # fixed: never
    assert at(lunch["end"]) <= rome(2026, 10, 24, 13)
    starts = db.execute("SELECT starts_at FROM calendar_events WHERE id = %s", (ids["coffee"],)).fetchone()
    assert starts == (rome(2026, 10, 24, 12),)  # Google's copy untouched: the move is Pisa's plan's


def test_a_soft_event_with_nothing_to_make_room_for_stays(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    day(db, "soft")
    plan = service(db, secrets_key, lambda p, o: p.plan())
    coffee = next(i for i in items(plan, "event") if i["title"] == "Caffè con Marta")
    assert at(coffee["start"]) == rome(2026, 10, 24, 12) and "moved_from" not in coffee


@pytest.mark.parametrize("kind", ["fixed", "window"])
def test_fixed_and_window_events_never_move(db, secrets_key, kind) -> None:  # type: ignore[no-untyped-def]
    ids = day(db, kind)
    got = lunch_before_the_meeting(db, secrets_key, ids)
    events = items(got, "event") if "items" in got else []
    assert not any("moved_from" in e for e in events)
    starts = {i["title"]: at(i["start"]) for i in events}
    assert starts.get("Riunione", rome(2026, 10, 24, 13)) == rome(2026, 10, 24, 13)
