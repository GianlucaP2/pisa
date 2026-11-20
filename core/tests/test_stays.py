"""Stays from the location stream (contract O): the pure detection, the REST upload with its
repeats and late departures, the WebSocket path, an ongoing stay as "here", and retention."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import psycopg

from pisa.domain.stays import Fix, Stay, Visit, detect, link
from tests.conftest import Clock, FakeModels, dsn, pair
from tests.places_env import place_env
from tests.places_fake import FakePlaces

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
CAFE = (52.358, 4.873)
MUSEUM = (52.354, 4.852)  # about 2 km away


def track(
    start: datetime, where: tuple[float, float], minutes: int, *, every: int = 1, acc: float = 10
) -> list[Fix]:
    """A fix every `every` minutes for `minutes`, a few metres of jitter."""
    return [
        Fix(where[0] + (0.00003 if i % 2 else -0.00003), where[1], acc, start + timedelta(minutes=i))
        for i in range(0, minutes + 1, every)
    ]


# --- detection (pure) -------------------------------------------------------------------------------------


def test_five_minutes_within_75_m_is_a_stay() -> None:
    fixes = track(T0, CAFE, 20) + track(T0 + timedelta(minutes=30), MUSEUM, 4)  # 4 min: not a stay yet
    (stay,) = detect(fixes)
    assert (stay.arrived_at, stay.departed_at) == (T0, T0 + timedelta(minutes=20))
    assert abs(stay.lat - CAFE[0]) < 0.0001


def test_a_stay_with_no_fix_after_it_is_ongoing() -> None:
    (stay,) = detect(track(T0, CAFE, 12))
    assert stay.departed_at is None and stay.last_seen_at == T0 + timedelta(minutes=12)


def test_gps_noise_at_2_3_km_makes_no_stays() -> None:
    """A phone with Precise Location off: ~2.3 km accuracy, wandering; nothing counts."""
    noisy = [
        Fix(
            CAFE[0] + 0.01 * ((i * 7) % 5 - 2),
            CAFE[1] + 0.01 * ((i * 3) % 5 - 2),
            2300,
            T0 + timedelta(minutes=i),
        )
        for i in range(120)
    ]
    assert detect(noisy) == []
    still = [Fix(CAFE[0], CAFE[1], 2300, T0 + timedelta(minutes=i)) for i in range(60)]
    assert detect(still) == []  # even standing still: accuracy over 150 m never counts


def test_a_stray_fix_does_not_split_a_stay() -> None:
    fixes = [*track(T0, CAFE, 10), Fix(MUSEUM[0], MUSEUM[1], 20, T0 + timedelta(minutes=11))]
    fixes += track(T0 + timedelta(minutes=12), CAFE, 15)
    (stay,) = detect(fixes)
    assert stay.arrived_at == T0 and stay.departed_at is None


def test_visits_count_directly_and_merge_with_fixes() -> None:
    visit = Visit(CAFE[0], CAFE[1], 40, T0 - timedelta(minutes=3), T0 + timedelta(minutes=25))
    (stay,) = detect(track(T0, CAFE, 20) + track(T0 + timedelta(minutes=40), MUSEUM, 2), [visit])
    assert (stay.arrived_at, stay.departed_at) == (T0 - timedelta(minutes=3), T0 + timedelta(minutes=25))
    lone = Visit(MUSEUM[0], MUSEUM[1], 60, T0, None)  # no departure yet: ongoing
    assert detect([], [lone]) == [Stay(T0, None, MUSEUM[0], MUSEUM[1], 60, T0)]


def test_a_stay_links_to_the_nearest_place_within_max_75_m_and_its_accuracy() -> None:
    stay = Stay(T0, None, CAFE[0], CAFE[1], 10, T0)
    near = ("near", CAFE[0] + 0.0005, CAFE[1])  # ~56 m
    nearer = ("nearer", CAFE[0] + 0.0002, CAFE[1])  # ~22 m
    far = ("far", CAFE[0] + 0.0010, CAFE[1])  # ~111 m
    assert link(stay, [near, far, nearer]) == "nearer"
    assert link(stay, [far]) is None
    vague = Stay(T0, None, CAFE[0], CAFE[1], 120, T0)
    assert link(vague, [far]) == "far"  # within its 120 m accuracy


# --- the upload (contract O) ------------------------------------------------------------------------------


def fix(at: datetime, where: tuple[float, float] = CAFE, acc: float = 12) -> dict[str, object]:
    return {"kind": "fix", "lat": where[0], "lon": where[1], "accuracy_m": acc, "measured_at": at.isoformat()}


def test_upload_fixes_and_visits(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = T0 + timedelta(hours=3)  # the uploads are of the last hours
    device_id, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    body = {"locations": [fix(T0 + timedelta(minutes=i)) for i in range(0, 11, 2)]}
    assert client.post("/v1/devices/me/locations", headers=auth, json=body).status_code == 204
    assert client.post("/v1/devices/me/locations", headers=auth, json=body).status_code == 204  # repeated
    assert db.execute("SELECT count(*) FROM device_locations").fetchone() == (6,)
    assert db.execute("SELECT arrived_at, departed_at FROM stays").fetchall() == [(T0, None)]

    visit = {"kind": "visit", "lat": MUSEUM[0], "lon": MUSEUM[1], "accuracy_m": 35,
             "arrived_at": (T0 + timedelta(hours=1)).isoformat(), "departed_at": None}  # fmt: skip
    assert (
        client.post("/v1/devices/me/locations", headers=auth, json={"locations": [visit]}).status_code == 204
    )
    left = visit | {"departed_at": (T0 + timedelta(hours=2)).isoformat()}  # the departure, in a later upload
    assert (
        client.post("/v1/devices/me/locations", headers=auth, json={"locations": [left]}).status_code == 204
    )
    again = visit  # an older copy without the departure doesn't erase it
    assert (
        client.post("/v1/devices/me/locations", headers=auth, json={"locations": [again]}).status_code == 204
    )
    assert db.execute("SELECT departed_at FROM device_visits").fetchall() == [(T0 + timedelta(hours=2),)]
    rows = db.execute("SELECT arrived_at, departed_at FROM stays ORDER BY arrived_at").fetchall()
    assert rows == [(T0, T0 + timedelta(minutes=10)), (T0 + timedelta(hours=1), T0 + timedelta(hours=2))]
    assert {str(r[0]) for r in db.execute("SELECT device_id FROM stays").fetchall()} == {device_id}


def test_upload_validation(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    visit_with_time = {"kind": "visit", "lat": 1, "lon": 1, "accuracy_m": 5, "arrived_at": T0.isoformat(),
                       "measured_at": T0.isoformat()}  # fmt: skip
    for body in (
        {"locations": []},
        {"locations": [fix(T0 + timedelta(seconds=i)) for i in range(201)]},
        {"locations": [visit_with_time]},
        {"locations": [fix(T0) | {"lat": 91}]},
        {"locations": [{"kind": "wifi", "lat": 1, "lon": 1}]},
    ):
        response = client.post("/v1/devices/me/locations", headers=auth, json=body)
        assert response.status_code == 400 and response.json()["error"]["code"] == "validation_failed", body
    assert client.post("/v1/devices/me/locations", json={"locations": [fix(T0)]}).status_code == 401


# --- an ongoing stay is "here" ----------------------------------------------------------------------------


def here_at(db: psycopg.Connection, secrets_key: str, now: datetime) -> tuple[float, float] | None:
    async def go() -> tuple[float, float] | None:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), FakeModels()) as env:
            try:
                return await env.places.here()
            except Exception:
                return None

    return asyncio.run(go())


def save(db: psycopg.Connection, device: str, fixes: list[Fix]) -> None:
    from pisa.services import stays

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            await stays.save(conn, uuid.UUID(device), fixes)

    asyncio.run(go())


def test_an_ongoing_stay_counts_as_here_for_two_hours(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    save(db, device, track(T0, CAFE, 10, every=5))  # the phone stops sending while the user sits still
    last = T0 + timedelta(minutes=10)
    assert here_at(db, secrets_key, last + timedelta(minutes=14)) == (CAFE[0] - 0.00003, CAFE[1])  # the fix
    staying = here_at(db, secrets_key, last + timedelta(minutes=90))
    assert staying is not None and abs(staying[0] - CAFE[0]) < 0.0001  # older than 15 min: the stay
    assert here_at(db, secrets_key, last + timedelta(minutes=121)) is None  # older than 2 h: unknown
    save(db, device, track(last + timedelta(minutes=5), MUSEUM, 1))  # moved on: the stay ended
    assert here_at(db, secrets_key, last + timedelta(minutes=60)) is None


# --- retention --------------------------------------------------------------------------------------------
