"""A regression test per finding, built from its scenario."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from pisa.domain.errors import PisaError
from pisa.domain.follow import status
from pisa.domain.stays import Fix, Visit, detect
from pisa.services import stays
from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import Entry, PlannerService
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, add_task, rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.live_app import live
from tests.owner import owner_at
from tests.places_env import place_env
from tests.places_fake import FakePlaces
from tests.test_follow import (
    HOTEL,
    LUNCH,
    MIDNIGHT,
    TRATTORIA,
    fixes,
    make_lunch_day,
    plan_and_tick,
    run,
    track,
)
from tests.test_plan_options import NOW, TZ, apple_place, make_trip_day
from tests.test_planner import place, with_place
from tests.test_stays import CAFE, MUSEUM


@pytest.fixture
def lunch_day(db: psycopg.Connection) -> dict[str, str]:
    return make_lunch_day(db)


@pytest.fixture
def trip_day(db: psycopg.Connection) -> dict[str, str]:
    return make_trip_day(db)


# ~1.5 km north of the trattoria: where a phone with Precise Location off puts the user.
OFF = (TRATTORIA[0] + 0.0135, TRATTORIA[1])


def noisy(start: datetime, end: datetime) -> list[Fix]:
    return [
        Fix(OFF[0], OFF[1], 2300, start + timedelta(minutes=m))
        for m in range(0, int((end - start).total_seconds() // 60) + 1, 2)
    ]


# --- H1: imprecise GPS makes no false "missed" ------------------------------------------------------------


def test_an_imprecise_location_is_no_location() -> None:
    now = rome(2026, 10, 21, 14, 5)
    args = {"now": now, "trip_min": 30, "day_start": MIDNIGHT}
    assert status(LUNCH, [], here=OFF, here_accuracy_m=2300, **args) == "planned"  # unknown, not away
    assert status(LUNCH, [], here=OFF, here_accuracy_m=100, **args) == "missed"  # 1.5 km > 150 + 100 m
    near = (TRATTORIA[0] + 0.0020, TRATTORIA[1])  # ~220 m: within 150 m plus its 100 m accuracy
    assert status(LUNCH, [], here=near, here_accuracy_m=100, **args) == "planned"


def test_owner_at_the_trattoria_with_noisy_fixes_gets_no_check_in(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    """At the trattoria 13:00-14:30, the phone reporting ~1.5 km off at 2300 m accuracy: at 14:05, no
    check-in."""
    track(db, lunch_day["device"], noisy(rome(2026, 10, 21, 13), rome(2026, 10, 21, 14, 4)))
    assert run(db, secrets_key, rome(2026, 10, 21, 14, 5), plan_and_tick) == 0
    assert db.execute("SELECT count(*) FROM check_ins").fetchone() == (0,)


def test_an_older_stay_is_not_here_once_newer_fixes_came(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    """Precise fixes at the hotel 12:00-12:40, then only 2300 m fixes near the trattoria: the hotel stay is
    still "ongoing" (imprecise fixes don't end it), but it isn't "here" any more: no check-in at 14:05."""
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12), rome(2026, 10, 21, 12, 40)))
    track(db, lunch_day["device"], noisy(rome(2026, 10, 21, 12, 44), rome(2026, 10, 21, 14, 4)))
    assert run(db, secrets_key, rome(2026, 10, 21, 14, 5), plan_and_tick) == 0


def test_an_unanswered_check_in_stops_blocking_after_an_hour(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    """An ignored check-in about an all-afternoon event would block every other one until midnight."""
    ade = add_event(db, "Golfslag", rome(2026, 10, 21, 12), minutes=360)
    with_place(db, "calendar_events", ade, place(db, "Museo", MUSEUM))
    db.execute(
        "INSERT INTO check_ins (id, day, kind, event_id, title, text, choices, created_at) VALUES "
        "(%s, '2026-10-21', 'missed', %s, 'Golfslag', 'x', '[]', %s)",
        (uuid.uuid4(), ade, rome(2026, 10, 21, 13, 4)),
    )
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 14, 4)))
    assert run(db, secrets_key, rome(2026, 10, 21, 14, 5), plan_and_tick) == 1  # the old one: 61 min, dropped
    assert db.execute(
        "SELECT title, dropped_at IS NOT NULL FROM check_ins ORDER BY created_at"
    ).fetchall() == [("Golfslag", True), ("Pranzo", False)]


def test_a_refresh_counts_retention_from_the_newest_point(db) -> None:  # type: ignore[no-untyped-def]
    """An upload of only 29-day-old points doesn't reach back to a stay that began 59 days ago."""
    device, _ = pair(db)
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    long_ago = now - timedelta(days=59) + timedelta(hours=1)
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, departed_at, lat, lon, accuracy_m, last_seen_at) "
        "VALUES (%s, %s, %s, NULL, 1, 1, 5, %s)",
        (uuid.uuid4(), device, long_ago, now - timedelta(days=29)),
    )
    track(db, device, fixes(CAFE, now - timedelta(minutes=20), now))  # the newest point: now
    track(db, device, fixes(MUSEUM, now - timedelta(days=29), now - timedelta(days=29, minutes=-20)))
    assert db.execute("SELECT count(*) FROM stays WHERE arrived_at = %s", (long_ago,)).fetchone() == (1,)


# --- H2: a bad time is refused and wipes nothing ----------------------------------------------------------


def visit(arrived: str, departed: str | None = None) -> dict[str, object]:
    return {"kind": "visit", "lat": CAFE[0], "lon": CAFE[1], "accuracy_m": 30, "arrived_at": arrived,
            "departed_at": departed}  # fmt: skip


def test_times_out_of_bounds_are_refused_and_stays_stay(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    clock.now = now
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    good = [
        {"kind": "fix", "lat": CAFE[0], "lon": CAFE[1], "accuracy_m": 10, "measured_at": at.isoformat()}
        for at in (now - timedelta(minutes=30 - i) for i in range(0, 21, 2))
    ]
    assert client.post("/v1/devices/me/locations", headers=auth, json={"locations": good}).status_code == 204
    before = db.execute("SELECT arrived_at, departed_at FROM stays").fetchall()
    assert len(before) == 1
    for bad, field in (
        (visit("2001-01-01T10:00:00+00:00"), "arrived_at"),
        (visit("0001-01-01T00:00:00+00:00"), "arrived_at"),  # was an OverflowError (500)
        (
            visit((now - timedelta(hours=1)).isoformat(), (now + timedelta(minutes=6)).isoformat()),
            "departed_at",
        ),
        (good[0] | {"measured_at": (now - timedelta(days=31)).isoformat()}, "measured_at"),
    ):
        response = client.post("/v1/devices/me/locations", headers=auth, json={"locations": [good[1], bad]})
        error = response.json()["error"]
        assert response.status_code == 400 and error["code"] == "validation_failed", bad
        assert error["message"].startswith(f"The time in locations[1].{field} is more than "), error
    assert db.execute("SELECT arrived_at, departed_at FROM stays").fetchall() == before
    assert db.execute("SELECT count(*) FROM device_visits").fetchone() == (0,)
    ahead = good[0] | {"measured_at": (now + timedelta(minutes=4)).isoformat()}  # within the 5 minutes
    assert (
        client.post("/v1/devices/me/locations", headers=auth, json={"locations": [ahead]}).status_code == 204
    )


def test_the_websocket_refuses_a_bad_time(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device, token = pair(db)
    with live(client, token, device) as app:
        old = (clock.now - timedelta(days=31)).isoformat()
        app.custom("pisa.location", {"lat": 1, "lon": 1, "accuracyM": 5, "measuredAt": old})
        error = app.next()
        assert error["type"] == "error" and error["code"] == "invalid_message"
        assert (
            error["message"] == "The location's measured_at is more than 30 days old, so it was not stored."
        )
        assert app.ping() == []
        (refused,) = [f for f in app.frames if f.get("control") == "error"]  # on the wire: the frame's error
        assert refused["code"] == "invalid_frame" and "30 days old" in refused["message"]
    assert db.execute("SELECT count(*) FROM device_locations").fetchone() == (0,)


def test_a_refresh_never_deletes_older_than_retention(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    old = now - timedelta(days=40)
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, departed_at, lat, lon, accuracy_m, last_seen_at) "
        "VALUES (%s, %s, %s, %s, 1, 1, 5, %s)",
        (uuid.uuid4(), device, old, old + timedelta(hours=1), old + timedelta(hours=1)),
    )

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            await stays.refresh(conn, uuid.UUID(device), now, since=now - timedelta(days=365))

    asyncio.run(go())
    assert db.execute("SELECT arrived_at FROM stays").fetchall() == [(old,)]


# --- M12: a visit with poor accuracy doesn't count --------------------------------------------------------


def test_a_visit_at_2_3_km_accuracy_is_no_stay() -> None:
    t = datetime(2026, 10, 21, 11, tzinfo=UTC)
    assert detect([], [Visit(CAFE[0], CAFE[1], 2300, t, t + timedelta(hours=1))]) == []
    assert len(detect([], [Visit(CAFE[0], CAFE[1], 150, t, t + timedelta(hours=1))])) == 1


def test_a_vague_visit_does_not_mark_lunch_done(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            vague = Visit(OFF[0], OFF[1], 2300, rome(2026, 10, 21, 12, 50), rome(2026, 10, 21, 14, 10))
            await stays.save(conn, uuid.UUID(lunch_day["device"]), [], [vague])

    asyncio.run(go())

    async def statuses(follow, env):  # type: ignore[no-untyped-def]
        plan = await follow._planner.plan(reason="test")
        return await follow.statuses(plan)

    assert set(run(db, secrets_key, rome(2026, 10, 21, 14, 15), statuses).values()) == {"planned"}


# --- L5: a past day is judged as it ended ----------------------------------------------------------------


def test_a_past_day_is_judged_as_it_ended(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    """Yesterday's lunch, read today: where the user is now (at the trattoria, as it happens) says nothing
    about yesterday. Yesterday ended at the hotel, with no stay at the trattoria: missed."""
    run(db, secrets_key, rome(2026, 10, 21, 12), lambda f, env: f._planner.plan(reason="test"))
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 22), rome(2026, 10, 21, 23, 58)))
    track(db, lunch_day["device"], fixes(TRATTORIA, rome(2026, 10, 22, 9), rome(2026, 10, 22, 9, 30)))

    async def yesterday(follow, env):  # type: ignore[no-untyped-def]
        return await follow.day(rome(2026, 10, 21).date())

    day = run(db, secrets_key, rome(2026, 10, 22, 9, 31), yesterday)
    assert [i["status"] for i in day["items"]] == ["missed"]
    track(db, lunch_day["device"], fixes(TRATTORIA, rome(2026, 10, 21, 13), rome(2026, 10, 21, 13, 50)))
    day = run(db, secrets_key, rome(2026, 10, 22, 9, 32), yesterday)
    assert [i["status"] for i in day["items"]] == ["done"]


# --- L6: the incremental refresh gives the same stays as a full one ----------------------------------------


def test_uploads_in_small_batches_give_the_stays_of_one_pass(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    t0 = datetime(2026, 10, 21, 8, tzinfo=UTC)
    points: list[Fix] = []
    for i, where in enumerate((CAFE, MUSEUM, CAFE, MUSEUM)):
        start = t0 + timedelta(minutes=50 * i)
        points += [Fix(where[0], where[1], 10, start + timedelta(minutes=m)) for m in range(0, 31, 3)]
        points.append(Fix(where[0] + 0.005, where[1], 10, start + timedelta(minutes=40)))  # on the move

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            for k in range(0, len(points), 4):
                await stays.save(conn, uuid.UUID(device), points[k : k + 4])

    asyncio.run(go())
    stored = db.execute("SELECT arrived_at, departed_at FROM stays ORDER BY arrived_at").fetchall()
    assert stored == [(s.arrived_at, s.departed_at) for s in detect(points)]
    assert len(stored) == 4


# --- options (contract N) ---------------------------------------------------------------------------------


def with_options(
    db: psycopg.Connection,
    secrets_key: str,
    action: Callable[[PlanOptionsService, Clock], Awaitable[Any]],
    *,
    now: datetime = NOW,
    fake: FakePlaces | None = None,
    keys: tuple[str, ...] = (),
) -> Any:
    async def go() -> Any:
        clock = Clock(now)
        async with place_env(dsn(db), secrets_key, fake or FakePlaces(), clock, keys=keys) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock)
            service = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(TZ), clock=env.clock)
            return await action(service, clock)

    return asyncio.run(go())


def sambalaya() -> FakePlaces:
    fake = FakePlaces()
    fake.apple_search = {"results": [apple_place("Toko Sambalaya", (52.344, 4.867), "Restaurant")]}
    return fake


def trips(plan: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (i.get("to_place_id"), i["start"], i["end"])
        for i in plan["items"]
        if i["kind"] in ("travel", "event")
    ]


def test_an_alternative_is_costed_at_its_own_place(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    """Toko Sambalaya is on the way to Spiraal: its option's trips, lateness and travel are the applied
    plan's."""

    async def go(service: PlanOptionsService, clock: Clock) -> tuple[dict[str, Any], dict[str, Any]]:
        got = await service.options({"event_id": trip_day["lunch"]}, "alternative")
        there = next(o for o in got["options"] if "Sambalaya" in o["summary"])
        return there, await service.apply(got["options_id"], there["id"])

    option, applied = with_options(db, secrets_key, go, fake=sambalaya(), keys=("apple",))
    assert trips(option["plan"]) == trips(applied)
    toko = next(i for i in applied["items"] if i.get("event_id") == trip_day["lunch"])
    leg = next(i for i in applied["items"] if i["kind"] == "travel" and i["to_place_id"] == toko["place_id"])
    assert leg["minutes"] < 8 and option["effects"]["extra_travel_min"] < 0  # nearer than Visbar, and says so
    assert option["why"].endswith("orari non noti.")  # Apple gives no hours (answer 5)


def test_one_options_set_applies_once(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    """A double tap (or voice and the app at once): one apply wins, the other finds the options gone."""

    async def go(service: PlanOptionsService, clock: Clock) -> tuple[Any, ...]:
        got = await service.options({"event_id": trip_day["lunch"]})
        first, second = (o["id"] for o in got["options"][:2])
        return await asyncio.gather(
            service.apply(got["options_id"], first),
            service.apply(got["options_id"], second),
            return_exceptions=True,
        )

    results = with_options(db, secrets_key, go)
    errors = [r for r in results if isinstance(r, PisaError)]
    assert len(errors) == 1 and errors[0].code == "options_expired", results
    assert db.execute("SELECT count(DISTINCT option_ref) FROM plan_adjustments").fetchone() == (1,)


def test_undo_removes_the_whole_option(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    async def go(service: PlanOptionsService, clock: Clock) -> tuple[list[dict[str, Any]], list[str]]:
        got = await service.options({"event_id": trip_day["lunch"]}, "alternative")
        there = next(o for o in got["options"] if "Sambalaya" in o["summary"])
        await service.apply(got["options_id"], there["id"])
        made = await service.adjustments()
        return made, await service.undo(made[-1]["id"])  # the second row of the two

    made, undone = with_options(db, secrets_key, go, fake=sambalaya(), keys=("apple",))
    assert [a["action"] for a in made] == ["move", "replace_place"]
    assert made[0]["option_ref"] == made[1]["option_ref"]
    assert undone == ["move", "replace_place"]
    assert db.execute("SELECT count(*) FROM plan_adjustments").fetchone() == (0,)


@pytest.mark.usefixtures("owner_plans_every_task")
def test_a_task_skipped_through_an_option_stays_on_the_day(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM calendar_events WHERE id = %s", (trip_day["lunch"],))
    gift = add_task(db, "Comprare un regalo", rome(2026, 10, 21).date())
    with_place(db, "tasks", gift, place(db, "Warenhuis Fictief", (52.358, 4.878)))

    async def go(service: PlanOptionsService, clock: Clock) -> tuple[dict[str, Any], dict[str, Any]]:
        planned = await service._planner.plan(reason="test")
        got = await service.options({"task_id": gift})
        skip = next(o for o in got["options"] if o["summary"].startswith("Salta"))
        return planned, await service.apply(got["options_id"], skip["id"])

    planned, applied = with_options(db, secrets_key, go)
    placed = next(i for i in planned["items"] if i.get("task_id") == gift)
    skipped = next(i for i in applied["items"] if i.get("task_id") == gift)
    assert skipped["status"] == "skipped" and (skipped["start"], skipped["end"]) == (
        placed["start"],
        placed["end"],
    )


def test_an_event_reached_after_its_end_is_dropped(db, secrets_key, trip_day) -> None:  # type: ignore[no-untyped-def]
    """Spiraal 14:40-15:10: lunch now (14:35-15:20) and Spiraal can't both be reached before they end: the one
    reached after its end is dropped, not "late"."""
    db.execute(
        "UPDATE calendar_events SET ends_at = %s WHERE id = %s",
        (rome(2026, 10, 21, 15, 10), trip_day["spiraal"]),
    )

    async def go(service: PlanOptionsService, clock: Clock) -> dict[str, Any]:
        return await service.options({"event_id": trip_day["lunch"]})

    got = with_options(db, secrets_key, go)
    now = next(o for o in got["options"] if o["summary"].startswith("Adesso"))
    assert len(now["effects"]["dropped"]) == 1 and now["effects"]["late_min"] == 0, now["effects"]


def test_tasks_are_compared_with_the_day_without_the_target() -> None:
    """A task the day as it is fits only through its lateness isn't "dropped" by skipping the target."""
    from pisa.planner.solver import Solution
    from pisa.services.plan_options import _effects
    from pisa.services.planner import Built, Entry

    errand = Entry("task", "t1", "Farmacia")
    lunch = Entry("event", "e1", "Pranzo", skipped=True)

    def built(*placed: str, events: tuple[Entry, ...] = ()) -> Built:
        solution = Solution("optimal", tasks={k: (0, 10) for k in placed})
        return Built(solution, [], {"t1": {"task": errand}}, list(events), [errand], [])

    as_is = built("t1", events=(replace(lunch, skipped=False),))
    skipped = built(events=(lunch,))  # without the lateness allowance, the errand doesn't fit
    day = SimpleNamespace(now=NOW, win=SimpleNamespace(start=NOW))
    assert _effects(day, as_is, skipped, skipped).dropped == ["Pranzo"]  # type: ignore[arg-type]
    moved = built(events=(replace(lunch, skipped=False),))
    assert _effects(day, as_is, skipped, moved).dropped == []  # type: ignore[arg-type]


def test_fit_offers_a_similar_place_when_nothing_fits_on_time(  # type: ignore[no-untyped-def]
    db, secrets_key, trip_day, monkeypatch
) -> None:
    from pisa.services import plan_options

    # what is offered, not how fast: under CI load the solve budget ran out and only "skip" came back
    monkeypatch.setattr(plan_options, "OPTIONS_SOLVE_S", 60.0)

    async def go(service: PlanOptionsService, clock: Clock) -> dict[str, Any]:
        return await service.options({"event_id": trip_day["lunch"]}, "fit")

    got = with_options(db, secrets_key, go, fake=sambalaya(), keys=("apple",))
    summaries = [o["summary"] for o in got["options"]]
    assert "Adesso da Toko Sambalaya" in summaries and "Salta Pranzo: Visbar Zilverschub" in summaries


def test_skip_is_offered_even_when_time_runs_out(db, secrets_key, trip_day, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import plan_options

    monkeypatch.setattr(plan_options, "OPTIONS_SOLVE_S", 0.0)  # the whole budget gone before the first solve

    async def go(service: PlanOptionsService, clock: Clock) -> dict[str, Any]:
        return await service.options({"event_id": trip_day["lunch"]})

    got = with_options(db, secrets_key, go)
    assert [o["summary"] for o in got["options"]] == ["Salta Pranzo: Visbar Zilverschub"]


def test_variants(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """L2: shortening needs the place open until the shorter end. L3: the hotel at the day's end is not the
    next stop. L4: a meal with no place moved to a time is its own block."""
    from pisa.services.plan_options import _next_stop, _variants
    from pisa.services.planner import _blocks

    hotel = place(db, "Hostel Fietsbel Amsterdam", (52.339, 4.876))
    db.execute("INSERT INTO calendars (id, name) VALUES ('primary', 'Primary') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, start_date, "
        "end_date, status, place_id) VALUES ('primary', 'stay', 'Hostel Fietsbel Amsterdam', %s, %s, true, "
        "'2026-10-20', '2026-10-24', 'confirmed', %s)",
        (rome(2026, 10, 20), rome(2026, 10, 24), hotel),
    )
    set_settings(db, {"language": "it-IT", "places": {"default_mode": "walk"}, "planner": {"buffer_min": 0}})
    dinner = add_event(db, "Cena", rome(2026, 10, 21, 20, 0), minutes=90)
    with_place(db, "calendar_events", dinner, place(db, "De Verzonnen Kas", (52.329, 4.879)))
    late = rome(2026, 10, 21, 20, 30)

    async def go(service: PlanOptionsService, clock: Clock) -> tuple[Any, ...]:
        day = await service._planner.gather(late, all_pairs=True)
        entry = next(e for e in day.events if e.key == dinner)
        return day, entry, _next_stop(day, entry)

    day, entry, nxt = with_options(db, secrets_key, go, now=late)
    assert any(e.key.startswith("base:") for e in day.events) and nxt is None  # L3

    # L2: a later dinner, with a next stop to leave for: shortening is offered, unless the place closes first.
    walk = replace(entry, start=rome(2026, 10, 21, 20, 0).astimezone(UTC))
    concert = replace(entry, key="c", title="Concerto", start=rome(2026, 10, 21, 21, 25).astimezone(UTC),
                      end=rome(2026, 10, 21, 23, 0).astimezone(UTC))  # fmt: skip
    shown = [v[0] for v in _variants(day, walk, concert, "fit", [], "it-IT")]
    assert "shorten" in shown
    closes = rome(2026, 10, 21, 21, 0).astimezone(UTC)
    day.places[entry.place_id] = {"windows": [(rome(2026, 10, 21, 12).astimezone(UTC), closes)]}
    day.places[entry.place_id]["windows"] = [(rome(2026, 10, 21, 12).astimezone(UTC), closes)]
    assert "shorten" not in [v[0] for v in _variants(day, walk, concert, "fit", [], "it-IT")]

    # L4: a lunch with no place moved to now, overlapping the next event: two blocks, not one.
    moved = Entry("event", "l", "Pranzo", rome(2026, 10, 21, 14).astimezone(UTC),
                  rome(2026, 10, 21, 15).astimezone(UTC), adjusted=True)  # fmt: skip
    museum = Entry("event", "f", "Spiraal", rome(2026, 10, 21, 14, 40).astimezone(UTC),
                   rome(2026, 10, 21, 16).astimezone(UTC), place_id="p", location=1)  # fmt: skip
    assert [b["key"] for b in _blocks([moved, museum])] == ["l", "f"]


# --- check-ins --------------------------------------------------------------------------------------------


def test_two_ticks_at_once_open_one_check_in(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 14, 4)))

    async def twice(follow, env):  # type: ignore[no-untyped-def]
        await follow._planner.plan(reason="test")
        return await asyncio.gather(follow.tick(), follow.tick())

    assert sorted(run(db, secrets_key, rome(2026, 10, 21, 14, 5), twice)) == [0, 1]
    assert db.execute("SELECT count(*) FROM check_ins").fetchone() == (1,)
    with pytest.raises(psycopg.errors.UniqueViolation):  # and the database refuses a second open one
        db.execute(
            "INSERT INTO check_ins (id, day, kind, event_id, title, text, choices, created_at) "
            "VALUES (%s, '2026-10-21', 'missed', 'x', 'X', 'x', '[]', now())",
            (uuid.uuid4(),),
        )


def test_two_answers_at_once_one_applies(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 13, 30)))
    run(db, secrets_key, rome(2026, 10, 21, 13, 31), plan_and_tick)
    check_in = db.execute("SELECT id::text FROM check_ins").fetchone()[0]  # type: ignore[index]

    async def both(follow, env):  # type: ignore[no-untyped-def]
        return await asyncio.gather(
            follow.answer(check_in, "skip"), follow.answer(check_in, "on_my_way"), return_exceptions=True
        )

    results = run(db, secrets_key, rome(2026, 10, 21, 13, 32), both)
    errors = [r for r in results if isinstance(r, PisaError)]
    assert [e.code for e in errors] == ["check_in_answered"], results
    (answer,) = db.execute("SELECT answer FROM check_ins").fetchone()  # type: ignore[misc]
    skips = db.execute("SELECT count(*) FROM plan_adjustments").fetchone()
    assert skips == ((1,) if answer == "skip" else (0,))


def test_a_stale_location_does_not_drop_the_check_in(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    """Missed lunch asked at 13:31; the user walks off (the last fix 17 minutes old, no stay): the location
    is unknown, the check-in stays open. It goes when the lunch is over by more than its own length."""
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 13, 30)))
    assert run(db, secrets_key, rome(2026, 10, 21, 13, 31), plan_and_tick) == 1
    walking = [Fix(HOTEL[0], HOTEL[1] - 0.004 * k, 10, rome(2026, 10, 21, 13, 32 + k)) for k in range(1, 4)]
    track(db, lunch_day["device"], walking)
    run(db, secrets_key, rome(2026, 10, 21, 13, 52), lambda f, env: f.tick())
    assert db.execute("SELECT dropped_at FROM check_ins").fetchone() == (None,)
    run(db, secrets_key, rome(2026, 10, 21, 15, 1), lambda f, env: f.tick())  # 14:00 + 60 min
    assert db.execute("SELECT dropped_at IS NOT NULL FROM check_ins").fetchone() == (True,)


def test_closing_the_socket_clears_companion_and_earbuds(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import presence
    from tests.test_follow import deliver_check_in, set_presence
    from tests.test_push import with_token

    device, _ = pair(db)
    with_token(db, device)
    set_presence(db, device, "app_background", "companion_on", "earbuds_connected")

    async def closed() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            await presence.reset(conn, uuid.UUID(device), datetime.now(UTC))

    asyncio.run(closed())
    row = db.execute("SELECT companion, earbuds FROM device_presence").fetchone()
    assert row == (None, None)
    _, apple = deliver_check_in(db, secrets_key, {device})  # a new socket, the state not sent yet
    assert len(apple.requests) == 1  # pushed as well: never only over a socket that may be dead


def test_an_apns_category_per_check_in_kind() -> None:
    from pisa.domain.follow import choices
    from pisa.services.notifications import _category

    assert _category("check_in", choices("missed", "it-IT")) == "CHECK_IN_MISSED"
    assert _category("check_in", choices("confirm", "it-IT")) == "CHECK_IN_CONFIRM"
    assert _category("reminder", None) == "NOTICE"  # Every other notice reports its dismissal
    assert [c["id"] for c in choices("missed", "en-US")] == ["reschedule", "alternative", "skip", "on_my_way",
                                                             "did_it"]  # fmt: skip


# --- the conversation, intake, the smoke device ------------------------------------------------------------


def test_skip_the_lunch_inside_a_longer_event(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """ "Golfslag Playground" 12:00-18:00 and "Pranzo" 13:30-14:15 are one block "Golfslag Playground /
    Pranzo": "salta il pranzo" skips the lunch, not Golfslag."""
    from tests.test_follow_tools import ask
    from tests.test_place_tools import data_of

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    ade = add_event(db, "Golfslag Playground", rome(2026, 10, 21, 12), minutes=360)
    lunch = add_event(db, "Pranzo", rome(2026, 10, 21, 13, 30), minutes=45)
    said = data_of(ask(db, secrets_key, "skip_item", {"item": "pranzo"}, now=rome(2026, 10, 21, 13)))
    assert said.startswith("Skipped.")
    assert db.execute("SELECT event_id, title FROM plan_adjustments").fetchall() == [(lunch, "Pranzo")]
    assert ade != lunch


def test_purge_refuses_any_device_but_the_smoke_one(db, app_settings, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from pisa.cli import main

    monkeypatch.setenv("PISA_DATABASE_URL", app_settings.database_url)
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level, **_: None)  # structlog stays
    phone, _ = pair(db, "iPhone")
    impostor, _ = pair(db, "smoke-test")  # an iOS device that happens to be called so
    old, _ = pair(db, "smoke-test", "other")
    db.execute("UPDATE devices SET created_at = now() - interval '25 hours' WHERE id = %s", (old,))
    for device in (phone, impostor, old):
        assert main(["devices", "purge", device]) == 1
        assert "is not the smoke test's throwaway device" in capsys.readouterr().err
    assert db.execute("SELECT count(*) FROM devices WHERE revoked_at IS NULL").fetchone() == (3,)
    smoke, _ = pair(db, "smoke-test", "other")
    assert main(["devices", "purge", smoke]) == 0
    assert db.execute("SELECT count(*) FROM devices").fetchone() == (3,)


def test_one_chunk_replies_and_model_time(db, secrets_key, trip_day, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from structlog.testing import capture_logs

    from pisa.services.assistant import AssistantDataService
    from tests.test_follow_tools import ask

    async def slow_context(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.3)
        return None

    async def model(messages, info):  # type: ignore[no-untyped-def]
        yield "Ok."

    monkeypatch.setattr(AssistantDataService, "context", slow_context)
    with capture_logs() as logs:
        ask(db, secrets_key, "", {}, model=model)
    (entry,) = [e for e in logs if e["event"] == "turn_latency"]
    assert entry["first_say_s"] is not None  # one sentence, said with the final chunk
    assert entry["context_s"] >= 0.3  # the slow context is measured apart, never as model time
    assert entry["model_s"] <= entry["total_s"] - entry["context_s"] + 0.002  # (each rounded to ms)


def test_the_plan_records_the_device_that_asked(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = NOW
    smoke, token = pair(db, "smoke-test", "other")
    assert client.get("/v1/plan", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert db.execute("SELECT requested_by::text FROM plans").fetchall() == [(smoke,)]
