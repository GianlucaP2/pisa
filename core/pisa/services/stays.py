"""Locations, iPhone visits and stays.

Fixes arrive over the WebSocket (`location`) or in batches over REST (`POST /v1/devices/me/locations`, while
the app is in the background); visits only over REST. After each arrival the device's stays are worked out
again (pisa.domain.stays) over the last 24 hours, stretched back to the start of a stay still open at that
point, and linked to known places. Raw fixes and visits are kept 30 days, stays 365 days.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from psycopg import AsyncConnection

from pisa.domain.stays import OLDEST, Fix, Stay, Visit, detect, link
from pisa.domain.taint import personal
from pisa.infra import locks
from pisa.services import outcomes

REFRESH_WINDOW = timedelta(hours=24)
ONGOING_FRESH = timedelta(hours=2)  # an ongoing stay counts as "here" while its last fix is this recent
NEAR_DEG = 0.02  # places within this many degrees of the stays (~2 km) are candidates for their link


async def save(
    conn: AsyncConnection, device_id: uuid.UUID, fixes: Sequence[Fix], visits: Sequence[Visit] = ()
) -> None:
    """Store fixes and visits (repeats ignored; a visit's departure may come later), then refresh the stays.
    Runs in one transaction."""
    async with conn.transaction():
        for f in fixes:
            await conn.execute(
                "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (device_id, measured_at) DO NOTHING",
                (device_id, f.lat, f.lon, f.accuracy_m, f.at),
            )
        for v in visits:
            await conn.execute(
                "INSERT INTO device_visits (device_id, lat, lon, accuracy_m, arrived_at, departed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (device_id, arrived_at) DO UPDATE SET "
                "departed_at = coalesce(EXCLUDED.departed_at, device_visits.departed_at)",
                (device_id, v.lat, v.lon, v.accuracy_m, v.arrived_at, v.departed_at),
            )
        times = [f.at for f in fixes] + [v.arrived_at for v in visits]
        if times:
            await refresh(conn, device_id, max(times), since=min(times))


async def refresh(
    conn: AsyncConnection, device_id: uuid.UUID, now: datetime, *, since: datetime | None = None
) -> list[Stay]:
    """Work out the device's stays again, incrementally: from the latest stay (it may still grow), or `now` -
    24 h when it is older, or `since` (an upload of older points), stretched back to a stay still open then.
    Never further back than the raw-data retention: older stays have no fixes left to rebuild them from.
    One device at a time."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (locks.STAYS, str(device_id)))
    last = await (
        await conn.execute("SELECT max(arrived_at) FROM stays WHERE device_id = %s", (device_id,))
    ).fetchone()
    cutoff = max(last[0], now - REFRESH_WINDOW) if last and last[0] else now - REFRESH_WINDOW
    cutoff = min(cutoff, since or now)
    row = await (
        await conn.execute(
            "SELECT min(arrived_at) FROM stays WHERE device_id = %s AND arrived_at < %s "
            "AND (departed_at IS NULL OR departed_at > %s)",
            (device_id, cutoff, cutoff),
        )
    ).fetchone()
    start = min(cutoff, row[0]) if row and row[0] else cutoff
    newest = await (
        await conn.execute("SELECT max(measured_at) FROM device_locations WHERE device_id = %s", (device_id,))
    ).fetchone()
    # The retention bound counts from the device's newest point, not this upload's (an upload of old points).
    start = max(start, max(now, newest[0] if newest and newest[0] else now) - OLDEST)
    fixes = [
        Fix(lat, lon, acc, at)
        for lat, lon, acc, at in await (
            await conn.execute(
                "SELECT lat, lon, accuracy_m, measured_at FROM device_locations WHERE device_id = %s "
                "AND measured_at >= %s",
                (device_id, start),
            )
        ).fetchall()
    ]
    visits = [
        Visit(lat, lon, acc, arrived, departed)
        for lat, lon, acc, arrived, departed in await (
            await conn.execute(
                "SELECT lat, lon, accuracy_m, arrived_at, departed_at FROM device_visits "
                "WHERE device_id = %s AND arrived_at >= %s",
                (device_id, start),
            )
        ).fetchall()
    ]
    stays = detect(fixes, visits)
    places: list[tuple[str, float, float]] = []
    if stays:  # only the places around these stays
        lats, lons = [s.lat for s in stays], [s.lon for s in stays]
        places = [
            (str(pid), lat, lon)
            for pid, lat, lon in await (
                await conn.execute(
                    "SELECT id, lat, lon FROM places WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s",
                    (min(lats) - NEAR_DEG, max(lats) + NEAR_DEG, min(lons) - NEAR_DEG, max(lons) + NEAR_DEG),
                )
            ).fetchall()
        ]
    await conn.execute("DELETE FROM stays WHERE device_id = %s AND arrived_at >= %s", (device_id, start))
    at_places: list[tuple[str, datetime]] = []
    for s in stays:
        place_id = link(s, places)
        await conn.execute(
            "INSERT INTO stays (id, device_id, arrived_at, departed_at, lat, lon, accuracy_m, last_seen_at, "
            "place_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4(), device_id, s.arrived_at, s.departed_at, s.lat, s.lon, s.accuracy_m,
                s.last_seen_at, place_id,
            ),
        )  # fmt: skip
        if place_id is not None:
            at_places.append((place_id, s.arrived_at))
    await outcomes.visits(conn, at_places, now)  # S1: a card's place, visited within 3 hours
    return stays


async def ongoing(conn: AsyncConnection, now: datetime) -> tuple[float, float] | None:
    """Where the user is staying now: the ongoing stay of the device that reported last (a fix or a visit),
    while its last fix is at most 2 hours old (it counts as a fresh location for the planner and
    `near=here`)."""
    staying = await ongoing_stay(conn, now)
    return (staying[0], staying[1]) if staying else None


async def ongoing_stay(conn: AsyncConnection, now: datetime) -> tuple[float, float, float, datetime] | None:
    """(lat, lon, accuracy, last seen) of that stay. The device that reported last: its latest fix and its
    latest visit, each found through an index."""
    fix = await (
        await conn.execute(
            "SELECT device_id, measured_at FROM device_locations ORDER BY measured_at DESC LIMIT 1"
        )
    ).fetchone()
    visit = await (
        await conn.execute(
            "SELECT device_id, coalesce(departed_at, arrived_at) FROM device_visits "
            "ORDER BY arrived_at DESC LIMIT 1"
        )
    ).fetchone()
    latest = max((r for r in (fix, visit) if r is not None), key=lambda r: r[1], default=None)
    if latest is None:
        return None
    row = await (
        await conn.execute(
            "SELECT lat, lon, accuracy_m, last_seen_at FROM stays WHERE device_id = %s "
            "AND departed_at IS NULL AND last_seen_at >= %s ORDER BY arrived_at DESC LIMIT 1",
            (latest[0], now - ONGOING_FRESH),
        )
    ).fetchone()
    return (row[0], row[1], row[2], row[3]) if row else None


async def between(conn: AsyncConnection, start: datetime, end: datetime) -> list[dict[str, object]]:
    """Stays overlapping [start, end), every device, in time order, with their place's name and address
    (theirs: `Personal`)."""
    rows = await (
        await conn.execute(
            "SELECT s.arrived_at, s.departed_at, s.lat, s.lon, s.place_id::text, p.name, s.last_seen_at, "
            "s.accuracy_m, p.lat, p.lon, p.category, p.address "
            "FROM stays s LEFT JOIN places p ON p.id = s.place_id "
            "WHERE s.arrived_at < %s AND (s.departed_at IS NULL OR s.departed_at > %s) ORDER BY s.arrived_at",
            (end, start),
        )
    ).fetchall()
    return [
        {
            "arrived_at": a,
            "departed_at": d,
            "lat": lat,
            "lon": lon,
            "place_id": pid,
            "name": personal(name),
            "address": personal(address),
            "last_seen_at": seen,
            "accuracy_m": acc,
            "place": (plat, plon) if plat is not None and plon is not None else None,
            "category": category,
        }
        for a, d, lat, lon, pid, name, seen, acc, plat, plon, category, address in rows
    ]
