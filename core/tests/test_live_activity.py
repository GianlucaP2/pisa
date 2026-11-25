"""The Live Activity "Next" (contract T): its tokens, the content-state from the latest plan, and the APNs
`liveactivity` pushes: start once at the day's start, updates on a change (priority 10 for a new stop or leave
time, else 5) at most once a minute, the end at the day's end; a dead token is dropped."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from pisa.domain import live_activity as rules
from pisa.infra.keys import KeyStore
from pisa.services.follow import FollowService
from pisa.services.live_activity import LiveActivityService
from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.push import PushService, set_apns_key
from pisa.services.tasks import TasksService
from tests.assistant_env import rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.places_env import place_env
from tests.places_fake import FakePlaces
from tests.test_planner import place
from tests.test_push import KEY_ID, TEAM_ID, TOPIC, Apple, p8, with_token

UPDATE = "cd" * 40
START = "ef" * 40


def plan(
    museum: str, leave: datetime, *, minutes: int = 11, title: str = "Golfslag · Pennenmuseum"
) -> dict[str, Any]:
    start = rome(2026, 10, 22, 15)
    return {
        "date": "2026-10-22",
        "items": [
            {"kind": "travel", "start": leave.isoformat(),
             "end": (leave + timedelta(minutes=minutes + 10)).isoformat(),
             "title": f"{minutes} min on foot", "mode": "walk", "minutes": minutes, "provider": "estimate",
             "to_place_id": museum, "leave_at": leave.isoformat()},
            {"kind": "event", "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
             "title": title, "event_id": "e1", "event_ids": ["e1"], "place_id": museum, "status": "planned"},
        ],
        "unplanned": [],
    }  # fmt: skip


def store_plan(db: psycopg.Connection, body: dict[str, Any], at: datetime) -> None:
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (%s, '2026-10-22', %s, "
        "'test', "
        "'f', %s)",
        (uuid.uuid4(), at, Jsonb(body)),
    )


def tick(db: psycopg.Connection, secrets_key: str, now: datetime, apple: Apple) -> int:
    async def go() -> int:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), keys=()) as env:
            keys = KeyStore(SecretStr(secrets_key))
            async with env.pool.connection() as conn:
                await set_apns_key(conn, keys, KEY_ID, p8()[0])
            async with httpx.AsyncClient(transport=httpx.MockTransport(apple)) as http:
                push = PushService(env.pool, keys, http, team_id=TEAM_ID, topic=TOPIC)
                tasks = TasksService(env.pool, owner=owner_at(), clock=env.clock)
                planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(), clock=env.clock)
                options = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(), clock=env.clock)
                follow = FollowService(env.pool, planner, options, tasks, env.places, owner=owner_at(),
                                       clock=env.clock)  # fmt: skip
                service = LiveActivityService(
                    env.pool, push, planner, follow, owner=owner_at(), clock=env.clock
                )
                return await service.tick()

    return asyncio.run(go())


def pushes(apple: Apple) -> list[tuple[dict[str, str], dict[str, Any]]]:
    return [(dict(r.headers), json.loads(r.content)) for r in apple.requests]


def device_with(db: psycopg.Connection, *tokens: tuple[str, str]) -> str:
    device, _ = pair(db)
    with_token(db, device)
    for kind, token in tokens:
        db.execute(
            "INSERT INTO device_live_activity (device_id, kind, token, updated_at) VALUES (%s, %s, %s, "
            "now())",
            (device, kind, token),
        )
    return device


def test_the_tokens(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    url = "/v1/devices/me/live-activity"
    assert client.put(url, headers=auth, json={"token": UPDATE.upper(), "kind": "update"}).status_code == 204
    assert client.put(url, headers=auth, json={"token": START, "kind": "start"}).status_code == 204
    rows = db.execute("SELECT kind, token FROM device_live_activity ORDER BY kind").fetchall()
    assert rows == [("start", START), ("update", UPDATE)]
    bad = client.put(url, headers=auth, json={"token": "not hex", "kind": "update"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_failed"
    for length, status in ((63, 400), (64, 204), (512, 204), (513, 400)):  # the push contract, as the relay
        body = {"token": ("ef" * 300)[:length], "kind": "start"}
        assert client.put(url, headers=auth, json=body).status_code == status, length
    client.put(url, headers=auth, json={"token": START, "kind": "start"})
    assert client.delete(url, headers=auth, params={"kind": "update"}).status_code == 204
    assert db.execute("SELECT kind FROM device_live_activity").fetchall() == [("start",)]


def test_updates_go_out_on_a_change_at_most_once_a_minute(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    museum = place(db, "Pennenmuseum", (52.354, 4.852))
    db.execute("UPDATE places SET address = 'Fantasieplein 6, 1071 DJ Amsterdam' WHERE id = %s", (museum,))
    device_with(db, ("update", UPDATE))
    now = rome(2026, 10, 22, 14, 30)
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 39)), now - timedelta(minutes=1))
    apple = Apple()
    assert tick(db, secrets_key, now, apple) == 1
    ((headers, body),) = pushes(apple)
    assert headers["apns-push-type"] == "liveactivity" and headers["apns-priority"] == "10"
    assert headers["apns-topic"] == f"{TOPIC}.push-type.liveactivity"
    assert body["aps"]["event"] == "update" and body["aps"]["timestamp"] == int(now.timestamp())
    assert body["aps"]["content-state"] == {
        "kind": "event", "title": "Golfslag · Pennenmuseum", "start": "2026-10-22T15:00:00+02:00",
        "end": "2026-10-22T16:00:00+02:00", "place": "Fantasieplein 6",
        "leave_at": "2026-10-22T14:39:00+02:00", "minutes": 11, "mode": "walk", "status": "planned",
        "late_min": 0,
    }  # fmt: skip
    assert apple.requests[0].url.path == f"/3/device/{UPDATE}"
    assert tick(db, secrets_key, now + timedelta(minutes=1), apple) == 0  # nothing changed
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 39), minutes=12), now + timedelta(minutes=1))
    assert tick(db, secrets_key, now + timedelta(minutes=2), apple) == 1
    assert pushes(apple)[-1][0]["apns-priority"] == "5"  # the same stop and leave time: 5
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 45), minutes=12), now + timedelta(minutes=2))
    assert tick(db, secrets_key, now + timedelta(minutes=2, seconds=30), apple) == 0  # within the minute
    assert tick(db, secrets_key, now + timedelta(minutes=3), apple) == 1
    assert pushes(apple)[-1][0]["apns-priority"] == "10"  # a new leave time: 10


def test_start_once_at_the_days_start_and_end_at_its_end(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    museum = place(db, "Pennenmuseum", (52.354, 4.852))
    device = device_with(db, ("start", START))
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 39)), rome(2026, 10, 22, 7, 50))
    apple = Apple()
    assert tick(db, secrets_key, rome(2026, 10, 22, 7, 59), apple) == 0  # before the day starts (08:00)
    assert tick(db, secrets_key, rome(2026, 10, 22, 8, 0), apple) == 1
    body = pushes(apple)[0][1]
    assert apple.requests[0].url.path == f"/3/device/{START}"
    assert body["aps"]["event"] == "start" and body["aps"]["attributes-type"] == "NextStopAttributes"
    assert body["aps"]["attributes"] == {} and body["aps"]["input-push-token"] == 1
    assert body["aps"]["content-state"]["title"] == "Golfslag · Pennenmuseum"
    assert tick(db, secrets_key, rome(2026, 10, 22, 9, 0), apple) == 0  # once a day
    db.execute(
        "INSERT INTO device_live_activity (device_id, kind, token, updated_at) VALUES (%s, 'update', %s, "
        "now())",
        (device, UPDATE),
    )  # the app registered the running activity's token
    assert tick(db, secrets_key, rome(2026, 10, 22, 21, 0), apple) == 1  # the day's end (21:00)
    body = pushes(apple)[-1][1]
    assert body["aps"]["event"] == "end" and "dismissal-date" in body["aps"]
    assert body["aps"]["content-state"]["kind"] == "none"  # nothing left of the day
    assert db.execute("SELECT kind FROM device_live_activity").fetchall() == [("start",)]
    assert tick(db, secrets_key, rome(2026, 10, 22, 21, 5), apple) == 0


def test_a_dead_token_is_dropped(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    museum = place(db, "Pennenmuseum", (52.354, 4.852))
    device_with(db, ("update", UPDATE))
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 39)), rome(2026, 10, 22, 14))
    apple = Apple()
    apple.answers.append((410, "Unregistered"))
    assert tick(db, secrets_key, rome(2026, 10, 22, 14, 30), apple) == 0
    assert db.execute("SELECT count(*) FROM device_live_activity").fetchone() == (0,)


def test_the_next_item() -> None:
    """Skipped and done items are passed over; a late trip says how late; nothing left: none."""
    end = rome(2026, 10, 22, 21)
    items = [
        {"kind": "event", "title": "Pranzo", "start": rome(2026, 10, 22, 13).isoformat(),
         "end": rome(2026, 10, 22, 14).isoformat(), "status": "skipped"},
        {"kind": "travel", "start": rome(2026, 10, 22, 14, 50).isoformat(),
         "end": rome(2026, 10, 22, 15, 12).isoformat(), "minutes": 22, "mode": "transit",
         "leave_at": rome(2026, 10, 22, 14, 50).isoformat()},
        {"kind": "event", "title": "Spiraal", "start": rome(2026, 10, 22, 15).isoformat(),
         "end": rome(2026, 10, 22, 16).isoformat(), "status": "planned", "place_id": "p"},
    ]  # fmt: skip
    state = rules.content_state(items, rome(2026, 10, 22, 13, 30), {"p": "Denkbeeldgracht 1"}, "en-US", end)
    assert (state["title"], state["late_min"], state["mode"]) == ("Spiraal", 12, "transit")
    assert state["place"] == "Denkbeeldgracht 1"
    none = rules.content_state(items, rome(2026, 10, 22, 16, 30), {}, "it-IT", end)
    assert (none["kind"], none["title"], none["start"]) == ("none", "Niente altro oggi", end.isoformat())
    assert rules.priority(state, state | {"late_min": 3}) == 5
    assert rules.priority(state, state | {"leave_at": "x"}) == 10 and rules.priority(None, state) == 10


def test_the_throttle_allows_a_drifting_minute_tick(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """LOW: ticks a little under 60 s apart still update each minute (not every other one)."""
    museum = place(db, "Pennenmuseum", (52.354, 4.852))
    device_with(db, ("update", UPDATE))
    now = rome(2026, 10, 22, 14, 30)
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 39)), now - timedelta(minutes=1))
    apple = Apple()
    assert tick(db, secrets_key, now, apple) == 1
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 45)), now)
    assert tick(db, secrets_key, now + timedelta(seconds=59, milliseconds=500), apple) == 1


def test_a_dismissed_activity_stays_off_an_ended_one_starts_again(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """M19 (verification): dismissed by the user → off for the rest of the day (it came back every minute,
    with an alert); ended by iOS (the 8-hour limit) → started again once, without an alert."""
    museum = place(db, "Pennenmuseum", (52.354, 4.852))
    device, token = pair(db)
    with_token(db, device)
    auth = {"Authorization": f"Bearer {token}"}
    url = "/v1/devices/me/live-activity"
    assert client.put(url, headers=auth, json={"token": START, "kind": "start"}).status_code == 204
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 39)), rome(2026, 10, 22, 7, 50))
    apple = Apple()
    assert tick(db, secrets_key, rome(2026, 10, 22, 9, 0), apple) == 1  # started, with the day's alert
    first = pushes(apple)[-1][1]["aps"]
    assert first["event"] == "start" and "alert" in first and first["stale-date"] > first["timestamp"]
    assert client.put(url, headers=auth, json={"token": UPDATE, "kind": "update"}).status_code == 204
    assert tick(db, secrets_key, rome(2026, 10, 22, 9, 1), apple) == 0  # running, nothing changed
    assert client.delete(url, headers=auth, params={"kind": "update"}).status_code == 204  # dismissed
    for minute in (2, 3, 30):
        assert tick(db, secrets_key, rome(2026, 10, 22, 9, minute), apple) == 0  # stays off today
    assert client.put(url, headers=auth, json={"token": UPDATE, "kind": "update"}).status_code == 204
    params = {"kind": "update", "reason": "ended"}
    assert client.delete(url, headers=auth, params=params).status_code == 204  # iOS ended it
    assert tick(db, secrets_key, rome(2026, 10, 22, 9, 31), apple) == 1
    again = pushes(apple)[-1][1]["aps"]
    assert again["event"] == "start" and "alert" not in again
    assert tick(db, secrets_key, rome(2026, 10, 22, 9, 32), apple) == 0
