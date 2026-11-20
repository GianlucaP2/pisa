"""The device session: what a connected device is to Pisa, whatever carries its frames.

`DeviceSession` is the shared pipeline behind `/v2/live` (a WebSocket), `POST /v2/runs` (one run over HTTP)
and the talk: the turn pipeline (a reply per final utterance, fragments, continuations, "dopo", the echo
guard, taps, partial transcripts), what a device reports (presence, location, place entries, what it showed),
the app's runs, A2UI actions, approval resumes and the catch-up on connect, and Pisa's sends to it, written as
contract 2 (CONTRACT-2 §3, §4.2, §6, §7, §8).

It speaks the internal messages of `session_messages` inside and writes them as AG-UI runs on its
`LiveSession` (`agui`): a reply's `Say` chunks are its text messages, its tool steps STEP_* and TOOL_CALL_*
events, what it shows A2UI surfaces (Ring 1) or MCP Apps views (Ring 2), and the rest `pisa.*` events. The
app's input becomes those messages through the public methods (`run`, `cancel`, `a2ui`, `custom`), validated
as they are made; an input it can't take raises `FrameError`, which each transport answers in its own way.

Threads (CONTRACT-2 §3.1): the app's conversation thread carries its turns and Pisa's answers;
`pisa-notices:<deviceId>` carries what Pisa says unasked (one short run per notice); `pisa-approval:<id>`
carries an approval's runs. A thread has one run at a time: a new reply ends the one it supersedes
(`cancelled`), and a short run that finds the conversation busy waits for the run in progress to end.

The session has no transport: its frames go to whatever the transport attached to its `LiveSession`, and the
`Transport` it is given closes the connection and writes its last frame (`api/live.py`: the socket;
`api/runs.py`: an HTTP request that collects one run's frames and acknowledges no surface).
"""

import asyncio
import json
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ValidationError
from structlog.stdlib import BoundLogger

from pisa.brain.place_tools import PlaceCard
from pisa.brain.present_tools import ACK_WAIT_S
from pisa.db.errors import UNREACHABLE, database_unreachable
from pisa.degrade import degrades, failed, unreachable
from pisa.domain import disclosure as disclosure_rules
from pisa.domain import locale, sheet, turn, visual
from pisa.domain import notice as notice_rules
from pisa.domain import self as self_rules
from pisa.domain import talk as form
from pisa.domain.errors import PisaError
from pisa.domain.sheet import PHONE_BLOCKS
from pisa.domain.stays import Fix, time_problem
from pisa.logging import get_logger
from pisa.ports.push import Outbox, OutgoingApproval, OutgoingNotification
from pisa.services import a2ui, approval_runs, disclosure, endpoints, presence, stays, turns
from pisa.services import devices as devices_service
from pisa.services.agui import LiveSession, RunWriter, new_run_id
from pisa.services.background import Background
from pisa.services.devices import Device
from pisa.services.runtime import Runtime
from pisa.services.session_messages import (
    ActionDone,
    Call,
    Cancel,
    Card,
    CloseObject,
    Compose,
    Correction,
    DeviceContext,
    Directions,
    DrawingAsk,
    Error,
    FocusObject,
    HandoffOpened,
    Location,
    Notify,
    ObjectEvent,
    PlaceEnter,
    Presence,
    Present,
    Presented,
    Say,
    SayAudio,
    ShownPlace,
    SourceLink,
    TalkAskToOpen,
    TurnHint,
    Understood,
    Utterance,
    new_id,
)
from pisa.services.voice import Speaker, b64

if TYPE_CHECKING:
    from pisa.services.talk import TalkRun

_log = get_logger("pisa.session")
_background = Background()

CLOSE_REPLACED = 4001  # a newer connection of the same device replaced this one
CLOSE_AUTH_FAILED = 4003  # revoked, or the session token's hour is over
LANGUAGES: list[Any] = list(locale.WIRE_TAGS)

# the utterance a reply's task answers: what that task sends (its words, what it shows, from the brain's tools
# too) belongs to it. A ContextVar, not a field: an earlier reply's task, cancelled and still finishing, and
# the work it started keep their own.
replying_to: ContextVar[str | None] = ContextVar("replying_to", default=None)

SAID_KEPT = 5  # the final utterances a continuation can name
DEFER_WITHIN_S = 60.0  # "dopo" puts off what Pisa said unasked this recently
DEFER_S = 600.0  # … and it comes back at the next breakpoint, or after this long
# spoken words that plainly don't end wait this long for the rest (the wait for an
# unfinished turn, the turn rules' WAITS_MS["unfinished"])
FRAGMENT_WAIT_S = turn.WAITS_MS["unfinished"] / 1000
# one "ask Pisa about this" every this many seconds per session; a tap
# sooner is answered with an error (visible on the phone), never dropped silently
DRAWING_ASK_EVERY_S = 2.0
CONTINUE_WITHIN_S = 60.0  # a continuation names the latest utterance, at most this long after it arrived
CONTINUED_MAX = 4000  # an utterance's text limit (protocol)


def _words(text: str) -> str:
    """Lowercase words only: the same turn compared across the recognizer's punctuation and capitals."""
    return " ".join(re.findall(r"\w+", text.lower()))


LATE_KEPT = 50  # the `present`s whose answer came too late, remembered to log it


OBJECT_KINDS = frozenset(sheet.OBJECT_KINDS) - {"drawing"}  # the objects a live session shows
ACTIONS = frozenset(
    {"pisa.choose", "pisa.ask", "pisa.sheet_action", "pisa.undo", "pisa.handoff_opened", "pisa.act"}
)
ENDED_KEPT = 200  # the replies whose runs ended, remembered so their late frames are dropped
STREAMS_KEPT = 50  # the latest replies' streams, for their audio
PARTIALS_KEPT = 20  # the latest partial transcripts, for their turn hints
SURFACES_KEPT = 200
INTERRUPTS_KEPT = 50  # the approvals' interrupt runs, for their resumes' parentRunId
# the approval a session is resuming now (its session, the approval): its own decision is reported in its run,
# never also as `pisa.approval.state`. A ContextVar, not a field: only the task taking that decision skips the
# broadcast it causes; anything else said about the approval meanwhile still reaches the app.
_resuming: ContextVar[tuple[int, str] | None] = ContextVar("pisa_live_resuming", default=None)


class FrameError(Exception):
    """An app frame Pisa can't take: answered `invalid_frame`."""


@dataclass
class Negotiated:
    """What `welcome` picked: only ever what the app listed."""

    catalogs: list[str]
    rings: list[int]
    mcp_apps: bool
    extensions: dict[str, int] | None  # None: no hello listed them (HTTP), so every `pisa.*` event goes
    audio_formats: list[str] = field(default_factory=list)

    @property
    def ring1(self) -> bool:
        return bool(self.catalogs)

    @property
    def turn_hints(self) -> bool:
        return "pisa.voice.turn_hints" in (self.extensions or {})


class Transport(Protocol):
    """What carries a session's frames, besides the sink attached to its `LiveSession`."""

    # a shown surface waits for the app's acknowledgment (`pisa.surface.ack`); over HTTP there is none
    acknowledges: bool

    def last(self, text: str) -> None:
        """A connection's last frame (replaced, revoked): stop sending the session's frames, and send this
        one alone (never kept for a resume)."""
        ...

    async def close(self, code: int, reason: str) -> None:
        """Send what is queued, then close the connection with `code`."""
        ...


def _message(model: type[BaseModel], body: dict[str, Any]) -> Any:
    """The session's message from an app frame's values, validated (JSON mode): a value out of its range is
    the frame's `invalid_frame`."""
    try:
        return model.model_validate_json(json.dumps({k: v for k, v in body.items() if v is not None}))
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "frame"
        raise FrameError(f"The field '{where}' is invalid: {first['msg']}.") from None


def control_error(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message[:500] or code, "retryable": retryable}


# the session's error codes with no run to end: the closest control error (CONTRACT-2 §3.1)
_CONTROL_CODES = {
    "auth_failed": "auth_failed",
    "rate_limited": "rate_limited",
    "internal": "internal",
}
# the session's messages that the wire sends as one `pisa.*` event (its extension: `agui.EXTENSION_OF`)
_EVENT_OF: dict[type[BaseModel], str] = {
    SayAudio: "pisa.voice.audio",
    Understood: "pisa.voice.understood",
    TurnHint: "pisa.voice.turn_hint",
    TalkAskToOpen: "pisa.ask_to_open",
    Notify: "pisa.notice",
    Directions: "pisa.directions",
    Compose: "pisa.compose",
    Call: "pisa.call",
    ActionDone: "pisa.action_done",
    FocusObject: "pisa.surface.focus",
}


def _lower_uuid(value: str | None) -> str | None:
    return value.lower() if value else value


def notice_value(message: Notify) -> dict[str, Any]:
    """The session's `Notify` → `pisa.notice` (`$defs/s_pisa_notice`), with its privacy class
    and generic text (`domain.notice`, the same as a relay push's)."""
    return notice_rules.value(message.model_dump(mode="python"), message.lang)


def say_audio_end(final: bool, spoken: int | None, sentence_end: bool = False) -> dict[str, Any]:
    """`final`, and on a final chunk after the audio stopped early: `complete: false` and `spoken_chars`;
    `sentence_end` on a chunk whose audio ends a sentence (not needed on the final chunk)."""
    if final and spoken is not None:
        return {"final": True, "complete": False, "spoken_chars": spoken}
    return {"final": final, "sentence_end": True} if sentence_end and not final else {"final": final}


def done_fields(done: dict[str, object] | None) -> dict[str, Any]:
    """A `done` notify's fields as `Notify` takes them: `undo_until` is stored in JSONB,
    so it comes back as text, and the strict model takes only a datetime."""
    fields: dict[str, Any] = dict(done or {})
    if isinstance(fields.get("undo_until"), str):
        fields["undo_until"] = datetime.fromisoformat(str(fields["undo_until"]))
    return fields


def failure(
    log: BoundLogger, exc: Exception, event: str, message: str, *, trace_id: str | None = None, **fields: Any
) -> PisaError:
    """The error a session reports for what failed at its edge (a reply, a frame, a resume); call it in the
    `except`. An unreachable database is logged as `database_unreachable` and said as such; anything else is
    logged as `event` by its cause (`degrade.failed`: class and code, the traceback only at debug) with a
    trace id (`trace_id`, else a new one) and becomes `internal` with `message`, whose `{trace}` names that
    id."""
    if isinstance(exc, UNREACHABLE):
        log.error("database_unreachable", **({"trace_id": trace_id} if trace_id else {}), **fields)
        return database_unreachable()
    trace_id = trace_id or uuid.uuid4().hex
    failed(log, event, exc, trace_id=trace_id, **fields)
    return PisaError("internal", message.format(trace=trace_id), "Try again")


def _bare_yes_or_no(text: str) -> bool:
    from pisa.domain.actions import is_short_no
    from pisa.domain.tasks import is_short_yes

    return is_short_yes(text) or is_short_no(text)


class SessionScreen:
    """This connection as the screen Pisa shows its objects on (present_tools.Device)."""

    def __init__(
        self,
        session: "DeviceSession",
        kinds: frozenset[str],
        blocks: frozenset[str] | None = None,
        width: float | None = None,
    ) -> None:
        self._session = session
        self.kinds = kinds
        self.blocks = blocks  # the drawing blocks it draws (None: it lists none)
        self.width = width  # the surface's width in points, when known (a diagram is laid out for it)

    async def present(self, message: dict[str, Any]) -> tuple[str, str] | None:
        try:
            frame = Present.model_validate_json(json.dumps(message))
        except ValidationError as err:
            # Pisa's bug: the object breaks the schema
            _log.warning("present_invalid", errors=err.error_count())
            return ("unsupported", "none")
        return await self._session.ask(frame.present_id, frame)

    async def focus(self, present_id: str, object_id: str, highlight: str | None) -> tuple[str, str] | None:
        """A focus has no acknowledgment: None once sent."""
        sent = await self._session.send(
            FocusObject(present_id=present_id, object_id=object_id, highlight=highlight)
        )
        return None if sent else ("unavailable", "none")

    async def close(self, object_id: str) -> bool:
        return await self._session.send(CloseObject(object_id=object_id))

    async def card(self, kind: str, title: str, lines: list[str]) -> bool:
        lines = [line[:200] for line in lines][:10]
        card = Card(card_id=new_id(), title=title[:120], lines=lines, kind=kind)  # type: ignore[arg-type]
        return await self._session.send(card)


class DeviceSession:
    """One device's session: the shared pipeline, with contract 2 as what it writes (its `LiveSession`) and a
    transport that carries it."""

    def __init__(
        self,
        transport: Transport,
        device: Device,
        runtime: Runtime,
        live: LiveSession,
        negotiated: Negotiated,
    ) -> None:
        self.transport = transport
        self.device = device
        self.runtime = runtime
        self.live = live
        self.negotiated = negotiated
        # the only `pisa.*` events it sends (RunWriter); None when no hello negotiated them (HTTP): all go
        live.extensions = None if negotiated.extensions is None else frozenset(negotiated.extensions)
        self.session_id = live.session_id
        # the voice formats Pisa's speaker may stream to it (none: the device speaks the text itself)
        self.audio_formats = frozenset(negotiated.audio_formats)
        self.turn_hints = negotiated.turn_hints  # partial transcripts get a turn hint back
        # the object kinds it shows; the `present`s awaiting their `presented`, and those answered too late
        # (only logged).
        kinds = (OBJECT_KINDS if negotiated.ring1 else frozenset()) | (
            {"drawing"} if negotiated.mcp_apps else frozenset()
        )
        self.screen = SessionScreen(
            self, frozenset(kinds), frozenset(PHONE_BLOCKS) if negotiated.ring1 else None
        )
        # a tap on a drawing while Pisa is answering waits here (one: a newer replaces it)
        self._queued_tap: Utterance | None = None
        self._acks: dict[str, asyncio.Future[tuple[str, str]]] = {}
        self._late: dict[str, float] = {}
        self._last_hint = float("-inf")  # monotonic time of the last partial scored
        self._replies: dict[str, asyncio.Task[None]] = {}  # stream_id and utterance id → reply task
        # continuation: the last final utterances, id → (their words, the history mark before them).
        self._said: dict[str, tuple[str, int | None]] = {}
        self._said_at: dict[str, float] = {}
        self._asked_at = float("-inf")  # the last drawing.ask (monotonic)
        self.closed = False
        # the notifications held for the end of the exchange (stored with `not_before`;
        # here only their ids), and the last thing Pisa said unasked (the user's "dopo" puts it
        # off once).
        self._waiting: list[str] = []
        self.last_unprompted: tuple[OutgoingNotification, float] | None = None
        # what Pisa said in this session (each reply's words, once final), for the echo guard
        self._pisa_lines: list[self_rules.Line] = []
        self._pisa_saying: dict[str, str] = {}
        self._last_final: tuple[float, str] | None = None  # their last final words answered, and when
        self._fragment: tuple[Utterance, asyncio.Task[None]] | None = None  # words waiting
        self.thread = f"conversation:{device.id}"  # the app's conversation thread (its runs name it)
        self.lang = LANGUAGES[-1]  # the language of the app's latest turn
        self._runs: dict[str, tuple[str, str]] = {}  # reply id → (thread, run id)
        self._reply_of_run: dict[str, str] = {}  # run id → reply id
        self._writers: dict[str, RunWriter] = {}  # reply id → its open run
        self._ended: OrderedDict[str, None] = OrderedDict()  # replies whose runs ended
        self._turns: set[str] = set()  # ids that are turns: a failure ends their run (RUN_ERROR)
        self._app_ids: dict[str, str] = {}  # reply id → the app's own id for the turn (message or turn id)
        self._streams: OrderedDict[str, str] = OrderedDict()  # say stream id → reply id (the latest)
        self._partials: OrderedDict[str, str] = OrderedDict()  # partial transcript id → the app's turn id
        self._pending: dict[str, str] = {}  # surface id → the present id its acknowledgment answers
        self._objects: OrderedDict[str, str] = OrderedDict()  # surface id → the object it shows
        self._deferred: dict[str, list[Callable[[], None]]] = {}  # thread → short runs waiting for it
        self._notice_audio: dict[str, RunWriter] = {}  # notice id → the run its audio goes in
        self._interrupt_runs: OrderedDict[str, str] = OrderedDict()  # approval id → its interrupt's run
        self.consent: dict[str, Any] | None = None  # the latest run's forwardedProps.pisa.consent

    def _said_by_pisa(self, message: BaseModel) -> None:
        if not isinstance(message, Say):
            return
        text = self._pisa_saying.pop(message.stream_id, "") + message.delta
        if not message.final:
            self._pisa_saying[message.stream_id] = text
        elif text.strip():
            self._pisa_lines = self_rules.kept(self._pisa_lines, self_rules.Line(time.monotonic(), text))

    def _heard_final(self, utterance: Utterance) -> None:
        """Their spoken words that plainly don't end
        (the turn rules: "poi", "Dobbiamo sicuramente…", "e la cena con") wait FRAGMENT_WAIT_S for the
        rest instead of being answered; words that come in the wait join them, one turn. A fragment joined to
        the next gets an empty final reply (the phone's wait for it ends); typed words never wait."""
        if self._fragment is not None and _bare_yes_or_no(utterance.text):
            # their "sì" or "no" answers alone ("e poi…" + "sì" was "e poi sì"): the fragment goes
            earlier, timer = self._fragment
            self._fragment = None
            timer.cancel()
            _background.start(self._close_fragment(earlier))
            self._start_reply(utterance)
            return
        if self._fragment is not None:
            earlier, timer = self._fragment
            self._fragment = None
            timer.cancel()
            joined = f"{earlier.text.rstrip()} {utterance.text.lstrip()}"[:CONTINUED_MAX]
            utterance = utterance.model_copy(update={"text": joined, "continues": None})
            _log.info("utterance_joined", device_id=str(self.device.id))
            _background.start(self._close_fragment(earlier))
        if (
            utterance.source != "typed"
            and not utterance.continues
            and len(utterance.text) < CONTINUED_MAX
            and turn.kind(utterance.text, utterance.lang) == "unfinished"
        ):
            waiting = asyncio.create_task(self._fragment_waits(utterance))
            self._fragment = (utterance, waiting)
            _log.info("utterance_waits", device_id=str(self.device.id), wait_s=FRAGMENT_WAIT_S)
            return
        self._start_reply(utterance)

    async def _fragment_waits(self, utterance: Utterance) -> None:
        await asyncio.sleep(FRAGMENT_WAIT_S)
        if self._fragment is not None and self._fragment[0] is utterance:
            self._fragment = None
            self._start_reply(utterance)  # nothing more came: their words, as they are

    async def _close_fragment(self, earlier: Utterance) -> None:
        await self.send(
            Say(reply_to=earlier.id, stream_id=new_id(), seq=0, delta="", final=True, lang=earlier.lang,
                priority="normal")  # type: ignore[arg-type]
        )  # fmt: skip

    def repeated(self, utterance: Utterance) -> str | None:
        """Why a final utterance is no new turn (None: it is one): their last words again within
        REPEAT_WITHIN_S (`same`), or, spoken, those words with Pisa's own after them (`echo`: the phone's
        recognizer heard Pisa's holding phrase or answer and sent their turn again). Never a continuation,
        never a bare yes or no."""
        last = self._last_final
        if last is None or utterance.continues or _bare_yes_or_no(utterance.text):
            return None
        at, earlier = last
        now = time.monotonic()
        if now - at > self_rules.REPEAT_WITHIN_S:
            return None
        pisa = [
            *self._pisa_saying.values(),
            *(line.text for line in self_rules.recent(self._pisa_lines, now)),
        ]
        why = self_rules.repeat_of(utterance.text, earlier, pisa)
        return None if why == "echo" and utterance.source == "typed" else why

    def heard_itself(self, utterance: Utterance) -> bool:
        """A spoken utterance that is Pisa's own recent line come back through the microphone (their
        "turn" repeated Pisa's "Ho sbagliato, non so cosa intendi" word for word), or the words of the
        reply Pisa is saying now (not over yet). Never a typed one."""
        now = time.monotonic()
        saying = [self_rules.Line(now, text) for text in self._pisa_saying.values()]
        return utterance.source != "typed" and self_rules.own_echo(
            utterance.text, [*self._pisa_lines, *saying], now, bare=_bare_yes_or_no
        )

    def talking(self) -> "TalkRun | None":
        """This device's open talk, if any (its talker is then Pisa's only voice)."""
        talk = getattr(self.runtime, "talk", None)
        run = talk.run_for(self.device.id) if talk is not None else None
        if run is not None and run.link_silent():
            # the talk's audio stopped past its idle timeout while the run is still
            # registered: its link is gone and the phone fell back to its own voice path. The run ends, and
            # the phone's words and notices are answered here again (never an empty `say`).
            run.timeline.add("link_silent")
            run.end("fallback", "peer", "no audio from the phone past the talk's idle timeout")
            return None
        return run

    def busy(self) -> bool:
        """The user is in an exchange with Pisa: a reply is being answered or said."""
        return any(not task.done() for task in self._replies.values())

    def wait(self, notification_id: str) -> None:
        self._waiting.append(notification_id)

    def _outbox(self) -> Outbox | None:
        """The stored notifications, when this process's session registry holds them (the API's)."""
        from pisa.services.session_registry import SessionRegistry  # it imports this module

        registry = self.runtime.live
        return registry.outbox if isinstance(registry, SessionRegistry) else None

    def breakpoint(self) -> None:
        """The exchange ended: what waited for it goes now, and what the user put off with "dopo"."""
        outbox = self._outbox()
        if self.busy() or not self._waiting or outbox is None:
            return
        waiting, self._waiting = self._waiting, []
        _background.start(outbox.release(waiting))

    async def put_off(self, utterance: Utterance) -> bool:
        """ "Dopo" right after Pisa spoke up unasked: that item comes back at the next breakpoint, once
        (rule 30); Pisa says only "Ok, dopo." with no model call."""
        last = self.last_unprompted
        if last is None or time.monotonic() - last[1] > DEFER_WITHIN_S or not form.defers(utterance.text):
            return False
        notification = last[0]
        outbox = self._outbox()
        # Stored, not in this session's memory: once only; back at the next breakpoint, or
        # after DEFER_S by the sweep (a push if the phone went away meanwhile).
        if outbox is None or not await outbox.put_off(notification.id, DEFER_S):
            return False
        self.last_unprompted = None
        self._waiting.append(notification.id)
        said = form.LATER.get(utterance.lang, form.LATER["en-US"])
        await self.send(
            Say(reply_to=utterance.id, stream_id=new_id(), seq=0, delta=said, final=True, lang=utterance.lang,
                priority="normal")  # type: ignore[arg-type]
        )  # fmt: skip
        return True

    async def ask(self, present_id: str, frame: BaseModel) -> tuple[str, str] | None:
        """Send a `present` or `focus` and wait for its `presented`: (status, surface); None past ACK_WAIT_S
        (the late answer is logged when it comes). Shown where nothing acknowledges (HTTP): None once sent."""
        if not self.transport.acknowledges:
            return None if await self.send(frame) else ("unavailable", "none")
        ack: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
        self._acks[present_id] = ack
        if not await self.send(frame):
            self._acks.pop(present_id, None)
            return ("unavailable", "none")
        try:
            return await asyncio.wait_for(asyncio.shield(ack), ACK_WAIT_S)
        except TimeoutError:
            self._acks.pop(present_id, None)
            self._late[present_id] = time.monotonic()
            while len(self._late) > LATE_KEPT:
                self._late.pop(next(iter(self._late)))
            return None

    def cancel_all(self) -> None:
        for task in set(self._replies.values()):
            task.cancel()
        if self._fragment is not None:  # words still waiting are never answered after it
            self._fragment[1].cancel()
            self._fragment = None

    async def handle(self, message: BaseModel) -> None:
        """One message from the device (an app frame, as the wire read it)."""
        match message:
            case Utterance():
                talking = self.talking()
                if talking is not None and message.source != "typed":
                    # while a talk is open, the
                    # talker is Pisa's one voice and hears the user itself; the phone's own recognizer words
                    # are never answered here too (no `say`, no audio). A final gets an empty final reply.
                    talking.timeline.add("ws_utterance", final=message.final, source=message.source)
                    _log.info("utterance_in_talk", device_id=str(self.device.id), final=message.final)
                    if message.final:
                        await self.send(
                            Say(reply_to=message.id, stream_id=new_id(), seq=0, delta="", final=True,
                                lang=message.lang, priority="normal")  # type: ignore[arg-type]
                        )  # fmt: skip
                elif message.final and (why := self.repeated(message)) is not None:
                    # their words again, or with Pisa's own glued after them: the reply under way answers
                    # them; no second turn (the phone's wait ends with an empty final reply)
                    _log.info("utterance_repeated", device_id=str(self.device.id), why=why)
                    await self.send(
                        Say(reply_to=message.id, stream_id=new_id(), seq=0, delta="", final=True,
                            lang=message.lang, priority="normal")  # type: ignore[arg-type]
                    )  # fmt: skip
                elif message.final and self.heard_itself(message):
                    # Pisa's own voice is never their words: nothing answered, nothing stored; the phone's
                    # wait ends with an empty final reply
                    _log.info("utterance_echo", device_id=str(self.device.id))
                    await self.send(
                        Say(reply_to=message.id, stream_id=new_id(), seq=0, delta="", final=True,
                            lang=message.lang, priority="normal")  # type: ignore[arg-type]
                    )  # fmt: skip
                elif message.final:
                    self._last_final = (time.monotonic(), message.text)
                    self._heard_final(message)
                else:
                    self._partial(message)
            case Cancel():
                task = self._replies.get(message.target_id)
                if task is not None:
                    task.cancel()
                    _log.info("reply_cancelled", device_id=str(self.device.id), target_id=message.target_id)
            case Location():
                problem = time_problem(message.measured_at, self.runtime.clock())
                if problem:
                    err = PisaError(
                        "invalid_message",
                        f"The location's measured_at {problem}, so it was not stored.",
                        "Check the device's clock",
                    )
                    await self.send(Error.from_error(err, reply_to=message.id))
                    return
                fix = Fix(message.lat, message.lon, message.accuracy_m, message.measured_at)
                async with self.runtime.pool.connection() as conn:
                    await stays.save(conn, self.device.id, [fix])
            case Presence():
                async with self.runtime.pool.connection() as conn:
                    state = message.state.model_dump(mode="json") if message.state is not None else None
                    trigger = await presence.update(
                        conn, self.device.id, message.event, self.runtime.clock(), state
                    )
                _log.info(
                    "device_presence", device_id=str(self.device.id), presence=message.event, trigger=trigger
                )
                if trigger is not None:
                    # The briefing may call a model: the frame loop never waits for it.
                    _background.start(self._presence_briefing(trigger))
            case PlaceEnter():  # Pisa answers with the note (a notice: in this session, or a push)
                await self.runtime.place_notes.enter(self.device.id, list(message.note_ids))
            case Presented():
                self._presented(message)
            case ObjectEvent():
                _background.start(self._object_event(message))
            case DrawingAsk():  # "ask Pisa about this", a tap in a drawing
                _background.start(self._drawing_ask(message))
            case DeviceContext():  # a landing, a settings change
                # the last known zone stays; the session goes on
                with degrades(_log, "device_context", device_id=str(self.device.id)):
                    await self.runtime.zone.update(
                        tz=message.tz, region=message.region, hour_cycle=message.hour_cycle,
                        device_id=self.device.id, now=self.runtime.clock(),
                    )  # fmt: skip
            case HandoffOpened():  # the user tapped what Pisa prepared (ids only)
                try:
                    await self.runtime.handoffs.opened(self.device.id, message.handoff_id, message.object_id)
                except UNREACHABLE:
                    unreachable(_log, "handoff_opened", device_id=str(self.device.id))
            case _:
                raise TypeError(f"'{getattr(message, 'type', '?')}' is sent by Pisa, not by devices")

    def _presented(self, message: Presented) -> None:
        ack = self._acks.pop(message.present_id, None)
        if ack is not None and not ack.done():
            ack.set_result((message.status, message.surface))
        elif (sent := self._late.pop(message.present_id, None)) is not None:
            _log.info(
                "present_ack_late", device_id=str(self.device.id), status=message.status,
                after_ms=int((time.monotonic() - sent) * 1000) + int(ACK_WAIT_S * 1000),
            )  # fmt: skip
            _background.start(
                self.runtime.conversations.object_event(
                    self.device.id, message.object_id, message.status, present_id=message.present_id
                )
            )
        else:
            _log.warning("present_ack_unknown", device_id=str(self.device.id), status=message.status)

    async def _object_event(self, message: ObjectEvent) -> None:
        """What the user did with an object (ids only): its show's outcome; a tap on a choice's option is the
        user's own next message ("[tap] o2: …")."""
        _log.info("object_event", device_id=str(self.device.id), what=message.event)
        try:
            said = await self.runtime.conversations.object_event(
                self.device.id, message.object_id, message.event, message.option_id,
                element_id=message.element_id,
            )  # fmt: skip
        except UNREACHABLE:
            unreachable(_log, "object_event", device_id=str(self.device.id))
            if message.event == "chosen":
                await self.send(Error.from_error(database_unreachable(), reply_to=message.id))
            return
        if message.event == "asked":
            await self._asked(message, said)
            return
        if said is None and message.event == "chosen":
            # a tap that starts no turn (a choice from before a
            # restart, closed or expired) still answers its own id, so the phone leaves "waiting" and listens.
            gone = PisaError("invalid_message", "That choice is no longer open: tell Pisa instead.")
            await self.send(Error.from_error(gone, reply_to=message.id))
            return
        if said is not None:
            words, lang = said
            # The tap's id is the object_event's own: the phone knows it, so Pisa's reply (`reply_to`) answers
            # the turn it started (a fresh id here made the phone drop the reply).
            tap = Utterance(
                id=message.id,
                text=words,
                lang=lang,  # type: ignore[arg-type]
                final=True,
                source="typed",
                wake="tap",
            )
            self._start_reply(tap)

    async def _asked(self, message: ObjectEvent, said: tuple[str, str] | None) -> None:
        """A tap on a drawing's element. Gone (superseded, unknown, closed, expired):
        "Non è più aperto.", no model call. While Pisa is answering it waits, one at most (a newer tap
        replaces it); else its turn starts now."""
        if said is None:
            lang = self.runtime.conversations.tap_lang(self.device.id, message.object_id)
            gone = visual.GONE.get(lang, visual.GONE["en-US"])
            await self.send(
                Say(reply_to=message.id, stream_id=new_id(), seq=0, delta=gone, final=True, lang=lang,  # type: ignore[arg-type]
                    priority="normal")
            )  # fmt: skip
            return
        words, lang = said
        tap = Utterance(id=message.id, text=words, lang=lang, final=True, source="typed", wake="tap")  # type: ignore[arg-type]
        self._tap_turn(tap)

    def _waited_tap(self) -> None:
        """Pisa's answer ended: the tap that waited for it starts its turn, still a tap's."""
        if not self._replies and (queued := self._queued_tap) is not None and not self.closed:
            self._queued_tap = None
            self._answer(queued, origin="drawing")

    def _tap_turn(self, tap: Utterance) -> None:
        """A tap in a drawing (a
        block's element or the box's `data-pisa-ask`) is their turn marked `origin: drawing`, never read from
        its words. While Pisa is answering it waits, one at most (a newer tap replaces it): it never cancels
        Pisa's reply."""
        if self._replies:
            replaced = self._queued_tap is not None
            self._queued_tap = tap
            _log.info("tap_queued", device_id=str(self.device.id), replaced=replaced)
            return
        self._answer(tap, origin="drawing")

    async def _drawing_ask(self, message: DrawingAsk) -> None:
        """A tap in a drawing Pisa showed is their turn marked `origin: drawing`
        ("Sul disegno: <label>"): read-only, whatever the label says. Ids and counts only in the logs."""
        _log.info("drawing_ask", device_id=str(self.device.id), chars=len(message.label))
        now = time.monotonic()
        if now - self._asked_at < DRAWING_ASK_EVERY_S:
            soon = PisaError(
                "invalid_message", "One question about the drawing at a time: tap again in a moment."
            )
            await self.send(Error.from_error(soon, reply_to=message.id))
            return
        self._asked_at = now
        said = await self.runtime.conversations.drawing_ask(self.device.id, message.drawing_id, message.label)
        if said is None:
            gone = PisaError("invalid_message", "That drawing is no longer open: tell Pisa instead.")
            await self.send(Error.from_error(gone, reply_to=message.id))
            return
        words, lang = said
        tap = Utterance(id=message.id, text=words, lang=lang, final=True, source="typed", wake="tap")  # type: ignore[arg-type]
        self._tap_turn(tap)

    def _partial(self, utterance: Utterance) -> None:
        """A partial transcript never starts a reply and is never stored or logged (its length
        only). With turn hints on (`pisa.voice.turn_hints`), at most 2 a second are scored and answered with
        a turn hint. Pisa's reflex reads them (economy too): Pisa's own data ready when the user stops."""
        self.runtime.reflex.heard(self.device.id, utterance.text, utterance.lang)
        now = time.monotonic()
        if not self.turn_hints or now - self._last_hint < turn.HINT_MIN_INTERVAL_S:
            _log.debug("partial_skipped", device_id=str(self.device.id), chars=len(utterance.text))
            return
        self._last_hint = now
        _background.start(self._turn_hint(utterance, now))

    async def _turn_hint(self, utterance: Utterance, started: float) -> None:
        try:
            hint = await self.runtime.turn_hints.hint(utterance.text, utterance.lang)
        except Exception as exc:
            _log.warning("turn_hint_failed", device_id=str(self.device.id), cause=exc.__class__.__name__)
            return
        await self.send(TurnHint(reply_to=utterance.id, p_end=hint.p_end, wait_ms=hint.wait_ms))
        _log.debug(
            "turn_hint",
            device_id=str(self.device.id),
            chars=len(utterance.text),
            p_end=hint.p_end,
            wait_ms=hint.wait_ms,
            source=hint.source,
            ms=round((time.monotonic() - started) * 1000),
        )

    async def _presence_briefing(self, trigger: presence.Trigger) -> None:
        with degrades(_log, "briefing", device_id=str(self.device.id), trigger=trigger):
            await self.runtime.briefings.on_presence(self.device.id, trigger)

    def _start_reply(self, utterance: Utterance) -> None:
        if self.last_unprompted is not None and form.defers(utterance.text):
            _background.start(self._put_off_or_answer(utterance))
            return
        self._answer(utterance)

    async def _put_off_or_answer(self, utterance: Utterance) -> None:
        if not await self.put_off(utterance):
            self._answer(utterance)

    def _answer(self, utterance: Utterance, origin: str = "words") -> None:
        if origin != "drawing":
            self._queued_tap = None  # their own words: a tap still waiting is moot
        # A new utterance supersedes the reply still in progress: the user moved on.
        superseded = set(self._replies.values())
        for task in superseded:
            task.cancel()
        if superseded:
            _log.info("reply_superseded", device_id=str(self.device.id), count=len(superseded))
        # "continues" — the user went on after Pisa closed the turn: one turn, the earlier words first.
        # Only the latest final utterance, within a minute: an older id would delete later, finished turns.
        latest = next(reversed(self._said), None)
        fresh = time.monotonic() - self._said_at.get(utterance.continues or "", 0.0) <= CONTINUE_WITHIN_S
        earlier = (
            self._said.pop(utterance.continues, None)
            if utterance.continues and utterance.continues == latest and fresh
            else None
        )
        if origin == "drawing":  # a drawing's tap joins no earlier words
            earlier = None
        if utterance.continues:
            _log.info("utterance_continued", device_id=str(self.device.id), known=earlier is not None)
        if earlier is not None and not _words(utterance.text).startswith(_words(earlier[0])):
            # The app sends the whole turn; an app that sends only the new words gets them joined
            # here, never twice ("compra il latte compra il latte e il pane").
            joined = f"{earlier[0]} {utterance.text}"[:CONTINUED_MAX]
            utterance = utterance.model_copy(update={"text": joined})
        self._said[utterance.id] = (utterance.text, None)
        self._said_at[utterance.id] = time.monotonic()
        while len(self._said) > SAID_KEPT:
            self._said.pop(next(iter(self._said)))
        self._said_at = {k: v for k, v in self._said_at.items() if k in self._said}
        stream_id = new_id()
        trace_id = uuid.uuid4().hex
        seq = 0

        async def send_audio(
            n: int, data: bytes, final: bool, fmt: str, rate: int, spoken: int | None, end: bool
        ) -> bool:
            audio = {"stream_id": stream_id, "seq": n, "format": fmt, "sample_rate": rate, "data": b64(data)}
            return await self.send(SayAudio.model_validate(audio | say_audio_end(final, spoken, end)))

        # a typed turn while a talk is open is answered in text only: no second voice
        talking = self.talking() is not None
        formats = frozenset[str]() if talking else self.audio_formats
        speaker = Speaker(self.runtime.voice, send_audio, trace_id, formats)
        speaker.warm()  # the voice and its connection ready while the model thinks

        async def emit(delta: str, final: bool, lang: str = utterance.lang) -> None:
            nonlocal seq
            speaker.feed(delta)  # each sentence to the voice as soon as it is complete
            if final:
                speaker.finish()
            await self.send(
                Say(
                    reply_to=utterance.id,
                    stream_id=stream_id,
                    seq=seq,
                    delta=delta,
                    final=final,
                    lang=lang,  # type: ignore[arg-type]
                    priority="normal",
                )
            )
            seq += 1

        async def send_holding(holding_id: str, text: str) -> None:
            """A holding phrase the app plays from its cache (no audio from Pisa: it has the phrase)."""
            nonlocal seq
            await self.send(
                Say(
                    reply_to=utterance.id, stream_id=stream_id, seq=seq, delta=text + " ", final=False,
                    lang=utterance.lang, priority="normal", holding=True, holding_id=holding_id,  # type: ignore[arg-type]
                )
            )  # fmt: skip
            seq += 1

        cancelled = False

        async def run() -> None:
            nonlocal cancelled
            replying_to.set(utterance.id)  # what this reply's task sends answers this utterance
            try:
                try:
                    if earlier is not None:
                        # The earlier reply stops first; then its words and partial reply leave the history.
                        await asyncio.gather(*superseded, return_exceptions=True)
                        if earlier[1] is not None:
                            await self.runtime.conversations.drop_after(self.device.id, earlier[1])
                    mark = await self.runtime.conversations.mark(self.device.id)
                    if utterance.id in self._said:
                        self._said[utterance.id] = (utterance.text, mark)
                    # the turn itself is one path with POST /v2/runs (`turns.answer`)
                    await turns.answer(
                        self.runtime,
                        self.device.id,
                        utterance.text,
                        utterance.lang,
                        emit,
                        trace_id=trace_id,
                        speaker=speaker,
                        continued=earlier is not None,
                        hold=None if talking else send_holding,  # no holding line while a talk is open
                        spoken=utterance.source != "typed",  # no slow line on a typed turn
                        reply_to=utterance.id,
                        heard=utterance.voice,  # the phone's observables, this turn only
                        origin=origin,  # "drawing" for a tap in a drawing (read-only)
                        senders=self.turn_senders(utterance.id),
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    speaker.cancel()  # cancel (or a new utterance): no more audio either
                    raise
                except PisaError as err:
                    failed = err
                except Exception as exc:
                    failed = failure(
                        _log, exc, "reply_failed", "Pisa failed while answering (trace {trace}).",
                        trace_id=trace_id, device_id=str(self.device.id),
                    )  # fmt: skip
                else:
                    return
                # What was said is spoken to its end, inside the reply's run: the error ends the run after it
                # (its task never leaks).
                speaker.finish()
                await speaker.done()
                await self.send(Error.from_error(failed, reply_to=utterance.id))
            finally:
                self._replies.pop(stream_id, None)
                self._replies.pop(utterance.id, None)
                self.reply_ended(utterance.id, cancelled)
                self.breakpoint()  # the end of an exchange
                self._waited_tap()

        self.reply_started(utterance.id)
        task = asyncio.create_task(run())
        # A device learns stream_id from the first chunk; before that it may cancel by the utterance id.
        self._replies[stream_id] = task
        self._replies[utterance.id] = task

    # --- the connection -----------------------------------------------------------------------------
    def _final_error(self, code: str, message: str) -> None:
        """A connection's last frame (replaced, revoked): to this connection only, never kept for a resume."""
        frame = {"control": "error", "seq": self.live.seq + 1, **control_error(code, message)}
        self.transport.last(json.dumps(frame, ensure_ascii=False))

    async def close(self, code: int, reason: str) -> None:
        """Close the connection with `code` (one a newer connection replaced: 4001)."""
        if self.closed:
            return
        if code == CLOSE_REPLACED:
            self._final_error("replaced", "A newer connection of this device replaced this one.")
        self.closed = True
        await self.transport.close(code, reason)
        self.cancel_all()

    async def revoke(self) -> None:
        """The device was revoked while connected: say so, then close with 4003."""
        self._final_error("device_revoked", "This device was revoked on the server.")
        await self.close(CLOSE_AUTH_FAILED, "device revoked")

    def tell(self, name: str, value: dict[str, Any]) -> bool:
        """A Ring 0 event (`pisa.ai.disclosure`, `pisa.endpoints`) in a short run on the notices thread."""
        if self.closed:
            return False
        self._once(self.live.notice_thread(), lambda w: w.custom(name, value))
        return self.live.attached

    async def consent_refused(self, consent: Any, words: str | None, lang: str | None = None) -> str | None:
        """Why this run may not use AI (CONTRACT-2 §4.4; in `lang`, else the latest turn's language), or None:
        the run's consent, else this session's latest, else the device's recorded one, against the disclosure
        as it is now."""
        if isinstance(consent, dict):
            self.consent = consent
        async with self.runtime.pool.connection() as conn:
            return await disclosure.check(
                conn, self.device.id, self.consent, words, self.runtime.settings.ollama_base_url,
                lang or self.lang,
            )  # fmt: skip

    # --- runs ---------------------------------------------------------------------------------------
    def _end(self, reply_to: str) -> None:
        self._writers.pop(reply_to, None)
        self._ended[reply_to] = None
        while len(self._ended) > ENDED_KEPT:
            ended, _ = self._ended.popitem(last=False)
            self._runs.pop(ended, None)
            self._app_ids.pop(ended, None)
            self._turns.discard(ended)
        for run_id in [r for r, reply in self._reply_of_run.items() if reply not in self._runs]:
            del self._reply_of_run[run_id]

    def _close(self, writer: RunWriter, how: Callable[[RunWriter], object]) -> None:
        """End a run, mark its reply ended, and start the short runs that waited for its thread."""
        how(writer)
        for reply_to, w in list(self._writers.items()):
            if w is writer:
                self._end(reply_to)
        self._flush(writer.thread_id)

    def _flush(self, thread: str) -> None:
        waiting = self._deferred.get(thread)
        while waiting and self.live.open_run(thread) is None:
            waiting.pop(0)()
        if not waiting:
            self._deferred.pop(thread, None)

    def _run_of(self, reply_to: str) -> tuple[str, str]:
        if reply_to not in self._runs:
            self._runs[reply_to] = (self.thread, new_run_id())
            self._reply_of_run[self._runs[reply_to][1]] = reply_to
        return self._runs[reply_to]

    def _once(
        self,
        thread: str,
        write: Callable[[RunWriter], object],
        run_id: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        """A short run, whole: now, or after the run in progress on its thread. On the notices thread the run
        in progress (a notice's audio) ends first: a notice never waits."""

        def go() -> None:
            if reply_to is not None and reply_to in self._ended:
                return
            writer = self.live.start_run(thread, run_id)
            if reply_to is not None:
                self._writers[reply_to] = writer
            write(writer)
            if not writer.closed:
                writer.finish()
            if reply_to is not None:
                self._end(reply_to)
            self._flush(thread)

        current = self.live.open_run(thread)
        if current is not None and thread == self.live.notice_thread():
            self._close(current, RunWriter.finish)
            current = None
        if current is None:
            go()
        else:
            self._deferred.setdefault(thread, []).append(go)

    def _reply_writer(self, reply_to: str | None) -> RunWriter | Literal[False] | None:
        """The open run of a reply; None when there is none; False when it ended (its frames are dropped)."""
        if reply_to is None:
            return None
        writer = self._writers.get(reply_to)
        if writer is not None and not writer.closed:
            return writer
        return False if reply_to in self._ended else None

    def _in_turn(self, write: Callable[[RunWriter], object]) -> bool:
        """What a turn sends beside its words: in the reply's run; else in the conversation's run in progress,
        or a short run of its own. Nothing for a reply whose run ended."""
        writer = self._reply_writer(replying_to.get())
        if writer is False:
            return False
        writer = writer or self.live.open_run(self.thread)
        if writer is not None:
            write(writer)
        else:
            self._once(self.thread, write)
        return True

    def reply_started(self, reply_to: str) -> None:
        current = self.live.open_run(self._run_of(reply_to)[0])
        if current is not None:  # the reply it supersedes was cancelled: its run ends so
            self._close(current, RunWriter.cancelled)
        if reply_to in self._ended:
            return
        thread, run_id = self._runs[reply_to]
        self._writers[reply_to] = self.live.start_run(thread, run_id)

    def end_notice_audio(self) -> None:
        """The connection is gone: a notice's audio stops here, and its run ends with it."""
        for writer in list(self._notice_audio.values()):
            if not writer.closed:
                self._close(writer, RunWriter.finish)
        self._notice_audio.clear()

    def reply_ended(self, reply_to: str, cancelled: bool) -> None:
        writer = self._writers.get(reply_to)
        if writer is not None and not writer.closed:
            self._close(writer, RunWriter.cancelled if cancelled else RunWriter.finish)

    def turn_senders(self, reply_to: str | None) -> dict[str, Any]:
        """What a turn shows on this device beside its words (cards, the compose and call screens, maps, what
        Pisa understood, the presentation objects): the economy path's and the talker's thinker's."""

        async def send_card(place: PlaceCard) -> None:
            await self.send(
                Card(
                    card_id=new_id(),
                    title=place.title,
                    lines=place.lines,
                    kind="place",
                    place_id=place.place_id,
                )
            )

        async def send_places(card: dict[str, Any]) -> bool:
            """show_places: the best 1 to 3 places, each requirement as checked."""
            return await self.send(
                Card(
                    card_id=card.get("card_id") or new_id(),  # the outcome log knows it
                    title=card["title"],
                    lines=card["lines"],
                    kind="places",
                    request=card["request"],
                    places=[ShownPlace.model_validate(p) for p in card["places"]],
                )
            )

        async def send_sources(card: dict[str, Any]) -> bool:
            """The links behind a web answer (only pages this turn's tools returned)."""
            return await self.send(
                Card(
                    card_id=new_id(),
                    title=card["title"],
                    lines=card["lines"],
                    kind="sources",
                    sources=[SourceLink.model_validate(s) for s in card["sources"]],
                )
            )

        async def send_understood(body: dict[str, Any]) -> bool:
            """What Pisa understood, before the answer (only when something changed or Pisa asks)."""
            return await self.send(
                Understood(
                    reply_to=reply_to,
                    meant=body["meant"][:2000],
                    raw=body["raw"][:2000],
                    corrections=[Correction.model_validate(c) for c in body["corrections"][:10]],
                    ask=body["ask"],
                )
            )

        async def send_directions(target: dict[str, Any]) -> bool:
            """start_navigation: the phone opens its maps app."""
            return await self.send(Directions(**target))

        async def send_compose(message: dict[str, Any]) -> bool:
            """compose_message: the phone opens the app's compose screen; the user's tap sends."""
            return await self.send(Compose.model_validate(message))

        async def send_call(message: dict[str, Any]) -> bool:
            """call_place: the phone dials after the user confirms."""
            return await self.send(Call.model_validate(message))

        senders: dict[str, Any] = {
            "card": send_card, "places_card": send_places, "sources_card": send_sources,
            "directions": send_directions, "compose": send_compose, "call_phone": send_call,
            "understood": send_understood, "device": self.screen,
        }  # fmt: skip
        if reply_to is not None:

            async def steps(what: str, call_id: str, name: str) -> None:
                writer = self._reply_writer(reply_to)
                if not writer:
                    return
                if what == "started":
                    writer.step_started(name)
                    writer.tool_call(call_id, name)
                else:
                    writer.step_finished(name)

            senders["steps"] = steps
        # an app that didn't pick an action's extension gets no sender for it: the brain's tool then says it
        # can't open it on this device, and Pisa's words carry the destination, the number or the message
        for key, name in (
            ("directions", "pisa.directions"),
            ("compose", "pisa.compose"),
            ("call_phone", "pisa.call"),
        ):
            if not self.live.grants(name):
                senders[key] = None
        return senders

    # --- approvals ----------------------------------------------------------------------------------
    async def approval(self, approval: OutgoingApproval) -> bool:
        """An approval to answer: a run on its own thread that ends with its interrupt (CONTRACT-2 §6.1)."""
        if self.closed:
            return False
        interrupt = approval_runs.interrupt_of(approval)
        if interrupt is None:
            _log.warning("approval_without_digest", approval_id=approval.id)
            return False
        approval_id = approval.id.lower()

        def write(w: RunWriter) -> None:
            self._interrupt_runs[approval_id] = w.run_id
            while len(self._interrupt_runs) > INTERRUPTS_KEPT:
                self._interrupt_runs.popitem(last=False)
            w.interrupt([interrupt])

        self._once(approval_runs.thread_of(approval_id), write)
        return self.live.attached

    async def approval_state(self, state: dict[str, Any]) -> bool:
        """An approval decided or closed elsewhere: `pisa.approval.state` in a short run on its thread, so the
        app drops the interrupt it holds. Not for the decision this session is resuming (its run says it)."""
        if self.closed or _resuming.get() == (id(self), str(state["approval_id"]).lower()):
            return False
        if not self.live.grants("pisa.approval.state"):
            return False
        value = approval_runs.state_value(state)
        self._once(
            approval_runs.thread_of(value["approvalId"]), lambda w: w.custom("pisa.approval.state", value)
        )
        return self.live.attached

    async def _resume(self, thread: str, run_id: str, body: dict[str, Any]) -> None:
        """The app's decision on an approval (`run` with `resume` on its thread): one run, which reports
        `pisa.action_done`, or RUN_ERROR with one of the approval thread's codes (`ApprovalErrorCode`,
        CONTRACT-2 §6.1 steps 4 to 6). A frame that can't be a resume (not an approval's thread, not its
        interrupt, a bad payload, an unknown approval) starts no run: `invalid_frame`. A failure outside
        those codes (the database, a crash, the watch approving) ends the run `cancelled`, nothing decided,
        with a control error naming it: the app may resume again."""
        approval_id = (approval_runs.approval_of_thread(thread) or "").lower()
        if not a2ui.is_uuid(approval_id):
            raise FrameError("A resume runs on an approval's thread, pisa-approval:<approvalId>.")
        if self.live.open_run(thread) is not None:
            raise FrameError(f"A run is in progress on {thread}.")
        try:
            entry = approval_runs.resume_entry(approval_id, body.get("resume"))
            payload = (
                None if entry["status"] == "cancelled" else approval_runs.payload_of(entry.get("payload"))
            )
            await self.runtime.approvals.row(approval_id)  # 404 before any run
        except PisaError as err:
            raise FrameError(err.message) from None
        writer = self.live.start_run(thread, run_id, parent_run_id=self._interrupt_runs.get(approval_id))
        if payload is None:  # let go: nothing is decided, the Approvals list keeps it
            writer.finish()
            self._flush(thread)
            return
        token = _resuming.set((id(self), approval_id))
        try:
            approval = await approval_runs.decide(self.runtime, approval_id, payload, self.device.id)
        except PisaError as err:
            if err.code in approval_runs.ERROR_CODES:
                writer.error(err.message, err.code)
            else:
                code = "invalid_frame" if err.code == "validation_failed" else "internal"
                self._resume_failed(writer, code, err.message, err.retryable)
            return
        except Exception as exc:
            err = failure(
                _log, exc, "approval_resume_failed", "Pisa failed to take the decision (trace {trace}).",
                device_id=str(self.device.id),
            )  # fmt: skip
            self._resume_failed(writer, "internal", err.message, True)
            return
        finally:
            _resuming.reset(token)
            if writer.closed:
                self._flush(thread)
        approval_runs.write_outcome(writer, approval, self.lang)
        self._flush(thread)

    def _resume_failed(self, writer: RunWriter, code: str, message: str, retryable: bool) -> None:
        """A resume that failed outside the approval thread's codes: its run ends `cancelled` (nothing was
        decided), and a control error says why."""
        writer.cancelled()
        self.live.control("error", **control_error(code, message, retryable))

    # --- what Pisa sends, as contract 2 -------------------------------------------------------------
    async def send(self, message: BaseModel) -> bool:
        """Write one message of the shared pipeline as AG-UI events; True when it went out (to the attached
        transport)."""
        self._said_by_pisa(message)
        if self.closed:
            return False
        try:
            sent = self._write_message(message)
        except Exception:
            _log.exception(
                "live_frame_failed", device_id=str(self.device.id), kind=getattr(message, "type", "?")
            )
            return False
        return sent and self.live.attached

    def _app_id(self, reply_to: str | None) -> str | None:
        return self._app_ids.get(reply_to, reply_to) if reply_to else None

    def _write_message(self, message: BaseModel) -> bool:
        """Each `pisa.*` event goes only when the welcome granted its extension (CONTRACT-2 §4.2; `RunWriter`
        refuses the rest). Checked here first, so no run opens for nothing: a notice or an action's outcome
        the app can't show as its event is said as Pisa's words instead (the contract is silent; the user
        never loses it), the rest is not sent (False)."""
        name = next((n for kind, n in _EVENT_OF.items() if isinstance(message, kind)), None)
        if name is not None and not self.live.grants(name):
            return self._in_words(message)
        match message:
            case Say():
                return self._say(message)
            case SayAudio():
                return self._audio(message)
            case Error():
                return self._error(message)
            case Understood():
                value = {
                    "replyTo": self._app_id(message.reply_to) or "-",
                    "meant": message.meant or "-",
                    "raw": message.raw or "-",
                    "corrections": [{"from": c.from_, "to": c.to} for c in message.corrections],
                    "ask": message.ask,
                }
                writer = self._reply_writer(message.reply_to)
                if writer is False:
                    return False
                if writer is not None:
                    writer.custom("pisa.voice.understood", value)
                    return True
                return self._in_turn(lambda w: w.custom("pisa.voice.understood", value))
            case TurnHint():
                value = {
                    "replyTo": self._partials.pop(message.reply_to, message.reply_to),
                    "pEnd": message.p_end,
                    "waitMs": min(message.wait_ms, 5000),
                }
                writer = self.live.open_run(self.thread)
                if writer is not None:
                    writer.custom("pisa.voice.turn_hint", value)
                else:
                    self._once(self.thread, lambda w: w.custom("pisa.voice.turn_hint", value))
                return True
            case Present():
                return self._present(message)
            case Card():
                if not self.negotiated.ring1:
                    return False
                surface_id = f"card:{message.card_id}"
                op, fallback = a2ui.card_to_surface(message.model_dump(mode="json"), surface_id, self.lang)
                content = {"a2ui_operations": [op], "pisa": a2ui.surface_pisa(fallback or message.title)}
                return self._in_turn(lambda w: w.surface(surface_id, content))
            case FocusObject():
                value: dict[str, Any] = {"surfaceId": self._surface_of(message.object_id), "mode": "take"}
                if message.highlight:
                    value["highlight"] = a2ui.cut(message.highlight, 80)
                return self._in_turn(lambda w: w.custom("pisa.surface.focus", value))
            case CloseObject():
                surface_id = self._surface_of(message.object_id)
                content = {
                    "a2ui_operations": [a2ui.delete_surface(surface_id)],
                    "pisa": a2ui.surface_pisa("-", focus="quiet"),
                }
                return self._in_turn(lambda w: w.surface(surface_id, content))
            case Directions():
                value = {
                    "directionsId": message.directions_id.lower(),
                    "name": message.name,
                    "lat": message.lat,
                    "lon": message.lon,
                    "mode": message.mode,
                }
                if message.place_id:
                    value["placeId"] = message.place_id.lower()
                return self._in_turn(lambda w: w.custom("pisa.directions", value))
            case Compose():
                recipient: dict[str, Any] = {"name": message.recipient.name}
                if message.recipient.phone:
                    recipient["phone"] = message.recipient.phone
                value = {
                    "composeId": message.compose_id.lower(),
                    "app": message.app,
                    "recipient": recipient,
                    "text": message.text,
                }
                return self._in_turn(lambda w: w.custom("pisa.compose", value))
            case Call():
                value = {"callId": message.call_id.lower(), "name": message.name, "phone": message.phone}
                return self._in_turn(lambda w: w.custom("pisa.call", value))
            case Notify():
                notice = notice_value(message)
                self._once(self.live.notice_thread(), lambda w: w.custom("pisa.notice", notice))
                return True
            case _ if isinstance(message, TalkAskToOpen):  # sent here; made only behind the guardian
                value = {"why": message.why, "line": a2ui.cut(message.line, 300), "lang": message.lang}
                if message.notification_id:
                    value["noticeId"] = message.notification_id.lower()
                self._once(self.live.notice_thread(), lambda w: w.custom("pisa.ask_to_open", value))
                return True
            case ActionDone():
                value = {
                    "approvalId": message.approval_id.lower(),
                    "kind": message.kind,
                    "summary": message.summary or "-",
                    "result": message.result or "-",
                }
                if message.undo_until is not None:
                    value["undoUntil"] = message.undo_until.isoformat()
                self._once(
                    f"pisa-approval:{value['approvalId']}", lambda w: w.custom("pisa.action_done", value)
                )
                return True
            case _:
                _log.warning("live_message_unmapped", kind=getattr(message, "type", "?"))
                return False

    def _in_words(self, message: BaseModel) -> bool:
        """A message whose `pisa.*` event the app did not pick. A notice, or an approved action's outcome, is
        something the user must not lose: Pisa's words, a text message in the run its event would have gone
        in. The rest is not sent: its words are in the reply already (what Pisa understood, the voice, a
        screen brought back), the brain was told it can't open maps, a call or a message here
        (`turn_senders`), and a talk Pisa asks to open stays its notice."""
        match message:
            case Notify():
                notice = notice_value(message)
                words = "\n".join(w for w in (notice.get("title"), notice["text"]) if w)
                if not words:
                    return False
                self._once(
                    self.live.notice_thread(),
                    lambda w: w.text(
                        [words],
                        lang=notice["lang"],
                        message_id=f"notice:{notice['noticeId']}",
                        priority=notice["priority"],
                        urgency=notice.get("urgency"),
                    ),
                )
                return True
            case ActionDone():
                approval_id = message.approval_id.lower()
                words = approval_runs.action_done_words(message.summary, message.result)
                self._once(approval_runs.thread_of(approval_id), lambda w: w.text([words], lang=self.lang))
                return True
            case _:
                return False

    def _say(self, say: Say) -> bool:
        reply_to = say.reply_to
        writer = self._reply_writer(reply_to)
        if writer is False:
            return False
        if reply_to is not None and say.stream_id not in self._streams:
            self._streams[say.stream_id] = reply_to
            while len(self._streams) > STREAMS_KEPT:
                self._streams.popitem(last=False)

        def write(w: RunWriter) -> None:
            if say.holding:
                # a holding line is a message of its own, Pisa's, with an id of its own (a UUID unrelated to
                # the answer's or the user's): the answer is the next message, and nothing of the holding
                # line ever joins another bubble
                w.text(
                    [say.delta.strip() or say.delta],
                    lang=say.lang,
                    message_id=str(uuid.uuid4()),
                    priority=say.priority,
                    holding=True,
                )
                return
            if say.delta and not w.message_open(say.stream_id):
                w.text_start(say.stream_id, lang=say.lang, priority=say.priority)
            if w.message_open(say.stream_id):
                w.text_content(say.stream_id, say.delta)
                if say.final:
                    w.text_end(say.stream_id)

        if writer is not None:
            write(writer)
            return True
        if reply_to is None:
            return self._in_turn(write)
        # a reply that is not a turn's answer (an echo, a fragment joined to the next words, "Ok, dopo."):
        # its run is short and whole
        thread, run_id = self._run_of(reply_to)
        self._once(thread, write, run_id, reply_to)
        return True

    def _audio(self, audio: SayAudio) -> bool:
        value: dict[str, Any] = {
            "seq": audio.seq,
            "format": audio.format,
            "sampleRate": audio.sample_rate,
            "data": audio.data,
            "final": audio.final,
        }
        for key, wire in (
            ("sentence_end", "sentenceEnd"),
            ("complete", "complete"),
            ("spoken_chars", "spokenChars"),
        ):
            if getattr(audio, key) is not None:
                value[wire] = getattr(audio, key)
        if audio.stream_id is not None:
            value["messageId"] = audio.stream_id
            writer = self._reply_writer(self._streams.get(audio.stream_id))
            if not writer:
                return False  # stale audio of a reply that ended is useless
            writer.custom("pisa.voice.audio", value)
            return True
        notice_id = (audio.notification_id or "").lower()
        value["noticeId"] = notice_id
        thread = self.live.notice_thread()
        writer = self._notice_audio.get(notice_id)
        if writer is None or writer.closed:
            current = self.live.open_run(thread)
            if current is not None:
                self._close(current, RunWriter.finish)
            writer = self._notice_audio[notice_id] = self.live.start_run(thread)
        writer.custom("pisa.voice.audio", value)
        if audio.final:
            self._notice_audio.pop(notice_id, None)
            self._close(writer, RunWriter.finish)
        return True

    def _error(self, error: Error) -> bool:
        reply_to = error.reply_to
        writer = self._reply_writer(reply_to)
        if writer is False:
            return False
        if writer is not None:
            self._close(writer, lambda w: w.error(error.message, error.code))
            return True
        if reply_to is not None and reply_to in self._turns:
            thread, run_id = self._run_of(reply_to)
            self._once(thread, lambda w: w.error(error.message, error.code), run_id, reply_to)
            return True
        code = _CONTROL_CODES.get(
            error.code, "invalid_frame" if error.code == "invalid_message" else "internal"
        )
        if error.code not in _CONTROL_CODES and error.code != "invalid_message":
            _log.info("live_error_without_run", code=error.code)
        self.live.control("error", **control_error(code, error.message, error.retryable))
        return True

    def _present(self, present: Present) -> bool:
        body = present.model_dump(mode="json", by_alias=True)
        body["reply_to"] = self._app_id(present.reply_to)
        obj = body["object"]
        if obj["kind"] == "drawing":
            if not self.negotiated.mcp_apps:
                return False
            content = a2ui.mcp_app_from_drawing(obj["data"], present.focus, body.get("expires_at"))
            surface_id = content["resourceUri"]
            self._remember(surface_id, obj["object_id"], present.present_id)
            return self._in_turn(lambda w: w.mcp_app(surface_id, content))
        if not self.negotiated.ring1:
            return False
        surface_id = a2ui.surface_id_for(obj["object_id"])
        content = a2ui.present_content(body, surface_id)
        if content is None:
            return False
        self._remember(surface_id, obj["object_id"], present.present_id)
        return self._in_turn(lambda w: w.surface(surface_id, content))

    def _remember(self, surface_id: str, object_id: str, present_id: str) -> None:
        self._objects[surface_id] = object_id
        self._objects.move_to_end(surface_id)
        self._pending[surface_id] = present_id
        while len(self._objects) > SURFACES_KEPT:
            gone, _ = self._objects.popitem(last=False)
            self._pending.pop(gone, None)

    def _surface_of(self, object_id: str) -> str:
        for surface_id, shown in reversed(self._objects.items()):
            if shown == object_id:
                return surface_id
        return a2ui.surface_id_for(object_id)

    def _object_of(self, surface_id: str) -> str:
        return self._objects.get(surface_id, surface_id)

    # --- what the app sends --------------------------------------------------------------------------

    async def frame(self, frame: dict[str, Any]) -> None:
        """One `op` frame of the app's after its hello (CONTRACT-2 §3.1): `ping`, `run`, `cancel`, `a2ui` or
        `custom`. A frame it can't take raises `FrameError` (the transport answers `invalid_frame`)."""
        op = frame.get("op")
        if op == "ping":
            self.live.control("pong")
        elif op == "run" and isinstance(frame.get("input"), dict):
            await self.run(frame["input"])
        elif op == "cancel" and isinstance(frame.get("runId"), str):
            await self.cancel(frame["runId"])
        elif op == "a2ui" and isinstance(frame.get("message"), dict):
            await self.action(frame["message"])
        elif op == "custom" and isinstance(frame.get("name"), str) and isinstance(frame.get("value"), dict):
            await self.custom(frame["name"], frame["value"])
        elif op == "hello":
            raise FrameError("The session already said hello.")
        else:
            raise FrameError(f"The frame's op {op!r} is unknown or its body is missing.")

    async def _handled(self, message: BaseModel) -> None:
        """A message of the app's, handled by the shared pipeline; a failure goes where its reply's would."""
        try:
            await self.handle(message)
        except PisaError as err:
            await self.send(Error.from_error(err, reply_to=getattr(message, "id", None)))
        except Exception as exc:
            err = failure(
                _log, exc, "message_failed", "Pisa failed to handle it (trace {trace}).",
                device_id=str(self.device.id),
            )  # fmt: skip
            await self.send(Error.from_error(err, reply_to=getattr(message, "id", None)))

    def _turn(self, reply_to: str, thread: str, run_id: str, app_id: str | None = None) -> None:
        self._runs[reply_to] = (thread, run_id)
        self._reply_of_run[run_id] = reply_to
        self._turns.add(reply_to)
        if app_id:
            self._app_ids[reply_to] = app_id

    def _refuse_run(self, thread: str, run_id: str, code: str, message: str) -> None:
        self._once(thread, lambda w: w.error(message, code), run_id)

    async def run(self, body: dict[str, Any]) -> None:
        thread, run_id = body.get("threadId"), body.get("runId")
        if not (
            isinstance(thread, str)
            and 0 < len(thread) <= 120
            and isinstance(run_id, str)
            and 0 < len(run_id) <= 80
        ):
            raise FrameError("A run's input needs a threadId (1-120 characters) and a runId (1-80).")
        if run_id in self._reply_of_run:
            raise FrameError(f"The run {run_id} was already started.")
        if thread.startswith(approval_runs.THREAD_PREFIX) or body.get("resume"):
            await self._resume(thread, run_id, body)
            return
        if thread.startswith("pisa-"):
            self._refuse_run(thread, run_id, "invalid_frame", f"The thread {thread} is the server's.")
            return
        self.thread = thread
        props = body.get("forwardedProps") or {}
        pisa = props.get("pisa") if isinstance(props, dict) else None
        pisa = pisa if isinstance(pisa, dict) else {}
        turn: dict[str, Any] = pisa["turn"] if isinstance(pisa.get("turn"), dict) else {}
        reply_to = str(uuid.uuid4())
        try:
            if isinstance(pisa.get("ask"), dict):
                message = self._ask(reply_to, pisa["ask"])
                words = str(pisa["ask"].get("label") or "")
            else:
                message = self._utterance(body.get("messages"), turn)
                reply_to = message.id
                words = message.text
        except FrameError as err:
            self._refuse_run(thread, run_id, "invalid_frame", str(err))
            return
        why = await self.consent_refused(pisa.get("consent"), words, getattr(message, "lang", None))
        if why is not None:  # before any AI call (CONTRACT-2 §4.4)
            self._refuse_run(thread, run_id, disclosure_rules.CONSENT_REQUIRED, why)
            return
        app_id = self._app_ids.get(reply_to)
        self._turn(reply_to, thread, run_id, app_id)
        await self._handled(message)

    def _ask(self, reply_to: str, ask: dict[str, Any]) -> BaseModel:
        """ "Ask about this" (CONTRACT-2 §8.2): a drawing's view, or a native diagram's element."""
        surface_id, label = str(ask.get("surfaceId") or ""), ask.get("label")
        if ask.get("source") == "mcp-apps":
            drawing_id = surface_id.removeprefix("ui://pisa/drawing/")
            return _message(DrawingAsk, {"id": reply_to, "drawing_id": drawing_id, "label": label})
        return _message(
            ObjectEvent,
            {
                "id": reply_to,
                "object_id": self._object_of(surface_id),
                "event": "asked",
                "element_id": ask.get("elementId"),
            },
        )

    def _utterance(self, messages: Any, turn: dict[str, Any]) -> Utterance:
        said = [m for m in messages or [] if isinstance(m, dict) and m.get("role") == "user"]
        if not said:
            raise FrameError("A run needs the user's message.")
        last = said[-1]
        content = last.get("content")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            raise FrameError("The user's message has no text.")
        app_id = str(last.get("id") or "")
        utterance_id = app_id.lower() if a2ui.is_uuid(app_id) else str(uuid.uuid4())
        if utterance_id in self._runs or utterance_id in self._ended:  # an id the app used before
            utterance_id = str(uuid.uuid4())
        lang = turn.get("lang") or self.lang
        continues = self._reply_of_app(turn.get("continues"))
        source = {"watch_mic": "phone_mic"}.get(turn.get("source") or "typed", turn.get("source") or "typed")
        wake = {"wake_phrase": "hey_pisa"}.get(turn.get("wake") or "", turn.get("wake"))
        utterance = _message(
            Utterance,
            {
                "id": utterance_id,
                "text": content,
                "lang": lang,
                "final": True,
                "source": source,
                "wake": wake if wake in ("hey_pisa", "stem_press", "tap", "reply") else None,
                "continues": continues,
                "voice": turn.get("voice"),
            },
        )
        self.lang = utterance.lang
        if app_id:
            self._app_ids[utterance.id] = app_id
        return utterance

    def _reply_of_app(self, app_id: Any) -> str | None:
        if not isinstance(app_id, str) or not app_id:
            return None
        for reply_to, own in self._app_ids.items():
            if own == app_id:
                return reply_to
        return app_id.lower() if a2ui.is_uuid(app_id) else None

    async def cancel(self, run_id: str) -> None:
        """Barge-in: the run ends `cancelled` now and nothing more is sent for it (CONTRACT-2 §3.1)."""
        reply_to = self._reply_of_run.get(run_id)
        if reply_to is None or reply_to in self._ended:
            return
        writer = self._reply_writer(reply_to)
        if writer:
            self._close(writer, RunWriter.cancelled)
        else:
            thread, _ = self._runs[reply_to]
            self._once(thread, RunWriter.cancelled, run_id, reply_to)
            if self._fragment is not None and self._fragment[0].id == reply_to:  # words still waiting
                self._fragment[1].cancel()
                self._fragment = None
        await self._handled(Cancel(target_id=reply_to))

    async def action(
        self, message: dict[str, Any], run_id: str | None = None, thread: str | None = None
    ) -> None:
        """An A2UI action (`op: a2ui`; over HTTP, `forwardedProps.pisa.a2uiAction` with the request's
        `run_id` and `thread`: the action's run takes that id on that conversation thread, and an action that
        starts no turn still gets its short run)."""
        if thread is not None:
            self.thread = thread
        if "error" in message and isinstance(message["error"], dict):
            err = message["error"]
            _log.warning(
                "app_a2ui_error", code=str(err.get("code"))[:40], surface=str(err.get("surfaceId"))[:80]
            )
            return
        action = message.get("action")
        if message.get("version") != "1.0" or not isinstance(action, dict):
            raise FrameError("An a2ui frame carries an A2UI 1.0 action or error.")
        name, context = action.get("name"), action.get("context") or {}
        if name not in ACTIONS or not isinstance(context, dict):
            raise FrameError(f"The action {name!r} is not one of the pisa.* actions.")
        surface_id = str(action.get("surfaceId") or "")
        object_id = self._object_of(surface_id)
        event_id = str(uuid.uuid4())
        if name == "pisa.sheet_action":
            await self._sheet_action(object_id, str(context.get("actionId") or ""), run_id)
            return
        if name == "pisa.handoff_opened":
            body = {"handoff_id": _lower_uuid(str(context.get("handoffId") or "")), "object_id": object_id}
            await self._handled(_message(HandoffOpened, body))
            if run_id is not None:
                self._once(self.thread, lambda w: None, run_id)
            return
        fields: dict[str, Any] = {"id": event_id, "object_id": object_id}
        if name == "pisa.choose":
            fields |= {"event": "chosen", "option_id": context.get("optionId")}
        elif name == "pisa.ask":
            fields |= {"event": "asked", "element_id": context.get("elementId")}
        elif name == "pisa.undo":
            fields |= {"event": "acted", "action": "undo"}
        else:  # pisa.act
            fields |= {"event": "acted", "action": context.get("action")}
        event = _message(ObjectEvent, fields)
        if event.event in ("chosen", "asked"):  # a tap that starts a turn: its own run on the conversation
            why = await self.consent_refused(None, None)
            if why is not None:
                self._refuse_run(self.thread, run_id or new_run_id(), disclosure_rules.CONSENT_REQUIRED, why)
                return
            self._turn(event_id, self.thread, run_id or new_run_id())
        await self._handled(event)
        if event.event not in ("chosen", "asked") and run_id is not None:
            self._once(self.thread, lambda w: None, run_id)

    async def _sheet_action(self, sheet_id: str, action_id: str, run_id: str | None = None) -> None:
        """A ritual sheet's button (as `POST /v1/sheets/{id}/actions/{a}`): what Pisa says, in a short run."""
        try:
            said = await self.runtime.rituals.act(sheet_id, action_id, "tap")
        except PisaError as err:
            refused = err
            self._once(self.thread, lambda w: w.error(refused.message, refused.code), run_id)
            return
        if said:
            self._once(self.thread, lambda w: w.text([said], lang=self.lang), run_id)
        elif run_id is not None:
            self._once(self.thread, lambda w: None, run_id)

    async def custom(self, name: str, value: dict[str, Any]) -> None:
        if name == "pisa.presence":
            state = value.get("state")
            if isinstance(state, dict):
                state = {{"glassesWorn": "glasses_worn"}.get(k, k): v for k, v in state.items()}
                if state.get("route") in ("airpods", "headphones"):
                    state["route"] = {"airpods": "earbuds", "headphones": "other"}[state["route"]]
            await self._handled(_message(Presence, {"event": value.get("event"), "state": state}))
        elif name == "pisa.location":
            await self._handled(
                _message(
                    Location,
                    {
                        "lat": value.get("lat"),
                        "lon": value.get("lon"),
                        "accuracy_m": value.get("accuracyM"),
                        "measured_at": value.get("measuredAt"),
                    },
                )
            )
        elif name == "pisa.place_enter":
            await self._handled(_message(PlaceEnter, {"note_ids": value.get("noteIds")}))
        elif name == "pisa.device_context":
            body = {
                "tz": value.get("tz"),
                "region": value.get("region"),
                "hour_cycle": value.get("hourCycle"),
            }
            await self._handled(_message(DeviceContext, body))
        elif name == "pisa.surface.ack":
            surface_id = str(value.get("surfaceId") or "")
            present_id = self._pending.pop(surface_id, None)
            if present_id is None:
                _log.info("surface_ack_unknown", device_id=str(self.device.id))
                return
            surface = {"watch": "phone"}.get(str(value.get("surface")), value.get("surface"))
            await self._handled(
                _message(
                    Presented,
                    {
                        "present_id": present_id,
                        "object_id": self._object_of(surface_id),
                        "status": value.get("status"),
                        "surface": surface,
                        "reason": value.get("reason"),
                    },
                )
            )
        elif name == "pisa.surface.event":
            body = {
                "object_id": self._object_of(str(value.get("surfaceId") or "")),
                "event": value.get("event"),
            }
            if body["event"] not in ("seen", "expanded", "kept", "closed"):
                raise FrameError("A surface event is seen, expanded, kept or closed.")
            await self._handled(_message(ObjectEvent, body))
        elif name == "pisa.voice.partial":
            if not self.negotiated.turn_hints:
                raise FrameError("Partial transcripts need the pisa.voice.turn_hints extension.")
            turn_id = str(value.get("turnId") or "")
            partial_id = turn_id.lower() if a2ui.is_uuid(turn_id) else str(uuid.uuid4())
            if turn_id:
                self._partials[partial_id] = turn_id
                while len(self._partials) > PARTIALS_KEPT:
                    self._partials.popitem(last=False)
            await self._handled(
                _message(
                    Utterance,
                    {
                        "id": partial_id,
                        "text": value.get("text"),
                        "lang": value.get("lang"),
                        "final": False,
                        "source": "phone_mic",
                    },
                )
            )
        else:
            raise FrameError(f"The event {name!r} is not one the app sends.")

    async def catch_up(self) -> None:
        """What changed since the device was last told: the AI disclosure (`pisa.ai.disclosure`, so the app
        asks again) and the server's endpoints (`pisa.endpoints`, so it trusts the next pin). A device never
        told yet reads both over REST (its consent screen, the pairing QR): it is marked told, not sent
        them."""
        runtime = self.runtime
        with degrades(_log, "catch_up", device_id=str(self.device.id)):  # never fails the connection
            async with runtime.pool.connection() as conn:
                told = await devices_service.told(conn, self.device.id)
                if told is None:
                    return
                ai_told, endpoints_told = told
                current = await disclosure.current(conn, runtime.settings.ollama_base_url)
                if ai_told != current["revision"]:
                    if ai_told is not None:
                        self.tell("pisa.ai.disclosure", current)
                    await disclosure.told(conn, self.device.id, current["revision"])
                try:
                    ends = endpoints.current(runtime.settings)
                except PisaError:
                    return  # none configured: nothing to tell
                if endpoints_told != ends["revision"]:
                    if endpoints_told is not None:
                        self.tell("pisa.endpoints", ends)
                    await devices_service.endpoints_told(conn, self.device.id, ends["revision"])
