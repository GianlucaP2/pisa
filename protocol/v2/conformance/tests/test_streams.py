import json

import pytest

from pisa_conformance.reference import scenarios
from pisa_conformance.validate import check_lifecycle, errors, server_frame_errors


@pytest.mark.parametrize("name", scenarios.VALID_SCENARIOS)
def test_every_frame_is_official_agui_and_a2ui(name):
    frames = scenarios.build(name)
    assert frames
    for f in frames:
        assert server_frame_errors(f) == [], (name, f["seq"])
    assert check_lifecycle(frames) == []


@pytest.mark.parametrize("name", list(scenarios.SCENARIOS))
def test_recorded_fixture_matches_the_emitter(name):
    recorded = [json.loads(line) for line in (scenarios.OUT / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert recorded == json.loads(json.dumps(scenarios.build(name)))


def test_seq_is_gapless_and_replay_after_works():
    s = scenarios.LiveSession(scenarios.DEVICE)
    scenarios.turn_places(s)
    assert [f["seq"] for f in s.frames] == list(range(1, len(s.frames) + 1))
    assert [f["seq"] for f in s.replay_after(5)] == list(range(6, len(s.frames) + 1))


def test_unknown_component_is_valid_agui_but_off_catalog():
    frames = scenarios.build("unknown_component")
    surface = next(f for f in frames if f["event"]["type"] == "ACTIVITY_SNAPSHOT")
    assert errors(surface["event"], "https://ag-ui.com/spec/1.0/schema.json") == []
    assert server_frame_errors(surface) != []  # the PISA catalog has no Hologram: an app must fall back


def test_approval_interrupt_message_holds_no_outside_text():
    frames = scenarios.build("approval")
    fin = next(f["event"] for f in frames if f["event"]["type"] == "RUN_FINISHED" and f["event"]["outcome"]["type"] == "interrupt")
    it = fin["outcome"]["interrupts"][0]
    assert it["message"] == "Send an email · notifies 1"
    assert "Dinner" not in json.dumps(it) and "sam@" not in json.dumps(it)


def test_approval_lives_on_its_own_thread():
    frames = scenarios.build("approval")
    threads = {f["threadId"] for f in frames}
    assert threads == {"thread-conversation", "pisa-approval:33333333-3333-4333-8333-333333333333"}
    convo_end = [f["event"] for f in frames if f["threadId"] == "thread-conversation" and f["event"]["type"] == "RUN_FINISHED"]
    assert convo_end[0]["outcome"]["type"] == "success"  # the talk is never blocked by a pending approval
