"""The approval thread's RUN_ERROR codes (action_failed added), and the payload the app hashes: exactly as sent."""
import json

from conftest import V2
from pisa_conformance.reference import scenarios
from pisa_conformance.reference.approvals import digest
from pisa_conformance.validate import check_lifecycle, server_frame_errors

SCHEMA = json.loads((V2 / "schema" / "pisa-contract-2.schema.json").read_text())
VECTORS = json.loads((V2 / "fixtures" / "approval_vectors.json").read_text())


def test_codes():
    assert SCHEMA["x-pisa-approval-error-codes"] == ["approval_changed", "approval_expired", "approval_decided", "biometric_required", "action_failed"]


def test_approval_failed_stream():
    frames = scenarios.build("approval_failed")
    assert check_lifecycle(frames) == []
    for f in frames:
        assert server_frame_errors(f) == [], f
    last = frames[-1]
    assert last["threadId"].startswith("pisa-approval:") and last["event"] == {**last["event"], "type": "RUN_ERROR", "code": "action_failed"}


def test_an_unknown_code_is_refused_only_on_an_approval_thread():
    frames = scenarios.build("approval_failed")
    bad = json.loads(json.dumps(frames[-1]))
    bad["event"]["code"] = "action_crashed"
    assert server_frame_errors(bad) != []
    bad["threadId"] = "thread-conversation"  # a conversation turn's codes are v1's open list
    assert server_frame_errors(bad) == []


def test_payload_as_sent_vectors():
    cases = VECTORS["payloadAsSent"]["cases"]
    assert any(c["matchesTheServer"] for c in cases) and any(not c["matchesTheServer"] for c in cases)
    first = {}
    for c in cases:
        d = digest(c["kind"], json.loads(c["payloadAsSent"]), c["recipients"])
        assert d == c["digest"], c["name"]
        first.setdefault(c["kind"], d)  # each kind's first case is the server's payload as sent
        assert (d == first[c["kind"]]) == c["matchesTheServer"], c["name"]
    email = json.loads(cases[0]["payloadAsSent"])
    assert list(email) == ["draft_id", "version", "to", "cc", "subject", "body", "attachments"]
    assert all(len(a) == 64 for a in email["attachments"])
    cal = next(json.loads(c["payloadAsSent"]) for c in cases if c["kind"] == "calendar_move")
    assert not [k for k in cal if k.startswith("_")]
