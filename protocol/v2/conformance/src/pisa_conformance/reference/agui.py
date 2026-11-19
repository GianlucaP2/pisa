"""AG-UI 1.0 events, framed for PISA's /v2/live WebSocket (CONTRACT-2 §3).

Every server frame is {"seq", "threadId", "runId"?, "event"}; "event" is a plain AG-UI 1.0 event and validates
against the official schema on its own. One AG-UI stream per thread; each obeys the AG-UI lifecycle.
"""
from __future__ import annotations

import itertools
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class Clock:
    """Deterministic milliseconds clock for fixtures."""

    now_ms: int = 1_763_460_000_000
    step_ms: int = 40

    def tick(self) -> int:
        self.now_ms += self.step_ms
        return self.now_ms


@dataclass
class Ids:
    """Deterministic UUIDs for fixtures (uuid5 over a counter)."""

    namespace: uuid.UUID = uuid.UUID("00000000-0000-4000-8000-0000000c0002")
    counter: itertools.count = field(default_factory=itertools.count)

    def new(self, prefix: str = "") -> str:
        value = str(uuid.uuid5(self.namespace, str(next(self.counter))))
        return f"{prefix}{value}" if prefix else value


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class LiveSession:
    """One /v2/live socket session as the server sees it: numbers frames, keeps them for replay."""

    def __init__(self, device_id: str, clock: Clock | None = None, ids: Ids | None = None) -> None:
        self.device_id = device_id
        self.clock = clock or Clock()
        self.ids = ids or Ids()
        self.session_id = self.ids.new("sess-")
        self.seq = 0
        self.frames: list[dict[str, Any]] = []

    # --- control frames -------------------------------------------------------------------------------
    def welcome(self, hello: dict[str, Any], server_catalogs: list[str], features: list[dict[str, Any]]) -> dict[str, Any]:
        """Pick from what the app offered (CONTRACT-2 §4). Never picks something the app did not list."""
        if "2.0" not in hello["contract"]["versions"]:
            return {"control": "error", "seq": self.seq, "code": "contract_unsupported",
                    "message": "This server speaks contract 2.0; update the app.", "retryable": False}
        offered = hello.get("a2ui", {}).get("1.0", {}).get("supportedCatalogIds", [])
        catalogs = [c for c in server_catalogs if c in offered]
        rings = [r for r in hello["rings"] if r in (0, 1, 2)]
        if not catalogs:
            rings = [r for r in rings if r != 1]
        mcp = hello.get("mcpApps")
        mcp_pick = {"protocolVersion": "2026-01-26"} if (2 in rings and mcp and "2026-01-26" in mcp["protocolVersions"]) else None
        if mcp_pick is None:
            rings = [r for r in rings if r != 2]
        server_ext = {"pisa.voice": 1, "pisa.voice.turn_hints": 1, "pisa.presence": 1, "pisa.location": 1, "pisa.notice": 1,
                      "pisa.directions": 1, "pisa.call": 1, "pisa.compose": 1, "pisa.approval": 1, "pisa.surface": 1}
        ext = {k: min(v, server_ext[k]) for k, v in hello.get("extensions", {}).items() if k in server_ext}
        return {"control": "welcome", "seq": self.seq, "sessionId": self.session_id, "contract": "2.0", "agui": "1.0",
                "a2ui": {"version": "1.0", "catalogIds": catalogs} if catalogs else None,
                "mcpApps": mcp_pick, "rings": rings, "extensions": ext, "features": features,
                "languages": ["it-IT", "en-US"], "replayed": False}

    # --- event frames ---------------------------------------------------------------------------------
    def emit(self, thread_id: str, run_id: str | None, event: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        frame = _clean({"seq": self.seq, "threadId": thread_id, "runId": run_id, "event": event})
        self.frames.append(frame)
        return frame

    def replay_after(self, last_seq: int) -> list[dict[str, Any]]:
        return [f for f in self.frames if f["seq"] > last_seq]

    @contextmanager
    def run(self, thread_id: str, run_id: str | None = None, parent_run_id: str | None = None,
            input_: dict[str, Any] | None = None) -> Iterator["RunWriter"]:
        writer = RunWriter(self, thread_id, run_id or self.ids.new("run-"), parent_run_id, input_)
        writer._start()
        yield writer
        if not writer.closed:
            writer.finish()

    def notice_thread(self) -> str:
        return f"pisa-notices:{self.device_id}"


class RunWriter:
    """Writes one AG-UI run in order: RUN_STARTED ... RUN_FINISHED (or RUN_ERROR)."""

    def __init__(self, session: LiveSession, thread_id: str, run_id: str, parent_run_id: str | None,
                 input_: dict[str, Any] | None) -> None:
        self.s, self.thread_id, self.run_id = session, thread_id, run_id
        self.parent_run_id, self.input = parent_run_id, input_
        self.closed = False
        self._open_messages: set[str] = set()

    def _ev(self, type_: str, **fields: Any) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("run already closed")
        event = _clean({"type": type_, "timestamp": self.s.clock.tick(), **fields})
        self.s.emit(self.thread_id, self.run_id, event)
        return event

    def _start(self) -> None:
        self._ev("RUN_STARTED", threadId=self.thread_id, runId=self.run_id, protocolVersion="1.0",
                 parentRunId=self.parent_run_id, input=self.input)

    # --- text -----------------------------------------------------------------------------------------
    def text(self, chunks: list[str], *, lang: str, priority: str = "normal", holding: bool = False,
             urgency: str | None = None, role: str = "assistant") -> str:
        """A spoken/written reply. v1 say fields ride in metadata.pisa (AG-UI BaseEvent.metadata)."""
        message_id = self.s.ids.new("msg-")
        meta = {"pisa": _clean({"lang": lang, "priority": priority, "holding": holding or None, "urgency": urgency})}
        self._ev("TEXT_MESSAGE_START", messageId=message_id, role=role, metadata=meta)
        for chunk in chunks:
            if chunk:
                self._ev("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=chunk)
        self._ev("TEXT_MESSAGE_END", messageId=message_id)
        return message_id

    def audio(self, message_id: str, chunks: list[str], fmt: str = "aac_adts", rate: int = 24000) -> None:
        for i, data in enumerate(chunks):
            value = {"messageId": message_id, "seq": i, "format": fmt, "sampleRate": rate, "data": data,
                     "final": i == len(chunks) - 1}
            if i == len(chunks) - 1:
                value["complete"] = True
            self.custom("pisa.voice.audio", value)

    # --- tools ----------------------------------------------------------------------------------------
    def tool(self, name: str, args_json_chunks: list[str], result: str, parent_message_id: str | None = None) -> str:
        call_id = self.s.ids.new("call-")
        self._ev("TOOL_CALL_START", toolCallId=call_id, toolCallName=name, parentMessageId=parent_message_id)
        for chunk in args_json_chunks:
            self._ev("TOOL_CALL_ARGS", toolCallId=call_id, delta=chunk)
        self._ev("TOOL_CALL_END", toolCallId=call_id)
        self._ev("TOOL_CALL_RESULT", messageId=self.s.ids.new("msg-"), toolCallId=call_id, content=result, role="tool")
        return call_id

    def step(self, name: str) -> "_Step":
        return _Step(self, name)

    # --- state ----------------------------------------------------------------------------------------
    def state_snapshot(self, snapshot: Any) -> None:
        self._ev("STATE_SNAPSHOT", snapshot=snapshot)

    def state_delta(self, patch: list[dict[str, Any]]) -> None:
        self._ev("STATE_DELTA", delta=patch)

    # --- UI: Ring 1 and Ring 2 ----------------------------------------------------------------------
    def surface(self, surface_id: str, operations: list[dict[str, Any]], pisa: dict[str, Any], replace: bool | None = None) -> None:
        self._ev("ACTIVITY_SNAPSHOT", messageId=surface_id, activityType="a2ui-surface",
                 content={"a2ui_operations": operations, "pisa": pisa}, replace=replace)

    def surface_patch(self, surface_id: str, patch: list[dict[str, Any]]) -> None:
        self._ev("ACTIVITY_DELTA", messageId=surface_id, activityType="a2ui-surface", patch=patch)

    def mcp_app(self, view_id: str, content: dict[str, Any]) -> None:
        self._ev("ACTIVITY_SNAPSHOT", messageId=view_id, activityType="mcp-apps", content=content)

    # --- extensions -----------------------------------------------------------------------------------
    def custom(self, name: str, value: dict[str, Any]) -> None:
        if not name.startswith("pisa."):
            raise ValueError("PISA only sends pisa.* custom events")
        self._ev("CUSTOM", name=name, value=value)

    # --- closing --------------------------------------------------------------------------------------
    def finish(self, outcome: dict[str, Any] | None = None, result: Any = None) -> None:
        self._ev("RUN_FINISHED", threadId=self.thread_id, runId=self.run_id, result=result,
                 outcome=outcome or {"type": "success"})
        self.closed = True

    def interrupt(self, interrupts: list[dict[str, Any]]) -> None:
        self.finish({"type": "interrupt", "interrupts": interrupts})

    def cancelled(self) -> None:
        self.finish({"type": "cancelled"})

    def error(self, message: str, code: str) -> None:
        self._ev("RUN_ERROR", message=message, code=code)
        self.closed = True


class _Step:
    def __init__(self, w: RunWriter, name: str) -> None:
        self.w, self.name = w, name

    def __enter__(self) -> None:
        self.w._ev("STEP_STARTED", stepName=self.name)

    def __exit__(self, *exc: object) -> None:
        self.w._ev("STEP_FINISHED", stepName=self.name)
