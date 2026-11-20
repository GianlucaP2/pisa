"""The day plan (contract M): inputs, the squeezed hour with real places and travel, why and unplanned,
no location, the travel cache, the re-plan triggers and the once-a-minute guard, "leave now" at most once,
the plan tools, and the briefing's first task."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from pisa.services.briefing import BriefingService
from pisa.services.planner import PlannerService
from pisa.services.reminders import RemindersService
from pisa.services.tasks import TasksService
from tests.assistant_env import (
    add_event,
    add_task,
    assistant_data,
    conversation,
    notifications,
    rome,
    set_settings,
)
from tests.conftest import Clock, FakeModels, dsn, pair
from tests.owner import owner_at
from tests.places_env import HERE_MILAN, PlaceEnv, at_location, place_env
from tests.places_fake import FakePlaces

# The user asks Pisa to plan every task (Pisa never times one by itself)
pytestmark = pytest.mark.usefixtures("owner_plans_every_task")

TZ = "Europe/Rome"
OFFICE = (45.4710, 9.0900)  # about 900 m north of here


def run(
    db: psycopg.Connection,
    secrets_key: str,
    now: datetime,
    action: Callable[[PlannerService, PlaceEnv], Awaitable[Any]],
    *,
    fake: FakePlaces | None = None,
    keys: tuple[str, ...] = ("apple", "google", "tavily"),
    models: FakeModels | None = None,
) -> Any:
    async def go() -> Any:
        async with place_env(
            dsn(db), secrets_key, fake or FakePlaces(), Clock(now), models, keys=keys
        ) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock)
            return await action(planner, env)

    return asyncio.run(go())


def place(db: psycopg.Connection, name: str, coord: tuple[float, float], google_id: str | None = None) -> str:
    pid = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, lat, lon, coords_source, coords_at, apple_id, google_place_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, 'apple', now(), %s, %s, now(), now())",
        (pid, name, coord[0], coord[1], f"I-{name}", google_id),
    )
    return str(pid)


def with_place(db: psycopg.Connection, table: str, row_id: str, place_id: str) -> None:
    query = sql.SQL("UPDATE {} SET place_id = %s WHERE id = %s").format(sql.Identifier(table))
    db.execute(query, (place_id, row_id))


def kinds(plan: dict[str, Any]) -> list[tuple[str, str]]:
    return [(i["kind"], i.get("title") or "") for i in plan["items"]]


def assert_ordered_and_apart(plan: dict[str, Any]) -> None:
    items = plan["items"]
    for a, b in pairwise(items):
        late_trip = a["kind"] == "travel" and b["kind"] == "event"  # a late trip overlaps its event's start
        if not late_trip:
            assert datetime.fromisoformat(a["end"]) <= datetime.fromisoformat(b["start"]), (a, b)


@pytest.fixture(autouse=True)
def _briefing_waits_for_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The briefing waits 3 s of real time for the plan (`PLACE_NOTES_S`); on a loaded CI runner the solver
    can take longer (CI run 529), and the tests that read the plan's content then find none. Here the wait is
    generous; the 3 s cut-off itself has its own test (`test_a_briefing_that_stops_waiting_…`, 0.01 s)."""
    from pisa.services import briefing as briefing_module

    monkeypatch.setattr(briefing_module, "PLACE_NOTES_S", 30.0)


@pytest.fixture
def squeezed_day(db: psycopg.Connection) -> dict[str, str]:
    """Tuesday 20 Oct: meetings at the office 09:00-10:00 and 11:00-13:20; the post office (open until 13:35)
    and a shop 150 m from it. The only free hour before the post office closes is 10:00-11:00."""
    office = place(db, "Ufficio", OFFICE)
    poste = place(db, "Poste Italiane", (45.467, 9.09), "ChIJposteItaliane0002")
    shop = place(db, "Cartoleria", (45.468, 9.09))  # no Google id: its hours are unknown, any time
    m1 = add_event(db, "Riunione", rome(2026, 10, 20, 9), minutes=60)
    m2 = add_event(db, "Workshop", rome(2026, 10, 20, 11), minutes=140)
    with_place(db, "calendar_events", m1, office)
    with_place(db, "calendar_events", m2, office)
    post = add_task(db, "Spedire il pacco", rome(2026, 10, 20).date())
    pens = add_task(db, "Comprare le penne", priority=2)  # mid: low is never planned
    db.execute("UPDATE tasks SET duration_min = 10 WHERE id = ANY(%s)", ([uuid.UUID(post), uuid.UUID(pens)],))
    with_place(db, "tasks", post, poste)
    with_place(db, "tasks", pens, shop)
    return {"post": post, "pens": pens, "office": office, "poste": poste, "shop": shop}


# --- the plan ---------------------------------------------------------------------------------------------


def test_the_squeezed_hour_with_real_places(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"default_mode": "walk"}, "planner": {"buffer_min": 5}})
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    assert plan["date"] == "2026-10-20" and plan["from"] == {
        "lat": OFFICE[0],
        "lon": OFFICE[1],
        "label": "here",
    }
    assert plan["unplanned"] == [] and plan["sources"] == {"routes": "apple", "skipped": []}
    assert_ordered_and_apart(plan)
    tasks = [i for i in plan["items"] if i["kind"] == "task"]
    assert [t["title"] for t in tasks] in (
        ["Spedire il pacco", "Comprare le penne"],
        ["Comprare le penne", "Spedire il pacco"],
    )
    for t in tasks:  # both in the free hour, chained
        assert rome(2026, 10, 20, 10) <= datetime.fromisoformat(t["start"])
        assert datetime.fromisoformat(t["end"]) <= rome(2026, 10, 20, 11)
    post = next(t for t in tasks if t["title"] == "Spedire il pacco")
    assert post["why"].startswith("Scade oggi; Poste Italiane chiude alle 13:35")
    travel = [i for i in plan["items"] if i["kind"] == "travel"]
    assert len(travel) == 3 and all(t["mode"] == "walk" and t["provider"] == "apple" for t in travel)
    first = travel[0]
    assert first["leave_at"] == first["start"] and first["title"] == f"{first['minutes']} min on foot"
    assert first["to_place_id"] in (squeezed_day["poste"], squeezed_day["shop"])
    events = [i for i in plan["items"] if i["kind"] == "event"]
    assert [e["title"] for e in events] == ["Riunione", "Workshop"]
    assert all(e["place_id"] == squeezed_day["office"] for e in events)


def test_get_stores_and_reuses_the_plan(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = rome(2026, 10, 20, 9, 0)
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    first = client.get("/v1/plan", headers=auth)
    assert first.status_code == 200
    assert client.get("/v1/plan", headers=auth).json() == first.json()
    assert db.execute("SELECT count(*) FROM plans").fetchone() == (1,)
    same = client.post("/v1/plan", headers=auth)  # within 15 s, nothing changed: that plan
    assert same.status_code == 201 and same.json() == first.json()
    assert db.execute("SELECT count(*) FROM plans").fetchone() == (1,)
    clock.now = rome(2026, 10, 20, 9, 0) + timedelta(seconds=16)
    again = client.post("/v1/plan", headers=auth)
    assert again.status_code == 201 and db.execute("SELECT count(*) FROM plans").fetchone() == (2,)
    task = again.json()["items"][0]  # made at 09:00:16: from the next whole minute
    assert task == {
        "kind": "task", "start": "2026-10-20T09:01:00+02:00", "end": "2026-10-20T09:31:00+02:00",
        "title": "Chiamare Marco", "task_id": task["task_id"], "place_id": None, "why": "Scade oggi.",
        "status": "planned", "scheduled_by": "owner",  # The user asked Pisa to plan it
        "private": False, "sensitive": False,  # What the wrist may name
    }  # fmt: skip
    assert client.get("/v1/plan", headers=auth, params={"date": "2026-10-19"}).status_code == 404
    bad = client.get("/v1/plan", headers=auth, params={"date": "ieri"})
    assert bad.status_code == 400 and bad.json()["error"]["message"] == "The date must be YYYY-MM-DD."


def test_unplanned_tasks_say_why(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    at_location(db, HERE_MILAN, rome(2026, 10, 25, 9, 59))  # Sunday: the post office is closed
    long = add_task(db, "Riordinare la cantina", priority=2)
    db.execute("UPDATE tasks SET duration_min = 480 WHERE id = %s", (uuid.UUID(long),))
    add_event(db, "Pranzo", rome(2026, 10, 25, 13), minutes=120)
    db.execute("UPDATE tasks SET kept_at = %s", (rome(2026, 10, 25, 9),))  # Kept, not aged out
    plan = run(db, secrets_key, rome(2026, 10, 25, 10, 0), lambda p, env: p.plan())
    reasons = {u["title"]: u["reason"] for u in plan["unplanned"]}
    assert reasons["Spedire il pacco"] == "Chiuso per il resto della giornata."
    assert (
        reasons["Riordinare la cantina"]
        == "Servono 480 min; il tempo libero più lungo rimasto oggi è 360 min."
    )


def test_without_a_location_the_plan_starts_at_the_first_event(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    plan = run(db, secrets_key, rome(2026, 10, 20, 8, 0), lambda p, env: p.plan())
    assert plan["from"]["label"] == "Ufficio"
    assert plan["sources"]["skipped"] == [
        "routes: no location from the last 15 minutes: the plan starts at Riunione"
    ]


def test_without_a_location_or_events_the_first_leg_is_left_out(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    poste = place(db, "Poste Italiane", (45.467, 9.09))
    task = add_task(db, "Spedire il pacco", rome(2026, 10, 20).date())
    with_place(db, "tasks", task, poste)
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    assert plan["from"] is None
    assert plan["sources"]["skipped"] == [
        "routes: no location from the last 15 minutes: the first leg is left out"
    ]
    assert plan["items"][0]["kind"] == "task"


def test_without_a_route_provider_times_are_estimated(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan(), keys=())
    skipped = plan["sources"]["skipped"]
    assert any(s.endswith("Travel times are estimated from the distance.") for s in skipped)
    assert plan["sources"]["routes"] == "estimate"  # N3: never a provider that didn't answer
    trips = [i for i in plan["items"] if i["kind"] == "travel"]
    assert trips and all(t["provider"] == "estimate" for t in trips)


# --- the travel cache -------------------------------------------------------------------------------------


def test_travel_cells_are_reused_for_the_day(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    """M6: cells between places serve the whole day; the row from where the user is serves an hour around
    its departure, from about the same place (~100 m)."""
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    fake = FakePlaces()
    run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan(), fake=fake)
    assert len(fake.routes("apple.etas")) == 4  # one row from here, one from each place
    fake = FakePlaces()
    run(db, secrets_key, rome(2026, 10, 20, 9, 14), lambda p, env: p.plan(), fake=fake)
    at_location(db, (OFFICE[0] + 0.0002, OFFICE[1]), rome(2026, 10, 20, 9, 50))  # 22 m: GPS jitter
    run(db, secrets_key, rome(2026, 10, 20, 9, 55), lambda p, env: p.plan(), fake=fake)
    assert fake.routes("apple.etas") == []  # all from the cache
    at_location(db, OFFICE, rome(2026, 10, 20, 10, 15))
    run(db, secrets_key, rome(2026, 10, 20, 10, 16), lambda p, env: p.plan(), fake=fake)
    assert len(fake.routes("apple.etas")) == 1  # over an hour later: the origin row only


# --- re-plan triggers -------------------------------------------------------------------------------------


def tick(db: psycopg.Connection, secrets_key: str, now: datetime) -> Any:
    plan = run(db, secrets_key, now, lambda p, env: p.tick())
    reason = db.execute("SELECT reason FROM plans ORDER BY generated_at DESC LIMIT 1").fetchone()
    return (plan is not None, reason[0] if reason else None)


def test_replan_triggers_and_the_once_a_minute_guard(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    assert tick(db, secrets_key, rome(2026, 10, 20, 7, 30)) == (False, None)  # before planner.day.start
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 0)) == (True, "first plan of the day")
    add_task(db, "Pagare la bolletta", rome(2026, 10, 20).date())
    assert tick(
        db,
        secrets_key,
        rome(
            2026,
            10,
            20,
            9,
            0,
        ),
    ) == (False, "first plan of the day")  # guard
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 1)) == (True, "tasks, events or settings changed")
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 5)) == (False, "tasks, events or settings changed")
    at_location(db, HERE_MILAN, rome(2026, 10, 20, 9, 6))
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 7)) == (True, "moved more than 1 km")
    at_location(db, (45.4640, 9.0930), rome(2026, 10, 20, 9, 9))  # 200 m: not a move
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 10)) == (False, "moved more than 1 km")
    at_location(db, (45.4640, 9.0930), rome(2026, 10, 20, 9, 36))
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 37)) == (True, "every 30 min")
    assert tick(db, secrets_key, rome(2026, 10, 20, 21, 0)) == (False, "every 30 min")  # the day is over


def test_an_event_change_replans(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    event = add_event(db, "Riunione", rome(2026, 10, 20, 15))
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 0))[0]
    db.execute("UPDATE calendar_events SET starts_at = %s WHERE id = %s", (rome(2026, 10, 20, 16), event))
    assert tick(db, secrets_key, rome(2026, 10, 20, 9, 2)) == (True, "tasks, events or settings changed")


# --- leave now --------------------------------------------------------------------------------------------


def test_leave_now_at_most_once(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": None}, "planner": {"leave_lead_min": 5}})
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    trip = next(i for i in plan["items"] if i["kind"] == "travel")
    target = plan["items"][plan["items"].index(trip) + 1]
    fire = datetime.fromisoformat(trip["leave_at"]) - timedelta(minutes=5)

    def remind(now: datetime) -> Any:
        async def go() -> Any:
            async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now)) as env:
                return await RemindersService(env.pool, owner=owner_at(TZ), clock=env.clock).run()

        return asyncio.run(go())

    remind(fire - timedelta(minutes=2))
    remind(fire)
    remind(fire + timedelta(minutes=1))
    leaves = [text for kind, text, _, _ in notifications(db) if text.startswith("Esci ora")]
    minutes = trip["minutes"]
    unit = "minuto" if minutes == 1 else "minuti"
    assert leaves == [f"Esci ora: {minutes} {unit} con i mezzi per {target['title']}."]
    assert db.execute("SELECT kind, fire_at FROM reminders_sent WHERE kind = 'leave'").fetchall() == [
        ("leave", fire)
    ]


def test_no_leave_now_when_switched_off(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": None}, "planner": {"leave_now": False}})
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    trip = next(i for i in plan["items"] if i["kind"] == "travel")
    fire = datetime.fromisoformat(trip["leave_at"]) - timedelta(minutes=5)

    async def go() -> Any:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(fire)) as env:
            return await RemindersService(env.pool, owner=owner_at(TZ), clock=env.clock).run()

    asyncio.run(go())
    assert notifications(db) == []


# --- the briefing -----------------------------------------------------------------------------------------


def test_the_briefing_names_the_first_task_and_when_to_leave(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"default_mode": "walk"}, "planner": {"buffer_min": 5}})
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    models = FakeModels()
    models.default = RuntimeError("provider down")  # the fixed template: deterministic

    async def brief(planner: PlannerService, env: PlaceEnv) -> str:
        tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
        service = BriefingService(
            env.pool,
            env.places._gateway,
            tasks,
            owner=owner_at(TZ),
            clock=env.clock,
            places=env.places,
            planner=planner,
        )
        return (await service.ask()).text

    set_settings(db, {"briefing": {"max_words": 200}})
    text = run(db, secrets_key, rome(2026, 10, 20, 9, 0), brief, models=models)
    plan = db.execute("SELECT plan FROM plans").fetchone()[0]  # type: ignore[index]
    first = next(i for i in plan["items"] if i["kind"] == "task")
    before = plan["items"][plan["items"].index(first) - 1]
    start = datetime.fromisoformat(first["start"])
    leave = datetime.fromisoformat(before["leave_at"])
    assert f"Prima attività: {first['title']} alle {start:%H:%M} (esci alle {leave:%H:%M})." in text


# --- the plan tools ---------------------------------------------------------------------------------------


def ask_tool(db: psycopg.Connection, secrets_key: str, tool: str, now: datetime) -> str:
    from pisa.services.settings import SettingsService
    from tests.test_place_tools import calls_tool

    device_id = uuid.UUID(pair(db)[0])
    models = FakeModels()
    models.default = calls_tool(tool, {})

    async def go(planner: PlannerService, env: PlaceEnv) -> str:
        tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)

        async def brief() -> str:
            return ""

        data = assistant_data(
            env.pool, tasks, clock=env.clock, places=env.places,
            settings=SettingsService(env.pool, owner=owner_at()), brief=brief, planner=planner,
        )  # fmt: skip
        service = conversation(env.pool, env.places._gateway, owner=owner_at(TZ), clock=env.clock, data=data)

        async def emit(delta: str, final: bool) -> None:
            pass

        return await service.respond(device_id, "Pisa?", "it-IT", emit, trace_id="t")

    return run(db, secrets_key, now, go, models=models)


def test_get_plan_and_replan_tools(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_place_tools import data_of

    add_event(db, "Riunione", rome(2026, 10, 20, 11))
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    text = data_of(ask_tool(db, secrets_key, "get_plan", rome(2026, 10, 20, 9, 0)))
    assert text == (
        "09:00-09:30 task: Chiamare Marco (Scade oggi.)\n11:00-12:00 event: Riunione\n"
        "Note: routes: no location from the last 15 minutes: the first leg is left out"
    )
    assert data_of(ask_tool(db, secrets_key, "replan", rome(2026, 10, 20, 9, 10))).startswith(
        "09:10-09:40 task: Chiamare Marco"
    )
    assert db.execute("SELECT count(*) FROM plans").fetchone() == (2,)


def test_what_now(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_place_tools import data_of

    add_event(db, "Riunione", rome(2026, 10, 20, 11))
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    during = data_of(ask_tool(db, secrets_key, "what_now", rome(2026, 10, 20, 9, 10)))
    # The "now" tool says the time first, in their zone
    assert during == (
        "The time: Tuesday 09:10 (Europe/Rome).\nNow: Chiamare Marco until 09:40.\nNext: Riunione at 11:00."
    )
    db.execute("UPDATE tasks SET status = 'done'")
    free = data_of(ask_tool(db, secrets_key, "what_now", rome(2026, 10, 20, 10, 0)))
    assert free == "The time: Tuesday 10:00 (Europe/Rome).\nNext: Riunione at 11:00.\nFree for 60 min."


# --- review fixes ---------------------------------------------------------------------------------------

FAR = (45.6590, 9.0830)  # about 21 km north of the office


def items_of(plan: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [i for i in plan["items"] if i["kind"] == kind]


def test_a_dinner_out_of_reach_still_gets_its_trip_and_says_so(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """H2 + M4: planning at 20:00 (the day ends at 21:00), dinner at 20:50-21:30 is over an hour away. The
    plan has the trip (leave now), the dinner keeps its own times and says it can't be reached in time, and
    the call that can't be made during the trip says why."""
    at_location(db, OFFICE, rome(2026, 10, 20, 19, 59))
    restaurant = place(db, "Ristorante", FAR)
    dinner = add_event(db, "Cena", rome(2026, 10, 20, 20, 50), minutes=40)
    with_place(db, "calendar_events", dinner, restaurant)
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    plan = run(db, secrets_key, rome(2026, 10, 20, 20, 0), lambda p, env: p.plan(), keys=())
    assert_ordered_and_apart(plan)
    (trip,) = items_of(plan, "travel")
    assert trip["leave_at"] == "2026-10-20T20:00:00+02:00" and trip["end"] == "2026-10-20T20:50:00+02:00"
    assert trip["minutes"] > 60
    (event,) = items_of(plan, "event")
    assert (event["start"], event["end"]) == ("2026-10-20T20:50:00+02:00", "2026-10-20T21:30:00+02:00")
    assert event["why"].startswith("Non ci arrivi prima che finisca: al più presto alle ")
    (call,) = plan["unplanned"]
    assert call["reason"] == "Servono 30 min; il tempo libero più lungo rimasto oggi è 0 min."


def test_when_the_solver_finds_nothing_the_plan_says_so(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """H2 / L8: no solution in time is never a silent empty plan."""
    from pisa.planner.solver import Solution
    from pisa.services import planner as planner_module

    monkeypatch.setattr(planner_module, "solve", lambda problem, time_limit_s: Solution("unknown"))
    add_event(db, "Riunione", rome(2026, 10, 20, 11))
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    assert (
        "plan: Pisa couldn't work out the day in time; the events are shown as they are"
        in (plan["sources"]["skipped"])
    )
    assert [i["title"] for i in items_of(plan, "event")] == ["Riunione"]
    assert plan["unplanned"][0]["reason"] == "Pisa non ha finito il piano in tempo; chiedine uno nuovo."


def test_planning_before_the_day_starts_leaves_in_time(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """H3: the day starts at 08:00; a meeting at 08:15 is a trip away. Planned at 07:30, the trip starts
    before 08:00 and the meeting keeps 08:15. The worker wakes for it early enough, not at 08:00."""
    set_settings(db, {"planner": {"buffer_min": 10, "leave_lead_min": 5}})
    meeting_place = place(db, "Cliente", (45.5090, 9.0890))  # about 4 km
    meeting = add_event(db, "Cliente", rome(2026, 10, 20, 8, 15), minutes=60)
    with_place(db, "calendar_events", meeting, meeting_place)
    at_location(db, OFFICE, rome(2026, 10, 20, 6, 50))
    assert tick(db, secrets_key, rome(2026, 10, 20, 6, 55)) == (False, None)  # not yet: no stale location
    at_location(db, OFFICE, rome(2026, 10, 20, 7, 29))
    woke, _ = tick(db, secrets_key, rome(2026, 10, 20, 7, 30))
    assert woke
    plan = db.execute("SELECT plan FROM plans").fetchone()[0]  # type: ignore[index]
    (trip,) = items_of(plan, "travel")
    (event,) = items_of(plan, "event")
    assert event["start"] == "2026-10-20T08:15:00+02:00" and "why" not in event
    leave = rome(2026, 10, 20, 8, 15) - timedelta(minutes=trip["minutes"] + 10)
    assert trip["leave_at"] == leave.isoformat() and leave < rome(2026, 10, 20, 8)
    assert trip["end"] == event["start"]  # L2: the buffer belongs to the trip, never free time


def test_a_long_buffer_is_never_free_time(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    """L2: a 20-minute buffer after a trip is part of the trip, so `what_now` can't offer a task in it."""
    set_settings(db, {"planner": {"buffer_min": 20}})
    at_location(db, HERE_MILAN, rome(2026, 10, 20, 8, 0))
    plan = run(db, secrets_key, rome(2026, 10, 20, 8, 0), lambda p, env: p.plan())
    assert_ordered_and_apart(plan)
    items = plan["items"]
    meeting = next(i for i in items if i["kind"] == "event")
    before = items[items.index(meeting) - 1]
    assert before["kind"] == "travel" and before["end"] == meeting["start"]


def test_items_start_on_a_whole_minute(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """L1: a plan made at 09:00:40 starts at 09:01, so nothing overlaps by the seconds."""
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0) + timedelta(seconds=40), lambda p, env: p.plan())
    assert items_of(plan, "task")[0]["start"] == "2026-10-20T09:01:00+02:00"


def test_one_plan_at_a_time_and_a_repeat_ask_gets_the_same_plan(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """L4: two asks at once make one plan."""
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())

    async def twice(planner: PlannerService, env: PlaceEnv) -> list[Any]:
        return list(await asyncio.gather(planner.plan(reason="a"), planner.plan(reason="b")))

    first, second = run(db, secrets_key, rome(2026, 10, 20, 9, 0), twice)
    assert first == second and db.execute("SELECT count(*) FROM plans").fetchone() == (1,)


def test_slow_travel_times_are_estimated_within_the_budget(
    db, secrets_key, squeezed_day, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """L5: the plan has an overall budget; travel times that don't arrive in it are estimated."""
    from pisa.services import planner as planner_module

    monkeypatch.setattr(planner_module, "PLAN_BUDGET_S", 1.0)
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))

    async def slow(planner: PlannerService, env: PlaceEnv) -> dict[str, Any]:
        async def travel_between(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(5)

        env.places.travel_between = travel_between  # type: ignore[method-assign]
        return await planner.plan()

    started = datetime.now()
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), slow)
    assert (datetime.now() - started).total_seconds() < 4
    assert (
        "routes: travel times took too long; the rest are estimated from the distance"
        in (plan["sources"]["skipped"])
    )
    assert all(t["provider"] == "estimate" for t in items_of(plan, "travel"))


def test_a_place_without_a_position_is_said_and_not_searched_every_time(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """L7: Google's coordinates were pruned and the place isn't found again: the plan says so, and the
    next re-plan doesn't search again."""
    lost = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, google_place_id, created_at, updated_at) VALUES (%s, 'Ferramenta "
        "Bianchi', 'ChIJlost0000', now(), now())",
        (lost,),
    )
    task = add_task(db, "Comprare le viti", rome(2026, 10, 20).date())
    with_place(db, "tasks", task, str(lost))
    fake = FakePlaces()
    first = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan(), fake=fake)
    assert (
        "places: no position for Ferramenta Bianchi: planned as if done anywhere"
        in (first["sources"]["skipped"])
    )
    searches = len(fake.routes("apple.search")) + len(fake.routes("google.search"))
    assert searches >= 1
    run(db, secrets_key, rome(2026, 10, 20, 9, 30), lambda p, env: p.plan(), fake=fake)
    assert len(fake.routes("apple.search")) + len(fake.routes("google.search")) == searches


def test_an_opening_that_falls_during_a_meeting(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    """L8: the post office is open until 13:35, all of it inside a long meeting."""
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (uuid.UUID(squeezed_day["pens"]),))
    db.execute("DELETE FROM calendar_events")
    add_event(db, "Consiglio", rome(2026, 10, 20, 8, 30), minutes=330)  # 08:30-14:00, no place
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    (post,) = plan["unplanned"]
    assert post["reason"] == "Aperto solo 09:00-13:35, durante Consiglio."


def test_free_time_in_reasons_is_measured_before_placing(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """L8: two 60-minute tasks, 90 free minutes: the one left out doesn't fit next to the other, which is
    not the same as "the longest free time is 30 min"."""
    set_settings(db, {"planner": {"day": {"start": "09:00", "end": "10:30"}}})
    first = add_task(db, "Scrivere il rapporto", rome(2026, 10, 20).date(), priority=1)
    second = add_task(db, "Riordinare l'archivio", rome(2026, 10, 20).date(), priority=2)
    db.execute(
        "UPDATE tasks SET duration_min = 60 WHERE id = ANY(%s)", ([uuid.UUID(first), uuid.UUID(second)],)
    )
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    assert [t["title"] for t in items_of(plan, "task")] == ["Scrivere il rapporto"]
    assert plan["unplanned"][0]["reason"] == "Oggi non ci sta, accanto ai task più urgenti."


def test_the_night_the_clocks_go_back(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """N4: a window 00:00-05:00 on 25 Oct 2026 lasts 6 hours: a 330-minute task fits."""
    set_settings(db, {"planner": {"day": {"start": "00:00", "end": "05:00"}}})
    task = add_task(db, "Fare il backup", rome(2026, 10, 25).date())
    db.execute("UPDATE tasks SET duration_min = 330 WHERE id = %s", (uuid.UUID(task),))
    plan = run(db, secrets_key, rome(2026, 10, 25, 0, 0), lambda p, env: p.plan())
    (placed,) = items_of(plan, "task")
    assert (placed["start"], placed["end"]) == ("2026-10-25T00:00:00+02:00", "2026-10-25T04:30:00+01:00")
    (free,) = items_of(plan, "free")
    assert (free["start"], free["end"]) == ("2026-10-25T04:30:00+01:00", "2026-10-25T05:00:00+01:00")


def test_a_day_of_re_plans_with_google_only_stays_cheap(db, secrets_key, squeezed_day) -> None:  # type: ignore[no-untyped-def]
    """M6: every minute from 08:00 to 21:00 (re-plans every 30 min, GPS jitter of a few metres), with only
    Google answering: the matrix elements stay few."""
    fake = FakePlaces()
    moment = rome(2026, 10, 20, 8, 0)
    jitter = 0
    while moment < rome(2026, 10, 20, 21, 0):
        jitter = (jitter + 1) % 3
        at_location(db, (OFFICE[0] + 0.00005 * jitter, OFFICE[1]), moment - timedelta(minutes=1))
        run(db, secrets_key, moment, lambda p, env: p.tick(), fake=fake, keys=("google",))
        moment += timedelta(minutes=10)
    elements = sum(
        len(json.loads(request.content)["destinations"])
        for route, request in fake.requests
        if route == "google.matrix"
    )
    plans = db.execute("SELECT count(*) FROM plans").fetchone()[0]  # type: ignore[index]
    assert plans >= 26
    assert elements <= 6 + 3 * 13  # the day's place cells once, then the origin row about once an hour


# --- M5: "leave now" once per (target, day) ---------------------------------------------------------------

CLIENT = (45.5090, 9.0890)


def store_plan(
    db: psycopg.Connection, made: datetime, leave_at: datetime, minutes: int, place_id: str
) -> None:
    arrive = leave_at + timedelta(minutes=minutes + 10)
    plan = {
        "date": "2026-10-20",
        "items": [
            {"kind": "travel", "start": leave_at.isoformat(), "end": arrive.isoformat(), "title": "x",
             "mode": "transit", "minutes": minutes, "provider": "apple", "to_place_id": place_id,
             "leave_at": leave_at.isoformat()},
            {"kind": "event", "start": arrive.isoformat(), "end": (arrive + timedelta(hours=1)).isoformat(),
             "title": "Cliente", "event_id": "ev1", "place_id": place_id},
        ],
    }  # fmt: skip
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (%s, '2026-10-20', %s, "
        "'x', 'f', %s)",
        (uuid.uuid4(), made, Jsonb(plan)),
    )


def remind_at(db: psycopg.Connection, secrets_key: str, now: datetime) -> Any:
    async def go() -> Any:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now)) as env:
            return await RemindersService(env.pool, owner=owner_at(TZ), clock=env.clock).run()

    return asyncio.run(go())


def leaves(db: psycopg.Connection) -> list[str]:
    return [text for _, text, _, _ in notifications(db) if text.startswith("Esci ora")]


def test_a_replan_while_travelling_does_not_repeat_leave_now(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """M5: sent at 08:55 for a 09:00 trip; re-plans after the user moved (leave 09:08) and in a new traffic
    slot (leave 09:12) send nothing more."""
    set_settings(db, {"quiet": {"hours": None}, "planner": {"leave_lead_min": 5}})
    client = place(db, "Cliente", CLIENT)
    store_plan(db, rome(2026, 10, 20, 8, 0), rome(2026, 10, 20, 9, 0), 30, client)
    remind_at(db, secrets_key, rome(2026, 10, 20, 8, 54))
    remind_at(db, secrets_key, rome(2026, 10, 20, 8, 55))
    assert leaves(db) == ["Esci ora: 30 minuti con i mezzi per Cliente."]
    store_plan(db, rome(2026, 10, 20, 9, 2), rome(2026, 10, 20, 9, 8), 25, client)
    remind_at(db, secrets_key, rome(2026, 10, 20, 9, 3))
    store_plan(db, rome(2026, 10, 20, 9, 5), rome(2026, 10, 20, 9, 12), 21, client)
    remind_at(db, secrets_key, rome(2026, 10, 20, 9, 7))
    assert len(leaves(db)) == 1
    assert db.execute("SELECT ref FROM reminders_sent WHERE kind = 'leave'").fetchall() == [
        ("ev1@2026-10-20",)
    ]


def test_a_plan_made_after_the_reminder_time_still_says_leave_now(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The user is already late: the plan made at 09:02 says leave at 09:03, so its "leave now" (09:03 - 5
    min) was never ahead of a check. It goes out at the next check, once."""
    set_settings(db, {"quiet": {"hours": None}, "planner": {"leave_lead_min": 5}})
    client = place(db, "Cliente", CLIENT)
    remind_at(db, secrets_key, rome(2026, 10, 20, 9, 1))
    store_plan(db, rome(2026, 10, 20, 9, 2), rome(2026, 10, 20, 9, 3), 30, client)
    remind_at(db, secrets_key, rome(2026, 10, 20, 9, 4))  # 08:58 is more than the 5-minute catch-up ago
    assert leaves(db) == ["Esci ora: 30 minuti con i mezzi per Cliente."]
    remind_at(db, secrets_key, rome(2026, 10, 20, 9, 5))
    assert len(leaves(db)) == 1


def test_a_leave_time_long_past_is_skipped(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": None}, "planner": {"leave_lead_min": 5}})
    client = place(db, "Cliente", CLIENT)
    remind_at(db, secrets_key, rome(2026, 10, 20, 8, 0))
    store_plan(db, rome(2026, 10, 20, 8, 1), rome(2026, 10, 20, 9, 0), 30, client)
    remind_at(db, secrets_key, rome(2026, 10, 20, 9, 30))  # Pisa was down from 08:00 to 09:30
    assert leaves(db) == []


def test_when_leave_now_may_go_out_again(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """M5: a trip that moved at least 10 minutes earlier may be announced again, but not within the last
    trip's length, nor while the user is getting closer to the target."""
    from pisa.services.reminders import Due, _leave_again

    client = place(db, "Cliente", CLIENT)
    db.execute(
        "INSERT INTO reminders_sent (kind, ref, fire_at, outcome, created_at) "
        "VALUES ('leave', 'ev1@2026-10-20', %s, 'sent', %s)",
        (rome(2026, 10, 20, 8, 55), rome(2026, 10, 20, 8, 55)),
    )
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 54))

    def may(leave: datetime, now: datetime, minutes: int = 30) -> bool:
        due = Due("leave", "ev1@2026-10-20", "Cliente", None, leave, leave, minutes, "transit", client)

        async def go() -> bool:
            async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
                return await _leave_again(conn, due, timedelta(minutes=5), now)

        return asyncio.run(go())

    assert not may(rome(2026, 10, 20, 8, 55), rome(2026, 10, 20, 9, 40))  # 5 min earlier: not enough
    assert not may(rome(2026, 10, 20, 8, 50), rome(2026, 10, 20, 9, 10))  # within the trip's 30 min
    assert may(rome(2026, 10, 20, 8, 50), rome(2026, 10, 20, 9, 30))  # 10 min earlier, 35 min later, still
    at_location(db, (45.4890, 9.0890), rome(2026, 10, 20, 9, 29))  # 2 km nearer the client
    assert not may(rome(2026, 10, 20, 8, 50), rome(2026, 10, 20, 9, 30))


def test_leave_now_again_when_the_trip_moves_much_later(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """CORE-5b question 1: a task moved from the morning to the afternoon gets its own "leave now" (later by
    more than max(trip, 30 min)); a small drift doesn't; nothing goes out once the user is there."""
    from pisa.services.reminders import Due, _leave_again

    client = place(db, "Cliente", CLIENT)
    db.execute(
        "INSERT INTO reminders_sent (kind, ref, fire_at, outcome, created_at) "
        "VALUES ('leave', 'ev1@2026-10-20', %s, 'sent', %s)",
        (rome(2026, 10, 20, 8, 55), rome(2026, 10, 20, 8, 55)),
    )  # leave time 09:00 (lead 5)
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 54))

    def may(leave: datetime, now: datetime, minutes: int = 20) -> bool:
        due = Due("leave", "ev1@2026-10-20", "Cliente", None, leave, leave, minutes, "transit", client)

        async def go() -> bool:
            async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
                return await _leave_again(conn, due, timedelta(minutes=5), now)

        return asyncio.run(go())

    assert not may(rome(2026, 10, 20, 9, 20), rome(2026, 10, 20, 9, 15))  # 20 min later: drift
    assert may(rome(2026, 10, 20, 15, 0), rome(2026, 10, 20, 14, 55))  # moved to the afternoon
    at_location(db, CLIENT, rome(2026, 10, 20, 14, 50))  # already at the client
    assert not may(rome(2026, 10, 20, 15, 0), rome(2026, 10, 20, 14, 55))


def test_a_briefing_that_stops_waiting_still_gets_the_plan_stored(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Answer 11: the briefing waits 3 s for the plan; when it gives up, the plan is still made and stored."""
    from pisa.services import briefing as briefing_module

    monkeypatch.setattr(briefing_module, "PLACE_NOTES_S", 0.01)
    add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    models = FakeModels()
    models.default = RuntimeError("provider down")

    async def brief(planner: PlannerService, env: PlaceEnv) -> int:
        tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
        service = BriefingService(
            env.pool, env.places._gateway, tasks, owner=owner_at(TZ), clock=env.clock, planner=planner
        )
        text = (await service.ask()).text
        assert "Prima attività" not in text  # it didn't wait
        for _ in range(100):
            async with env.pool.connection() as conn:
                row = await (await conn.execute("SELECT count(*) FROM plans")).fetchone()
            if row and row[0]:
                return row[0]
            await asyncio.sleep(0.05)
        return 0

    assert run(db, secrets_key, rome(2026, 10, 20, 9, 0), brief, models=models) == 1


def test_back_to_back_meetings_at_two_places_keep_the_trip_and_leave_now(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Meeting A 09:00-10:00 at the office, B 10:00-11:00 across town. Being late for B
    must not hide the trip: it keeps its real time (overlapping B's start) and its "leave at"."""
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 0))
    office, client = place(db, "Ufficio", OFFICE), place(db, "Cliente", CLIENT)
    a = add_event(db, "Riunione A", rome(2026, 10, 20, 9, 0))
    b = add_event(db, "Riunione B", rome(2026, 10, 20, 10, 0))
    with_place(db, "calendar_events", a, office)
    with_place(db, "calendar_events", b, client)
    plan = run(db, secrets_key, rome(2026, 10, 20, 8, 0), lambda p, env: p.plan(), keys=())
    assert_ordered_and_apart(plan)
    trips = [t for t in items_of(plan, "travel") if t["to_place_id"] == client]
    assert len(trips) == 1 and trips[0]["leave_at"] == "2026-10-20T10:00:00+02:00"
    assert trips[0]["end"] > "2026-10-20T10:00:00+02:00"  # its real arrival, after B's start
    (event_b,) = [e for e in items_of(plan, "event") if e["event_id"] == b]
    assert event_b["start"] == "2026-10-20T10:00:00+02:00" and "min di ritardo" in event_b["why"]


def test_an_event_ending_with_seconds_is_not_overlapped(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """An event ending at 10:30:30 keeps the minute it ends in."""
    at_location(db, OFFICE, rome(2026, 10, 20, 9, 0))
    db.execute(
        "UPDATE calendar_events SET ends_at = ends_at + interval '30 seconds' WHERE id = %s",
        (add_event(db, "Call", rome(2026, 10, 20, 9, 30)),),
    )
    task = add_task(db, "Chiamare Marco", rome(2026, 10, 20).date())
    db.execute("UPDATE tasks SET duration_min = 45 WHERE id = %s", (task,))  # doesn't fit before the call
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan(), keys=())
    assert_ordered_and_apart(plan)
    (placed,) = items_of(plan, "task")
    assert placed["start"] >= "2026-10-20T10:31:00+02:00"


def test_overlapping_events_are_separate_items_naming_each_other(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Each event its own item, `overlaps` naming the
    others."""
    first = add_event(db, "Call", rome(2026, 10, 20, 10, 0))
    second = add_event(db, "Webinar", rome(2026, 10, 20, 10, 30))
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan(), keys=())
    call, webinar = items_of(plan, "event")
    assert call["event_ids"] == [first] and call["title"] == "Call"
    assert call["overlaps"] == [{"event_id": second, "title": "Webinar"}]
    assert webinar["event_ids"] == [second] and webinar["overlaps"] == [{"event_id": first, "title": "Call"}]


def test_the_hotel_is_the_base_without_a_location(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A hotel booked as a multi-day all-day event is where the plan starts and ends on its days when
    there is no fresh location: the first trip leaves from it, and the day ends with the trip back."""
    hotel = place(db, "Hostel Fietsbel Amsterdam", (52.339, 4.876))
    db.execute("INSERT INTO calendars (id, name) VALUES ('primary', 'Primary') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, "
        "start_date, end_date, "
        "status, place_id) VALUES ('primary', 'stay', 'Hostel Fietsbel Amsterdam', %s, %s, true, "
        "'2026-10-20', '2026-10-24', 'confirmed', %s)",
        (rome(2026, 10, 20), rome(2026, 10, 24), hotel),
    )
    spiraal = place(db, "Spiraal", (52.352, 4.868))
    visit = add_task(db, "Vedere la mostra al Spiraal", rome(2026, 10, 21).date())
    with_place(db, "tasks", visit, spiraal)
    plan = run(db, secrets_key, rome(2026, 10, 21, 9, 0), lambda p, env: p.plan(), keys=())
    assert plan["from"]["label"] == "Hostel Fietsbel Amsterdam"
    assert plan["sources"]["skipped"][0] == (
        "routes: no location from the last 15 minutes: the plan starts and ends at Hostel Fietsbel Amsterdam"
    )
    trips = [i for i in plan["items"] if i["kind"] == "travel"]
    assert (
        trips[0]["to_place_id"] == spiraal and trips[-1]["to_place_id"] == hotel
    )  # out, and back by the end
    assert not any(
        i.get("event_id") == "stay" or i.get("title") == "Hostel Fietsbel Amsterdam"
        for i in items_of(plan, "event")
    )
    assert datetime.fromisoformat(trips[-1]["end"]) <= rome(2026, 10, 21, 21, 1)


def test_a_plan_naming_an_email_made_task_is_outside_text(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """What_now's plan holds a task an email made: the turn marks
    it as outside text (`invite`), through `_respond`'s foreign titles."""
    add_task(db, "Pagare il canone come dice la mail", rome(2026, 10, 20).date())
    db.execute("UPDATE tasks SET source_kind = 'email'")
    said = ask_tool(db, secrets_key, "what_now", rome(2026, 10, 20, 9, 10))
    assert "<invite-" in said and "<plan-" not in said
