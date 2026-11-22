"""The model_server signed device op: the OpenAPI delta, the canonical text and target, and the request bodies."""
import hashlib
import json

import jsonschema
import pytest

from conftest import PROTOCOL, V2
from pisa_conformance.reference import device_ops

FX = json.loads((V2 / "fixtures" / "device_ops" / "model_server.json").read_text(encoding="utf-8"))
DELTA = json.loads((V2 / "openapi-v2-delta.json").read_text())
SCHEMAS = DELTA["components"]["schemas"]


def schema_of(name):
    """A delta component as a standalone JSON Schema (its local refs resolve against the delta's components)."""
    return {"$defs": SCHEMAS, **json.loads(json.dumps(SCHEMAS[name]).replace("#/components/schemas/", "#/$defs/"))}


def test_recorded_fixture_matches_the_reference():
    assert FX == json.loads(json.dumps(device_ops.build()))


@pytest.mark.parametrize("case", FX["cases"], ids=lambda c: c["name"][:40])
def test_target_and_canonical(case):
    raw = case["body"].encode("utf-8")
    assert case["bodySha256"] == hashlib.sha256(raw).hexdigest()
    pid = json.loads(case["body"]).get("id") or case["path"].rsplit("/", 1)[1]
    assert case["challenge"]["target"] == f"{pid}:{case['bodySha256']}"
    assert case["canonical"] == "\n".join(["pisa-device", "op=model_server", f"target={case['challenge']['target']}",
                                          f"nonce={case['nonce']}", f"expires_at={case['expires_at']}"])
    assert jsonschema.Draft202012Validator(schema_of("DeviceChallengeRequest")).is_valid(case["challenge"])


def test_bytes_as_sent_not_a_reserialisation():
    a, b = FX["cases"][0], FX["cases"][1]
    assert json.loads(a["body"]) == json.loads(b["body"]) and a["challenge"]["target"] != b["challenge"]["target"]


def test_target_is_put_on_one_line():
    for t in FX["targetLine"]:
        assert device_ops.target_line(t["target"]) == t["line"]
        assert device_ops.canonical("model_server", t["target"], "n", "e").count("\n") == 4


@pytest.mark.parametrize("name", ["ServerCreate", "ServerUpdate"])
def test_request_bodies(name):
    v = jsonschema.Draft202012Validator(schema_of(name))
    for body in FX["bodies"][name]["valid"]:
        assert v.is_valid(body), body
    for bad in FX["bodies"][name]["invalid"]:
        assert not v.is_valid(bad["body"]), bad["why"]


def test_delta_marks_exactly_post_and_patch_as_signed():
    signed, unsigned = set(), set()
    for path, ops in DELTA["paths"].items():
        if path.startswith("/v1/ai/providers"):
            for method, o in ops.items():
                (signed if o.get("x-pisa-signed-op", {}).get("op") == "model_server" else unsigned).add((method.upper(), path))
    assert signed == {("POST", "/v1/ai/providers"), ("PATCH", "/v1/ai/providers/{id}")}
    assert unsigned == {("DELETE", "/v1/ai/providers/{id}"), ("POST", "/v1/ai/providers/{id}/probe"),
                        ("PUT", "/v1/ai/providers/{id}/capabilities/overrides"), ("GET", "/v1/ai/providers/{id}/capabilities")}
    for method, path in signed:
        o = DELTA["paths"][path][method.lower()]
        assert {"403", "501"} <= set(o["responses"])
        assert {p["$ref"].rsplit("/", 1)[1] for p in o["parameters"] if "$ref" in p} == {"XPisaNonce", "XPisaSignature", "XPisaKeyId"}


def test_device_op_enum_is_v1s_without_coder_and_code_pairing_plus_model_server():
    ops = SCHEMAS["DeviceChallengeRequest"]["properties"]["op"]["enum"]
    v1 = json.loads((PROTOCOL / "openapi.json").read_text())["components"]["schemas"]["ChallengeIn"]["properties"]["op"]["anyOf"][0]["enum"]
    proposed = SCHEMAS["DeviceChallengeRequest"]["properties"]["op"]["x-pisa-pairing-ops"]
    assert proposed == ["pairing_offer", "pairing_confirm"]
    assert set(ops) == (set(v1) - {"coder_secret", "pairing_code"}) | {"model_server"} | set(proposed)


def test_probe_overrides_and_get_answer_alike():
    caps = {"$ref": "#/components/schemas/ProviderCapabilities"}
    for path, method in (("/v1/ai/providers/{id}/capabilities", "get"), ("/v1/ai/providers/{id}/probe", "post"),
                         ("/v1/ai/providers/{id}/capabilities/overrides", "put")):
        assert DELTA["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"] == caps
    unprobed = {"model": "m", "probed_at": None, **{k: False for k in device_ops_caps()}, "overrides": {}, "features_needing_tools": []}
    assert jsonschema.Draft202012Validator(schema_of("ProviderCapabilities")).is_valid(unprobed)


def device_ops_caps():
    return ["tools", "json_schema", "json_object", "tool_choice", "vision", "audio", "embeddings"]
