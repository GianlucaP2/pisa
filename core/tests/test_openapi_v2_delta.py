"""The generated `protocol/openapi.json` against contract 2's REST delta
(`protocol/v2/openapi-v2-delta.json`): every operation the delta defines and this server implements is
there, and its shapes match where the delta defines them: each property the delta names exists with a
compatible type, what the delta requires of a response is always in ours, and a request never requires what
the delta does not define.

Known differences are listed with their reason (`V1_KEPT`, `DIFFERENT`); operations of later
packages are in `PENDING`. A test fails when one of those lists goes stale, so it is never left in place."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pisa_conformance.paths import data_root
from pisa_conformance.validate import AGUI_ID, EXT_ID, registry

ROOT = Path(__file__).resolve().parents[2] / "protocol"
OURS = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
V2 = data_root()
DELTA = json.loads((V2 / "openapi-v2-delta.json").read_text(encoding="utf-8"))
CONTRACT = json.loads((V2 / "schema" / "pisa-contract-2.schema.json").read_text(encoding="utf-8"))

# operations of later packages: not served yet
PENDING = {
    ("get", "/v2/live"): "a WebSocket: asyncapi-live.json, checked by the live tests",
}
# operations served in another shape for now
DIFFERENT = {
    ("get", "/v1/sheets/{}"): "the sheet as an A2UI surface: not in B3/B4",
}
# request properties the delta here still lists and the contract has since removed: not served (a body
# carrying one is 400). Empty while protocol/v2 matches main; the stale check below keeps it honest.
REMOVED_ON_MAIN: dict[tuple[str, str], set[str]] = {}
# REST request bodies that also serve what the delta does not define: the phone's challenge for actions with
# no contract 2 counterpart yet (an autonomy level, a skill, a new key). v1's push token and approval
# challenge (without `interrupt_id`) are gone (B6b).
V1_KEPT = {
    ("post", "/v1/phone/challenge"): {"type", "level", "skill_id", "key_id", "public_key"},
}


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _resolve(schema: Any, doc: dict[str, Any]) -> Any:
    """Follow `$ref`s: ours to our components; the delta's to its components or the contract schema. A
    vendored AG-UI schema stays opaque (`{}`)."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 20:
        seen += 1
        ref = schema["$ref"]
        if ref.startswith("#/$defs/") and "$defs" not in doc:  # a contract schema's own ref, met by name
            schema, doc = CONTRACT["$defs"][ref.rsplit("/", 1)[1]], CONTRACT
        elif ref.startswith("#/"):
            target: Any = doc
            for part in ref[2:].split("/"):
                target = target[part]
            schema = target
        elif ref.startswith("schema/pisa-contract-2.schema.json#/$defs/"):
            schema, doc = CONTRACT["$defs"][ref.rsplit("/", 1)[1]], CONTRACT
        elif ref.startswith("#/$defs/"):
            schema = doc["$defs"][ref.rsplit("/", 1)[1]]
        else:
            return {}
    return schema


def _variants(schema: Any, doc: dict[str, Any]) -> list[dict[str, Any]]:
    schema = _resolve(schema, doc)
    if not isinstance(schema, dict):
        return []
    for key in ("oneOf", "anyOf"):
        if key in schema:
            return [v for s in schema[key] for v in _variants(s, doc)]
    return [schema]


def _types(schema: Any, doc: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for v in _variants(schema, doc):
        t = v.get("type")
        if t is None and "const" in v:
            t = {bool: "boolean", int: "integer", str: "string"}.get(type(v["const"]))
        if t is None and "enum" in v:
            t = "string"
        out |= set(t) if isinstance(t, list) else {t} if t else set()
    return out - {"null"}


def _object(schema: Any, doc: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Every property any variant names, and what every variant requires."""
    props: dict[str, Any] = {}
    required: set[str] | None = None
    for v in _variants(schema, doc):
        for part in [v, *[_resolve(a, doc) for a in v.get("allOf", [])]]:
            if isinstance(part, dict):
                props |= part.get("properties", {})
        req = set(v.get("required", []))
        required = req if required is None else required & req
    return props, required or set()


def _ours(method: str, path: str) -> dict[str, Any] | None:
    for p, ops in OURS["paths"].items():
        if _norm(p) == _norm(path) and method in ops:
            return ops[method]
    return None


def _json(content: dict[str, Any] | None) -> Any:
    return ((content or {}).get("application/json") or {}).get("schema")


def _delta_ops() -> list[tuple[str, str, dict[str, Any]]]:
    return [(m, p, op) for p, ops in DELTA["paths"].items() for m, op in ops.items()]


def _compare(where: tuple[str, str, str], theirs: Any, ours: Any, response: bool) -> list[str]:
    method, path, part = where
    their_props, their_req = _object(theirs, DELTA)
    our_props, our_req = _object(ours, OURS)
    problems = []
    removed = set() if response else REMOVED_ON_MAIN.get((method, _norm(path)), set())
    for name, schema in their_props.items():
        if name in removed:
            continue
        if name not in our_props:
            problems.append(f"{method} {path} {part}: no property {name}")
            continue
        theirs_t, ours_t = _types(schema, DELTA), _types(our_props[name], OURS)
        if theirs_t and ours_t and not theirs_t & ours_t:
            problems.append(f"{method} {path} {part}.{name}: {sorted(ours_t)} is not {sorted(theirs_t)}")
    if response:
        problems += [f"{method} {path} {part}: {n} is not always there" for n in their_req - our_req]
        extra = set(our_req) - set(their_props)
        if their_props and _resolve(theirs, DELTA).get("additionalProperties") is False:
            problems += [f"{method} {path} {part}: {name} is not in the delta" for name in sorted(extra)]
    elif their_props:  # an opaque schema (AG-UI's RunAgentInput, vendored) defines no properties to compare
        allowed = V1_KEPT.get((method, _norm(path)), set())
        problems += [
            f"{method} {path} {part}: requires {n}, which the delta does not define"
            for n in our_req - set(their_props) - allowed
        ]
    return problems


@pytest.mark.parametrize(
    ("method", "path", "op"), _delta_ops(), ids=lambda x: x if isinstance(x, str) else ""
)
def test_each_delta_operation_is_served_with_its_shapes(method: str, path: str, op: dict[str, Any]) -> None:
    ours = _ours(method, path)
    if (method, _norm(path)) in PENDING | DIFFERENT:
        return
    assert ours is not None, f"{method.upper()} {path} is in the delta and not served"
    problems: list[str] = []
    their_body = _json((op.get("requestBody") or {}).get("content"))
    if their_body is not None:
        our_body = _json((ours.get("requestBody") or {}).get("content"))
        assert our_body is not None, f"{method} {path}: no JSON request body"
        problems += _compare((method, path, "request"), their_body, our_body, response=False)
    for status, response in op["responses"].items():
        if status == "default" or not status.startswith("2"):
            continue
        assert status in ours["responses"], f"{method} {path}: no {status} response"
        theirs = _json(_resolve(response, DELTA).get("content"))
        if theirs is not None:
            problems += _compare(
                (method, path, status), theirs, _json(ours["responses"][status].get("content")), True
            )
    assert problems == []


def test_the_pending_and_refined_lists_are_not_stale() -> None:
    delta = {(m, _norm(p)) for m, p, _ in _delta_ops()}
    assert set(PENDING) <= delta
    assert set(DIFFERENT) <= delta and all(_ours(*k) is not None for k in DIFFERENT)
    served = [k for k in PENDING if _ours(*k) is not None]
    assert served == [], f"served now: move them out of PENDING: {served}"
    assert set(V1_KEPT) <= delta
    for (method, path), names in REMOVED_ON_MAIN.items():  # still in the delta here, and not served
        ours = _ours(method, path)
        assert ours is not None
        theirs_props = _object(_json(DELTA["paths"][path][method]["requestBody"]["content"]), DELTA)[0]
        our_props = _object(_json(ours["requestBody"]["content"]), OURS)[0]
        assert names <= set(theirs_props), (
            f"{method} {path}: the delta dropped it, drop it from REMOVED_ON_MAIN"
        )
        assert not names & set(our_props), f"{method} {path}: served again"


def _inline(schema: Any) -> Any:
    """The delta's schema with its references made absolute for the kit's registry (AG-UI 1.0 and the contract
    schema) and its own components inlined."""
    if isinstance(schema, list):
        return [_inline(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref.startswith("vendor/ag-ui-1.0/schema.json"):
            return {"$ref": AGUI_ID + ref.removeprefix("vendor/ag-ui-1.0/schema.json")}
        if ref.startswith("schema/pisa-contract-2.schema.json"):
            return {"$ref": EXT_ID + ref.removeprefix("schema/pisa-contract-2.schema.json")}
        if ref.startswith("#/components/"):
            return _inline(_resolve(schema, DELTA))
    return {k: _inline(v) for k, v in schema.items()}


def delta_body_errors(method: str, path: str, status: str, body: Any) -> list[str]:
    """A response body checked against the delta's own schema for it (CONTRACT-2 §10)."""
    response = _resolve(DELTA["paths"][path][method]["responses"][status], DELTA)
    schema = _inline(_json(response.get("content")))
    validator = Draft202012Validator(schema, registry=registry())
    return [f"{e.json_path}: {e.message[:200]}" for e in validator.iter_errors(body)]


def test_the_body_check_refuses_what_the_delta_refuses() -> None:
    run = {"type": "RUN_STARTED", "threadId": "t", "runId": "r"}
    assert delta_body_errors("post", "/v2/runs", "200", {"events": [run]}) == []
    assert delta_body_errors("post", "/v2/runs", "200", {"run_id": "r", "events": [run]})
    assert delta_body_errors("post", "/v2/runs", "202", {"runId": "r"}) == []
    assert delta_body_errors("post", "/v2/runs", "202", {"run_id": "r"})
