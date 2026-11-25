"""A trip day with a festival: the HOLLOW BELL festival at Veenmond (12:00-23:00), lunch at BOBBY'S on
the way, the user's default mode walk. "Plan it" put the lunch in "not fit for today" and
planned a 194-minute walk; now the festival is a window, the lunch goes first on the way, the long leg goes by
transit (the ferry), and the festival's arrival moves. Plus the event kinds, the user's override, the
allowed modes and the walking limit, options when something fixed is in the way, and after / instead."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.places_env import at_location, place_env
from tests.places_fake import FakePlaces
from tests.test_planner import place, with_place

# The user asks Pisa to plan every task (Pisa never times one by itself)
pytestmark = pytest.mark.usefixtures("owner_plans_every_task")

TZ = "Europe/Rome"  # Amsterdam keeps the same clock
HERE_AMS = (52.359, 4.889)  # where the user is at 11:30
BOBBYS = (52.363, 4.888)  # BOBBY'S Bitterballen, near the ferry: on the way
VEENMOND = (52.439, 4.900)  # the festival: ~8.6 km, about 110 minutes on foot, ~30 by transit
NOW = rome(2026, 10, 24, 11, 30)


@pytest.fixture
def festival(db: psycopg.Connection) -> dict[str, str]:
    set_settings(db, {"language": "it-IT", "places": {"default_mode": "walk"}, "planner": {"buffer_min": 5}})
    event = add_event(db, "Golfslag · HOLLOW BELL FESTIVAL", rome(2026, 10, 24, 12), minutes=11 * 60)
    with_place(db, "calendar_events", event, place(db, "HOLLOW BELL", VEENMOND))
    at_location(db, HERE_AMS, NOW - timedelta(minutes=1))
    return {"festival": event, "bobbys": place(db, "BOBBY'S Bitterballen", BOBBYS)}


def service(db: psycopg.Connection, secrets_key: str, action: Any, now: datetime = NOW) -> Any:
    async def go() -> Any:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now)) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock)
            options = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(TZ), clock=env.clock)
            return await action(planner, options)

    return asyncio.run(go())


def items(plan: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [i for i in plan["items"] if i["kind"] == kind]


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_the_festival_day_lunch_on_the_way_then_the_festival(db, secrets_key, festival) -> None:  # type: ignore[no-untyped-def]
    plan = service(db, secrets_key, lambda p, o: o.insert(
        task_id=None, place_id=festival["bobbys"], when="before", relative_to=festival["festival"],
        duration_min=45,
    ))  # fmt: skip
    assert "options_id" not in plan  # it fits: the plan, not options
    (lunch,) = items(plan, "task")
    (fest,) = items(plan, "event")
    assert lunch["title"] == "BOBBY'S Bitterballen"
    assert at(lunch["start"]) >= NOW and at(lunch["end"]) - at(lunch["start"]) == timedelta(minutes=45)
    # The festival is a window: its arrival moves after the lunch and the trip; not late.
    assert (
        fest["event_kind"] == "window" and fest["kind_source"] == "inferred" and fest["min_stay_min"] == 120
    )
    assert at(fest["arrive_at"]) > rome(2026, 10, 24, 12) and at(fest["arrive_at"]) >= at(lunch["end"])
    assert "why" not in fest  # never "N min late"
    # On the way: here → BOBBY'S (a few minutes on foot) → Veenmond by transit (the ferry), never 110' afoot.
    trips = sorted(items(plan, "travel"), key=lambda i: i["start"])
    to_fest = next(t for t in trips if t["to_place_id"] == fest["place_id"])
    assert to_fest["mode"] == "transit" and to_fest["minutes"] < 45
    assert at(to_fest["start"]) >= at(lunch["end"]) and at(to_fest["end"]) == at(fest["arrive_at"])
    assert not any(t["mode"] == "walk" and t["minutes"] > 45 for t in trips)
    assert not plan["unplanned"]


def test_a_fixed_event_in_the_way_gives_options(db, secrets_key, festival) -> None:  # type: ignore[no-untyped-def]
    """The user says the festival is fixed (a timed entry at 12:00): lunch can't go first without being late,
    so options in the shape: lunch now (late), after it, or instead of it."""
    service(db, secrets_key, lambda p, o: p.set_kind(festival["festival"], "fixed", None))
    got = service(db, secrets_key, lambda p, o: o.insert(
        task_id=None, place_id=festival["bobbys"], when="before", relative_to=festival["festival"],
        duration_min=45,
    ))  # fmt: skip
    assert got["options_id"] and got["target_title"] == "BOBBY'S Bitterballen"
    summaries = [o["summary"] for o in got["options"]]
    assert any(s.startswith("Salta Golfslag · HOLLOW BELL FESTIVAL") for s in summaries)
    assert any(s.startswith("Adesso") or s.startswith("Dopo") for s in summaries)
    late = [o for o in got["options"] if o["summary"].startswith("Adesso")]
    assert not late or late[0]["effects"]["late_min"] > 0


def test_after_places_it_after_the_event(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"default_mode": "transit"}, "planner": {"buffer_min": 5}})
    meeting = add_event(db, "Riunione", rome(2026, 10, 24, 12), minutes=60)
    with_place(db, "calendar_events", meeting, place(db, "Ufficio", HERE_AMS))
    at_location(db, HERE_AMS, NOW - timedelta(minutes=1))
    shop = place(db, "BOBBY'S Bitterballen", BOBBYS)
    plan = service(db, secrets_key, lambda p, o: o.insert(
        task_id=None, place_id=shop, when="after", relative_to=meeting, duration_min=30,
    ))  # fmt: skip
    (task,) = items(plan, "task")
    assert at(task["start"]) >= rome(2026, 10, 24, 13)


def test_the_events_kinds_and_the_owners_override(db, secrets_key, festival) -> None:  # type: ignore[no-untyped-def]
    dinner = add_event(db, "Cena con amici", rome(2026, 10, 24, 20), minutes=180)  # long, but a dinner: fixed
    listed = service(db, secrets_key, lambda p, o: p.events(None, 1))
    kinds = {e["title"]: (e["kind"], e["kind_source"], e["min_stay_min"]) for e in listed["events"]}
    assert kinds["Golfslag · HOLLOW BELL FESTIVAL"] == ("window", "inferred", 120)
    assert kinds["Cena con amici"] == ("fixed", "inferred", None)
    changed = service(db, secrets_key, lambda p, o: p.set_kind(festival["festival"], "window", 240))
    assert (changed["kind"], changed["kind_source"], changed["min_stay_min"]) == ("window", "owner", 240)
    back = service(db, secrets_key, lambda p, o: p.set_kind(festival["festival"], None, None))
    assert back["kind_source"] == "inferred"
    from pisa.domain.errors import PisaError

    with pytest.raises(PisaError, match="window event only"):
        service(db, secrets_key, lambda p, o: p.set_kind(dinner, "fixed", 30))
    row = db.execute("SELECT title FROM calendar_events WHERE id = %s", (festival["festival"],)).fetchone()
    assert row == ("Golfslag · HOLLOW BELL FESTIVAL",)  # Google's data untouched: the kind is Pisa's own


def test_the_walking_limit_and_the_allowed_modes(db, secrets_key, festival) -> None:  # type: ignore[no-untyped-def]
    def trip_to_festival() -> dict[str, Any]:
        plan = service(db, secrets_key, lambda p, o: p.plan())
        return next(t for t in items(plan, "travel") if t["to_place_id"])

    assert trip_to_festival()["mode"] == "transit"  # 110' on foot > 45: the next allowed mode
    set_settings(db, {"places": {"walk_max_min": 150}})
    assert trip_to_festival()["mode"] == "walk"  # the user walks that far
    set_settings(db, {"places": {"modes": ["walk", "taxi"], "walk_max_min": 45}})
    taxi = trip_to_festival()
    assert taxi["mode"] == "taxi" and "taxi" in taxi["title"]
    set_settings(db, {"places": {"modes": ["walk"]}})
    assert trip_to_festival()["mode"] == "walk"  # walking is all the user allows


def test_settings_modes_and_default_mode_say_the_same(db) -> None:  # type: ignore[no-untyped-def]
    pair(db)
    assert set_settings(db, {"places": {"modes": ["car", "transit"]}})["places"]["default_mode"] == "drive"
    assert set_settings(db, {"places": {"default_mode": "walk"}})["places"]["modes"] == [
        "walk",
        "car",
        "transit",
    ]


def test_by_voice_lunch_before_the_festival(db, secrets_key, festival) -> None:  # type: ignore[no-untyped-def]
    """ "Pranzo da BOBBY'S prima del festival": plan_stop with the place found this turn, before the
    festival by its title."""
    from tests.test_follow_tools import ask

    said = ask(db, secrets_key, "plan_stop", {
        "when": "before", "relative_to": "festival", "place_id": festival["bobbys"], "minutes": 45,
    }, now=NOW)  # fmt: skip
    assert "Planned. The plan now:" in said and "BOBBY'S Bitterballen" in said
    stored = db.execute("SELECT action, source FROM plan_adjustments").fetchall()
    assert stored == [("move", "voice")]  # pinned in Pisa's plan, said by voice; the calendar is untouched


def test_instead_skips_the_event_and_takes_its_time(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"default_mode": "transit"}, "planner": {"buffer_min": 5}})
    meeting = add_event(db, "Riunione", rome(2026, 10, 24, 12), minutes=60)
    with_place(db, "calendar_events", meeting, place(db, "Ufficio", HERE_AMS))
    at_location(db, HERE_AMS, NOW - timedelta(minutes=1))
    shop = place(db, "BOBBY'S Bitterballen", BOBBYS)
    plan = service(db, secrets_key, lambda p, o: o.insert(
        task_id=None, place_id=shop, when="instead", relative_to=meeting, duration_min=30,
    ))  # fmt: skip
    (task,) = items(plan, "task")
    assert rome(2026, 10, 24, 12) <= at(task["start"]) and at(task["end"]) <= rome(2026, 10, 24, 13)
    skipped = [i for i in items(plan, "event") if i.get("status") == "skipped"]
    assert [i["title"] for i in skipped] == ["Riunione"]


def test_a_refused_stop_leaves_no_task_behind(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The 12 review, M3: "after" late at night has no event to follow; the place's task made for it goes."""
    set_settings(db, {"places": {"default_mode": "transit"}})
    at_location(db, HERE_AMS, NOW - timedelta(minutes=1))
    shop = place(db, "BOBBY'S Bitterballen", BOBBYS)
    from pisa.domain.errors import PisaError

    for _ in range(2):  # a retry adds nothing either
        with pytest.raises(PisaError):
            service(db, secrets_key, lambda p, o: o.insert(
                task_id=None, place_id=shop, when="after", relative_to=None, duration_min=30,
            ))  # fmt: skip
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
