"""The signing: the phone's P-256 keys and one-use challenges it signs
after Face ID. A challenge names exactly what the signature allows (`subject`); the phone shows `canonical`,
asks Face ID and signs it with its Secure Enclave key (`.ecdsaSignatureMessageX962SHA256`, DER, base64). A
nonce works once and for 5 minutes.

The first key needs nothing but an iPhone paired with a host code; once one exists, a new
key needs a signature from a registered key over an `add_key` challenge naming it.

The user's account-level actions need the same signature, so one
copied device token can't take over: `device` (`op` and `target`: a pairing offer, revoking another device, a
provider key, connecting or disconnecting Google), and the `approval` (a `requires_biometric` approval).
Until the store holds a first key (a fresh install, before the iPhone registers its key) the token alone still
does them.

Any client signs with its own device key: the P-256 key it registered at pairing
(`devices.device_key`), named `device-<its device id>`. It is not stored here: it is the key the device
already proves at every session. The same proof as a Face ID key's (a single-use challenge, its 5 minutes,
the exact text, the device not revoked), and it signs:
- for the device that sends the request only: a device op, an approval's `pisa-approval/2` text issued to that
  device, an autonomy level, a skill;
- `add_key` for another device's new key, never for its own device (a device key does not make itself a Face
  ID key).
A device that registered a Face ID key signs with that key: its device key signs nothing here (the app's
approvals stay Face ID's, CONTRACT-2 §6).

A key a device key vouched for is stored as kind `device`, never `biometric`, with a device key's limits, and
records its voucher (`vouched_by`): revoking the voucher deletes it, and while the voucher is revoked it signs
nothing. It never vouches itself, and both rules follow the chain: revoking a device deletes the
keys its vouchees' devices vouched for, and so on down. A watch (the requesting device's platform,
whatever the key) never approves, sets an autonomy level, turns on a skill, vouches for or gets a key, and
signs no device op, `pairing_offer` included, signed or not and even before the first Face ID key (a
watch only rejects; erasing, revoking and the rest are done from a phone, and a lost watch is revoked there).
"""

import base64
import binascii
import hashlib
import secrets
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import approval_sign
from pisa.domain import autonomy as autonomy_rules
from pisa.domain import pairing as pairing_rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger

Action = Literal["autonomy", "add_key", "skill", "approval", "device"]
DeviceOp = Literal[
    "revoke_device",
    "provider_key",
    "google_connect",
    "google_disconnect",
    "place_key",
    "google_credentials",
    "messages_retention",
    "erase",
    "connector",
    "connector_credential",
    "connector_consent",
    "model_server",
    "pairing_offer",
    "pairing_confirm",
]
# a `device` signature travels in headers (the routes keep their bodies; DELETE has none)
NONCE_HEADER, SIGNATURE_HEADER, KEY_ID_HEADER = "x-pisa-nonce", "x-pisa-signature", "x-pisa-key-id"
CHALLENGE_TTL = timedelta(minutes=5)
# a key's kind: `biometric`, the phone's Face ID key (every action); `signer`, a device key granted on the
# server (`pisa pair --signer`), which signs device ops only; `device`, a client's own device key
# (`devices.device_key`, never in the store, named `device-<its device id>`) or a key a device key vouched for
BIOMETRIC, SIGNER = "biometric", "signer"
DEVICE = "device"
_log = get_logger("pisa.phone_keys")


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Check the request")


def _refused() -> PisaError:
    return PisaError(
        "biometric_required",
        "This needs a signature: Face ID in Pisa, or this client's own device key (key id "
        "device-<its device id>) over the challenge.",
        "Sign the challenge with Face ID, or with this device's key",
    )


def _key_device(key_id: str) -> uuid.UUID | None:
    """The device a `device-<uuid>` key id names; None for any other key id."""
    if not key_id.startswith(pairing_rules.DEVICE_KEY_PREFIX):
        return None
    try:
        return uuid.UUID(key_id.removeprefix(pairing_rules.DEVICE_KEY_PREFIX))
    except ValueError:
        return None


@dataclass(frozen=True)
class _Key:
    """A key that may sign: its X9.63 point, its kind, its device and that device's platform; for a device's
    own key (`own`, `device-<id>`), whether that device holds a Face ID key."""

    public: bytes
    kind: str
    device: uuid.UUID | None = None
    platform: str | None = None
    face_id: bool = False
    own: bool = False


# what a watch may never sign, whatever the key (a watch only rejects)
# a skill too: a skill can enable acts, so it is no reject; and every device op
WATCH_REFUSED: frozenset[str] = frozenset({"approval", "autonomy", "add_key", "skill", "device"})


def _refusal(key: _Key, action: Action, requester: uuid.UUID | None, platform: str | None) -> str | None:
    """Why this key may not sign this action for the device sending the request (`requester`, on
    `platform`), or None (the rules in the module's docstring)."""
    if platform == "watchos" and action in WATCH_REFUSED:
        return "a watch only rejects"
    if key.kind == SIGNER:  # a server-made signer's device key signs device ops only
        return None if action == "device" else "a signer key for a Face ID action"
    if key.kind != DEVICE:
        return None  # a Face ID key
    # a device key: a device's own, or one a device key vouched for
    if key.own and key.face_id:
        return "this device signs with its Face ID key"
    if action == "add_key":
        if not key.own:  # a key a device key vouched for never vouches (no chain of vouches)
            return "a vouched key vouches for no key"
        if key.platform == "watchos":
            return "a watch vouches for no key"
        return "a device key adds no key for its own device" if key.device == requester else None
    if requester is None or key.device != requester:
        return "a device key signs for its own device only"
    if action == "approval" and key.platform == "watchos":
        return "a watch only rejects an approval"
    return None


def _decode(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid(f"{what} must be base64.") from None


def public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise _invalid("public_key must be a P-256 key in X9.63 form.") from None


def canonical(action: Action, subject: dict[str, str], nonce: str, expires_at: str) -> str:
    if action == "autonomy":
        return autonomy_rules.canonical(subject["type"], subject["level"], nonce, expires_at)
    if action == "skill":
        what = (
            f"id={subject['skill_id']}\nname={subject['name']}\nreads={subject['reads']}\n"
            f"digest={subject['digest'][:16]}"
        )
        return f"pisa-skill\n{what}\nnonce={nonce}\nexpires_at={expires_at}"
    if action == "device":
        what = f"op={subject['op']}\ntarget={subject['target']}"
        return f"pisa-device\n{what}\nnonce={nonce}\nexpires_at={expires_at}"
    # the new key's fingerprint is in what the phone signs (the key itself, not only its name)
    what = f"key_id={subject['key_id']}\nfingerprint={fingerprint(subject['public_key'])}"
    return f"pisa-add-key\n{what}\nnonce={nonce}\nexpires_at={expires_at}"


def digest(text: str) -> str:
    """The SHA-256 hex of a text (UTF-8): an approval's summary, a provider key, a Google code."""
    return hashlib.sha256(text.encode()).hexdigest()


def device_subject(op: DeviceOp, target: str) -> dict[str, str]:
    """One line (control characters out, as the values) and a device id in lowercase, the
    same whichever casing the phone sent."""
    clean = "".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in target)
    return {"op": op, "target": clean.lower() if op == "revoke_device" else clean}


def fingerprint(public_key_b64: str) -> str:
    """The first 16 hex digits of the key's SHA-256 (what the phone shows beside "add this key")."""
    return hashlib.sha256(_decode(public_key_b64, "public_key")).hexdigest()[:16]


@dataclass(frozen=True)
class ChallengeRequest:
    """What the phone asks to sign (`POST /v1/phone/challenge`), as parsed: the action and the fields it
    needs (`skill`: skill_id; `approval`: approval_id and interrupt_id; `device`: op and target; `autonomy`:
    type and level; `add_key`: key_id and public_key)."""

    action: Action
    approval_id: str | None = None
    interrupt_id: str | None = None
    op: DeviceOp | None = None
    target: str | None = None
    skill_id: str | None = None
    type: str | None = None
    level: str | None = None
    key_id: str | None = None
    public_key: str | None = None


def _missing(message: str, action: str = "Check the request") -> PisaError:
    return PisaError("validation_failed", message, action)


class PhoneKeys:
    def __init__(self, pool: AsyncConnectionPool, *, owner: Owner, clock: Callable[[], datetime]) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    async def challenge_for(
        self,
        request: ChallengeRequest,
        device_id: uuid.UUID,
        *,
        skill: Callable[[str], Awaitable[dict[str, Any]]],
        approval_subject: Callable[[str, str, uuid.UUID], Awaitable[dict[str, str]]],
    ) -> dict[str, Any]:
        """The challenge for what the phone asks to sign (after Face ID): an autonomy level, a new key (once
        one exists), a skill turned on (`skill`: its challenge), an owner action of this device's, or an
        approval (`approval_subject`: contract 2's `pisa-approval/2`, bound to `device_id`, CONTRACT-2
        §6.2). 400 `validation_failed` when the action's fields are missing."""
        if request.action == "skill":
            if request.skill_id is None:
                raise _missing("A skill challenge needs skill_id.")
            return await skill(request.skill_id)
        if request.action == "approval":  # a requires_biometric approval (contract 2's interrupt)
            if request.approval_id is None:
                raise _missing("An approval challenge needs approval_id.")
            if request.interrupt_id is None:
                raise _missing("An approval challenge needs interrupt_id.")
            subject = await approval_subject(request.approval_id, request.interrupt_id, device_id)
            return await self.challenge_approval(subject)
        if request.action == "device":  # an owner action
            if request.op is None or request.target is None:
                raise _missing("A device challenge needs op and target.")
            return await self.challenge("device", device_subject(request.op, request.target))
        if request.action == "autonomy":
            if request.type is None or request.level is None:
                raise _missing("An autonomy challenge needs type and level.")
            subject = {"type": request.type, "level": request.level}
        else:
            if request.key_id is None or request.public_key is None:
                raise _missing("An add_key challenge needs key_id and public_key.", "Check it")
            subject = {"key_id": request.key_id, "public_key": request.public_key}
        return await self.challenge(request.action, subject)

    async def challenge(self, action: Action, subject: dict[str, str]) -> dict[str, Any]:
        now = self._clock()
        nonce = secrets.token_urlsafe(24)
        expires = now + CHALLENGE_TTL
        expires_text = expires.astimezone(self._owner.zone).isoformat()
        text = canonical(action, subject, nonce, expires_text)
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM phone_challenges WHERE expires_at < %s", (now - timedelta(days=1),)
            )
            await conn.execute(
                "INSERT INTO phone_challenges (nonce, action, canonical, subject, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (nonce, action, text, Jsonb(subject), now, expires),
            )
        return {"nonce": nonce, "canonical": text, "expires_at": expires_text, "action": action}

    async def challenge_approval(self, subject: dict[str, str]) -> dict[str, Any]:
        """Contract 2's approval challenge (CONTRACT-2 §6.1 step 4): the `pisa-approval/2` text over
        `subject` (`ApprovalsService.challenge_subject`: server, device, approval, interrupt, kind, digest,
        recipients count), a fresh nonce and its 5 minutes, written in UTC. The phone rebuilds the same text
        itself and signs it only when it is byte-identical."""
        now = self._clock()
        nonce = secrets.token_urlsafe(24)
        expires = now + CHALLENGE_TTL
        expires_text = expires.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        stored = subject | {"expires_at": expires_text}
        text = _approval_text(stored, nonce)
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM phone_challenges WHERE expires_at < %s", (now - timedelta(days=1),)
            )
            await conn.execute(
                "INSERT INTO phone_challenges (nonce, action, canonical, subject, created_at, expires_at) "
                "VALUES (%s, 'approval', %s, %s, %s, %s)",
                (nonce, text, Jsonb(stored), now, expires),
            )
        return {"nonce": nonce, "canonical": text, "expires_at": expires_text, "action": "approval"}

    async def verify_approval(
        self, expected: dict[str, str], nonce: str, signature: str, key_id: str
    ) -> None:
        """RAIL (CONTRACT-2 §6.4 checks 5 and 6): a contract 2 approval challenge with this nonce, unused, in
        time, issued to this device for this approval, interrupt, kind, digest and count (`expected`, from the
        row as it is now), and the signature over the canonical text rebuilt by the server, by a Face ID key
        or by a device key of the device in `expected` (its own `device-<id>`, or a key vouched by a device
        key; never a server-made signer's key). A watch approves with no key at all. The nonce is spent at the
        first try, either way. 403 `biometric_required` otherwise."""
        requester = _uuid(expected.get("device"))
        found, key, platform = await self._spend(nonce, key_id, requester)
        if found is None or key is None:
            _log.info("phone_signature_refused", why="no challenge or key")
            raise _refused()
        if why := _refusal(key, "approval", requester, platform):
            _log.info("phone_signature_refused", why=why)
            raise _refused()
        found_action, text, subject, expires_at = found
        if (
            found_action != "approval"
            or "interrupt_id" not in subject
            or any(subject.get(k) != v for k, v in expected.items())
            or expires_at < self._clock()
        ):
            _log.info("phone_signature_refused", why="another challenge, device or content, or late")
            raise _refused()
        try:
            rebuilt = _approval_text(subject, nonce)
        except (KeyError, ValueError):
            raise _refused() from None
        try:
            if rebuilt != text:
                raise InvalidSignature
            public_key(key.public).verify(
                _decode(signature, "signature"), rebuilt.encode(), ec.ECDSA(hashes.SHA256())
            )
        except (InvalidSignature, PisaError):
            _log.info("phone_signature_refused", why="signature")
            raise _refused() from None

    async def _spend(
        self, nonce: str, key_id: str, requester: uuid.UUID | None, *, vouch: bool = False
    ) -> tuple[Any, _Key | None, str | None]:
        """Spend the nonce (its challenge, or None), find the key (None for an unknown one) and the platform
        of the device sending the request (`requester`). A key of a revoked device, or one vouched for by a
        device since revoked, is refused by name. `device-<id>` is the device key of the requester, or with
        `vouch` (an `add_key`) of any paired device; any other key id is the store's."""
        now = self._clock()
        owner = _key_device(key_id)
        mine = owner is not None and (owner == requester or vouch)
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE phone_challenges SET used_at = %s WHERE nonce = %s AND used_at IS NULL "
                    "RETURNING action, canonical, subject, expires_at",
                    (now, nonce),
                )
            ).fetchone()
            asking = None
            if requester is not None:
                asking = await (
                    await conn.execute("SELECT platform FROM devices WHERE id = %s", (requester,))
                ).fetchone()
            key: _Key | None
            if mine:
                found = await (
                    await conn.execute(  # the key the device proves at every session (pairing)
                        "SELECT d.device_key, d.revoked_at IS NOT NULL, d.platform, EXISTS (SELECT 1 "
                        "FROM phone_keys k WHERE k.device_id = d.id AND k.kind = %s) FROM devices d "
                        "WHERE d.id = %s",
                        (BIOMETRIC, owner),
                    )
                ).fetchone()
                revoked = found is not None and bool(found[1])
                key = (
                    None
                    if found is None or found[0] is None or revoked
                    else _Key(_point(bytes(found[0])), DEVICE, owner, found[2], bool(found[3]), own=True)
                )
            else:
                stored = await (
                    await conn.execute(  # a key of a revoked device, or vouched by one, signs nothing
                        # any voucher up the chain (the devices whose keys vouched for the keys of
                        # the device that vouched for this one, and so on; UNION ends a cycle)
                        "SELECT k.public_key, k.kind, k.device_id, d.platform FROM phone_keys k "
                        "JOIN devices d ON d.id = k.device_id WHERE k.key_id = %s AND d.revoked_at IS NULL "
                        "AND NOT EXISTS (WITH RECURSIVE up(device) AS (SELECT k.vouched_by "
                        "WHERE k.vouched_by IS NOT NULL UNION SELECT p.vouched_by FROM phone_keys p "
                        "JOIN up ON p.device_id = up.device WHERE p.vouched_by IS NOT NULL) "
                        "SELECT 1 FROM up JOIN devices v ON v.id = up.device WHERE v.revoked_at IS NOT NULL)",
                        (key_id,),
                    )
                ).fetchone()
                key = None if stored is None else _Key(bytes(stored[0]), stored[1], stored[2], stored[3])
                revoked = (
                    key is None
                    and await (
                        await conn.execute("SELECT 1 FROM phone_keys WHERE key_id = %s", (key_id,))
                    ).fetchone()
                    is not None
                )
        if revoked and mine:
            _log.info("phone_signature_refused", why="device key of a revoked device")
            raise PisaError(
                "biometric_required",
                "This key belongs to a revoked device: it signs nothing.",
                "Pair the device again with `pisa pair <name>` on the server",
            )
        if revoked:  # the iPhone was paired again (its old key's device is revoked), or its voucher was
            _log.info("phone_signature_refused", why="key of a revoked device, or vouched by one")
            raise PisaError(
                "biometric_required",
                "This key belongs to a revoked device, or was vouched for by one.",
                "Reset the keys on the server: pisa phone-keys reset --keep <the iPhone's device id> "
                "(see pisa devices list), then try again",
            )
        return row, key, asking[0] if asking else None

    async def _platform(self, device_id: uuid.UUID) -> str | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT platform FROM devices WHERE id = %s", (device_id,))
            ).fetchone()
        return row[0] if row else None

    async def any_key(self) -> bool:
        async with self._pool.connection() as conn:
            return await (await conn.execute("SELECT 1 FROM phone_keys LIMIT 1")).fetchone() is not None

    async def guard(
        self,
        op: DeviceOp,
        target: str,
        nonce: str | None,
        signature: str | None,
        key_id: str | None,
        *,
        always: bool = False,
        device_id: uuid.UUID | None = None,
    ) -> None:
        """RAIL: an owner action needs a signature over a `device` challenge naming it, once the store holds
        a key: a Face ID key's, a server-made signer's, or a device key of the device sending the request
        (`device_id`). 403 `biometric_required` otherwise. `always` (their deletion by family): signed even on
        a fresh install (no Face ID key yet: the device's own key signs it). A watch signs no device op, and
        is refused one even unsigned on a fresh install."""
        if device_id is not None and await self._platform(device_id) == "watchos":
            _log.info("phone_signature_refused", why="a watch only rejects", op=op)
            raise _refused()
        if not always and not await self.any_key():
            return  # a fresh install: the first key isn't there yet (only a host-paired iPhone registers it)
        if not (nonce and signature and key_id):
            _log.info("phone_signature_refused", why="unsigned owner action", op=op)
            raise _refused()
        await self.verify("device", device_subject(op, target), nonce, signature, key_id, device_id=device_id)

    async def verify(
        self,
        action: Action,
        subject: dict[str, str],
        nonce: str,
        signature: str,
        key_id: str,
        device_id: uuid.UUID | None = None,
    ) -> str:
        """RAIL: the signature of a key that may sign this action over its own challenge, unused and in time;
        the nonce is spent either way. `device_id`: the device sending the request (whose device keys may
        sign; for `add_key`, the device the new key is for, whose own keys may not vouch). Returns the signing
        key's kind. 403 `biometric_required` otherwise."""
        now = self._clock()
        row, key, platform = await self._spend(nonce, key_id, device_id, vouch=action == "add_key")
        if row is None or key is None:
            _log.info("phone_signature_refused", why="no challenge or key")
            raise _refused()
        if why := _refusal(key, action, device_id, platform):
            _log.info("phone_signature_refused", why=why, action=action)
            raise _refused()
        found_action, text, found_subject, expires_at = row
        if found_action != action or found_subject != subject or expires_at < now:
            _log.info("phone_signature_refused", why="another challenge or late")
            raise _refused()
        try:
            public_key(key.public).verify(
                _decode(signature, "signature"), text.encode(), ec.ECDSA(hashes.SHA256())
            )
        except (InvalidSignature, PisaError):
            _log.info("phone_signature_refused", why="signature")
            raise _refused() from None
        return key.kind

    async def register(
        self,
        key_id: str,
        public_key_b64: str,
        label: str | None,
        *,
        device_id: uuid.UUID,
        platform: str,
        nonce: str | None = None,
        signature: str | None = None,
        signer_key_id: str | None = None,
    ) -> dict[str, Any]:
        """A key belongs to the paired device that registered it. The first key of the store
        needs no signature but only a phone (iOS: the Secure Enclave) may register it, and the check and the
        insert are one locked step (two at once can't both be first); every later key needs a registered
        key's signature over an `add_key` challenge naming it and its fingerprint. A key a
        Face ID key vouched for is a Face ID key; one another paired device's key vouched for (never this
        device's own) is a `device` key, with a device key's limits, and goes when that device is
        revoked. A watch neither vouches nor gets a key. A key id `device-...` names a device's own
        key, never a stored one.
        """
        if key_id.startswith(pairing_rules.DEVICE_KEY_PREFIX):
            raise _invalid("A key_id starting with device- names a device's own key: choose another.")
        if platform == "watchos":  # a watch only rejects; it needs no key
            _log.info("phone_key_refused", why="a watch")
            raise _refused()
        raw = _decode(public_key_b64, "public_key")
        public_key(raw)
        kind, voucher = BIOMETRIC, None
        if nonce and signature and signer_key_id:
            vouched = await self.verify(
                "add_key",
                {"key_id": key_id, "public_key": public_key_b64},
                nonce,
                signature,
                signer_key_id,
                device_id=device_id,
            )
            voucher = await self._key_owner(signer_key_id)
            kind = BIOMETRIC if vouched == BIOMETRIC else DEVICE
            signed = True
        else:
            signed = False
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            any_key = await _first_key_rule(conn, platform, device_id)
            if any_key is not None and not signed:
                raise _refused()
            made = await conn.execute(
                "INSERT INTO phone_keys (key_id, public_key, label, device_id, created_at, kind, vouched_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key_id) DO NOTHING",
                (key_id, raw, label, device_id, now, kind, voucher),
            )
            if not made.rowcount:
                raise _invalid("That key_id is taken.")
        _log.info("phone_key_registered", first=any_key is None, kind=kind)
        return {
            "key_id": key_id,
            "label": label,
            "created_at": now.astimezone(self._owner.zone).isoformat(),
        }

    async def _key_owner(self, key_id: str) -> uuid.UUID | None:
        """The device a key belongs to: a `device-<id>` key's device, or a stored key's."""
        if (owner := _key_device(key_id)) is not None:
            return owner
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT device_id FROM phone_keys WHERE key_id = %s", (key_id,))
            ).fetchone()
        return row[0] if row else None


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _point(der: bytes) -> bytes:
    """A device key (SubjectPublicKeyInfo DER, as pairing stores it) as the X9.63 point `public_key` reads; an
    empty point (refused by `public_key`) for a stored key that is not P-256."""
    try:
        return pairing_rules.x963(der)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return b""


def _approval_text(subject: dict[str, str], nonce: str) -> str:
    """`pisa-approval/2` (CONTRACT-2 §6.2) from a stored contract 2 approval challenge."""
    return approval_sign.canonical(
        server=subject["server"],
        device=subject["device"],
        approval_id=subject["approval_id"],
        interrupt_id=subject["interrupt_id"],
        decision="approve",
        kind=subject["kind"],
        digest_hex=subject["digest"],
        recipients_count=int(subject["recipients_count"]),
        nonce=nonce,
        expires_at=subject["expires_at"],
    )


async def _first_key_rule(conn: Any, platform: str, device_id: uuid.UUID) -> object:
    """One locked step decides the first key (`/v1/phone/keys`), and only an iPhone
    registers it: one paired with a code made on the host (the platform is the device's own
    claim, a host code is the user's). Returns a row when a Face ID key exists already: a signer's device key
    alone (its Face ID key's iPhone since revoked) does not make the store's first key."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.phone_keys'))")
    any_key = await (
        await conn.execute("SELECT 1 FROM phone_keys WHERE kind = %s LIMIT 1", (BIOMETRIC,))
    ).fetchone()
    if any_key is None and platform != "ios":
        raise PisaError(
            "validation_failed", "Only the phone registers Pisa's first key.", "Use Pisa on your iPhone"
        )
    if any_key is None:
        via = await (
            await conn.execute("SELECT paired_via FROM devices WHERE id = %s", (device_id,))
        ).fetchone()
        if via is None or via[0] != "host":
            _log.info("phone_key_refused", why="first key from a device paired from the app")
            raise PisaError(
                "validation_failed",
                "Pisa's first Face ID key comes only from an iPhone paired with a code made on the server.",
                "Pair the iPhone with `pisa pair` on the server, then open Pisa",
            )
    return any_key
