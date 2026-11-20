"""The relay push: a notice sealed to the device, and the server's signature on what it sends the relay
(CONTRACT-2 §9; `relay/PROTOCOL.md`). Pure: no I/O.

The seal is HPKE (RFC 9180) in base mode with DHKEM(X25519, HKDF-SHA256), HKDF-SHA256 and ChaCha20-Poly1305,
`info` "pisa-push/2" and `aad` the relay's handle, through `pyhpke` (the `cryptography` package's own HPKE
takes no `aad` in its public interface). The plaintext is `{"v": 2, "notice": <pisa.notice>, "sentAt"}`, plus
`apns`,
how the banner is filed (its category, thread and interruption level), padded with JSON whitespace so the
sealed blob is exactly 512 bytes, or 2048 when it does not fit in 512. A notice too long for 2048 is cut
first: its text, then its title, then the fields a banner can do without. `sealed` is the encapsulated key (32
bytes) followed by the ciphertext, base64url without padding.
"""

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pyhpke import AEADId, CipherSuite, KDFId, KEMId

INFO = b"pisa-push/2"
ENC_BYTES = 32  # the X25519 encapsulated key
TAG_BYTES = 16  # ChaCha20-Poly1305
BUCKETS = (512, 2048)
REPLAY_WINDOW_S = 300  # the relay refuses a request whose timestamp is further than this from its clock
TIMESTAMP_HEADER = "X-Pisa-Relay-Timestamp"
SIGNATURE_HEADER = "X-Pisa-Relay-Signature"
_SUITE = CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.CHACHA20_POLY1305)
_CUT = "…"
# what a banner can do without, dropped in this order when even a cut text does not fit
_OPTIONAL = ("refs", "place", "undo", "wrist", "checkIn", "title")


class SealError(ValueError):
    """A blob that does not open: another key, another handle, or tampered with."""


def suite() -> CipherSuite:
    return _SUITE


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise SealError("The sealed blob is not base64url.") from None


def _dump(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def sent_at_text(moment: datetime) -> str:
    """`sentAt`: RFC 3339 in UTC, to the second, ending in Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plaintext(
    notice: dict[str, Any], apns: dict[str, Any] | None = None, *, sent_at: datetime
) -> dict[str, Any]:
    """`$defs/PushPlaintext`. `sentAt` is required: the extension drops a push older than 7 days, dated more
    than
    5 minutes ahead, or repeating a noticeId it saw, so the notice's id is the stable
    id of what it announces (the stored notification, the approval)."""
    body: dict[str, Any] = {"v": 2, "notice": notice, "sentAt": sent_at_text(sent_at)}
    if apns:
        body["apns"] = apns
    return body


def fitted(body: dict[str, Any]) -> bytes:
    """The plaintext as bytes, cut by the notice rules until it fits the largest bucket."""
    room = BUCKETS[-1] - ENC_BYTES - TAG_BYTES
    data = _dump(body)
    if len(data) <= room:
        return data
    notice = dict(body["notice"])
    for field in ("text", "title"):
        value = notice.get(field)
        if not isinstance(value, str):
            continue
        low, high = 1, len(value)
        while low < high:  # the longest prefix that fits
            mid = (low + high + 1) // 2
            notice[field] = value[:mid].rstrip() + _CUT
            if len(_dump(body | {"notice": notice})) <= room:
                low = mid
            else:
                high = mid - 1
        notice[field] = value[:low].rstrip() + _CUT
        data = _dump(body | {"notice": notice})
        if len(data) <= room:
            return data
    for field in _OPTIONAL:
        notice.pop(field, None)
        data = _dump(body | {"notice": notice})
        if len(data) <= room:
            return data
    raise SealError("The notice does not fit a push even when cut.")


def padded(data: bytes) -> bytes:
    """Padded with spaces (JSON whitespace: the padded text still parses) to the smallest bucket it fits."""
    for bucket in BUCKETS:
        room = bucket - ENC_BYTES - TAG_BYTES
        if len(data) <= room:
            return data + b" " * (room - len(data))
    raise SealError("The plaintext is longer than the largest bucket.")


def seal(body: dict[str, Any], public_key: bytes, handle: str) -> str:
    """The blob for the relay's `sealed`: exactly 512 or 2048 bytes before base64url."""
    pk = _SUITE.kem.deserialize_public_key(public_key)
    enc, context = _SUITE.create_sender_context(pk, INFO)
    ciphertext = context.seal(padded(fitted(body)), handle.encode())
    return _b64url(enc + ciphertext)


def open_sealed(sealed: str, private_key: bytes, handle: str) -> dict[str, Any]:
    """What the Notification Service Extension does (tests, and the reference for the app): open, parse."""
    raw = _unb64url(sealed)
    if len(raw) not in BUCKETS:
        raise SealError("The sealed blob is not one of the bucket sizes.")
    try:
        sk = _SUITE.kem.deserialize_private_key(private_key)
        context = _SUITE.create_recipient_context(raw[:ENC_BYTES], sk, INFO)
        data = context.open(raw[ENC_BYTES:], handle.encode())
    except Exception as exc:  # pyhpke raises its own errors for a wrong key or a tampered blob
        raise SealError("The sealed blob does not open with this key and handle.") from exc
    return json.loads(data)


# --- what the relay answers ---------------------------------------------------------------------------------
RelayRoute = Literal["push", "forget"]
RelayMeaning = Literal["done", "gone", "activity_gone"]
# The relay's real answers, per route (`relay/pisa_relay/app.py`, `relay/PROTOCOL.md`), and what each means.
# `push` (POST /v1/push): 202 handed to APNs (or the exact request again); 410 the relay no longer knows the
# handle; 422 a Live Activity's token is gone (only for a `liveactivity` push). `forget` (DELETE
# /v1/register): 204 forgotten; 410 it was gone already. Any other status (a 404 from a mistyped URL or a
# proxy, a 401, a 5xx) is a failure, logged, and never a sign that the handle is gone.
RELAY_ANSWERS: dict[RelayRoute, dict[int, RelayMeaning]] = {
    "push": {202: "done", 410: "gone", 422: "activity_gone"},
    "forget": {204: "done", 410: "gone"},
}


def relay_meaning(route: RelayRoute, status: int) -> RelayMeaning | None:
    """What the relay's `status` on `route` means; None for a failure."""
    return RELAY_ANSWERS[route].get(status)


# --- the request to the relay -------------------------------------------------------------------------------
def relay_key(secret: bytes) -> bytes:
    """The HMAC key: SHA-256 of the secret the relay gave the app, so the relay keeps only that hash."""
    return hashlib.sha256(secret).digest()


def relay_signature(secret: bytes, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over `<timestamp>\\n<body>`, base64url without padding."""
    message = str(timestamp).encode() + b"\n" + body
    return _b64url(hmac.new(relay_key(secret), message, hashlib.sha256).digest())


def relay_headers(secret: bytes, timestamp: int, body: bytes) -> dict[str, str]:
    """The request's headers, spelled as CONTRACT-2 §9 spells them."""
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: relay_signature(secret, timestamp, body),
    }


def relay_request(
    handle: str, kind: str, *, sealed: str | None = None, collapse_id: str | None = None,
    activity: dict[str, Any] | None = None,
) -> bytes:  # fmt: skip
    """The body of `POST <relay>/v1/push`: `{handle, type, collapse_id?, sealed?, activity?}`. A background
    wake carries neither a blob nor an activity."""
    body: dict[str, Any] = {"handle": handle, "type": kind}
    if collapse_id:
        body["collapse_id"] = collapse_id[:64]
    if sealed is not None:
        body["sealed"] = sealed
    if activity is not None:
        body["activity"] = activity
    return _dump(body)
