"""The contract files cannot drift: frozen files keep their digests, generated files match their generators, the
vendored standards keep their pinned digests, and the AsyncAPI and OpenAPI documents point only at what exists."""
import copy
import hashlib
import json
import os
import runpy

import pytest

from conftest import PROTOCOL, V2

FROZEN = json.loads((V2 / "FROZEN.json").read_text())
SCHEMA = json.loads((V2 / "schema" / "pisa-contract-2.schema.json").read_text())
ASYNCAPI = json.loads((V2 / "asyncapi-live.json").read_text())
OPENAPI = json.loads((V2 / "openapi-v2-delta.json").read_text())


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.parametrize("rel", list(FROZEN["files"]))
def test_frozen_file_is_unchanged(rel):
    assert sha(V2 / rel) == FROZEN["files"][rel]["sha256"], f"{rel} is frozen: a change is a new contract minor (see FROZEN.json)"


def test_frozen_metadata_matches_the_files():
    assert SCHEMA["x-pisa-contract"] == FROZEN["files"]["schema/pisa-contract-2.schema.json"]["contract"]
    assert SCHEMA["$id"] == FROZEN["files"]["schema/pisa-contract-2.schema.json"]["id"]
    for rel in ("catalog/pisa-core.catalog.v1.json", "catalog/pisa-core.catalog.v2.json"):
        cat = json.loads((V2 / rel).read_text())
        assert cat["catalogId"] == cat["$id"] == FROZEN["files"][rel]["catalogId"]


def test_generated_files_do_not_drift():
    gen = runpy.run_path(str(V2 / "tools" / "generate.py"))
    assert gen["drift"]() == [], "run python3 protocol/v2/tools/generate.py and commit"


def test_vendored_standards_keep_their_pinned_digests():
    pins = [line.split("  ", 1) for line in (V2 / "vendor" / "SHA256SUMS").read_text().splitlines() if line.strip()]
    assert len(pins) == 8
    for digest, rel in pins:
        assert sha(V2 / "vendor" / rel) == digest, rel
    # the digests FROZEN.json pins: the two lists agree exactly
    vendored = {rel: v["sha256"] for rel, v in FROZEN["vendored"]["files"].items()}
    assert vendored == {f"vendor/{rel}": digest for digest, rel in pins if rel.endswith(".json")}


def _refs(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            out.append(node["$ref"])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)
    return out


def _resolve(ref, base):
    path, _, frag = ref.partition("#")
    if not path:
        doc = base
    else:
        doc = json.loads((V2 / path).resolve().read_text())
    if frag.startswith("/"):
        for part in frag[1:].split("/"):
            doc = doc[part.replace("~1", "/").replace("~0", "~")]
        return doc
    if frag:  # a JSON Schema 2020-12 $anchor
        found = [d for d in _walk(doc) if d.get("$anchor") == frag]
        assert found, ref
        return found[0]
    return doc


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.mark.parametrize("name,doc", [("asyncapi", ASYNCAPI), ("openapi", OPENAPI)])
def test_every_ref_resolves(name, doc):
    refs = _refs(doc, [])
    assert refs
    for r in refs:
        assert _resolve(r, doc) is not None, (name, r)


def test_asyncapi_lists_every_frame_and_event_of_the_schema():
    msgs = ASYNCAPI["components"]["messages"]
    for name in SCHEMA["x-pisa-server-events"]:
        assert "event." + name.replace(".", "_") in msgs
    for name in SCHEMA["x-pisa-client-events"]:
        assert "custom." + name.replace(".", "_") in msgs
    assert {"hello", "run", "cancel", "a2ui", "custom", "ping", "welcome", "pong", "error", "event"} <= set(msgs)
    assert not [k for k in msgs if "enrol" in k]  # enrolment is not on /v2/live (G5)
    assert ASYNCAPI["info"]["version"] == SCHEMA["x-pisa-contract"]


def test_openapi_delta_covers_spec_section_10():
    paths = OPENAPI["paths"]
    wanted = {("get", "/v1/server"), ("get", "/v2/live"), ("post", "/v2/runs"), ("post", "/v1/approvals/{id}/resume"),
              ("post", "/v1/phone/challenge"), ("get", "/v1/approvals/{id}"), ("get", "/v1/ai/providers/{id}/capabilities"),
              ("put", "/v1/devices/me/push"), ("post", "/v1/pair"), ("get", "/v1/pair/{pairingId}"), ("get", "/v1/devices/session/nonce"),
              ("post", "/v1/devices/session"), ("delete", "/v1/devices/me"), ("get", "/v1/server/endpoints"), ("get", "/v1/ai/disclosure"),
              ("post", "/v1/ai/consent"), ("get", "/v1/sheets/{id}"),
              # the model_server op (architect 1, 18 Nov)
              ("post", "/v1/ai/providers"), ("patch", "/v1/ai/providers/{id}"), ("delete", "/v1/ai/providers/{id}"),
              ("post", "/v1/ai/providers/{id}/probe"), ("put", "/v1/ai/providers/{id}/capabilities/overrides"),
              # pairing a new device from a paired phone (proposed)
              ("post", "/v1/pair/begin"), ("post", "/v1/pair/offers"), ("get", "/v1/pair/offers/{offerId}"), ("delete", "/v1/pair/offers/{offerId}"),
              ("post", "/v1/pair/offers/{offerId}/confirm")}
    have = {(m, p) for p, ops in paths.items() for m in ops}
    assert wanted == have
    ids = [op["operationId"] for ops in paths.values() for op in ops.values()]
    assert len(ids) == len(set(ids))


def _stub_external(doc):
    """Structure-only copy: external $refs (into the contract schema, the vendored standards, v1's OpenAPI) are checked
    by test_every_ref_resolves; the official validators here only judge the document's own structure."""
    doc = copy.deepcopy(doc)
    for d in _walk(doc):
        if isinstance(d.get("$ref"), str) and not d["$ref"].startswith("#"):
            d.pop("$ref")
            d["description"] = d.get("description", "external schema")
    return doc


def test_openapi_delta_is_valid_openapi_3_1():
    v = pytest.importorskip("openapi_spec_validator")
    v.validate(_stub_external(OPENAPI))


def test_asyncapi_is_valid_asyncapi_3():
    path = os.environ.get("ASYNCAPI_3_SCHEMA")
    if not path:
        pytest.skip("set ASYNCAPI_3_SCHEMA to @asyncapi/specs schemas/3.0.0.json (CI does)")
    import jsonschema
    schema = json.loads(open(path, encoding="utf-8").read())
    assert schema["$id"] == "http://asyncapi.com/definitions/3.0.0/asyncapi.json"
    v = jsonschema.Draft7Validator(schema)
    assert [e.message for e in v.iter_errors(ASYNCAPI)] == []
    broken = copy.deepcopy(ASYNCAPI)
    next(iter(broken["operations"].values()))["action"] = "publish"  # an AsyncAPI 2 verb: must be refused
    assert list(v.iter_errors(broken))
