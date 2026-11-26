"""The user's settings, stored as one JSON value in app_settings['settings']."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting
from pisa.domain import retention as retention_rules
from pisa.domain import settings as rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.infra import hard_limit
from pisa.infra.hard_limit import HardLimits
from pisa.logging import get_logger
from pisa.services import retention

KEY = "settings"
_log = get_logger("pisa.settings")


async def load(conn: AsyncConnection, language: str) -> dict[str, Any]:
    """Their settings over the defaults; `language` is Pisa's start language (the Owner's)."""
    return await _resolved(conn, rules.with_defaults(await get_setting(conn, KEY), language))


async def read(source: AsyncConnection | AsyncConnectionPool, language: str) -> dict[str, Any]:
    """`load` on a connection, or on one of the pool's (the Owner's reader)."""
    if isinstance(source, AsyncConnectionPool):
        async with source.connection() as conn:
            return await load(conn, language)
    return await load(source, language)


async def _resolved(conn: AsyncConnection, settings: dict[str, Any]) -> dict[str, Any]:
    """The values chosen when read: voice.provider null is OpenAI when an OpenAI key is set, else the phone's
    voice."""
    if settings["voice"]["provider"] is None:
        row = await (await conn.execute("SELECT 1 FROM provider_keys WHERE provider = 'openai'")).fetchone()
        settings["voice"]["provider"] = "openai" if row else "device"
    return settings


class SettingsService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        hard_limits: HardLimits = hard_limit.DEFAULT,
        locked: bool = False,
    ) -> None:
        self._pool = pool
        self._locked = locked  # review mode: the app changes no setting (as the AI keys and caps)
        self._hard = hard_limits
        self._owner = owner
        self._clock = clock

    async def get(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            return await self._owner.settings(conn)

    async def patch_from_app(
        self,
        patch: Any,
        *,
        device_id: uuid.UUID,
        signed: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """The app's PATCH /v1/settings: `patch`, unless this is a demo server (App Store review mode), whose
        settings are its operator's (409 `connector_off`, nothing changed). Reading them still works."""
        if self._locked:
            raise PisaError(
                "connector_off",
                "This is a demo server (App Store review mode): its settings are set by its operator.",
                "Use the settings this server already has",
            )
        return await self.patch(patch, device_id=device_id, signed=signed)

    async def patch(
        self,
        patch: Any,
        *,
        device_id: uuid.UUID | None = None,
        signed: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Merge-PATCH (objects merge, arrays and null replace), validated as a whole, under a row lock.

        A shorter `messages_retention` (irreversible: the prune deletes) needs their signature:
        `signed(value)` is the phone-key guard (op `messages_retention`, target the new value), checked under
        the lock against the value it replaces. Lengthening it, or forever, needs none."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (KEY, Jsonb(rules.defaults(self._owner.tag))),
            )
            row = await (
                await conn.execute("SELECT value FROM app_settings WHERE key = %s FOR UPDATE", (KEY,))
            ).fetchone()
            current = rules.with_defaults(row[0] if row else None, self._owner.tag)
            updated = rules.apply_patch(current, patch)
            cap, hard = updated["places"]["monthly_cap_eur"], self._hard.monthly_eur
            if Decimal(str(cap)) > hard:
                raise PisaError(
                    "validation_failed",
                    f"The places cap (€{cap:.2f} a month) can't be above Pisa's hard limit (€{hard:.2f} a "
                    "month for every paid service together).",
                    "Choose a lower cap; only the server's operator can raise the hard limit",
                )
            value, previous = updated["messages_retention"], current["messages_retention"]
            if retention_rules.shortens(value, previous):
                if signed is None:
                    raise PisaError(
                        "biometric_required",
                        "Keeping messages for less time needs Face ID on the iPhone.",
                        "Confirm the change on the iPhone",
                    )
                await signed(value)
            await conn.execute(
                "UPDATE app_settings SET value = %s, updated_at = now() WHERE key = %s", (Jsonb(updated), KEY)
            )
            if value != previous:  # the grace counts from the database's now()
                await retention.mark_changed(conn, value, previous, device_id)
            talker = ("mode", "talker")
            voice = ("provider", "model", "voice")
            if [updated["conversation"].get(k) for k in talker] != [
                current["conversation"].get(k) for k in talker
            ] or [updated["voice"].get(k) for k in voice] != [current["voice"].get(k) for k in voice]:
                # the talker is a processor in natural mode, and the server's voice while it speaks
                from pisa.services import disclosure

                await disclosure.changed(conn)  # every connected device is told the new disclosure
            resolved = await _resolved(conn, updated)
        _log.info("settings_updated", fields=sorted(_paths(patch)))
        return resolved

    async def messages_preview(self, value: str) -> dict[str, Any]:
        """How many messages `value` would remove, and from when (the first 03:30 a day after a
        change)."""
        if value not in retention_rules.MESSAGES_RETENTION:
            raise PisaError(
                "validation_failed",
                "The messages' retention must be forever, 1y or 90d.",
                "Pick one of the three values",
            )
        async with self._pool.connection() as conn:
            current = rules.with_defaults(await get_setting(conn, KEY), self._owner.tag)["messages_retention"]
            return await retention.preview(conn, value, current, self._owner.tz)


def _paths(patch: Any, prefix: str = "") -> list[str]:
    if not isinstance(patch, dict):
        return [prefix] if prefix else []
    out: list[str] = []
    for key, value in patch.items():
        where = f"{prefix}.{key}" if prefix else str(key)
        out += _paths(value, where) if isinstance(value, dict) else [where]
    return out
