"""Approvals as AG-UI interrupts, signed by the phone's Face ID key (CONTRACT-2 §6).

Changes from v1 (K30): the canonical text holds NO outside text (v1 put the approval's summary, which could
carry an email subject, into it). Every value in the canonical is either server-generated (ids, enums, counts,
times) or a hex digest. The content being approved is bound through `digest`, whose payload hash uses
RFC 8785 (JCS) so the phone can recompute it from what it displays.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

CANONICAL_HEADER = "pisa-approval/2"
DIGEST_HEADER = "pisa-approval-digest/2"
KINDS = ("calendar_create", "calendar_move", "calendar_cancel", "email_send")


def jcs(value: Any) -> str:
    """RFC 8785 JSON Canonicalization Scheme for the JSON values approvals carry.

    Numbers follow ECMAScript Number::toString (RFC 8785 §3.2.2.3): integers are written as the IEEE 754 double
    they become, floats from their shortest round-trip digits (Python's repr gives the same digits as ECMAScript)."""
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
    if isinstance(value, list):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + jcs(v) for k, v in items) + "}"
    raise TypeError(type(value))


def _es_number(value: float) -> str:
    """ECMAScript Number::toString for a finite double (ECMA-262 §6.1.6.1.20), digits from Python's shortest repr."""
    if value == 0:
        return "0"  # also -0
    if value < 0:
        return "-" + _es_number(-value)
    mant, _, exp = repr(value).partition("e")
    whole, _, frac = mant.partition(".")
    digits = (whole + frac).lstrip("0")
    # value = 0.<whole><frac> * 10^(len(whole) + exp); strip the zeros the lstrip removed
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


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def normalise_recipients(recipients: list[str]) -> list[str]:
    return sorted({r.strip().lower() for r in recipients if r.strip()})


def digest(kind: str, payload: dict[str, Any], recipients: list[str]) -> str:
    if kind not in KINDS:
        raise ValueError(kind)
    pre = "\n".join([DIGEST_HEADER, kind, sha256_hex(jcs(payload)), ",".join(normalise_recipients(recipients))])
    return sha256_hex(pre)


def _line(key: str, value: str) -> str:
    if any(ch in value for ch in "\r\n=") and key not in ("expires_at", "nonce"):
        raise ValueError(f"{key} must be single-line and '='-free")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be single-line")
    return f"{key}={value}"


def canonical(*, server: str, device: str, approval_id: str, interrupt_id: str, decision: str, kind: str,
              digest_hex: str, recipients_count: int, nonce: str, expires_at: str) -> str:
    """The exact bytes the Secure Enclave key signs. Fixed key order, '\n' separators, no trailing newline."""
    if decision != "approve":
        raise ValueError("only an approve is signed; a reject never needs a signature")
    if kind not in KINDS or len(digest_hex) != 64 or any(c not in "0123456789abcdef" for c in digest_hex):
        raise ValueError("bad kind or digest")
    lines = [CANONICAL_HEADER,
             _line("server", server), _line("device", device), _line("approval_id", approval_id),
             _line("interrupt_id", interrupt_id), _line("decision", decision), _line("kind", kind),
             _line("digest", digest_hex), _line("recipients_count", str(int(recipients_count))),
             _line("nonce", nonce), _line("expires_at", expires_at)]
    return "\n".join(lines)


def parse_canonical(text: str) -> dict[str, str]:
    """What the phone does before Face ID: reject anything not exactly our shape (v1 CanonicalCheck, stricter)."""
    lines = text.split("\n")
    if lines[0] != CANONICAL_HEADER:
        raise ValueError("challenge_mismatch: header")
    expected = ["server", "device", "approval_id", "interrupt_id", "decision", "kind", "digest", "recipients_count", "nonce", "expires_at"]
    keys, out = [], {}
    for ln in lines[1:]:
        k, sep, v = ln.partition("=")
        if not sep:
            raise ValueError("challenge_mismatch: line")
        keys.append(k)
        out[k] = v
    if keys != expected:
        raise ValueError("challenge_mismatch: keys")
    return out


def interrupt_for(*, interrupt_id: str, approval_id: str, kind: str, requires_biometric: bool, digest_hex: str,
                  recipients_count: int, undo_minutes: int | None, expires_at: str, server: str,
                  tool_call_id: str | None, lang: str = "en-US") -> dict[str, Any]:
    """An AG-UI Interrupt. `message` is templated from enums and counts only: never outside text."""
    verbs = {"en-US": {"email_send": "Send an email", "calendar_create": "Add an event", "calendar_move": "Move an event",
                       "calendar_cancel": "Cancel an event"},
             "it-IT": {"email_send": "Mandare un'email", "calendar_create": "Aggiungere un evento",
                       "calendar_move": "Spostare un evento", "calendar_cancel": "Annullare un evento"}}[lang]
    notifies = {"en-US": " · notifies {n}", "it-IT": " · avvisa {n}"}[lang]
    message = verbs[kind] + (notifies.format(n=recipients_count) if recipients_count else "")
    response_schema = {"type": "object", "required": ["decision", "auth", "digest"],
                       "properties": {"decision": {"enum": ["approve", "reject"]}, "auth": {"enum": ["tap", "voice", "biometric"]},
                                      "digest": {"const": digest_hex}, "nonce": {"type": "string"},
                                      "signature": {"type": "string"}, "keyId": {"type": "string"}}}
    out = {"id": interrupt_id, "reason": "pisa.approval", "message": message, "expiresAt": expires_at,
           "responseSchema": response_schema,
           "metadata": {"pisa": {"approvalId": approval_id, "kind": kind, "risk": "R2", "requiresBiometric": requires_biometric,
                                 "digest": digest_hex, "recipientsCount": recipients_count, "undoMinutes": undo_minutes,
                                 "detailsRef": f"approval:{approval_id}", "server": server}}}
    if tool_call_id:
        out["toolCallId"] = tool_call_id
    return out


class ApprovalVerifier:
    """Server side of a resume carrying an approve (spike: the signature check is injected)."""

    def __init__(self, verify_signature, now_iso) -> None:
        self.verify_signature = verify_signature  # (key_id, canonical_bytes, signature_b64) -> bool
        self.now_iso = now_iso
        self.used_nonces: set[str] = set()
        self.decided: set[str] = set()

    def check(self, *, row: dict[str, Any], challenge: dict[str, Any], resume_payload: dict[str, Any],
              interrupt_id: str, server: str, device: str) -> str:
        """Returns 'ok' or the refusal code. row = the approval as stored NOW (recomputed, never trusted from the app)."""
        if row["approval_id"] in self.decided:
            return "approval_decided"
        if row["expires_at"] <= self.now_iso():
            return "approval_expired"
        current = digest(row["kind"], row["payload"], row["recipients"])
        if resume_payload["decision"] == "reject":
            self.decided.add(row["approval_id"])
            return "ok"
        if resume_payload.get("digest") != current:
            return "approval_changed"
        if not row["requires_biometric"]:
            if resume_payload["auth"] in ("tap", "voice", "biometric"):
                self.decided.add(row["approval_id"])
                return "ok"
        if resume_payload.get("auth") != "biometric" or not resume_payload.get("signature"):
            return "biometric_required"
        nonce = resume_payload.get("nonce", "")
        if nonce != challenge["nonce"] or nonce in self.used_nonces or challenge["expires_at"] <= self.now_iso():
            return "biometric_required"
        if challenge["device"] != device:
            return "biometric_required"
        text = canonical(server=server, device=device, approval_id=row["approval_id"], interrupt_id=interrupt_id,
                         decision="approve", kind=row["kind"], digest_hex=current,
                         recipients_count=len(normalise_recipients(row["recipients"])), nonce=nonce,
                         expires_at=challenge["expires_at"])
        self.used_nonces.add(nonce)  # spent even when the signature fails: one try per challenge
        if not self.verify_signature(resume_payload.get("keyId"), text.encode("utf-8"), resume_payload["signature"]):
            return "biometric_required"
        self.decided.add(row["approval_id"])
        return "ok"
