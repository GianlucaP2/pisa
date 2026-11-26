"""Every pisa-device-protocol.v1 (formerly j-device-protocol.v1) WebSocket message -> its contract-2 form. Executable half of MAPPING.md.

map_server(msg) returns the frames a v2 server sends instead; map_client(msg) returns the frames a v2 app
sends instead. Both raise KeyError for a type nobody mapped, so a new v1 type fails the tests.
"""
from __future__ import annotations

from typing import Any

from .a2ui import card_to_surface, mcp_app_from_drawing, object_to_surface
from .agui import Clock, Ids, LiveSession
from .approvals import digest, interrupt_for

SERVER_ID = "srv_synthetic0001_Qm7Zr2Xc"


def _camel_presence_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    m = {"glasses_worn": "glassesWorn"}
    return {m.get(k, k): v for k, v in state.items()}


def map_server(msg: dict[str, Any], session: LiveSession | None = None) -> list[dict[str, Any]]:
    s = session or LiveSession("00000000-0000-4000-8000-000000000100", Clock(), Ids())
    t = msg["type"]
    convo = "thread-conversation"
    notices = s.notice_thread()

    def run_with(thread: str, fn) -> list[dict[str, Any]]:
        start = len(s.frames)
        with s.run(thread) as w:
            fn(w)
        return s.frames[start:]

    if t == "welcome":
        return [{"control": "welcome", "seq": s.seq, "sessionId": msg["session_id"], "contract": "2.0", "agui": "1.0",
                 "a2ui": None, "mcpApps": None, "rings": [0], "extensions": {}, "languages": msg["languages"]}]
    if t == "pong":
        return [{"control": "pong", "seq": s.seq}]
    if t == "error":
        if msg["code"] in ("approval_expired", "approval_decided", "biometric_required", "model_not_configured",
                           "provider_key_missing", "provider_error", "budget_exceeded", "rate_limited", "internal",
                           "google_ai_tier_not_paid"):
            # A turn's failure: RUN_ERROR in the run it belongs to.
            start = len(s.frames)
            with s.run(convo) as w:
                w.error(msg["message"], msg["code"])
            return s.frames[start:]
        code = {"auth_failed": "auth_failed", "protocol_version_unsupported": "contract_unsupported",
                "invalid_message": "invalid_frame"}[msg["code"]]
        return [{"control": "error", "seq": s.seq, "code": code, "message": msg["message"], "retryable": msg["retryable"]}]
    if t == "say":
        meta = {"lang": msg["lang"], "priority": msg["priority"], "holding": msg.get("holding"), "urgency": msg.get("urgency")}
        return run_with(convo, lambda w: w.text([msg["delta"]], lang=meta["lang"], priority=meta["priority"],
                                                holding=bool(meta["holding"]), urgency=meta["urgency"]))
    if t == "say_audio":
        def f(w):
            v = {"seq": msg["seq"], "format": msg["format"], "sampleRate": msg["sample_rate"], "data": msg["data"], "final": msg["final"]}
            v["messageId" if "stream_id" in msg else "noticeId"] = msg.get("stream_id") or msg["notification_id"]
            for k1, k2 in (("sentence_end", "sentenceEnd"), ("complete", "complete"), ("spoken_chars", "spokenChars")):
                if k1 in msg:
                    v[k2] = msg[k1]
            w.custom("pisa.voice.audio", v)
        return run_with(convo, f)
    if t == "turn_hint":
        return run_with(convo, lambda w: w.custom("pisa.voice.turn_hint", {"replyTo": msg["reply_to"], "pEnd": msg["p_end"], "waitMs": msg["wait_ms"]}))
    if t == "understood":
        return run_with(convo, lambda w: w.custom("pisa.voice.understood", {
            "replyTo": msg.get("reply_to", "unknown"), "meant": msg["meant"], "raw": msg["raw"],
            "corrections": [{"from": c.get("from", c.get("heard", "")) or "-", "to": c.get("to", c.get("meant", "")) or "-"} for c in msg["corrections"]],
            "ask": msg["ask"]}))
    if t == "notify":
        v: dict[str, Any] = {"noticeId": msg["notification_id"], "kind": msg["kind"] if msg["kind"] not in ("coder_needs_you", "coder_done") else "info",
                             "priority": msg["priority"], "lang": msg["lang"], "text": msg["text"]}
        if msg.get("weekly"):
            v["kind"] = "weekly"
        if msg.get("ledger_week"):
            v["kind"] = "ledger_week"; v["refs"] = [f"ledger_week:{msg['ledger_week']}"]
        for k1, k2 in (("title", "title"), ("place", "place"), ("urgency", "urgency"), ("voice", "voice"), ("wrist", "wrist"), ("sensitive", "sensitive")):
            if k1 in msg:
                v[k2] = msg[k1]
        if msg.get("check_in_id"):
            v["checkIn"] = {"checkInId": msg["check_in_id"], "choices": msg["choices"], **({"expectsReply": msg["expects_reply"]} if "expects_reply" in msg else {})}
        if msg.get("undo_until"):
            target = f"task:{msg['task_id']}" if msg.get("task_id") else f"event:{msg.get('event_id')}"
            v["undo"] = {"undoId": target, "until": msg["undo_until"]}
        refs = v.get("refs", [])
        for k, prefix in (("task_id", "task:"), ("sheet_id", "sheet:"), ("job_id", "coder_job:")):
            if msg.get(k):
                refs.append(prefix + msg[k])
        if refs:
            v["refs"] = refs
        return run_with(notices, lambda w: w.custom("pisa.notice", v))
    if t == "talk.ask_to_open":
        return run_with(notices, lambda w: w.custom("pisa.ask_to_open", {k2: msg[k1] for k1, k2 in (("why", "why"), ("line", "line"), ("lang", "lang"), ("notification_id", "noticeId")) if k1 in msg}))
    if t == "directions":
        return run_with(convo, lambda w: w.custom("pisa.directions", {k2: msg[k1] for k1, k2 in (("directions_id", "directionsId"), ("name", "name"), ("lat", "lat"), ("lon", "lon"), ("mode", "mode"), ("place_id", "placeId")) if k1 in msg}))
    if t == "call":
        return run_with(convo, lambda w: w.custom("pisa.call", {"callId": msg["call_id"], "name": msg["name"], "phone": msg["phone"]}))
    if t == "compose":
        r = msg["recipient"]
        rec = {"name": r["name"] if isinstance(r, dict) else str(r)}
        if isinstance(r, dict) and r.get("phone"):
            rec["phone"] = r["phone"]
        return run_with(convo, lambda w: w.custom("pisa.compose", {"composeId": msg["compose_id"], "app": msg["app"], "recipient": rec, "text": msg["text"]}))
    if t == "action_done":
        v = {"approvalId": msg["approval_id"], "kind": msg["kind"], "summary": msg["summary"], "result": msg["result"]}
        if msg.get("undo_until"):
            v["undoUntil"] = msg["undo_until"]
        return run_with(f"pisa-approval:{msg['approval_id']}", lambda w: w.custom("pisa.action_done", v))
    if t == "approval_request":
        start = len(s.frames)
        kind = msg.get("kind", "email_send")
        dg = digest(kind, {"synthetic": True, "approval_id": msg["approval_id"]}, [])
        with s.run(f"pisa-approval:{msg['approval_id']}") as w:
            w.interrupt([interrupt_for(interrupt_id=f"int-{msg['approval_id']}", approval_id=msg["approval_id"], kind=kind,
                                       requires_biometric=msg["requires_biometric"], digest_hex=dg, recipients_count=0,
                                       undo_minutes=msg.get("undo_minutes"), expires_at=msg["expires_at"], server=SERVER_ID,
                                       tool_call_id=None, lang="it-IT")])
        return s.frames[start:]
    if t == "card":
        op, fb = card_to_surface(msg, f"card:{msg['card_id']}")
        return run_with(convo, lambda w: w.surface(f"card:{msg['card_id']}", [op], {"focus": "offer", "fallbackText": fb or msg["title"]}))
    if t == "present":
        obj = msg["object"]
        sid = obj["object_id"]
        if obj["kind"] == "drawing":
            content = mcp_app_from_drawing(obj["data"], msg["focus"])
            if msg.get("expires_at"):
                content["pisa"]["expiresAt"] = msg["expires_at"]  # G6
            return run_with(convo, lambda w: w.mcp_app(sid, content))
        op, fb = object_to_surface(obj, sid)  # type: ignore[misc]
        pisa = {"purpose": msg["purpose"], "focus": msg["focus"], "fallbackText": fb or msg["purpose"], "lang": msg["lang"]}
        if msg.get("reply_to"):
            pisa["replyTo"] = msg["reply_to"]
        if msg.get("expires_at"):
            pisa["expiresAt"] = msg["expires_at"]  # G6
        return run_with(convo, lambda w: w.surface(sid, [op], pisa))
    if t == "focus":
        return run_with(convo, lambda w: w.custom("pisa.surface.focus", {"surfaceId": msg["object_id"], "mode": "take", **({"highlight": msg["highlight"]} if msg.get("highlight") else {})}))
    if t == "close":
        return run_with(convo, lambda w: w.surface(msg["object_id"], [{"version": "1.0", "deleteSurface": {"surfaceId": msg["object_id"]}}], {"focus": "quiet", "fallbackText": "-"}))
    raise KeyError(t)


def map_client(msg: dict[str, Any]) -> list[dict[str, Any]]:
    t = msg["type"]
    if t == "hello":
        caps = msg["capabilities"]
        hello: dict[str, Any] = {"op": "hello", "contract": {"versions": ["2.0"]}, "agui": {"protocolVersions": ["1.0"]},
                                 "a2ui": {"1.0": {"supportedCatalogIds": ["urn:pisa:a2ui:catalog:core:1"]}},
                                 "mcpApps": {"protocolVersions": ["2026-01-26"], "mimeTypes": ["text/html;profile=mcp-app"],
                                             "bundledLibraries": ["d3@7.9.0", "mermaid@12.2.0", "vega@6.4.0", "vega-lite@6.5.0", "vega-embed@7.3.0", "vega-interpreter@2.3.2"]},
                                 "rings": [0, 1, 2] if caps.get("display") else [0],
                                 "extensions": {"pisa.voice": 1, "pisa.presence": 1, "pisa.location": 1, "pisa.notice": 1, "pisa.directions": 1,
                                                "pisa.call": 1, "pisa.compose": 1, "pisa.approval": 1, "pisa.surface": 1},
                                 "device": {"deviceId": msg["device_id"], "appVersion": msg["app_version"], "platform": msg["platform"],
                                            "surfaces": ["phone"] + (["glasses_display"] if msg.get("wearable") else []),
                                            "audioFormats": caps.get("audio_formats") or [], "wearable": msg.get("wearable")}}
        if msg.get("turn_hints"):
            hello["extensions"]["pisa.voice.turn_hints"] = 1
        for k1, k2 in (("tz", "tz"), ("region", "region"), ("hour_cycle", "hourCycle")):
            if k1 in msg:
                hello["device"][k2] = msg[k1]
        return [hello]
    if t == "utterance":
        if not msg["final"]:
            return [{"op": "custom", "name": "pisa.voice.partial", "value": {"text": msg["text"], "lang": msg["lang"], "turnId": msg["id"]}}]
        wake = {"hey_j": "wake_phrase", "hey_pisa": "wake_phrase"}.get(msg.get("wake", "tap"), msg.get("wake", "tap"))
        turn = {"source": msg["source"], "wake": wake, "lang": msg["lang"], "origin": "phone"}
        if msg.get("continues"):
            turn["continues"] = msg["continues"]
        if msg.get("voice"):
            turn["voice"] = msg["voice"]
        return [{"op": "run", "input": {"threadId": "thread-conversation", "runId": f"run-{msg['id']}",
                                        "messages": [{"id": msg["id"], "role": "user", "content": msg["text"]}],
                                        "forwardedProps": {"pisa": {"turn": turn}}}}]
    if t == "approval_response":
        payload = {"decision": msg["decision"], "auth": msg["auth"], "digest": "0" * 64}
        if msg["auth"] == "biometric":
            payload.update({"nonce": "n", "signature": "c2ln", "keyId": "k"})
        return [{"op": "run", "input": {"threadId": f"pisa-approval:{msg['approval_id']}", "runId": f"run-{msg['id']}", "messages": [],
                                        "resume": [{"interruptId": f"int-{msg['approval_id']}", "status": "resolved", "payload": payload}]}}]
    if t == "cancel":
        return [{"op": "cancel", "runId": f"run-{msg['target_id']}"}]
    if t == "ping":
        return [{"op": "ping"}]
    if t == "presence":
        v = {"event": msg["event"]}
        if msg.get("state") is not None:
            v["state"] = _camel_presence_state(msg["state"])
        return [{"op": "custom", "name": "pisa.presence", "value": v}]
    if t == "location":
        return [{"op": "custom", "name": "pisa.location", "value": {"lat": msg["lat"], "lon": msg["lon"], "accuracyM": msg["accuracy_m"], "measuredAt": msg["measured_at"]}}]
    if t == "place_enter":
        return [{"op": "custom", "name": "pisa.place_enter", "value": {"noteIds": msg["note_ids"]}}]
    if t == "device_context":
        return [{"op": "custom", "name": "pisa.device_context", "value": {k2: msg[k1] for k1, k2 in (("tz", "tz"), ("region", "region"), ("hour_cycle", "hourCycle")) if k1 in msg}}]
    if t == "presented":
        return [{"op": "custom", "name": "pisa.surface.ack", "value": {"surfaceId": msg["object_id"], "status": msg["status"], "surface": msg["surface"], **({"reason": msg["reason"]} if msg.get("reason") else {})}}]
    if t == "object_event":
        ev = msg["event"]
        if ev in ("seen", "expanded", "kept", "closed"):
            return [{"op": "custom", "name": "pisa.surface.event", "value": {"surfaceId": msg["object_id"], "event": ev}}]
        name, ctx = ("pisa.choose", {"optionId": msg["option_id"]}) if ev == "chosen" else ("pisa.act", {"action": msg["action"], "ref": msg["object_id"]})
        return [{"op": "a2ui", "message": {"version": "1.0", "action": {"name": name, "surfaceId": msg["object_id"], "sourceComponentId": "root",
                                                                       "timestamp": msg["ts"], "context": ctx}}}]
    if t == "drawing.ask":
        return [{"op": "run", "input": {"threadId": "thread-conversation", "runId": f"run-{msg['id']}",
                                        "messages": [{"id": msg["id"], "role": "user", "content": "Tell me more about this."}],
                                        "forwardedProps": {"pisa": {"ask": {"source": "mcp-apps", "surfaceId": f"ui://pisa/drawing/{msg['drawing_id']}", "label": msg["label"]}}}}}]
    if t == "handoff_opened":
        return [{"op": "a2ui", "message": {"version": "1.0", "action": {"name": "pisa.handoff_opened", "surfaceId": msg["object_id"], "sourceComponentId": msg["handoff_id"][:8],
                                                                       "timestamp": msg["ts"], "context": {"handoffId": msg["handoff_id"]}}}}]
    raise KeyError(t)


SERVER_TYPES = {"welcome", "pong", "error", "say", "say_audio", "turn_hint", "understood", "notify", "talk.ask_to_open", "directions",
                "call", "compose", "action_done", "approval_request", "card", "present", "focus", "close"}
CLIENT_TYPES = {"hello", "utterance", "approval_response", "cancel", "ping", "presence", "location", "place_enter", "device_context",
                "presented", "object_event", "drawing.ask", "handoff_opened"}


# ---------- the talk data channel (CONV-1 / VOICE-ID-1): the gaps G2, G4, G5 ----------
ENROL_SERVER = {"enrol.quality": "pisa.voice.enrol.quality", "enrol.test_result": "pisa.voice.enrol.test_result", "enrol.done": "pisa.voice.enrol.done"}
ENROL_CLIENT = {"enrol.block": "pisa.voice.enrol.block", "enrol.test": "pisa.voice.enrol.test", "enrol.finish": "pisa.voice.enrol.finish",
                "enrol.cancel": "pisa.voice.enrol.cancel"}
_CAMEL = {"voiced_s": "voicedS", "snr_db": "snrDb", "accepted_owner": "acceptedOwner", "owner_total": "ownerTotal",
          "accepted_strangers": "acceptedStrangers", "strangers_total": "strangersTotal", "by_snr": "bySnr",
          "owner_false_reject": "ownerFalseReject", "strangers_per_min": "strangersPerMin"}


def map_talk_route(msg: dict[str, Any], *, foreground: bool = True) -> dict[str, Any]:
    """G2. v1 talk.route -> pisa.presence. As in v1 presence, a route change re-sends the current
    app_foreground/app_background event with the new state."""
    return {"op": "custom", "name": "pisa.presence",
            "value": {"event": "app_foreground" if foreground else "app_background", "state": {"foreground": foreground, "route": msg["route"]}}}


def talk_ended_result(msg: dict[str, Any]) -> dict[str, Any]:
    """G4. v1 talk.ended {reason} -> the RUN_FINISHED.result of the talk's last run."""
    return {"talkEnded": msg["reason"]}


def map_talk_gate(msg: dict[str, Any]) -> dict[str, Any]:
    """G5. v1 talk.gate {heard} -> the value of a pisa.voice.gate CUSTOM event (inside the talk's run)."""
    return {"heard": msg["heard"]}


def map_enrol_client(msg: dict[str, Any]) -> dict[str, Any]:
    """G5. v1 enrol.* (app -> server) -> an EnrolClientFrame."""
    return {"op": "custom", "name": ENROL_CLIENT[msg["type"]], "value": {k: v for k, v in msg.items() if k != "type"}}


def map_enrol_server(msg: dict[str, Any], seq: int) -> dict[str, Any]:
    """G5. v1 enrol.* (server -> app) -> an EnrolServerFrame (no thread, no run)."""
    value = {_CAMEL.get(k, k): v for k, v in msg.items() if k != "type"}
    if msg["type"] == "enrol.done" and msg.get("ok") is False and "reason" not in msg:
        value["reason"] = "not_reliable"  # v1: prints made and stored, but the route's gate cannot rely on them
    return {"seq": seq, "event": {"type": "CUSTOM", "name": ENROL_SERVER[msg["type"]], "value": value}}


def map_talk_transcript(msg: dict[str, Any]) -> dict[str, Any]:
    """v1 talk.transcript (or talk.ignored) -> the value of a pisa.voice.transcript CUSTOM event."""
    v: dict[str, Any] = {"text": msg.get("text", ""), "final": msg.get("final", True)}
    if msg.get("ignored"):
        v["ignored"] = msg["ignored"]
    return v
