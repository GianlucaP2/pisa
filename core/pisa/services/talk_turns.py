"""A talk's turns: what Pisa's brain does with the user's final words in a talk (`TalkService.open`'s
`think`), and the taps a talk's screen sends back (a choice, an element of a drawing, an approval).

The brain is today's conversation service, its tools and rails, with this device's screen: what a turn shows
beside its words (its senders: what Pisa understood, the cards, a phone action, the presentation objects) goes
on the talk's channel, in the exchange's run (the talk's turn id), never streamed on the phone's live session
too. Its reply comes back as text for one whisper, shortened for the talk's route (`spoken`).
"""

import uuid
from dataclasses import dataclass, replace
from typing import Any

from pisa.brain.research_turn import WORKING
from pisa.domain import approvals as approval_rules
from pisa.domain import talk as form
from pisa.domain import wrist
from pisa.domain.errors import PisaError
from pisa.domain.whisper import Kind
from pisa.logging import get_logger
from pisa.services.runtime import Runtime
from pisa.services.session import DeviceSession
from pisa.services.session_registry import SessionRegistry
from pisa.services.talk import Opening, TalkRun, Thought, spoken_reply

_log = get_logger("pisa.talk_turns")


@dataclass
class TalkTurns:
    """One talk's thinking, for `device_id` in `lang`; `sessions` holds the device's live session, read at
    each turn (the phone may reconnect during a talk). `watch`: the talk opened from the watch."""

    runtime: Runtime
    sessions: SessionRegistry
    device_id: uuid.UUID
    lang: str
    watch: bool = False

    def live(self) -> DeviceSession | None:
        return self.sessions.session(self.device_id)

    def conversation_thread(self) -> str:
        """The thread a contract 2 talk's runs go on: the app's conversation thread on `/v2/live` (the one its
        latest run named), else the server's default for this device (as `/v2/live` starts with)."""
        thread = getattr(self.live(), "thread", None)
        return thread if isinstance(thread, str) and thread else f"conversation:{self.device_id}"

    def opening(self, route: Any, line: str, ask_id: uuid.UUID | None) -> Opening:
        """How the talk opens: its route, the line it opens with (an ask to open), and how it answers the
        taps on its screen; what it shows is what the phone's live session listed (all, with none)."""
        live = self.live()
        kinds = live.screen.kinds if live is not None else frozenset()
        blocks = getattr(live.screen, "blocks", None) if live is not None else None
        return Opening(route=route, line=line, ask_id=ask_id if line else None, chosen=self.chosen,
                       asked=self.asked, decide=self.decide, blocks=blocks, in_card=self.in_card,
                       spoken=self.spoken, **({"kinds": kinds} if kinds else {}))  # fmt: skip

    def in_card(self, turn_id: str | None, text: str, lang: str) -> bool:
        """The answer to the last words of a talk whose peer was lost, into the talk's own turn (`turn_id`,
        the talk card's) on the app's open live session: True when it went (the app is open), else the
        caller sends it as a notice."""
        live = self.live()
        if live is None or live.closed or not live.live.attached:
            return False
        return live.talk_answer(self.conversation_thread(), turn_id, text, lang)

    async def spoken(self, trace_id: str, lines: list[str]) -> None:
        """The talk's turn `trace_id` went in silently: its stored reply is what Pisa's voice said."""
        await self.runtime.conversations.spoken(self.device_id, trace_id, " ".join(x.strip() for x in lines))

    async def think(self, words: str, trace_id: str, yes_by_code: bool, origin: str = "words") -> Thought:
        """Pisa's brain on the user's final words; its reply (never its progress lines: LOW) comes back as
        text for one whisper. `yes_by_code`: a spoken yes may decide Pisa's question only right after Pisa
        said it aloud (MED 10)."""
        runtime, device_id = self.runtime, self.device_id
        run = runtime.talk.run_for(device_id)
        senders = turn_senders(self.live(), run)

        async def emit(delta: str, final: bool) -> None:
            # the reply is the return value; the talker speaks it. Research's "Cerco, un
            # attimo" before a slow tool (web search, read page, maps) is said at once, through a whisper
            if not final and run is not None and delta.strip() in WORKING.values():
                await run.working(delta.strip())

        since = runtime.clock()
        text = await runtime.conversations.respond(
            device_id,
            words,
            self.lang,
            emit,
            trace_id=trace_id,
            yes_by_code=yes_by_code,
            voice=True,
            origin=origin,  # a tap in a drawing, by its flag (never its words)
            **senders,
        )
        kind = await runtime.talk.kind_of(device_id, trace_id, since)
        asks = runtime.conversations.asks_owner(device_id)
        claimed = runtime.conversations.claimed(device_id)
        thought = thought_of(
            spoken_reply(text),
            kind,
            run,
            self.lang,
            asks=asks,
            claimed=claimed,
            words=words,
            watch=self.watch,
        )
        # the turn's record for the clean-turn rate (what wasn't done, claims rewritten)
        failed, unbacked = runtime.conversations.record_of(device_id, trace_id)
        # where the brain's time went, into the talk's timeline and measures
        return replace(
            thought,
            parts=runtime.conversations.parts_of(device_id, trace_id),
            failed=failed,
            unbacked=unbacked,
            # said after the reply as its own line, stored as its own message (never spliced into it)
            correction=runtime.conversations.correction_of(device_id, trace_id),
        )

    async def chosen(self, object_id: str, option: str) -> str | None:
        """A tap on a shown choice's option is the user's own next words (the "[tap] o2")."""
        said = await self.runtime.conversations.object_event(self.device_id, object_id, "chosen", option)
        return said[0] if said is not None else None

    async def asked(self, object_id: str, element_id: str) -> str | None:
        """A tap on a drawing's element Pisa showed: "Sul disegno: …" around its label."""
        said = self.runtime.conversations.tap(self.device_id, object_id, element_id)
        return said[0] if said is not None else None

    async def decide(self, approval_id: str, yes: bool) -> tuple[str, bool]:
        return await tap_decision(self.runtime, self.device_id, approval_id, yes, self.lang)


def turn_senders(live: Any, run: TalkRun | None) -> dict[str, Any]:
    """What the talk's thinker shows beside its words: the live session's senders, writing on the open talk's
    channel (what Pisa understood, the cards, a phone action: in the exchange's run, the talk's turn id), with
    the talk's screen as the device. The talk's turn is never streamed on the live session too: the app shows
    it once, in the talk's card. (No talk: the live session's, as the economy path.)"""
    if live is None:
        senders: dict[str, Any] = {}
    elif run is not None and run.screen is not None:
        senders = live.turn_senders(None, live.in_talk(run.screen))
    else:
        senders = live.turn_senders(None)
    if run is not None and run.screen is not None:
        senders["device"] = run.screen
        run.screen.turn()
    return senders


def thought_of(
    reply: str,
    kind: Kind,
    run: TalkRun | None,
    lang: str,
    *,
    asks: bool,
    claimed: bool,
    words: str | None = None,
    watch: bool = False,
) -> Thought:
    """The brain's reply as the talk's thought. A question to the user (an approval, a
    draft's read-back and its "Lo mando?") is said whole, never shortened (a yes answers only what Pisa
    said aloud); `asks`: the turn asked the user something or read a draft back."""
    said = spoken(reply, run, lang, whole=kind is Kind.ASK or asks, words=words, watch=watch)
    return Thought(said, kind, claimed_done=claimed, shortened=said != reply)


def spoken(
    said: str,
    run: TalkRun | None,
    lang: str,
    *,
    whole: bool = False,
    words: str | None = None,
    watch: bool = False,
) -> str:
    """What Pisa's voice says of the brain's reply in a talk: one line when the turn showed something,
    on AirPods, the glasses or in the car one sentence, the rest on the screen as a note. `whole`:
    Pisa's question and a read-back are never shortened.
    RAIL: on those short routes and on the watch, a turn whose words or answer name their health or a
    sensitive place (`wrist.touches_sensitive`) is never said aloud, a question included: Pisa says only
    that it is on the screen, and the answer is there (a spoken yes then decides nothing: it was not said)."""
    screen = run.screen if run is not None else None
    short = watch or (run is not None and run.route in form.SHORT_ROUTES)
    if short and wrist.touches_sensitive(words, said):
        if screen is not None:
            screen.note("Pisa", [said])
        return form.SHOWN.get(lang, form.SHOWN["en-US"])
    if whole:
        return said
    if screen is not None and screen.in_turn:
        return form.shown_line(said, lang)
    if run is not None and run.route in form.SHORT_ROUTES and (short := form.short_line(said)) != said:
        if screen is not None:
            screen.note("Pisa", [said])
        return short
    return said


async def tap_decision(
    runtime: Runtime, device_id: uuid.UUID, approval_id: str, yes: bool, lang: str
) -> tuple[str, bool]:
    """A tap on a shown approval decides it by code (auth tap): only this device's, still waiting,
    never by anything Pisa read. A Face ID one stays on the phone. (Pisa's line, whether it was done); ("",
    False) when it is no longer open."""
    try:
        pending = await runtime.approvals.pending_for(device_id, [approval_id])
    except ValueError:
        return "", False
    if not pending:
        return "", False
    lang = str(pending[0]["lang"] or lang)
    try:
        view = await runtime.approvals.decide(approval_id, "approve" if yes else "reject", "tap", talk=True)
    except PisaError as err:
        if err.code == "biometric_required" and err.action == "Unlock the phone":  # locked
            return err.message, False
        if err.code == "biometric_required":
            return form.TAP_FACE_ID.get(lang, form.TAP_FACE_ID["en-US"]), False
        _log.info("tap_decision_refused", cause=type(err).__name__, code=err.code)  # no line: it isn't open
        return "", False
    line = approval_rules.outcome_sentence(view["status"], view["result"], lang) or ""
    return line, view["status"] == "done"
