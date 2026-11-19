"""Push setup: the APNs key on the server, and each device's APNs token."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import psycopg
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.apns import ApnsClient, ApnsKey
from pisa.connectors.push_relay import RelayClient
from pisa.domain import live_activity as live_activity_rules
from pisa.domain import push_contract, push_seal
from pisa.domain.errors import PisaError
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.ports.push import PushOutcome, PushSender

APNS_SECRET = "apns"  # noqa: S105 (the row name, not a secret)
RELAY_HANDLE_MAX = 64
_log = get_logger("pisa.push")


def not_configured() -> PisaError:
    return PisaError(
        "push_not_configured",
        "The server has no APNs key yet, so it can't send pushes; this device's token is saved and pushes "
        "start once the key is installed.",
        "Install the APNs key on the server (pisa secrets set-apns)",
    )


def apple_setting(
    value: str | None,
    env: str,
    what: str,
    code: Literal["push_not_configured", "place_provider_not_configured"] = "push_not_configured",
) -> str:
    """A setting an Apple key needs (the team id, the push topic): unset, Pisa says which variable to set."""
    if not value:
        raise PisaError(
            code,
            f"The server has an Apple key but no {what}: set {env} in the server's environment.",
            f"Set {env} and restart the server",
        )
    return value


def push_configured(team_id: str | None, topic: str | None) -> bool:
    """APNs is optional: pushes need the Apple team id and the app's topic (PISA_APNS_TEAM_ID,
    PISA_APNS_TOPIC) and the .p8 key stored on the server. Without them Pisa runs with push off."""
    return bool(team_id and topic)


def push_key_stored(conn: psycopg.Connection) -> bool:
    """The APNs key is stored (`pisa secrets set-apns`), read on a plain connection (the health check's)."""
    return conn.execute("SELECT 1 FROM server_secrets WHERE name = %s", (APNS_SECRET,)).fetchone() is not None


def check_p8(pem: str) -> None:
    """An APNs auth key is a PEM PKCS#8 EC (P-256) private key."""
    try:
        key = load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError):
        raise PisaError(
            "validation_failed",
            # gitleaks reads the header in this message as a key: none is here
            "That is not an APNs key: expected the content of the .p8 file "
            "(-----BEGIN PRIVATE KEY-----).",  # gitleaks:allow
            "Paste the whole .p8 text, including the BEGIN and END lines",
        ) from None
    if not isinstance(key, EllipticCurvePrivateKey):
        raise PisaError("validation_failed", "That key is not an EC key, so it isn't an APNs .p8 key.")


def normalize_p8(text: str) -> str:
    """A .p8 as pasted in the app: a password manager may join the lines or drop the line breaks. Rebuild a
    PEM with 64-character lines from whatever base64 sits between the BEGIN and END markers."""
    body = text.strip()
    begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    if begin in body and end in body:
        body = body.split(begin, 1)[1].split(end, 1)[0]
    b64 = "".join(body.split())
    return begin + "\n" + "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64)) + "\n" + end + "\n"


async def apns_key_status(conn: AsyncConnection) -> dict[str, object]:
    """Whether a key is installed, its key id and when (never the key)."""
    row = await (
        await conn.execute("SELECT meta, set_at FROM server_secrets WHERE name = %s", (APNS_SECRET,))
    ).fetchone()
    if row is None:
        return {"configured": False, "key_id": None, "set_at": None}
    return {"configured": True, "key_id": row[0].get("key_id"), "set_at": row[1].isoformat()}


async def delete_apns_key(conn: AsyncConnection) -> None:
    await conn.execute("DELETE FROM server_secrets WHERE name = %s", (APNS_SECRET,))
    _log.info("apns_key_deleted")


async def set_apns_key(conn: AsyncConnection, keys: KeyStore, key_id: str, pem: str) -> None:
    """Store the APNs .p8 (Fernet-encrypted) next to its key id (not secret)."""
    key_id = key_id.strip()
    if not key_id.isalnum() or len(key_id) != 10:
        raise PisaError("validation_failed", "An APNs key id is 10 letters and digits, e.g. ABCDE12345.")
    pem = normalize_p8(pem)
    check_p8(pem)
    await conn.execute(
        "INSERT INTO server_secrets (name, ciphertext, meta, set_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (name) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, meta = EXCLUDED.meta, "
        "set_at = now()",
        (APNS_SECRET, keys.seal(pem), Jsonb({"key_id": key_id})),
    )
    _log.info("apns_key_set", key_id=key_id)


class PushService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        http: httpx.AsyncClient,
        *,
        team_id: str | None,
        topic: str | None,
        clock: Callable[[], float] | None = None,
        relay_url: str = "",
    ) -> None:
        self._pool = pool
        self._keys = keys
        self._http = http
        self._team_id = team_id
        self._topic = topic
        self._clock = clock
        self._client: ApnsClient | None = None
        self._client_key: Any = None
        kwargs = {"clock": clock} if clock else {}
        self.relay: RelayClient | None = RelayClient(http, relay_url, **kwargs) if relay_url else None
        self.keys = (
            keys  # what opens a relay secret (`devices.end` asks the relay to forget a handle with it)
        )

    def configured(self) -> bool:
        """The Apple settings are there (PISA_APNS_TEAM_ID, PISA_APNS_TOPIC); the key is checked apart."""
        return push_configured(self._team_id, self._topic)

    def relay_configured(self) -> bool:
        """PISA_PUSH_RELAY_URL is set: relay mode is offered (discovery, readyz)."""
        return self.relay is not None

    # --- the relay (CONTRACT-2 §9; relay/PROTOCOL.md)
    # ----------------------------------------------------------
    async def _relay_target(
        self, conn: AsyncConnection, device_id: object
    ) -> tuple[str, bytes, bytes] | None:
        row = await (
            await conn.execute(
                "SELECT handle, secret, public_key FROM device_push_relay WHERE device_id = %s", (device_id,)
            )
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), self._keys.unseal_bytes(bytes(row[1])), bytes(row[2])

    async def _relay_send(
        self, conn: AsyncConnection, device_id: object, kind: str, *, sealed: str | None = None,
        collapse_id: str | None = None, activity: dict[str, Any] | None = None,
        target: tuple[str, bytes, bytes] | None = None,
    ) -> PushOutcome | None:  # fmt: skip
        target = target or await self._relay_target(conn, device_id)
        if target is None:
            return None
        if self.relay is None:
            return PushOutcome(
                "failed", "This device is registered with the push relay, and PISA_PUSH_RELAY_URL is not set."
            )
        handle, secret, _ = target
        body = push_seal.relay_request(
            handle, kind, sealed=sealed, collapse_id=collapse_id, activity=activity
        )
        outcome = await self.relay.send(secret, body, kind)
        if outcome.status == "token_invalid":  # the relay forgot the handle: the registration is gone
            await conn.execute("DELETE FROM device_push_relay WHERE device_id = %s", (device_id,))
            _log.warning("push_relay_dropped", device_id=str(device_id))
        _log.info("push_relay_sent", device_id=str(device_id), type=kind, status=outcome.status)
        return outcome

    async def send_relay_notice(
        self, conn: AsyncConnection, device_id: object, notice: dict[str, Any], *,
        apns: dict[str, Any] | None = None, collapse_id: str | None = None,
    ) -> PushOutcome | None:  # fmt: skip
        """A notice sealed to the device's X25519 key (`{"v": 2, "notice": ..., "apns": ...}`, padded to 512 B
        or 2 KB) and sent through the relay as an alert. None when the device has no relay registration."""
        target = await self._relay_target(conn, device_id)
        if target is None:
            return None
        handle, _, public_key = target
        sent_at = datetime.now(UTC)
        sealed = push_seal.seal(push_seal.plaintext(notice, apns, sent_at=sent_at), public_key, handle)
        return await self._relay_send(conn, device_id, "alert", sealed=sealed, collapse_id=collapse_id,
                                      target=target)  # fmt: skip

    async def send_relay_activity(
        self,
        conn: AsyncConnection,
        device_id: object,
        activity: dict[str, Any],
        *,
        collapse_id: str | None = None,
    ) -> PushOutcome | None:
        """A Live Activity push through the relay: the activity's token, its event, its priority and the
        moment of sending, never anything of the user's day (`live_activity.relay_activity`). A field
        outside that set (a stale date, a progress, a time) is refused here, before anything is sent."""
        extra = set(activity) - live_activity_rules.RELAY_FIELDS
        if extra:
            raise ValueError(f"A relay-mode Live Activity update carries no {', '.join(sorted(extra))}.")
        return await self._relay_send(
            conn, device_id, "liveactivity", activity=activity, collapse_id=collapse_id
        )

    async def sender(self, conn: AsyncConnection) -> PushSender | None:
        """The APNs client, or None when no key is installed. Rebuilt when the key changes."""
        row = await (
            await conn.execute(
                "SELECT ciphertext, meta, set_at FROM server_secrets WHERE name = %s", (APNS_SECRET,)
            )
        ).fetchone()
        if row is None:
            return None
        if self._client is None or self._client_key != row[2]:
            team_id = apple_setting(self._team_id, "PISA_APNS_TEAM_ID", "Apple team id")
            topic = apple_setting(self._topic, "PISA_APNS_TOPIC", "push topic (the app's bundle id)")
            key = ApnsKey(str(row[1]["key_id"]), team_id, self._keys.unseal(bytes(row[0])))
            kwargs = {"clock": self._clock} if self._clock else {}
            self._client = ApnsClient(self._http, key, topic=topic, **kwargs)
            self._client_key = row[2]
        return self._client

    async def register(self, device_id: uuid.UUID, token: str, environment: str) -> None:
        """Store the device's token. Without the APNs settings and key it is kept, and push stays off (readyz,
        discovery) until they are there."""
        low, high = push_contract.DEVICE_TOKEN_HEX
        hex_token = push_contract.hex_token(token, (low, high))
        if hex_token is None:
            raise PisaError(
                "validation_failed",
                f"The apns_token must be the device token in hex ({low} to {high} hex digits).",
                "Send the token from didRegisterForRemoteNotificationsWithDeviceToken as hex",
            )
        token = hex_token
        if environment not in push_contract.APNS_ENVIRONMENTS.values():
            raise PisaError("validation_failed", "The environment must be development or production.")
        async with self._pool.connection() as conn:
            await self.forget_relay(conn, device_id)  # one mode at a time: direct replaces the relay
            async with conn.transaction():
                await conn.execute("DELETE FROM device_push_relay WHERE device_id = %s", (device_id,))
                # A token belongs to one live device: the app paired again takes it over.
                await conn.execute(
                    "UPDATE devices SET apns_token = NULL, apns_environment = NULL, push_updated_at = now() "
                    "WHERE apns_token = %s AND id <> %s",
                    (token, device_id),
                )
                await conn.execute(
                    "UPDATE devices SET apns_token = %s, apns_environment = %s, push_updated_at = now() "
                    "WHERE id = %s",
                    (token, environment, device_id),
                )
            configured = self.configured() and await self.sender(conn) is not None
        _log.info(
            "push_token_registered", device_id=str(device_id), environment=environment, push_on=configured
        )

    async def register_relay(
        self, device_id: uuid.UUID, handle: str, secret: bytes, public_key: bytes
    ) -> None:
        """Contract 2's relay mode (CONTRACT-2 §9): store the relay's handle for this device, the secret it
        shares with the relay (sealed), and the device's X25519 key the notices are sealed to; it replaces a
        direct token."""
        if not 1 <= len(handle) <= RELAY_HANDLE_MAX or len(secret) != 32 or len(public_key) != 32:
            raise PisaError(
                "validation_failed",
                f"A relay registration is a handle of 1 to {RELAY_HANDLE_MAX} characters, a 32-byte secret "
                "and a 32-byte X25519 public key.",
                "Send what the relay's registration returned",
            )
        async with self._pool.connection() as conn:
            await self.forget_relay(conn, device_id, keep=handle)  # an earlier handle of this device
        async with self._pool.connection() as conn, conn.transaction():
            await drop_token(conn, device_id)
            await conn.execute(
                "INSERT INTO device_push_relay (device_id, handle, secret, public_key, created_at) "
                "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (device_id) DO UPDATE SET "
                "handle = excluded.handle, secret = excluded.secret, public_key = excluded.public_key, "
                "created_at = excluded.created_at",
                (device_id, handle, self._keys.seal_bytes(secret), public_key),
            )
        _log.info("push_relay_registered", device_id=str(device_id))

    async def mode(self, device_id: uuid.UUID) -> dict[str, Any]:
        """How notices reach this device (`relay`, `direct` or `none`) and since when; never a secret, token
        or key."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT r.created_at, d.apns_token IS NOT NULL, d.push_updated_at FROM devices d "
                    "LEFT JOIN device_push_relay r ON r.device_id = d.id WHERE d.id = %s",
                    (device_id,),
                )
            ).fetchone()
        if row is None:
            return {"mode": "none", "since": None}
        relay_at, direct, updated_at = row
        if relay_at is not None:
            return {"mode": "relay", "since": relay_at}
        return {"mode": "direct" if direct else "none", "since": updated_at}

    async def unregister(self, device_id: uuid.UUID) -> None:
        """Mode `none`: no token, no relay registration (the relay is told to forget it)."""
        async with self._pool.connection() as conn:
            await self.forget_relay(conn, device_id)
            async with conn.transaction():
                await conn.execute("DELETE FROM device_push_relay WHERE device_id = %s", (device_id,))
                await drop_token(conn, device_id)

    async def forget_relay(
        self, conn: AsyncConnection, device_id: object, *, keep: str | None = None
    ) -> None:
        """The device's relay registration goes: the relay is asked to forget it (`DELETE /v1/register`, best
        effort: a failure is logged, the relay then drops it at its expiry), then the server's row. `keep`: a
        handle that stays (the app registered it again)."""
        await forget_at_relay(conn, self._keys, self.relay, device_id, keep=keep)


async def forget_at_relay(
    conn: AsyncConnection,
    keys: KeyStore,
    relay: RelayClient | None,
    device_id: object,
    *,
    keep: str | None = None,
) -> None:
    """Ask the relay to forget the device's registration, then delete the server's row (also from the CLI's
    `pisa devices revoke`). Best effort: the relay being off or unreachable never stops the revocation."""
    row = await (
        await conn.execute("SELECT handle, secret FROM device_push_relay WHERE device_id = %s", (device_id,))
    ).fetchone()
    if row is None or row[0] == keep:
        return
    handle = str(row[0])
    if relay is None:
        _log.warning(
            "push_relay_not_forgotten", device_id=str(device_id), why="PISA_PUSH_RELAY_URL is not set"
        )
    else:
        try:
            outcome = await relay.forget(keys.unseal_bytes(bytes(row[1])), handle)
        except Exception as exc:  # a broken key or a relay error never stops the revocation
            outcome = PushOutcome("failed", exc.__class__.__name__)
        if outcome.status == "sent":
            _log.info("push_relay_forgotten", device_id=str(device_id))
        else:
            _log.warning("push_relay_not_forgotten", device_id=str(device_id), cause=outcome.message)
    await conn.execute(
        "DELETE FROM device_push_relay WHERE device_id = %s AND handle = %s", (device_id, handle)
    )


async def drop_token(conn: AsyncConnection, device_id: object) -> None:
    await conn.execute(
        "UPDATE devices SET apns_token = NULL, apns_environment = NULL, push_updated_at = now() "
        "WHERE id = %s",
        (device_id,),
    )
