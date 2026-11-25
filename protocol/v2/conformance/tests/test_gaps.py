"""MAPPING.md §6: the gaps package A closes in the schema (G1, G2, G4, G5, G6), with their fixtures."""
import json

import pytest

from conftest import PROTOCOL, V2
from pisa_conformance.reference import enrol, scenarios, v1map
from pisa_conformance.validate import (EXT_ID, check_lifecycle, check_seq, client_frame_errors, enrol_client_frame_errors,
                                       enrol_server_frame_errors, errors, server_frame_errors)

CHECKS = {"client_frame": client_frame_errors, "server_frame": server_frame_errors,
          "enrol_server_frame": enrol_server_frame_errors, "enrol_client_frame": enrol_client_frame_errors}
GAPS = V2 / "fixtures" / "gaps"
VALID = sorted((GAPS / "valid").glob("*.json"))
INVALID = sorted((GAPS / "invalid").glob("*.json"))
SCHEMA = json.loads((V2 / "schema" / "pisa-contract-2.schema.json").read_text())


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_every_gap_has_valid_and_invalid_fixtures():
    for gap in ("G1", "G2", "G4", "G5", "G6"):
        assert any(load(p)["gap"] == gap for p in VALID), gap
        assert any(load(p)["gap"] == gap for p in INVALID), gap


@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_valid_gap_fixture(path):
    fx = load(path)
    assert CHECKS[fx["check"]](fx["frame"]) == [], path.name


@pytest.mark.parametrize("path", INVALID, ids=[p.stem for p in INVALID])
def test_invalid_gap_fixture(path):
    fx = load(path)
    assert CHECKS[fx["check"]](fx["frame"]) != [], path.name


def _mapped(fx):
    v1, check = fx["v1"], fx["check"]
    t = v1["type"]
    if t == "hello":
        return v1map.map_client(v1)[0]
    if t == "talk.route":
        return v1map.map_talk_route(v1)
    if t == "talk.ended":
        return fx["frame"] | {"event": fx["frame"]["event"] | {"result": v1map.talk_ended_result(v1)}}
    if t == "talk.transcript":
        return fx["frame"] | {"event": fx["frame"]["event"] | {"value": v1map.map_talk_transcript(v1)}}
    if t == "talk.gate":
        return fx["frame"] | {"event": fx["frame"]["event"] | {"value": v1map.map_talk_gate(v1)}}
    if check == "enrol_server_frame":
        return v1map.map_enrol_server(v1, fx["frame"]["seq"])
    if check == "enrol_client_frame":
        return v1map.map_enrol_client(v1)
    if t == "present":
        return next(f for f in v1map.map_server(v1) if f["event"]["type"] == "ACTIVITY_SNAPSHOT")
    raise KeyError(t)


WITH_V1 = [p for p in VALID if "v1" in load(p)]


@pytest.mark.parametrize("path", WITH_V1, ids=[p.stem for p in WITH_V1])
def test_v1_message_maps_to_the_fixture(path):
    fx = load(path)
    assert _mapped(fx) == fx["frame"]


def test_g1_no_backend_field_and_the_v1_booleans_are_gone():
    hello = load(GAPS / "valid" / "g1_hello_from_v1_drops_capability_booleans.json")
    assert "capabilities" not in hello["frame"]["device"]
    assert "G1" in SCHEMA["$defs"]["Hello"]["$comment"]


def test_g2_routes_cover_every_v1_talk_route():
    v1_talk_routes = {"speaker", "receiver", "airpods", "headphones", "car", "glasses"}
    route = SCHEMA["$defs"]["c_pisa_presence"]["properties"]["state"]["properties"]["route"]["enum"]
    assert v1_talk_routes <= set(route)
    assert set(SCHEMA["$defs"]["s_pisa_voice_enrol_done"]["properties"]["route"]["enum"]) == v1_talk_routes


def test_g4_every_v1_talk_end_reason_is_a_result():
    for reason in ("idle", "owner", "budget", "error", "fallback", "route"):
        assert errors({"talkEnded": reason}, EXT_ID + "#/$defs/TalkEndedResult") == []


def test_g5_enrolment_is_a_closed_set_kept_off_the_live_socket():
    assert SCHEMA["x-pisa-enrol-server-events"] == ["pisa.voice.enrol.quality", "pisa.voice.enrol.test_result", "pisa.voice.enrol.done"]
    assert set(SCHEMA["x-pisa-enrol-client-events"]) == {"pisa.voice.enrol.block", "pisa.voice.enrol.test", "pisa.voice.enrol.finish", "pisa.voice.enrol.cancel"}
    live = set(SCHEMA["x-pisa-server-events"]) | set(SCHEMA["x-pisa-client-events"])
    assert not {n for n in live if n.startswith("pisa.voice.enrol.")}
    assert "pisa.voice.gate" in SCHEMA["x-pisa-server-events"]


@pytest.mark.parametrize("name", list(enrol.FILES))
def test_g5_recorded_enrolment_matches_the_reference(name):
    recorded = [json.loads(line) for line in (V2 / "fixtures" / "enrol" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert recorded == enrol.FILES[name]()


def test_g5_enrolment_sessions_are_valid_and_gapless():
    for name in ("server_ok", "server_missing"):
        frames = enrol.FILES[name]()
        assert check_seq(frames, first=1) == []
        for f in frames:
            assert enrol_server_frame_errors(f) == [], (name, f)
        assert frames[-1]["event"]["name"] == "pisa.voice.enrol.done"
    for f in enrol.client_session():
        assert enrol_client_frame_errors(f) == []


def test_g4_g5_g6_in_a_whole_gated_talk():
    frames = scenarios.build("talk_gated")
    assert check_lifecycle(frames) == []
    for f in frames:
        assert server_frame_errors(f) == []
    names = [f["event"].get("name") for f in frames]
    assert "pisa.voice.gate" in names
    assert frames[-1]["event"]["result"] == {"talkEnded": "owner"}
    surface = next(f["event"] for f in frames if f["event"]["type"] == "ACTIVITY_SNAPSHOT")
    assert surface["content"]["pisa"]["expiresAt"] == "2026-11-18T17:05:00Z"


def test_g6_v1_present_fixture_carries_expires_at():
    v1 = json.loads((PROTOCOL / "fixtures" / "valid" / "present_choice.json").read_text())
    surface = next(f["event"] for f in v1map.map_server(v1) if f["event"]["type"] == "ACTIVITY_SNAPSHOT")
    assert surface["content"]["pisa"]["expiresAt"] == v1["expires_at"]


def test_transcript_ignored_reasons_cover_v1():
    """Architect 1's ruling on flag 5: every v1 ignored reason (K4 noise/echo, VOICE-ID-1 language/fragment) exists."""
    enum = SCHEMA["$defs"]["s_pisa_voice_transcript"]["properties"]["ignored"]["enum"]
    assert set(enum) == {"noise", "echo", "language", "fragment"}
    assert any(load(p)["gap"] == "transcript" for p in INVALID)


def test_enrol_reasons_are_closed_and_each_has_a_fixture():
    reasons = SCHEMA["$defs"]["EnrolReason"]["enum"]
    assert reasons == SCHEMA["x-pisa-enrol-reasons"] == ["missing", "consent", "no_speech", "timeout", "not_reliable", "error"]
    have = {load(p)["frame"]["event"]["value"].get("reason") for p in VALID if p.stem.startswith("enrol_reason_")}
    assert have == set(reasons)
    assert "clean" in SCHEMA["$defs"]["s_pisa_voice_enrol_done"]["properties"]["bySnr"]["required"]


def test_v1_unreliable_result_becomes_not_reliable():
    v = v1map.map_enrol_server({"type": "enrol.done", "route": "speaker", "ok": False, "owner_false_reject": 0.3,
                                "strangers_per_min": 0.2, "by_snr": {"clean": 1, "10": 1, "5": 1, "0": 1}}, 1)
    assert v["event"]["value"]["reason"] == "not_reliable"


def test_talk_text_is_an_additive_client_event_of_contract_2_1():
    """Contract 2.1 adds one client event, `pisa.voice.talk.text` (the feature talk.text), and changes no frame."""
    assert SCHEMA["x-pisa-contract"] in ("2.1", "2.2", "2.3", "2.4")  # 2.2 to 2.4 are additive over it
    assert "pisa.voice.talk.text" in SCHEMA["x-pisa-client-events"]
    assert set(SCHEMA["$defs"]["c_pisa_voice_talk_text"]["required"]) == {"id", "text"}
    frame = {"op": "custom", "name": "pisa.voice.talk.text", "value": {"id": "5b0e4c1e-8a7d-4c4b-9a39-2f4f1c9d7e10"}}
    assert client_frame_errors(frame | {"value": frame["value"] | {"text": "a" * 4000}}) == []
    assert client_frame_errors(frame | {"value": frame["value"] | {"text": "a" * 4001}}) != []
    assert client_frame_errors(frame | {"value": frame["value"] | {"text": "Ciao", "turnId": "x"}}) != []


def test_app_actions_and_the_screen_map_are_additive_in_contract_2_2():
    """Contract 2.2 adds the extension pisa.app (pisa.app.open, pisa.app.changed; the client's pisa.app.result), the run's
    screen map (TurnProps.app, $defs/AppMap) and the approval kind setting_change; no frame of 2.1 changes."""
    assert SCHEMA["x-pisa-contract"] in ("2.2", "2.3", "2.4") and "pisa.app" in SCHEMA["x-pisa-extensions"]  # 2.3, 2.4 additive
    assert {"pisa.app.open", "pisa.app.changed"} <= set(SCHEMA["x-pisa-server-events"])
    assert "pisa.app.result" in SCHEMA["x-pisa-client-events"]
    kinds = SCHEMA["$defs"]["ApprovalInterrupt"]["properties"]["metadata"]["properties"]["pisa"]["properties"]["kind"]["enum"]
    assert kinds[-1] == "setting_change"
    app = {"version": "1.2.0", "tabs": [{"id": "pisa", "title": "Pisa"}, {"id": "oggi", "title": "Oggi"},
           {"id": "impostazioni", "title": "Impostazioni"}], "current": "oggi", "cards": ["riepilogo", "tasks"],
           "settings": ["Memoria", "AI e privacy"]}
    def run(props):
        return {"op": "run", "input": {"threadId": "t-1", "runId": "r-1", "state": {}, "tools": [], "context": [],
                "messages": [{"id": "m-1", "role": "user", "content": "Cosa c'è in Oggi?"}],
                "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "it-IT"}} | props}}}
    assert errors({"turn": {"source": "typed", "lang": "it-IT"}, "app": app}, EXT_ID + "#/$defs/TurnProps") == []
    for bad in ({"tabs": []}, {"current": "Oggi"}, {"cards": ["x"] * 13}, {"settings": ["a" * 41]},
                {"tabs": [{"id": "pisa", "title": "Pi\nsa"}]}, {"extra": 1}):
        assert errors({"app": app | bad}, EXT_ID + "#/$defs/TurnProps") != [], bad
    assert client_frame_errors(run({"app": app})) == []


def test_phone_maps_and_local_notifications_are_additive_in_contract_2_3():
    """Contract 2.3 adds the extensions pisa.maps (pisa.maps.search, pisa.maps.eta; the client's pisa.maps.result) and
    pisa.notify (pisa.notify.schedule, .cancel, .regions; the client's pisa.notify.ack), each with valid and invalid
    fixtures; no frame of 2.2 changes. What the phone sends back is untrusted input: bounded and closed."""
    assert SCHEMA["x-pisa-contract"] in ("2.3", "2.4")  # 2.4 is additive over it
    assert SCHEMA["x-pisa-extensions"][-2:] == ["pisa.maps", "pisa.notify"]
    assert SCHEMA["x-pisa-server-events"][-5:] == ["pisa.maps.search", "pisa.maps.eta", "pisa.notify.schedule", "pisa.notify.cancel",
                                                   "pisa.notify.regions"]
    assert SCHEMA["x-pisa-client-events"][-2:] == ["pisa.maps.result", "pisa.notify.ack"]
    for gap in ("maps.phone", "notify.local"):
        assert any(load(p)["gap"] == gap for p in VALID), gap
        assert any(load(p)["gap"] == gap for p in INVALID), gap
    d = SCHEMA["$defs"]
    assert d["MapsPlace"]["additionalProperties"] is False and d["MapsPlace"]["properties"]["name"]["maxLength"] == 120
    assert d["c_pisa_maps_result"]["properties"]["places"]["maxItems"] == 10
    assert d["s_pisa_notify_schedule"]["properties"]["items"]["maxItems"] == 50
    assert d["s_pisa_notify_regions"]["properties"]["regions"]["maxItems"] == 20
    rid = "3f2a1b0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c"
    item = {"id": "rem-1", "fireAt": "2026-11-25T08:30:00+01:00", "title": "Promemoria", "kind": "task"}

    def schedule(link):
        return {"seq": 1, "threadId": "pisa-notices", "runId": "r-1", "event": {"type": "CUSTOM", "name": "pisa.notify.schedule",
                "value": {"revision": 1, "items": [item | {"deepLink": link}]}}}
    for good in ("pisa://chat", "pisa://riepilogo", "pisa://task/" + rid, "pisa://reminder/" + rid):
        assert server_frame_errors(schedule(good)) == [], good
    for bad in ("pisa://pair?v=1", "pisa://task/1", "pisa://chat/x", "https://example.org", "pisa://Task/" + rid, "pisa://chat\n"):
        assert server_frame_errors(schedule(bad)) != [], bad
    place = {"id": "p-1", "name": "Example", "lat": 45.0, "lon": 9.0}
    result = {"op": "custom", "name": "pisa.maps.result", "value": {"requestId": rid, "ok": True, "places": [place]}}
    assert client_frame_errors(result) == []
    for bad in ({"name": "Exa\u202emple"}, {"lat": 91}, {"address": ""}, {"phone": "555"}, {"distanceMeters": -1}):
        assert client_frame_errors(result | {"value": result["value"] | {"places": [place | bad]}}) != [], bad
    assert client_frame_errors(result | {"value": {"requestId": rid, "ok": False, "reason": "no_results"}}) == []
    assert client_frame_errors(result | {"value": {"requestId": rid, "ok": False}}) != []


def test_a_presence_with_no_event_is_valid_in_contract_2_4():
    """Contract 2.4 makes pisa.presence's event optional (additive: every 2.3 presence stays valid): a route change or a
    lock with no event of its own sends state alone. Still closed: no other property, the event a closed set."""
    assert SCHEMA["x-pisa-contract"] == "2.4"
    assert SCHEMA["$defs"]["c_pisa_presence"].get("required", []) == []

    def presence(value):
        return {"op": "custom", "name": "pisa.presence", "value": value}
    assert client_frame_errors(presence({"state": {"route": "car"}})) == []
    assert client_frame_errors(presence({"event": "app_foreground", "state": {"route": "car"}})) == []
    assert client_frame_errors(presence({})) == []
    assert client_frame_errors(presence({"event": "route_changed"})) != []
    assert client_frame_errors(presence({"state": {"route": "car"}, "where": "x"})) != []
