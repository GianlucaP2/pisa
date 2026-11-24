"""AG-UI 1.0 events, framed for the `/v2/live` WebSocket (CONTRACT-2 §3).

Every server frame is `{"seq", "threadId", "runId"?, "event"}`, where `event` is a plain AG-UI 1.0 event that
validates against the official schema on its own, or a control frame `{"control": "welcome" | "pong" |
"error", "seq", ...}`. The socket carries one AG-UI stream per thread, and each obeys the AG-UI lifecycle on
its own: RUN_STARTED, then the run's events, then RUN_FINISHED or RUN_ERROR; one run at a time per thread.

`LiveSession` is one device's session: it numbers frames (`seq` grows by one per frame, control frames
included), keeps the last frames for a resume, and outlives a socket. `RunWriter` writes one run in order and
keeps its text messages, tool calls and steps balanced: closing a run closes what it left open.

Ported from the conformance kit's reference emitter (`pisa_conformance/reference/agui.py`). Everything here is
synchronous: a frame is numbered, kept and handed to the socket's writer in one step, so frames never
interleave out of order.
"""

import json
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from pisa.domain import protocol_versions as versions

PROTOCOL_VERSION = versions.AGUI
JSON_WAIT_S = 8.0  # the HTTP binding's JSON mode (a run, a resume): collected within this, else 202
REPLAY_FRAMES = 512  # the frames a session keeps for a resume ...
REPLAY_BYTES = 8_000_000  # ... and their size at most (drawings and audio are large)
# each `pisa.*` event a server sends → the extension (the schema's `x-pisa-extensions`) the welcome must have
# granted for it to be sent: what was not picked is never sent (CONTRACT-2 §4.2). `pisa.ai.disclosure`,
# `pisa.endpoints` and `pisa.entitlement.required` belong to none: Ring 0, always sent.
EXTENSION_OF: dict[str, str] = {
    "pisa.voice.audio": "pisa.voice",
    "pisa.voice.state": "pisa.voice",
    "pisa.voice.gate": "pisa.voice",
    "pisa.voice.transcript": "pisa.voice",
    "pisa.voice.understood": "pisa.voice",
    "pisa.ask_to_open": "pisa.voice",  # it asks to open a voice talk
    "pisa.voice.turn_hint": "pisa.voice.turn_hints",
    "pisa.notice": "pisa.notice",
    "pisa.directions": "pisa.directions",
    "pisa.call": "pisa.call",
    "pisa.compose": "pisa.compose",
    "pisa.action_done": "pisa.approval",
    "pisa.approval.state": "pisa.approval",
    "pisa.surface.focus": "pisa.surface",
    "pisa.app.open": "pisa.app",  # contract 2.2
    "pisa.app.changed": "pisa.app",
}


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def new_run_id() -> str:
    return f"run-{uuid.uuid4()}"


def now_ms() -> int:
    return int(time.time() * 1000)


class LiveSession:
    """One `/v2/live` session as the server sees it. It survives a dropped socket: a `hello` that resumes
    it with the last `seq` the app saw gets every later frame once (`replay_after`)."""

    def __init__(self, device_id: str, *, clock_ms: Callable[[], int] = now_ms) -> None:
        self.device_id = device_id
        self.session_id = f"sess-{uuid.uuid4()}"
        self.clock_ms = clock_ms
        self.seq = 0
        self._kept: deque[tuple[int, str]] = deque()  # (seq, the frame as sent)
        self._kept_bytes = 0
        self._sink: Callable[[str], None] | None = None
        self.runs: dict[str, RunWriter] = {}  # thread → its open run
        # the extensions the latest welcome granted; None when no hello negotiated any (a talk's channel, the
        # HTTP binding): every event goes
        self.extensions: frozenset[str] | None = None

    # --- the socket ---------------------------------------------------------------------------------
    def attach(self, sink: Callable[[str], None]) -> None:
        """Frames go to `sink` (the socket's writer) from now on."""
        self._sink = sink

    def detach(self, sink: Callable[[str], None] | None = None) -> None:
        """The socket is gone: frames are only kept (for a resume); with `sink`, only if it is that one."""
        if sink is None or self._sink is sink:
            self._sink = None

    @property
    def attached(self) -> bool:
        return self._sink is not None

    def grants(self, name: str) -> bool:
        """Whether the `pisa.*` event `name` may be sent: its extension was granted, or it belongs to none."""
        extension = EXTENSION_OF.get(name)
        return self.extensions is None or extension is None or extension in self.extensions

    # --- frames -------------------------------------------------------------------------------------
    def _out(self, frame: dict[str, Any], *, keep: bool = True) -> dict[str, Any]:
        text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if keep:
            self._kept.append((frame["seq"], text))
            self._kept_bytes += len(text)
            while self._kept and (len(self._kept) > REPLAY_FRAMES or self._kept_bytes > REPLAY_BYTES):
                self._kept_bytes -= len(self._kept.popleft()[1])
        if self._sink is not None:
            self._sink(text)
        return frame

    def welcome(self, body: dict[str, Any], *, seq: int | None = None) -> dict[str, Any]:
        """The welcome. Its `seq` is the last one the app has (the frames after it follow); it takes no
        number of its own and is never replayed."""
        return self._out({"control": "welcome", "seq": self.seq if seq is None else seq, **body}, keep=False)

    def control(self, kind: str, **fields: Any) -> dict[str, Any]:
        """A `pong` or an `error`: a frame of the session like any other."""
        self.seq += 1
        return self._out({"control": kind, "seq": self.seq, **fields})

    def emit(self, thread_id: str, run_id: str | None, event: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        return self._out(_clean({"seq": self.seq, "threadId": thread_id, "runId": run_id, "event": event}))

    def replay_after(self, last_seq: int) -> list[str] | None:
        """Every kept frame after `last_seq`, in order; None when some of them are no longer kept (or the
        app claims a frame this session never sent)."""
        if last_seq > self.seq or last_seq < 0:
            return None
        if last_seq == self.seq:
            return []
        if not self._kept or self._kept[0][0] > last_seq + 1:
            return None
        return [text for seq, text in self._kept if seq > last_seq]

    def kept(self) -> list[dict[str, Any]]:
        """The kept frames, decoded (tests and diagnostics)."""
        return [json.loads(text) for _, text in self._kept]

    # --- runs ---------------------------------------------------------------------------------------
    def start_run(
        self,
        thread_id: str,
        run_id: str | None = None,
        *,
        parent_run_id: str | None = None,
        input_: dict[str, Any] | None = None,
    ) -> "RunWriter":
        """Open a run on a thread that has none open (AG-UI: a thread's runs are sequential)."""
        if thread_id in self.runs:
            raise RuntimeError(f"a run is already open on {thread_id}")
        writer = RunWriter(self, thread_id, run_id or new_run_id(), parent_run_id, input_)
        self.runs[thread_id] = writer
        writer._start()
        return writer

    def open_run(self, thread_id: str) -> "RunWriter | None":
        return self.runs.get(thread_id)

    def notice_thread(self) -> str:
        return f"pisa-notices:{self.device_id}"


class RunWriter:
    """Writes one AG-UI run in order: RUN_STARTED ... RUN_FINISHED (or RUN_ERROR)."""

    def __init__(
        self,
        session: LiveSession,
        thread_id: str,
        run_id: str,
        parent_run_id: str | None,
        input_: dict[str, Any] | None,
    ) -> None:
        self.s, self.thread_id, self.run_id = session, thread_id, run_id
        self.parent_run_id, self.input = parent_run_id, input_
        self.closed = False
        self._messages: set[str] = set()
        self._tools: set[str] = set()
        self._steps: list[str] = []

    def _ev(self, type_: str, **fields: Any) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("run already closed")
        event = _clean({"type": type_, "timestamp": self.s.clock_ms(), **fields})
        self.s.emit(self.thread_id, self.run_id, event)
        return event

    def _start(self) -> None:
        self._ev(
            "RUN_STARTED",
            threadId=self.thread_id,
            runId=self.run_id,
            protocolVersion=PROTOCOL_VERSION,
            parentRunId=self.parent_run_id,
            input=self.input,
        )

    # --- text -----------------------------------------------------------------------------------------
    def text_start(
        self,
        message_id: str | None = None,
        *,
        lang: str,
        priority: str = "normal",
        holding: bool = False,
        urgency: str | None = None,
        role: str = "assistant",
    ) -> str:
        """Open a message of Pisa's words; its per-reply fields ride `metadata.pisa` (CONTRACT-2 §5.1)."""
        message_id = message_id or f"msg-{uuid.uuid4()}"
        meta = {
            "pisa": _clean(
                {"lang": lang, "priority": priority, "holding": holding or None, "urgency": urgency}
            )
        }
        self._ev("TEXT_MESSAGE_START", messageId=message_id, role=role, metadata=meta)
        self._messages.add(message_id)
        return message_id

    def text_content(self, message_id: str, delta: str) -> None:
        if delta:
            self._ev("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=delta)

    def text_end(self, message_id: str) -> None:
        if message_id in self._messages:
            self._messages.discard(message_id)
            self._ev("TEXT_MESSAGE_END", messageId=message_id)

    def text(self, chunks: list[str], *, lang: str, message_id: str | None = None, **meta: Any) -> str:
        """A whole message at once."""
        message_id = self.text_start(message_id, lang=lang, **meta)
        for chunk in chunks:
            self.text_content(message_id, chunk)
        self.text_end(message_id)
        return message_id

    def message_open(self, message_id: str) -> bool:
        return message_id in self._messages

    # --- tools and steps ------------------------------------------------------------------------------
    def tool_call(
        self, call_id: str, name: str, args: list[str] | None = None, parent_message_id: str | None = None
    ) -> None:
        """The server's own tool call, opened and closed (its arguments' JSON chunks only when given)."""
        self._ev("TOOL_CALL_START", toolCallId=call_id, toolCallName=name, parentMessageId=parent_message_id)
        self._tools.add(call_id)
        for chunk in args or []:
            self._ev("TOOL_CALL_ARGS", toolCallId=call_id, delta=chunk)
        self._tools.discard(call_id)
        self._ev("TOOL_CALL_END", toolCallId=call_id)

    def tool_result(self, call_id: str, content: str, message_id: str | None = None) -> None:
        self._ev(
            "TOOL_CALL_RESULT",
            messageId=message_id or f"msg-{uuid.uuid4()}",
            toolCallId=call_id,
            content=content,
            role="tool",
        )

    def step_started(self, name: str) -> None:
        self._steps.append(name)
        self._ev("STEP_STARTED", stepName=name)

    def step_finished(self, name: str) -> None:
        if name in self._steps:
            self._steps.remove(name)
            self._ev("STEP_FINISHED", stepName=name)

    # --- state ----------------------------------------------------------------------------------------
    def state_snapshot(self, snapshot: Any) -> None:
        self._ev("STATE_SNAPSHOT", snapshot=snapshot)

    def state_delta(self, patch: list[dict[str, Any]]) -> None:
        self._ev("STATE_DELTA", delta=patch)

    # --- UI: Ring 1 and Ring 2 ----------------------------------------------------------------------
    def surface(self, surface_id: str, content: dict[str, Any], replace: bool | None = None) -> None:
        """An A2UI surface (`$defs/A2UISurfaceActivity`: `a2ui_operations` and `pisa`)."""
        self._ev(
            "ACTIVITY_SNAPSHOT",
            messageId=surface_id,
            activityType="a2ui-surface",
            content=content,
            replace=replace,
        )

    def surface_patch(self, surface_id: str, patch: list[dict[str, Any]]) -> None:
        self._ev("ACTIVITY_DELTA", messageId=surface_id, activityType="a2ui-surface", patch=patch)

    def mcp_app(self, view_id: str, content: dict[str, Any]) -> None:
        self._ev("ACTIVITY_SNAPSHOT", messageId=view_id, activityType="mcp-apps", content=content)

    # --- extensions -----------------------------------------------------------------------------------
    def custom(self, name: str, value: dict[str, Any]) -> bool:
        """A `pisa.*` event; False, and nothing written, when the welcome did not grant its extension: every
        `pisa.*` event goes through here, so none passes the negotiation."""
        if not name.startswith("pisa."):
            raise ValueError("PISA only sends pisa.* custom events")
        if not self.s.grants(name):
            return False
        self._ev("CUSTOM", name=name, value=value)
        return True

    # --- closing --------------------------------------------------------------------------------------
    def _close_open(self) -> None:
        for message_id in sorted(self._messages):
            self.text_end(message_id)
        for name in list(reversed(self._steps)):
            self.step_finished(name)

    def _closed(self) -> None:
        self.closed = True
        if self.s.runs.get(self.thread_id) is self:
            del self.s.runs[self.thread_id]

    def finish(self, outcome: dict[str, Any] | None = None, result: Any = None) -> None:
        if self.closed:
            return
        self._close_open()
        self._ev(
            "RUN_FINISHED",
            threadId=self.thread_id,
            runId=self.run_id,
            result=result,
            outcome=outcome or {"type": "success"},
        )
        self._closed()

    def interrupt(self, interrupts: list[dict[str, Any]]) -> None:
        self.finish({"type": "interrupt", "interrupts": interrupts})

    def cancelled(self) -> None:
        self.finish({"type": "cancelled"})

    def error(self, message: str, code: str) -> None:
        if self.closed:
            return
        self._close_open()
        self._ev("RUN_ERROR", message=message or code, code=code)
        self._closed()
