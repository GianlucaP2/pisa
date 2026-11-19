"""QR pairing with HMAC, a committed SAS, and the device key's short-lived session tokens (CONTRACT-2 §4.3,
§4.3.1; `protocol/v2/schema/pisa-contract-2.schema.json`).

1. `pisa pair` (or a paired phone's offer) makes a one-time token (10 minutes) and shows it in a QR with the
   server's addresses and TLS pin.
2. The app sends `POST /v1/pair/begin`: the server picks its nonce and answers only its commitment, before it
   can see the app's transcript (so nobody can grind the SAS).
3. The app sends `POST /v1/pair` with that pairing id, its device key, a nonce and the HMAC of the transcript
   under the token. The server finds the open token whose HMAC matches (spending it: single use), reveals its
   nonce with its own HMAC, and keeps the request waiting.
4. Both sides show the SAS; the user confirms on the server side (`pisa pair`'s prompt, or the offering
   phone). Only then is the device made, holding its device key.
5. The device asks for a nonce (`GET /v1/devices/session/nonce`: 60 s, single use, at most 5 outstanding per
   client address) and signs it with its device key (`POST /v1/devices/session`): a 1 h session token.

Only SHA-256 hashes of nonces and session tokens are stored. The pairing token itself is kept until it is used
(the HMAC is checked with it), sealed with the secrets key when the server has one, and erased when used.
"""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from pisa.domain import pairing as rules
from pisa.domain.errors import PisaError
from pisa.infra import locks
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger

_log = get_logger("pisa.pairing")

TOKEN_TTL = timedelta(minutes=10)
CONFIRM_TTL = timedelta(minutes=5)  # the user confirms the SAS on the server side within this
BEGIN_TTL = timedelta(minutes=5)  # a begun pairing waits this long for its POST /v1/pair
# begun and not yet used pairings per client address (its /64 or IPv4 address): a backstop shared by everyone
# behind one NAT, so well above what one household begins in 5 minutes
MAX_OPEN_BEGINS = 20
NONCE_TTL = timedelta(seconds=60)
SESSION_TTL = timedelta(hours=1)
MAX_OPEN_TOKENS = 5
MAX_OUTSTANDING_NONCES = 5  # per device (bound nonces), or per client address (unbound ones)
MAX_UNBOUND_NONCES = 10_000  # unbound nonces outstanding in all: the backstop behind many addresses
# The address backstop (CONTRACT-2 §4.3 step 5): failed pairing requests per client
# address (its IPv6 /64 or IPv4 address) in BACKSTOP_WINDOW, past which `begin` and `pair` answer 429
# `rate_limited`. A failed request names no token, so it never touches one; the token's protection is its 128
# bits, single use, 10-minute expiry and the 5-open cap. The backstop bounds only volume (each failure HMACs
# every open token under both pins and writes a row): one failure every 10 seconds, sustained, from one
# address, so a household or office behind one NAT, with a typo or
# a stale QR now and then, never meets it.
MAX_ADDRESS_FAILURES = 60
BACKSTOP_WINDOW = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class PairToken:
    id: uuid.UUID
    token: str
    expires_at: datetime


def _seal(keys: KeyStore, token: str) -> bytes:
    return keys.seal(token) if keys.can_store else token.encode()


def _unseal(keys: KeyStore, data: bytes) -> str | None:
    try:
        return keys.unseal(data) if keys.can_store else data.decode()
    except (PisaError, UnicodeDecodeError) as exc:  # sealed under another secrets key: that token can't match
        _log.info("pair_token_unreadable", cause=exc.__class__.__name__)
        return None


async def create_token(
    conn: AsyncConnection, keys: KeyStore, label: str, *, origin: str = "host", signer: bool = False,
    now: datetime | None = None, ttl: timedelta = TOKEN_TTL,
) -> PairToken:  # fmt: skip
    """A one-time pairing token for the QR. At most MAX_OPEN_TOKENS are open at once."""
    now = now or _now()
    label = label.strip()[:60] or "device"
    ttl = min(ttl, TOKEN_TTL)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.PAIRING,))
        await conn.execute(
            "UPDATE pair_tokens SET token = NULL WHERE token IS NOT NULL AND expires_at <= %s", (now,)
        )
        row = await (
            await conn.execute(
                "SELECT count(*) FROM pair_tokens WHERE used_at IS NULL AND expires_at > %s", (now,)
            )
        ).fetchone()
        if row is not None and row[0] >= MAX_OPEN_TOKENS:
            raise PisaError(
                "rate_limited",
                f"{MAX_OPEN_TOKENS} pairing codes are already open.",
                f"Use one of them, or wait {TOKEN_TTL.seconds // 60} minutes",
            )
        token = rules.new_token()
        token_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO pair_tokens (id, token, label, origin, signer, created_at, expires_at) VALUES (%s, "
            "%s, %s, %s, %s, %s, %s)",
            (token_id, _seal(keys, token), label, origin, signer, now, now + ttl),
        )
    _log.info("pair_token_created", origin=origin, signer=signer)
    return PairToken(token_id, token, now + ttl)


def _refused(message: str) -> PisaError:
    return PisaError("pairing_code_invalid", message, "Start pairing again with a new code")


async def _backstop(conn: AsyncConnection, source: str, now: datetime) -> None:
    """The address backstop: 429 `rate_limited` once MAX_ADDRESS_FAILURES pairing requests from this address
    (its /64 or IPv4 address) failed in BACKSTOP_WINDOW. No failure touches a token."""
    await conn.execute("DELETE FROM pairing_failures WHERE at < %s", (now - BACKSTOP_WINDOW,))
    failed = await (
        await conn.execute(
            "SELECT count(*) FROM pairing_failures WHERE source = %s AND at > %s",
            (source, now - BACKSTOP_WINDOW),
        )
    ).fetchone()
    if failed is not None and failed[0] >= MAX_ADDRESS_FAILURES:
        raise PisaError(
            "rate_limited",
            f"{MAX_ADDRESS_FAILURES} pairing attempts from here did not match a code.",
            f"Wait {BACKSTOP_WINDOW.seconds // 60} minutes, then scan a new code",
        )


async def begin(conn: AsyncConnection, *, source: str, now: datetime | None = None) -> dict[str, Any]:
    """`POST /v1/pair/begin` (`$defs/PairBegin`): the server picks serverNonce now, keeps it with a new
    pairing id, and sends only its commitment, before it can see the app's transcript (CONTRACT-2 §15). At
    most
    MAX_OPEN_BEGINS unused ones per client address; an address locked out of pairing gets none."""
    now = now or _now()
    source = source[:200] or "local"
    server_nonce = rules.new_nonce()
    pairing_id = "pair_" + rules.b64url(secrets.token_bytes(16))
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.PAIRING,))
        await _backstop(conn, source, now)
        await conn.execute(  # the unused ones of the past hour and more: never revealed, nothing to keep
            "DELETE FROM pairings WHERE state = 'begun' AND expires_at < %s", (now - timedelta(hours=1),)
        )
        row = await (
            await conn.execute(
                "SELECT count(*) FROM pairings WHERE state = 'begun' AND address = %s AND expires_at > %s",
                (source, now),
            )
        ).fetchone()
        if row is not None and row[0] >= MAX_OPEN_BEGINS:
            raise PisaError(
                "rate_limited",
                f"{MAX_OPEN_BEGINS} pairings were begun from here and not finished.",
                f"Finish one, or wait {BEGIN_TTL.seconds // 60} minutes",
            )
        await conn.execute(
            "INSERT INTO pairings (id, device_id, server_nonce, state, address, created_at, expires_at) "
            "VALUES "
            "(%s, %s, %s, 'begun', %s, %s, %s)",
            (pairing_id, uuid.uuid4(), server_nonce, source, now, now + BEGIN_TTL),
        )
    return {
        "pairingId": pairing_id,
        "commitment": rules.commitment(server_nonce),
        "expiresAt": (now + BEGIN_TTL).isoformat(),
    }


async def pair(
    conn: AsyncConnection, keys: KeyStore, body: dict[str, Any], *, pins: Sequence[str], source: str,
    now: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`POST /v1/pair`: the begun pairing (`pairingId`, spent by this request whatever its outcome) and the
    open token whose HMAC matches (spent too), kept waiting for the user's confirmation;
    `$defs/PairResponse`, which reveals the serverNonce committed to at the begin. An unknown, expired or used
    pairing id, or a request no token matches, is 400 `pairing_code_invalid` and counts against its client
    address."""
    now = now or _now()
    try:
        der, _ = rules.device_key(body["deviceKey"])
        rules.unb64(body["clientNonce"], "clientNonce")
    except rules.PairingInputError as err:
        raise PisaError(
            "validation_failed", str(err), "Send the device key and nonce as the contract says"
        ) from None
    source = source[:200] or "local"
    pairing_id = str(body["pairingId"])[:80]
    why = ""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.PAIRING,))
        await _backstop(conn, source, now)
        begun = await (
            await conn.execute(
                "SELECT device_id, server_nonce, expires_at FROM pairings WHERE id = %s AND state = 'begun' "
                "FOR UPDATE",
                (pairing_id,),
            )
        ).fetchone()
        match: tuple[uuid.UUID, bytes, str] | None = None
        if begun is None or begun[2] <= now:
            why = "This pairing was not begun, was already used, or expired (POST /v1/pair/begin first)."
        else:
            rows = await (
                await conn.execute(
                    "SELECT id, token FROM pair_tokens WHERE used_at IS NULL AND token IS NOT NULL "
                    "AND expires_at > %s",
                    (now,),
                )
            ).fetchall()
            for token_id, sealed in rows:
                token = _unseal(keys, bytes(sealed))
                if token is None:
                    continue
                key = rules.token_bytes(token)
                for pin in pins:
                    text = rules.transcript(pin, body["deviceKey"], body["clientNonce"])
                    if rules.macs_equal(body["mac"], rules.client_mac(key, text)):
                        match = (token_id, key, pin)
                        break
                if match is not None:
                    break
            if match is None:
                why = "No open pairing code matches this request (wrong, used or expired code)."
        if match is None:
            # the address backstop only: the request names no token, so it never touches one
            await conn.execute("INSERT INTO pairing_failures (source, at) VALUES (%s, %s)", (source, now))
            if begun is not None:  # a begun pairing serves one request: a commitment is never reused
                await conn.execute(
                    "UPDATE pairings SET state = 'refused', decided_at = %s, address = NULL WHERE id = %s",
                    (now, pairing_id),
                )
        else:
            assert begun is not None
            token_id, key, pin = match
            # the device's id is drawn now, as the client address leaves the row: a device id and a client
            # address are never kept together (a known device id pushes out that device's bound nonces)
            device_id, server_nonce = uuid.uuid4(), str(begun[1])
            text = rules.transcript(pin, body["deviceKey"], body["clientNonce"])
            mac = rules.server_mac(key, text, server_nonce)
            await conn.execute(
                "UPDATE pair_tokens SET used_at = %s, token = NULL WHERE id = %s", (now, token_id)
            )
            await conn.execute(
                "UPDATE pairings SET token_id = %s, device_key = %s, name = %s, platform = %s, mac = %s, "
                "sas = %s, spki = %s, device_key_sent = %s, client_nonce = %s, state = "
                "'awaiting_confirmation', expires_at = %s, device_id = %s, address = NULL WHERE id = %s",
                (token_id, der, body["name"][:60], body["platform"], mac, rules.sas(text, server_nonce), pin,
                 body["deviceKey"], body["clientNonce"], now + CONFIRM_TTL, device_id, pairing_id),
            )  # fmt: skip
    if match is None:
        _log.info("pairing_refused", why="no begun pairing or no token matches")
        raise _refused(why)
    _log.info("pairing_requested", platform=body["platform"])
    return {
        "pairingId": pairing_id,
        "deviceId": str(device_id),
        "serverNonce": server_nonce,
        "mac": mac,
        "state": "awaiting_confirmation",
    }


async def _expire(conn: AsyncConnection, now: datetime) -> None:
    await conn.execute(
        "UPDATE pairings SET state = 'expired', decided_at = %s WHERE state = 'awaiting_confirmation' "
        "AND expires_at <= %s",
        (now, now),
    )


async def state(conn: AsyncConnection, pairing_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """`GET /v1/pair/{pairingId}`: the request's state (`expired` once its confirmation window passed). A
    pairing only begun is not found: its serverNonce is revealed by `POST /v1/pair` alone."""
    now = now or _now()
    await _expire(conn, now)
    row = await (
        await conn.execute(
            "SELECT device_id, server_nonce, mac, state FROM pairings WHERE id = %s AND mac IS NOT NULL",
            (pairing_id[:80],),
        )
    ).fetchone()
    if row is None:
        raise PisaError("not_found", "There is no such pairing.", "Scan the pairing code again")
    device_id, server_nonce, mac, current = row
    return {
        "pairingId": pairing_id,
        "deviceId": str(device_id),
        "serverNonce": server_nonce,
        "mac": mac,
        "state": current,
    }


@dataclass(frozen=True)
class Waiting:
    pairing_id: str
    name: str
    platform: str
    sas: str
    expires_at: datetime


async def waiting(
    conn: AsyncConnection, *, token_id: uuid.UUID | None = None, now: datetime | None = None
) -> list[Waiting]:
    """The requests waiting for the user's confirmation on the server (of one token, or all): a pairing a
    paired
    phone offered is that phone's to confirm, never the CLI's."""
    now = now or _now()
    rows = await (
        await conn.execute(
            "SELECT p.id, p.name, p.platform, p.sas, p.expires_at FROM pairings p "
            "JOIN pair_tokens t ON t.id = p.token_id WHERE p.state = 'awaiting_confirmation' "
            "AND p.expires_at > %s AND t.offered_by IS NULL AND (%s::uuid IS NULL OR p.token_id = %s::uuid) "
            "ORDER BY p.created_at",
            (now, token_id, token_id),
        )
    ).fetchall()
    return [Waiting(*r) for r in rows]


async def signer_key_skipped(conn: AsyncConnection, device_id: uuid.UUID) -> bool:
    """The device was paired from a `--signer` token, yet its key is not in the phone-key store: Pisa had no
    Face ID key when it was confirmed, and nothing adds it later."""
    row = await (
        await conn.execute(
            "SELECT t.signer AND NOT EXISTS (SELECT 1 FROM phone_keys k WHERE k.key_id = %s) FROM pairings p "
            "JOIN pair_tokens t ON t.id = p.token_id WHERE p.device_id = %s AND p.state = 'confirmed'",
            (rules.device_key_id(device_id), device_id),
        )
    ).fetchone()
    return bool(row and row[0])


async def token_state(conn: AsyncConnection, token_id: uuid.UUID, now: datetime | None = None) -> str:
    """`open`, `used` or `expired`."""
    now = now or _now()
    row = await (
        await conn.execute("SELECT used_at, expires_at FROM pair_tokens WHERE id = %s", (token_id,))
    ).fetchone()
    if row is None or (row[0] is None and row[1] <= now):
        return "expired"
    return "used" if row[0] is not None else "open"


async def decide(
    conn: AsyncConnection, pairing_id: str, confirm: bool, *, now: datetime | None = None,
    offered_by: uuid.UUID | None = None, sas: str | None = None,
) -> uuid.UUID | None:  # fmt: skip
    """The user's answer: on the server (`pisa pair`), or on the phone that offered the pairing (`offered_by`,
    with the SAS it showed, which must be the server's). Confirmed: the device is made with its device key
    (and,
    for a token made with `signer`, its key also signs device ops). Returns the device id when confirmed."""
    now = now or _now()
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.PAIRING,))
        row = await (
            await conn.execute(
                "SELECT p.device_id, p.device_key, p.name, p.platform, p.state, p.expires_at, t.origin, "
                "t.signer, t.offered_by, p.sas FROM pairings p JOIN pair_tokens t ON t.id = p.token_id "
                "WHERE p.id = %s FOR UPDATE OF p",
                (pairing_id,),
            )
        ).fetchone()
        if row is None or row[8] != offered_by:
            raise PisaError("not_found", "There is no such pairing.", "Start pairing again")
        device_id, der, name, platform, current, expires_at, origin, signer, _, expected = row
        if current != "awaiting_confirmation" or expires_at <= now:
            raise PisaError(
                "pairing_code_invalid",
                "This pairing is no longer waiting (it was decided, or its 5 minutes passed).",
                "Start pairing again",
                status=409,
            )
        if confirm and sas is not None and sas != expected:
            raise PisaError(
                "pairing_code_invalid",
                "These are not the digits of this pairing: nothing was confirmed.",
                "Compare the 6 digits on both devices again",
                status=409,
            )
        if not confirm:
            await conn.execute(
                "UPDATE pairings SET state = 'refused', decided_at = %s WHERE id = %s", (now, pairing_id)
            )
            _log.info("pairing_refused", why="the user said no")
            return None
        await conn.execute(
            "INSERT INTO devices (id, name, platform, created_at, paired_via, device_key) VALUES "
            "(%s, %s, %s, %s, %s, %s)",
            (device_id, name, platform, now, origin, bytes(der)),
        )
        # RAIL: a signer's device key joins the phone-key store only beside a Face ID
        # key already there. The first key comes only through `/v1/phone/keys` and its first-key rule (an
        # iPhone paired on the server); while the store is empty, account-level ops need no signature anyway.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.phone_keys'))")
        face_id = "SELECT 1 FROM phone_keys WHERE kind = 'biometric' LIMIT 1"
        first = await (await conn.execute(face_id)).fetchone() is None
        if signer and first:
            _log.info("signer_key_not_added", device_id=str(device_id), why="no Face ID key yet")
        elif signer:
            await conn.execute(
                "INSERT INTO phone_keys (key_id, public_key, label, device_id, created_at, kind) VALUES (%s, "
                "%s, %s, %s, %s, 'signer') ON CONFLICT (key_id) DO NOTHING",
                (rules.device_key_id(device_id), rules.x963(bytes(der)), f"{name} (device key)"[:80],
                 device_id, now),
            )  # fmt: skip
        await conn.execute(
            "UPDATE pairings SET state = 'confirmed', decided_at = %s WHERE id = %s", (now, pairing_id)
        )
    _log.info("pairing_confirmed", device_id=str(device_id), platform=platform, signer=signer)
    return device_id


# --- pairing a new device from a paired phone (CONTRACT-2 §4.3.1, proposed)
# -----------------------------------
async def offer(
    conn: AsyncConnection, keys: KeyStore, device_id: uuid.UUID, *, name: str | None,
    addresses: Sequence[str], pins: Sequence[str], now: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`POST /v1/pair/offers`: a token as `pisa pair` makes it, offered by this device, which alone reads the
    offer and confirms its pairing. `$defs PairingOffer` with its QR and typed token. Never a signer: a key
    that signs device ops is granted only on the server."""
    now = now or _now()
    made = await create_token(conn, keys, name or "device", origin="app", signer=False, now=now)
    offer_id = "offer_" + rules.b64url(secrets.token_bytes(16))
    await conn.execute(
        "UPDATE pair_tokens SET offer_id = %s, offered_by = %s WHERE id = %s", (offer_id, device_id, made.id)
    )
    _log.info("pairing_offered", device_id=str(device_id))
    return {
        "offerId": offer_id,
        "state": "open",
        "qr": rules.qr_uri(addresses, pins[0], made.token, int(made.expires_at.timestamp())),
        "token": made.token,
        "expiresAt": made.expires_at.isoformat(),
    }


async def _offer_row(conn: AsyncConnection, offer_id: str, device_id: uuid.UUID) -> tuple[Any, ...]:
    row = await (
        await conn.execute(
            "SELECT id, used_at, expires_at FROM pair_tokens WHERE offer_id = %s AND offered_by = %s",
            (offer_id[:80], device_id),
        )
    ).fetchone()
    if row is None:  # another device's offer is not found either
        raise PisaError("not_found", "There is no such pairing offer.", "Make a new offer")
    return tuple(row)


async def offer_state(
    conn: AsyncConnection, offer_id: str, device_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    """`GET /v1/pair/offers/{offerId}`: `open` until a new device pairs with it; then its pairing's state,
    with what the offering phone needs to check the commitment and compute the SAS itself while it waits."""
    now = now or _now()
    await _expire(conn, now)
    token_id, used_at, expires_at = await _offer_row(conn, offer_id, device_id)
    out: dict[str, Any] = {"offerId": offer_id, "state": "open", "expiresAt": expires_at.isoformat()}
    row = await (
        await conn.execute(
            "SELECT id, state, name, platform, spki, device_key_sent, client_nonce, server_nonce, device_id, "
            "expires_at FROM pairings WHERE token_id = %s",
            (token_id,),
        )
    ).fetchone()
    if row is None:
        if used_at is not None:
            out["state"] = "refused"  # withdrawn before a device paired
        elif expires_at <= now:
            out["state"] = "expired"
        return out
    pairing_id, current, name, platform, spki, key_sent, client_nonce, server_nonce, new_id, until = row
    out |= {"state": current, "pairingId": pairing_id, "newDevice": {"name": name, "platform": platform}}
    if current == "awaiting_confirmation":
        out["expiresAt"] = until.isoformat()
        out["transcript"] = {
            "spki": spki,
            "deviceKey": key_sent,
            "clientNonce": client_nonce,
            "commitment": rules.commitment(server_nonce),
            "serverNonce": server_nonce,
        }
    if current == "confirmed":
        out["deviceId"] = str(new_id)
    return out


async def withdraw(
    conn: AsyncConnection, offer_id: str, device_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """`DELETE /v1/pair/offers/{offerId}`: the token is spent, and a pairing awaiting confirmation is
    refused."""
    now = now or _now()
    token_id, _, _ = await _offer_row(conn, offer_id, device_id)
    async with conn.transaction():
        await conn.execute(
            "UPDATE pair_tokens SET used_at = coalesce(used_at, %s), token = NULL WHERE id = %s",
            (now, token_id),
        )
        await conn.execute(
            "UPDATE pairings SET state = 'refused', decided_at = %s WHERE token_id = %s "
            "AND state = 'awaiting_confirmation'",
            (now, token_id),
        )
    _log.info("pairing_offer_withdrawn", device_id=str(device_id))


async def confirm_offer(
    conn: AsyncConnection, offer_id: str, device_id: uuid.UUID, pairing_id: str, sas: str,
    *, now: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`POST /v1/pair/offers/{offerId}/confirm`: the offering phone confirms the pairing whose digits the user
    compared; 409 when it is not this offer's pairing awaiting confirmation, or the digits differ."""
    now = now or _now()
    token_id, _, _ = await _offer_row(conn, offer_id, device_id)
    row = await (
        await conn.execute("SELECT token_id FROM pairings WHERE id = %s", (pairing_id[:80],))
    ).fetchone()
    if row is None or row[0] != token_id:
        raise PisaError(
            "pairing_code_invalid",
            "This pairing is not the one this offer is waiting for: nothing was confirmed.",
            "Check the offer again",
            status=409,
        )
    await decide(conn, pairing_id, True, now=now, offered_by=device_id, sas=sas)
    return await offer_state(conn, offer_id, device_id, now=now)


# --- session tokens -----------------------------------------------------------------------------------------
async def _paired_device(conn: AsyncConnection, device_id: str) -> uuid.UUID:
    """A device that can ask for a session: paired, not revoked, with a device key. 400 otherwise (the same
    answer for each case)."""
    try:
        device = uuid.UUID(device_id)
    except ValueError:
        device = None
    row = None
    if device is not None:
        row = await (
            await conn.execute(
                "SELECT 1 FROM devices WHERE id = %s AND revoked_at IS NULL AND device_key IS NOT NULL",
                (device,),
            )
        ).fetchone()
    if device is None or row is None:
        raise PisaError(
            "validation_failed", "There is no such paired device.", "Pair this device again (pisa pair)"
        )
    return device


async def session_nonce(
    conn: AsyncConnection, address: str, *, device_id: str | None = None, now: datetime | None = None
) -> dict[str, str]:
    """`GET /v1/devices/session/nonce`: 32 random bytes for the device key to sign, 60 s, single use.

    With `device_id` the nonce is bound to that paired device: only its session
    request spends it, and the newest MAX_OUTSTANDING_NONCES per device are kept (an older one is dropped), so
    no one behind the same NAT, CGNAT or proxy can starve it. Without it, at most MAX_OUTSTANDING_NONCES per
    client address and MAX_UNBOUND_NONCES in all (429)."""
    now = now or _now()
    address = address[:200] or "local"
    async with conn.transaction():
        await conn.execute("DELETE FROM session_nonces WHERE expires_at < %s", (now - timedelta(hours=1),))
        device = None if device_id is None else await _paired_device(conn, device_id)
        if device is not None:
            await conn.execute(
                "DELETE FROM session_nonces WHERE nonce_hash IN (SELECT nonce_hash FROM session_nonces WHERE "
                "device_id = %s AND used_at IS NULL AND expires_at > %s ORDER BY created_at DESC OFFSET %s)",
                (device, now, MAX_OUTSTANDING_NONCES - 1),
            )
        else:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.session_nonces'))")
            row = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE address = %s), count(*) FROM session_nonces WHERE "
                    "device_id IS NULL AND used_at IS NULL AND expires_at > %s",
                    (address, now),
                )
            ).fetchone()
            if row is not None and row[0] >= MAX_OUTSTANDING_NONCES:
                raise PisaError(
                    "rate_limited",
                    f"{MAX_OUTSTANDING_NONCES} session nonces are already outstanding from this address.",
                    "Use one, or wait a minute; or ask with deviceId",
                )
            if row is not None and row[1] >= MAX_UNBOUND_NONCES:
                _log.warning("session_nonces_full", outstanding=row[1])
                raise PisaError(
                    "rate_limited",
                    "Too many session nonces are outstanding.",
                    "Wait a minute; or ask with deviceId",
                )
        nonce = rules.new_nonce()
        await conn.execute(
            "INSERT INTO session_nonces (nonce_hash, address, created_at, expires_at, device_id) VALUES (%s, "
            "%s, %s, %s, %s)",
            # a bound nonce keeps its device and no address: never the two together
            (_hash(nonce), None if device is not None else address, now, now + NONCE_TTL, device),
        )
    return {"nonce": nonce, "expires_at": (now + NONCE_TTL).isoformat()}


def _auth_failed(message: str) -> PisaError:
    return PisaError("auth_failed", message, "Get a new nonce and sign it again, or pair this device again")


async def create_session(
    conn: AsyncConnection, device_id: str, nonce: str, signature: str, *, now: datetime | None = None
) -> dict[str, str]:
    """`POST /v1/devices/session`: the nonce is spent first (it is gone even if what follows fails), then the
    device key's signature over `pisa-session/1\\n<deviceId>\\n<nonce>` is checked. A 1 h token; only its hash
    is kept."""
    now = now or _now()
    spent = await (
        await conn.execute(
            "UPDATE session_nonces SET used_at = %s WHERE nonce_hash = %s AND used_at IS NULL RETURNING "
            "expires_at, device_id",
            (now, _hash(nonce)),
        )
    ).fetchone()
    if spent is None or spent[0] <= now:
        raise _auth_failed("The nonce is unknown, already used or expired.")
    try:
        device = uuid.UUID(device_id)
    except ValueError:
        raise _auth_failed("There is no such device.") from None
    if spent[1] is not None and spent[1] != device:  # a bound nonce serves its own device only
        raise _auth_failed("This nonce was asked for by another device.")
    row = await (
        await conn.execute("SELECT device_key, revoked_at FROM devices WHERE id = %s", (device,))
    ).fetchone()
    if row is None:
        raise _auth_failed("There is no such device.")
    der, revoked_at = row
    if revoked_at is not None:
        raise PisaError("device_revoked", "This device was revoked on the server.", "Pair this device again")
    if der is None:
        raise _auth_failed("This device was paired with a v1 code and has no device key: pair it again.")
    if not rules.verify_session(bytes(der), str(device), nonce, signature):
        _log.info("session_refused", why="signature")
        raise _auth_failed("The signature does not verify with this device's key.")
    token = rules.b64url(secrets.token_bytes(32))
    async with conn.transaction():
        await conn.execute("DELETE FROM device_sessions WHERE expires_at < %s", (now - timedelta(days=1),))
        await conn.execute(
            "INSERT INTO device_sessions (token_hash, device_id, created_at, expires_at) VALUES (%s, %s, %s, "
            "%s)",
            (_hash(token), device, now, now + SESSION_TTL),
        )
    _log.info("session_created", device_id=str(device))
    return {"token": token, "expires_at": (now + SESSION_TTL).isoformat()}
