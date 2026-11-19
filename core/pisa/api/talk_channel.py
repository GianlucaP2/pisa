"""The talk's data channel in contract 2 (CONTRACT-2 §3.3).

The phone opens the talk's "pisa" data channel with the subprotocol `pisa.v2` and gets contract 2 on it; a
channel opened without it is refused (closed). `TalkRun` and the voice enrolment send their own messages
(`talk.*`, `enrol.*`, internal to the talk) and read the phone's, and the dialect here writes them as
contract 2 (`talk_media.ChannelDialect`).

A talk's channel (`TalkChannel`) carries the same frames as `/v2/live`, with their own `seq` from 1: AG-UI
1.0 events framed `{"seq", "threadId", "runId", "event"}` on the app's conversation thread, and control frames
(`pong`, `error`). Its runs:
- a run opens with the first event and stays open while the user and Pisa talk;
- the user's words after Pisa answered start the next run (the previous one ends `success`), so each run is
  one exchange;
- a barge-in ends the run in progress `cancelled`: the phone's `op: cancel` for it (a touch), or the gate
  hearing the user over Pisa (`talk.playout_stop`);
- the talk's end is its last run's `RUN_FINISHED`, with `{"talkEnded": reason}` as its result (G4), and then
  the channel closes.

The talk's messages → contract 2, server to phone: `talk.state` → `pisa.voice.state`; `talk.transcript`
(with `ignored`) → `pisa.voice.transcript`; `talk.gate` → `pisa.voice.gate` (G5); `talk.said` →
`TEXT_MESSAGE_*`, with only what is new in each delta (a line that rewrites what was already said adds
nothing); `talk.present` → an `a2ui-surface` activity (a drawing: an `mcp-apps` view); `talk.dismiss` →
`deleteSurface`. Phone to server:
`op: cancel` → `talk.stop`; `op: a2ui` `pisa.choose` / `pisa.ask` → `talk.choose` / `talk.ask` (never a
yes or no on an approval: that is the approval sheet's); `pisa.presence` with `state.route` → `talk.route`.
`op: ping` gets a `pong`.

An enrolment's channel (`EnrolChannel`, a talk session opened with `purpose: "enrol"`) carries only
`pisa.voice.enrol.*` (G5): the server's frames are `{"seq", "event": CUSTOM}` with no thread and no run, the
app's `{"op": "custom", name, value}`. Fields keep the talk's meaning in camelCase.
"""

import json
import uuid
from collections.abc import Callable
from typing import Any

from pisa.domain import talk as talk_rules
from pisa.domain.protocol_versions import SUBPROTOCOL
from pisa.logging import get_logger
from pisa.services import a2ui
from pisa.services.agui import LiveSession, RunWriter, now_ms
from pisa.services.talk_media import ChannelDialect, Dialects

_log = get_logger("pisa.talk.channel")

STATES = frozenset({"listening", "thinking", "speaking", "idle"})
HEARD = frozenset({"owner", "other", "none"})
IGNORED = frozenset({"noise", "echo", "language", "fragment"})
TALK_ENDED = frozenset({"idle", "owner", "budget", "error", "fallback", "route"})
TRANSCRIPT_MAX = 4000
SURFACES_KEPT = 200

ENROL_OUT = {
    "enrol.quality": "pisa.voice.enrol.quality",
    "enrol.test_result": "pisa.voice.enrol.test_result",
    "enrol.done": "pisa.voice.enrol.done",
}
ENROL_IN = {
    "pisa.voice.enrol.block": "enrol.block",
    "pisa.voice.enrol.test": "enrol.test",
    "pisa.voice.enrol.finish": "enrol.finish",
    "pisa.voice.enrol.cancel": "enrol.cancel",
}
_CAMEL = {
    "voiced_s": "voicedS",
    "snr_db": "snrDb",
    "accepted_owner": "acceptedOwner",
    "owner_total": "ownerTotal",
    "accepted_strangers": "acceptedStrangers",
    "strangers_total": "strangersTotal",
    "by_snr": "bySnr",
    "owner_false_reject": "ownerFalseReject",
    "strangers_per_min": "strangersPerMin",
}
BY_SNR = ("clean", "10", "5", "0")
# enrol.done with ok false and no reason: the prints were made, but this route's gate can't rely on them
NOT_RELIABLE = "not_reliable"


def _control_error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message[:500] or code, "retryable": False}


class TalkChannel:
    """One talk's data channel in contract 2: the talk's messages out as AG-UI runs on the app's
    conversation thread, the app's `op` frames in as the talk's messages."""

    def __init__(self, device_id: str, thread: str, lang: str) -> None:
        self.live = LiveSession(device_id)
        self.thread = thread
        self.lang = lang
        self._run: RunWriter | None = None
        self._replied = False  # the open run already carries Pisa's words: the user's next words start a run
        self._line: tuple[str, str] | None = None  # Pisa's line being said: (its message id, what was sent)
        self._ended = False
        self._surfaces: dict[str, str] = {}  # surface id → the show id of what it shows now
        self._surface_of_show: dict[str, str] = {}  # show id → its surface
        self._approvals: set[str] = set()  # surfaces showing an approval (never decided by a tap here)

    def attach(self, sink: Callable[[str], None]) -> None:
        self.live.attach(sink)

    # --- runs -------------------------------------------------------------------------------------------
    def _writer(self) -> RunWriter:
        if self._run is None or self._run.closed:
            self._run = self.live.start_run(self.thread)
            self._replied, self._line = False, None
        return self._run

    def _close(self, how: Callable[[RunWriter], object]) -> None:
        if self._run is not None and not self._run.closed:
            how(self._run)
        self._run, self._replied, self._line = None, False, None

    # --- the talk's messages out ------------------------------------------------------------------------
    def out(self, message: dict[str, Any]) -> None:
        if self._ended:
            return
        kind = message.get("type")
        try:
            self._out(kind, message)
        except Exception:  # a message the dialect can't write is lost, never the talk
            _log.exception("talk_channel_frame_failed", kind=str(kind)[:40])

    def _out(self, kind: Any, m: dict[str, Any]) -> None:
        if kind == "talk.state":
            if m.get("state") in STATES:
                self._writer().custom("pisa.voice.state", {"state": m["state"]})
        elif kind == "talk.gate":
            if m.get("heard") in HEARD:
                self._writer().custom("pisa.voice.gate", {"heard": m["heard"]})
        elif kind == "talk.transcript":
            self._transcript(m)
        elif kind == "talk.said":
            self._said(str(m.get("text") or ""), bool(m.get("final")))
        elif kind == "talk.present":
            self._present(m)
        elif kind == "talk.dismiss":
            self._dismiss(str(m.get("id") or ""))
        elif kind == "talk.playout_stop":
            self._close(RunWriter.cancelled)
        elif kind == "talk.ended":
            reason = m.get("reason") if m.get("reason") in TALK_ENDED else "error"
            self._writer()  # a talk that ends before anything was said still has its one run
            self._close(lambda w: w.finish(result={"talkEnded": reason}))
            self._ended = True
        else:
            _log.warning("talk_channel_unmapped", kind=str(kind)[:40])

    def _transcript(self, m: dict[str, Any]) -> None:
        text = a2ui.cut(m.get("text") or "", TRANSCRIPT_MAX)
        value: dict[str, Any] = {"text": text, "final": bool(m.get("final", True))}
        if m.get("ignored") in IGNORED:
            value["ignored"] = m["ignored"]
        elif text.strip() and self._replied and self._line is None:
            # the user's next words, once Pisa's line is over: the exchange before them is done
            self._close(RunWriter.finish)
        self._writer().custom("pisa.voice.transcript", value)

    def _said(self, text: str, final: bool) -> None:
        writer = self._writer()
        if self._line is None:
            if not text:
                return
            message_id = writer.text_start(f"msg-{uuid.uuid4()}", lang=self.lang)
            self._line = (message_id, "")
        message_id, sent = self._line
        if text.startswith(sent):  # only what is new: a rewrite of what was said adds nothing
            writer.text_content(message_id, text[len(sent) :])
            sent = text
        self._line = (message_id, sent)
        self._replied = True
        if final:
            writer.text_end(message_id)
            self._line = None

    def _present(self, m: dict[str, Any]) -> None:
        obj = m.get("object")
        if not isinstance(obj, dict):
            return
        show_id, object_id = str(m.get("id") or ""), str(obj.get("object_id") or "")
        focus = str(m["focus"]) if m.get("focus") in ("take", "offer", "quiet") else "offer"
        writer = self._writer()
        if obj.get("kind") == "drawing":
            content = a2ui.mcp_app_from_drawing(obj["data"], focus)
            surface_id = content["resourceUri"]
            writer.mcp_app(surface_id, content)
        else:
            surface_id = a2ui.surface_id_for(object_id)
            body = {"object": obj, "focus": focus, "purpose": m.get("purpose"), "lang": self.lang}
            content = a2ui.present_content(body, surface_id)
            if content is None:
                return
            writer.surface(surface_id, content)
        self._remember(surface_id, show_id, obj)

    def _remember(self, surface_id: str, show_id: str, obj: dict[str, Any]) -> None:
        self._surfaces.pop(surface_id, None)
        self._surfaces[surface_id] = show_id
        self._surface_of_show[show_id] = surface_id
        ref = obj.get("ref")
        if isinstance(ref, dict) and str(ref.get("ref", "")).startswith("approval:"):
            self._approvals.add(surface_id)
        else:
            self._approvals.discard(surface_id)
        while len(self._surfaces) > SURFACES_KEPT:
            gone = next(iter(self._surfaces))
            self._surface_of_show.pop(self._surfaces.pop(gone), None)
            self._approvals.discard(gone)

    def _dismiss(self, show_id: str) -> None:
        surface_id = self._surface_of_show.pop(show_id, None)
        if surface_id is None:
            return
        self._surfaces.pop(surface_id, None)
        self._approvals.discard(surface_id)
        content = {
            "a2ui_operations": [a2ui.delete_surface(surface_id)],
            "pisa": a2ui.surface_pisa("-", focus="quiet"),
        }
        self._writer().surface(surface_id, content)

    # --- the phone's frames in --------------------------------------------------------------------------
    def into(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        if self._ended:
            return []
        op = body.get("op")
        if op == "ping":
            self.live.control("pong")
            return []
        if op == "cancel" and isinstance(body.get("runId"), str):
            return self._cancel(body["runId"])
        if op == "a2ui" and isinstance(body.get("message"), dict):
            return self._a2ui(body["message"])
        if op == "custom" and isinstance(body.get("name"), str) and isinstance(body.get("value"), dict):
            return self._custom(body["name"], body["value"])
        if op in ("run", "hello"):
            return self._refuse("A talk's data channel takes no hello and no run: they go on /v2/live.")
        return self._refuse(f"The frame's op {str(op)[:40]!r} is unknown or its body is missing.")

    def _refuse(self, message: str) -> list[dict[str, Any]]:
        self.live.control("error", **_control_error("invalid_frame", message))
        return []

    def _cancel(self, run_id: str) -> list[dict[str, Any]]:
        """A barge-in by touch: the run in progress ends `cancelled` now, and Pisa stops. A run that already
        ended is not stopped again (its words are over)."""
        if self._run is None or self._run.closed or self._run.run_id != run_id:
            return []
        self._close(RunWriter.cancelled)
        return [{"type": "talk.stop"}]

    def _a2ui(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(message.get("error"), dict):
            err = message["error"]
            _log.warning(
                "talk_a2ui_error", code=str(err.get("code"))[:40], surface=str(err.get("surfaceId"))[:80]
            )
            return []
        action = message.get("action")
        if message.get("version") != "1.0" or not isinstance(action, dict):
            return self._refuse("An a2ui frame carries an A2UI 1.0 action or error.")
        name, context = action.get("name"), action.get("context") or {}
        if not isinstance(context, dict):
            return self._refuse("The action's context is an object.")
        surface_id = str(action.get("surfaceId") or "")
        show_id = self._surfaces.get(surface_id, surface_id)  # one shown before the talk: its object id
        if name == "pisa.choose":
            if surface_id in self._approvals:  # an approval is decided on the approval sheet, never here
                _log.info("talk_approval_tap_refused")
                return []
            return [{"type": "talk.choose", "id": show_id, "choice": str(context.get("optionId") or "")}]
        if name == "pisa.ask":
            return [{"type": "talk.ask", "id": show_id, "element_id": str(context.get("elementId") or "")}]
        return self._refuse(f"The action {str(name)[:40]!r} goes on /v2/live, not on a talk's channel.")

    def _custom(self, name: str, value: dict[str, Any]) -> list[dict[str, Any]]:
        if name == "pisa.presence":
            state = value.get("state")
            route = state.get("route") if isinstance(state, dict) else None
            return [{"type": "talk.route", "route": route}] if route in talk_rules.ROUTES else []
        if name in ("pisa.surface.ack", "pisa.surface.event"):
            return []  # what a talk shows needs no acknowledgment: the phone is in front
        return self._refuse(f"The event {name[:60]!r} is not one a talk's channel takes.")


class EnrolChannel:
    """One enrolment's data channel in contract 2 (G5): `pisa.voice.enrol.*` only, outside any AG-UI run."""

    def __init__(self) -> None:
        self.seq = 0
        self._sink: Callable[[str], None] | None = None

    def attach(self, sink: Callable[[str], None]) -> None:
        self._sink = sink

    def out(self, message: dict[str, Any]) -> None:
        frame = enrol_frame(message, self.seq + 1)
        if frame is None:
            _log.warning("enrol_channel_unmapped", kind=str(message.get("type"))[:40])
            return
        self.seq = frame["seq"]
        if self._sink is not None:
            self._sink(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))

    def into(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        message = enrol_message(body)
        if message is None:  # the enrolment channel has no control frames: a frame it can't read is dropped
            _log.info("enrol_channel_frame_dropped", op=str(body.get("op"))[:20])
            return []
        return [message]


def enrol_frame(message: dict[str, Any], seq: int) -> dict[str, Any] | None:
    """The enrolment's `enrol.*` (server → app) → its `$defs/EnrolServerFrame`; None for anything else."""
    name = ENROL_OUT.get(str(message.get("type")))
    if name is None:
        return None
    value = {_CAMEL.get(k, k): v for k, v in message.items() if k != "type"}
    if name == "pisa.voice.enrol.done" and value.get("ok") is False and not value.get("reason"):
        value["reason"] = NOT_RELIABLE
    if name == "pisa.voice.enrol.done" and value.get("reason") != "missing":
        value.pop("missing", None)
    if name == "pisa.voice.enrol.done" and value.get("ok") is True:
        value.pop("reason", None)
    if isinstance(value.get("bySnr"), dict):  # only the contract's four conditions
        by_snr = {k: v for k, v in value["bySnr"].items() if k in BY_SNR}
        value["bySnr"] = by_snr
        if len(by_snr) < len(BY_SNR) and name == "pisa.voice.enrol.done" and value.get("ok") is False:
            del value["bySnr"]
    if name == "pisa.voice.enrol.done" and value.get("ok") is False:
        _close_enrol_reason(value)
    return {"seq": seq, "event": {"type": "CUSTOM", "timestamp": now_ms(), "name": name, "value": value}}


# The contract's closed EnrolReason set. Any other cause (an error's code) is said as "error"; not_reliable
# carries the whole report it is about, else it too is "error".
ENROL_REASONS = frozenset({"missing", "consent", "no_speech", "timeout", NOT_RELIABLE, "error"})
_REPORT = ("ownerFalseReject", "strangersPerMin", "bySnr")


def _close_enrol_reason(value: dict[str, Any]) -> None:
    if value.get("reason") not in ENROL_REASONS:
        value["reason"] = "error"
    if value["reason"] == NOT_RELIABLE and not all(key in value for key in _REPORT):
        value["reason"] = "error"
    if value["reason"] != NOT_RELIABLE:
        for key in _REPORT:
            value.pop(key, None)


def enrol_message(frame: dict[str, Any]) -> dict[str, Any] | None:
    """A `$defs/EnrolClientFrame` → the `enrol.*` the enrolment reads; None for anything else."""
    kind = ENROL_IN.get(str(frame.get("name")))
    value = frame.get("value")
    if frame.get("op") != "custom" or kind is None or not isinstance(value, dict):
        return None
    return {"type": kind, **{k: v for k, v in value.items() if k in ("block", "state")}}


def talk_dialects(device_id: str, thread: str, lang: str) -> Dialects:
    """A talk's channel: contract 2 for `pisa.v2`; any other subprotocol (or none) is refused."""

    def choose(subprotocol: str) -> ChannelDialect | None:
        return TalkChannel(device_id, thread, lang) if subprotocol == SUBPROTOCOL else None

    return choose


def enrol_dialects() -> Dialects:
    """An enrolment's channel: contract 2 for `pisa.v2`; any other subprotocol (or none) is refused."""

    def choose(subprotocol: str) -> ChannelDialect | None:
        return EnrolChannel() if subprotocol == SUBPROTOCOL else None

    return choose
