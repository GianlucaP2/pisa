import json

from pisa_conformance.reference.agui import LiveSession
from pisa_conformance.reference.v1map import map_client
from conftest import PROTOCOL, V2
from pisa_conformance.validate import client_frame_errors, errors, EXT_ID, server_frame_errors

HELLO_V1 = json.loads((PROTOCOL / "fixtures" / "valid" / "hello_present_drawing.json").read_text())
CAT = "urn:pisa:a2ui:catalog:core:1"
FEATURES = [{"id": "stop", "version": 1}, {"id": "approvals.signed", "version": 2}, {"id": "today", "version": 1}, {"id": "push.relay", "version": 1, "entitlement": "pisa.pro"}]


def hello():
    return map_client(HELLO_V1)[0]


def test_hello_validates():
    assert client_frame_errors(hello()) == []


def test_welcome_picks_only_what_the_app_offered():
    w = LiveSession("d").welcome(hello(), [CAT, "urn:pisa:a2ui:catalog:core:2"], FEATURES)
    assert server_frame_errors(w) == []
    assert w["a2ui"] == {"version": "1.0", "catalogIds": [CAT]}
    assert w["rings"] == [0, 1, 2] and w["mcpApps"] == {"protocolVersion": "2026-01-26"}


def test_no_common_catalog_means_no_ring_1():
    h = hello()
    h["a2ui"] = {"1.0": {"supportedCatalogIds": ["urn:other:catalog"]}}
    w = LiveSession("d").welcome(h, [CAT], FEATURES)
    assert w["a2ui"] is None and 1 not in w["rings"]


def test_app_without_ring_2_gets_no_mcp_apps():
    h = hello()
    h["rings"] = [0, 1]
    w = LiveSession("d").welcome(h, [CAT], FEATURES)
    assert w["mcpApps"] is None and w["rings"] == [0, 1]


def test_unsupported_contract_is_one_clear_error():
    h = hello()
    h["contract"]["versions"] = ["3.0"]
    w = LiveSession("d").welcome(h, [CAT], FEATURES)
    assert w["code"] == "contract_unsupported" and w["retryable"] is False
    assert server_frame_errors(w) == []


def test_server_discovery_example_validates():
    doc = json.loads((V2 / "examples" / "server.json").read_text())
    assert errors(doc, EXT_ID + "#/$defs/ServerDiscovery") == []
