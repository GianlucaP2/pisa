"""Devices: session-token authentication, listing and revocation (pairing is `services.pairing`).

Only SHA-256 hashes of session tokens are stored.

Every path that ends a device (`pisa devices revoke`, `pisa phone-keys reset`, `pisa devices purge`, revoking
from the app, unpairing) goes through `end`, in one order: the revocation first, committed, then the push
relay is asked to forget the device's handle.
"""

import dataclasses
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from psycopg import AsyncConnection
from psycopg.rows import class_row

from pisa.connectors.push_relay import RelayClient
from pisa.domain.errors import PisaError
from pisa.infra import locks
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.services import retention
from pisa.services.push import forget_at_relay

_log = get_logger("pisa.devices")

_LOCK_PAIRING = locks.PAIRING  # advisory lock id serializing pairing and revoking from the app
# Postgres NOTIFY channel: the payload is a revoked device id. The API closes that device's live sockets.
REVOKED_CHANNEL = "pisa_device_revoked"
# Only the smoke test's throwaway device can be purged.
PURGE_NAME = "smoke-test"
PURGE_PLATFORM = "other"
PURGE_WITHIN = timedelta(hours=24)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Device:
    id: uuid.UUID
    name: str
    platform: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    # when the session token it authenticated with ends (None: not read from a session token, e.g. listed)
    session_expires_at: datetime | None = None


async def authenticate(conn: AsyncConnection, token: str | None, *, now: datetime | None = None) -> Device:
    """Resolve a session token (contract 2, 1 h) to its device. Missing or unknown: auth_failed; an ended
    session: session_expired; a revoked device's session: device_revoked."""
    if not token:
        raise PisaError(
            "auth_failed",
            "No session token was sent (Authorization: Bearer <token>).",
            "Get one with POST /v1/devices/session, or pair this device again",
        )
    now = now or _utcnow()
    cur = conn.cursor(row_factory=class_row(Device))
    session = await (
        await conn.execute(
            "SELECT device_id, expires_at FROM device_sessions WHERE token_hash = %s", (sha256_hex(token),)
        )
    ).fetchone()
    if session is None:
        raise PisaError(
            "auth_failed",
            "That is not a session token of this server.",
            "Get one with POST /v1/devices/session, or pair this device again",
        )
    device_id, expires_at = session
    device = await (
        await cur.execute(
            "UPDATE devices SET last_seen_at = %s WHERE id = %s RETURNING id, name, platform, "
            "created_at, last_seen_at, revoked_at",
            (now, device_id),
        )
    ).fetchone()
    if device is None or device.revoked_at is not None:
        raise PisaError("device_revoked", "This device was revoked on the server.", "Pair this device again")
    if expires_at <= now:
        raise PisaError(
            "session_expired",
            "The session token expired (they last an hour).",
            "Get a new nonce, sign it with the device key and send POST /v1/devices/session",
        )
    return dataclasses.replace(device, session_expires_at=expires_at)


async def list_devices(conn: AsyncConnection) -> list[Device]:
    cur = conn.cursor(row_factory=class_row(Device))
    return await (
        await cur.execute(
            "SELECT id, name, platform, created_at, last_seen_at, revoked_at FROM devices ORDER BY created_at"
        )
    ).fetchall()


async def revoke_device(conn: AsyncConnection, device_id: uuid.UUID, *, now: datetime | None = None) -> None:
    row = await (
        await conn.execute(
            "UPDATE devices SET revoked_at = coalesce(revoked_at, %s) WHERE id = %s RETURNING id",
            (now or _utcnow(), device_id),
        )
    ).fetchone()
    if row is None:
        raise PisaError(
            "not_found", f"There is no device with id {device_id}.", "List devices with `pisa devices list`"
        )
    await withdraw_offers(conn, device_id, now or _utcnow())
    await forget_vouched_keys(conn, device_id)
    await conn.execute("SELECT pg_notify(%s, %s)", (REVOKED_CHANNEL, str(device_id)))


async def forget_vouched_keys(conn: AsyncConnection, device_id: uuid.UUID) -> None:
    """A revoked device's vouch goes with it: the keys it vouched for (`add_key`) are deleted, and
    the keys the devices holding those vouched for, down the whole chain (UNION ends a cycle)."""
    gone = await conn.execute(
        "WITH RECURSIVE down(device) AS (SELECT %s::uuid UNION SELECT k.device_id FROM phone_keys k "
        "JOIN down ON k.vouched_by = down.device) "
        "DELETE FROM phone_keys WHERE vouched_by IN (SELECT device FROM down)",
        (device_id,),
    )
    if gone.rowcount:
        _log.info("vouched_keys_revoked", count=gone.rowcount)


async def withdraw_offers(conn: AsyncConnection, device_id: uuid.UUID, now: datetime) -> None:
    """A revoked device's open pairing offers are spent: no one can pair with them any more, and they stop
    counting against the server-wide 5 open tokens (so revoking ends a device's hold on `pisa pair`)."""
    await conn.execute(
        "UPDATE pair_tokens SET used_at = %s, token = NULL WHERE offered_by = %s AND used_at IS NULL",
        (now, device_id),
    )


async def unpair(conn: AsyncConnection, device_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Unpair and delete, from the device itself (CONTRACT-2 §4.5): the device is revoked (its session tokens
    then answer `device_revoked`, and its live socket closes), and its approval keys, push registration (APNs
    token; the relay's goes in `end`, after the commit) and Live Activity tokens are deleted. Its history
    stays the user's."""
    now = now or _utcnow()
    async with conn.transaction():
        await conn.execute(
            "UPDATE devices SET revoked_at = coalesce(revoked_at, %s), apns_token = NULL, apns_environment = "
            "NULL, push_updated_at = %s WHERE id = %s",
            (now, now, device_id),
        )
        await conn.execute("DELETE FROM phone_keys WHERE device_id = %s", (device_id,))
        await forget_vouched_keys(conn, device_id)
        await conn.execute("DELETE FROM device_live_activity WHERE device_id = %s", (device_id,))
        await withdraw_offers(conn, device_id, now)
        await conn.execute("SELECT pg_notify(%s, %s)", (REVOKED_CHANNEL, str(device_id)))
    _log.info("device_unpaired", device_id=str(device_id))


async def endpoints_told(conn: AsyncConnection, device_id: uuid.UUID, revision: str) -> None:
    await conn.execute("UPDATE devices SET endpoints_told = %s WHERE id = %s", (revision, device_id))


async def told(conn: AsyncConnection, device_id: uuid.UUID) -> tuple[str | None, str | None] | None:
    """The AI disclosure's and the endpoints' revisions the device was last told (None: no such device)."""
    row = await (
        await conn.execute("SELECT ai_told, endpoints_told FROM devices WHERE id = %s", (device_id,))
    ).fetchone()
    return None if row is None else (row[0], row[1])


async def revoked(conn: AsyncConnection, device_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, bool]:
    """Whether each of these devices is revoked; a device that no longer exists is left out."""
    rows = await (
        await conn.execute(
            "SELECT id, revoked_at IS NOT NULL FROM devices WHERE id = ANY(%s)", (list(device_ids),)
        )
    ).fetchall()
    return {device_id: is_revoked for device_id, is_revoked in rows}


async def purge_device(
    conn: AsyncConnection, device_id: uuid.UUID, now: datetime, *, keys: KeyStore, relay: RelayClient | None
) -> dict[str, int]:
    """Remove a device and everything it made: its conversations and messages, the notifications sent to it
    alone and the briefings they carried, the tasks it added by voice, the plans it asked for, its presence,
    locations and stays. Nothing of the user's other devices or shared history is touched.

    Only the smoke test's throwaway device: named "smoke-test", platform "other", paired in the last 24 hours.
    Any other device is refused; there is no override."""
    row = await (
        await conn.execute("SELECT name, platform, created_at FROM devices WHERE id = %s", (device_id,))
    ).fetchone()
    if row is None:
        raise PisaError("not_found", f"There is no device {device_id}.", "See `pisa devices list`")
    name, platform, created = row
    if name != PURGE_NAME or platform != PURGE_PLATFORM or created <= now - PURGE_WITHIN:
        raise PisaError(
            "validation_failed",
            f"Device {device_id} ({name!r}, {platform}) is not the smoke test's throwaway device: only a "
            f"device named {PURGE_NAME!r}, platform {PURGE_PLATFORM!r}, paired in the last 24 hours can be "
            "purged.",
            "Use `pisa devices revoke` to cut a real device off",
        )
    await end(conn, [device_id], keys=keys, relay=relay)  # the live socket closes, the relay forgets
    counts: dict[str, int] = {}
    async with conn.transaction():
        cur = await conn.execute("DELETE FROM plans WHERE requested_by = %s", (device_id,))
        counts["plans"] = cur.rowcount
        cur = await conn.execute(
            "DELETE FROM tasks WHERE source_kind = 'voice' AND source_ref = %s", (f"device:{device_id}",)
        )
        counts["tasks"] = cur.rowcount
        cur = await conn.execute(
            "DELETE FROM briefings WHERE notification_id IN "
            "(SELECT id FROM notifications WHERE device_id = %s)",
            (device_id,),
        )
        counts["briefings"] = cur.rowcount
        cur = await conn.execute("DELETE FROM notifications WHERE device_id = %s", (device_id,))
        counts["notifications"] = cur.rowcount
        # what Pisa kept from its messages (0090: SET NULL)
        await retention.forget_cited(conn, await retention.device_messages(conn, device_id))
        cur = await conn.execute("DELETE FROM conversations WHERE device_id = %s", (device_id,))
        counts["conversations"] = cur.rowcount  # their messages go with them
        await conn.execute("DELETE FROM devices WHERE id = %s", (device_id,))  # presence, locations, stays
    _log.info("device_purged", device_id=str(device_id), **counts)
    return counts


async def revoke_from_app(conn: AsyncConnection, device_id: uuid.UUID) -> None:
    """Revoke a device from the app. The last active device can't be revoked this way: the app would
    lose its only way in, and pairing again would need the server."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_PAIRING,))
        others = await (
            await conn.execute(
                "SELECT count(*) FROM devices WHERE revoked_at IS NULL AND id <> %s", (device_id,)
            )
        ).fetchone()
        active = await (
            await conn.execute("SELECT revoked_at IS NULL FROM devices WHERE id = %s", (device_id,))
        ).fetchone()
        if active is not None and active[0] and others is not None and others[0] == 0:
            raise PisaError(
                "last_device",
                "This is the only paired device: revoking it would leave no way into Pisa from the app.",
                "Pair another device first (Settings → Devices → Pair another device)",
            )
        await revoke_device(conn, device_id)


Ending = Literal["revoke", "from_app", "unpair"]
_ENDINGS: dict[Ending, Callable[[AsyncConnection, uuid.UUID], Awaitable[None]]] = {
    "revoke": revoke_device,  # the server's CLI
    "from_app": revoke_from_app,  # another device, from the app (never the last one)
    "unpair": unpair,  # the device itself (CONTRACT-2 §4.5)
}


async def end(
    conn: AsyncConnection,
    which: Sequence[uuid.UUID] | Callable[[AsyncConnection], Awaitable[Sequence[uuid.UUID]]],
    *,
    keys: KeyStore,
    relay: RelayClient | None,
    how: Ending = "revoke",
) -> list[uuid.UUID]:
    """End devices, the one way every path takes: each is revoked (`how`) in one transaction, which
    commits first (its sessions end, its live socket closes, the keys it vouched for go); then the push relay
    is asked to forget each one's handle and the server's relay registration goes (`forget_at_relay`: best
    effort, a relay that is off or fails never stops or undoes a revocation, and it drops the handle at its
    expiry). `which`: the devices, or a function that picks them inside that transaction (and may do more
    there, or refuse). `conn` is in autocommit mode, never inside a transaction. Returns the devices ended."""
    async with conn.transaction():
        ended = list(which if not callable(which) else await which(conn))
        for device_id in ended:
            await _ENDINGS[how](conn, device_id)
    for device_id in ended:
        await forget_at_relay(conn, keys, relay, device_id)
    return ended


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None
