"""Check this client's byte formats against the contract's own test vectors (no server needed).

    python vectors.py [--protocol path/to/protocol/v2]

Pairing (QR, transcript, both MACs, commitment, SAS), the session signature, the approval digest and canonical
text (JCS included), and opening a sealed push. A client in another language should port these checks first.
"""

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from approvals import approval_digest, canonical_text, jcs, parse_canonical
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pair import client_mac, commitment, parse_qr, sas, server_mac, token_bytes, transcript
from push import open_sealed
from session import session_text

HERE = Path(__file__).resolve()
results: list[bool] = []


def check(name: str, ok: bool, detail: Any = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}".rstrip())


def pairing(v: dict[str, Any]) -> None:
    p, i = v["pairing"], v["pairing"]["inputs"]
    qr = parse_qr(p["qr"])
    check(
        "the QR parses (addresses with %2C, the pin's '=')",
        qr["addrs"] == i["addrs"] and qr["spki"] == p["spkiPin"],
    )
    token = token_bytes(qr["token"])
    check("the token is the QR's 16 bytes", token.hex() == i["tokenHex"])
    for typed in p["typedVariants"]:
        check(f"typed {typed!r}", token_bytes(parse_qr(typed)["token"]) == token)
    text = transcript(p["spkiPin"], i["deviceKey"], i["clientNonce"])
    check("transcript", text.decode() == p["transcript"])
    check("the app's MAC", client_mac(token, text) == p["appMac"])
    check("the server's MAC", server_mac(token, text, i["serverNonce"]) == p["serverMac"])
    check("commitment", commitment(i["serverNonce"]) == p["commitment"])
    bad = p["revealMismatch"]
    check(
        "a revealed nonce that is not the committed one is caught",
        commitment(bad["revealedServerNonce"]) != bad["commitment"],
    )
    check("SAS", sas(text, i["serverNonce"]) == p["sas"])
    s = v["session"]
    check("session text", session_text(s["deviceId"], s["nonce"]).decode() == s["text"])
    key = serialization.load_der_public_key(base64.b64decode(s["devicePublicKeySpkiBase64"]))
    assert isinstance(key, ec.EllipticCurvePublicKey)
    try:
        key.verify(base64.b64decode(s["signatureDerBase64"]), s["text"].encode(), ec.ECDSA(hashes.SHA256()))
        check("the session signature verifies (ECDSA P-256 SHA-256, DER)", True)
    except InvalidSignature:
        check("the session signature verifies (ECDSA P-256 SHA-256, DER)", False)


def approvals(v: dict[str, Any]) -> None:
    for case in v["jcs"]:
        check(
            f"JCS {json.dumps(case['input'])[:40]}", jcs(case["input"]) == case["output"], jcs(case["input"])
        )
    for case in v["digest"]:
        check(
            f"digest {case['kind']}",
            approval_digest(case["kind"], case["payload"], case["recipients"]) == case["digest"],
        )
    servers: dict[str, str] = {}  # each kind's first case is the payload exactly as the server sent it
    for case in v["payloadAsSent"]["cases"]:
        got = approval_digest(case["kind"], json.loads(case["payloadAsSent"]), case["recipients"])
        server = servers.setdefault(case["kind"], got)
        ok = got == case["digest"] and (got == server) == case["matchesTheServer"]
        check(f"payload as sent: {case['name'][:50]}", ok)
    for case in v["canonical"]:
        fields = {("digest" if k == "digest_hex" else k): x for k, x in case["fields"].items()}
        check(f"canonical text {fields['kind']}", canonical_text(fields) == case["text"])
        check("it parses back", parse_canonical(case["text"])["digest"] == fields["digest"])
    for case in v["phoneRejects"]:
        try:
            parse_canonical(case["text"])
            check(f"refuses to sign: {case['why']}", False)
        except ValueError:
            check(f"refuses to sign: {case['why']}", True)


def push(v: dict[str, Any]) -> None:
    p = v["push"]
    for case in p["cases"]:
        opened = open_sealed(case["sealed"], bytes.fromhex(p["recipientPrivateKeyHex"]), p["aad"])
        check(f"sealed push opens: {case['name']}", opened == json.loads(case["plaintext"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=HERE.parents[3] / "protocol" / "v2")
    fixtures = parser.parse_args().protocol / "fixtures"
    pairing_vectors = json.loads((fixtures / "pairing" / "vectors.json").read_text())
    pairing(pairing_vectors)
    approvals(json.loads((fixtures / "approval_vectors.json").read_text()))
    push(pairing_vectors)
    print(f"vectors: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
