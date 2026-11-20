"""The approval vectors as data: fixtures/approval_vectors.json (CONTRACT-2 §6, §10 item 4).

Both backends (and the app) check their own JCS, digest, canonical text and resume verifier against this file, in
any language. Signatures are ECDSA P-256 over SHA-256, DER, base64: what a Secure Enclave key produces. The key is a
throwaway generated when the file was written; only its public half is stored. The file is written once and
committed; re-running `python -m pisa_conformance.reference.vectors` makes new signatures (the test checks the
committed file against the reference, not byte equality with a fresh run). Synthetic data only.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from ..paths import FIXTURES
from .approvals import canonical, digest, jcs, normalise_recipients

OUT = FIXTURES / "approval_vectors.json"
SERVER, DEVICE, KEY_ID = "srv_synthetic0001_Qm7Zr2Xc", "00000000-0000-4000-8000-0000000000d1", "phone-key-1"
ROW = {"approval_id": "33333333-3333-4333-8333-333333333333", "kind": "email_send", "requires_biometric": True,
       "payload": {"to": ["sam@example.org"], "subject": "Dinner\ndecision=approve", "body": "See you at 8."},
       "recipients": ["Sam@Example.org "], "expires_at": "2026-11-18T10:10:00Z"}
CHALLENGE = {"nonce": "bm9uY2UtMQ==", "expires_at": "2026-11-18T10:05:00Z", "device": DEVICE}
NOW = "2026-11-18T10:00:00Z"

JCS_CASES: list[Any] = [
    {"b": 1, "a": [True, None, "é"], "c": 1.5},
    1e21, 10.0, -0.0, 1e-7, 123456789012345680000, 0.000001, 333333333.3333333,
    {"€": "Euro", "\r": "CR", "1": "One", "ö": "o-umlaut", "😀": "emoji", "דּ": "hebrew"},
    {"numbers": [333333333.33333329, 1e30, 4.5, 2e-3, 0.000000000000000000000000001], "string": "€$\u000f\nA'B\"\\\\\"/",
     "literals": [None, True, False]},
    {"nested": {"z": [], "y": {}, "x": [{"b": 2, "a": 1}]}},
]


def _digest_cases() -> list[dict[str, Any]]:
    cases = [
        ("email_send", ROW["payload"], ROW["recipients"]),
        ("email_send", {"to": ["a@example.org", "b@example.org"], "cc": ["c@example.org"], "subject": "Hello", "body": "Hi"},
         [" B@example.org", "a@example.org", "c@example.org", "a@example.org"]),
        ("calendar_create", {"calendar": "primary", "title": "Lunch", "start": "2026-11-19T12:30:00+01:00",
                             "end": "2026-11-19T13:30:00+01:00", "place": "Example Bistro", "attendees": []}, []),
        ("calendar_move", {"event": "evt-1", "start": "2026-11-20T09:00:00Z", "end": "2026-11-20T10:00:00Z", "attendees": ["x@example.net"]},
         ["X@Example.net"]),
        ("calendar_cancel", {"event": "evt-2", "notify": True, "attendees": ["y@example.net", "z@example.net"]}, ["z@example.net", "y@example.net"]),
    ]
    return [{"kind": k, "payload": p, "recipients": r, "jcsPayload": jcs(p), "recipientsNormalised": ",".join(normalise_recipients(r)),
             "digest": digest(k, p, r)} for k, p, r in cases]


def _canonical_fields(interrupt_id: str = "int-1", row: dict[str, Any] = ROW) -> dict[str, Any]:
    return {"server": SERVER, "device": DEVICE, "approval_id": row["approval_id"], "interrupt_id": interrupt_id, "decision": "approve",
            "kind": row["kind"], "digest_hex": digest(row["kind"], row["payload"], row["recipients"]),
            "recipients_count": len(normalise_recipients(row["recipients"])), "nonce": CHALLENGE["nonce"], "expires_at": CHALLENGE["expires_at"]}


ATTACH = "9f2b6d1c4e8a7f30b5c2d9e1a6f4b8c07d3e5a91f2c6b4d8e0a7f3c5b9d1e2a4"  # a stored attachment's sign (sha256)
EMAIL_SENT = ('{"draft_id":"drf-demo-1","version":3,"to":["sam@example.org","alex@example.org"],"cc":[],'
              '"subject":"Dinner","body":"See you at 8.","attachments":["' + ATTACH + '"]}')
CAL_ROW = {"calendar": "primary", "event": "evt-demo-1", "start": "2026-11-20T09:00:00+01:00", "end": "2026-11-20T10:00:00+01:00",
           "title": "Planning", "place": "Room 2", "attendees": ["kim@example.org"], "_etag": "e-77", "_synced_at": "2026-11-18T07:00:00Z"}


def _payload_cases() -> list[dict[str, Any]]:
    """The app hashes the `payload` of GET /v1/approvals/{id} exactly as the server sent it (JCS of the parsed value),
    never a payload it rebuilt from what it shows. JCS makes object key order irrelevant; everything else counts."""
    email = json.loads(EMAIL_SENT)
    rcpt = email["to"] + email["cc"]
    cal = {k: v for k, v in CAL_ROW.items() if not k.startswith("_")}  # the stored payload minus internal fields
    cases: list[tuple[str, str, Any, list[str], bool]] = [
        ("email: as sent", "email_send", EMAIL_SENT, rcpt, True),
        ("email: the same object, keys in another order (JCS sorts them)", "email_send",
         json.dumps(dict(reversed(list(email.items())))), rcpt, True),
        ("email: recipients in another order (an array keeps its order)", "email_send",
         json.dumps(dict(email, to=list(reversed(email["to"])))), rcpt, False),
        ("email: rebuilt by the app from what it shows (no draft_id, no version)", "email_send",
         json.dumps({k: v for k, v in email.items() if k not in ("draft_id", "version")}), rcpt, False),
        ("email: the attachment named instead of its stored sign", "email_send",
         json.dumps(dict(email, attachments=["menu.pdf"])), rcpt, False),
        ("email: version as a string", "email_send", json.dumps(dict(email, version="3")), rcpt, False),
        ("calendar: the stored payload minus internal fields, as sent", "calendar_move", json.dumps(cal), cal["attendees"], True),
        ("calendar: internal fields left in", "calendar_move", json.dumps(CAL_ROW), cal["attendees"], False),
        ("calendar: the time rewritten in UTC (the same instant, other bytes)", "calendar_move",
         json.dumps(dict(cal, start="2026-11-20T08:00:00Z", end="2026-11-20T09:00:00Z")), cal["attendees"], False),
    ]
    reference = {"email_send": digest("email_send", email, rcpt), "calendar_move": digest("calendar_move", cal, cal["attendees"])}
    out = []
    for name, kind, sent, recipients, same in cases:
        d = digest(kind, json.loads(sent), recipients)
        assert (d == reference[kind]) == same, name
        out.append({"name": name, "kind": kind, "payloadAsSent": sent, "recipients": recipients, "digest": d,
                    "matchesTheServer": same})
    return out


def build(sign) -> dict[str, Any]:
    """sign(text) -> base64 DER signature."""
    good_text = canonical(**_canonical_fields())
    good = {"decision": "approve", "auth": "biometric", "digest": _canonical_fields()["digest_hex"], "nonce": CHALLENGE["nonce"],
            "signature": sign(good_text), "keyId": KEY_ID}
    other_interrupt = dict(good, signature=sign(canonical(**_canonical_fields("int-2"))))
    lines = good_text.split("\n")
    swapped = "\n".join([lines[0], lines[2], lines[1], *lines[3:]])
    later = lambda t: {"now": t}  # noqa: E731

    def case(name: str, expect: list[str], payload: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
        c = {"name": name, "now": NOW, "row": ROW, "challenge": CHALLENGE, "interruptId": "int-1", "server": SERVER, "device": DEVICE,
             "usedNonces": [], "payload": payload or good, "expect": expect}
        c.update(over)
        return c

    tap = {"decision": "approve", "auth": "tap", "digest": good["digest"]}
    verifier = [
        case("valid signature approves once; the second try is approval_decided", ["ok", "approval_decided"]),
        case("a spent nonce is refused", ["biometric_required"], usedNonces=[CHALLENGE["nonce"]]),
        case("body changed after signing", ["approval_changed"], row=dict(ROW, payload=dict(ROW["payload"], body="See you at 9."))),
        case("recipient added after signing", ["approval_changed"], row=dict(ROW, recipients=ROW["recipients"] + ["eve@example.net"])),
        case("signature made for another interrupt", ["biometric_required"], payload=other_interrupt),
        case("signature presented to another server", ["biometric_required"], server="srv_other_install_9Tq4LmWx2p"),
        case("challenge issued to another device", ["biometric_required"], challenge=dict(CHALLENGE, device="other-device")),
        case("tap cannot approve a biometric approval", ["biometric_required"], payload=tap),
        case("reject needs no signature", ["ok"], payload={"decision": "reject", "auth": "tap", "digest": "0" * 64}),
        case("approval expired", ["approval_expired"], **later("2026-11-18T10:11:00Z")),
        case("challenge expired", ["biometric_required"], **later("2026-11-18T10:06:00Z")),
        case("tap approves an approval that does not require biometrics", ["ok"], payload=tap, row=dict(ROW, requires_biometric=False)),
        case("signature over different bytes", ["biometric_required", "biometric_required"],
             payload=dict(good, signature=sign(good_text + " "))),
    ]
    return {
        "description": "PISA contract 2 approval vectors (CONTRACT-2 §6). Synthetic. Each verifier case runs on a fresh verifier "
                       "holding usedNonces; payload is submitted once per entry of expect, in order.",
        "signature": {"algorithm": "ECDSA P-256 SHA-256, DER, base64", "keyId": KEY_ID},
        "jcs": [{"input": v, "output": jcs(v)} for v in JCS_CASES],
        "digest": _digest_cases(),
        "canonical": [{"fields": _canonical_fields(), "text": good_text},
                      {"fields": _canonical_fields("int-2", dict(ROW, kind="calendar_move", payload={"event": "evt-1"}, recipients=[])),
                       "text": canonical(**_canonical_fields("int-2", dict(ROW, kind="calendar_move", payload={"event": "evt-1"}, recipients=[])))}],
        "phoneRejects": [
            {"why": "an extra line", "text": good_text + "\nsummary=hello"},
            {"why": "lines reordered", "text": swapped},
            {"why": "wrong header", "text": good_text.replace("pisa-approval/2", "pisa-approval/1", 1)},
            {"why": "a trailing newline", "text": good_text + "\n"},
        ],
        "verifier": verifier,
        "payloadAsSent": {
            "rule": "The app hashes exactly the `payload` field of GET /v1/approvals/{id} as the server sent it (parsed, then JCS), "
                    "never a payload rebuilt from what it shows. Email: {draft_id, version, to, cc, subject, body, attachments} "
                    "with attachments as their stored signs. Calendar: the stored payload minus internal fields. A case whose "
                    "matchesTheServer is false must give a different digest: the app then refuses to offer Approve.",
            "cases": _payload_cases()},
    }


def main() -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    doc = build(lambda text: base64.b64encode(key.sign(text.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode())
    doc["signature"]["publicKey"] = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
