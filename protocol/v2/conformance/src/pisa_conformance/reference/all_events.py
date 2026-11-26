"""One valid example of each of the 31 AG-UI 1.0 event types, plus every pisa.* server event.
Written to fixtures/events/*.jsonl for the Swift codec round-trip tests. Run: python -m pisa_conformance.reference.all_events"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import FIXTURES

OUT = FIXTURES / "events"
T = 1763460000000
PATCH = [{"op": "replace", "path": "/n", "value": 3}]

AGUI_EVENTS = [
    {"type": "RUN_STARTED", "threadId": "t1", "runId": "r1", "protocolVersion": "1.0", "parentRunId": "r0",
     "input": {"threadId": "t1", "runId": "r1", "messages": [{"id": "m0", "role": "user", "content": "Hi"}]}},
    {"type": "RUN_FINISHED", "threadId": "t1", "runId": "r1", "result": {"ok": True}, "outcome": {"type": "success", "pendingToolCallIds": ["c9"]}},
    {"type": "RUN_FINISHED", "threadId": "t1", "runId": "r2", "outcome": {"type": "interrupt", "interrupts": [
        {"id": "i1", "reason": "pisa.approval", "message": "Send an email", "toolCallId": "c1", "expiresAt": "2026-11-18T10:10:00Z",
         "responseSchema": {"type": "object"}, "metadata": {"k": 1}}]}},
    {"type": "RUN_FINISHED", "threadId": "t1", "runId": "r3", "outcome": {"type": "cancelled"}},
    {"type": "RUN_ERROR", "message": "Budget reached", "code": "budget_exceeded"},
    {"type": "STEP_STARTED", "stepName": "search"},
    {"type": "STEP_FINISHED", "stepName": "search"},
    {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant", "metadata": {"pisa": {"lang": "en-US", "priority": "normal"}}},
    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Hello"},
    {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
    {"type": "TEXT_MESSAGE_CHUNK", "messageId": "m2", "role": "assistant", "delta": "Hi"},
    {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "search_places", "parentMessageId": "m1"},
    {"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": "{\"q\":"},
    {"type": "TOOL_CALL_END", "toolCallId": "c1"},
    {"type": "TOOL_CALL_CHUNK", "toolCallId": "c2", "toolCallName": "weather", "delta": "{}"},
    {"type": "TOOL_CALL_RESULT", "messageId": "m3", "toolCallId": "c1", "content": "{\"found\":2}", "role": "tool"},
    {"type": "STATE_SNAPSHOT", "snapshot": {"n": 1}},
    {"type": "STATE_DELTA", "delta": PATCH},
    {"type": "MESSAGES_SNAPSHOT", "messages": [{"id": "m0", "role": "user", "content": "Hi"}, {"id": "m1", "role": "assistant", "content": "Hello"}]},
    {"type": "ACTIVITY_SNAPSHOT", "messageId": "a1", "activityType": "a2ui-surface", "content": {"n": 1}, "replace": True},
    {"type": "ACTIVITY_DELTA", "messageId": "a1", "activityType": "a2ui-surface", "patch": PATCH},
    {"type": "RAW", "event": {"anything": [1, 2]}, "source": "provider"},
    {"type": "CUSTOM", "name": "pisa.voice.state", "value": {"state": "listening"}},
    {"type": "REASONING_START", "messageId": "rz1"},
    {"type": "REASONING_MESSAGE_START", "messageId": "rz1", "role": "reasoning"},
    {"type": "REASONING_MESSAGE_CONTENT", "messageId": "rz1", "delta": "thinking"},
    {"type": "REASONING_MESSAGE_END", "messageId": "rz1"},
    {"type": "REASONING_MESSAGE_CHUNK", "messageId": "rz1", "delta": "more"},
    {"type": "REASONING_END", "messageId": "rz1"},
    {"type": "REASONING_ENCRYPTED_VALUE", "subtype": "message", "entityId": "rz1", "encryptedValue": "b3BhcXVl"},
    {"type": "SUBAGENT_STARTED", "subagentRunId": "s1", "name": "research", "description": "Looks things up", "parentToolCallId": "c1"},
    {"type": "SUBAGENT_FINISHED", "subagentRunId": "s1", "outcome": {"type": "success"}},
    {"type": "SUBAGENT_ERROR", "subagentRunId": "s2", "message": "failed", "code": "internal"},
]

PISA_EVENTS = [
    ("pisa.voice.audio", {"messageId": "m1", "seq": 0, "format": "aac_adts", "sampleRate": 24000, "data": "AAAA", "final": True, "complete": True}),
    ("pisa.voice.state", {"state": "speaking"}),
    ("pisa.voice.gate", {"heard": "owner"}),
    ("pisa.voice.transcript", {"text": "Hello there", "final": False}),
    ("pisa.voice.turn_hint", {"replyTo": "u1", "pEnd": 0.8, "waitMs": 300}),
    ("pisa.voice.understood", {"replyTo": "u1", "meant": "the station", "raw": "the nation", "corrections": [{"from": "nation", "to": "station"}], "ask": None}),
    ("pisa.notice", {"noticeId": "44444444-4444-4444-8444-444444444444", "kind": "done", "priority": "normal", "lang": "en-US", "text": "Done: parcel",
                     "wrist": "Done", "privacy": "none", "undo": {"undoId": "task:demo-2", "until": "2026-11-19T10:00:00Z"}}),
    ("pisa.ask_to_open", {"why": "leave_now", "line": "Time to go.", "lang": "en-US"}),
    ("pisa.directions", {"directionsId": "66666666-6666-4666-8666-666666666666", "name": "Example Station", "lat": 45.1, "lon": 9.1, "mode": "walk"}),
    ("pisa.call", {"callId": "88888888-8888-4888-8888-888888888888", "name": "Example Clinic", "phone": "+15550100100"}),
    ("pisa.compose", {"composeId": "99999999-9999-4999-8999-999999999999", "app": "messages", "recipient": {"name": "Sam"}, "text": "On my way"}),
    ("pisa.action_done", {"approvalId": "33333333-3333-4333-8333-333333333333", "kind": "email_send", "summary": "Email sent", "result": "Sent to 1 person"}),
    ("pisa.approval.state", {"approvalId": "33333333-3333-4333-8333-333333333333", "interruptId": "int-1", "state": "approved", "by": "voice"}),
    ("pisa.ai.disclosure", {"revision": "6b1f0c2d9e8a7b3c", "healthRequiresSeparateConsent": True, "processors": [
        {"slot": "conversation", "provider": {"id": "example-cloud", "name": "Example Cloud AI", "kind": "cloud", "region": "EU"},
         "model": "example-model-large", "dataCategories": ["conversation", "calendar", "places"], "features": ["conversation"]},
        {"slot": "mail_triage", "provider": {"id": "local", "name": "A model on your server", "kind": "on_server"},
         "model": "example-local-8b", "dataCategories": ["mail"]}]}),
    ("pisa.endpoints", {"revision": "r2", "spki": ["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
                        "endpoints": [{"kind": "lan", "address": "pisa-home.local", "port": 8443}, {"kind": "relay", "address": "relay.example.net", "relayHandle": "h-123"}]}),
    ("pisa.surface.focus", {"surfaceId": "places:demo-1", "mode": "take"}),
    ("pisa.entitlement.required", {"feature": "push.relay", "level": "pisa.pro"}),
    ("pisa.app.open", {"actionId": "77777777-7777-4777-8777-777777777777", "target": "tab", "id": "oggi"}),
    ("pisa.app.changed", {"views": ["riepilogo", "plan"], "at": "2026-11-19T10:00:00Z"}),
    ("pisa.maps.search", {"requestId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "query": "pharmacy", "near": "here", "kind": "pharmacy", "limit": 3}),
    ("pisa.maps.eta", {"requestId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "from": "here", "to": {"lat": 45.1, "lon": 9.1}, "mode": "walk"}),
    ("pisa.notify.schedule", {"revision": 1, "items": [{"id": "rem-1", "fireAt": "2026-11-19T09:30:00Z", "title": "Reminder", "kind": "reminder",
                                                        "deepLink": "pisa://chat"}]}),
    ("pisa.notify.cancel", {"revision": 2, "ids": ["rem-1"]}),
    ("pisa.notify.regions", {"revision": 3, "regions": [{"id": "reg-1", "lat": 45.1, "lon": 9.1, "radiusMeters": 200, "title": "Buy bread",
                                                         "on": "enter"}]}),
]


def agui_events() -> list[dict]:
    return [dict(e, timestamp=T + i) for i, e in enumerate(AGUI_EVENTS)]


def pisa_events() -> list[dict]:
    return [{"type": "CUSTOM", "timestamp": T + i, "name": n, "value": v} for i, (n, v) in enumerate(PISA_EVENTS)]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, evs in (("agui_all_types", agui_events()), ("pisa_server_events", pisa_events())):
        (OUT / f"{name}.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in evs), encoding="utf-8")


if __name__ == "__main__":
    main()
