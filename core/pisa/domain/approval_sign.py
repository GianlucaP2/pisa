"""Contract 2's signed approvals: the digest, the canonical text and the server's checks on a resume
(CONTRACT-2 §6). Pure rules, no I/O. Ported from the conformance kit's reference
(`pisa_conformance/reference/approvals.py`); the kit's approval vectors run against this module.

- **The digest** (`pisa-approval-digest/2`): `sha256_hex("pisa-approval-digest/2\\n" + kind + "\\n" +
  sha256_hex(JCS(payload)) + "\\n" + recipients)`. JCS is RFC 8785, so the phone can recompute it from what it
  shows (`GET /v1/approvals/{id}` returns `payload` and `recipients` exactly as hashed). `recipients`: every
  address the action writes to or notifies, lowercased and trimmed, once each, sorted, joined by `,`.
- **The canonical text** (`pisa-approval/2`): the exact bytes the phone's key signs. No line holds outside
  text: every value is a server id, an enum, a count, a time or a hex digest. Outside text reaches the
  signature only through the digest.
- **The checks on a resume**, in order, the first failure wins: decided, expired, a reject (never signed), the
  digest recomputed now from the stored row, a tap or voice yes on an approval that needs Face ID, then the
  challenge (unknown, spent, late, another device's) and the signature over the canonical text the server
  rebuilds from its own row. The nonce is spent at the first try of a signature, success or not.
- **The interrupt** (`$defs/ApprovalInterrupt`): its `message` is templated from the kind and a count, never
  outside text.
"""

import hashlib
import json
import math
from collections.abc import Callable
from typing import Any, Literal

from pisa.domain import approvals

CANONICAL_HEADER = "pisa-approval/2"
DIGEST_HEADER = "pisa-approval-digest/2"
KINDS = approvals.KINDS
CANONICAL_KEYS = (
    "server",
    "device",
    "approval_id",
    "interrupt_id",
    "decision",
    "kind",
    "digest",
    "recipients_count",
    "nonce",
    "expires_at",
)
INTERRUPT_REASON = "pisa.approval"
# what a resume can be refused with (CONTRACT-2 §6.1 step 6)
Refusal = Literal["approval_decided", "approval_expired", "approval_changed", "biometric_required"]
# the pure checks' verdict: done ("ok"), refused, or the challenge and the signature are next
Precheck = Literal["ok", "signature"] | Refusal


# --- RFC 8785 (JCS) -------------------------------------------------------------------------------------
def jcs(value: Any) -> str:
    """RFC 8785 JSON Canonicalization Scheme for the JSON values approvals carry.

    Numbers follow ECMAScript Number::toString (RFC 8785 §3.2.2.3): integers are written as the IEEE 754
    double they become, floats from their shortest round-trip digits (Python's repr gives the same digits as
    ECMAScript)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value) if abs(value) < 2**53 else _es_number(float(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JCS forbids NaN and Infinity")
        return _es_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: str(kv[0]).encode("utf-16-be"))
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + jcs(v) for k, v in items) + "}"
    raise TypeError(type(value))


def _es_number(value: float) -> str:
    """ECMAScript Number::toString for a finite double (ECMA-262 §6.1.6.1.20), digits from Python's repr."""
    if value == 0:
        return "0"  # also -0
    if value < 0:
        return "-" + _es_number(-value)
    mant, _, exp = repr(value).partition("e")
    whole, _, frac = mant.partition(".")
    digits = (whole + frac).lstrip("0")
    # value = 0.<whole><frac> * 10^(len(whole) + exp), less the leading zeros the lstrip removed
    n = len(whole) + int(exp or 0) - (len(whole + frac) - len(digits))
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    return digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e > 0 else "-") + str(abs(e))


# --- the digest -----------------------------------------------------------------------------------------
def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def normalise_recipients(recipients: list[str]) -> list[str]:
    return sorted({r.strip().lower() for r in recipients if r.strip()})


def digest(kind: str, payload: dict[str, Any], recipients: list[str]) -> str:
    if kind not in KINDS:
        raise ValueError(kind)
    pre = "\n".join(
        [DIGEST_HEADER, kind, sha256_hex(jcs(payload)), ",".join(normalise_recipients(recipients))]
    )
    return sha256_hex(pre)


# --- the canonical text ---------------------------------------------------------------------------------
def _line(key: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be single-line")
    if "=" in value and key not in ("expires_at", "nonce"):
        raise ValueError(f"{key} must be '='-free")
    return f"{key}={value}"


def canonical(
    *,
    server: str,
    device: str,
    approval_id: str,
    interrupt_id: str,
    decision: str,
    kind: str,
    digest_hex: str,
    recipients_count: int,
    nonce: str,
    expires_at: str,
) -> str:
    """The exact bytes the phone's key signs: fixed key order, `\\n` separators, no trailing newline."""
    if decision != "approve":
        raise ValueError("only an approve is signed; a reject never needs a signature")
    if kind not in KINDS or len(digest_hex) != 64 or any(c not in "0123456789abcdef" for c in digest_hex):
        raise ValueError("bad kind or digest")
    values = (server, device, approval_id, interrupt_id, decision, kind, digest_hex)
    values += (str(int(recipients_count)), nonce, expires_at)
    return "\n".join([CANONICAL_HEADER, *(_line(k, v) for k, v in zip(CANONICAL_KEYS, values, strict=True))])


def parse_canonical(text: str) -> dict[str, str]:
    """What the phone does before Face ID: refuse anything that is not exactly this shape."""
    lines = text.split("\n")
    if lines[0] != CANONICAL_HEADER:
        raise ValueError("challenge_mismatch: header")
    keys, out = [], {}
    for line in lines[1:]:
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError("challenge_mismatch: line")
        keys.append(key)
        out[key] = value
    if tuple(keys) != CANONICAL_KEYS:
        raise ValueError("challenge_mismatch: keys")
    return out


# --- the interrupt --------------------------------------------------------------------------------------
def interrupt_id_for(approval_id: str) -> str:
    return f"int-{approval_id}"


def approval_of_interrupt(interrupt_id: str) -> str | None:
    return interrupt_id.removeprefix("int-") if interrupt_id.startswith("int-") else None


_VERBS = {
    "en-US": {
        "email_send": "Send an email",
        "calendar_create": "Add an event",
        "calendar_move": "Move an event",
        "calendar_cancel": "Cancel an event",
    },
    "it-IT": {
        "email_send": "Mandare un'email",
        "calendar_create": "Aggiungere un evento",
        "calendar_move": "Spostare un evento",
        "calendar_cancel": "Annullare un evento",
    },
}
_NOTIFIES = {"en-US": " · notifies {n}", "it-IT": " · avvisa {n}"}


def interrupt_message(kind: str, recipients_count: int, lang: str) -> str:
    """The interrupt's line, from the kind and a count only (never outside text)."""
    lang = lang if lang in _VERBS else "en-US"
    notifies = _NOTIFIES[lang].format(n=recipients_count) if recipients_count else ""
    return _VERBS[lang][kind] + notifies


def response_schema(digest_hex: str) -> dict[str, Any]:
    """The resume payload's schema, with the digest as a `const`."""
    return {
        "type": "object",
        "required": ["decision", "auth", "digest"],
        "properties": {
            "decision": {"enum": ["approve", "reject"]},
            "auth": {"enum": ["tap", "voice", "biometric"]},
            "digest": {"const": digest_hex},
            "nonce": {"type": "string"},
            "signature": {"type": "string"},
            "keyId": {"type": "string"},
        },
    }


def interrupt_for(
    *,
    interrupt_id: str,
    approval_id: str,
    kind: str,
    requires_biometric: bool,
    digest_hex: str,
    recipients_count: int,
    undo_minutes: int | None,
    expires_at: str,
    server: str,
    tool_call_id: str | None = None,
    lang: str = "en-US",
) -> dict[str, Any]:
    """An AG-UI Interrupt (`$defs/ApprovalInterrupt`)."""
    out: dict[str, Any] = {
        "id": interrupt_id,
        "reason": INTERRUPT_REASON,
        "message": interrupt_message(kind, recipients_count, lang),
        "expiresAt": expires_at,
        "responseSchema": response_schema(digest_hex),
        "metadata": {
            "pisa": {
                "approvalId": approval_id,
                "kind": kind,
                "risk": "R2",
                "requiresBiometric": requires_biometric,
                "digest": digest_hex,
                "recipientsCount": recipients_count,
                "undoMinutes": undo_minutes,
                "detailsRef": f"approval:{approval_id}",
                "server": server,
            }
        },
    }
    if tool_call_id:
        out["toolCallId"] = tool_call_id
    return out


# --- the server's checks on a resume ------------------------------------------------------------------
def precheck(
    *, decided: bool, expired: bool, current_digest: str, requires_biometric: bool, payload: dict[str, Any]
) -> Precheck:
    """Checks 1 to 4 (CONTRACT-2 §6.4), from the row as it is now: `ok` (a reject, or a yes that needs no
    Face ID), a refusal, or `signature` (the challenge and the signature are checked next)."""
    if decided:
        return "approval_decided"
    if expired:
        return "approval_expired"
    if payload.get("decision") == "reject":
        return "ok"
    if payload.get("digest") != current_digest:
        return "approval_changed"
    if not requires_biometric and payload.get("auth") in ("tap", "voice", "biometric"):
        return "ok"
    if payload.get("auth") != "biometric" or not payload.get("signature"):
        return "biometric_required"
    return "signature"


class ApprovalVerifier:
    """The checks over in-memory state: the kit's verifier vectors run against it. The server runs the same
    `precheck`, then the challenge and the signature against its database (`PhoneKeys.verify_approval`)."""

    def __init__(
        self, verify_signature: Callable[[str | None, bytes, str], bool], now_iso: Callable[[], str]
    ) -> None:
        self.verify_signature = verify_signature  # (key id, canonical bytes, signature base64) -> bool
        self.now_iso = now_iso
        self.used_nonces: set[str] = set()
        self.decided: set[str] = set()

    def check(
        self,
        *,
        row: dict[str, Any],
        challenge: dict[str, Any],
        resume_payload: dict[str, Any],
        interrupt_id: str,
        server: str,
        device: str,
    ) -> str:
        """'ok' or the refusal's code. `row`: the approval as stored now (never the app's word)."""
        current = digest(row["kind"], row["payload"], row["recipients"])
        verdict = precheck(
            decided=row["approval_id"] in self.decided,
            expired=row["expires_at"] <= self.now_iso(),
            current_digest=current,
            requires_biometric=bool(row["requires_biometric"]),
            payload=resume_payload,
        )
        if verdict == "ok":
            self.decided.add(row["approval_id"])
        if verdict != "signature":
            return verdict
        nonce = str(resume_payload.get("nonce") or "")
        if (
            nonce != challenge["nonce"]
            or nonce in self.used_nonces
            or challenge["expires_at"] <= self.now_iso()
        ):
            return "biometric_required"
        if challenge["device"] != device:
            return "biometric_required"
        text = canonical(
            server=server,
            device=device,
            approval_id=row["approval_id"],
            interrupt_id=interrupt_id,
            decision="approve",
            kind=row["kind"],
            digest_hex=current,
            recipients_count=len(normalise_recipients(row["recipients"])),
            nonce=nonce,
            expires_at=challenge["expires_at"],
        )
        self.used_nonces.add(nonce)  # spent even when the signature fails: one try per challenge
        if not self.verify_signature(
            resume_payload.get("keyId"), text.encode("utf-8"), resume_payload["signature"]
        ):
            return "biometric_required"
        self.decided.add(row["approval_id"])
        return "ok"
