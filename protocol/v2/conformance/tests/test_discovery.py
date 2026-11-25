"""GET /v1/server: a required serverId, and the safety features (stop, approvals.signed) always on."""
import json

import pytest

from conftest import V2
from pisa_conformance import cli

D = V2 / "fixtures" / "discovery"
VALID = sorted((D / "valid").glob("*.json"))
INVALID = sorted((D / "invalid").glob("*.json"))
SCHEMA = json.loads((V2 / "schema" / "pisa-contract-2.schema.json").read_text())


@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_valid(path):
    assert cli.discovery_problems(json.loads(path.read_text())["document"]) == []


@pytest.mark.parametrize("path", INVALID, ids=[p.stem for p in INVALID])
def test_invalid(path):
    assert cli.discovery_problems(json.loads(path.read_text())["document"]) != []


def test_schema_alone_refuses_every_invalid_document():
    from pisa_conformance.validate import EXT_ID, errors
    for p in INVALID:
        assert errors(json.loads(p.read_text())["document"], EXT_ID + "#/$defs/ServerDiscovery"), p.name


def test_required_features_are_declared_and_minimal_profile_is_exactly_them():
    assert SCHEMA["x-pisa-required-features"] == ["stop", "approvals.signed"] == list(cli.REQUIRED_FEATURES)
    minimal = json.loads((V2 / "examples" / "server-minimal.json").read_text())
    assert {f["id"] for f in minimal["features"]} == set(cli.REQUIRED_FEATURES)
    assert "serverId" in SCHEMA["$defs"]["ServerDiscovery"]["required"]


def test_approval_interrupt_signs_the_same_server_id():
    assert SCHEMA["$defs"]["ApprovalInterrupt"]["properties"]["metadata"]["properties"]["pisa"]["properties"]["server"] == {"$ref": "#/$defs/ServerId"}


def test_discovery_cli_exit_codes(tmp_path):
    assert cli.main(["discovery", str(V2 / "examples" / "server-minimal.json")]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(json.loads((D / "invalid" / "no_stop.json").read_text())["document"]))
    assert cli.main(["discovery", str(bad)]) == 1


def test_live_negotiation_requires_safety_in_welcome():
    hello = cli.default_hello()
    w = {"control": "welcome", "seq": 0, "sessionId": "s", "contract": "2.0", "agui": "1.0",
         "a2ui": {"version": "1.0", "catalogIds": ["urn:pisa:a2ui:catalog:core:1"]}, "mcpApps": None, "rings": [0, 1],
         "extensions": {"pisa.notice": 1}, "features": [{"id": "today", "version": 1}]}
    probs = cli.negotiation_problems(hello, [w])
    assert any("stop" in p for p in probs) and any("approvals.signed" in p for p in probs) and any("pisa.approval" in p for p in probs)
