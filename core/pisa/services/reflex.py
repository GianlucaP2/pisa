"""Pisa's reflex on the user's partial words, in natural and economy, and the shared state it
writes (`pisa.domain.state`).

On each partial (a few a second, at most one pass at a time per device; the latest words win):
1. **Read the words**: the reflex model (`conversation.reflex`, a small fast model, metered in the ledger)
   within REFLEX_BUDGET_S, else the rules (`pisa.domain.reflex`, free). Intent, the goal, which reads, the
   day.
2. **Speculate under the rails** (`pisa.domain.speculation`): Pisa's own reads (the day's agenda, open tasks,
   today's plan, what waits for a yes) at once; an outside read (a travel time, a provider's paid call) only
   once the words are a stable prefix; never a write.
3. **The ready answer**: for a question, once the words are stable and the reads are in, the reflex drafts the
   obvious answer from Pisa's data only; code keeps it only when every time and number in it is in that data.

The final words then find the state ready: the main thinker reads it as a head start (no second read of the
same data, the draft answer to confirm), and in natural mode the talker gets Pisa's data and the ready answer
as whispers (`pisa.services.talk`). Logs carry ids, lengths and times only, never the words.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from pisa.brain import reflex as brain
from pisa.brain.conversation import agenda_read
from pisa.brain.models import rests
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.domain import speculation
from pisa.domain.errors import PisaError
from pisa.domain.latency import REFLEX_WINDOW_MS
from pisa.domain.owner import Owner
from pisa.domain.reflex import RulesRead, rules_read
from pisa.domain.state import OUTSIDE_MARK, STATE_FOR, ConversationState, Fact, Ready, grounded
from pisa.logging import get_logger
from pisa.ports.assistant import AssistantData, PlaceData

# a model read slower than this: the rules read the words; the talk report's window
REFLEX_BUDGET_S = REFLEX_WINDOW_MS / 1000
TARGET_MS = 300
MIN_WORDS = 3  # fewer words than this say nothing yet
GROW_WORDS = 2  # a new pass once the words grew this much (or became a stable prefix)
SETTLE_WAIT_S = 0.4  # the final words wait at most this long for a reflex pass under way on them
TASKS_LISTED = 6
REST_AFTER_FAILURE_S = 300.0  # a refused or retired model: the rules alone for 5 minutes (no call per pass)
SLOW_IN_A_ROW = 3  # this many reads over the budget in a row: the rules alone for REST_AFTER_SLOW_S
REST_AFTER_SLOW_S = 60.0
# natural mode gets no partial words (Gemini transcribes at the end of the user's turn), so the
# reflex reads the final words once: Pisa's own reads at once, the draft within FINAL_DRAFT_S
FINAL_DRAFT_S = 2.0
FINAL_READS = ("agenda", "plan", "tasks")  # a question that names none of Pisa's reads: the day's own data
WEATHER_CHARS = 700
_log = get_logger("pisa.reflex")


@dataclass(frozen=True)
class Reflexed:
    """One reflex pass, for the talk's timeline and Pisa's voice (no words but the goal and Pisa's data)."""

    words: str
    by: str  # model (inside the window), slow (the model past it: the rules read), rules, or final
    ms: int  # the read's time
    intent: str
    reads: tuple[str, ...]  # run now
    held: tuple[str, ...]  # outside reads waiting for a stable prefix (RAIL)
    stable: bool
    ready: str | None


OnRead = Callable[[Reflexed], Awaitable[None]]


class Waiting(Protocol):
    """What the reflex needs from Pisa's ledger (ActionsService)."""

    async def waiting(self, device_id: uuid.UUID) -> list[dict[str, Any]]: ...


class PisaReads:
    """Pisa's reads the reflex may run on partial words, each giving the same words the tool would."""

    def __init__(
        self,
        data: AssistantData | None,
        actions: Waiting | None,
        places: PlaceData | None,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
    ) -> None:
        self._data, self._actions, self._places = data, actions, places
        self._owner = owner
        self._clock = clock

    def day(self, value: str | None) -> date:
        today = self._owner.day(self._clock())
        if value in (None, "", "today"):
            return today
        if value == "tomorrow":
            return today + timedelta(days=1)
        if value == "after":
            return today + timedelta(days=2)
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return today

    async def run(self, read: str, *, device: uuid.UUID, day: date, place: str | None) -> str | None:
        # a read holding a stranger's title is marked outside
        if read == "agenda" and self._data is not None:
            text, foreign = await agenda_read(self._data, day, self._owner.zone)
            return OUTSIDE_MARK + text if foreign else text
        if read == "tasks" and self._data is not None:
            tasks = [t for t in await self._data.tasks("open")][:TASKS_LISTED]
            listed = "; ".join(
                t.title
                + (f" (due {t.due_at.astimezone(self._owner.zone):%a %d %b %H:%M})" if t.due_at else "")
                for t in tasks
            )
            mark = OUTSIDE_MARK if any(t.source_kind == "email" for t in tasks) else ""
            return mark + f"Open tasks: {listed}." if listed else "No open tasks."
        if read == "plan" and self._data is not None:
            seen = getattr(self._data, "today_seen", None)
            text = await seen() if seen is not None else None
            foreign_in = getattr(self._data, "foreign_in", None)
            if text and foreign_in is not None and await foreign_in(text):
                return OUTSIDE_MARK + text
            return text
        if read == "waiting" and self._actions is not None:
            rows = await self._actions.waiting(device)
            listed = "; ".join(f"«{r['summary']}»" for r in rows if not r.get("expired"))
            return (
                f"Waiting for the user's yes, NOT done: {listed}." if listed else "Nothing waits for a yes."
            )
        if read == "weather" and self._places is not None:
            # the forecast where the user is, for the day named (an outside read: final words)
            view = await self._places.weather(place, day.isoformat())
            return f"Weather: {json.dumps(view, ensure_ascii=False, default=str)[:WEATHER_CHARS]}"
        if read == "travel" and self._places is not None and place:
            result = await self._places.travel(place, None, None, None, None)
            return OUTSIDE_MARK + (  # a provider's place names
                f"To {result['name']}: {result['minutes']} min ({result['mode']}, from {result['provider']})."
            )
        return None


@dataclass
class _Track:
    words: str = ""
    prefix_since: float = 0.0  # when the words last stopped being the same prefix (monotonic)
    passed: str = ""  # the words of the latest pass
    task: asyncio.Task[None] | None = None
    pending: tuple[str, str, OnRead | None] | None = None
    done: set[tuple[str, str, str]] = field(default_factory=set)  # (read, day, place) run for these words
    stable_passed: bool = False  # a pass ran on these words once they were a stable prefix


class ReflexService:
    def __init__(
        self,
        gateway: ModelGateway | None,
        reads: PisaReads,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        budget_s: float = REFLEX_BUDGET_S,
    ) -> None:
        self._gateway = gateway
        self._reads = reads
        self._owner = owner
        self._clock = clock
        self._budget_s = budget_s
        self._rest_until = 0.0  # monotonic: before this, the model isn't called (the rules read the words)
        self._model: str | None = None  # the slot's model, as the last call used it (for the rest's warning)
        self._slow = 0
        self.states: dict[uuid.UUID, ConversationState] = {}
        self._tracks: dict[uuid.UUID, _Track] = {}

    def state(self, device_id: uuid.UUID) -> ConversationState:
        """The device's shared state; a new one after STATE_FOR unused (hours-old voice lines never reach
        the next brain turn)."""
        now = self._clock()
        state = self.states.get(device_id)
        if state is None or (state.touched is not None and now - state.touched > STATE_FOR):
            state = self.states[device_id] = ConversationState()
        state.touched = now
        return state

    # --- the user's words ------------------------------------------------------------------------------

    def heard(self, device_id: uuid.UUID, words: str, lang: str, *, on_read: OnRead | None = None) -> None:
        """Partial words: a pass when they grew enough or became a stable prefix (one at a time; the latest
        words wait and win)."""
        if len(speculation.words(words)) < MIN_WORDS:
            return
        track = self._tracks.setdefault(device_id, _Track())
        now = time.monotonic()
        if not speculation.same_prefix(track.words, words):
            track.prefix_since = now
            if not speculation.follows(track.passed, words):
                track.done = set()  # other words: what was read for the old ones counts no more
        other = not track.passed or not speculation.follows(track.passed, words)
        if other:
            track.stable_passed = False
        track.words = words
        grown = len(speculation.words(words)) - len(speculation.words(track.passed)) >= GROW_WORDS
        newly_stable = self._stable(track, now) and not track.stable_passed
        if not (other or grown or newly_stable):
            return
        if track.task is not None and not track.task.done():
            track.pending = (words, lang, on_read)
            return
        track.task = asyncio.create_task(self._loop(device_id, track, words, lang, on_read))

    async def settle(self, device_id: uuid.UUID, words: str) -> None:
        """The final words: wait (briefly) for a pass under way on them: the main thinker finds its reads."""
        track = self._tracks.get(device_id)
        if track is None or track.task is None or track.task.done():
            return
        if not speculation.follows(track.words, words):
            return
        try:
            await asyncio.wait_for(asyncio.shield(track.task), SETTLE_WAIT_S)
        except TimeoutError:  # still running: the main thinker goes on without it
            return
        except Exception as exc:  # the pass logs its own failures; one escaping it is a bug, said here
            _log.warning("reflex_settle_failed", device_id=str(device_id), cause=exc.__class__.__name__)
            return

    async def final(
        self, device_id: uuid.UUID, words: str, lang: str, *, on_read: OnRead | None = None
    ) -> str | None:
        """The user's final words, when no pass ran on them (natural mode: Gemini gives no
        partial words). Only a question (a change is the brain's to act on): the rules read it (free, at
        once); Pisa's own reads run (the forecast too: these are final words); the obvious answer is drafted
        from that data in one small call and kept only when `grounded` (RAIL). The pass is the track's
        task, so the main thinker's `settle` finds its reads. Returns the ready answer, or None."""
        read = rules_read(words)
        if read.intent != "question":
            return None  # a change, chat or too few words: the brain answers (no model call here)
        track = self._tracks.setdefault(device_id, _Track())
        if track.task is not None and not track.task.done() and speculation.follows(track.words, words):
            return None  # a pass on these words is under way: its ready answer is the one
        if track.stable_passed and speculation.follows(track.passed, words):
            return None  # the partial words' stable pass read these words already and found no answer
        out: list[str | None] = [None]
        track.words, track.passed, track.pending, track.stable_passed = words, words, None, True
        track.task = asyncio.create_task(self._final(device_id, words, lang, read, on_read, out))
        try:
            await asyncio.shield(track.task)
        except Exception as err:  # a pass never breaks the conversation
            _log.warning("reflex_failed", device_id=str(device_id), error=type(err).__name__)
        return out[0]

    async def _final(
        self, device_id: uuid.UUID, words: str, lang: str, read: RulesRead, on_read: OnRead | None,
        out: list[str | None],
    ) -> None:  # fmt: skip
        state = self.state(device_id)
        started = time.monotonic()
        day = self._reads.day(read.day)
        wanted = list(read.reads) or list(FINAL_READS)
        allowed = [r for r in wanted if speculation.may_read(r, final=True)]

        async def one(r: str) -> tuple[str, str | None]:
            try:
                return r, await self._reads.run(r, device=device_id, day=day, place=read.place)
            except Exception as err:  # one read failing never costs the others
                _log.info("reflex_read_failed", read=r, error=type(err).__name__)
                return r, None

        ran: list[str] = []
        for r, text in await asyncio.gather(*(one(r) for r in allowed)):
            if text:
                state.add_fact(Fact(r, text.removeprefix(OUTSIDE_MARK), self._clock(), words,
                                    outside=text.startswith(OUTSIDE_MARK)))  # fmt: skip
                ran.append(r)
        data = [f.text for f in state.fresh_facts(self._clock()) if f.words == words]
        ready: str | None = None
        if data and self._gateway is not None and time.monotonic() >= self._rest_until:
            drafted = await self._answer(words, data, lang, budget_s=FINAL_DRAFT_S)
            if drafted and grounded(drafted, data):  # RAIL: no time or number Pisa's data doesn't hold
                ready = drafted
        out[0] = ready
        ms = int((time.monotonic() - started) * 1000)
        _log.info("reflex", device_id=str(device_id), by="final", ms=ms, intent=read.intent, reads=ran,
                  held=[], stable=True, ready=ready is not None, chars=len(words))  # fmt: skip
        if on_read is not None:
            await on_read(Reflexed(words, "final", ms, read.intent, tuple(ran), (), True, ready))

    def _stable(self, track: _Track, now: float) -> bool:
        return (
            len(speculation.words(track.words)) >= speculation.STABLE_KEEP
            and now - track.prefix_since >= speculation.STABLE_S
        )

    async def _loop(
        self, device_id: uuid.UUID, track: _Track, words: str, lang: str, on_read: OnRead | None
    ) -> None:
        while True:
            try:
                await self._pass(device_id, track, words, lang, on_read)
            except Exception as err:  # a pass never breaks the conversation
                _log.warning("reflex_failed", device_id=str(device_id), error=type(err).__name__)
            if track.pending is None:
                return
            (words, lang, on_read), track.pending = track.pending, None

    async def _pass(
        self, device_id: uuid.UUID, track: _Track, words: str, lang: str, on_read: OnRead | None
    ) -> None:
        state = self.state(device_id)
        state.set_ready(None)  # a new pass: the ready answer is for these words or none
        stable = self._stable(track, time.monotonic())
        started = time.monotonic()
        read, by = await self._read(words, lang)
        ms = int((time.monotonic() - started) * 1000)
        track.passed = words
        track.stable_passed = track.stable_passed or stable
        if read.goal:
            state.read_goal(read.goal, words)
        day = self._reads.day(read.day)
        ran: list[str] = []
        held: list[str] = []
        for r in read.reads:
            key = (r, day.isoformat(), read.place or "")
            if key in track.done:
                continue
            if not speculation.may_read(r):
                held.append(r)  # RAIL: an outside read waits for the final words
                continue
            text = await self._reads.run(r, device=device_id, day=day, place=read.place)
            track.done.add(key)
            if text:
                state.add_fact(Fact(r, text.removeprefix(OUTSIDE_MARK), self._clock(), words,
                                    outside=text.startswith(OUTSIDE_MARK)))  # fmt: skip
                ran.append(r)
        ready: str | None = None
        now = self._clock()
        data = [f.text for f in state.fresh_facts(now) if speculation.follows(f.words, words)]
        if read.intent == "question" and stable and data and by == "model":
            drafted = await self._answer(words, data, lang)
            if drafted and grounded(drafted, data):  # RAIL: no time or number Pisa's data doesn't hold
                ready = drafted
                state.set_ready(Ready(drafted, words, now))
        _log.info("reflex", device_id=str(device_id), by=by, ms=ms, intent=read.intent, reads=ran,
                  held=held, stable=stable, ready=ready is not None, chars=len(words))  # fmt: skip
        if on_read is not None:
            await on_read(Reflexed(words, by, ms, read.intent, tuple(ran), tuple(held), stable, ready))

    # --- the model, or the rules ----------------------------------------------------------------------------

    def _now_line(self) -> str:
        return f"{self._owner.local(self._clock()):%A %Y-%m-%d %H:%M}"

    def _rest(self, seconds: float, why: str, *, alert: bool = True) -> None:
        """The model left alone for `seconds` (the rules read the words); a failure's rest is counted and
        warned about once an hour (`rests`), never a slot with no model chosen."""
        self._rest_until = time.monotonic() + seconds
        self._slow = 0
        _log.info("reflex_model_resting", seconds=int(seconds), why=why)
        if alert:
            rests.rested(brain.SLOT, self._model, why, seconds)

    async def _read(self, words: str, lang: str) -> tuple[RulesRead, str]:
        if self._gateway is not None and time.monotonic() >= self._rest_until:
            gateway = self._gateway

            async def call(ctx: CallContext) -> brain.ReflexRead:
                self._model = str(ctx.ref)
                out, usage = await brain.read(ctx.model, words, lang, self._now_line())
                ctx.usage.update(usage)
                return out

            try:
                out = await asyncio.wait_for(
                    gateway.run(brain.SLOT, call, prompt_chars=brain.prompt_chars(words),
                                max_output_tokens=brain.READ_TOKENS, bill_reserve_on_cancel=True),
                    self._budget_s,
                )  # fmt: skip
                self._slow = 0
                return RulesRead(out.intent, out.goal, list(out.reads), out.day, out.place), "model"
            except TimeoutError:
                _log.info("reflex_slow", budget_ms=int(self._budget_s * 1000))
                self._slow += 1
                if self._slow >= SLOW_IN_A_ROW:
                    self._rest(REST_AFTER_SLOW_S, "slow")
                # a model read past the window, cut; the rules read the words
                return rules_read(words), "slow"
            except PisaError as err:
                if err.code != "model_not_configured":
                    _log.info("reflex_model_refused", code=err.code, detail=err.detail)
                # the cause's machine word when the gateway has one ("token_limit": the output limit spent)
                self._rest(
                    REST_AFTER_FAILURE_S, err.detail or err.code, alert=err.code != "model_not_configured"
                )
            except Exception as err:
                _log.warning("reflex_model_failed", error=type(err).__name__)
                self._rest(REST_AFTER_FAILURE_S, type(err).__name__)
        return rules_read(words), "rules"

    async def _answer(
        self, words: str, data: list[str], lang: str, *, budget_s: float | None = None
    ) -> str | None:
        assert self._gateway is not None

        async def call(ctx: CallContext) -> str | None:
            out, usage = await brain.answer(ctx.model, words, data, lang, self._now_line())
            ctx.usage.update(usage)
            return out

        try:
            return await asyncio.wait_for(
                self._gateway.run(brain.SLOT, call, prompt_chars=brain.prompt_chars(words, data),
                                  max_output_tokens=brain.ANSWER_TOKENS, bill_reserve_on_cancel=True),
                budget_s if budget_s is not None else self._budget_s * 2,
            )  # fmt: skip
        except Exception as err:
            _log.info("reflex_answer_failed", error=type(err).__name__)
            return None
