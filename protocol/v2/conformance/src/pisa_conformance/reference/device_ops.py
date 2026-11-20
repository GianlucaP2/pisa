"""Signed device ops (v1 CORE-SEC, prefix pisa-device in contract 2), and the model_server op.

The phone signs, after Face ID:

    pisa-device\\nop=<op>\\ntarget=<target>\\nnonce=<nonce>\\nexpires_at=<iso>

and sends the request with X-Pisa-Nonce, X-Pisa-Signature (DER, base64) and X-Pisa-Key-Id. For model_server the
target is `<provider id>:<SHA-256 hex of the request body, exactly as sent>`: the id from the body on POST
/v1/ai/providers, from the path on PATCH /v1/ai/providers/{id}. The hash is over the bytes on the wire, never a
re-serialisation, so the server hashes the raw body before parsing it.

Writes fixtures/device_ops/model_server.json. Run: python -m pisa_conformance.reference.device_ops
Synthetic data only (addresses from the documentation ranges, RFC 5737).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..paths import FIXTURES

OUT = FIXTURES / "device_ops" / "model_server.json"
HEADER = "pisa-device"


def target_line(target: str) -> str:
    """The server puts the target on one line: control characters become spaces (as v1)."""
    return "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in target)


def canonical(op: str, target: str, nonce: str, expires_at: str) -> str:
    return "\n".join([HEADER, f"op={op}", f"target={target_line(target)}", f"nonce={nonce}", f"expires_at={expires_at}"])


def model_server_target(provider_id: str, body: bytes) -> str:
    return f"{provider_id}:{hashlib.sha256(body).hexdigest()}"


NONCE, EXPIRES = "bm9uY2UtbW9kZWwtc2VydmVy", "2026-11-18T10:05:00Z"

# (name, method, path, provider id, body as sent)
CASES = [
    ("add a LAN server", "POST", "/v1/ai/providers", "lab-gpu",
     '{"type":"openai_compatible","id":"lab-gpu","base_url":"http://192.0.2.20:8000/v1"}'),
    ("the same body with another key order hashes differently: the bytes as sent are signed", "POST", "/v1/ai/providers", "lab-gpu",
     '{"id":"lab-gpu","type":"openai_compatible","base_url":"http://192.0.2.20:8000/v1"}'),
    ("add with a name (UTF-8 bytes) and a private CA", "POST", "/v1/ai/providers", "studio",
     '{"type": "openai_compatible", "id": "studio", "base_url": "https://198.51.100.7/v1", "name": "Studio · Büro",'
     ' "ca_bundle": "/etc/pisa/ca/studio.pem"}'),
    ("move a server to a new address (forgets its probes)", "PATCH", "/v1/ai/providers/lab-gpu", "lab-gpu",
     '{"base_url":"http://203.0.113.9:8000/v1"}'),
    ("remove a private CA", "PATCH", "/v1/ai/providers/studio", "studio", '{"ca_bundle":null}'),
]

VALID_CREATE = [json.loads(c[4]) for c in CASES if c[1] == "POST"]
VALID_UPDATE = [json.loads(c[4]) for c in CASES if c[1] == "PATCH"] + [{}]
INVALID_CREATE = [
    ("no base_url", {"type": "openai_compatible", "id": "lab-gpu"}),
    ("an id with capitals", {"type": "openai_compatible", "id": "Lab", "base_url": "http://192.0.2.20:8000/v1"}),
    ("a built-in type", {"type": "builtin", "id": "lab-gpu", "base_url": "http://192.0.2.20:8000/v1"}),
    ("an unknown field", {"type": "openai_compatible", "id": "lab-gpu", "base_url": "http://192.0.2.20:8000/v1", "key": "x"}),
]
INVALID_UPDATE = [("the id cannot change", {"id": "other"}), ("an empty address", {"base_url": ""})]


def build() -> dict[str, Any]:
    cases = []
    for name, method, path, pid, body in CASES:
        raw = body.encode("utf-8")
        target = model_server_target(pid, raw)
        cases.append({"name": name, "method": method, "path": path, "body": body, "bodySha256": hashlib.sha256(raw).hexdigest(),
                      "challenge": {"action": "device", "op": "model_server", "target": target},
                      "nonce": NONCE, "expires_at": EXPIRES, "canonical": canonical("model_server", target, NONCE, EXPIRES)})
    return {
        "description": "The model_server signed device op (contract 2). body is the exact request body; the target hashes its UTF-8 "
                       "bytes as sent. A server that does not offer model servers answers 501 unsupported (challenge and routes).",
        "canonicalFormat": "pisa-device\\nop=<op>\\ntarget=<target>\\nnonce=<nonce>\\nexpires_at=<iso>",
        "signedRoutes": [{"method": "POST", "path": "/v1/ai/providers", "idFrom": "body"},
                         {"method": "PATCH", "path": "/v1/ai/providers/{id}", "idFrom": "path"}],
        "unsignedRoutes": [{"method": "DELETE", "path": "/v1/ai/providers/{id}"}, {"method": "POST", "path": "/v1/ai/providers/{id}/probe"},
                           {"method": "PUT", "path": "/v1/ai/providers/{id}/capabilities/overrides"}],
        "cases": cases,
        "targetLine": [{"target": "lab-gpu:abc\ndecision=approve", "line": "lab-gpu:abc decision=approve"}],
        "bodies": {"ServerCreate": {"valid": VALID_CREATE, "invalid": [{"why": w, "body": b} for w, b in INVALID_CREATE]},
                   "ServerUpdate": {"valid": VALID_UPDATE, "invalid": [{"why": w, "body": b} for w, b in INVALID_UPDATE]}},
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
