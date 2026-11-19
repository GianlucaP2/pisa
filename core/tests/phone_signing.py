"""The phone's side of the signed approvals in tests: a P-256 key, its registration, and signed challenges."""

import base64
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from pisa.domain import approval_sign


def new_key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    private = ec.generate_private_key(ec.SECP256R1())
    raw = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return private, base64.b64encode(raw).decode()


def sign(private: ec.EllipticCurvePrivateKey, text: str) -> str:
    return base64.b64encode(private.sign(text.encode(), ec.ECDSA(hashes.SHA256()))).decode()


def register(client: Any, auth: dict[str, str], key_id: str = "k1") -> ec.EllipticCurvePrivateKey:
    private, public = new_key()
    made = client.post("/v1/phone/keys", json={"key_id": key_id, "public_key": public}, headers=auth)
    assert made.status_code == 201, made.text
    return private


def challenge(client: Any, auth: dict[str, str], **body: Any) -> dict[str, Any]:
    made = client.post("/v1/phone/challenge", json=body, headers=auth)
    assert made.status_code == 200, made.text
    return made.json()


def device_headers(
    client: Any,
    auth: dict[str, str],
    private: ec.EllipticCurvePrivateKey,
    op: str,
    target: str,
    key_id: str = "k1",
) -> dict[str, str]:
    """The headers of an owner action signed over its `device` challenge."""
    c = challenge(client, auth, action="device", op=op, target=target)
    return auth | {
        "X-Pisa-Nonce": c["nonce"],
        "X-Pisa-Signature": sign(private, c["canonical"]),
        "X-Pisa-Key-Id": key_id,
    }


def shown_digest(client: Any, auth: dict[str, str], approval_id: str) -> str:
    """The digest the phone recomputes from `GET /v1/approvals/{id}` (CONTRACT-2 §6.3)."""
    shown = client.get(f"/v1/approvals/{approval_id}", headers=auth)
    if shown.status_code != 200:
        return "0" * 64  # an unknown approval: the resume says why
    body = shown.json()
    return approval_sign.digest(body["kind"], body["payload"], body["recipients"])


def resume(client: Any, auth: dict[str, str], approval_id: str, body: dict[str, Any]) -> Any:
    """`POST /v1/approvals/{id}/resume`; when it decided, the decided approval (`GET /v1/approvals/{id}`) in
    place of the resuming run, so a test reads its status and result."""
    resumed = client.post(f"/v1/approvals/{approval_id}/resume", headers=auth, json=body)
    if resumed.status_code != 200:
        return resumed
    return client.get(f"/v1/approvals/{approval_id}", headers=auth)


def decide(
    client: Any, auth: dict[str, str], approval_id: str, decision: str = "approve", how: str = "tap"
) -> Any:
    """The user's tap (or voice) on the approval's card, over contract 2's REST resume."""
    digest = shown_digest(client, auth, approval_id)
    return resume(client, auth, approval_id, {"decision": decision, "auth": how, "digest": digest})


def signed_approval(
    client: Any,
    auth: dict[str, str],
    private: ec.EllipticCurvePrivateKey,
    approval_id: str,
    key_id: str = "k1",
) -> dict[str, str]:
    """The resume of an approve signed over the approval's `pisa-approval/2` challenge."""
    c = challenge(client, auth, action="approval", approval_id=approval_id, interrupt_id=f"int-{approval_id}")
    return {
        "decision": "approve",
        "auth": "biometric",
        "digest": approval_sign.parse_canonical(c["canonical"])["digest"],
        "nonce": c["nonce"],
        "signature": sign(private, c["canonical"]),
        "keyId": key_id,
    }
