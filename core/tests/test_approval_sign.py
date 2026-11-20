"""Contract 2's signed approvals (`pisa.domain.approval_sign`), ported from the conformance kit: its tests
(`conformance/tests/test_approvals.py`) and every approval vector (`fixtures/approval_vectors.json`): JCS, the
digest, the canonical text byte for byte, the phone's refusals and the server's checks on a resume."""

import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pisa_conformance.paths import data_root
from pisa_conformance.reference import approvals as reference
from pisa_conformance.validate import EXT_ID, errors

from pisa.domain.approval_sign import (
    ApprovalVerifier,
    canonical,
    digest,
    interrupt_for,
    jcs,
    normalise_recipients,
    parse_canonical,
)

V2 = data_root()
VECTORS = json.loads((V2 / "fixtures" / "approval_vectors.json").read_text(encoding="utf-8"))
SWIFT = json.loads((V2 / "fixtures" / "approval_canonical.json").read_text(encoding="utf-8"))
PUBLIC = serialization.load_der_public_key(base64.b64decode(VECTORS["signature"]["publicKey"]))


def verify_vector(key_id: str | None, data: bytes, sig_b64: str) -> bool:
    try:
        PUBLIC.verify(base64.b64decode(sig_b64), data, ec.ECDSA(hashes.SHA256()))  # type: ignore[union-attr,call-arg]
    except Exception:
        return False
    return key_id == VECTORS["signature"]["keyId"]


# --- the vectors ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("case", VECTORS["jcs"], ids=lambda c: c["output"][:40])
def test_jcs(case: dict[str, Any]) -> None:
    assert jcs(case["input"]) == case["output"]


@pytest.mark.parametrize("case", VECTORS["digest"], ids=lambda c: c["kind"])
def test_digest(case: dict[str, Any]) -> None:
    assert jcs(case["payload"]) == case["jcsPayload"]
    assert ",".join(normalise_recipients(case["recipients"])) == case["recipientsNormalised"]
    assert digest(case["kind"], case["payload"], case["recipients"]) == case["digest"]


@pytest.mark.parametrize("case", VECTORS["canonical"], ids=lambda c: c["fields"]["kind"])
def test_canonical_is_byte_identical(case: dict[str, Any]) -> None:
    assert canonical(**case["fields"]).encode() == case["text"].encode()
    assert parse_canonical(case["text"])["kind"] == case["fields"]["kind"]


@pytest.mark.parametrize("case", VECTORS["phoneRejects"], ids=lambda c: c["why"])
def test_phone_rejects(case: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_canonical(case["text"])


@pytest.mark.parametrize("case", VECTORS["verifier"], ids=lambda c: c["name"])
def test_verifier(case: dict[str, Any]) -> None:
    v = ApprovalVerifier(verify_vector, lambda: case["now"])
    v.used_nonces.update(case["usedNonces"])
    got = [
        v.check(
            row=case["row"],
            challenge=case["challenge"],
            resume_payload=case["payload"],
            interrupt_id=case["interruptId"],
            server=case["server"],
            device=case["device"],
        )
        for _ in case["expect"]
    ]
    assert got == case["expect"]


def test_the_swift_fixture_s_interrupt_and_canonical_are_ours() -> None:
    it = SWIFT["interrupt"]
    meta = it["metadata"]["pisa"]
    ours = interrupt_for(
        interrupt_id=it["id"],
        approval_id=meta["approvalId"],
        kind=meta["kind"],
        requires_biometric=meta["requiresBiometric"],
        digest_hex=meta["digest"],
        recipients_count=meta["recipientsCount"],
        undo_minutes=meta["undoMinutes"],
        expires_at=it["expiresAt"],
        server=meta["server"],
        tool_call_id=it["toolCallId"],
    )
    assert ours == it and errors(ours, EXT_ID + "#/$defs/ApprovalInterrupt") == []
    text = canonical(
        server=meta["server"],
        device=SWIFT["device"],
        approval_id=meta["approvalId"],
        interrupt_id=it["id"],
        decision="approve",
        kind=meta["kind"],
        digest_hex=meta["digest"],
        recipients_count=meta["recipientsCount"],
        nonce=SWIFT["nonce"],
        expires_at=SWIFT["expiresAt"],
    )
    assert text == SWIFT["canonical"]


def test_ours_agree_with_the_kit_s_reference_on_every_vector() -> None:
    for case in VECTORS["digest"]:
        assert digest(case["kind"], case["payload"], case["recipients"]) == reference.digest(
            case["kind"], case["payload"], case["recipients"]
        )
    for case in VECTORS["canonical"]:
        assert canonical(**case["fields"]) == reference.canonical(**case["fields"])
    assert interrupt_for(
        interrupt_id="int-1", approval_id="33333333-3333-4333-8333-333333333333", kind="calendar_move",
        requires_biometric=False, digest_hex="0" * 64, recipients_count=3, undo_minutes=60,
        expires_at="2026-11-18T10:10:00Z", server="srv_synthetic0001_Qm7Zr2Xc", lang="it-IT",
    )["message"] == "Spostare un evento · avvisa 3"  # fmt: skip


# --- the kit's own tests, ported ------------------------------------------------------------------------
SERVER, DEVICE = "srv_synthetic0001_Qm7Zr2Xc", "00000000-0000-4000-8000-0000000000d1"
ROW = {
    "approval_id": "33333333-3333-4333-8333-333333333333",
    "kind": "email_send",
    "requires_biometric": True,
    "payload": {"to": ["sam@example.org"], "subject": "Dinner\ndecision=approve", "body": "See you at 8."},
    "recipients": ["Sam@Example.org "],
    "expires_at": "2026-11-18T10:10:00Z",
}
KEY = ec.generate_private_key(ec.SECP256R1())  # stands in for the Secure Enclave key, made in the test


def sign(text: str) -> str:
    return base64.b64encode(KEY.sign(text.encode(), ec.ECDSA(hashes.SHA256()))).decode()


def verify(key_id: str | None, data: bytes, sig_b64: str) -> bool:
    try:
        KEY.public_key().verify(base64.b64decode(sig_b64), data, ec.ECDSA(hashes.SHA256()))
        return key_id == "phone-key-1"
    except Exception:
        return False


def make(now: str = "2026-11-18T10:00:00Z") -> ApprovalVerifier:
    return ApprovalVerifier(verify, lambda: now)


def challenge(nonce: str = "bm9uY2UtMQ==") -> dict[str, str]:
    return {"nonce": nonce, "expires_at": "2026-11-18T10:05:00Z", "device": DEVICE}


def signed_payload(
    row: dict[str, Any] = ROW, ch: dict[str, str] | None = None, interrupt: str = "int-1"
) -> tuple[dict[str, Any], str]:
    ch = ch or challenge()
    dg = digest(row["kind"], row["payload"], row["recipients"])
    text = canonical(server=SERVER, device=DEVICE, approval_id=row["approval_id"], interrupt_id=interrupt,
                     decision="approve", kind=row["kind"], digest_hex=dg, recipients_count=1,
                     nonce=ch["nonce"], expires_at=ch["expires_at"])  # fmt: skip
    payload = {"decision": "approve", "auth": "biometric", "digest": dg, "nonce": ch["nonce"]}
    return payload | {"signature": sign(text), "keyId": "phone-key-1"}, text


def check(v: ApprovalVerifier, payload: dict[str, Any], **over: Any) -> str:
    args: dict[str, Any] = {"row": ROW, "challenge": challenge(), "resume_payload": payload,
                            "interrupt_id": "int-1", "server": SERVER, "device": DEVICE}  # fmt: skip
    return v.check(**(args | over))


def test_canonical_carries_no_outside_text() -> None:
    _, text = signed_payload()
    assert "Dinner" not in text and "sam@" not in text and "See you" not in text
    parsed = parse_canonical(text)
    assert parsed["decision"] == "approve" and parsed["recipients_count"] == "1"


def test_injected_subject_cannot_add_lines() -> None:
    _, text = signed_payload()
    assert text.count("decision=") == 1


def test_valid_signature_approves_once() -> None:
    v = make()
    p, _ = signed_payload()
    assert check(v, p) == "ok"
    assert check(v, p) == "approval_decided"


def test_replayed_nonce_refused() -> None:
    v = make()
    p, _ = signed_payload()
    v.used_nonces.add(p["nonce"])
    assert check(v, p) == "biometric_required"


def test_changed_payload_after_signing_is_refused() -> None:
    p, _ = signed_payload()
    changed = dict(ROW, payload=dict(ROW["payload"], body="See you at 9."))
    assert check(make(), p, row=changed) == "approval_changed"


def test_added_recipient_after_signing_is_refused() -> None:
    p, _ = signed_payload()
    changed = dict(ROW, recipients=[*ROW["recipients"], "eve@example.net"])
    assert check(make(), p, row=changed) == "approval_changed"


def test_signature_bound_to_interrupt_server_and_device() -> None:
    p, _ = signed_payload(interrupt="int-2")
    assert check(make(), p) == "biometric_required"
    p, _ = signed_payload()
    assert check(make(), p, server="srv_other_install_9Tq4LmWx2p") == "biometric_required"
    assert check(make(), p, challenge=dict(challenge(), device="other")) == "biometric_required"


def test_tap_cannot_approve_a_biometric_approval_and_a_reject_needs_no_signature() -> None:
    dg = digest(ROW["kind"], ROW["payload"], ROW["recipients"])
    assert check(make(), {"decision": "approve", "auth": "tap", "digest": dg}) == "biometric_required"
    assert check(make(), {"decision": "reject", "auth": "tap", "digest": "0" * 64}) == "ok"


def test_expired_approval_and_expired_challenge() -> None:
    p, _ = signed_payload()
    assert check(make("2026-11-18T10:11:00Z"), p) == "approval_expired"
    assert check(make("2026-11-18T10:06:00Z"), p) == "biometric_required"


def test_a_line_cannot_carry_a_newline_or_an_equals_sign() -> None:
    fields = VECTORS["canonical"][0]["fields"]
    with pytest.raises(ValueError):
        canonical(**(fields | {"device": "a\ndecision=approve"}))
    with pytest.raises(ValueError):
        canonical(**(fields | {"server": "a=b"}))
    with pytest.raises(ValueError):
        canonical(**(fields | {"decision": "reject"}))


@pytest.mark.parametrize("case", VECTORS["payloadAsSent"]["cases"], ids=lambda c: c["name"])
def test_the_app_hashes_the_payload_as_sent(case: dict[str, Any]) -> None:
    """CONTRACT-2 §6.3: JCS of `GET /v1/approvals/{id}`'s `payload` exactly as sent; a payload rebuilt from
    what the app shows gives another digest."""
    assert digest(case["kind"], json.loads(case["payloadAsSent"]), case["recipients"]) == case["digest"]


def test_every_payload_as_sent_vector_runs() -> None:
    cases = VECTORS["payloadAsSent"]["cases"]
    assert len(cases) >= 9 and any(not c["matchesTheServer"] for c in cases)
