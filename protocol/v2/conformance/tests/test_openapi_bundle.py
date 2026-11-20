"""pisa_conformance.openapi: the delta bundled into one document whose every $ref is local and resolves."""
import json
import re

import pytest
from pisa_conformance.openapi import bundled


def _resolve(doc, ref):
    node = doc
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")] if isinstance(node, dict) else node[int(part)]
    return node


def test_every_reference_is_local_and_resolves():
    """Every $ref is local; every one reachable from the paths resolves (v1's openapi.json is carried whole)."""
    doc = bundled()
    refs = set(re.findall(r'"\$ref": "([^"]*)"', json.dumps(doc)))
    assert refs and all(r.startswith("#/") for r in refs)
    todo, seen = set(re.findall(r'"\$ref": "([^"]*)"', json.dumps(doc["paths"]))), set()
    while todo:
        r = todo.pop()
        seen.add(r)
        todo |= set(re.findall(r'"\$ref": "([^"]*)"', json.dumps(_resolve(doc, r)))) - seen
    assert len(seen) > 20
    assert '"$id"' not in json.dumps(doc["components"]["schemas"]["x-bundled-pisa2"])


def test_operations_are_kept_by_id():
    doc = bundled({"getApproval", "resumeApproval"})
    ops = {o["operationId"] for p in doc["paths"].values() for o in p.values() if isinstance(o, dict)}
    assert ops == {"getApproval", "resumeApproval"}
    with pytest.raises(KeyError):
        bundled({"noSuchOperation"})


def test_schemathesis_loads_it():
    schemathesis = pytest.importorskip("schemathesis")
    schema = schemathesis.openapi.from_dict(bundled({"resumeApproval", "getServerEndpoints"}))
    labels = sorted(o.ok().label for o in schema.get_all_operations())
    assert labels == ["GET /v1/server/endpoints", "POST /v1/approvals/{id}/resume"]
