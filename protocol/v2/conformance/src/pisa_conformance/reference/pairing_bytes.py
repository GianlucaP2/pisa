"""The bytes of pairing, sessions, the relay and the sealed push (CONTRACT-2 §3.4, §4.3, §9).

Written from the specification text, independently of any backend's code, so that the vectors it writes
(fixtures/pairing/*.json) cross-check implementations. HPKE is implemented here from RFC 9180 (base mode,
DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20-Poly1305) and checked in the tests against RFC 9180 A.2.1 and
against `cryptography`'s own HPKE.

Encodings, as the contract fixes them:
- b64url: base64url without padding (MACs, the relay signature, the sealed blob);
- the SPKI pin is the one exception: standard base64 WITH padding (44 characters), as `s_pisa_endpoints.spki`;
- the pairing token is 16 bytes, base32 without padding (26 characters, A-Z2-7).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization

# ---------- encodings ----------


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def spki_pin(spki_der: bytes) -> str:
    """SHA-256 of the TLS key's SubjectPublicKeyInfo (DER), standard base64 with padding."""
    return base64.b64encode(hashlib.sha256(spki_der).digest()).decode()


def token_text(token: bytes) -> str:
    if len(token) != 16:
        raise ValueError("the pairing token is 16 bytes")
    return base64.b32encode(token).decode().rstrip("=")


def token_from_typed(typed: str) -> bytes:
    """The typed fallback: any case; spaces and dashes ignored."""
    t = "".join(c for c in typed if c not in " -").upper()
    if len(t) != 26 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in t):
        raise ValueError("not a pairing code")
    return base64.b32decode(t + "=" * 6)


# ---------- the QR link (§4.3 step 1) ----------


ADDR_SAFE = ":/[]@"  # left literal in an address; everything else outside RFC 3986's unreserved set is encoded


def qr_link(addrs: list[str], pin: str, token: bytes, exp: int) -> str:
    """pisa://pair?v=1&addrs=<a1>,<a2>&spki=<pin>&token=<token>&exp=<unix seconds>. Each address is percent-encoded
    except ':/[]@' (so its own comma is %2C, and ?, &, =, +, # and % are encoded); the commas between addresses are
    literal. The pin is fully percent-encoded (its +, / and = too). A reader splits on & and , BEFORE decoding,
    and percent-decodes without turning + into a space."""
    a = ",".join(quote(x, safe=ADDR_SAFE) for x in addrs)
    return f"pisa://pair?v=1&addrs={a}&spki={quote(pin, safe='')}&token={token_text(token)}&exp={exp}"


def parse_qr(link: str) -> dict[str, Any]:
    u = urlsplit(link)
    if u.scheme != "pisa" or u.netloc != "pair":
        raise ValueError("not a pairing link")
    raw: dict[str, str] = {}
    for part in u.query.split("&"):
        k, _, v = part.partition("=")
        raw.setdefault(k, v)
    if raw.get("v") != "1":
        raise ValueError("another version of the pairing link")
    token_from_typed(raw["token"])
    return {"v": 1, "addrs": [unquote(a) for a in raw["addrs"].split(",") if a], "spki": unquote(raw["spki"]),
            "token": raw["token"], "exp": int(raw["exp"])}


# ---------- the transcript, the MACs and the SAS (§4.3 steps 3-5) ----------


def transcript(pin: str, device_key: str, client_nonce: str) -> bytes:
    """pisa-pair/1, the pin, deviceKey and clientNonce, each exactly as sent, joined by newlines (UTF-8)."""
    return "\n".join(["pisa-pair/1", pin, device_key, client_nonce]).encode("utf-8")


def app_mac(token: bytes, tr: bytes) -> str:
    return b64url(hmac.new(token, tr, hashlib.sha256).digest())


def server_mac(token: bytes, tr: bytes, server_nonce: str) -> str:
    return b64url(hmac.new(token, tr + b"\n" + server_nonce.encode("utf-8"), hashlib.sha256).digest())


def sas(tr: bytes, server_nonce: str) -> str:
    """6 digits: SHA-256('pisa-sas/1' \\n transcript \\n serverNonce), first 4 bytes big-endian, mod 1 000 000."""
    h = hashlib.sha256(b"pisa-sas/1\n" + tr + b"\n" + server_nonce.encode("utf-8")).digest()
    return f"{struct.unpack('>I', h[:4])[0] % 1_000_000:06d}"


def sas_commitment(server_nonce: str) -> str:
    """C = SHA-256('pisa-sas-commit/1\\n' || serverNonce), base64url without padding. The server sends it in the
    pair-begin response, BEFORE it has seen the app's transcript; it reveals serverNonce in the pair response."""
    return b64url(hashlib.sha256(b"pisa-sas-commit/1\n" + server_nonce.encode("utf-8")).digest())


def reveal_matches(commitment: str, server_nonce: str) -> bool:
    """The app checks the revealed serverNonce against C before it shows any digits; a mismatch aborts pairing."""
    return hmac.compare_digest(sas_commitment(server_nonce), commitment)


def sas_display(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


# ---------- sessions (§4.3 step 6) ----------


def session_text(device_id: str, nonce: str) -> bytes:
    return f"pisa-session/1\n{device_id}\n{nonce}".encode("utf-8")


# ---------- the relay's sending API (§9) ----------


def relay_key(secret: bytes) -> bytes:
    """The relay stores only sha256(secret), which is exactly the HMAC key."""
    if len(secret) != 32:
        raise ValueError("the relay secret is 32 bytes")
    return hashlib.sha256(secret).digest()


def relay_signature(secret: bytes, timestamp: int, body: bytes) -> str:
    return b64url(hmac.new(relay_key(secret), str(timestamp).encode() + b"\n" + body, hashlib.sha256).digest())


def relay_headers(secret: bytes, timestamp: int, body: bytes) -> dict[str, str]:
    return {"Content-Type": "application/json", "X-Pisa-Relay-Timestamp": str(timestamp),
            "X-Pisa-Relay-Signature": relay_signature(secret, timestamp, body)}


# What the server does with each relay answer (CONTRACT-2 §9). 422 is for a liveactivity push only: that
# activity's token is dead (APNs 410, or 400 BadDeviceToken, for it); the handle stays registered.
RELAY_ANSWERS = {
    202: "sent",                    # handed to APNs (or queued), or an exact repeat of an accepted push (sent once): count it as sent
    400: "log",                     # the body is not as the relay's API says
    401: "log",                     # unknown signature, a timestamp outside the window, or a Live Activity token not registered for the handle
    404: "delete_registration",     # the relay no longer knows the handle
    410: "delete_registration",
    422: "drop_activity_token",     # liveactivity only: forget that activity's token, keep the registration
    413: "log",                     # payload too large: log it, do not retry, do not delete the registration (E1 R18)
    429: "log",                     # past the handle's rate limit; the notice stays in the app's list
}


def relay_answer_action(status: int, push_type: str) -> str:
    """What the user's server does with the relay's answer to POST /v1/push."""
    if status == 422 and push_type != "liveactivity":
        return "log"  # a 422 only means something for a Live Activity update
    if 500 <= status <= 599:
        return "log"  # 500 (the relay's storage key failed), 502 (Apple), 503: never a sign the handle is gone; keep it
    return RELAY_ANSWERS.get(status, "log")


# The relay's registration API, called by the app (POST /v1/register, POST /v1/register/activity), and what the
# app does with each answer. Not the user's server's concern; listed so the app's requirements are testable.
REGISTER_ANSWERS = {
    201: "store_handle_and_secret",       # register: handle + secret to the user's server (PUT /v1/devices/me/push); app_secret stays on the phone
    204: "done",                          # register/activity, or DELETE /v1/register
    400: "log",
    200: "show_devices",                  # register/devices: the subscription's registrations
    401: "log",                           # an app-signed request whose app MAC did not verify
    403: "show_subscription_problem",     # subscription_not_verified: not Apple's, expired, stale, refunded or revoked
    409: "ask_to_free_a_device",          # too_many_devices: 10 handles, all used within 30 days
    413: "log",
    429: "retry_later",                   # too_many_registrations: 5 an hour per subscription, counted after the transaction verifies
}
RELAY_LIMITS = {"handlesPerSubscription": 10, "evictIdleDays": 30, "registerPerSubscriptionHour": 5,
                "activityTokensKeptPerKind": 4, "transactionMaxAgeS": 7 * 24 * 3600, "pushBodyMaxBytes": 8192, "replayKeepS": 600}


# The relay's error codes on the app's API ({"error": code}) and what the app does.
REGISTER_ERRORS = {
    "malformed": "log",
    "subscription_not_verified": "show_subscription_problem",
    "account_not_verified": "show_subscription_problem",   # the transaction names an account key this phone does not hold
    "too_many_devices": "open_devices_screen",              # 409: list the devices, let the user remove one
    "too_large": "log",
    "too_many_registrations": "retry_later",
    "unknown_device": "refresh_devices",                    # 404 from devices/remove
    "not_allowed": "explain_not_allowed",                   # 403 from devices/remove: needs the account key, itself, or a target idle 30 days
}


def may_remove_device(*, signer_proved_account_key: bool, target_is_signer: bool, target_idle_days: int,
                      signer_registered_first: bool = False) -> bool:
    """POST /v1/register/devices/remove (E1 N3b). The order of registration is deliberately NOT a condition:
    `signer_registered_first` is accepted only to show that it changes nothing."""
    del signer_registered_first
    return signer_proved_account_key or target_is_signer or target_idle_days >= 30


def register_answer_action(status: int, error: str | None = None) -> str:
    if 500 <= status <= 599:
        return "retry_later"
    if error is not None:
        return REGISTER_ERRORS.get(error, "log")
    return REGISTER_ANSWERS.get(status, "log")


# ---------- the app's own requests to the relay (its app_secret; the relay's API, stated in CONTRACT-2 §9) ----------
# app_secret comes back from POST /v1/register beside secret. It never leaves the phone (this-device-only keychain):
# the user's server gets handle, secret and public_key only.

APP_MAC_LABEL = b"pisa-relay-app/1\n"


def relay_app_mac(app_secret: bytes, method: str, path: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256(SHA-256(app_secret), 'pisa-relay-app/1\\n' METHOD ' ' PATH '\\n' timestamp '\\n' body), base64url
    without padding. Covers the method and the path, so a signed request cannot be replayed to another route."""
    if len(app_secret) != 32:
        raise ValueError("app_secret is 32 bytes")
    if method != method.upper() or "?" in path or not path.startswith("/"):
        raise ValueError("METHOD upper case; PATH exactly as sent, no host, no query")
    msg = APP_MAC_LABEL + method.encode() + b" " + path.encode() + b"\n" + str(timestamp).encode() + b"\n" + body
    return b64url(hmac.new(hashlib.sha256(app_secret).digest(), msg, hashlib.sha256).digest())


def relay_app_headers(app_secret: bytes, method: str, path: str, timestamp: int, body: bytes) -> dict[str, str]:
    return {"Content-Type": "application/json", "X-Pisa-Relay-Timestamp": str(timestamp),
            "X-Pisa-Relay-Signature": relay_app_mac(app_secret, method, path, timestamp, body)}


def app_account_token(account_key: bytes) -> str:
    """The StoreKit appAccountToken set at purchase: UUIDv8 (RFC 9562) of SHA-256('pisa-relay/app-account-token\\n'
    || account_key)[0:16]. The 32-byte account key lives in the iCloud Keychain, so every phone of the user has it."""
    if len(account_key) != 32:
        raise ValueError("the account key is 32 bytes")
    d = bytearray(hashlib.sha256(b"pisa-relay/app-account-token\n" + account_key).digest()[:16])
    d[6] = (d[6] & 0x0F) | 0x80
    d[8] = (d[8] & 0x3F) | 0x80
    h = d.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


RELAY_MODE_FORBIDDEN = ("progress", "time", "staleDate")  # staleDate from the plan would leak the leave time (E1 gap 2)


def _refuse_day_facts(fields: Any) -> None:
    bad = [f for f in RELAY_MODE_FORBIDDEN if f in fields]
    if bad:
        raise ValueError(f"relay mode sends no {', '.join(bad)}: nothing about the user's day")


def live_activity_body(handle: str, token: str, event: str, timestamp: int, priority: int = 5, **optional: Any) -> dict[str, Any]:
    """A relay-mode Live Activity update (E1 R5): no progress, no time and no staleDate, nor anything else about the
    user's day. The app fills them in when it is open."""
    _refuse_day_facts(optional)
    activity: dict[str, Any] = {"token": token, "event": event, "priority": priority, "timestamp": timestamp, **optional}
    return {"handle": handle, "type": "liveactivity", "activity": activity}


def live_activity_aps(activity: dict[str, Any]) -> dict[str, Any]:
    """The aps the relay sends APNs for a relay-mode update: an EMPTY content-state (APNs requires one; the widget
    keeps its last values until the app is opened), plus the optional fields the server gave. No stale-date.
    A push-to-start also needs attributes-type and attributes (APNs requires both): attributes is {} (content-free),
    so the app's ActivityAttributes and ContentState must both decode from {}."""
    _refuse_day_facts(activity)
    if activity["event"] not in ("start", "update", "end"):
        raise ValueError("event is start, update or end")
    if "attributes" in activity and activity["attributes"] != {}:
        raise ValueError("relay mode starts with attributes {}: nothing about the user's day")
    aps: dict[str, Any] = {"timestamp": activity["timestamp"], "event": activity["event"], "content-state": {}}
    if activity["event"] == "start":
        if not activity.get("attributesType"):
            raise ValueError("a push-to-start needs attributesType")
        aps["attributes-type"] = activity["attributesType"]
        aps["attributes"] = {}
        aps["input-push-token"] = 1
    elif "attributesType" in activity:
        raise ValueError("attributesType is for a start only")
    if "dismissalDate" in activity:
        if activity["event"] != "end":
            raise ValueError("dismissalDate is for an end only")
        aps["dismissal-date"] = activity["dismissalDate"]
    return aps


# ---------- HPKE, RFC 9180 base mode, single shot ----------

KEM_ID, KDF_ID, AEAD_ID = 0x0020, 0x0001, 0x0003
SUITE_KEM = b"KEM" + KEM_ID.to_bytes(2, "big")
SUITE = b"HPKE" + KEM_ID.to_bytes(2, "big") + KDF_ID.to_bytes(2, "big") + AEAD_ID.to_bytes(2, "big")


def _extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()


def _expand(prk: bytes, info: bytes, n: int) -> bytes:
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


def _lextract(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _extract(salt, b"HPKE-v1" + suite + label + ikm)


def _lexpand(suite: bytes, prk: bytes, label: bytes, info: bytes, n: int) -> bytes:
    return _expand(prk, n.to_bytes(2, "big") + b"HPKE-v1" + suite + label + info, n)


def _raw(pub: X25519PublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def hpke_key_schedule(shared_secret: bytes, info: bytes) -> tuple[bytes, bytes]:
    psk_id_hash = _lextract(SUITE, b"", b"psk_id_hash", b"")
    info_hash = _lextract(SUITE, b"", b"info_hash", info)
    ctx = b"\x00" + psk_id_hash + info_hash
    secret = _lextract(SUITE, shared_secret, b"secret", b"")
    return _lexpand(SUITE, secret, b"key", ctx, 32), _lexpand(SUITE, secret, b"base_nonce", ctx, 12)


def hpke_encap(pk_r: bytes, sk_e: bytes) -> tuple[bytes, bytes]:
    e = X25519PrivateKey.from_private_bytes(sk_e)
    enc = _raw(e.public_key())
    dh = e.exchange(X25519PublicKey.from_public_bytes(pk_r))
    eae_prk = _lextract(SUITE_KEM, b"", b"eae_prk", dh)
    return _lexpand(SUITE_KEM, eae_prk, b"shared_secret", enc + pk_r, 32), enc


def hpke_seal(pk_r: bytes, info: bytes, aad: bytes, pt: bytes, sk_e: bytes) -> tuple[bytes, bytes]:
    shared, enc = hpke_encap(pk_r, sk_e)
    key, nonce = hpke_key_schedule(shared, info)
    return enc, ChaCha20Poly1305(key).encrypt(nonce, pt, aad)


def hpke_open(sk_r: bytes, enc: bytes, info: bytes, aad: bytes, ct: bytes) -> bytes:
    r = X25519PrivateKey.from_private_bytes(sk_r)
    dh = r.exchange(X25519PublicKey.from_public_bytes(enc))
    eae_prk = _lextract(SUITE_KEM, b"", b"eae_prk", dh)
    shared = _lexpand(SUITE_KEM, eae_prk, b"shared_secret", enc + _raw(r.public_key()), 32)
    key, nonce = hpke_key_schedule(shared, info)
    return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)


# ---------- the sealed push (§9) ----------

PUSH_INFO = b"pisa-push/2"
SIZES = (512, 2048)          # enc (32) + ciphertext (plaintext + 16)
OVERHEAD = 32 + 16


def push_plaintext(notice: dict[str, Any], apns: dict[str, Any] | None = None, *, sent_at: str) -> bytes:
    """{"v": 2, "notice", "apns"?} as compact UTF-8 JSON, padded with trailing spaces to 464 or 2000 bytes (so enc +
    ciphertext is exactly 512 or 2048). The padding is JSON whitespace: the padded text still parses."""
    doc: dict[str, Any] = {"v": 2, "notice": notice, "sentAt": sent_at}
    if apns:
        doc["apns"] = apns
    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    for size in SIZES:
        if len(body) <= size - OVERHEAD:
            return body + b" " * (size - OVERHEAD - len(body))
    raise ValueError("notice too long: the server cuts it first (text, then title, then optional fields)")


# ---------- what the Notification Service Extension accepts (E1 R11: no replay of old sealed blobs) ----------

NSE_WINDOW_S = 7 * 24 * 3600   # a push whose sentAt is older than this is dropped
NSE_SEEN_MAX = 1000            # the seen-set keeps at most this many noticeIds (oldest evicted first)
NSE_FUTURE_S = 300             # clock skew allowed for a sentAt ahead of the phone's clock


class NoticeGate:
    """The NSE's bounded seen-set. A noticeId seen within the window is a repeat; an id older than the window can
    be forgotten, because its push would be dropped as stale anyway."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}   # noticeId -> sentAt (unix seconds), insertion-ordered

    def accept(self, plaintext: dict[str, Any], now: int) -> str:
        """'show', or why it is dropped: 'stale' (sentAt older than 7 days), 'future' (more than 5 minutes ahead),
        'repeat', or 'invalid'."""
        from datetime import datetime
        try:
            sent = int(datetime.fromisoformat(plaintext["sentAt"].replace("Z", "+00:00")).timestamp())
            nid = plaintext["notice"]["noticeId"]
        except (KeyError, TypeError, ValueError):
            return "invalid"
        if now - sent > NSE_WINDOW_S:
            return "stale"
        if sent - now > NSE_FUTURE_S:
            return "future"  # a far-future sentAt would otherwise pin its id in the seen-set past the window
        if nid in self.seen:
            return "repeat"
        self.seen[nid] = sent
        for old in [k for k, t in self.seen.items() if now - t > NSE_WINDOW_S]:
            del self.seen[old]
        while len(self.seen) > NSE_SEEN_MAX:
            del self.seen[next(iter(self.seen))]
        return "show"


def seal_push(device_public: bytes, handle: str, plaintext: bytes, sk_e: bytes) -> str:
    enc, ct = hpke_seal(device_public, PUSH_INFO, handle.encode("utf-8"), plaintext, sk_e)
    blob = enc + ct
    assert len(blob) in SIZES
    return b64url(blob)


def open_push(device_private: bytes, handle: str, sealed: str) -> dict[str, Any]:
    blob = unb64url(sealed)
    if len(blob) not in SIZES:
        raise ValueError("a sealed push is 512 or 2048 bytes")
    return json.loads(hpke_open(device_private, blob[:32], PUSH_INFO, handle.encode("utf-8"), blob[32:]))
