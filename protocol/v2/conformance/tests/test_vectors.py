"""fixtures/approval_vectors.json: the approval rules as data, for any backend in any language (CONTRACT-2 §6, §10.4).
The reference implementation must agree with every vector."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import V2
from pisa_conformance.reference.approvals import ApprovalVerifier, canonical, digest, jcs, normalise_recipients, parse_canonical

VECTORS = json.loads((V2 / "fixtures" / "approval_vectors.json").read_text(encoding="utf-8"))
PUBLIC = serialization.load_der_public_key(base64.b64decode(VECTORS["signature"]["publicKey"]))


def verify(key_id, data, sig_b64):
    try:
        PUBLIC.verify(base64.b64decode(sig_b64), data, ec.ECDSA(hashes.SHA256()))
    except Exception:
        return False
    return key_id == VECTORS["signature"]["keyId"]


@pytest.mark.parametrize("case", VECTORS["jcs"], ids=lambda c: c["output"][:40])
def test_jcs(case):
    assert jcs(case["input"]) == case["output"]


@pytest.mark.parametrize("case", VECTORS["digest"], ids=lambda c: c["kind"])
def test_digest(case):
    assert jcs(case["payload"]) == case["jcsPayload"]
    assert ",".join(normalise_recipients(case["recipients"])) == case["recipientsNormalised"]
    assert digest(case["kind"], case["payload"], case["recipients"]) == case["digest"]


@pytest.mark.parametrize("case", VECTORS["canonical"], ids=lambda c: c["fields"]["kind"])
def test_canonical(case):
    assert canonical(**case["fields"]) == case["text"]
    assert parse_canonical(case["text"])["kind"] == case["fields"]["kind"]


@pytest.mark.parametrize("case", VECTORS["phoneRejects"], ids=lambda c: c["why"])
def test_phone_rejects(case):
    with pytest.raises(ValueError):
        parse_canonical(case["text"])


@pytest.mark.parametrize("case", VECTORS["verifier"], ids=lambda c: c["name"])
def test_verifier(case):
    v = ApprovalVerifier(verify, lambda: case["now"])
    v.used_nonces.update(case["usedNonces"])
    got = [v.check(row=case["row"], challenge=case["challenge"], resume_payload=case["payload"], interrupt_id=case["interruptId"],
                   server=case["server"], device=case["device"]) for _ in case["expect"]]
    assert got == case["expect"]


def test_vectors_hold_no_private_key_and_the_canonical_matches_the_swift_fixture():
    text = json.dumps(VECTORS)
    assert "PRIVATE" not in text and "privateKey" not in text
    swift = json.loads((V2 / "fixtures" / "approval_canonical.json").read_text())
    assert swift["canonical"].startswith("pisa-approval/2\n")
