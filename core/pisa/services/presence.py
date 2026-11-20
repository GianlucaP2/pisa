"""The latest presence per device (protocol `presence`).

Only a **change** is a briefing trigger: a presence that repeats the stored state is ignored. When a device's
socket closes, its state resets to "app in background, glasses unknown", so reopening the app after it was
killed still counts as `app_open`. Companion mode and earbuds are stored for delivery (
while both are on, proactive messages go over the WebSocket only); they trigger nothing.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.domain.body import stamped
from pisa.domain.locked import UNLOCKED_SEEN, is_locked

PresenceEvent = Literal[
    "glasses_on", "glasses_off", "app_foreground", "app_background",
    "earbuds_connected", "earbuds_disconnected", "companion_on", "companion_off",
]  # fmt: skip
Trigger = Literal["glasses_on", "app_open"]

_FIELD: dict[str, tuple[str, str]] = {
    "glasses_on": ("glasses", "on"),
    "glasses_off": ("glasses", "off"),
    "app_foreground": ("app", "foreground"),
    "app_background": ("app", "background"),
    "earbuds_connected": ("earbuds", "connected"),
    "earbuds_disconnected": ("earbuds", "disconnected"),
    "companion_on": ("companion", "on"),
    "companion_off": ("companion", "off"),
}
_TRIGGERS: dict[str, Trigger] = {"glasses_on": "glasses_on", "app_foreground": "app_open"}


async def update(
    conn: AsyncConnection, device_id: uuid.UUID, event: str, now: datetime, body: dict[str, Any] | None = None
) -> Trigger | None:
    """Store the event (and the device's state, when the app sends it: Pisa's body line; each field with its
    time, so one the app stops sending ages out); returns the briefing trigger it starts (a change to glasses
    on or app foreground)."""
    if event not in _FIELD:
        return None
    column, value = _FIELD[event]
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT app, glasses, earbuds, companion, state FROM device_presence WHERE device_id = %s "
                "FOR UPDATE",
                (device_id,),
            )
        ).fetchone()
        values = row[:4] if row else ("background", None, None, None)
        kept = stamped(row[4] if row else None, body, now)
        if body and body.get("locked") is False and kept is not None:  # unlocked this session
            kept = kept | {UNLOCKED_SEEN: True}
        state: dict[str, str | None] = dict(
            zip(("app", "glasses", "earbuds", "companion"), values, strict=True)
        )
        before = state[column]
        state[column] = value
        await conn.execute(
            "INSERT INTO device_presence (device_id, app, glasses, earbuds, companion, state, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (device_id) DO UPDATE SET app = EXCLUDED.app, "
            "glasses = EXCLUDED.glasses, earbuds = EXCLUDED.earbuds, companion = EXCLUDED.companion, "
            "state = EXCLUDED.state, updated_at = EXCLUDED.updated_at",
            (device_id, state["app"], state["glasses"], state["earbuds"], state["companion"],
             Jsonb(kept) if kept else None, now),
        )  # fmt: skip
    return _TRIGGERS.get(event) if before != value else None


async def reset(conn: AsyncConnection, device_id: uuid.UUID, now: datetime) -> None:
    """A session started or the socket closed: app in background; glasses, companion mode and earbuds unknown
    (the device sends their actual state after each `welcome`)."""
    await conn.execute(
        "UPDATE device_presence SET app = 'background', glasses = NULL, companion = NULL, earbuds = NULL, "
        "state = NULL, updated_at = %s WHERE device_id = %s",
        (now, device_id),
    )


async def phone_locked(conn: AsyncConnection, device_id: uuid.UUID | None, now: datetime) -> bool:
    """The device's latest state says `locked: true` (within the 10
    minutes), never unlocked this session, companion mode off. Unknown or old: False."""
    if device_id is None:
        return False
    row = await (
        await conn.execute("SELECT state, companion FROM device_presence WHERE device_id = %s", (device_id,))
    ).fetchone()
    return row is not None and is_locked(row[0], now, companion=row[1] == "on")
