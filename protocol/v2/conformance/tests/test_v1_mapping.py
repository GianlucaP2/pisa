"""Every v1 WebSocket message in protocol/fixtures/valid maps to valid contract-2 frames."""
import json

import pytest

from pisa_conformance.reference.v1map import CLIENT_TYPES, SERVER_TYPES, map_client, map_server
from conftest import PROTOCOL, V2
from pisa_conformance.validate import check_lifecycle, client_frame_errors, server_frame_errors

V1 = PROTOCOL / "fixtures" / "valid"
FIXTURES = sorted(V1.glob("*.json"))
SCHEMA = json.loads((PROTOCOL / "schema" / "pisa-device-protocol.v1.json").read_text())


def _types_in_schema():
    out = set()
    for d in SCHEMA["$defs"].values():
        c = d.get("properties", {}).get("type", {}).get("const")
        if c:
            out.add(c)
    return out


def test_every_v1_type_has_a_mapping():
    v1 = {t.replace("drawing_ask", "drawing.ask") for t in _types_in_schema()}
    unmapped = v1 - SERVER_TYPES - CLIENT_TYPES
    assert unmapped == set(), unmapped


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_fixture_maps_to_valid_v2(path):
    msg = json.loads(path.read_text())
    t = msg["type"]
    if t == "error":
        out = map_server(msg)
        for f in out:
            assert server_frame_errors(f) == []
        return
    if t in SERVER_TYPES:
        out = map_server(msg)
        assert out
        for f in out:
            assert server_frame_errors(f) == [], (path.name, f)
        assert check_lifecycle(out) == []
    else:
        assert t in CLIENT_TYPES, t
        out = map_client(msg)
        assert out
        for f in out:
            assert client_frame_errors(f) == [], (path.name, f)


BUILD60 = sorted((PROTOCOL / "fixtures").glob("proposed-*/present_*.json"))


@pytest.mark.parametrize("path", BUILD60, ids=[f"{p.parent.name}/{p.stem}" for p in BUILD60])
def test_build60_blocks_map_to_catalog(path):
    """K33 drawings (chart, diagram, timeline) and the answer blocks (findings, map, product) that build 60 renders."""
    out = map_server(json.loads(path.read_text()))
    for f in out:
        assert server_frame_errors(f) == [], (path.name, f)
    surface = next(f["event"] for f in out if f["event"]["type"] == "ACTIVITY_SNAPSHOT")
    comps = surface["content"]["a2ui_operations"][0]["createSurface"]["components"]
    assert surface["content"]["pisa"]["fallbackText"]
    for c in comps:
        if c["component"] in ("Chart", "Diagram", "Timeline", "Map", "Table", "Handoff", "PlaceSummary", "Ref"):
            assert c["fallback"].strip()
