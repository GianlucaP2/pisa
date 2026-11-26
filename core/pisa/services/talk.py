"""Pisa talks through a live voice model. `TalkService` opens a conversation (the talker's
session, the phone's WebRTC link, the ledger's row) and `TalkRun` runs it: the user's audio to the talker,
Pisa's voice to the phone, the user's and Pisa's words on the data channel, and Pisa's brain (the conversation
service, all its tools and rails) answering in whispers.

- **The brain runs on the user's final words** (speculation on partial words is). Its answer is one
  whisper: `done` only when code finished an action in that turn (an `act` stored with the turn's trace id, or
  an approval executed), `ask` when Pisa asks something (said verbatim), else `say`. It answers the talker's
  pending `ask_brain` call, or goes in by itself (PAUSE when the talker hasn't answered, SILENT when it
  already did). A spoken yes/no to Pisa's question is decided by code in the brain's own path, from the
  final words.
- **Fresh** (the time, where the user is, the plan's next item, the weather, Pisa's open questions) seeds the
  session and is re-sent SILENT when it changes; the day's summary seeds it too (continuity is Pisa's).
- **Grounding** (RAIL): Pisa's words are checked against what it was given; a "fatto" with no `done` is
  corrected at once, every flag is an incident (counted, never the words).
- **Lifecycle**: the session closes after 30 s of silence (`idle`), at the user's `talk.end` (`owner`), over
  a cap (`budget`), on a failure (`error`), or when the talker drops (`fallback`: the app's economy path goes
  on). Words said as the user ended it are never answered after their own end; after a lost peer they are,
  once (into the talk's turn while the app is open, else as a notice).
- **Heard once, kept as heard**: what a turn shows goes on the talk's channel only (the talk's turn id); a
  brain reply that goes in silently leaves the turn's stored reply as the talker said it (`Hooks.spoken`);
  words typed into the talk (`talk.text`) are the user's turn, answered by voice.
- **Spend**: the talker's minutes in the ledger (slot `voice.talk`), reserved a minute at a time under the
  caps.

Logs carry ids and times only.
"""

import asyncio
import re
import time
import uuid
from array import array
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from wordfreq import zipf_frequency

from pisa.brain import history
from pisa.brain.research_turn import WORKING
from pisa.db.settings import get_setting
from pisa.domain import actions as action_rules
from pisa.domain import clean as clean_rules
from pisa.domain import google_tier, restate, speculation, understand, yescard
from pisa.domain import self as self_rules
from pisa.domain import talk as form
from pisa.domain import turn as turn_rules
from pisa.domain import visual as visual_rules
from pisa.domain import weather as weather_rules
from pisa.domain.claims import talker_claims_change
from pisa.domain.errors import PisaError
from pisa.domain.latency import MODEL_READS
from pisa.domain.learning import sensitive_place
from pisa.domain.owner import Owner
from pisa.domain.ready import NOT_NEXT, Hit
from pisa.domain.reflex import rules_read
from pisa.domain.settings import is_quiet
from pisa.domain.sheet import OBJECT_KINDS
from pisa.domain.state import FACTS_FOR, grows
from pisa.domain.tasks import is_short_yes
from pisa.domain.voice_gate import dropped as gate_drop
from pisa.domain.whisper import (
    NOT_DONE,
    OWN_WORDS,
    Fresh,
    Kind,
    Priority,
    Whisper,
    adds_fact,
    grounding,
    holding_line,
    instructions,
    own_words,
)
from pisa.failures import external_failure, reason_of, redact
from pisa.infra import hard_limit, ledger
from pisa.infra.fx import usd_to_eur
from pisa.infra.hard_limit import HardLimits
from pisa.infra.keys import KeyStore
from pisa.infra.spend import period_starts
from pisa.logging import get_logger
from pisa.ports.talker import (
    AskBrain,
    Audio,
    Closed,
    OwnerSpeaking,
    OwnerWords,
    PisaWords,
    Resumable,
    Speaking,
    Talker,
    TalkerConfig,
    TalkerError,
    TalkerSession,
)
from pisa.services import devices, disclosure, notifications
from pisa.services.plan_store import stored_current
from pisa.services.reflex import Reflexed
from pisa.services.voice_id import RATE as GATE_RATE
from pisa.services.voice_id import LiveGate, VoiceIdService, run_enrolment

if TYPE_CHECKING:
    from pisa.services.ready import ReadyService
    from pisa.services.reflex import ReflexService

SLOT = "voice.talk"
IDLE_AFTER_S = 30.0  # FACT: the session closes after 30 s of silence
# the user's voice arriving keeps a session open, but not
# past this long with nothing at all from the talker (a TV: the talker hears nothing to answer).
# the user's voice ran this long with no words from the talker:
# their turn is
# ended by hand, so it transcribes; a second time with still no words, Pisa says once that it can't hear them
HAND_END_S = 12.0
# a final this soon after an `interrupted` line it grows, in the same turn, is its retry
RETRY_S = 2.0
# the user's voice with no words at all (a TV): Pisa says it can't hear them after 2 minutes and
# closes the talk after 3, with the reason in its timeline
LAST_FINAL_WAIT_S = 1.5  # on Gemini: the final of words the user said as they closed
# Gemini sends no partial words: their voice this recent at the close is words still unsaid
LAST_VOICE_S = 2.0
UNHEARD_SAY_S = 120.0
UNHEARD_CLOSE_S = 180.0
UNHEARD_VOICE = {
    "it-IT": "Non ti sento bene, puoi ripetere?",
    "en-US": "I can't hear you well, can you say that again?",
}
TICK_S = 1.0  # how often the session checks its silence, its minute and Fresh
# The user stopped and no final came (noise): their words are no longer "being heard" after this long.
OWNER_FINAL_WAIT_S = 3.0
# A final the same as the previous one is the talker's repeat while that one is unanswered, or this recent
# with no answer from the talker since; else it is the user saying it again ("sì" … "sì").
REPEAT_WINDOW_S = 5.0
WORD_ZIPF = 3.0  # a word this common (wordfreq's Zipf scale) is a word, never a transcript's cut one
METER_EVERY_S = 60.0  # the ledger reserves the talker a minute at a time
FRESH_EVERY_S = 60.0
HOLD_WHISPER_AFTER_S = 2.5  # Pisa's brain still working after this: a HOLD whisper shapes the talker's filler
_SPOKEN_KINDS = frozenset({Kind.SAY, Kind.ASK, Kind.CORRECT, Kind.DONE})
ASK_WAIT_S = 3.0  # an ask_brain before the user's final words waits this long for them
ADDRESSED_WAIT_S = 2.0  # a bare yes or no counts as said to Pisa when the talker answers it within this
# a spoken yes counts within 10 s of Pisa saying the card aloud (later, Pisa asks it again)
YES_WINDOW_S = yescard.READ_OUT_GRACE.total_seconds()
ROUTE_RECENT = timedelta(minutes=30)  # the last talk's route still counts this long after it
ASK_FRESH = timedelta(minutes=10)  # a talk opens with Pisa's asked line only this soon after
RESUME_FOR = timedelta(hours=2)  # a provider's resumption handle is kept this long (Gemini's 2 h)
SAID_KEPT = 4  # Pisa's words kept for the latest finals only (`_said_for` is bounded)
GIVEN_KEPT = 12  # the whispers grounding checks Pisa's words against
TIMELINE_MAX = 4000  # a session's timeline keeps this many events (a long session: the rest counted)
TIMELINE_TEXT = 300  # words in the timeline are cut to this many characters
VOICE_PEAK = 1500  # FACT: a second of the phone's audio with a sample above this (of 32767) carries a voice
# the user's voice stopped this long ago and the talker gave no sign it heard it (no words, no
# start of speech): noted once. Never while the voice goes on: Gemini transcribes a monologue at its end.
DEAF_AFTER_S = 3.0
# `route`: a gated talk's route changed (or their prints went): the phone reopens it
EndReason = Literal["idle", "owner", "budget", "error", "fallback", "route"]
# who ended a session: the phone (talk.end or the DELETE, with its reason), a new session for the
# device, the phone's peer gone, the talker (with the provider's reason), the silence, a cap, a failed part,
# Pisa's shutdown.
ClosedBy = Literal["phone", "replaced", "peer", "talker", "idle", "budget", "error", "server"]
BARGE_ECHO_S = 3.0  # a short final this soon after a barge-in on Pisa's line, inside that line, is its echo
# the talker's start of speech while Pisa speaks is the user's only when the
# phone's audio carried a voice (a sample at VOICE_PEAK) this recently; else it is Pisa's own voice heard back
BARGE_VOICE_S = 1.0
# the phone's audio comes every 20 ms: a gap this long ends a stretch of it (less is too tight
# for cellular jitter)
FRAME_GAP_S = 0.5
# no audio from the phone for this long (the talk's idle timeout): its talk link is gone, and the
# phone's spoken utterances are answered on the socket again
LINK_SILENT_S = 30.0
# an ask_brain whose user's words have begun waits for their final this long at most
# (their turn ended by hand after as long: HAND_END_S); "unheard" only when nothing of theirs came
ASK_WORDS_WAIT_S = 12.0
# a whole line of at least this many words, said again whole for the same final words: cut;
# a shorter one ("Va bene.") is never judged
LINE_AGAIN_WORDS = 4
# with the gate on, Pisa's answer to a turn of theirs waits this long at most for that turn's
# words
# (Gemini transcribes at the turn's end): a turn Pisa drops gets no reply at all, so none of it is played
# before its words are judged; with no words by then, the answer plays
JUDGE_WAIT_S = 1.0
# the talker's ask_brain call on a dropped turn gets this, silent (never said)
DROPPED_RESULT = "Those words were not for Pisa: say nothing about them."
# their open turn with no audio from the phone this long closes; a gated talk with none this long
# ends (the gated counterpart of today's deaf and unheard closes)
GATE_NO_FRAMES_CLOSE_S = 2.0
GATE_NO_FRAMES_END_S = 5.0
PHONE_REASONS = frozenset({"press", "call", "route", "background", "idle", "error"})  # talk.end's `reason`
# the phone's peer closed with no talk.end: its talk.end (or the DELETE) may still follow this soon, and then
# the last words are the user's to drop
PEER_END_GRACE_S = 2.0
ENDED_KEPT = 64  # the talks lately ended, kept for a late DELETE ...
ENDED_FOR_S = 600.0  # ... this long
REVOKE_WAIT_S = 5.0  # a revoked device's talk: its close is waited for this long at most
State = Literal["listening", "thinking", "speaking", "idle"]
_log = get_logger("pisa.talk")
_ELEMENT = re.compile(r"n\d{1,2}|e\d{1,2}|s[1-4]/p\d{1,2}|t\d{1,2}")  # a drawing's element id


@dataclass(frozen=True)
class Thought:
    text: str
    kind: Kind  # DONE, ASK or SAY
    claimed_done: bool = False  # the reply claims a change and Pisa's record backs it (code's mark)
    # `text` is a shortening of the brain's reply: it can't hold Pisa's question
    shortened: bool = False
    parts: dict[str, Any] | None = None  # where the brain's time went (ms, tokens)
    # the brain's record of the turn: tools that said it wasn't done, claims it rewrote
    failed: int = 0
    unbacked: int = 0
    # the one correction line said after `text` (a claim the record didn't back, a task's state the store
    # contradicts): its own whisper and its own message, never spliced into the reply
    correction: str = ""


# (the user's final words, the turn's trace id, whether a bare yes may answer Pisa's question; by keyword
# `origin="drawing"` for a tap in a drawing) → the answer
Think = Callable[..., Awaitable[Thought]]


def peer_gone(link: "Link") -> tuple[EndReason, ClosedBy, str]:
    """Why a talk ends when its peer closes: media that never connected (no path for the phone's UDP, ICE
    failed or timed out: `MediaPeer.unreachable`) is `fallback`, voice unavailable and the phone carries on
    over /v2/live; a peer lost after it connected is `error`."""
    if getattr(link, "unreachable", False):
        return "fallback", "peer", "the phone's media never connected (ICE): voice unavailable, text goes on"
    return "error", "peer", "the phone's WebRTC peer failed or closed"


class Link(Protocol):
    """The phone's side of one conversation (pisa.services.talk_media.MediaPeer; a fake in tests)."""

    messages: asyncio.Queue[dict[str, Any]]
    on_closed: Callable[[], None] | None  # the peer failed or closed: the phone is gone

    def audio_in(self, rate: int) -> AsyncIterator[bytes]: ...

    def play(self, pcm: bytes) -> None: ...

    def clear(self) -> None: ...

    @property
    def playing(self) -> bool: ...

    @property
    def buffered_ms(self) -> int: ...

    def send(self, message: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


@dataclass
class Timeline:
    """What happened in one session, event by event, each with its offset in ms from the start. The
    user's and Pisa's words are in it (read as in the study report); never the audio."""

    started: float = field(default_factory=time.monotonic)
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0

    def add(self, kind: str, **data: Any) -> None:
        if len(self.events) >= TIMELINE_MAX and kind != "close":  # the close always fits
            self.dropped += 1
            return
        event: dict[str, Any] = {"ms": int((time.monotonic() - self.started) * 1000), "kind": kind}
        for key, value in data.items():
            if key in ("ms", "kind"):  # a field so named would overwrite the event's own offset or kind
                key = f"its_{key}"
            if value is not None:
                event[key] = value[:TIMELINE_TEXT] if isinstance(value, str) else value
        self.events.append(event)

    def stored(self) -> dict[str, Any]:
        return {"events": self.events, "dropped": self.dropped}


def peak(pcm: bytes) -> int:
    """The loudest sample of 16-bit PCM (every fourth one: enough to tell a voice from silence)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    sampled = samples[::4]
    return max((abs(v) for v in sampled), default=0)


@dataclass
class Exchange:
    """One owner turn and Pisa's answer, for the measures (ids and times only)."""

    n: int
    owner_end_at: datetime
    owner_end: float  # monotonic
    final_at: float | None = None  # the user's final words arrived
    first_sound_ms: int | None = None
    whisper_ms: int | None = None
    whisper_kind: str | None = None
    asked_brain: bool = False
    # the first words Pisa's voice was given to say for this turn (the reflex's ready answer or the
    # brain's), ms after the user stopped; and the reflex's latest read time
    first_content_ms: int | None = None
    first_content_by: str | None = None
    reflex_ms: int | None = None
    reflex_by: str | None = None  # who read it (model, slow, rules, final; the window)
    route: str | None = None  # where the user heard Pisa in this turn
    parts: dict[str, Any] | None = None  # the brain's time by part
    # the clean turn: tools that said it wasn't done, claims without a record (the brain's
    # rewritten ones and the talker's "fatto" with no done), their next words corrected or repeated these
    failed: int = 0
    unbacked: int = 0
    redone: bool = False
    ready: str | None = None  # "hit" (a ready answer answered) or "stale"


@dataclass
class Hooks:
    """What a run needs from Pisa beyond the talker and the phone (TalkService's, a fake's in tests)."""

    think: Think
    fresh: Callable[[], Awaitable[Fresh]]
    meter: Callable[[], Awaitable[bool]]  # another minute reserved; False over a cap
    exchange: Callable[[Exchange], Awaitable[None]]  # store one exchange's measures
    # their next final corrected or repeated exchange n (already stored): mark it redone
    redone: Callable[[int], Awaitable[None]] = lambda n: _nothing()
    # Pisa's ready answer for these final words (a hit, stale, or none), and Pisa changed
    # something (its ready answers are rebuilt)
    ready_answer: Callable[[str], Awaitable[Hit | None]] = lambda words: _no_hit()
    changed: Callable[[], None] = lambda: None
    resumable: Callable[[str], None] = lambda handle: None
    # the shared conversation state (pisa.services.reflex): the user's partial words to the reflex,
    # its ready answer for the final words, Pisa's voice lines into the state, what the talker may say from it
    heard: Callable[[str], None] = lambda words: None
    ready: Callable[[str], str | None] = lambda words: None
    # no ready answer from partial words (natural mode: Gemini gives none): the reflex reads the
    # final words once and drafts from Pisa's own data (≈1.5 s), None when it doesn't answer them
    ready_final: Callable[[str], Awaitable[str | None]] = lambda words: _no_words()
    # (text, backed, retried): retried is the talker's retry of its `interrupted` line
    said: Callable[[str, bool, bool], None] = lambda text, backed, retried: None
    state: Callable[[], str] = lambda: ""
    supports: Callable[[], list[str]] = lambda: []
    # a tap on a shown choice (its object id, the option) → the user's words for it ("[tap] o2:
    # …"), None when no longer open; a tap on a shown approval (its id, yes) → (Pisa's line, whether it was
    # done)
    chosen: Callable[[str, str], Awaitable[str | None]] = lambda object_id, option: _no_words()
    # a tap on a drawing's element (its object id, the element id) → the turn's words
    # ("Sul disegno: …"), None when gone
    asked: Callable[[str, str], Awaitable[str | None]] = lambda object_id, element_id: _no_words()
    decide: Callable[[str, bool], Awaitable[tuple[str, bool]]] = lambda approval_id, yes: _not_open()
    first_said: Callable[[], Awaitable[None]] = lambda: _nothing()  # Pisa's own first line was said
    # Pisa's question was just said aloud: the cards it asks are read out now
    asked_aloud: Callable[[], Awaitable[None]] = lambda: _nothing()
    # the route moved: stored on the session (the "the last talk's route" reads it)
    routed: Callable[[str], Awaitable[None]] = lambda route: _nothing()
    # the user's last words, said as they closed the talk (a final whose thought was under way,
    # finals not yet thought, or partial words when the press came): handed on as the talk closes, and
    # answered after it only when the peer was lost (TalkService._after). (the words, the thought under way
    # for them or None)
    after: Callable[[str, "asyncio.Future[Thought] | None"], None] = lambda words, thought: None
    # the brain's reply to a turn went in silently (Pisa's voice had answered by itself): what the voice said
    # for those words becomes the turn's stored reply, so the chat holds what was heard. (the turn's trace id,
    # the lines said; called again as more lines for those words are said)
    spoken: Callable[[str, list[str]], Awaitable[None]] = lambda trace_id, lines: _nothing()


async def _nothing() -> None:
    return None


async def _no_words() -> str | None:
    return None


async def _no_hit() -> Hit | None:
    return None


async def _not_open() -> tuple[str, bool]:
    return "", False


class TalkScreen:
    """While a talk is open, the talk's data channel is the screen Pisa shows on
    (present_tools.Device), never the WebSocket too. There is no acknowledgment: the talk's phone is in
    front."""

    def __init__(
        self, link: "Link", kinds: frozenset[str], lang: str, blocks: frozenset[str] | None = None
    ) -> None:
        self._link = link
        self.kinds = kinds
        self.blocks = blocks  # the drawing blocks the phone draws (None: none)
        self._lang = lang
        self._shows: dict[str, dict[str, Any]] = {}  # show id → the object shown (still on the screen)
        self._latest: dict[str, str] = {}  # object id → its latest show id
        self._answered: set[str] = set()  # object ids whose choice a tap answered
        self.in_turn = 0  # objects shown since `turn()`: Pisa's voice then says one line for them

    def turn(self) -> None:
        self.in_turn = 0

    def _send(self, show_id: str, obj: dict[str, Any], purpose: str | None, focus: str | None) -> None:
        message: dict[str, Any] = {"type": "talk.present", "id": show_id, "object": obj}
        if purpose:
            message["purpose"] = purpose[:120]
        if focus:
            message["focus"] = focus
        object_id, revision = str(obj.get("object_id")), int(obj.get("revision") or 0)
        older = [k for k, o in self._shows.items() if str(o.get("object_id")) == object_id
                 and int(o.get("revision") or 0) < revision]  # fmt: skip
        for key in older:  # a newer revision replaces it: a tap on the old answers nothing
            del self._shows[key]
        if older:
            self._answered.discard(object_id)  # the new revision asks afresh
        self._shows[show_id] = obj
        self._latest[object_id] = show_id
        self.in_turn += 1
        self._link.send(message)

    async def present(self, message: dict[str, Any]) -> tuple[str, str] | None:
        self._send(str(message["present_id"]), dict(message["object"]), message.get("purpose"),
                   message.get("focus"))  # fmt: skip
        return ("shown", "phone")

    async def focus(self, present_id: str, object_id: str, highlight: str | None) -> tuple[str, str] | None:
        shown = self._latest.get(object_id)
        if shown is None:
            return ("unavailable", "none")
        self._send(present_id, self._shows[shown], None, "take")
        return ("shown", "phone")

    async def close(self, object_id: str) -> bool:
        shown = self._latest.pop(object_id, None)
        if shown is None:
            return False
        for key in [k for k, o in self._shows.items() if str(o.get("object_id")) == object_id]:
            del self._shows[key]  # dismissed: a tap on it answers nothing
        self._link.send({"type": "talk.dismiss", "id": shown})
        return True

    async def card(self, kind: str, title: str, lines: list[str]) -> bool:
        """Today's `card` as a `note` object on the channel (plan and info are info; a warning stays one)."""
        self.note(title, lines, "warning" if kind == "warning" else "info")
        return True

    def custom(self, name: str, value: dict[str, Any]) -> None:
        """A `pisa.*` event of the talk's turn (what Pisa understood, a phone action), in the exchange's run
        on the talk's channel: never on the live session too."""
        self._link.send({"type": "talk.custom", "name": name, "value": value})

    def surface(self, surface_id: str, content: dict[str, Any]) -> None:
        """A card of the talk's turn (a place, the sources), in the exchange's run on the talk's channel."""
        self._link.send({"type": "talk.surface", "surface_id": surface_id, "content": content})

    def note(self, title: str, lines: list[str], kind: str = "info") -> None:
        kept = [line[:200] for line in lines if line.strip()][:6] or [title[:200]]
        obj = {"object_id": f"note:{uuid.uuid4()}", "kind": "note", "revision": 0,
               "data": {"title": title[:120], "lines": kept, "kind": kind}}  # fmt: skip
        self._send(str(uuid.uuid4()), obj, None, "offer")

    def shown(self, show_id: str) -> dict[str, Any] | None:
        """The object a show id put on the screen in this talk and still there (a tap names it), or None."""
        return self._shows.get(show_id)

    def answered(self, object_id: str) -> bool:
        """A tap already answered this choice (once: a second tap answers nothing)."""
        return object_id in self._answered

    def answer(self, object_id: str) -> None:
        self._answered.add(object_id)


def _worse_read(e: Exchange, by: str, ms: int) -> bool:
    """Whether this reflex pass replaces the turn's recorded one: a model read over any other pass, a read
    past the window over one inside it, the slower of two alike; with no model read yet, the latest pass."""
    kept = e.reflex_by
    if kept not in MODEL_READS:
        return True
    if by not in MODEL_READS:
        return False
    if (by == "slow") != (kept == "slow"):
        return by == "slow"
    return ms > (e.reflex_ms or 0)


@dataclass
class TalkRun:
    session_id: uuid.UUID
    talker: TalkerSession
    link: Link
    hooks: Hooks
    lang: str
    fresh: Fresh
    first_text: str = ""  # the words said with the wake phrase, the user's first final words
    reason: EndReason | None = None
    closed_by: ClosedBy | None = None
    close_detail: str = ""
    incidents: int = 0
    timeline: Timeline = field(default_factory=Timeline)
    # the phone's audio this second (frames, bytes, the loudest sample), and Pisa's
    _in_frames: int = 0
    _in_bytes: int = 0
    _in_peak: int = 0
    _out_bytes: int = 0
    _voice_at: float | None = None  # the user's voice last arrived in the phone's audio (monotonic)
    _voice_since: float | None = None  # the user's voice has arrived since, with the talker hearing nothing
    _heard_at: float = 0.0  # the talker last showed it heard the user (words, or the start of speech)
    _run_since: float | None = None  # the user's voice has run since, with no words from the talker
    _hand_ends: int = 0  # the user's turn ended by hand, in a row with no words after
    _unheard_said: bool = False  # "Non ti sento bene" said (once a session)
    _wordless_since: float | None = None  # the user's voice has come since, and no word of it
    # Pisa's last final line (text, when, the final it answers, cut by the talker)
    _pisa_final: tuple[str, float, int, bool] | None = None
    _closing: bool = False  # the talk is closing (awaiting the user's last final)
    # its device revoked, or its conversation deleted: ended at once, no last final, no last words
    _revoked: bool = False
    _partial: str = ""  # the user's partial words since their last final
    _final_events: int = 0  # the talker's finals of the user's words so far (empty ones too)
    _state: State | None = None
    _last_activity: float = field(default_factory=time.monotonic)
    _words: asyncio.Queue[tuple[str, float, int, float]] = field(default_factory=asyncio.Queue)
    _calls: list[tuple[str, int]] = field(default_factory=list)  # ask_brain calls: (id, the final they await)
    _final: asyncio.Event = field(default_factory=asyncio.Event)  # set (and replaced) at each final
    _responded: asyncio.Event = field(default_factory=asyncio.Event)  # set (and replaced) when Pisa responds
    _responded_at: float = 0.0  # when Pisa last started speaking or asked its brain (monotonic)
    _given: list[Whisper] = field(default_factory=list)
    _current: list[Whisper] = field(default_factory=list)  # this exchange's whispers
    _owner_words: str = ""
    _owner_words_n: int | None = None  # the exchange their latest final words were in
    _waiting_after_stop: set[int] = field(default_factory=set)  # finals a call made after talk.stop waits on
    _thinking: bool = False
    _pending_tap: tuple[str, str] | None = None  # a tap on a drawing waiting for a thought
    # the finals a tap in a drawing made (their numbers): a flag carried to
    # `respond` as `origin: drawing`, never read from the words, never joined to their words
    _drawing_finals: set[int] = field(default_factory=set)
    # The user's latest final words, normalised: Grok sends one utterance's final several times, repeated or
    # grown. A repeat is dropped; words that grow the
    # previous final before its thought arrived replace it (its thought is cancelled).
    _last_final: list[str] = field(default_factory=list)
    _last_final_at: float = 0.0
    _superseded: set[int] = field(default_factory=set)
    _think_task: "asyncio.Future[Thought] | None" = None
    _thinking_on: int = 0
    _speaking: bool = False
    _finals: int = 0  # the user's final words so far
    _thought_for: int = 0  # the final words the brain's latest answer answered
    _said_for: dict[int, list[str]] = field(default_factory=dict)  # Pisa's final words after each final
    _line_for: int | None = None  # the final Pisa's current line answers (set when the line starts)
    _owner_open: bool = False
    _owner_stopped_at: float | None = None  # the user's words are being heard (partials since the last final)
    _unchecked: list[tuple[str, bool]] = field(default_factory=list)  # done claims awaiting this thought
    _wrong_before: set[int] = field(default_factory=set)  # finals the talker said its own facts on
    # finals Pisa already corrected the talker on (once each)
    _corrected: set[int] = field(default_factory=set)
    _dropped_upto: int = 0  # talk.stop: the brain's answers up to these final words are dropped
    _stopped: bool = False  # a talk.stop since the user's last final words
    _ask: Whisper | None = None  # the latest ASK whisper, waiting to be spoken
    _ask_spoken_at: float | None = None  # when Pisa said it
    _sent_ms: int = 0  # Pisa's audio received in this response (for truncate)
    _whispers: int = 0
    _exchange: Exchange | None = None
    _n: int = 0
    _ended: asyncio.Event = field(default_factory=asyncio.Event)
    _asking: set[asyncio.Task[None]] = field(default_factory=set)
    _state_sent: str = ""  # the shared state as Pisa's voice last got it
    _ready_for: dict[int, Whisper] = field(default_factory=dict)  # the reflex's ready answer given per final
    # Pisa's voice heard back (a speaker, the simulator) is never the user's words, each a new turn: Pisa's
    # recent lines: (when, words, the words of its questions)
    _pisa_lines: list[tuple[float, list[str], list[str]]] = field(default_factory=list)
    _pisa_line: str = ""  # Pisa's line being said now (its partial words)
    _barged_at: float = 0.0  # the user's voice last cut Pisa's line
    _barge_echo: bool = False  # that barge-in had no voice in the phone's audio (Pisa's echo)
    _barge_line: list[str] = field(default_factory=list)  # … and the words of the line it landed on
    _barge_asked: list[str] = field(default_factory=list)  # … the words of what it asked the user
    _frame_at: float | None = None  # the phone's audio last arrived (monotonic) …
    _opened_at: float = field(default_factory=time.monotonic)  # … or, before any, the run began
    _flow_since: float | None = None  # … and has arrived without a gap since
    _words_at: float = 0.0  # the user's partial words last arrived …
    _words_for: int = 0  # … for this final (the one after the last final)
    _closed_words: int = 0  # empty finals: the user's words closed with nothing in them
    # Pisa's whole lines per final (their words, and when said); a whisper given since carries new words
    _whole_for: dict[int, list[tuple[list[str], float]]] = field(default_factory=dict)
    _whispered_at: float = 0.0  # the talker last got words to say (no HOLD)
    _own_fixed: set[int] = field(default_factory=set)  # finals Pisa corrected the talker's own words on
    _line_cut: bool = False  # Pisa's current line was cut already (by the user's voice or words)
    _muted: bool = False  # the user's words cut Pisa's line: the rest of its audio isn't played
    screen: TalkScreen | None = None  # what Pisa shows while talking
    last_turn: str | None = None  # the talk's turn id its last words belong to (set as it closes)
    first_line: str = ""  # Pisa's own line, said first in a talk opened for it
    route: str | None = None  # where the user hears Pisa now
    # the user-voice gate (None: today's path). With it only their turns reach the talker
    gate: LiveGate | None = None
    _judging: bool = False  # their turn closed and its words aren't judged yet: Pisa's answer is held
    _held_audio: list[bytes] = field(default_factory=list)
    _silenced: bool = False  # their last turn was dropped: no reply to it at all
    _judge_timer: asyncio.TimerHandle | None = None
    # their gated turn's latest interim words' language (Gemini's `languageCode`), judged
    # on at JUDGE_WAIT_S when no final came;: the turn's judgement, once made (None: not yet).
    # A later final in the same turn never makes a second exchange
    _partial_lang: str | None = None
    _turn_judged: Literal["dropped", "kept"] | None = None
    _turn_n: int | None = None  # … and the final their kept turn became
    _working_for: set[int] = field(default_factory=set)  # finals Pisa said "Cerco, un attimo" for
    _awaiting_reply: bool = False  # their gated turn closed and its reply hasn't ended yet
    _drop_reply: bool = False  # … a new turn of theirs came first: that older reply is never played
    _turn_events: int = 0  # the timeline's length when their gated turn opened (words scrubbed on a drop)
    _trace_for: dict[int, str] = field(default_factory=dict)  # the brain's trace id per final it thought on
    _typed: set[int] = field(default_factory=set)  # finals the user typed (talk.text): said to Pisa
    # finals whose brain reply went in silently: their stored reply is what Pisa's voice said (with the
    # correction line, said and stored apart, left out)
    _heard_for: dict[int, str] = field(default_factory=dict)

    # --- the loop -----------------------------------------------------------------------------------------

    async def run(self) -> EndReason:
        tasks: list[asyncio.Task[None]] = []
        try:
            self._send_state("listening")
            if self.first_line.strip():  # a talk opened for Pisa's own line starts with it
                self.timeline.add("first_line", text=self.first_line)
                await self._own(Whisper(Kind.SAY, Priority.NOW, self.first_line[:1800], id="first"))
            if self.first_text.strip():  # said in one breath with the wake phrase
                await self._owner_final(self.first_text.strip())
            tasks = [
                asyncio.create_task(self._pump(), name="pump"),
                asyncio.create_task(self._events(), name="talker"),
                asyncio.create_task(self._phone(), name="phone"),
                asyncio.create_task(self._brain(), name="brain"),
                asyncio.create_task(self._clock(), name="clock"),
            ]
            for task in tasks:  # a part that fails ends the run, never deafens it
                task.add_done_callback(self._failed)
            await self._ended.wait()
        finally:  # whatever happened, the talker and the peer are closed
            if not self._revoked:  # a revoked device's talk ends at once: nothing of it is answered after
                await self._final_before_close()
                self._last_words()  # before the brain stops, what the user said last is kept
            for task in [*tasks, *self._asking]:
                task.cancel()
            await asyncio.gather(*tasks, *self._asking, return_exceptions=True)
            try:
                await self._flush_exchange()
            except Exception:
                _log.warning("talk_measure_failed", session_id=str(self.session_id))
            self._send_state("idle")
            self.timeline.add("close", reason=self.reason or "error", by=self.closed_by or "error",
                              detail=self.close_detail or None)  # fmt: skip
            self.link.send({"type": "talk.ended", "reason": self.reason or "error"})
            for close in (self.talker.close, self.link.close):
                try:
                    await close()
                except Exception:
                    _log.warning("talk_close_failed", session_id=str(self.session_id))
        return self.reason or "error"

    def _busy(self) -> bool:
        """Pisa thinks or speaks (its line still playing on the phone)."""
        return self._thinking or self._speaking or self.link.playing

    async def _final_before_close(self) -> None:
        """The user closed while their words were still partial: their turn is
        ended at the talker (Gemini transcribes at a turn's end) and its final awaited a moment, so their last
        words are whole, not the partial. Gemini sends no partials, so their voice in the last
        LAST_VOICE_S counts as words still unsaid too."""
        if self.gate is not None:  # their open turn ends at the gate; its final comes as usual
            if self.reason == "owner" and self.gate.is_open:
                await self._final_after(self.gate.close_now)
            return
        voice = self._voice_at is not None and time.monotonic() - self._voice_at <= LAST_VOICE_S
        if self.reason != "owner" or not (self._partial.strip() or voice):
            return
        await self._final_after(self.talker.end_turn)

    async def _final_after(self, end: Callable[[], Awaitable[None]]) -> None:
        self._closing = True  # no hand end while the last final is awaited
        heard = self._final_events
        try:
            await asyncio.wait_for(end(), LAST_FINAL_WAIT_S)
        except TimeoutError:  # the talker took too long: the words heard so far are the last
            return
        except Exception as exc:  # the talker failed closing the turn: the same, said in the log
            _log.warning(
                "talk_last_final_failed", session_id=str(self.session_id), cause=exc.__class__.__name__
            )
            return
        waited = 0.0
        while self._final_events == heard and waited < LAST_FINAL_WAIT_S:
            await asyncio.sleep(0.05)
            waited += 0.05
        self.timeline.add("last_final", waited=round(waited, 2), whole=self._final_events > heard)

    def turn_id(self) -> str | None:
        """The talk's turn id: the exchange in progress on the talk's channel (its run), the one the app's
        talk card knows; None when the link has none (no channel yet)."""
        turn = getattr(self.link, "turn_id", None)
        made = turn() if callable(turn) else None
        return made if isinstance(made, str) else None

    def _last_words(self) -> None:
        """the
        words the user said as the talk ended are handed on to be answered after it (`TalkService._after`
        decides how: never after the user's own talk.end): the thought under way for their latest final, the
        finals no thought took yet, or their partial words when the press came. The talk's turn they belong
        to is kept (`last_turn`): an answer after a lost peer goes into that turn of the talk's card."""
        self.last_turn = self.turn_id()
        thinking = self._think_task
        if thinking is not None and not thinking.done() and self._thinking_on not in self._superseded:
            self.timeline.add("last_words", n=self._thinking_on, by="thinking")
            self.hooks.after(self._owner_words, thinking)
        queued: list[str] = []
        while not self._words.empty():
            words, _, n, _ = self._words.get_nowait()
            if n not in self._superseded and n > self._dropped_upto:
                queued.append(words)
        partial = self._partial.strip()
        if (
            self.gate is not None
            and partial
            and (self._turn_judged is not None or self._verdict(partial, self._partial_lang) is not None)
        ):
            partial = ""  # their gated turn's words were judged already (a final), or they are not theirs
        words = _final_words(partial)
        if partial and (_noise(partial, words) or len(words) < 2 or self._heard_back(partial)):
            partial = ""  # a cough, one word, or Pisa's own line heard back: never a turn
        said = " … ".join([*queued, *([partial] if partial else [])])
        if said:
            self.timeline.add("last_words", by="partial" if partial else "final", text=said)
            self.hooks.after(said, None)

    def revoke(self, detail: str = "device_revoked") -> None:
        """Its device was revoked (security review TK-1), or its conversation deleted (`detail`
        "conversation_deleted", CD-1): the talk ends now, as the owner's end does (no last-words answer), with
        no wait for a last final; the server closed it."""
        self._revoked = True
        self.end("owner", "server", detail)

    def end(self, reason: EndReason, by: ClosedBy | None = None, detail: str = "") -> None:
        """The session ends; the first reason and who ended it are kept (the timeline's close)."""
        if self.reason is None:
            self.reason = reason
            self.closed_by = by or _CLOSED_BY[reason]
            self.close_detail = detail[:400]
        self._ended.set()

    def _failed(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            # the part and the error's own words (a provider's reason; never the user's words, a key)
            detail = f"{task.get_name()}: {type(error).__name__}: {reason_of(error)}"
            # the log line: the part and the class only (an exception of Pisa's own may quote their words)
            _log.error("talk_part_failed", session_id=str(self.session_id), part=task.get_name(),
                       error=type(error).__name__)  # fmt: skip
            self.timeline.add("failed", detail=detail)
            self.end("error", "error", detail)

    async def _pump(self) -> None:
        gate = self.gate
        async for pcm in self.link.audio_in(GATE_RATE if gate is not None else self.talker.input_rate):
            self._in_frames += 1
            self._in_bytes += len(pcm)
            now = time.monotonic()
            if self._frame_at is None or now - self._frame_at > FRAME_GAP_S:
                self._flow_since = now  # a new stretch of the phone's audio
            self._frame_at = now
            loud = peak(pcm)
            self._in_peak = max(self._in_peak, loud)
            if gate is not None:
                # the gate decides what of the phone's audio is theirs and reaches the talker; a
                # stranger's voice keeps nothing open and ends no turn by hand
                await gate.feed(pcm)
                continue
            if loud >= VOICE_PEAK:
                now = time.monotonic()
                await self._late_voice(now)
                if self._run_since is None or (
                    self._voice_at is not None and now - self._voice_at >= DEAF_AFTER_S
                ):
                    self._run_since = now  # a new stretch of the user's voice
                if self._wordless_since is None and not self._busy():
                    self._wordless_since = now  # counted from Pisa's line's end, never during it
                self._voice_at = now
                if self._voice_since is None:
                    self._voice_since = now
            await self.talker.send_audio(pcm)

    # --- the gate's target ----------------------------------------------------------------------------------

    def pisa_speaking(self) -> bool:
        return self._speaking or self.link.playing

    def phone(self, message: dict[str, Any]) -> None:
        self.link.send(message)

    def timeline_gate(self, **data: Any) -> None:
        self.timeline.add("gate", **data)

    def gate_failed(self, detail: str) -> None:
        self.end("error", "error", detail)  # three failures of the gate in one talk

    def profile_gone(self) -> None:
        self.timeline.add("gate", decision="profile_gone")
        self.end("route", "phone", "their voice profile was deleted during the talk")

    async def activity_start(self, barge: bool, score: float) -> None:
        """Their turn opens at the gate: on a barge-in Pisa's line stops here (the phone already stopped its
        playout), and the talker hears their turn start. The previous turn's reply, spoken or not
        yet, is never played after it (unless the talker cancels it itself)."""
        now = time.monotonic()
        self._heard_at = self._voice_at = now
        if self._awaiting_reply and not getattr(self.talker, "cancels_on_activity", False):
            self._drop_reply = True
        self._silenced = self._judging = False
        self._held_audio.clear()
        self._turn_events = len(self.timeline.events)
        self._partial, self._partial_lang = "", None  # their new turn's words …
        self._turn_judged = self._turn_n = None  # … and its judgement, still to make
        if self.pisa_speaking():
            self._barged_at = now
            self._barge_line = form.echo_words(self._pisa_line or (self._pisa_final or ("",))[0])
            self._barge_asked = _asked_words(self._pisa_line or (self._pisa_final or ("",))[0])
            self._barge_echo = False
            self.timeline.add("barge_in", by="gate")
            self._line_cut = self._muted = True  # the rest of Pisa's line isn't played (NO_INTERRUPTION)
        self._owner_stopped_at = None
        await self._cut()
        self._send_state("listening")
        await self.talker.activity_start()

    async def owner_audio(self, pcm: bytes) -> None:
        rate = self.talker.input_rate
        await self.talker.send_audio(pcm if rate == GATE_RATE else resampled(pcm, GATE_RATE, rate))

    async def activity_end(self, reason: str) -> None:
        """Their turn closes at the gate: the talker answers it; Pisa's answer is held until its words are
        judged, JUDGE_WAIT_S at most."""
        await self.talker.activity_end()
        self._awaiting_reply = True
        self._voice_at = time.monotonic()
        self._owner_stopped_at = time.monotonic()
        self._judging = True
        if self._judge_timer is not None:
            self._judge_timer.cancel()
        self._judge_timer = asyncio.get_running_loop().call_later(JUDGE_WAIT_S, self._judge_now)
        await self._owner_ended()

    def _judge_now(self) -> None:
        """Item 1: JUDGE_WAIT_S passed with no final: their turn is judged on the latest interim
        words (and their language), which become its final; with no words at all the held reply plays."""
        self._judge_timer = None
        if not self._judging:
            return
        if not self._partial.strip():
            self._judged(False)
            return
        words, lang = self._partial, self._partial_lang
        # judged now, before any final of the talker's can come in between
        self._turn_judged = "dropped" if self._verdict(words, lang) is not None else "kept"
        self.timeline.add("gate", decision="judged_on_interim", word_count=len(_final_words(words)))
        task = asyncio.create_task(self._owner_final(words, lang=lang))
        self._asking.add(task)
        task.add_done_callback(self._asking.discard)
        task.add_done_callback(self._failed)

    def _judged(self, drop: bool) -> None:
        """Their turn's words are judged (or the wait is over): the held answer plays, or never does."""
        if self._judge_timer is not None:
            self._judge_timer.cancel()
            self._judge_timer = None
        if not self._judging:
            return
        self._judging = False
        held, self._held_audio = self._held_audio, []
        if drop:
            self._silenced = True
            self._muted = True
            return
        for pcm in held:
            self.link.play(pcm)

    async def _dropped(self, text: str, why: str) -> None:
        """Item 4: a gated turn Pisa doesn't answer, ever: silent, nothing of its reply plays.
        a turn not taken as their leaves no words: not in the timeline, not to the phone."""
        self._turn_judged = "dropped"
        for event in self.timeline.events[self._turn_events :]:
            if event["kind"] == "owner_words":
                event["text"] = ""
        self.timeline.add("gate", decision="drop", reason=why, word_count=len(_final_words(text)))
        for call, waits in [c for c in self._calls if c[1] > self._finals]:  # its call came before the drop
            self._calls.remove((call, waits))
            self.timeline.add("ask_brain", call=call, dropped=True)
            await self._answer(call, Whisper(Kind.FACT, Priority.SILENT, DROPPED_RESULT))
        self.link.send({"type": "talk.transcript", "text": "", "final": True, "ignored": why})
        self._judged(True)
        await self._cut()
        self._send_state("listening")

    def _second(self, now: float) -> None:
        """The phone's audio and Pisa's in the second just gone, and the talker that hears nothing while
        the user's voice arrives."""
        self.timeline.add("audio", frames=self._in_frames, bytes=self._in_bytes, peak=self._in_peak,
                          out=self._out_bytes or None)  # fmt: skip
        self._in_frames = self._in_bytes = self._in_peak = self._out_bytes = 0
        since, last = self._voice_since, self._voice_at
        if since is not None and self._heard_at >= since:
            self._voice_since = None  # the talker heard it
        elif since is not None and last is not None and now - last >= DEAF_AFTER_S:
            self._voice_since = None  # the user's turn ended and no words came of it
            seconds = round(last - since, 1)
            _log.warning("talk_talker_deaf", session_id=str(self.session_id), seconds=seconds)
            self.timeline.add("talker_deaf", seconds=seconds)

    async def _deaf(self, now: float) -> None:
        """The user's voice has run HAND_END_S with no words from the talker: their turn is ended
        by hand (the talker transcribes what it has); the second time in a row Pisa says once that it can't
        hear them."""
        since = self._run_since
        if self._closing or since is None or now - since < HAND_END_S:
            return
        if self._voice_at is None or now - self._voice_at >= DEAF_AFTER_S:
            return  # the voice stopped: the talker's own end of turn comes (or `talker_deaf` notes it)
        self._run_since = now  # the next hand end only after as long again
        self._hand_ends += 1
        _log.info("talk_hand_end", session_id=str(self.session_id), seconds=round(now - since, 1))
        self.timeline.add("hand_end", seconds=round(now - since, 1), n=self._hand_ends)
        try:
            await self.talker.end_turn()
        except Exception as err:  # the provider refused it: the session goes on
            _log.warning("talk_hand_end_failed", session_id=str(self.session_id), error=type(err).__name__)

    async def _unheard(self, now: float) -> bool:
        """A voice with no words (a TV in the room): Pisa says once that it can't hear them after
        UNHEARD_SAY_S, and closes the talk after UNHEARD_CLOSE_S. True when it closed."""
        if self._voice_at is None or now - self._voice_at >= IDLE_AFTER_S:
            self._wordless_since = None  # the voice stopped: the next one starts afresh
            return False
        since = self._wordless_since
        if since is None:
            return False
        if now - since >= UNHEARD_CLOSE_S:
            _log.warning("talk_unheard_closed", session_id=str(self.session_id), seconds=round(now - since))
            self.timeline.add("unheard_close", seconds=round(now - since, 1), hand_ends=self._hand_ends)
            self.end("idle", detail=f"unheard: {round(now - since)} s of voice with no words")
            return True
        if now - since >= UNHEARD_SAY_S and not self._unheard_said:
            self._unheard_said = True
            line = UNHEARD_VOICE[self.lang if self.lang in UNHEARD_VOICE else "en-US"]
            await self._own(Whisper(Kind.SAY, Priority.NOW, line, id="unheard_voice"))
        return False

    async def _phone(self) -> None:
        while True:
            message = await self.link.messages.get()
            kind = message.get("type")
            if kind == "talk.stop":
                # barge-in by touch (contract 8): Pisa stops at once and the brain's pending answer for
                # this turn is dropped; what was done stays done, and its `done` goes in silently
                self.timeline.add("barge_in", by="touch")
                self._dropped_upto, self._stopped = self._finals, True
                await self._cut()
                await self.talker.stop()
            elif kind == "talk.end":
                why = str(message.get("reason") or "")
                self.end("owner", "phone", why if why in PHONE_REASONS else "")
            elif kind == "talk.route":
                await self._route(str(message.get("route") or ""))
            elif kind == "talk.choose":
                await self._choose(str(message.get("id") or ""), str(message.get("choice") or ""))
            elif kind == "talk.ask":  # a tap on a drawing's element
                await self._tap(str(message.get("id") or ""), str(message.get("element_id") or ""))

    async def _route(self, route: str) -> None:
        """The user's audio moved (speaker, AirPods, the car, the glasses): the session stays."""
        if route not in form.ROUTES or route == self.route:
            return
        self.timeline.add("route", route=route, was=self.route)
        if self.gate is not None:
            # their prints are per route and the talker's setup is once: the talk ends and the
            # phone reopens it on the new route (gated by that route's prints, or today's path)
            self.end("route", "phone", f"route {self.route} -> {route} under the voice gate")
            return
        self.route = route
        if self._exchange is not None and self._exchange.final_at is None:
            self._exchange.route = route
        try:  # AirPods taken out mid-talk no longer count as worn
            await self.hooks.routed(route)
        except Exception:
            _log.warning("talk_route_store_failed", session_id=str(self.session_id))

    async def _choose(self, show_id: str, choice: str) -> None:
        """The user tapped something Pisa showed in this talk. A choice's option is the user's own
        next words; an approval's yes or no is decided by code (auth tap), and Pisa says what happened. A
        tap on anything else (not shown here, closed, answered, expired) answers nothing: Pisa says so."""
        screen = self.screen
        obj = screen.shown(show_id) if screen is not None else None
        kind = obj.get("kind") if obj else None
        self.timeline.add("choose", object_kind=kind, choice=choice[:8])
        lang = self.lang if self.lang in TAP_GONE else "en-US"
        if obj is not None and kind == "choice" and screen is not None:
            options = {o.get("id") for o in obj.get("data", {}).get("options", [])}
            object_id = str(obj["object_id"])
            # a choice is answered once: a second tap on it gives no second words
            fresh = choice in options and not screen.answered(object_id)
            words = await self.hooks.chosen(object_id, choice) if fresh else None
            if words:
                screen.answer(object_id)
                await self._owner_final(words, tap=True)
                return
        elif obj is None and choice not in ("yes", "no") and show_id:
            # a choice Pisa showed before the talk, tapped by its own object id: the
            # conversation's path checks it is this device's and still open (object_event)
            done = screen is not None and screen.answered(show_id)
            words = None if done else await self.hooks.chosen(show_id, choice)
            if words:
                if screen is not None:
                    screen.answer(show_id)
                await self._owner_final(words, tap=True)
                return
        ref = str((obj or {}).get("ref", {}).get("ref", "")) if kind == "item" else ""
        if ref.startswith("approval:") and choice in ("yes", "no"):
            line, done = await self.hooks.decide(ref.split(":", 1)[1], choice == "yes")
            if line:
                await self._own(
                    Whisper(Kind.DONE if done else Kind.SAY, Priority.PAUSE, line[:1800], id="tap")
                )
                return
        await self._own(Whisper(Kind.SAY, Priority.PAUSE, TAP_GONE[lang], id="tap"))

    async def _tap(self, show_id: str, element_id: str) -> None:
        """The user tapped an element of a drawing Pisa showed in this talk: their act
        of asking about it (Pisa's template around the label from the server's copy; read-only). A superseded
        revision, a dismissed sheet or an unknown element: "Non è più aperto.", no model call. While Pisa is
        thinking it waits, one at most (a newer tap replaces it)."""
        if self._thinking:
            replaced = self._pending_tap is not None
            self._pending_tap = (show_id, element_id)
            self.timeline.add("ask_queued", replaced=replaced)
            return
        screen = self.screen
        obj = screen.shown(show_id) if screen is not None else None
        self.timeline.add("ask", object_kind=obj.get("kind") if obj else None)
        words = None
        if obj is not None and _ELEMENT.fullmatch(element_id):
            words = await self.hooks.asked(str(obj.get("object_id")), element_id)
        elif obj is None and show_id.startswith("sheet:") and _ELEMENT.fullmatch(element_id):
            words = await self.hooks.asked(show_id, element_id)  # shown before the talk
        if words:
            await self._owner_final(words, tap=True, drawing=True)
            return
        lang = self.lang if self.lang in visual_rules.GONE else "en-US"
        await self._own(Whisper(Kind.SAY, Priority.PAUSE, visual_rules.GONE[lang], id="tap"))

    async def typed(self, text: str) -> None:
        """talk.text: words the user typed into this open talk. They are the user's own final words (their
        bubble in the exchange's run; a turn of the brain's, stored as theirs), and the talker hears them as
        the user's text turn and answers by voice. Never noise, an echo or a repeat (their own act, as a
        tap)."""
        text = text.strip()
        if not text:
            return
        self._last_activity = time.monotonic()
        self.timeline.add("typed", text=text)
        self._typed = {n for n in self._typed if n > self._finals - SAID_KEPT} | {self._finals + 1}
        await self._owner_final(text, tap=True)
        await self.talker.owner_text(text)

    async def proactive(self, text: str, speak: str) -> None:
        """Something Pisa says first while the user is in a talk (leave-now, a reply waiting, a
        proposal lapsing, the check-in): leave-now at once (after the current sentence), the rest at the next
        pause."""
        priority = Priority.NOW if speak == "leave_now" else Priority.PAUSE
        self.timeline.add("proactive", speak=speak, text=text)
        await self._own(Whisper(Kind.SAY, priority, text[:1800], id=f"notice-{speak}"))

    async def working(self, line: str) -> None:
        """Item 5: Pisa's brain called a slow tool (web search, read page, maps): its fixed line
        ("Cerco, un attimo") is said now, once per turn: it states nothing and promises nothing. Nothing is
        added to any turn's prompt: it is a whisper Pisa says, like any other line of its own."""
        n = self._finals
        if n in self._working_for or (self._stopped and n <= self._dropped_upto):
            return  # once a turn; never after they stopped Pisa on it
        self._working_for.add(n)
        self.timeline.add("working", n=n)
        priority = Priority.PAUSE if self.pisa_speaking() else Priority.NOW
        await self._own(Whisper(Kind.SAY, priority, line, id=f"working{n}"))

    async def _own(self, w: Whisper) -> None:
        """A line of Pisa's own (not a thought on the user's words): grounding counts it as given."""
        self._given = [*self._given, w][-GIVEN_KEPT:]
        self._current.append(w)
        await self._whisper(w)

    async def _cut(self) -> None:
        """What Pisa was saying stops on the phone, and the talker's record of it ends where it was heard."""
        if self.link.playing:
            heard = self._sent_ms - self.link.buffered_ms
            self.link.clear()
            await self.talker.truncate(heard)

    async def _clock(self) -> None:
        metered = fresh_at = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            self._second(now)
            stopped = self._owner_stopped_at
            if self._owner_open and stopped is not None and now - stopped >= OWNER_FINAL_WAIT_S:
                self._owner_open = False  # noise with no final: Pisa's lines answer the words before it
                if self._unchecked:
                    await self._answered_thought(self._finals)  # the claims that waited on it are judged now
            busy = self._busy()
            if busy:
                self._last_activity = now
                self._run_since = None  # Pisa's thinking and speaking are no stretch of the user's
                self._wordless_since = None  # nor of their voice with no words
            voice = self._voice_at is not None and now - self._voice_at < IDLE_AFTER_S
            # never closed idle while the user's voice arrives;
            # a voice with no words is ended by hand instead (the minute meter still bounds the spend)
            if not busy and now - self._last_activity >= IDLE_AFTER_S and not voice:
                self.end("idle")
                return
            if self.gate is not None:
                quiet = now - (self._frame_at if self._frame_at is not None else self._opened_at)
                if self.gate.is_open and quiet >= GATE_NO_FRAMES_CLOSE_S:
                    await self.gate.close_now("no_audio")
                if quiet >= GATE_NO_FRAMES_END_S:
                    self.end("idle", detail=f"no audio from the phone for {quiet:.0f} s under the voice gate")
                    return
            if not busy and self.gate is None:
                await self._deaf(now)
            # never "Non ti sento" over Pisa's line, nor a close while Pisa thinks or speaks
            if not busy and self.gate is None and await self._unheard(now):
                return
            if now - metered >= METER_EVERY_S:
                metered = now
                try:
                    within = await self.hooks.meter()
                except Exception as err:  # never talk on unmetered
                    _log.exception("talk_meter_failed", session_id=str(self.session_id))
                    self.end("error", "error", f"meter: {type(err).__name__}")
                    return
                if not within:
                    self.end("budget")
                    return
            if now - fresh_at >= FRESH_EVERY_S:
                fresh_at = now
                try:  # a failed refresh keeps the old Fresh, and the clock goes on
                    await self._refresh()
                except Exception:
                    _log.warning("talk_fresh_failed", session_id=str(self.session_id))

    async def _refresh(self) -> None:
        fresh = await self.hooks.fresh()
        if fresh.text() != self.fresh.text():
            self.fresh = fresh
            await self._whisper(fresh.whisper())

    # --- the talker's events ------------------------------------------------------------------------------

    async def _events(self) -> None:
        async for event in self.talker.events():
            self._last_activity = time.monotonic()
            match event:
                case Audio(pcm=pcm):
                    if self._muted or self._silenced or self._drop_reply:
                        continue  # the rest of a line the user's words cut off, or a dropped turn's reply
                    if self._judging:
                        self._held_audio.append(pcm)  # held until their turn's words are judged
                    else:
                        self.link.play(pcm)
                    self._out_bytes += len(pcm)
                    self._sent_ms += len(pcm) * 1000 // (2 * self.talker.output_rate)
                    if self._exchange is not None and self._exchange.first_sound_ms is None:
                        self._exchange.first_sound_ms = int(
                            (time.monotonic() - self._exchange.owner_end) * 1000
                        )
                case OwnerSpeaking(on=True):
                    self._heard_at = time.monotonic()
                    self._run_since = None  # the talker hears a turn: the stretch starts again
                    self.timeline.add("owner_speaking", on=True)
                    if self._speaking or self.link.playing:
                        self._barged_at = time.monotonic()
                        self._barge_line = form.echo_words(self._pisa_line or (self._pisa_final or ("",))[0])
                        self._barge_asked = _asked_words(self._pisa_line or (self._pisa_final or ("",))[0])
                        self._barge_echo = self._echo_barge()
                        if self._barge_echo:
                            # no voice in the phone's audio: Pisa's own voice heard back.
                            # Pisa's line plays on; it is never cut for it.
                            self.timeline.add("barge_in", by="echo")
                            continue
                        self.timeline.add("barge_in", by="voice")
                        self._line_cut = True
                    self._owner_stopped_at = None
                    await self._cut()  # the user talks over Pisa: what Pisa was saying stops here
                    self._send_state("listening")
                case OwnerSpeaking(on=False):
                    self._run_since = None  # their turn ended
                    self.timeline.add("owner_speaking", on=False)
                    self._owner_stopped_at = time.monotonic()
                    await self._owner_ended()
                case OwnerWords(text=text, final=final, lang=heard_lang):
                    self._heard_at = time.monotonic()
                    if self._turn_judged == "dropped" and not final:
                        continue  # more words of a dropped turn: never kept, never shown
                    if text.strip():  # words came: the voice is heard, a new stretch starts
                        self._run_since, self._hand_ends, self._wordless_since = None, 0, None
                    self._partial = "" if final else text
                    if not final:
                        self._partial_lang = heard_lang or self._partial_lang
                    self._final_events += 1 if final else 0
                    if not final and text.strip():  # their words for the next final are arriving
                        self._words_at, self._words_for = time.monotonic(), self._finals + 1
                    if not final:
                        self.timeline.add("owner_words", text=text)
                        self.hooks.heard(text)  # the reflex reads the words while the user speaks
                        if self._talked_over(text):
                            await self._barge_in_by_words()
                    if final and text.strip() and await self._late_final(text):
                        continue  # one gated turn, one judgement, one exchange
                    if final and text.strip():
                        await self._owner_final(text, lang=heard_lang)
                    else:  # an empty final closes the words too (noise)
                        if final and self.gate is not None and self._judging:
                            await self._dropped("", "fragment")  # a turn with no words
                        if final:
                            self.timeline.add("owner_final", text="")
                            self._closed_words += 1  # a call waiting on these words stops waiting
                            self._final.set()
                            self._final = asyncio.Event()
                        self._owner_open = not final and (self._owner_open or bool(text.strip()))
                        self.link.send({"type": "talk.transcript", "text": text, "final": final})
                case PisaWords(text=text, final=final, interrupted=interrupted):
                    if self._silenced or self._drop_reply:
                        if final:  # the dropped reply ended; the next is played
                            self._silenced = self._drop_reply = self._awaiting_reply = False
                        continue  # a dropped turn's reply is never shown nor said
                    if final:
                        self._awaiting_reply = False
                    # Gemini's "<!-- No response -->" never reaches the user's cards
                    text = unmarked(text)
                    self.link.send({"type": "talk.said", "text": text, "final": final})
                    self._pisa_line = "" if final else text
                    if not final and not self._muted and self._said_again(text):
                        await self._line_again(text)
                        continue
                    if final and text.strip():
                        now = time.monotonic()
                        # a line can come back until its speech ends, plus the window
                        kept = [
                            line
                            for line in self._pisa_lines
                            if self_rules.until(line[0], len(line[1])) >= now
                        ]
                        self._pisa_lines = [*kept, (now, form.echo_words(text), _asked_words(text))]
                    if self._line_for is None:  # filed when Pisa's line starts, not when it ends
                        self._line_for = self._finals + 1 if self._owner_open else self._finals
                    if final:
                        line_for, self._line_for = self._line_for, None
                        if line_for > self._finals and not self._owner_open:
                            line_for = self._finals  # the final it waited for never came (noise)
                        prev, self._pisa_final = (
                            self._pisa_final,
                            (text, time.monotonic(), line_for, interrupted),
                        )
                        if text.strip() and not interrupted:  # a whole line, said once per final
                            self._whole_for.setdefault(line_for, []).append(
                                (form.echo_words(text), time.monotonic())
                            )
                            for old in [k for k in self._whole_for if k <= line_for - SAID_KEPT]:
                                del self._whole_for[old]
                        if (
                            prev is not None
                            and prev[3]
                            and prev[2] == line_for
                            and time.monotonic() - prev[1] <= RETRY_S
                            and text.strip()
                            and grows(prev[0], text)
                        ):  # Gemini's interrupted + retry: one line, the whole one. Only
                            # after the talker's own `interrupted`, in the same turn (no owner final between)
                            self._retried(prev[0], text, prev[2])
                            retried = True
                        else:
                            self.timeline.add("pisa_said", text=text, line_for=line_for)
                            retried = False
                        if text.strip():
                            await self._pisa_said(text, line_for, retried=retried)
                case AskBrain(call_id=call_id, question=question):
                    if self._silenced or self._drop_reply:
                        # Pisa's brain never answers a dropped turn: the call
                        # gets a silent result, never left open
                        self.timeline.add("ask_brain", call=call_id, dropped=True)
                        await self._answer(call_id, Whisper(Kind.FACT, Priority.SILENT, DROPPED_RESULT))
                        continue
                    self._responding()
                    if self._exchange is not None:
                        self._exchange.asked_brain = True
                    # tied to the user's words now, before any await: the thought on its way or the next one
                    waits = self._finals if self._thought_for < self._finals else self._finals + 1
                    self._calls.append((call_id, waits))
                    self.timeline.add("ask_brain", call=call_id, waits=waits, question=question)
                    if self._stopped and waits > self._finals:
                        self._waiting_after_stop.add(waits)
                    if (ready := self._ready_for.get(waits)) is not None and waits > self._thought_for:
                        # the reflex's answer for these words is ready: the call gets it now
                        self._calls.remove((call_id, waits))
                        await self._answer(call_id, ready)
                        continue
                    asking = asyncio.create_task(self._asked(call_id, waits))
                    self._asking.add(asking)
                    asking.add_done_callback(self._asking.discard)
                    asking.add_done_callback(self._failed)
                case Speaking(on=on):
                    if not on:  # silencing covers only the dropped turn's own reply
                        self._silenced = self._drop_reply = self._awaiting_reply = False
                    self.timeline.add("pisa_speaking", on=on)
                    self._speaking = on
                    self._muted = False  # a new line, or the cut one ended
                    if on:
                        self._sent_ms, self._line_cut = 0, False
                        self._responding()
                    self._send_state("speaking" if on else ("thinking" if self._thinking else "listening"))
                case Resumable(handle=handle):
                    self.hooks.resumable(handle)
                case Closed(reason=why, detail=detail):
                    _log.warning("talker_closed", session_id=str(self.session_id), reason=why, detail=detail)
                    self.end("fallback", "talker", f"{why}: {detail}" if detail else why)
                    return
        self.end("fallback", "talker", "the talker's stream ended")

    def _talked_over(self, text: str) -> bool:
        """On the speaker, Gemini's start of speech is set low and may not fire while the user talks over
        Pisa's long answer ("No, no. Calma, calma, no."): their words (two or more, or a bare
        "no", "basta", "aspetta") while Pisa speaks cut Pisa, unless they are Pisa's own line heard back."""
        words = _final_words(text)
        speaking = (self._speaking or self.link.playing) and not self._line_cut
        if not speaking or _noise(text, words):
            return False
        if len(words) < 2 and not (words and words[0] in _STOP_WORDS):
            return False
        return not self._heard_back(text)

    def _heard_back(self, text: str) -> bool:
        """Partial words that are Pisa's own recent line heard back: an echo by `_echo`, or most of them, in
        Pisa's order, inside one of Pisa's lines (the one being said included)."""
        if self._echo(text):
            return True
        words = form.echo_words(text)
        now = time.monotonic()
        lines = [w for at, w, _ in self._pisa_lines if self_rules.until(at, len(w)) >= now]
        lines.append(form.echo_words(self._pisa_line))
        return any(_in_order(words, line) * 2 > len(words) for line in lines if line)

    def _echo_barge(self) -> bool:
        """The talker heard the user start while Pisa spoke, but the phone's audio has flowed
        and carried no voice for BARGE_VOICE_S: what it heard is Pisa's own voice come back.
        With no audio from the phone at all, nothing is known: the talker's word stands."""
        now = time.monotonic()
        flowing = self._frame_at is not None and now - self._frame_at <= FRAME_GAP_S
        if not flowing or self._flow_since is None or now - self._flow_since < BARGE_VOICE_S:
            return False  # the phone's audio hasn't flowed the whole second: nothing is known
        return self._voice_at is None or now - self._voice_at > BARGE_VOICE_S

    async def _late_voice(self, now: float) -> None:
        """BARGE_VOICE_S counts from their voice's first frame, not from the talker's start of
        speech: their voice arriving within it after a barge-in judged an echo (the talker's VAD can fire on
        their first syllable, before a sample reaches VOICE_PEAK) makes it theirs: Pisa's line is cut then."""
        if not self._barge_echo or now - self._barged_at > BARGE_VOICE_S:
            return
        self._barge_echo = False
        if self._line_cut or not (self._speaking or self.link.playing):
            return
        self.timeline.add("barge_in", by="voice", late=True)
        self._line_cut = True
        await self._cut()
        self._send_state("listening")

    def link_silent(self) -> bool:
        """The phone's audio hasn't arrived for LINK_SILENT_S: the talk's link died while the run
        is still registered (the phone then talks on its economy path)."""
        last = self._frame_at if self._frame_at is not None else self._opened_at
        return time.monotonic() - last >= LINK_SILENT_S

    def _said_again(self, text: str) -> bool:
        """Pisa's line being said is a whole line it already said for the same final words:
        never said twice. A line cut
        off (the talker's own `interrupted`) is no whole line: its retry is said.

        the talker's partial words are cumulative, so a line is "said again" only once it is
        that same whole line, normalised, ending its sentence: never a longer line that begins the same way
        ("Domani hai la riunione con Marco." then "Domani hai la riunione con Marco alle 10…"). A line
        carrying a whisper or a correction given since the whole line was said is never cut."""
        if not text.rstrip().endswith(_LINE_END):
            return False  # the line may go on: nothing is known yet
        words = form.echo_words(text)
        if len(words) < LINE_AGAIN_WORDS:
            return False
        n = self._line_for if self._line_for is not None else self._finals
        return any(line == words and at >= self._whispered_at for line, at in self._whole_for.get(n, []))

    async def _line_again(self, text: str) -> None:
        """The talker started a line it already said whole for these words: it stops, and the phone hears no
        more of it."""
        self.timeline.add("pisa_again", text=text, line_for=self._line_for)
        self._muted = True
        await self._cut()
        await self.talker.stop()

    async def _barge_in_by_words(self) -> None:
        """The user's words cut Pisa's line: the phone stops it, the talker stops its turn, and what is left
        of that turn's audio isn't played."""
        self.timeline.add("barge_in", by="words")
        self._barged_at = time.monotonic()
        self._line_cut = self._muted = True
        await self._cut()
        await self.talker.stop()
        self._send_state("listening")

    def _verdict(self, text: str, lang: str | None) -> str | None:
        """Item 4: why their gated turn's words are dropped (None: kept)."""
        assert self.gate is not None
        text = one_utterance(text)
        words = _final_words(text)
        why = gate_drop(words, lang, self.gate.last_score, self.gate.theta, echo=self._echo(text))
        return "fragment" if why is None and _noise(text, words) else why

    async def _late_final(self, text: str) -> bool:
        """A final of their gated turn after it was judged (the interim words at JUDGE_WAIT_S,
        or a first final grown at `turnComplete`). Dropped: the timeline gets its word count, nothing else.
        Kept: it replaces the turn's words while their thought is pending; otherwise it is no new exchange.
        True: handled here."""
        if self._turn_judged is None:
            return False
        count = len(_final_words(text))
        if self._turn_judged == "dropped":
            self.timeline.add("gate", decision="late_final", dropped=True, word_count=count)
            return True
        n = self._turn_n
        pending = n is not None and n == self._finals and self._thought_for < n
        if not pending or _final_words(one_utterance(text)) == self._last_final:
            self.timeline.add("final_repeated", word_count=count)  # never the words
            return True
        await self._owner_final(text, judged=True)  # supersedes the pending thought once the words pass
        return True

    async def _owner_final(
        self, text: str, *, tap: bool = False, lang: str | None = None, judged: bool = False,
        drawing: bool = False,
    ) -> None:  # fmt: skip
        """The user's final words: one exchange, one thought (contract 2: one end-of-turn per final). `tap`:
        the user's tap on a shown choice, their own act: never noise, echo or a repeat.
        `judged`: their gated turn's words were judged kept already. `drawing`:
        a tap in a drawing, its words Pisa's "Sul disegno: …" around the label: answered alone and
        read-only (`origin: drawing`), never their words (no redo, no ready answer, no echo of it: the phone
        shows its own line)."""
        text = text if tap else one_utterance(text)  # the same words twice are said once
        words = _final_words(text)
        if self.gate is not None and not tap and not judged:
            # a strange language with a weak score, a fragment, Pisa's own line: dropped
            why = self._verdict(text, lang)
            if why is not None:
                await self._dropped(text, why)
                return
            self._turn_judged = "kept"
            self._judged(False)
        if not tap and _noise(text, words):
            # noise ("AJ", "A": a cough, words to someone else) is never a turn ("Non ho capito «A»"):
            # the phone is told it was set aside (no card, no end of turn).
            self.timeline.add("noise", text=text)
            self.link.send({"type": "talk.transcript", "text": text, "final": True, "ignored": "noise"})
            return
        if not tap and self._echo(text):
            # Pisa's own voice heard back is never the user's words (each would become a turn, then a loop)
            _log.info("talk_echo", session_id=str(self.session_id))
            self.timeline.add("echo", text=text)
            self.link.send({"type": "talk.transcript", "text": text, "final": True, "ignored": "echo"})
            return
        unanswered = self._thought_for < self._finals
        recent = (
            time.monotonic() - self._last_final_at < REPEAT_WINDOW_S
            and self._responded_at < self._last_final_at
        )
        if not tap and words and words == self._last_final and (unanswered or recent):
            _log.info("talk_final_repeated", session_id=str(self.session_id))
            self.timeline.add("final_repeated", text=text)
            return  # the same final again: no second exchange, no second thought
        if self._finals and self._thought_for < self._finals and (judged or _grows(self._last_final, words)):
            self._supersede(self._finals)
        self._last_final, self._last_final_at = words, time.monotonic()
        self.timeline.add("owner_final", text=text, n=self._finals + 1)
        if not drawing:
            self.link.send({"type": "talk.transcript", "text": text, "final": True})
        if self._exchange is None or self._exchange.final_at is not None:
            await self._owner_ended()
        assert self._exchange is not None
        self._exchange.final_at = time.monotonic()
        before, before_n = self._owner_words, self._owner_words_n
        if (
            not drawing
            and before_n is not None
            and before_n != self._exchange.n
            and self._thought_for >= self._finals
            and clean_rules.redone(before, text)
        ):  # they corrected or repeated the turn Pisa answered: that turn wasn't clean
            self.timeline.add("redone", n=before_n)
            await self.hooks.redone(before_n)
        self._owner_words, self._owner_words_n = text, self._exchange.n
        self._owner_open = False
        self._finals += 1
        if self._turn_judged == "kept" and not tap:
            self._turn_n = self._finals  # their gated turn's one exchange
        if self._line_for is not None and self._line_for < self._finals - 1:
            self._line_for = None  # a line cut off with no final of its own: never file the next one by it
        self._current = []  # a "done" now needs this exchange's own DONE
        self._stopped = False
        if drawing:
            self._drawing_finals = {n for n in self._drawing_finals if n > self._finals - 16}
            self._drawing_finals.add(self._finals)
        else:
            self._pending_tap = None  # their own words drop a tap still waiting
        self._words.put_nowait((text, time.monotonic(), self._finals, self._exchange.owner_end))
        self._final.set()
        self._final = asyncio.Event()
        if drawing:
            return
        await self._ready_now(text, self._finals)
        await self._heard(text, self._finals)

    def _echo(self, text: str) -> bool:
        """The final words are Pisa's own line heard back:
        most of them, in Pisa's order,
        in what Pisa said until ECHO_AFTER_S past its speech; or, two words or more said within
        BARGE_ECHO_S of cutting Pisa's line, all inside that line. Never a bare yes or no (the user's "sì" to
        Pisa's "sì o no?")."""
        # numbers compared as digits on both sides ("…e trentadue" heard back as "2:32")
        words = form.echo_words(text)
        if not words:
            return False
        if _bare_yes_or_no(text):
            # "certo" (a bare yes) right after a barge-in is Pisa's own word come back unless
            # the line it cut asked them something
            cut_now = time.monotonic() - self._barged_at <= BARGE_ECHO_S
            return cut_now and len(words) == 1 and self._one_word_echo(words[0])
        # a final that asks ("alle 2:32?") is the user asking Pisa's words back, never an
        # echo, unless Pisa itself asked it: then only Pisa's own questions are matched
        asked = text.rstrip().endswith("?")
        now = time.monotonic()
        lines = [q if asked else w for at, w, q in self._pisa_lines if self_rules.until(at, len(w)) >= now]
        if self._pisa_line:
            lines.append(_asked_words(self._pisa_line) if asked else form.echo_words(self._pisa_line))
        lines = [line for line in lines if line]
        if not lines:
            return False
        heard = [w for line in lines for w in line]
        if len(words) >= 3 and _in_order(words, heard) >= self_rules.ECHO_COVER * len(words):
            return True
        cut_now = now - self._barged_at <= BARGE_ECHO_S
        if cut_now and len(words) == 1 and self._one_word_echo(words[0]):
            return True
        return (
            cut_now and len(words) >= 2 and any(_in_order(words, line) == len(words) for line in lines[-2:])
        )

    def _one_word_echo(self, word: str) -> bool:
        """One word, right after a barge-in on Pisa's line, is that line come
        back ("certo", "attimi" of "un attimo"), or a word of no language Pisa speaks here (Pisa's voice
        transcribed as another language). "basta", "aspetta" are theirs.

        decided from the word and its timing, never from the phone's peak alone (a quiet voice
        never reaches VOICE_PEAK). A yes or no to a line that asked them is always theirs; so is a word of
        what the line asked them ("Vuoi chiamare Marco o Nino?" → "Marco.", "Alle nove o alle dieci?" →
        "Dieci."): only a word the line says and its question did not ask about can be its echo."""
        if word in _STOP_WORDS:
            return False
        if self._barge_asked and (
            is_short_yes(word)
            or action_rules.is_short_no(word)
            or any(_near_word(word, asked) for asked in self._barge_asked)
        ):
            return False
        if any(_near_word(word, said) for said in self._barge_line):
            return True
        return _foreign(word, self.lang)

    async def _ready_now(self, text: str, n: int) -> None:
        """The reflex's ready answer for these final words (drafted from Pisa's own data on the partial
        words, checked against it): said at once, while the main thinker works. A call waiting on these words
        gets it; else it goes in at the pause. The brain's answer then goes in silently when Pisa's voice
        already said it (`_deliver`), or is said when it adds something. Never for a bare yes or no (code
        decides)."""
        if n <= self._dropped_upto:
            return
        ready: str | None = None
        if form.asks_next(text):
            # "Pisa, what's next?" in about 2 s: the plan's next item from Pisa's own data (a
            # fresh read, no model, no outside call), said at once; the brain's answer goes in silently when
            # it says the same
            try:
                self.fresh = await self.hooks.fresh()
            except Exception:
                _log.warning("talk_fresh_failed", session_id=str(self.session_id))
            if self.fresh.plan or self.fresh.next_item:  # no plan, the brain answers
                ready = form.next_line(self.fresh.next_item, self.lang)
        ident = f"next{n}" if ready is not None else f"reflex{n}"
        if ready is None and (hit := await self._ready_answer(text, n)) is not None:
            ready, ident = hit, f"ready{n}"
        if ready is None:
            ready = self.hooks.ready(text)
        if _bare_yes_or_no(text):
            return
        if ready is None:
            # no partial words came (natural mode): the reflex on the final words, beside the
            # brain; its answer is said first only while the brain hasn't answered these words
            final = asyncio.create_task(self._ready_final(text, n))
            self._asking.add(final)
            final.add_done_callback(self._asking.discard)
            final.add_done_callback(self._failed)
            return
        await self._give_ready(ready, ident, n)

    async def _ready_answer(self, text: str, n: int) -> str | None:
        """A question one of Pisa's ready answers matches: said at once when nothing
        changed since it was made (`ready_hit`); else the brain answers (`ready_stale`). Never for a bare yes
        or no."""
        if _bare_yes_or_no(text):
            return None
        try:
            hit = await self.hooks.ready_answer(text)
        except Exception as err:  # Pisa's ready answers never break a talk: the brain answers
            _log.warning("talk_ready_failed", session_id=str(self.session_id), error=type(err).__name__)
            return None
        if hit is None:
            return None
        self.timeline.add("ready_hit" if hit.hit else "ready_stale", n=n, topic=hit.topic, age_s=hit.age_s,
                          verified=hit.hit)  # fmt: skip
        if self._exchange is not None and self._finals == n:
            self._exchange.ready = "hit" if hit.hit else "stale"
        return hit.text if hit.hit else None

    async def _heard(self, text: str, n: int) -> None:
        """Heard in 1 s: no ready answer for these words, and they ask a search with something
        unusual in it: Pisa's voice says it back at once ("Pizza al taglio da guida, non la più vicina…
        cerco."), never a generic holding phrase. No fact and no claim: their own words."""
        if (
            n in self._ready_for
            or n <= self._dropped_upto
            or (line := restate.heard(text, self.lang)) is None
        ):
            return
        self.timeline.add("heard", n=n)
        await self._whisper(Whisper(Kind.HOLD, Priority.PAUSE, line, id=f"heard{n}"))

    async def _ready_final(self, text: str, n: int) -> None:
        started = time.monotonic()
        ready = await self.hooks.ready_final(text)
        took = int((time.monotonic() - started) * 1000)
        late = n != self._finals or n <= self._thought_for or n <= self._dropped_upto or n in self._superseded
        self.timeline.add("ready_final", n=n, took_ms=took, ready=ready is not None, late=late or None)
        if ready is not None and not late:
            await self._give_ready(ready, f"final{n}", n)

    async def _give_ready(self, ready: str, ident: str, n: int) -> None:
        # a ready answer is code's own record ("Oggi ho spostato «Cena»…"): it backs what it says
        whisper = Whisper(Kind.SAY, Priority.PAUSE, ready[:1800], id=ident, backed=ident.startswith("ready"))
        self._ready_for = {n: whisper}
        self._given = [*self._given, whisper][-GIVEN_KEPT:]
        self._current.append(whisper)
        mine = [call for call, waits in self._calls if waits == n]
        if mine:
            self._calls.remove((mine[0], n))
            await self._answer(mine[0], whisper)
        else:
            await self._whisper(whisper)

    async def reflexed(self, read: Reflexed) -> None:
        """One reflex pass on the user's partial words: into the timeline; Pisa's data it read goes to
        the talker as a silent whisper (what it may say besides Pisa's answers)."""
        self.timeline.add(
            "reflex", by=read.by, took_ms=read.ms, intent=read.intent, reads=list(read.reads) or None,
            held=list(read.held) or None, stable=read.stable, ready=read.ready is not None,
        )  # fmt: skip
        if self._exchange is not None and _worse_read(self._exchange, read.by, read.ms):
            # a turn keeps its slowest model read; a later pass by the
            # rules (the reflex resting after slow reads) or a faster read never hides it
            self._exchange.reflex_ms, self._exchange.reflex_by = read.ms, read.by
        text = self.hooks.state()[:1800]
        if text and text != self._state_sent:
            self._state_sent = text
            whisper = Whisper(Kind.FACT, Priority.SILENT, text, id="state")
            self._given = [*self._given, whisper][-GIVEN_KEPT:]
            await self._whisper(whisper)

    def _supersede(self, n: int) -> None:
        """The user's words n grew into the next final before their thought arrived: that thought is
        dropped (cancelled if under way) and the calls waiting on n wait on the next final instead."""
        _log.info("talk_final_superseded", session_id=str(self.session_id))
        self.timeline.add("final_superseded", n=n)
        self._superseded.add(n)
        self._calls = [(call, n + 1 if waits == n else waits) for call, waits in self._calls]
        if self._thinking_on == n and self._think_task is not None:
            self._think_task.cancel()

    def _fold(self, old: int, new: int) -> None:
        """The user's words `old` came while Pisa was thinking and join the later words `new`: one thought
        answers both; calls waiting on `old` wait on `new`."""
        self.timeline.add("final_folded", n=old, into=new)
        self._superseded.add(old)
        self._calls = [(call, new if waits == old else waits) for call, waits in self._calls]

    def _responding(self) -> None:
        self._responded_at = time.monotonic()
        self._responded.set()
        self._responded = asyncio.Event()

    def _retried(self, cut: str, whole: str, n: int) -> None:
        """The line Pisa's talker retried after `interrupted`: its timeline line and what Pisa said for
        these words become the whole line, never two. The cut line leaves what Pisa said for these
        words; the caller then says the whole line through `_j_said` (grounding, false claims, the marks)."""
        for event in reversed(self.timeline.events):
            if event["kind"] == "pisa_said" and event.get("text") == cut:
                event["text"], event["retried"] = whole, True
                break
        lines = self._said_for.get(n, [])
        if cut in lines:
            lines.remove(cut)

    async def _pisa_said(self, text: str, n: int, *, retried: bool = False) -> None:
        backed = any(w.kind is Kind.DONE or w.backed for w in self._current)
        self.hooks.said(text, backed, retried)
        if self.first_line and _says(text, self.first_line):  # Pisa's own line was said (measured)
            self.first_line = ""
            self.timeline.add("first_line_said")
            await self.hooks.first_said()
        if self._ask is not None and _says(text, self._ask.text):
            self._ask, self._ask_spoken_at = None, time.monotonic()
            await self.hooks.asked_aloud()  # the card's read-out is now
        # what Pisa said after the user's latest words: the brain's answer goes in silently only when
        # these words already carry it (no filler list). Words Pisa says while the user's words are still
        # being heard (Grok answers before the final transcript) belong to those words, the next final; a line
        # the user interrupts belongs to the words it was answering (`n`, fixed when the line started).
        self._said_for.setdefault(n, []).append(text)
        for old in [k for k in self._said_for if k <= n - SAID_KEPT]:
            del self._said_for[old]  # bounded: only the latest finals are ever looked at
        if n in self._heard_for:  # the stored reply for these words is what the voice says: this line too
            await self._heard_stored(n)
        if self._thought_for < n and talker_claims_change(text):
            # a done claim is judged once this final's thought has arrived (the brain may say it was done)
            self._unchecked.append((text, True))
            return
        await self._check(text, early=False, n=n)

    async def _owner_ended(self) -> None:
        await self._flush_exchange()
        self._n += 1
        self._exchange = Exchange(self._n, datetime.now().astimezone(), time.monotonic(), route=self.route)

    async def _flush_exchange(self) -> None:
        if self._exchange is not None and self._exchange.final_at is not None:
            await self.hooks.exchange(self._exchange)
        self._exchange = None

    # --- Pisa's brain
    # ----------------------------------------------------------------------------------------

    async def _brain(self) -> None:
        """One thought at a time. The latest words win:
        finals that came while Pisa was thinking are one turn,
        their words joined, answered once."""
        while True:
            words, at, n, owner_end = await self._words.get()
            pending: list[tuple[str, float, int, float]] = [(words, at, n, owner_end)]
            while not self._words.empty():
                pending.append(self._words.get_nowait())
            pending = [p for p in pending if p[2] not in self._superseded]
            # a tap is never joined to their words (by its flag): their words are
            # answered first, the newest tap next, on its own. A tap's final never dangles
            # (its words are Pisa's template around a label), and a tap that comes while their cut words wait
            # neither ends the wait nor drops their "…"
            held: list[tuple[str, float, int, float]] = []
            taps = [p for p in pending if p[2] in self._drawing_finals]
            if taps and len(taps) < len(pending):
                held = taps[-1:]
                pending = [p for p in pending if p[2] not in self._drawing_finals]
            elif len(taps) > 1:
                pending = taps[-1:]
            trailing = bool(pending) and _trails(pending[-1][0], self.lang)
            cut = bool(pending) and (_dangling(pending[-1][0]) or trailing)
            if cut and pending[-1][2] not in self._drawing_finals:
                # words cut mid-phrase wait a moment
                # for the rest; with none, the brain gets them marked cut ("…") and asks. Words that end
                # on a connector ("Scusa… e poi", "and then") wait as the economy path's turn rules do
                # (their unfinished wait), and on while the user's voice or words still come: one
                # utterance the talker split in two is one turn, answered once
                self.timeline.add("dangling", n=pending[-1][2], trailing=trailing or None)
                started = time.monotonic()
                wait = max(DANGLING_WAIT_S, CONNECTOR_WAIT_S) if trailing else DANGLING_WAIT_S
                deadline = started + wait
                while (left := deadline - time.monotonic()) > 0 or (
                    trailing and (left := self._still_saying(started, wait)) > 0
                ):
                    waiter = self._final
                    try:
                        await asyncio.wait_for(waiter.wait(), left)
                    except TimeoutError:
                        continue  # the wait is over, unless the user is still saying the rest
                    came: list[tuple[str, float, int, float]] = []
                    while not self._words.empty():
                        came.append(self._words.get_nowait())
                    came = [p for p in came if p[2] not in self._superseded]
                    held = ([p for p in came if p[2] in self._drawing_finals] or held)[-1:]
                    his = [p for p in came if p[2] not in self._drawing_finals]
                    pending += his
                    if his:
                        break
                pending = [p for p in pending if p[2] not in self._superseded]
                if pending and _dangling(pending[-1][0]):
                    last = pending[-1]
                    pending[-1] = (f"{last[0].rstrip(' .,;:')}…", *last[1:])
            for tap in held:
                self._words.put_nowait(tap)  # the newest tap, answered on its own after their words
            if not pending:
                continue  # grown into the next final: its thought answers
            words, at, n, owner_end = pending[-1] if len(pending) == 1 else (words, at, n, owner_end)
            if len(pending) > 1:
                latest = pending[-1][2]
                for _, _, old, _ in pending[:-1]:
                    self._fold(old, latest)
                words = " … ".join(p[0] for p in pending)
                at, n, owner_end = pending[0][1], latest, pending[-1][3]
            drawn = n in self._drawing_finals
            if (
                len(pending) == 1 and not drawn and _bare_yes_or_no(words)
                and not await self._addressed(n, owner_end)
            ):  # fmt: skip
                # a yes or no the talker didn't answer was said to someone else (or the TV)
                _log.info("talk_words_not_for_pisa", session_id=str(self.session_id))
                self.timeline.add("not_for_pisa", n=n)
                await self._unanswered(n)  # a call waiting on those words isn't left hanging
                await self._answered_thought(n)
                continue
            await self._think(words, at, n, drawing=drawn)

    def _still_saying(self, since: float, wait: float) -> float:
        """How much longer words that ended on a connector wait: `wait` past the user's latest voice or words
        (they are still saying the rest), at most CONNECTOR_MAX_S after `since`; 0 when the wait is over."""
        now = time.monotonic()
        heard = max(self._voice_at or 0.0, self._words_at if self._words_for > self._finals else 0.0)
        if heard <= since:
            return 0.0
        return max(0.0, min(heard + wait, since + CONNECTOR_MAX_S) - now)

    async def _addressed(self, n: int, owner_end: float) -> bool:
        """Whether the talker took the user's words n as said to it: it spoke or asked Pisa's brain after the
        user stopped speaking (Grok often answers before the transcript is final)."""
        if n in self._waiting_after_stop or n in self._typed:
            # after a stop the talker's call waits on them: it can't answer first; typed words are Pisa's
            return True
        deadline = time.monotonic() + ADDRESSED_WAIT_S
        while self._responded_at < owner_end:
            if self._finals != n:
                return True  # more words came: these were part of the conversation
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                await asyncio.wait_for(self._responded.wait(), left)
            except TimeoutError:
                return False
        return True

    async def _think(self, words: str, at: float, n: int, *, drawing: bool = False) -> None:
        self._thinking = True
        if not self._speaking:
            self._send_state("thinking")
        trace = uuid.uuid4().hex
        self.timeline.add("think", n=n, trace=trace)
        self._trace_for[n] = trace
        for old in [k for k in self._trace_for if k <= n - SAID_KEPT]:
            del self._trace_for[old]  # bounded, as `_said_for`
        # a spoken yes decides Pisa's question by code only within 10 s of Pisa having said it aloud
        spoken = self._ask_spoken_at is not None and time.monotonic() - self._ask_spoken_at <= YES_WINDOW_S
        hold = asyncio.create_task(self._hold_later())
        try:
            self._think_task, self._thinking_on = (
                asyncio.ensure_future(
                    self.hooks.think(words, trace, spoken, origin="drawing")
                    if drawing
                    else self.hooks.think(words, trace, spoken)
                ),
                n,
            )
            # shielded, so a thought under way when the talk ends is still answered (as a notice)
            thought = await asyncio.shield(self._think_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if n in self._superseded and not (current is not None and current.cancelling()):
                return  # the user's words grew: the next final's thought answers
            raise
        except Exception as err:
            _log.exception("talk_think_failed", session_id=str(self.session_id), trace_id=trace)
            self.timeline.add("think_failed", n=n, error=type(err).__name__)
            thought = Thought(_FAILED[self.lang if self.lang in _FAILED else "en-US"], Kind.SAY)
        finally:
            hold.cancel()
            self._thinking = False
            self._think_task = None
            if (pending := self._pending_tap) is not None:
                self._pending_tap = None
                asked = asyncio.create_task(self._tap(*pending))
                self._asking.add(asked)
                asked.add_done_callback(self._asking.discard)
                asked.add_done_callback(self._failed)
        if thought.parts is not None:  # where the brain's time went, in the timeline and the measures
            parts = {k: v for k, v in thought.parts.items() if k != "trace"}
            self.timeline.add("brain_parts", n=n, **parts)
            if self._exchange is not None and self._finals == n:  # the turn these words opened
                self._exchange.parts = parts
        if thought.kind is Kind.DONE or thought.claimed_done:
            self.hooks.changed()  # Pisa changed something: its ready answers are rebuilt
        if self._exchange is not None and self._finals == n:  # the brain's record
            self._exchange.failed += thought.failed
            self._exchange.unbacked += thought.unbacked
        if not thought.text.strip():
            await self._unanswered(n, _NOTHING)  # a call waiting on it gets an answer all the same
            await self._answered_thought(n)
            return
        self._whispers += 1
        priority = Priority.SILENT if n <= self._dropped_upto else Priority.PAUSE
        if n < self._finals and thought.kind is not Kind.DONE and priority is Priority.PAUSE:
            # the user spoke again while Pisa thought: this answer goes in silently (the talker knows
            # it), the next thought answers the newer words with it in its history; a done is still said
            priority = Priority.SILENT
            self.timeline.add("stale_answer", n=n, latest=self._finals)
        whisper = Whisper(
            thought.kind, priority, thought.text[:1800], id=f"w{self._whispers}", backed=thought.claimed_done
        )
        right = any(_says(said, whisper.text, self._owner_words) for said in self._said_for.get(n, []))
        if (
            n in self._wrong_before
            and priority is not Priority.SILENT
            and not right
            and thought.kind is Kind.SAY
        ):
            # the talker said a fact of its own while Pisa's brain worked: Pisa's answer corrects it now
            # (a done or a question is always said anyway)
            whisper = Whisper(Kind.CORRECT, Priority.NOW, whisper.text, id=whisper.id, backed=whisper.backed,
                              replaces="talker")  # fmt: skip
        # a yes may answer Pisa's question only when Pisa's voice got all of it
        voiced = await self._deliver(whisper, n, whole=not thought.shortened and whisper.text == thought.text)
        if not voiced:  # the chat keeps what Pisa's voice said for these words, not the unspoken reply
            self._heard_for[n] = thought.correction.strip()
            for old in [k for k in self._heard_for if k <= n - SAID_KEPT]:
                del self._heard_for[old]
            await self._heard_stored(n)
        if thought.correction.strip():
            # its own line after the answer, said even when the answer went in silently (the talker
            # answered first: what it said may be the very claim corrected)
            fix = Priority.SILENT if n <= self._dropped_upto else Priority.PAUSE
            await self._whisper(Whisper(Kind.SAY, fix, thought.correction[:1800], id=f"{whisper.id}-fix"))
        if self._exchange is not None and self._exchange.whisper_ms is None:
            self._exchange.whisper_ms = int((time.monotonic() - at) * 1000)
            self._exchange.whisper_kind = whisper.kind.value
        if not self._speaking:
            self._send_state("listening")

    async def _hold_later(self) -> None:
        await asyncio.sleep(HOLD_WHISPER_AFTER_S)
        text = _HOLD[self.lang if self.lang in _HOLD else "en-US"]
        await self._whisper(Whisper(Kind.HOLD, Priority.SILENT, text, id="hold"))

    async def _deliver(self, whisper: Whisper, n: int, *, whole: bool = True) -> bool:
        """The brain's answer to the user's final words n: to the talker's ask_brain call waiting for them,
        else by itself. RAIL: `done` and `ask` are always said (unless the user stopped Pisa); a plain answer
        the talker already gave goes in silently (no answering twice). `whole`: the whisper is the brain's
        whole reply (never shortened or cut). False when it went in silently (Pisa's voice doesn't say it)."""
        self._thought_for = n
        if whisper.kind is Kind.ASK and whisper.priority is not Priority.SILENT:
            # a new question replaces the last one, and a spoken yes counts only
            # once Pisa said this one aloud, and only when its whisper held all of it (the question included)
            self._ask, self._ask_spoken_at = (whisper if whole else None), None
        self._given = [*self._given, whisper][-GIVEN_KEPT:]
        self._current.append(whisper)
        silent = Whisper(Kind.DONE if whisper.kind is Kind.DONE else Kind.FACT, Priority.SILENT, whisper.text,
                         id=whisper.id, backed=whisper.backed)  # fmt: skip
        mine = [call for call, waits in self._calls if waits == n]
        self._calls = [(call, waits) for call, waits in self._calls if waits != n]
        # the reflex's ready answer for these words was given to Pisa's voice already: never twice
        ready = self._ready_for.get(n)
        voiced = [*self._said_for.get(n, []), *([ready.text] if ready is not None else [])]
        # Pisa's voice may answer by itself and then say the brain's paraphrase at the pause. A plain answer
        # after a line of content goes in silently unless it adds a fact (a time, a number, a name) or a no; a
        # wrong claim of Pisa's voice is corrected by grounding (`_check`)
        talked = [said for said in self._said_for.get(n, []) if not holding_line(said)]
        again = whisper.kind is Kind.SAY and bool(talked) and not _adds_to(whisper.text, talked)
        out_loud = True
        if mine:
            await self._answer(mine[0], silent if again else whisper)
            out_loud = not again and whisper.priority is not Priority.SILENT
            for call in mine[1:]:
                await self._answer(call, silent)
        elif whisper.priority is Priority.SILENT or again:
            await self._whisper(silent)
            out_loud = False
        elif whisper.kind in (Kind.DONE, Kind.ASK, Kind.CORRECT) or not any(
            _says(said, whisper.text, self._owner_words) for said in voiced
        ):
            await self._whisper(whisper)
        else:
            await self._whisper(silent)
            out_loud = False
        await self._answered_thought(n)
        return out_loud

    async def _heard_stored(self, n: int) -> None:
        """The brain's reply to the words n went in silently: the turn's stored reply becomes what Pisa's
        voice said for them (no holding line, no correction: that is its own message). Again as each new line
        for those words is said; nothing while the voice said nothing yet."""
        trace, fix = self._trace_for.get(n), self._heard_for.get(n, "")
        lines = [
            line
            for line in self._said_for.get(n, [])
            if line.strip() and not holding_line(line) and not (fix and _says(line, fix))
        ]
        if trace is None or not lines:
            return
        self.timeline.add("stored_as_said", n=n, lines=len(lines))
        try:
            await self.hooks.spoken(trace, lines)
        except Exception as err:  # the chat keeps the brain's reply rather than the talk failing
            _log.warning("talk_said_not_stored", session_id=str(self.session_id), error=type(err).__name__)

    async def _answered_thought(self, n: int) -> None:
        """The brain's thought on the user's words n is in (or there is none): the done claims Pisa made
        before it are judged now."""
        self._thought_for = max(self._thought_for, n)
        if self._thought_for < self._finals:
            return
        waiting, self._unchecked = self._unchecked, []
        for said, early in waiting:
            await self._check(said, early=early)

    async def _unanswered(self, n: int, text: dict[str, str] | None = None) -> None:
        """Calls waiting on the user's words n that no thought will answer: a HOLD that tells the talker
        so."""
        mine = [call for call, waits in self._calls if waits == n]
        self._calls = [(call, waits) for call, waits in self._calls if waits != n]
        table = text or _UNHEARD
        for call in mine:
            line = table[self.lang if self.lang in table else "en-US"]
            await self._answer(call, Whisper(Kind.HOLD, Priority.PAUSE, line, id="unheard"))

    async def _asked(self, call_id: str, waits: int) -> None:
        """An ask_brain call. RAIL (3, MED 7): only the user's own final words reach
        Pisa's brain, never the talker's question. The call waits for the thought on the user's words it
        belongs to: the ones being answered now, or (when those were answered already: the transcript lags)
        the next ones, for ASK_WAIT_S; with none, the talker is told Pisa didn't hear and asks the user
        again."""
        if waits <= self._finals:
            return  # the thought on its way answers it
        # after a talk.stop the talker's call waits for the user's next words, however long (
        # an empty final from noise, or seconds of silence, never end that wait)
        stopped = self._stopped
        started = time.monotonic()
        closed = self._closed_words
        while self._finals < waits:
            if stopped:
                await self._final.wait()
                continue
            # while their words for these finals arrive, the wait goes on until their final, or until they
            # close with nothing in them. They arrive while a new partial word, or their voice in
            # the phone's audio, came within ASK_WAIT_S; the cap is ASK_WORDS_WAIT_S from their last partial
            # words (never from the call: a long dictation is never cut); partials that stop and never close
            # are nothing of theirs after ASK_WAIT_S.
            words = self._words_for == waits and self._words_at > 0
            if words and self._closed_words > closed:
                break
            if words:
                arriving = max(self._words_at, self._voice_at or 0.0)
                deadline = min(arriving + ASK_WAIT_S, self._words_at + ASK_WORDS_WAIT_S)
            else:
                deadline = started + ASK_WAIT_S
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(self._final.wait(), left)
            except TimeoutError:
                continue  # their words may have gone on meanwhile: the deadline moves with them
        if self._finals >= waits or (call_id, waits) not in self._calls:
            return  # the user's words came: their thought answers it
        self._calls.remove((call_id, waits))
        text = _UNHEARD[self.lang if self.lang in _UNHEARD else "en-US"]
        await self._answer(call_id, Whisper(Kind.HOLD, Priority.PAUSE, text, id="unheard"))

    async def _check(self, said: str, *, early: bool, n: int | None = None) -> None:
        """RAIL: Pisa's words against what it was given; a "done" with no `done` in this exchange is
        corrected. A done the brain itself states in this exchange's whisper ("Sì, l'ho messo alle 21",
        its own claim rail backs it) counts; claims Pisa said before the brain answered (`early`) wait for
        it."""
        # an early claim too is judged against this exchange only (an earlier
        # `done` let "Fatto" pass while this exchange's thought asked for a yes); "l'hai messo davvero?"
        # stands on the brain's own "Sì, l'ho messo" in this exchange.
        del early
        # Pisa's voice may also state what the shared state holds (Pisa's data read for these words)
        state = [Whisper(Kind.FACT, Priority.SILENT, t[:1800], id="state") for t in self.hooks.supports()]
        await self._own_words(said, [*self._given, *state], self._finals if n is None else n)
        result = grounding(said, [*self._given, *state], self._owner_words, self.fresh, current=self._current)
        if result.ok:
            return
        self.incidents += 1
        _log.warning("talk_grounding", session_id=str(self.session_id), unsupported=len(result.unsupported),
                     done_without_done=result.done_without_done)  # fmt: skip
        self.timeline.add("grounding", said=said, unsupported=len(result.unsupported),
                          done_without_done=result.done_without_done)  # fmt: skip
        if result.done_without_done:
            if self._exchange is not None and self._exchange.final_at is not None:
                self._exchange.unbacked += 1  # the talker's "fatto" with no record
            text = NOT_DONE[self.lang if self.lang in NOT_DONE else "en-US"]
            await self._whisper(Whisper(Kind.CORRECT, Priority.NOW, text, id="fix", replaces="talker"))
        # Only a time or a number the talker said in digits ("una cosa", "sei a posto",
        # "due minuti" read as 1, 6, 2 made Pisa correct a filler and loop), and once per exchange.
        written = {m.group(0).lower().replace(".", ":") for m in _DIGITS.finditer(said)}
        if not any(claim in written for claim in result.unsupported):
            return  # a name or a number word the heuristic took for a fact: counted, never corrected aloud
        # RAIL: the talker said a time or a number Pisa never gave it.
        # Pisa's answer to those words is said at once, as a correction: never
        # silently because the talker "already answered".
        exchange = self._finals if n is None else n
        if exchange in self._corrected:
            return
        self._corrected.add(exchange)
        if self._thought_for < exchange:
            self._wrong_before.add(exchange)
        elif self._current and self._current[-1].kind is not Kind.HOLD:
            answer = self._current[-1]
            await self._whisper(Whisper(Kind.CORRECT, Priority.NOW, answer.text, id=f"{answer.id}-again",
                                        replaces="talker", backed=answer.backed))  # fmt: skip

    async def _own_words(self, said: str, given: list[Whisper], n: int) -> None:
        """A cause or a promise the talker said of its own ("ho avuto un
        problema col sistema vocale", "cercherò di risolvere…"): an incident, and the talker takes it back,
        once per final words."""
        kind = own_words(said, given, self._owner_words)
        if kind is None:
            return
        self.incidents += 1
        _log.warning("talk_own_words", session_id=str(self.session_id), kind=kind)
        self.timeline.add("own_words", said=said, own=kind, n=n)
        if n in self._own_fixed:
            return
        self._own_fixed.add(n)
        text = OWN_WORDS[self.lang if self.lang in OWN_WORDS else "en-US"]
        await self._whisper(Whisper(Kind.CORRECT, Priority.NOW, text, id="own", replaces="talker"))

    async def _whisper(self, w: Whisper) -> None:
        self._record(w)
        await self.talker.whisper(w)

    async def _answer(self, call_id: str, w: Whisper) -> None:
        self._record(w, call_id)
        await self.talker.answer(call_id, w)

    def _record(self, w: Whisper, call_id: str | None = None) -> None:
        """Each whisper in the timeline: its kind, priority, the call it answers, how long after the
        user's latest final words, and its words."""
        # Pisa's next line may carry these words. Only a whisper
        # the talker is to say (SAY, ASK, CORRECT, DONE, not silent); a silent fact or the state refresh
        # says nothing new, so it never lets a whole line be said again
        if w.kind in _SPOKEN_KINDS and w.priority is not Priority.SILENT:
            self._whispered_at = time.monotonic()
        after = int((time.monotonic() - self._last_final_at) * 1000) if self._last_final_at else None
        self.timeline.add("whisper", id=w.id, whisper_kind=w.kind.value, priority=w.priority.value,
                          call=call_id, after_final_ms=after, text=w.text)  # fmt: skip
        e = self._exchange
        heard = w.id.startswith("heard")  # their words said back are Pisa's first content
        content = (w.kind is not Kind.HOLD or heard) and (
            w.priority is not Priority.SILENT or call_id is not None
        )
        if content and e is not None and e.final_at is not None and e.first_content_ms is None:
            # time to first content, from the user's stop to the first words Pisa's voice may say
            e.first_content_ms = int((time.monotonic() - e.owner_end) * 1000)
            by = next(
                (by for by in ("reflex", "final", "next", "heard", "ready") if w.id.startswith(by)), "brain"
            )
            e.first_content_by = "reflex" if by == "final" else by  # the reflex on the final words
            self.timeline.add(
                "first_content", took_ms=e.first_content_ms, by=e.first_content_by, route=e.route
            )

    def _send_state(self, state: State) -> None:
        if state != self._state:
            self._state = state
            self.timeline.add("state", state=state)
            self.link.send({"type": "talk.state", "state": state})


_CLOSED_BY: dict[str, ClosedBy] = {
    "idle": "idle", "owner": "phone", "budget": "budget", "error": "error", "fallback": "talker",
    "route": "phone",
}  # fmt: skip
_DIGITS = re.compile(r"\b\d{1,2}(?:[:.]\d{2})?\b")
_LINE_END = (".", "!", "?", "…")  # a line's words end a sentence here
# Words that take back or correct what came before, at the end of a question (no ready answer then).
_TAKE_BACK = frozenset({"no", "anzi", "invece", "aspetta", "meglio", "wait", "instead", "actually",
                        "spostalo", "spostala", "cancellalo", "cancellala"})  # fmt: skip
_HOLD = {"it-IT": "Sto ancora controllando.", "en-US": "Still checking."}
# a tap on something no longer open (said by Pisa's voice)
TAP_GONE = {
    "it-IT": "Quella scelta non è più aperta: dimmelo a voce.",
    "en-US": "That choice isn't open any more: just tell me.",
}
# the notice's title for the answer to words said as the talk closed
_LAST_WORDS = {"it-IT": "Le tue ultime parole", "en-US": "Your last words"}
_FAILED = {
    "it-IT": "Non riesco a controllare adesso: riprova tra poco.",
    "en-US": "I can't check that right now: try again in a moment.",
}
_NOTHING = {
    "it-IT": "Pisa non ha niente da aggiungere: rispondi tu, in poche parole.",
    "en-US": "Pisa has nothing to add: answer yourself, in a few words.",
}
_UNHEARD = {
    "it-IT": "Pisa non ha sentito bene le parole del proprietario: chiedigli di ripetere, in poche parole.",
    "en-US": "Pisa didn't catch the user's words: ask them to say it again, in a few words.",
}


def resampled(pcm: bytes, rate_in: int, rate_out: int) -> bytes:
    """16-bit PCM from one rate to another (linear: the talker's ears, never played to anyone)."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = len(x) * rate_out // rate_in
    if n == 0:
        return b""
    y = np.interp(np.arange(n) * (rate_in / rate_out), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def spoken_reply(text: str) -> str:
    """The brain's reply as one whisper: without research's first line ("Cerco, un attimo"), which is for the
    phone's voice in economy."""
    reply = text.strip()
    for line in WORKING.values():
        if reply.startswith(line):
            reply = reply[len(line) :].strip()
    return reply


def _final_words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def one_utterance(text: str) -> str:
    """A final that says the user's words twice over, whole or grown ("Che ora ho
    l'appuntamento? Che ora ho l'appuntamento?", "Che ora ho? Che ora ho l'appuntamento?"), is said once: the
    later, longer form is kept. Three words at least repeat, so "no no" and "sì sì, va bene" stay as said."""
    while True:
        spans = [(m.start(), m.group(0).casefold()) for m in re.finditer(r"\w+", text)]
        words = [w for _, w in spans]
        cut = _repeat_at(words)
        if cut is None:
            return text
        start, keep_later = cut
        text = text[spans[start][0] :] if keep_later else text[: spans[start][0]].rstrip(" ,")


def _repeat_at(words: list[str]) -> tuple[int, bool] | None:
    """Where the words begin again, word for word (a word cut short by the transcript counts: "appunta"):
    (k, True) when words[k:] restate all of words[:k] (keep from k: the later form, maybe grown); (k, False)
    when the tail from k restates the start, shorter (keep up to k). "Metti la call alle 5, metti la call alle
    6" is two requests: 5 is not 6, nothing is cut."""
    n = len(words)
    for k in range(3, n - 2):
        size = min(k, n - k)
        if size < 3 or not all(
            _same_word(a, b) for a, b in zip(words[:size], words[k : k + size], strict=True)
        ):
            continue
        return (k, True) if n - k >= k else (k, False)
    return None


def _same_word(a: str, b: str) -> bool:
    """The same word, or one the transcript cut short ("appunta" of "appuntamento"); a real word is never a
    cut one ("lista pane" is not "lista panettone")."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short) and _cut_short(short)


# a final whose last word needs what follows ("…chiamandolo", "…alle", "…con il"): cut mid-phrase
_DANGLING = frozenset(
    "chiamandolo chiamandola chiamalo chiamala chiamato chiamata intitolato intitolata intitolalo titolo "
    "nome di del della dei delle per con a al alla alle ai da dal dalla in nel nella su sul sulla tra fra il "
    "lo la le gli un una uno e o ma che se tipo cioè called named titled the a an to of for with and or at "
    "on in from".split()
)
DANGLING_WAIT_S = 2.0
# words that end on a connector wait as the economy path's unfinished turn does (the turn rules' wait) ...
CONNECTOR_WAIT_S = turn_rules.WAITS_MS["unfinished"] / 1000
CONNECTOR_MAX_S = 12.0  # ... and on while the user's voice still comes, this long at most
_FILLERS = frozenset("eh ehm mmm uhm er erm hmm uh um".split())


def _trails(text: str, lang: str) -> bool:
    """The words end on a connector, the turn rules' unfinished last word ("Scusa… e poi", "and then",
    "and…"): the rest is still to come. Never a question, an exclamation or a sentence closed with a full
    stop."""
    said = text.rstrip()
    found = turn_rules.words(said)
    if not found or said.endswith(("?", "!", ".")) or set(found) <= _FILLERS:
        return False  # a lone "ehm" is no connector: nothing of theirs to wait for
    return found[-1] in turn_rules.RULES[turn_rules.language(lang)].unfinished_last


def _dangling(text: str) -> bool:
    words = _final_words(text)
    return len(words) >= 3 and words[-1] in _DANGLING and not text.rstrip().endswith(("?", "!", "."))


def _cut_short(word: str) -> bool:
    return zipf_frequency(word, "it") < WORD_ZIPF and zipf_frequency(word, "en") < WORD_ZIPF


def _grows(before: list[str], after: list[str]) -> bool:
    """The later final restates the earlier one and goes on ("Ciao Ilung, mi suggeri…" → "Ciao Ilunga, mi
    suggerisci un film per stasera?"): most of the earlier words begin the later ones, word for word."""
    if not before or len(after) <= len(before):
        return False
    same = sum(1 for a, b in zip(before, after, strict=False) if a.startswith(b) or b.startswith(a))
    return same * 4 >= len(before) * 3


# Gemini's own placeholders in Pisa's transcript, never words Pisa said
PLACEHOLDER = re.compile(
    r"<!--.*?(?:-->|$)|[\[<]\s*(?:no speech(?: detected)?|no response|silence|inaudible)\s*[\]>]"
    r"|\{\s*[^\s{}]*\s*\}",
    re.I | re.S,
)
_DOTS = re.compile(r"[\s.…]*")  # a line of only "..." is no line


def unmarked(text: str) -> str:
    """Pisa's words without Gemini's placeholders: a line of only dots is empty."""
    text = PLACEHOLDER.sub("", text)
    return "" if _DOTS.fullmatch(text) else text


def _noise(text: str, words: list[str]) -> bool:
    """Final words with nothing in them to answer: no word of three letters or more, no number, and not a yes
    or a no ("AJ", "A", "e")."""
    return not _bare_yes_or_no(text) and not any(len(w) >= 3 or w.isdigit() for w in words)


def _in_order(words: list[str], heard: list[str]) -> int:
    """How many of `words` appear in `heard` in the same order (a greedy subsequence; a word cut short by the
    transcript counts)."""
    got, i = 0, 0
    for w in words:
        j = i
        while j < len(heard) and not _same_word(w, heard[j]):
            j += 1
        if j < len(heard):
            got, i = got + 1, j + 1
    return got


def _asked_words(text: str) -> list[str]:
    """The words of a line's questions (its sentences ending in "?"), numbers as digits."""
    return [w for question in re.findall(r"[^.!?…]+\?", text) for w in form.echo_words(question)]


def _near_word(word: str, said: str) -> bool:
    """The same word, cut short, or one letter off at its end ("attimi" for "attimo", "certa" for "certo"): a
    transcript of Pisa's own word heard back."""
    if _same_word(word, said):
        return True
    if min(len(word), len(said)) < 4 or abs(len(word) - len(said)) > 1:
        return False
    common = next(
        (i for i, (a, b) in enumerate(zip(word, said, strict=False)) if a != b), min(len(word), len(said))
    )
    return common >= max(len(word), len(said)) - 1


_LANGS = ("it", "en", "fr", "es", "de", "pt")


def _foreign(word: str, lang: str) -> bool:
    """A word of another language and not of the session's."""
    own = lang.split("-")[0]
    if zipf_frequency(word, own) >= WORD_ZIPF:
        return False
    return any(zipf_frequency(word, other) >= WORD_ZIPF for other in _LANGS if other != own)


def _bare_yes_or_no(text: str) -> bool:
    return is_short_yes(text) or action_rules.is_short_no(text)


def _says(said: str, asked: str, owner: str = "") -> bool:
    """Whether Pisa's words carry what it was given: more than half of its content words, not counting the
    user's own ("Vediamo domani…" doesn't answer "domani piove"), every number in it, and the same yes or no
    ("Sì, piove" doesn't carry "No, non piove")."""
    said_l, asked_l = said.lower(), asked.lower()
    heard = set(re.findall(r"\w+", owner.lower()))
    want = {w for w in re.findall(r"\w+", asked_l) if len(w) > 3 and w not in heard}
    got = set(re.findall(r"\w+", said_l))
    numbers = set(re.findall(r"\d+", asked_l))
    if not (want or numbers) or len(want & got) * 2 < len(want) + (1 if want else 0):
        return False
    if not numbers <= set(re.findall(r"\d+", said_l)):
        return False
    return bool(_NO.search(said_l)) == bool(_NO.search(asked_l))


_NO = re.compile(r"\b(?:no|non|not|never|mai)\b|n't\b")
# a single word of the user's that stops Pisa mid-line
_STOP_WORDS = frozenset({"no", "stop", "basta", "aspetta", "wait", "fermo", "fermati"})


def _adds_to(answer: str, talked: list[str]) -> bool:
    """The brain's answer says more than Pisa's voice lines `talked`: a time, a number or a name they lack, or
    the other yes or no ("Va bene" then "Non l'ho cancellato")."""
    said = " ".join(talked).lower()
    return adds_fact(answer, talked) or bool(_NO.search(answer.lower())) != bool(_NO.search(said))


# --- the service: opening, metering, closing --------------------------------------------------------------


@dataclass(frozen=True)
class Opening:
    """How a talk opens beyond the fields: where the user hears it, Pisa's own line to say
    first and the ask it answers, the kinds the phone shows (the WebSocket hello's, all with none),
    and what a tap on a shown choice or approval does (the API's: the conversation's and the approvals'
    own paths)."""

    route: str | None = None
    line: str = ""
    ask_id: uuid.UUID | None = None
    kinds: frozenset[str] = frozenset(OBJECT_KINDS) - {"sheet", "drawing"}
    blocks: frozenset[str] | None = None  # the drawing blocks the phone draws (its WebSocket hello's)
    chosen: Callable[[str, str], Awaitable[str | None]] = lambda object_id, option: _no_words()
    # a tap on a drawing's element (its object id, the element id) → the turn's words
    # ("Sul disegno: …"), None when gone
    asked: Callable[[str, str], Awaitable[str | None]] = lambda object_id, element_id: _no_words()
    decide: Callable[[str, bool], Awaitable[tuple[str, bool]]] = lambda approval_id, yes: _not_open()
    # the answer to the last words after a lost peer, written into the talk's turn (its id, the text, the
    # language) on the app's open live session: True when it went (else it is a notice)
    in_card: Callable[[str | None, str, str], bool] = lambda turn_id, text, lang: False
    # the brain's reply to a turn went in silently: what Pisa's voice said for those words is the turn's
    # stored reply (the turn's trace id, the lines said)
    spoken: Callable[[str, list[str]], Awaitable[None]] = lambda trace_id, lines: _nothing()


@dataclass
class Opened:
    session_id: uuid.UUID
    talker: Talker
    session: TalkerSession
    call_id: uuid.UUID  # the ledger's row
    started: float


async def _opened(talker: Talker, cfg: TalkerConfig) -> TalkerSession:
    """The talker's session, or TalkerError for any failure to open it (a connector's unexpected error is a
    refusal too: the phone then hears `talker_unavailable`, never a 500)."""
    try:
        return await talker.open(cfg)
    except TalkerError:
        raise
    except Exception as exc:
        reason = redact(str(exc), cfg.key)
        raise TalkerError(f"{talker.name} failed to open ({exc.__class__.__name__}).", None, reason) from None


class TalkService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        talkers: Mapping[str, Talker],
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        reflex: "ReflexService | None" = None,
        ready: "ReadyService | None" = None,
        voice: VoiceIdService | None = None,
        hard_limits: HardLimits = hard_limit.DEFAULT,
        asked_aloud: Callable[[uuid.UUID], Awaitable[None]] | None = None,
        ollama_base_url: str | None = None,
    ) -> None:
        self._pool = pool
        self._ollama = ollama_base_url  # for the AI disclosure the talk's consent is checked against
        self._hard = hard_limits
        self._voice = voice  # the user-voice gate (None: never gated)
        self._keys = keys
        self._talkers = talkers
        self._reflex = reflex  # the reflex and the shared state, per device
        self._answers = ready
        self._owner = owner
        self._clock = clock
        self.runs: dict[uuid.UUID, TalkRun] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._by_device: dict[uuid.UUID, uuid.UUID] = {}
        self._resume: dict[uuid.UUID, tuple[str, datetime]] = {}
        self._opening: dict[uuid.UUID, asyncio.Lock] = {}  # one POST at a time per device
        self._device_of: dict[uuid.UUID, uuid.UUID] = {}  # an open talk's device
        # talks that ended lately, with their device: a DELETE for one is answered as done (idempotent)
        self._ended: OrderedDict[uuid.UUID, tuple[uuid.UUID, float]] = OrderedDict()
        self._owner_ends: set[uuid.UUID] = set()  # talks the phone ended itself (talk.end or the DELETE)
        self._revoked_devices: set[uuid.UUID] = set()  # devices `revoked` ended (a revoke is for good)
        self._run_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}  # an open talk's run, until it is stored
        # an open enrolment: its device and how it stops (its peer closed)
        self._enrolling: dict[uuid.UUID, tuple[uuid.UUID, Callable[[], Awaitable[None]]]] = {}
        # a device's asked cards read out now
        self.asked_aloud = asked_aloud

    async def _read_out_now(self, device_id: uuid.UUID) -> None:
        if self.asked_aloud is not None:
            await self.asked_aloud(device_id)

    # --- whether natural mode may run now ------------------------------------------------------------------

    async def _ready(self, conn: AsyncConnection, lang: str | None = None) -> tuple[Talker, str, str]:
        """(talker, voice, key) or PisaError: economy by setting or by the guard (from 50% of a cap), or a
        missing key."""
        from pisa.services.conversation import CHEAP_AT  # the guard's one threshold, for both modes

        settings = await self._owner.settings(conn)
        conversation = settings["conversation"]
        if conversation["mode"] == "economy":
            raise PisaError(
                "talk_economy", "Conversation mode is economy (Settings → Conversation).", "Talk in economy",
                extra={"economy_reason": "setting"},
            )  # fmt: skip
        now = self._clock()
        day, month = period_starts(now, self._owner.tz)
        caps = await ledger.get_caps(conn)
        today = await ledger.spent_since(conn, await ledger.cap_day_start(conn, day))
        this_month = await ledger.spent_since(conn, month)
        if today >= caps.daily_eur * CHEAP_AT or this_month >= caps.monthly_eur * CHEAP_AT:
            reason = "day_guard" if today >= caps.daily_eur * CHEAP_AT else "month_guard"
            share = int(CHEAP_AT * 100)
            # the month's guard says so: not "for the rest of the day"
            message = (
                f"Today's AI spend passed {share}% of a cap: Pisa talks in economy for the rest of the day."
                if reason == "day_guard"
                else f"This month's AI spend passed {share}% of a cap: Pisa talks in economy until the "
                "month's spend is under it."
            )
            raise PisaError(
                "talk_economy",
                message,
                "Talk in economy, or raise the cap in Settings → AI spend",
                extra={"economy_reason": reason},
            )
        # a talker this install doesn't offer (the experimental xAI talker, off): the Gemini talker
        talker = self._talkers.get(conversation["talker"]) or self._talkers["gemini"]
        key = await self._keys.get(conn, talker.key_provider)
        if not key:
            raise PisaError(
                "provider_key_missing",
                f"{talker.name} needs the {talker.key_provider} key, and none is set.",
                "Add it in Settings → AI providers, or pick the other talker",
                extra={"provider": talker.key_provider},
            )
        if talker.key_provider == google_tier.PROVIDER and not google_tier.talker_allowed(
            await get_setting(conn, "settings")
        ):  # RAIL: Gemini Live hears the whole talk: never on Google's unpaid services (Gemini API terms)
            _log.info("google_ai_tier_refused", code=google_tier.CODE, talker=talker.id)
            raise PisaError(google_tier.CODE, *google_tier.refusal(lang or self._owner.tag, talker=True))
        return talker, conversation["voices"][talker.id], key

    # --- opening -------------------------------------------------------------------------------------------

    async def open(
        self,
        device_id: uuid.UUID,
        *,
        trigger: str,
        lang: str,
        link_for: Callable[[int], Awaitable[Link]],
        think: Think,
        first_text: str = "",
        opening: Opening | None = None,
    ) -> tuple[uuid.UUID, Link]:
        """A new conversation for the device (its previous one ends). Raises PisaError talk_economy,
        provider_key_missing, budget_exceeded or talker_unavailable: the app then talks in economy.
        `first_text`: the words said with the wake phrase, the user's first final words.
        `opening`: the route, Pisa's own line to start with, the screen's kinds and the taps."""
        lock = self._opening.setdefault(device_id, asyncio.Lock())
        async with lock:  # two POSTs at once never make two sessions
            return await self._open(
                device_id, trigger, lang, link_for, think, first_text, opening or Opening()
            )

    async def _open(
        self,
        device_id: uuid.UUID,
        trigger: str,
        lang: str,
        link_for: Callable[[int], Awaitable[Link]],
        think: Think,
        first_text: str,
        opening: "Opening",
    ) -> tuple[uuid.UUID, Link]:
        if (previous := self._by_device.get(device_id)) is not None and previous in self.runs:
            self.runs[previous].end("owner", "replaced")
        session_id = uuid.uuid4()
        now = self._clock()
        async with self._pool.connection() as conn:
            talker, voice, key = await self._ready(conn, lang)
            # the talker hears their voice: their AI consent must cover it, as a run's (CONTRACT-2 §4.4); and
            # it hears everything said in the talk, not only the first words, so health consent too
            await disclosure.require(conn, device_id, first_text, self._ollama, lang, health=True)
            fresh = await self.fresh(conn, device_id, lang)
            seed = await self._seed(conn, device_id)
            rate = await usd_to_eur(conn)
            minute = (Decimal(str(talker.usd_per_min)) * rate).quantize(Decimal("0.000001"))
            call_id = await ledger.reserve(
                conn, slot=SLOT, model_ref=f"{talker.id}:{voice}", estimate_eur=minute, price_known=True,
                trace_id=session_id.hex, now=now, tz=self._owner.tz, hard=self._hard,
            )  # fmt: skip
        resume = self._resume.get(device_id)
        # the gate is on when the switch is, they consented, and this route has their prints; the
        # talker's instructions are the same either way (nothing is added to any prompt)
        gated = await self._voice.gate_for(opening.route) if self._voice is not None else None
        cfg = TalkerConfig(
            key=key, voice=voice, lang=lang,
            instructions=instructions(fresh, seed, lang, first_text, pisa_lang=self._owner.tag),
            resume=resume[0] if resume and now - resume[1] < RESUME_FOR else None, route=opening.route,
            client_activity=gated is not None,
        )  # fmt: skip
        session: TalkerSession | None = None
        link: Link | None = None
        try:
            try:
                session = await _opened(talker, cfg)
            except TalkerError as err:
                if cfg.resume is None:
                    raise
                # Google may no longer know the last session (1008 "session not found or expired"), which
                # would drop the talk to economy: a refused resumption opens a fresh session instead.
                self._resume.pop(device_id, None)
                _log.info("talker_resume_refused", talker=talker.id, status=err.status, reason=err.reason)
                cfg = replace(cfg, resume=None)
                session = await _opened(talker, cfg)
            link = await link_for(session.output_rate)
            opened = Opened(session_id, talker, session, call_id, time.monotonic())
            hooks = Hooks(
                think=think,
                fresh=lambda: self._fresh_now(device_id, lang),
                meter=lambda: self._meter(opened, minute),
                exchange=lambda e: self._store_exchange(session_id, e),
                redone=lambda n: self._store_redone(session_id, n),
                resumable=lambda handle: self._resume.__setitem__(device_id, (handle, self._clock())),
                chosen=opening.chosen,
                asked=opening.asked,
                decide=opening.decide,
                first_said=lambda: self._asked_said(opening.ask_id),
                asked_aloud=lambda: self._read_out_now(device_id),
                routed=lambda route: self._routed(session_id, route),
                after=lambda words, thought: self._after(
                    device_id, lang, think, words, thought, run=run, in_card=opening.in_card
                ),
                spoken=opening.spoken,
            )
            run = TalkRun(session_id, session, link, hooks, lang, fresh, first_text=first_text)
            run.route = opening.route
            if gated is not None and self._voice is not None:
                voice_id, profile = self._voice, gated[0]
                run.gate = LiveGate(
                    await voice_id.loaded(), profile, gated[1], run,
                    still=lambda: voice_id.still(profile.route, profile.model_version),
                )  # fmt: skip
                run.timeline.add("gate", decision="on", route=opening.route, model=gated[0].model)
            run.screen = TalkScreen(link, opening.kinds, lang, opening.blocks)
            run.first_line = opening.line
            if self._reflex is not None:
                self._shared(hooks, run, device_id, lang)
            if (answers := self._answers) is not None:  # ready for this talk's questions
                answers.refresh_soon(lang)
                hooks.ready_answer = lambda words: answers.match(words, lang)
                hooks.changed = answers.changed
            # the phone vanished: nothing more is metered; wired before any await
            link.on_closed = lambda: run.end(*peer_gone(link))
            async with self._pool.connection() as conn:  # inside the try, like the rest
                await conn.execute(
                    "INSERT INTO talk_sessions (id, device_id, talker, voice, trigger, lang, started_at, "
                    "llm_call_id, route, ask_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (session_id, device_id, talker.id, voice, trigger, lang, now, call_id, opening.route,
                     opening.ask_id),
                )  # fmt: skip
        except BaseException as err:
            # a failed or cancelled POST (the phone gave up) leaves no reservation, no open talker, no
            # peer
            await asyncio.shield(self._abandon(call_id, session, err, link))
            if isinstance(err, TalkerError):
                _log.warning("talker_open_failed", talker=talker.id, status=err.status, reason=err.reason)
                external_failure(talker.id, "open", err.status, err.reason or err.message)
                raise PisaError("talker_unavailable", err.message, "Pisa talks in economy for now") from None
            raise
        self.runs[session_id] = run
        self._by_device[device_id] = session_id
        self._device_of[session_id] = device_id
        task = asyncio.create_task(self._run(opened, run, device_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._run_tasks[session_id] = task
        task.add_done_callback(lambda _: self._run_tasks.pop(session_id, None))
        _log.info("talk_opened", session_id=str(session_id), talker=talker.id, trigger=trigger)
        _log.info("talk_session_opened", session_id=str(session_id), talker=talker.id, mode="natural")
        # E1: a revoke while the talker was opening found no talk of the device's to end; registered now, the
        # device is checked again and its talk ends at once (a later revoke finds it in `runs`)
        if await self._device_revoked(device_id):
            self._owner_ends.add(session_id)
            run.revoke()
            _log.info("talk_revoked", session_id=str(session_id), device_id=str(device_id), opening=True)
        return session_id, link

    async def _device_revoked(self, device_id: uuid.UUID) -> bool:
        """The device was revoked: `revoked` ran for it, or its row says so (the revoke committed, its notice
        not yet here). An unreachable database leaves the talk to the revoke's own path."""
        if device_id in self._revoked_devices:
            return True
        try:
            async with self._pool.connection() as conn:
                return (await devices.revoked(conn, [device_id])).get(device_id, True)
        except Exception:
            _log.warning("talk_revoke_check_failed", device_id=str(device_id))
            return False

    async def enrol(self, device_id: uuid.UUID, route: str, link: Link) -> uuid.UUID:
        """An enrol session on the talk's own path (the voiceprint hears what the gate will): no
        talker opens and no model money is spent; the phone's blocks are recorded until enrol.finish or
        enrol.cancel. A talk the device had open ends."""
        if self._voice is None:
            raise PisaError("internal", "Voice enrolment isn't available on this server.", "Update Pisa")
        if (previous := self._by_device.get(device_id)) is not None and previous in self.runs:
            self.runs[previous].end("owner", "replaced")
        session_id = uuid.uuid4()
        enrolment = await self._voice.start_enrolment(session_id, route, link.send)

        async def run() -> None:
            try:
                outcome = await run_enrolment(enrolment, lambda: link.audio_in(GATE_RATE), link.messages,
                                              link.close)  # fmt: skip
                _log.info("voice_enrolment_ended", session_id=str(session_id), outcome=outcome)
            except Exception:
                _log.exception("voice_enrolment_failed", session_id=str(session_id))
                link.send({"type": "enrol.done", "route": route, "ok": False, "reason": "error"})
                await link.close()
            finally:
                self._voice.enrolments.pop(session_id, None) if self._voice is not None else None

        async def stop() -> None:
            enrolment.finished.set()
            await link.close()

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._enrolling[session_id] = (device_id, stop)
        task.add_done_callback(lambda _: self._enrolling.pop(session_id, None))
        link.on_closed = lambda: (enrolment.finished.set(), None)[1]
        _log.info("voice_enrolment_opened", session_id=str(session_id), route=route, mini=enrolment.mini)
        return session_id

    def _after(
        self,
        device_id: uuid.UUID,
        lang: str,
        think: Think,
        words: str,
        thought: "asyncio.Future[Thought] | None",
        *,
        run: "TalkRun | None" = None,
        in_card: Callable[[str | None, str, str], bool] | None = None,
    ) -> None:
        """The user's last words, answered after the talk (never lost), unless the user ended it."""
        task = asyncio.create_task(
            self._answer_after(device_id, lang, think, words, thought, run=run, in_card=in_card)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _by_owner(self, run: "TalkRun | None") -> bool:
        """The user ended this talk themselves: their talk.end or the DELETE, even one that came after the
        phone's peer had already gone."""
        if run is None:
            return False
        return run.session_id in self._owner_ends or (run.reason == "owner" and run.closed_by == "phone")

    async def _answer_after(
        self,
        device_id: uuid.UUID,
        lang: str,
        think: Think,
        words: str,
        thought: "asyncio.Future[Thought] | None",
        *,
        run: "TalkRun | None" = None,
        in_card: Callable[[str | None, str, str], bool] | None = None,
    ) -> None:
        """The answer to the last words. Never after the user's own talk.end (a thought under way still ends
        and is kept in the conversation, as any turn): they ended it. After a lost peer (no talk.end), once:
        into the talk's own turn on the app's open live session (`in_card`), else, with the app closed, as a
        notice. A talk.end may follow the peer's close: it is waited for a moment."""
        if self._by_owner(run):
            _log.info("talk_last_words_left", device_id=str(device_id), by="owner")
            return
        if run is not None and run.closed_by == "peer":
            await asyncio.sleep(PEER_END_GRACE_S)  # the phone may have closed its peer before its talk.end
            if self._by_owner(run):
                _log.info("talk_last_words_left", device_id=str(device_id), by="owner")
                return
        try:
            answer = await thought if thought is not None else await think(words, uuid.uuid4().hex, False)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # the notice says it wasn't answered, rather than nothing
            _log.warning("talk_last_words_failed", device_id=str(device_id), error=type(err).__name__)
            answer = Thought(_FAILED[lang if lang in _FAILED else "en-US"], Kind.SAY)
        text = spoken_reply(answer.text).strip()
        if answer.correction.strip():  # its own line, after the answer
            text = f"{text}\n{answer.correction.strip()}".strip()
        if not text:
            return
        if self._by_owner(run):  # their talk.end came while Pisa thought
            _log.info("talk_last_words_left", device_id=str(device_id), by="owner")
            return
        if in_card is not None and in_card(run.last_turn if run is not None else None, text, lang):
            # the app is open: the answer goes in the talk's own turn, never also as a notice
            _log.info("talk_last_words_in_card", device_id=str(device_id), chars=len(text))
            return
        async with self._pool.connection() as conn:
            await notifications.enqueue(
                conn, kind="info", text=notifications.fit(text), lang=lang, now=self._clock(),
                timezone=self._owner.tz, device_id=device_id,
                summary=_LAST_WORDS.get(lang, _LAST_WORDS["en-US"]), path="talk.last_words",
            )  # fmt: skip
        _log.info("talk_last_words_answered", device_id=str(device_id), chars=len(text))

    async def _run(self, opened: Opened, run: TalkRun, device_id: uuid.UUID) -> None:
        reason: str = "error"
        try:
            reason = await run.run()
        except Exception:
            _log.exception("talk_failed", session_id=str(opened.session_id))
        finally:  # stored however the run ended (a cancel at shutdown too)
            await asyncio.shield(self._stored(opened, run, device_id, reason))

    async def _stored(self, opened: Opened, run: TalkRun, device_id: uuid.UUID, reason: str) -> None:
        seconds = time.monotonic() - opened.started
        async with self._pool.connection() as conn:
            rate = await usd_to_eur(conn)
            cost = (Decimal(str(opened.talker.usd_per_min)) * Decimal(seconds) / 60 * rate).quantize(
                Decimal("0.000001")
            )
            await ledger.settle(
                conn, opened.call_id, input_tokens=0, output_tokens=0, cost_eur=cost, price_known=True,
                latency_ms=int(seconds * 1000), now=self._clock(),
            )  # fmt: skip
            await conn.execute(
                "UPDATE talk_sessions SET ended_at = %s, end_reason = %s, seconds = %s, cost_eur = %s, "
                "grounding_incidents = %s, timeline = %s, closed_by = %s, close_detail = %s WHERE id = %s",
                (self._clock(), reason, round(seconds, 1), cost, run.incidents, Jsonb(run.timeline.stored()),
                 run.closed_by or "error", run.close_detail or None, opened.session_id),
            )  # fmt: skip
        self.runs.pop(opened.session_id, None)
        self._device_of.pop(opened.session_id, None)
        if self._by_device.get(device_id) == opened.session_id:
            self._by_device.pop(device_id, None)
        self._ended[opened.session_id] = (device_id, time.monotonic())
        while len(self._ended) > ENDED_KEPT:
            gone, _ = self._ended.popitem(last=False)
            self._owner_ends.discard(gone)
        _log.info("talk_ended", session_id=str(opened.session_id), reason=reason, by=run.closed_by,
                  detail=(run.close_detail or "").split(":", 1)[0] or None,  # the part or the talker's reason
                  seconds=round(seconds, 1))  # fmt: skip

    async def _abandon(
        self, call_id: uuid.UUID, session: TalkerSession | None, err: BaseException, link: Link | None = None
    ) -> None:
        for closing in (session, link):
            if closing is None:
                continue
            try:
                await closing.close()
            except Exception:
                _log.warning("talk_close_failed")
        code = "provider_error" if isinstance(err, TalkerError) else "internal"
        async with self._pool.connection() as conn:
            await ledger.fail(conn, call_id, error_code=code, latency_ms=0, now=self._clock())

    def _shared(self, hooks: Hooks, run: TalkRun, device_id: uuid.UUID, lang: str) -> None:
        """The run's side of the shared conversation state (the reflex's, per device)."""
        reflex = self._reflex
        assert reflex is not None
        state = reflex.state(device_id)

        def ready(words: str) -> str | None:
            # Used once, at the final it was drafted for, then gone (a ready answer never
            # cleared said the pre-move time again); never when the final words correct or command
            # ("…domani? no, dopodomani", "…spostalo alle 18").
            r, state.ready = state.ready, None
            fresh = r is not None and self._clock() - r.at <= FACTS_FOR
            if r is None or not fresh or not speculation.follows(r.words, words):
                return None
            added = speculation.words(words)[len(speculation.words(r.words)) :]
            if set(added) & _TAKE_BACK or rules_read(words).intent == "change":
                return None
            return r.text

        hooks.heard = lambda words: reflex.heard(device_id, words, lang, on_read=run.reflexed)
        hooks.ready = ready
        hooks.ready_final = lambda words: reflex.final(device_id, words, lang, on_read=run.reflexed)
        hooks.said = lambda text, backed, retried: state.pisa_said(
            text, "voice", self._clock(), backed=backed, retried=retried
        )
        hooks.state = lambda: state.for_talker(self._clock())
        hooks.supports = lambda: state.supports(self._clock())

    async def opened_for(self, device_id: uuid.UUID, ask_id: uuid.UUID) -> str:
        """The talk.ask_to_open this talk answers: its line (the notification's words), marked
        opened; "" for an unknown ask, another device's, one already opened, or one asked more than ASK_FRESH
        ago ("Esci adesso" hours later is false; an ordinary talk then)."""
        now = self._clock()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE talk_asks a SET opened_at = %s FROM notifications n WHERE a.id = %s "
                    "AND a.device_id = %s AND a.opened_at IS NULL AND a.asked_at > %s "
                    "AND n.id = a.notification_id RETURNING n.text",
                    (now, ask_id, device_id, now - ASK_FRESH),
                )
            ).fetchone()
        if row is not None:
            _log.info("talk_ask_opened", device_id=str(device_id), ask_id=str(ask_id))
        return str(row[0]) if row is not None else ""

    async def may_ask(
        self, device_id: uuid.UUID, notification_id: uuid.UUID, speak: str, priority: str
    ) -> uuid.UUID | None:
        """The rails for `talk.ask_to_open` (no talk open: the caller's): a reason Pisa may speak
        first for; never a low-priority item; outside quiet hours unless urgent or leave-now; natural
        mode allowed now and inside the budget; their AI consent covering the talk (else the talk it asks for
        would be refused, `ai_consent_required`); the user wearing AirPods or the glasses, or
        the last talk on AirPods, the glasses or the car within ROUTE_RECENT; at most ASKS_PER_HOUR an hour;
        never twice for one item. The ask's id (stored as asked), or None."""
        if speak not in form.SPEAKS or priority == "low":
            return None
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            if (
                is_quiet(now, await self._owner.settings(conn), self._owner.tz)
                and priority != "urgent"
                and speak != "leave_now"
            ):
                return None
            try:
                await self._ready(conn)
            except PisaError:
                return None
            if await disclosure.check(conn, device_id, None, None, self._ollama, health=True) is not None:
                return None  # the talk it asks for would be refused (`ai_consent_required`): never asked
            worn = await (
                await conn.execute(
                    "SELECT coalesce(earbuds = 'connected', false) OR coalesce(glasses = 'on', false) "
                    "FROM device_presence WHERE device_id = %s",
                    (device_id,),
                )
            ).fetchone()
            last = await (
                await conn.execute(
                    "SELECT route FROM talk_sessions WHERE device_id = %s "
                    "AND coalesce(ended_at, started_at) > %s ORDER BY started_at DESC LIMIT 1",
                    (device_id, now - ROUTE_RECENT),
                )
            ).fetchone()
            if not (worn and worn[0]) and not (last and last[0] in form.SPEAK_FIRST_ROUTES):
                return None
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"talk_ask:{device_id}",))
            recent = await (
                await conn.execute(
                    "SELECT count(*) FROM talk_asks WHERE device_id = %s AND asked_at > %s",
                    (device_id, now - timedelta(hours=1)),
                )
            ).fetchone()
            if recent and recent[0] >= form.ASKS_PER_HOUR:
                _log.info("talk_ask_rate", device_id=str(device_id))
                return None
            row = await (
                await conn.execute(
                    "INSERT INTO talk_asks (id, device_id, notification_id, why, asked_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (notification_id) DO NOTHING RETURNING id",
                    (uuid.uuid4(), device_id, notification_id, speak, now),
                )
            ).fetchone()
        if row is None:
            return None
        _log.info("talk_ask", device_id=str(device_id), why=speak)
        return uuid.UUID(str(row[0]))

    async def _routed(self, session_id: uuid.UUID, route: str) -> None:
        """The talk's route now (`may_ask` reads the last talk's)."""
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE talk_sessions SET route = %s WHERE id = %s", (route, session_id))

    async def _asked_said(self, ask_id: uuid.UUID | None) -> None:
        """Measured: the ask's line was said in the talk opened for it."""
        if ask_id is None:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE talk_asks SET said_at = %s WHERE id = %s AND said_at IS NULL", (self._clock(), ask_id)
            )

    def run_for(self, device_id: uuid.UUID) -> "TalkRun | None":
        """The device's open talk, when it has one (its screen, its route; Pisa's own line goes in)."""
        session_id = self._by_device.get(device_id)
        return self.runs.get(session_id) if session_id is not None else None

    def end(self, session_id: uuid.UUID, device_id: uuid.UUID, reason: str | None = None) -> bool:
        """The phone ends it (the DELETE); `reason` is talk.end's (press, call, route, …), kept in the
        timeline. Idempotent for the device's own talk: one that already ended (its peer closed first, a newer
        talk replaced it, the DELETE came twice) is ended all the same (True), and the user's end means its
        last words are not answered after it. False: no such talk of this device's."""
        run = self.runs.get(session_id)
        if run is not None and self._device_of.get(session_id) == device_id:
            self._owner_ends.add(session_id)
            run.end("owner", "phone", reason if reason in PHONE_REASONS else "")
            return True
        ended = self._ended.get(session_id)
        if ended is None or ended[0] != device_id or time.monotonic() - ended[1] > ENDED_FOR_S:
            return False
        self._owner_ends.add(session_id)
        _log.info("talk_end_after_end", session_id=str(session_id))
        return True

    def devices(self) -> set[uuid.UUID]:
        """The devices with a talk or an enrolment open now."""
        return set(self._device_of.values()) | {device for device, _ in self._enrolling.values()}

    async def revoked(self, device_id: uuid.UUID) -> int:
        """The device was revoked (security review TK-1): its open talk ends at once, as the owner's end
        (its last words are not answered after it), its talker session and its peer closed; an enrolment it
        had open stops. Ids only in the log. The number of sessions ended."""
        self._revoked_devices.add(device_id)  # a talk still opening ends once it is registered (`_open`)
        ended, runs = self._end_open(device_id, TalkRun.revoke, "talk_revoked")
        for session_id, (device, stop) in list(self._enrolling.items()):
            if device != device_id:
                continue
            ended += 1
            _log.info("voice_enrolment_revoked", session_id=str(session_id), device_id=str(device_id))
            try:
                await stop()
            except Exception:
                _log.warning("talk_close_failed", session_id=str(session_id))
        await self._closed(runs)
        return ended

    async def conversation_deleted(self, device_id: uuid.UUID) -> int:
        """The device deleted its conversation (`DELETE /v1/conversations/{threadId}`, security review CD-1):
        its open talk ends at once, as a revoked device's does (no last final awaited, its last words neither
        kept nor answered), and is closed and stored before the delete removes its rows. The device may open
        a new talk right after. Ids only in the log. The number of talks ended."""
        ended, runs = self._end_open(
            device_id, lambda run: run.revoke("conversation_deleted"), "talk_conversation_deleted"
        )
        await self._closed(runs)
        return ended

    def _end_open(
        self, device_id: uuid.UUID, end: Callable[["TalkRun"], None], event: str
    ) -> tuple[int, list[asyncio.Task[None]]]:
        """The device's open talks ended with `end`, as the owner's end: how many, and their run tasks."""
        ended = 0
        runs: list[asyncio.Task[None]] = []
        for session_id, device in list(self._device_of.items()):
            run = self.runs.get(session_id)
            if device != device_id or run is None:
                continue
            self._owner_ends.add(session_id)
            end(run)
            ended += 1
            if (task := self._run_tasks.get(session_id)) is not None:
                runs.append(task)
            _log.info(event, session_id=str(session_id), device_id=str(device_id))
        return ended, runs

    @staticmethod
    async def _closed(runs: list[asyncio.Task[None]]) -> None:
        """Closed (the talker and the peer) before the caller goes on; a stuck close is not waited for."""
        if runs:
            await asyncio.wait(runs, timeout=REVOKE_WAIT_S)

    async def _meter(self, opened: Opened, minute: Decimal) -> bool:
        """Another minute reserved under the caps; False when a cap refuses it (the session ends: budget)."""
        try:
            async with self._pool.connection() as conn:
                await ledger.grow(
                    conn,
                    opened.call_id,
                    extra_eur=minute,
                    slot=SLOT,
                    now=self._clock(),
                    tz=self._owner.tz,
                    hard=self._hard,
                )
        except PisaError as err:
            if err.code == "budget_exceeded":
                return False
            raise
        return True

    async def _store_exchange(self, session_id: uuid.UUID, e: Exchange) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO talk_turns (session_id, n, owner_end_at, first_sound_ms, whisper_ms, "
                "whisper_kind, asked_brain, first_content_ms, first_content_by, reflex_ms, reflex_by, route, "
                "parts, failed, unbacked, redone, ready) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    session_id,
                    e.n,
                    e.owner_end_at,
                    e.first_sound_ms,
                    e.whisper_ms,
                    e.whisper_kind,
                    e.asked_brain,
                    e.first_content_ms,
                    e.first_content_by,
                    e.reflex_ms,
                    e.reflex_by,
                    e.route,
                    Jsonb(e.parts) if e.parts is not None else None,
                    min(e.failed, 32767),
                    min(e.unbacked, 32767),
                    e.redone,
                    e.ready,
                ),
            )

    async def _store_redone(self, session_id: uuid.UUID, n: int) -> None:
        """Their next final corrected or repeated exchange n (stored when the next one began)."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE talk_turns SET redone = true WHERE session_id = %s AND n = %s", (session_id, n)
            )

    # --- what the talker knows -----------------------------------------------------------------------------

    async def _fresh_now(self, device_id: uuid.UUID, lang: str) -> Fresh:
        async with self._pool.connection() as conn:
            return await self.fresh(conn, device_id, lang)

    async def fresh(self, conn: AsyncConnection, device_id: uuid.UUID, lang: str) -> Fresh:
        """The time, where the user is, the plan's next item, today's weather, Pisa's open questions: Pisa's
        own data, read with no paid call."""
        tz = self._owner.zone
        now = self._clock().astimezone(tz)
        place = await (
            await conn.execute(
                "SELECT p.name, p.category FROM stays s JOIN places p ON p.id = s.place_id "
                "WHERE s.device_id = %s AND s.departed_at IS NULL AND s.last_seen_at > %s "
                "ORDER BY s.arrived_at DESC LIMIT 1",
                (device_id, now - timedelta(minutes=30)),
            )
        ).fetchone()
        if place is not None and sensitive_place(place[0], place[1]):
            place = None  # Pisa never says it on its own (their question gets it from `recall`)
        plan = await stored_current(conn, now.date())  # never a "next" from a plan of tasks gone since
        next_item = ""
        for item in (plan or {}).get("items", []):
            start = item.get("start")
            if item.get("kind") not in ("event", "task"):
                continue  # a free slot or a trip is no "next" for the user
            if item.get("status") in NOT_NEXT:
                continue  # skipped or already done is no "next"
            if start and datetime.fromisoformat(start) > now:
                when = datetime.fromisoformat(start).astimezone(tz)
                next_item = f"{item.get('title', '')} {_at(lang)} {when:%H:%M}"
                break
        cached = await (
            await conn.execute(
                "SELECT forecast FROM weather_cache WHERE fetched_at > %s ORDER BY fetched_at DESC LIMIT 1",
                (now - timedelta(hours=6),),
            )
        ).fetchone()
        weather = ""
        if cached:
            for day in cached[0].get("days", []):
                if day.get("date") == now.date().isoformat():
                    weather = weather_rules.briefing_line(
                        weather_rules.Day(**(day | {"date": now.date()})), lang
                    )
        asked = await (
            await conn.execute(
                "SELECT summary FROM approvals WHERE status = 'pending' AND expires_at > %s "
                "AND device_id = %s ORDER BY created_at DESC LIMIT 3",
                (now, device_id),
            )
        ).fetchall()
        return Fresh(
            now=f"{now:%A %d %B %Y, %H:%M}", place=place[0] if place else "", next_item=next_item.strip(),
            weather=weather, approvals=tuple(a for (a,) in asked), plan=plan is not None,
        )  # fmt: skip

    async def _seed(self, conn: AsyncConnection, device_id: uuid.UUID) -> str:
        """Today's conversation with this device, as Pisa's summary of it (continuity is Pisa's, not the
        provider's)."""
        day, _ = period_starts(self._clock(), self._owner.tz)
        rows = await (
            await conn.execute(
                "SELECT m.role, m.content, m.meant, m.corrections FROM messages m JOIN conversations c "
                "ON c.id = m.conversation_id WHERE c.device_id = %s AND m.created_at >= %s ORDER BY m.id",
                (device_id, day),
            )
        ).fetchall()
        # the model reads the marked form, built from their words and Pisa's `meant`
        return history.digest(
            [(role, understand.as_read(content, meant, fixes)) for role, content, meant, fixes in rows]
        )

    # --- what Pisa's brain said, as a whisper's kind
    # ----------------------------------------------------------

    async def kind_of(self, device_id: uuid.UUID, trace_id: str, since: datetime) -> Kind:
        """RAIL: the kind is code's, never the reply's words. `done` only when code finished an action
        in this turn: an act Pisa's decision log stored with the turn's trace id, or an approval this turn's
        spoken yes decided and ran (11: never one approved by a tap or elsewhere).
        `ask` only when this turn asked the user something code waits on (MED 8: an approval made for this
        device in this turn, or a question Pisa's decision log stored with this trace): said verbatim, never
        because the reply ends on "?". Anything else `say`."""
        async with self._pool.connection() as conn:
            moves = {
                move
                for (move,) in await (
                    await conn.execute(
                        "SELECT move FROM pisa_decisions WHERE trace_id = %s AND ((move = 'act' "
                        "AND (outcome IS NULL OR outcome = 'done')) OR move = 'ask')",
                        (trace_id,),
                    )
                ).fetchall()
            }
            approvals = {
                status
                for (status,) in await (
                    await conn.execute(
                        "SELECT status FROM approvals WHERE device_id = %s AND ((status = 'done' "
                        "AND decided_auth IN ('voice', 'words') AND decided_at >= %s) "
                        "OR (status = 'pending' AND created_at >= %s))",
                        (device_id, since, since),
                    )
                ).fetchall()
            }
        if "act" in moves or "done" in approvals:
            return Kind.DONE
        if "ask" in moves or "pending" in approvals:
            return Kind.ASK
        return Kind.SAY

    async def close_all(self) -> None:
        for run in list(self.runs.values()):
            run.end("error", "server", "Pisa's server stopped")
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _at(lang: str) -> str:
    return "alle" if lang == "it-IT" else "at"
