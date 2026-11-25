"""Diagnostics from the phone: MetricKit payloads stored for 30 days, call stacks and exception
types only (pisa.domain.diagnostics). Never logged."""

import uuid
from datetime import date, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.domain.diagnostics import PAYLOAD_MAX_BYTES, payload_size, sanitize, summary
from pisa.domain.errors import PisaError
from pisa.logging import get_logger

_log = get_logger("pisa.diagnostics")


async def store(
    conn: AsyncConnection,
    device_id: uuid.UUID,
    *,
    kind: str,
    app_version: str,
    os_version: str,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    size = payload_size(payload)
    if size > PAYLOAD_MAX_BYTES:
        raise PisaError(
            "validation_failed",
            f"The diagnostic payload is {size / 1_000_000:.1f} MB; at most 1 MB is accepted.",
            "Send the payload's call stacks only",
        )
    kept = sanitize(payload, kind)
    exception, frames = summary(kept, kind)
    await conn.execute(
        "INSERT INTO device_diagnostics (id, device_id, kind, app_version, os_version, exception_type, "
        "top_frames, payload, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), device_id, kind, app_version, os_version, exception, frames, Jsonb(kept), now),
    )
    _log.info("device_diagnostic", device_id=str(device_id), kind=kind, app_version=app_version)


async def store_metrics(
    conn: AsyncConnection,
    device_id: uuid.UUID,
    day: date,
    numbers: dict[str, Any],
    *,
    app_version: str,
    os_version: str,
    now: datetime,
) -> None:
    """The battery numbers: one row per device and day, a later upload for the day replaces it.
    `numbers` holds only what the schema kept (numbers, the day, the companion sessions' start times)."""
    await conn.execute(
        "INSERT INTO device_metrics (device_id, day, app_version, os_version, numbers, received_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (device_id, day) DO UPDATE SET "
        "app_version = EXCLUDED.app_version, os_version = EXCLUDED.os_version, numbers = EXCLUDED.numbers, "
        "received_at = EXCLUDED.received_at",
        (device_id, day, app_version, os_version, Jsonb(numbers), now),
    )
    _log.info("device_metrics", device_id=str(device_id), day=day.isoformat(), app_version=app_version)
