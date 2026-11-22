"""Re-planning on request (contract N): a trip day running late, similar places on the way, meal windows,
the rest of the day, applying an option, adjustments and undo, plan_changed and expiry."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, add_task, rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.places_env import at_location, place_env
from tests.places_fake import FakePlaces
from tests.test_planner import place, with_place

TZ = "Europe/Rome"  # Amsterdam keeps the same clock
HOSTEL = (52.339, 4.876)  # the hostel, where the user is at 14:10
VISBAR = (52.358, 4.881)  # lunch, 13:30-14:15
SPIRAAL = (52.352, 4.868)  # the museum, 14:40
NOW = rome(2026, 10, 21, 14, 10)


@pytest.fixture
def trip_day(db: psycopg.Connection) -> dict[str, str]:
    return make_trip_day(db)


def make_trip_day(db: psycopg.Connection) -> dict[str, str]:
    """A trip day running late: at 14:10 near Hostel Fietsbel Amsterdam, lunch at Visbar Zilverschub
    13:30-14:15 not reached, Spiraal at 14:40."""
    set_settings(
        db, {"language": "it-IT", "places": {"default_mode": "transit"}, "planner": {"buffer_min": 10}}
    )
    lunch = add_event(db, "Pranzo: Visbar Zilverschub", rome(2026, 10, 21, 13, 30), minutes=45)
    museum = add_event(db, "Spiraal", rome(2026, 10, 21, 14, 40), minutes=80)
    with_place(db, "calendar_events", lunch, place(db, "Visbar Zilverschub", VISBAR))
    with_place(db, "calendar_events", museum, place(db, "Spiraal", SPIRAAL))
    at_location(db, HOSTEL, NOW - timedelta(minutes=1))
    return {"lunch": lunch, "spiraal": museum}


def options(
    db: psycopg.Connection,
    secrets_key: str,
    target: dict[str, str] | None,
    goal: str = "fit",
    *,
    now: datetime = NOW,
    fake: FakePlaces | None = None,
    keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        async with place_env(dsn(db), secrets_key, fake or FakePlaces(), Clock(now), keys=keys) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock)
            service = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(TZ), clock=env.clock)
            return await service.options(target, goal)  # type: ignore[arg-type]

    return asyncio.run(go())


def test_the_trip_day_late_for_lunch(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    """No time for lunch before Spiraal: skipping it keeps Spiraal on time and ranks first; lunch now makes
    Spiraal 50 minutes late; after Spiraal is past the lunch window (15:30). Everything is said in Italian."""
    got = options(db, secrets_key, {"event_id": trip_day["lunch"]})
    assert got["target_title"] == "Pranzo: Visbar Zilverschub"
    assert datetime.fromisoformat(got["expires_at"]) == NOW + timedelta(minutes=15)
    first, second = got["options"]
    assert first["summary"] == "Salta Pranzo: Visbar Zilverschub"
    assert first["effects"] == {
        "late_min": 0,
        "extra_travel_min": -13,
        "dropped": ["Pranzo: Visbar Zilverschub"],
    }
    assert first["why"].startswith("Arrivi a Spiraal alle 14:40 in orario")
    assert first["changes"] == [{"action": "skip", "event_id": trip_day["lunch"]}]
    assert second["summary"] == "Adesso da Visbar Zilverschub"
    assert (
        second["effects"]["late_min"] == 50
        and second["why"] == "Arrivi a Spiraal alle 15:30, 50 min in ritardo."
    )
    (move,) = second["changes"]
    assert (move["start"], move["end"]) == ("2026-10-21T14:35:00+02:00", "2026-10-21T15:20:00+02:00")
    lunch = next(i for i in second["plan"]["items"] if i.get("event_id") == trip_day["lunch"])
    assert lunch["adjusted"] is True and lunch["start"] == move["start"]


def apple_place(name: str, where: tuple[float, float], category: str) -> dict[str, Any]:
    return {
        "id": f"I{abs(hash(name)) % 10**12:012d}",
        "name": name,
        "coordinate": {"latitude": where[0], "longitude": where[1]},
        "formattedAddressLines": [f"{name} 1", "Amsterdam"],
        "poiCategory": category,
    }


def test_a_similar_place_on_the_way(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    """A restaurant on the way from the hostel to Spiraal is offered; a café 3 km off the way and a museum are
    not. Apple gives no hours, so the place counts as open."""
    fake = FakePlaces()
    on_the_way = (52.344, 4.867)  # between the hostel and Spiraal
    fake.apple_search = {
        "results": [
            apple_place("Toko Sambalaya", on_the_way, "Restaurant"),
            apple_place("Café Far Away", (52.393, 4.856), "Cafe"),
            apple_place("Prentenmuseum", (52.351, 4.855), "Museum"),
        ]
    }
    got = options(db, secrets_key, {"event_id": trip_day["lunch"]}, "alternative", fake=fake, keys=("apple",))
    summaries = [o["summary"] for o in got["options"]]
    assert set(summaries) == {"Adesso da Toko Sambalaya", "Salta Pranzo: Visbar Zilverschub"}
    there = next(o for o in got["options"] if o["summary"] == "Adesso da Toko Sambalaya")
    assert [c["action"] for c in there["changes"]] == ["move", "replace_place"]
    alt = db.execute("SELECT name FROM places WHERE id = %s", (there["changes"][1]["place_id"],)).fetchone()
    assert alt == ("Toko Sambalaya",)
    assert not any("Far Away" in s or "Prentenmuseum" in s for s in summaries)


def test_meals_stay_in_their_window(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """At 15:10, lunch now would have 20 minutes before the window closes at 15:30: not offered. Shortening
    needs 30 minutes too. Only skip is left."""
    lunch = add_event(db, "Pranzo", rome(2026, 10, 21, 13, 0), minutes=60)
    got = options(db, secrets_key, {"event_id": lunch}, now=rome(2026, 10, 21, 15, 10))
    assert [o["summary"] for o in got["options"]] == ["Salta Pranzo"]


def test_the_rest_of_the_day(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    """No target: the day as it is from here, or skipping one of the next events."""
    got = options(db, secrets_key, None, "reorganize")
    assert got["target_title"] == "Il resto della giornata"
    summaries = [o["summary"] for o in got["options"]]
    assert "Salta Pranzo: Visbar Zilverschub" in summaries and len(summaries) <= 3


# --- over REST: apply, adjustments, undo, plan_changed, expiry --------------------------------------------


def test_apply_adjust_and_undo(client, db, clock, http_handler, trip_day) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    got = client.post("/v1/plan/options", headers=auth, json={"target": {"event_id": trip_day["lunch"]}})
    assert got.status_code == 200
    clock.now = NOW + timedelta(seconds=30)  # each plan after the last
    now_option = next(o for o in got.json()["options"] if o["summary"].startswith("Adesso"))
    applied = client.post(
        f"/v1/plan/options/{got.json()['options_id']}/apply",
        headers=auth,
        json={"option_id": now_option["id"]},
    )
    assert applied.status_code == 200
    lunch = next(i for i in applied.json()["items"] if i.get("event_id") == trip_day["lunch"])
    assert (lunch["start"], lunch["adjusted"]) == ("2026-10-21T14:35:00+02:00", True)
    listed = client.get("/v1/plan/adjustments", headers=auth, params={"date": "2026-10-21"}).json()[
        "adjustments"
    ]
    (adjustment,) = listed
    assert adjustment | {"id": "x", "created_at": "x", "option_ref": "x"} == {
        "id": "x", "date": "2026-10-21", "action": "move", "event_id": trip_day["lunch"],
        "title": "Pranzo: Visbar Zilverschub", "start": "2026-10-21T14:35:00+02:00",
        "end": "2026-10-21T15:20:00+02:00", "place_id": None, "duration_min": None, "created_at": "x",
        "source": "app", "option_ref": "x",
    }  # fmt: skip
    again = client.post(
        f"/v1/plan/options/{got.json()['options_id']}/apply", headers=auth, json={"option_id": "o1"}
    )
    assert again.status_code == 404 and again.json()["error"]["code"] == "options_expired"  # applied: used up
    clock.now = NOW + timedelta(minutes=1)
    assert client.delete(f"/v1/plan/adjustments/{adjustment['id']}", headers=auth).status_code == 204
    plan = client.get("/v1/plan", headers=auth).json()
    lunch = next(i for i in plan["items"] if i.get("event_id") == trip_day["lunch"])
    assert lunch["start"] == "2026-10-21T13:30:00+02:00" and "adjusted" not in lunch
    assert client.delete(f"/v1/plan/adjustments/{adjustment['id']}", headers=auth).status_code == 404


def test_a_change_to_the_day_makes_options_stale(client, db, clock, http_handler, trip_day) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    got = client.post(
        "/v1/plan/options", headers=auth, json={"target": {"event_id": trip_day["lunch"]}}
    ).json()
    add_task(db, "Comprare un regalo", rome(2026, 10, 21).date())  # a new task: the day changed
    stale = client.post(f"/v1/plan/options/{got['options_id']}/apply", headers=auth, json={"option_id": "o1"})
    assert stale.status_code == 409 and stale.json()["error"] == {
        "code": "plan_changed",
        "message": "The day changed since these options were made (an event, a task or an adjustment).",
        "action": "Ask for options again",
    }


def test_options_expire_after_15_minutes(client, db, clock, http_handler, trip_day) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    got = client.post(
        "/v1/plan/options", headers=auth, json={"target": {"event_id": trip_day["lunch"]}}
    ).json()
    clock.now = NOW + timedelta(minutes=15)
    late = client.post(f"/v1/plan/options/{got['options_id']}/apply", headers=auth, json={"option_id": "o1"})
    assert late.status_code == 404 and late.json()["error"]["code"] == "options_expired"
    unknown = client.post("/v1/plan/options/nope/apply", headers=auth, json={"option_id": "o1"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "options_expired"


def test_options_request_validation(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    both = {"target": {"event_id": "e", "task_id": "t"}}
    assert client.post("/v1/plan/options", headers=auth, json=both).status_code == 400
    assert client.post("/v1/plan/options", headers=auth, json={"goal": "teleport"}).status_code == 400
    missing = client.post("/v1/plan/options", headers=auth, json={"target": {"event_id": "nope"}})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_a_skipped_item_stays_on_the_day_without_a_trip(client, db, clock, http_handler, trip_day) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    got = client.post(
        "/v1/plan/options", headers=auth, json={"target": {"event_id": trip_day["lunch"]}}
    ).json()
    skip = next(o for o in got["options"] if o["summary"].startswith("Salta"))
    plan = client.post(
        f"/v1/plan/options/{got['options_id']}/apply", headers=auth, json={"option_id": skip["id"]}
    ).json()
    lunch = next(i for i in plan["items"] if i.get("event_id") == trip_day["lunch"])
    assert lunch["status"] == "skipped" and lunch["start"] == "2026-10-21T13:30:00+02:00"
    visbar = lunch["place_id"]
    assert not any(i["kind"] == "travel" and i["to_place_id"] == visbar for i in plan["items"])


def test_option_variants_make_no_rain_pass(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Walking in the rain (13:00-15:59), the base plan's rain pass is bounded by what's
    left of the 2 s, and no variant makes one of its own (no rain hours: the base's matrix and modes)."""
    day = make_trip_day(db)
    set_settings(db, {"places": {"default_mode": "walk"}})
    calls: list[tuple[int, float | None]] = []
    solve_day = PlannerService.solve_day

    async def spy(self, d, events, tasks, limit, rain_budget_s=None):  # type: ignore[no-untyped-def]
        calls.append((len(d.rain), rain_budget_s))
        return await solve_day(self, d, events, tasks, limit, rain_budget_s)

    monkeypatch.setattr(PlannerService, "solve_day", spy)

    async def go() -> None:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(NOW), keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(
                env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock, weather=env.weather
            )
            service = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(TZ), clock=env.clock)
            await service.options({"event_id": day["lunch"]})

    asyncio.run(go())
    base, again, *variants = calls
    assert base[0] > 0 and base[1] is not None and 0 <= base[1] <= 2.0  # the base: rain, within the budget
    # Its re-solve with transit times: what is left of the budget, no rain pass.
    assert again[0] == 0 and again[1] is not None and 0 <= again[1] <= base[1]
    # The variants: no rain pass, and no wait for a long walk's transit time (budget 0: an estimate).
    assert variants and all(rain == 0 and budget == 0 for rain, budget in variants)
