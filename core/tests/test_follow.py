"""What really happened: statuses from fixed location tracks, check-ins (once per item, never two
open, quiet hours), their answers, the day so far, and contract P (companion mode: no double push)."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from pisa.db.pool import open_pool
from pisa.domain.follow import Item, Seen, status
from pisa.domain.stays import Fix
from pisa.infra.keys import KeyStore
from pisa.services import presence, stays
from pisa.services.follow import FollowService
from pisa.services.notifications import Dispatcher, enqueue
from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.push import PushService, set_apns_key
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, add_task, notifications, rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.places_env import PlaceEnv, place_env
from tests.places_fake import FakePlaces
from tests.test_planner import place, with_place
from tests.test_push import KEY_ID, TEAM_ID, TOKEN, TOPIC, Apple, Live, p8, with_token

TZ = "Europe/Rome"
TRATTORIA = (52.357, 4.868)
HOTEL = (52.330, 4.877)  # an invented point, about 3 km away


def fixes(where: tuple[float, float], start: datetime, end: datetime, every: int = 2) -> list[Fix]:
    out, t = [], start
    while t <= end:
        out.append(Fix(where[0], where[1], 15, t))
        t += timedelta(minutes=every)
    return out


# --- statuses (pure) --------------------------------------------------------------------------------------

LUNCH = Item("event", rome(2026, 10, 21, 13), rome(2026, 10, 21, 14), "p-lunch", TRATTORIA)
MIDNIGHT = rome(2026, 10, 21)


def seen(
    where: tuple[float, float], start: datetime, end: datetime | None, place_id: str | None = None
) -> Seen:
    return Seen(start, end, where[0], where[1], 15, place_id)


def test_went_to_lunch() -> None:
    there = [seen(TRATTORIA, rome(2026, 10, 21, 13, 5), rome(2026, 10, 21, 13, 50))]
    now = rome(2026, 10, 21, 14, 10)
    assert status(LUNCH, there, now=now, here=HOTEL, trip_min=18, day_start=MIDNIGHT) == "done"
    still = [seen(TRATTORIA, rome(2026, 10, 21, 13, 5), None)]
    at_1330 = rome(2026, 10, 21, 13, 30)
    assert status(LUNCH, still, now=at_1330, here=TRATTORIA, trip_min=0, day_start=MIDNIGHT) == "ongoing"


def test_walked_past_it_never_went_and_late_but_on_the_way() -> None:
    # Passing by is no stay (under 5 minutes): after lunch ended, 3 km away, it was missed.
    assert (
        status(LUNCH, [], now=rome(2026, 10, 21, 14, 5), here=HOTEL, trip_min=18, day_start=MIDNIGHT)
        == "missed"
    )
    # 13:20, 18 minutes away with 40 left: on the way, not missed yet.
    at_1320 = rome(2026, 10, 21, 13, 20)
    assert status(LUNCH, [], now=at_1320, here=HOTEL, trip_min=18, day_start=MIDNIGHT) == "planned"
    # 13:30, 18 minutes away with 30 left: more than half of what is left: missed.
    at_1330 = rome(2026, 10, 21, 13, 30)
    assert status(LUNCH, [], now=at_1330, here=HOTEL, trip_min=18, day_start=MIDNIGHT) == "missed"
    # Before 13:15 nothing is missed; within 150 m nothing is missed.
    assert (
        status(LUNCH, [], now=rome(2026, 10, 21, 13, 14), here=HOTEL, trip_min=60, day_start=MIDNIGHT)
        == "planned"
    )
    near = (TRATTORIA[0] + 0.001, TRATTORIA[1])  # ~110 m
    assert status(LUNCH, [], now=at_1330, here=near, trip_min=2, day_start=MIDNIGHT) == "planned"
    # A stay at the place from 30 minutes before the start counts as having been there.
    early = [seen(TRATTORIA, rome(2026, 10, 21, 12, 35), rome(2026, 10, 21, 12, 45))]
    assert (
        status(LUNCH, early, now=rome(2026, 10, 21, 14, 5), here=HOTEL, trip_min=18, day_start=MIDNIGHT)
        == "planned"
    )


def test_a_short_visit_is_done_only_at_half_or_20_minutes() -> None:
    long = Item(
        "event", rome(2026, 10, 21, 10), rome(2026, 10, 21, 14), "p", TRATTORIA
    )  # 4 h: 20 min is enough
    twenty = [seen(TRATTORIA, rome(2026, 10, 21, 10, 0), rome(2026, 10, 21, 10, 20))]
    later = rome(2026, 10, 21, 14, 30)
    assert status(long, twenty, now=later, here=HOTEL, trip_min=18, day_start=MIDNIGHT) == "done"
    short = Item("event", rome(2026, 10, 21, 10), rome(2026, 10, 21, 10, 30), "p", TRATTORIA)  # half = 15 min
    ten = [seen(TRATTORIA, rome(2026, 10, 21, 10, 0), rome(2026, 10, 21, 10, 10))]
    assert status(short, ten, now=later, here=HOTEL, trip_min=18, day_start=MIDNIGHT) != "done"


def test_a_tasks_place_visited_means_ask() -> None:
    errand = Item("task", rome(2026, 10, 21, 16), rome(2026, 10, 21, 16, 30), "p-post", TRATTORIA)
    there = [seen(TRATTORIA, rome(2026, 10, 21, 11), rome(2026, 10, 21, 11, 20))]
    assert (
        status(errand, there, now=rome(2026, 10, 21, 12), here=HOTEL, trip_min=18, day_start=MIDNIGHT)
        == "visited"
    )


# --- the services -----------------------------------------------------------------------------------------


def run(
    db: psycopg.Connection,
    secrets_key: str,
    now: datetime,
    action: Callable[[FollowService, PlaceEnv], Awaitable[Any]],
) -> Any:
    async def go() -> Any:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock)
            options = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(TZ), clock=env.clock)
            follow = FollowService(
                env.pool, planner, options, tasks, env.places, owner=owner_at(TZ), clock=env.clock
            )
            return await action(follow, env)

    return asyncio.run(go())


def track(db: psycopg.Connection, device: str, points: list[Fix]) -> None:
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            await stays.save(conn, uuid.UUID(device), points)

    asyncio.run(go())


@pytest.fixture
def lunch_day(db: psycopg.Connection) -> dict[str, str]:
    return make_lunch_day(db)


def make_lunch_day(db: psycopg.Connection) -> dict[str, str]:
    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}, "places": {"default_mode": "transit"}})
    trattoria = place(db, "Trattoria", TRATTORIA)
    lunch = add_event(db, "Pranzo", rome(2026, 10, 21, 13), minutes=60)
    with_place(db, "calendar_events", lunch, trattoria)
    device, _ = pair(db)
    return {"lunch": lunch, "device": device, "trattoria": trattoria}


async def plan_and_tick(follow: FollowService, env: PlaceEnv) -> int:
    await follow._planner.plan(reason="test")
    return await follow.tick()


def test_a_missed_lunch_asks_once(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 14, 4)))
    assert run(db, secrets_key, rome(2026, 10, 21, 14, 5), plan_and_tick) == 1
    (row,) = notifications(db)
    kind, text = row[0], row[1]
    assert (kind, text) == (
        "check_in",
        "Sembra che tu non sia andato a Pranzo. Lo sposto, cerco un altro posto o lo salto?",
    )
    stored = db.execute("SELECT check_in_id, choices, expects_reply FROM notifications").fetchone()
    assert stored is not None and stored[2] is True
    assert [c["label"] for c in stored[1]] == ["Sposta", "Alternativa", "Salta", "Sto arrivando", "Fatto"]
    assert run(db, secrets_key, rome(2026, 10, 21, 14, 6), lambda f, env: f.tick()) == 0  # one open already
    db.execute("UPDATE check_ins SET answered_at = now(), answer = 'did_it'")
    assert (
        run(db, secrets_key, rome(2026, 10, 21, 14, 7), lambda f, env: f.tick()) == 0
    )  # once per item per day


def test_no_check_ins_when_switched_off_or_in_quiet_hours(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 14, 4)))
    set_settings(db, {"planner": {"check_ins": False}})
    assert run(db, secrets_key, rome(2026, 10, 21, 14, 5), plan_and_tick) == 0
    set_settings(db, {"planner": {"check_ins": True}, "quiet": {"hours": {"start": "14:00", "end": "15:00"}}})
    assert run(db, secrets_key, rome(2026, 10, 21, 14, 10), plan_and_tick) == 0  # deferred
    # When quiet ends, only what is still relevant is asked: here the user went after all.
    track(db, lunch_day["device"], fixes(TRATTORIA, rome(2026, 10, 21, 14, 20), rome(2026, 10, 21, 14, 59)))
    assert run(db, secrets_key, rome(2026, 10, 21, 15, 0), plan_and_tick) == 0
    assert db.execute("SELECT count(*) FROM check_ins").fetchone() == (0,)


def test_an_open_check_in_is_dropped_when_no_longer_relevant(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 13, 30)))
    assert run(db, secrets_key, rome(2026, 10, 21, 13, 31), plan_and_tick) == 1
    track(db, lunch_day["device"], fixes(TRATTORIA, rome(2026, 10, 21, 13, 40), rome(2026, 10, 21, 13, 59)))
    run(db, secrets_key, rome(2026, 10, 21, 14, 0), lambda f, env: f.tick())  # the user got there after all
    assert db.execute("SELECT dropped_at IS NOT NULL FROM check_ins").fetchone() == (True,)
    open_ = run(db, secrets_key, rome(2026, 10, 21, 14, 1), lambda f, env: f.check_ins("open"))
    assert open_ == []


def test_answers(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 13, 30)))
    run(db, secrets_key, rome(2026, 10, 21, 13, 31), plan_and_tick)
    ids = [db.execute("SELECT id::text FROM check_ins").fetchone()[0]]  # type: ignore[index]

    def answer(choice: str) -> Callable[[FollowService, PlaceEnv], Awaitable[Any]]:
        async def go(follow: FollowService, env: PlaceEnv) -> Any:
            return await follow.answer(ids[-1], choice)

        return go

    from pisa.domain.errors import PisaError

    with pytest.raises(PisaError) as bad:
        run(db, secrets_key, rome(2026, 10, 21, 13, 32), (answer("teleport")))
    assert bad.value.code == "validation_failed"
    out = run(db, secrets_key, rome(2026, 10, 21, 13, 32), (answer("on_my_way")))
    assert out["check_in"]["answer"] == "on_my_way" and out["plan"] is None and out["options"] is None
    with pytest.raises(PisaError) as again:
        run(db, secrets_key, rome(2026, 10, 21, 13, 33), (answer("skip")))
    assert again.value.code == "check_in_answered"
    # Asked again 15 minutes later, still missed; this time: skip.
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 13, 32), rome(2026, 10, 21, 13, 46)))
    assert run(db, secrets_key, rome(2026, 10, 21, 13, 46), lambda f, env: f.tick()) == 0  # not yet
    assert run(db, secrets_key, rome(2026, 10, 21, 13, 48), lambda f, env: f.tick()) == 1
    ids.append(db.execute("SELECT id::text FROM check_ins WHERE answered_at IS NULL").fetchone()[0])  # type: ignore[index]
    skipped = run(db, secrets_key, rome(2026, 10, 21, 13, 49), (answer("skip")))
    lunch = next(i for i in skipped["plan"]["items"] if i.get("event_id") == lunch_day["lunch"])
    assert lunch["status"] == "skipped"
    assert db.execute("SELECT action, source FROM plan_adjustments").fetchall() == [("skip", "check_in")]


def test_reschedule_gives_options_and_did_it_marks_done(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 13, 30)))
    run(db, secrets_key, rome(2026, 10, 21, 13, 31), plan_and_tick)
    check_in = db.execute("SELECT id::text FROM check_ins").fetchone()[0]  # type: ignore[index]
    out = run(db, secrets_key, rome(2026, 10, 21, 13, 32), lambda f, env: f.answer(check_in, "reschedule"))
    assert out["options"]["target_title"] == "Pranzo" and out["options"]["options"]
    db.execute("UPDATE check_ins SET answered_at = NULL, answer = NULL")
    done = run(db, secrets_key, rome(2026, 10, 21, 13, 33), lambda f, env: f.answer(check_in, "did_it"))
    lunch = next(i for i in done["plan"]["items"] if i.get("event_id") == lunch_day["lunch"])
    assert lunch["status"] == "done"


@pytest.mark.usefixtures("owner_plans_every_task")
def test_a_visited_errand_asks_to_confirm_and_done_completes_it(db, secrets_key, lunch_day) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM calendar_events")
    poste = place(db, "Poste Italiane", TRATTORIA)
    task = add_task(db, "Ritirare il pacco", rome(2026, 10, 21).date())
    with_place(db, "tasks", task, poste)
    track(db, lunch_day["device"], fixes(TRATTORIA, rome(2026, 10, 21, 9, 0), rome(2026, 10, 21, 9, 20)))
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 9, 40), rome(2026, 10, 21, 9, 59)))
    assert run(db, secrets_key, rome(2026, 10, 21, 10, 0), plan_and_tick) == 1
    kind, text = db.execute("SELECT kind, text FROM check_ins").fetchone()  # type: ignore[misc]
    assert (kind, text) == ("confirm", "Sei passato da Poste Italiane: hai fatto «Ritirare il pacco»?")
    check_in = db.execute("SELECT id::text FROM check_ins").fetchone()[0]  # type: ignore[index]
    run(db, secrets_key, rome(2026, 10, 21, 10, 1), lambda f, env: f.answer(check_in, "done"))
    assert db.execute("SELECT status FROM tasks").fetchone() == ("done",)


def test_the_day_so_far(client, db, clock, http_handler, lunch_day) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = FakePlaces()
    clock.now = rome(2026, 10, 21, 14, 10)
    track(db, lunch_day["device"], fixes(TRATTORIA, rome(2026, 10, 21, 13, 5), rome(2026, 10, 21, 13, 50)))
    track(db, lunch_day["device"], fixes(HOTEL, rome(2026, 10, 21, 14, 0), rome(2026, 10, 21, 14, 9)))
    auth = {"Authorization": f"Bearer {pair(db, 'iPad')[1]}"}
    db.execute("UPDATE calendar_events SET ends_at = %s", (rome(2026, 10, 21, 15),))  # still in the plan
    assert client.post("/v1/plan", headers=auth).status_code == 201
    day = client.get("/v1/day", headers=auth, params={"date": "2026-10-21"}).json()
    assert next((s["name"], s["arrived_at"]) for s in day["stays"]) == (
        "Trattoria",
        "2026-10-21T13:05:00+02:00",
    )
    assert day["items"] == [
        {"kind": "event", "event_id": lunch_day["lunch"], "title": "Pranzo", "status": "done"}
    ]
    listed = client.get("/v1/check-ins", headers=auth, params={"status": "all"})
    assert listed.status_code == 200 and listed.json() == {"check_ins": []}
    missing = client.post(f"/v1/check-ins/{uuid.uuid4()}/answer", headers=auth, json={"choice": "skip"})
    assert missing.status_code == 404


# --- contract P: companion mode -------------------------------------------------------------------------


def deliver_check_in(db: psycopg.Connection, secrets_key: str, online: set[str]) -> tuple[Live, Apple]:
    apple, live = Apple(), Live(online)
    keys = KeyStore(SecretStr(secrets_key))
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

    async def go() -> None:
        pool = await open_pool(dsn(db), max_size=3)
        await pool.wait()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(apple)) as http:
                push = PushService(pool, keys, http, team_id=TEAM_ID, topic=TOPIC)
                async with pool.connection() as conn:
                    await set_apns_key(conn, keys, KEY_ID, p8()[0])
                    notification_id, _ = await enqueue(
                        conn, kind="check_in", text="Hai saltato il pranzo?", lang="it-IT", now=now,
                        timezone=TZ, check_in_id=uuid.uuid4(), choices=[{"id": "skip", "label": "Salta"}],
                    )  # fmt: skip
                await Dispatcher(pool, live, push, clock=Clock(now), owner=owner_at()).deliver(
                    notification_id
                )
        finally:
            await pool.close()

    asyncio.run(go())
    return live, apple


def set_presence(db: psycopg.Connection, device: str, *events: str) -> None:
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            for event in events:
                await presence.update(conn, uuid.UUID(device), event, datetime.now(UTC))

    asyncio.run(go())


def test_companion_mode_with_earbuds_means_no_push(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    with_token(db, device)
    set_presence(db, device, "app_background", "companion_on", "earbuds_connected")
    live, apple = deliver_check_in(db, secrets_key, {device})
    ((_, sent),) = live.sent
    assert (sent.kind, sent.expects_reply, sent.choices) == (
        "check_in",
        True,
        [{"id": "skip", "label": "Salta"}],
    )
    assert apple.requests == []  # spoken in the earbuds: no push as well
    db.execute("DELETE FROM notifications")
    set_presence(db, device, "earbuds_disconnected")
    live, apple = deliver_check_in(db, secrets_key, {device})
    (request,) = apple.requests  # backgrounded without earbuds: the push too, as before
    body = json.loads(request.content)
    assert body["aps"]["category"] == "CHECK_IN_MISSED" and body["choices"] == [
        {"id": "skip", "label": "Salta"}
    ]
    assert body["check_in_id"] and body["kind"] == "check_in"


def test_the_socket_down_still_pushes_in_companion_mode(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    with_token(db, device, TOKEN)
    set_presence(db, device, "companion_on", "earbuds_connected")
    live, apple = deliver_check_in(db, secrets_key, set())
    assert live.sent == [] and len(apple.requests) == 1


# --- Following the day: scenarios as location tracks (fixes → stays → status) ------------------------------


def status_from_track(points: list[Fix], now: datetime, here: tuple[float, float]) -> str:
    from pisa.domain.stays import detect, link

    found = detect(points)
    seen_ = [
        Seen(s.arrived_at, s.departed_at, s.lat, s.lon, s.accuracy_m, link(s, [("p-lunch", *TRATTORIA)]))
        for s in found
    ]
    from pisa.services.planner import _estimate

    return status(
        LUNCH, seen_, now=now, here=here, trip_min=_estimate(here, TRATTORIA, "transit"), day_start=MIDNIGHT
    )


def test_tracks_went_walked_past_never_went_late_but_on_the_way() -> None:
    near = (TRATTORIA[0], TRATTORIA[1] + 0.0060)  # ~400 m along the street
    went = fixes(HOTEL, rome(2026, 10, 21, 12, 30), rome(2026, 10, 21, 12, 50)) + fixes(
        TRATTORIA, rome(2026, 10, 21, 13, 2), rome(2026, 10, 21, 13, 50)
    )
    assert status_from_track(went, rome(2026, 10, 21, 14, 10), TRATTORIA) == "done"
    walked_past = (
        fixes(near, rome(2026, 10, 21, 13, 0), rome(2026, 10, 21, 13, 10))
        + fixes(TRATTORIA, rome(2026, 10, 21, 13, 12), rome(2026, 10, 21, 13, 14))  # two minutes: no stay
        + fixes(HOTEL, rome(2026, 10, 21, 13, 30), rome(2026, 10, 21, 14, 5))
    )
    assert status_from_track(walked_past, rome(2026, 10, 21, 14, 5), HOTEL) == "missed"
    never = fixes(HOTEL, rome(2026, 10, 21, 12, 0), rome(2026, 10, 21, 13, 40))
    assert status_from_track(never, rome(2026, 10, 21, 13, 40), HOTEL) == "missed"
    on_the_way = [
        *fixes(HOTEL, rome(2026, 10, 21, 12, 0), rome(2026, 10, 21, 13, 10)),
        Fix(52.353, 4.874, 20, rome(2026, 10, 21, 13, 18)),  # ~700 m out, 13:20: a few minutes left to walk
    ]
    assert status_from_track(on_the_way, rome(2026, 10, 21, 13, 20), (52.353, 4.874)) == "planned"
