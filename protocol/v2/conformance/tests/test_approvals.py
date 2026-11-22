import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from pisa_conformance.reference.approvals import ApprovalVerifier, canonical, digest, jcs, parse_canonical

SERVER, DEVICE = "srv_synthetic0001_Qm7Zr2Xc", "00000000-0000-4000-8000-0000000000d1"
ROW = {"approval_id": "33333333-3333-4333-8333-333333333333", "kind": "email_send", "requires_biometric": True,
       "payload": {"to": ["sam@example.org"], "subject": "Dinner\ndecision=approve", "body": "See you at 8."},
       "recipients": ["Sam@Example.org "], "expires_at": "2026-11-18T10:10:00Z"}
KEY = ec.generate_private_key(ec.SECP256R1())  # stands in for the Secure Enclave key


def sign(text: str) -> str:
    return base64.b64encode(KEY.sign(text.encode(), ec.ECDSA(hashes.SHA256()))).decode()


def verify(key_id, data, sig_b64):
    try:
        KEY.public_key().verify(base64.b64decode(sig_b64), data, ec.ECDSA(hashes.SHA256()))
        return key_id == "phone-key-1"
    except Exception:
        return False


def make(now="2026-11-18T10:00:00Z"):
    return ApprovalVerifier(verify, lambda: now)


def challenge(nonce="bm9uY2UtMQ=="):
    return {"nonce": nonce, "expires_at": "2026-11-18T10:05:00Z", "device": DEVICE}


def signed_payload(row=ROW, ch=None, interrupt="int-1"):
    ch = ch or challenge()
    dg = digest(row["kind"], row["payload"], row["recipients"])
    text = canonical(server=SERVER, device=DEVICE, approval_id=row["approval_id"], interrupt_id=interrupt, decision="approve",
                     kind=row["kind"], digest_hex=dg, recipients_count=1, nonce=ch["nonce"], expires_at=ch["expires_at"])
    return {"decision": "approve", "auth": "biometric", "digest": dg, "nonce": ch["nonce"], "signature": sign(text), "keyId": "phone-key-1"}, text


def test_jcs_matches_rfc8785_examples():
    assert jcs({"b": 1, "a": [True, None, "é"], "c": 1.5}) == '{"a":[true,null,"é"],"b":1,"c":1.5}'
    assert jcs(1e21) == "1e+21" and jcs(10.0) == "10"


def test_canonical_carries_no_outside_text():
    _, text = signed_payload()
    assert "Dinner" not in text and "sam@" not in text and "See you" not in text
    parsed = parse_canonical(text)
    assert parsed["decision"] == "approve" and parsed["recipients_count"] == "1"


def test_injected_subject_cannot_add_lines():
    # the subject holds "\ndecision=approve" but it only reaches the digest, never a line
    _, text = signed_payload()
    assert text.count("decision=") == 1


def test_valid_signature_approves_once():
    v = make()
    p, _ = signed_payload()
    assert v.check(row=ROW, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "ok"
    assert v.check(row=ROW, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "approval_decided"


def test_replayed_nonce_refused_even_on_another_approval():
    v = make()
    p, _ = signed_payload()
    v.used_nonces.add(p["nonce"])
    assert v.check(row=ROW, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "biometric_required"


def test_changed_payload_after_signing_is_refused():
    v = make()
    p, _ = signed_payload()
    changed = dict(ROW, payload=dict(ROW["payload"], body="See you at 9."))
    assert v.check(row=changed, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "approval_changed"


def test_added_recipient_after_signing_is_refused():
    v = make()
    p, _ = signed_payload()
    changed = dict(ROW, recipients=ROW["recipients"] + ["eve@example.net"])
    assert v.check(row=changed, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "approval_changed"


@pytest.mark.parametrize("field,value", [("interrupt", "int-2")])
def test_signature_bound_to_interrupt(field, value):
    v = make()
    p, _ = signed_payload(interrupt=value)
    assert v.check(row=ROW, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "biometric_required"


def test_signature_bound_to_server_and_device():
    v = make()
    p, _ = signed_payload()
    assert v.check(row=ROW, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server="srv_other_install_9Tq4LmWx2p", device=DEVICE) == "biometric_required"
    v2 = make()
    assert v2.check(row=ROW, challenge=dict(challenge(), device="other"), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "biometric_required"


def test_tap_cannot_approve_a_biometric_approval():
    v = make()
    dg = digest(ROW["kind"], ROW["payload"], ROW["recipients"])
    assert v.check(row=ROW, challenge=challenge(), resume_payload={"decision": "approve", "auth": "tap", "digest": dg},
                   interrupt_id="int-1", server=SERVER, device=DEVICE) == "biometric_required"


def test_reject_needs_no_signature():
    v = make()
    assert v.check(row=ROW, challenge=challenge(), resume_payload={"decision": "reject", "auth": "tap", "digest": "0" * 64},
                   interrupt_id="int-1", server=SERVER, device=DEVICE) == "ok"


def test_expired_approval_and_expired_challenge():
    p, _ = signed_payload()
    assert make("2026-11-18T10:11:00Z").check(row=ROW, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "approval_expired"
    assert make("2026-11-18T10:06:00Z").check(row=ROW, challenge=challenge(), resume_payload=p, interrupt_id="int-1", server=SERVER, device=DEVICE) == "biometric_required"


def test_phone_rejects_a_canonical_with_extra_or_reordered_lines():
    _, text = signed_payload()
    with pytest.raises(ValueError):
        parse_canonical(text + "\nsummary=hello")
    lines = text.split("\n")
    lines[1], lines[2] = lines[2], lines[1]
    with pytest.raises(ValueError):
        parse_canonical("\n".join(lines))
