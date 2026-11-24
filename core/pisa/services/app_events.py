"""App events from the phone: kept until they delete them, a daily cap per device and family
(pisa.domain.app_events.PER_DAY); an event dated after now (`FUTURE_SLACK`) is dropped.
Never logged one by one: the log says how many were kept and dropped."""

import uuid
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from pisa.domain.app_events import FUTURE_SLACK, PER_DAY, families, family, sanitize
from pisa.logging import get_logger
from pisa.services import attend

_log = get_logger("pisa.app_events")


async def store(
    conn: AsyncConnection, device_id: uuid.UUID, items: list[Any], *, now: datetime, timezone: str
) -> int:
    """Keep what fits each family's room today; returns how many were stored."""
    # A phone whose clock is wrong would date a call months ahead, and Pisa would hold for it then: not kept.
    events = [e for e in sanitize(items) if e["at"] <= now + FUTURE_SLACK]
    day_start = datetime.combine(now.astimezone(ZoneInfo(timezone)).date(), time(), ZoneInfo(timezone))
    async with conn.transaction():
        # One device's uploads one after the other, so two batches can't both fill today's room.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('pisa.app_events'), hashtext(%s))", (str(device_id),)
        )
        kept: list[dict[str, Any]] = []
        room: dict[str, int] = {}
        for event in events:
            name = family(event["event"])
            if name not in room:
                row = await (
                    await conn.execute(
                        "SELECT count(*) FROM device_app_events WHERE device_id = %s AND received_at >= %s "
                        "AND event = ANY(%s)",
                        (device_id, day_start, list(families(event["event"]))),
                    )
                ).fetchone()
                room[name] = max(0, PER_DAY[name] - (row[0] if row else 0))
            if room[name] > 0:
                room[name] -= 1
                kept.append(event)
        for event in kept:
            await conn.execute(
                "INSERT INTO device_app_events (id, device_id, event, code, site, state, background, at, "
                "received_at, notification_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (uuid.uuid4(), device_id, event["event"], event["code"], event["site"], event["state"],
                 event["background"], event["at"], now, event["notification_id"]),
            )  # fmt: skip
            if event["event"] == "notification_dismissed":  # `dismissed`, a label for attend
                await attend.dismissed(conn, uuid.UUID(event["notification_id"]), event["at"])
    _log.info("device_app_events", device_id=str(device_id), kept=len(kept), dropped=len(items) - len(kept))
    return len(kept)
