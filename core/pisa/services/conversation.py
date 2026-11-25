"""Conversation service: one utterance in, a reply streamed out sentence by sentence.

History lives in `conversations` / `messages`. The last 20 messages go to the model as context; after
30 minutes of silence a new conversation starts. The open check-in and the latest plan options go along as
data, so "la seconda" or "saltalo" work.

Each turn logs its latency (info, no content): the whole turn, the model's share, each tool's time, and the
time to the first `say` chunk.
"""

import asyncio
import json
import re
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterable, Awaitable, Callable, Collection, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection, DatabaseError
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    InstructionPart,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from pisa.brain import findings as findings_rules
from pisa.brain import history, owner_scope
from pisa.brain import skills as skills_rules
from pisa.brain import understand as understanding
from pisa.brain.action_tools import SendToPhone
from pisa.brain.app_tools import AppAction
from pisa.brain.conversation import (
    HISTORY_MESSAGES,
    MAX_OUTPUT_TOKENS,
    RESEARCH_GUIDE,
    SLOT,
    UNDATED_LISTED,
    SentenceSplitter,
    Turn,
    TurnContext,
    asked_instruction,
    build_agent,
    build_history,
    clock_line,
    context_instruction,
    doubt_instruction,
    families,
    handed,
    handed_instruction,
    instructions_block,
    language_instruction,
    profile_instruction,
    system_prompt,
)
from pisa.brain.drawing_tools import DrawingTurn, drawing_box_on
from pisa.brain.follow_tools import SendDirections
from pisa.brain.history import chars as history_chars
from pisa.brain.history import kept as history_kept
from pisa.brain.models.compat import cut_notice_once
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.brain.place_tools import PlaceCard
from pisa.brain.present_tools import OBJECTS_KEPT, Cards, Device, Focus, Presenter, Shown
from pisa.brain.profile_tools import ProfileTurn, talk_instruction
from pisa.brain.public_tools import OFFER_ONCE
from pisa.brain.research_turn import WORKING, ResearchTurn
from pisa.brain.skills import block as skills_block
from pisa.brain.spoken import (
    FINISH,
    NOT_READY,
    PRESENTATION,
    TERMINAL,
    Loop,
    Round,
    spoke_and_finished,
    went_on_to_tools,
)
from pisa.brain.untrusted import Taint, current, data_block, open_turn, watch_turn
from pisa.db.errors import UNREACHABLE
from pisa.db.settings import delete_setting, get_setting, set_setting
from pisa.degrade import degrades, fails_closed, unreachable
from pisa.domain import abilities as abilities_rules
from pisa.domain import actions as action_rules
from pisa.domain import autonomy as autonomy_rules
from pisa.domain import body as body_rules
from pisa.domain import claims as claim_rules
from pisa.domain import clean as clean_rules
from pisa.domain import connectors as connector_rules
from pisa.domain import (
    decisions,
    experimental,
    holding,
    restate,
    results,
    tongue,
    tool_choice,
    wrist,
    yescard,
)
from pisa.domain import drawing as drawing_rules
from pisa.domain import episodes as episode_rules
from pisa.domain import follow as follow_rules
from pisa.domain import handoffs as handoff_rules
from pisa.domain import locked as locked_rules
from pisa.domain import memoria as memoria_rules
from pisa.domain import research as research_rules
from pisa.domain import rituals as ritual_rules
from pisa.domain import sheet as sheet_rules
from pisa.domain import stop as stop_rules
from pisa.domain import talk as form
from pisa.domain import tools as tool_registry
from pisa.domain import understand as rules
from pisa.domain import visual as visual_rules
from pisa.domain import voice1 as voice_rules
from pisa.domain import voice3 as voice3_rules
from pisa.domain import voice4 as voice4_rules
from pisa.domain import voice6 as voice6_rules
from pisa.domain.app_map import AppMap
from pisa.domain.claims import LEAD_ONLY, NOT_SHOWN, ON_SCREEN, claim, split_screen_claim
from pisa.domain.connections import ACKED
from pisa.domain.errors import PisaError
from pisa.domain.holding import HOLD_AFTER_S, PROGRESS_AFTER_S
from pisa.domain.latency import TURN_BUDGET_S
from pisa.domain.learning import sensitive_place
from pisa.domain.lexicon.advice import NOT_ADVICE
from pisa.domain.owner import Owner
from pisa.domain.sheet import SheetData
from pisa.domain.speculation import follows
from pisa.domain.tasks import LOW, in_small_hours, is_short_yes
from pisa.infra import ledger
from pisa.infra.spend import period_starts
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services import outcomes, presence, retention
from pisa.services.conversation_deps import ConversationDeps
from pisa.services.phone_maps import PhoneMaps
from pisa.services.phone_maps import using as phone_maps_turn
from pisa.services.skills import SkillsService
from pisa.services.stop import stopped_now
from pisa.services.voice import Speaker
from pisa.services.voice6 import outside_kinds as voice6_outside

if TYPE_CHECKING:
    pass

NEW_CONVERSATION_AFTER = timedelta(minutes=30)
# one holding phrase per wait: another utterance this soon after one, with no reply between,
# gets none (the progress line still comes on long work)
HOLD_ONCE_PER_S = 60.0
# the user's words in this many earlier turns of the conversation still ground a change after an
# email was read (each turn on its own, never words joined across turns)
EARLIER_TURNS = 3
# RAIL: from 50% of the day's (or month's) AI cap, the cheaper mode.
CHEAP_AT = Decimal("0.5")
CHEAP_SAID_KEY = "budget_guard_said"  # the day Pisa last said so
CHEAP_LINE = {
    "it-IT": "Per oggi uso il modello più economico.",
    "en-US": "For today I'm using the cheaper model.",
}
THREADS_SHOWN = timedelta(hours=2)  # how far back the open threads name what Pisa showed
# the guard's state, a dynamic line while it holds: Pisa says it plainly when asked, never denies it.
GUARD_LINE = (
    "\nBudget guard: today you answer with the cheaper model, because Pisa's AI spend passed {share}% "
    "of the {period} budget. When the user asks why you sound different, which model you use or about the "
    "budget, say so plainly («Oggi uso il modello più economico: abbiamo superato il {share}% del budget "
    "{period_it}»); never deny it."
)
GUARD_PERIOD = {"day": ("day's", "del giorno"), "month": ("month's", "del mese")}


def guard_instruction(guard: tuple[str, str] | None) -> str:
    """The guard's state for the instructions: '' while Pisa uses its usual model."""
    if guard is None:
        return ""
    period, period_it = GUARD_PERIOD.get(guard[1], GUARD_PERIOD["day"])
    return GUARD_LINE.format(period=period, period_it=period_it, share=int(CHEAP_AT * 100))


@dataclass
class TurnState:
    """The turn being answered, for the tools that need it (delete_task's confirmation):
    the user's words, the conversation, and every tool called so far in this turn."""

    conversation_id: uuid.UUID
    text: str
    tools: list[str] = field(default_factory=list)
    # The device the user is talking from, for what its tools create (a voice task records it: the smoke
    # test's throwaway device can then remove everything it made).
    device: uuid.UUID | None = None
    # the user's words of the device's previous turn when it got no answer.
    # They count for this turn's writes (`words`), never for a yes or no to Pisa's question (`text`).
    unanswered: str = ""
    earlier: tuple[str, ...] = ()  # their words in the talk's earlier turns (never taken back)
    answered: bool = False  # a reply to these words was saved
    # the reply only asked the user something (it ends with "?", changed nothing, asked no yes): the
    # user's next words answer it, and these words count for that answer's writes ("metti il dentista
    # giovedì" → "A che ora?" → "alle 15:30"), as an unanswered turn's do.
    questioned: bool = False
    at: float = field(default_factory=time.monotonic)
    heard_at: datetime | None = None  # when their words arrived (a spoken yes is timed from it)
    spoken: bool = True  # False for typed words: the voice's clocks don't apply to their yes
    # What this turn changed: a turn the user went on with (`continues`) is answered
    # again with the joined words and must not do it twice: the tasks it added (title key → id), a line each.
    added: dict[str, str] = field(default_factory=dict)
    done: list[str] = field(default_factory=list)
    # the approvals this turn asked (ids). At the turn's end, only those its one question names:
    # the user's yes in their next turn answers exactly them, by code.
    approvals: list[str] = field(default_factory=list)
    # … and every one the device's previous turn asked: the user's "sì, solo il primo" (answer_proposal).
    asked_before: frozenset[str] = frozenset()
    asked_order: tuple[str, ...] = ()  # the same, in the order Pisa said them
    acted: list[str] = field(default_factory=list)  # … and the changes it made on the user's words (ids)
    proposed: list[str] = field(default_factory=list)  # every approval this turn asked, before the bundle
    profile_asked: bool = False  # … unless Pisa's last question was a profile one (newest wins)
    # The events Pisa's action tools touched in this turn, and those of the device's previous turn: named for
    # the user's "spostala" now.
    events: list[str] = field(default_factory=list)
    named_events: frozenset[str] = frozenset()
    # The options Pisa offered in this turn, and in the device's previous one: only those can be Pisa's own
    # proposal applied on vague words; options seen only in the day's context, next to invite titles, can't.
    offered: list[str] = field(default_factory=list)
    named_options: frozenset[str] = frozenset()
    # When Pisa finished asking, in the `Unasked` sequence: anything Pisa said unasked after
    # it is newer, and a "sì" answers only the newest thing Pisa asked.
    asked: int | None = None
    before: "TurnState | None" = None  # the device's previous turn (one link)
    # the turn's loop (its rounds, `finish`, the results' statuses).
    loop: Round = field(default_factory=Round)
    model_calls: int = 0  # the model requests this turn made (one for a turn that spoke and showed)
    # where the turn's time went (ms): before the model, each model round (to its first words,
    # with its tokens), each tool; for the talk's timeline and the talk report's "where the time goes"
    parts: dict[str, Any] | None = None
    tokens: tuple[int, int] = (0, 0)  # the provider's input and output tokens for the turn
    claimed: bool = False  # the reply said Pisa made a change, and the claim rail let it through
    # every tool call of the turn with its arguments (the replay counts writes without owner_said)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # the indexes in `calls` the agent refused as an unknown tool (a deferred
    # tool called before `search_tools` revealed it): attempted, never run, so nothing was read or written
    refused: list[int] = field(default_factory=list)
    # the traits they approved that this turn's instructions carried (`character_block`): what a
    # trait guarantees is its line in the prompt, never a fixed phrase in the reply (the replay checks it)
    character: tuple[str, ...] = ()
    # what this turn's tools said wasn't done ("Not changed: …", Pisa's error status), and how
    # many of the reply's claims the record didn't back (each rewritten into what happened)
    failures: dict[tuple[str, str], str] = field(default_factory=dict)  # (tool, its target) → what it said
    done_tools: set[str] = field(default_factory=set)  # the tools whose change this turn did
    # the tools whose outward act ran but whose read-back didn't confirm it: never "fatto"
    unverified: set[str] = field(default_factory=set)
    proposals: list[tuple[str, str]] = field(default_factory=list)  # plan_task's (title, HH:MM): proposed
    lately_tools: frozenset[str] = frozenset()  # … and the changes Pisa made for this device in the last 2 h
    # … and those of them whose read-back didn't confirm them: no claim stands on them
    unverified_lately: frozenset[str] = frozenset()
    unbacked: int = 0
    # "drawing" when a tap in a drawing started the turn ("ask Pisa about this"): its
    # words are the drawing's label, never their command (they ground nothing, join nothing, answer no yes)
    origin: str = "words"
    trace_id: str = ""  # the turn's trace, for the talk's record of it (`record_of`)
    # the record the turn's claims stand on, kept at its end (the replay's check)
    kept: claim_rules.Record | None = None
    # their words correct Pisa's own change of the previous turn ("no aspetta, intendevo sabato"):
    # that turn's words, which a replacement may stand on with their new day
    corrects: str = ""
    # a talk's one correction line, said and stored after its reply (never spliced in); "" for none
    correction: str = ""

    def record(self) -> claim_rules.Record:
        """Code's record for this turn's claims: what this turn's tools did and what failed, the
        changes Pisa made lately (or read back), and whether this turn asked a yes or failed."""
        # what this turn's tools did, and the acts code recorded (never a proposal: plan_task)
        # an outward act whose read-back didn't confirm it is never a done change
        done = (set(self.done_tools) | (decisions.acted_tools() - claim_rules.PROPOSES)) - self.unverified
        if self.done:
            done.add(claim_rules.ANY)  # a change its first part made
        lately = set(self.lately_tools) | ({claim_rules.ANY} if READ_BACK & set(self.tools) else set())
        failed = frozenset(tool for tool, _ in self.failures)
        blocked = bool(self.approvals or failed)
        # they asked about what is (or was done), not for a change: "l'hai messo?", "sono per oggi, il 30?"
        asked = claim_rules.asks_what_was_done(self.text) or (
            "?" in self.text and not action_rules.commands(self.text)
        )
        return claim_rules.Record(
            frozenset(done), failed, frozenset(lately), blocked, asked,
            frozenset(self.unverified) | self.unverified_lately,
        )  # fmt: skip

    @property
    def words(self) -> str:
        """The user's words a write may stand on: this turn's, after an unanswered turn's. Words
        with a command of their own are their own sentence (joined with a space,
        "metti il dentista giovedì" + "intanto metti la palestra alle 17" put the dentist at 17)."""
        if self.origin == "drawing":  # a drawing's label is never their words
            return ""
        text = action_rules.without_label(self.text)  # a tap's label is the model's, never theirs
        if not self.unanswered:
            return text
        if not action_rules.commands(text):
            return f"{self.unanswered} {text}"  # "metti il dentista giovedì" + "alle 15:30": one command
        if "?" in self.unanswered:
            # their question, answered,
            # carries no command; joined, its "?" made their own command a question nothing could stand on
            return text
        return f"{self.unanswered.rstrip(' .')}. {text}"


# The turn being answered, opened by `_respond` (and by the yes code decides, `_answered`): the services its
# tools reach read it here (actions, assistant, rituals, the skill starters), several through other services
# (the accountant's draft, a plan option's write, a ritual's fix).
current_turn: ContextVar[TurnState | None] = ContextVar("current_turn", default=None)


def turn_device() -> uuid.UUID | None:
    """The device the turn being answered came from (None outside a turn)."""
    turn = current_turn.get()
    return turn.device if turn is not None else None


SendHolding = Callable[[str, str], Awaitable[None]]
"""(holding_id, text) → send a holding `say`."""
REST_TITLE = {"it-IT": "Dettagli", "en-US": "Details"}  # the note of a short answer's rest
REST_LINES, REST_LINE = 6, 200  # the note's lines and characters; past them, a sheet
RESEARCH_TOOLS = frozenset(
    {"web_search", "read_page", "map_search", "place_details", "open_at", "show_places", "walk_times",
     "show_findings"}
)  # fmt: skip


@dataclass
class TurnWatch:
    """One turn's clock: when the utterance arrived, Pisa's last sentence, the tool running, the kind."""

    arrived: float
    spoke: float | None = None
    finished: bool = False
    tool: str | None = None
    doing: str | None = None  # the model's words for the step it is on (a research tool's `saying`)
    held: float | None = None  # seconds to the holding phrase
    progress: bool = False
    kind: str = "chat"  # yes | question | repeat | action | research | tool | chat
    device: uuid.UUID | None = None
    head: bool = False  # the reflex's reads on the partial words were ready for the final ones


# The tools that change something for the user: a reply after them carries titles from their results.
WRITES = results.WRITE_TOOLS


def _own_data(text: str) -> str:
    """Pisa's own data the reflex read (titles may come from an invite): data, never instructions."""
    block = data_block("day-context", text, what="Pisa's own data, read for the user's words")
    return f"{block.rule}\n{block.text}"


def _outside_data(text: str) -> str:
    """The reflex read a stranger's title (an invite, an email's
    task, a provider's place): outside text, it taints the turn."""
    block = data_block("invite", text, what="Pisa's own data, read for the user's words; titles are others'")
    return f"{block.rule}\n{block.text}"


def _reply_kind(turn: TurnState, research: Any) -> str:
    if WRITES & set(turn.tools):
        return "action"
    # a follow-up answered from Pisa's findings keeps the research conversation going (the next
    # follow-up's spoken answer is short too)
    if any(t in RESEARCH_TOOLS or t == research_rules.FINDINGS_TOOL for t in turn.tools):
        return "research"
    return "tool" if turn.tools else "chat"


# the `understood` event to the device that spoke ({meant, raw, corrections, ask}).
SendUnderstood = Callable[[dict[str, Any]], Awaitable[bool]]
LAST_UNDERSTOOD = timedelta(minutes=5)  # a user's "no, ho detto X" corrects Pisa's fixes this recent
# when most of the small model's recent answers came after the budget (paid for, never
# used), the step stops asking it for a while: the confirm slot's model is too slow for 300 ms.
SLOW_WINDOW = 5
SLOW_LATE = 3
SLOW_PAUSE = timedelta(minutes=10)
# app_settings: they said "disegna meno" / "solo testo" ({"since"}):
# Pisa draws nothing of its own until they ask for a drawing or says "disegna pure"
WORDS_ONLY_KEY = "drawings_words_only"
# the reads whose result lists their open tasks for a day: the tasks-omitted rail checks the reply
TASK_DAY_READS = frozenset({"get_tasks", "get_plan", "what_now"})
_TOMORROW = re.compile(r"\b(?:domani|tomorrow)\b", re.IGNORECASE)
TAPS_KEPT = 64  # the taps waiting for their turn, at most
LAST_FIXED_KEY = "understand_last"  # + ":<device>": Pisa's last fixes for that device ({at, corrections})
PROGRESS_POLL_S = 0.1  # after the progress window: how often the timer looks for a step that started late
_log = get_logger("pisa.conversation")
# Model requests in one turn (with the research loop guard, the turn's other safety
# ceiling: a pick's parallel rounds take ~5-8); past it the turn ends with NO_ANSWER, not an error.
# RAIL: a turn's model requests are bounded.
MAX_MODEL_REQUESTS = 20
DIFFERENT_LINE = (
    "\nSomething different, your choice: a places card may show ONE place unlike the user's "
    "usual (another style, area or cuisine), with different: true and a `why` that says how it differs, when "
    "it would be welcome (their mood, the same kind of place again and again). About one card in five; the "
    "last one was {since} cards ago. Never one against a hard limit or something the user avoids. Say it "
    "lightly, once."
)
_WITHDRAW = re.compile(
    r"\b(?:lascia stare|lascia perdere|lasciamo perdere|anzi no|non importa|annulla|cancella tutto|niente|"
    r"never ?mind|forget it|scratch that|cancel)\b|^\W*no\b",
    re.IGNORECASE,
)


def _withdraws(text: str) -> bool:
    """The user's words take back what came before ("no, lascia stare", "anzi no", "forget it")."""
    return bool(_WITHDRAW.search(text)) or action_rules.is_short_no(text)


UNANSWERED_JOIN_S = 300.0  # an unanswered turn's words count for the next turn this recent
UNANSWERED_CHARS = 1000


# the round that says a reply cut at the output cap whole (the provider's `length`, or the cap
# reached)
def as_continuation(past: list[Any]) -> list[Any]:
    """A follow-up round's note goes as instructions, and
    instructions alone make an empty request the provider drops, so a history ending on the model's own reply
    would be sent ending on it: a prefill, rejected with thinking on (and without thinking, a continuation of
    the very reply the round is replacing). That reply was not said and its sentences are in the round's note
    as data, so the round starts from the request before it (their words or the tool results), never from a
    new user message (Pisa reads the newest one as their words: the turn, the cache prefix, the unsaid
    clauses)."""
    if past and isinstance(past[-1], ModelResponse) and not past[-1].tool_calls:
        return past[:-1]
    return past


REWRITE_LINE = (  # a rail's miss as feedback, never Pisa's canned words
    "\nThese sentences of your reply were not said to the user (why, beside each: Pisa's own notes, its "
    "lists, its rules, what it couldn't show, a change it didn't make, a name no result wrote):\n{lines}\n"
    "The user heard everything else. If they still need something from them, say it in one short sentence in "
    "plain words for them, never about Pisa's tools or rules, each name as a result wrote it; if not, say "
    "nothing. "
    "Call no tool."
)
SEARCH_LINE = (  # the general tool before "no"
    "\nThese sentences of your reply were not said to the user: you didn't look it up:\n{lines}\nLook it "
    "up now with web_search (search_tools finds it when it isn't offered), then answer them from what it "
    "finds in one or two short sentences; if it finds nothing, say so in one short sentence. Never send them "
    "to another app, never estimate by eye what a search can give."
)
READ_LINE = (  # their lists read before saying what is or isn't there
    "\nThese sentences of your reply were not said to the user: they say what is or isn't on their lists (or "
    "already done), and you didn't read the lists this turn:\n{lines}\nTheir lists change outside this "
    "conversation (the app, another device). Read them now (get_tasks; my_intentions for what they mean to "
    "do), then answer from what the result says, in one or two short sentences. The user heard the rest of "
    "your reply: never say it again, in any words."
)
CUT_LINE = (
    "\nYour reply was cut at the output limit, mid-sentence:\n{fragment}\nThe user heard only the "
    "sentences before it. Say that sentence again, whole, and end your reply in at most two short sentences. "
    "Call no tool."
)


def cut_short(messages: list[Any], cap: int) -> bool:
    """Whether the turn's last model response stopped at the output cap (reasoning counts against it)."""
    last = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
    if last is None:
        return False
    return last.finish_reason == "length" or (last.usage.output_tokens or 0) >= cap


NO_ANSWER = {
    "it-IT": "Ho cercato a lungo senza una risposta sicura: prova a chiedermelo in modo più preciso.",
    "en-US": "I searched for a long time without a solid answer: try asking me more precisely.",
}

Emit = Callable[[str, bool], Awaitable[None]]
"""(delta, final) → send one `say` chunk."""
SendCard = Callable[[PlaceCard], Awaitable[None]]
"""Send a `card` kind `place` to the device."""
SendPlaces = Callable[[dict[str, Any]], Awaitable[bool]]
"""Send a `card` kind `places` to the device (show_places)."""
SendStep = Callable[[str, str, str], Awaitable[None]]
"""(`started` | `finished`, tool call id, tool name) → one of the turn's tool steps, as it happens (contract
2's STEP_* and TOOL_CALL_* events). Names only: a tool's arguments and results stay on the server."""


async def _step(steps: SendStep | None, what: str, call_id: str, name: str) -> None:
    """A tool step for the device; a device that fails to take it never fails the turn."""
    if steps is None:
        return
    try:
        await steps(what, call_id, name)
    except Exception as exc:
        _log.warning("tool_step_failed", cause=exc.__class__.__name__)


SOURCES_TITLE = {"it-IT": "Fonti", "en-US": "Sources"}
KEPT = {"it-IT": "Capito.", "en-US": "Got it."}  # all it said asked
THANKS = {"it-IT": "Di niente.", "en-US": "You're welcome."}  # after their thanks, all it said asked
HELLO = {"it-IT": "Ciao!", "en-US": "Hi!"}  # after a bare greeting, all it said asked


async def _conversation_for(conn: AsyncConnection, device_id: uuid.UUID, now: datetime) -> uuid.UUID:
    row = await (
        await conn.execute(
            "SELECT id, last_message_at FROM conversations WHERE device_id = %s ORDER BY "
            "last_message_at DESC LIMIT 1",
            (device_id,),
        )
    ).fetchone()
    if row is not None and now - row[1] <= NEW_CONVERSATION_AFTER:
        return row[0]
    conversation_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation_id, device_id, now, now),
    )
    return conversation_id


async def _recent_turns(conn: AsyncConnection, conversation_id: uuid.UUID) -> list[Turn]:
    rows = await (
        await conn.execute(
            "SELECT role, content, quoted, model_messages, outside_kinds, meant, corrections FROM (SELECT "
            "role, content, quoted, model_messages, outside_kinds, meant, corrections, created_at, id FROM "
            "messages WHERE conversation_id = %s ORDER BY created_at DESC, id DESC LIMIT %s) recent "
            "ORDER BY created_at, id",
            (conversation_id, HISTORY_MESSAGES),
        )
    ).fetchall()
    # the model reads the marked form, built now from their words and Pisa's `meant`; what
    # grounds a change is their words with the step's corrections only
    turns: list[Turn] = []
    for r in rows:
        if (
            r[0] == "assistant"
            and claim_rules.is_correction(r[1])
            and turns
            and turns[-1].role == "assistant"
        ):
            # a talk's correction line, stored apart: the model reads it with the reply it corrects
            turns[-1] = replace(turns[-1], content=_with_correction(turns[-1].content, r[1]))
            continue
        turns.append(
            Turn(
                r[0],
                rules.as_read(r[1], r[5], r[6]),
                r[2],
                r[3],
                tuple(r[4] or ()),
                said=rules.his_words(r[1], r[6]),
            )
        )
    return turns


def _with_correction(reply: str, correction: str) -> str:
    """The reply with its correction line, for the history: before the reply's closing question, which stays
    the last thing Pisa asked (their "sì" answers it)."""
    pieces = form.split_sentences(reply)
    if len(pieces) > 1 and pieces[-1].rstrip().endswith("?"):
        return " ".join([*(p.strip() for p in pieces[:-1]), correction, pieces[-1].strip()])
    return f"{reply.rstrip()} {correction}"


_FILLERS = frozenset(p.text for p in holding.PHRASES)


def _filler(text: str) -> bool:
    """A lone holding fragment ("Dunque…", "Let's see…"): nothing said on its own."""
    one = text.strip()
    return not one or one in _FILLERS or re.fullmatch(r"\w+(?:,?\s\w+)?\s*(?:…|\.\.\.)", one) is not None


def _readable(turn: Turn) -> str:
    """A turn as the history's digest tells it: a tap's quoted label (outside text) never retold."""
    if turn.role == "user" and turn.quoted:
        return turn.content.split(":", 1)[0] + ": (their tap on Pisa's drawing)"
    return turn.content


async def _first_today(conn: AsyncConnection, now: datetime, timezone: str) -> bool:
    """No reply from Pisa yet today (the user's local day): the one exchange that may open with a greeting."""
    tz = ZoneInfo(timezone)
    midnight = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM messages WHERE role = 'assistant' AND created_at >= %s)",
            (midnight,),
        )
    ).fetchone()
    return not (row and row[0])


async def _add_message(
    conn: AsyncConnection,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    lang: str,
    now: datetime,
    quoted: bool = False,
    model: list[Any] | None = None,
    outside: list[str] | None = None,
    meant: str | None = None,
    corrections: list[dict[str, str]] | None = None,
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO messages (conversation_id, role, content, lang, created_at, quoted, model_messages, "
            "outside_kinds, meant, corrections, origin) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (conversation_id, role, content, lang, now, quoted, Jsonb(model) if model else None,
             outside or [], meant, Jsonb(corrections) if meant is not None else None, wrist.current()),
        )
    ).fetchone()  # fmt: skip
    await conn.execute("UPDATE conversations SET last_message_at = %s WHERE id = %s", (now, conversation_id))
    assert row is not None
    return int(row[0])


@dataclass(frozen=True)
class ResearchArm:
    """An arm of the A/B on pick and deep turns only (their words
    ask to find, recommend or compare); every other turn runs as today. `effort`: the model's reasoning
    effort on those turns (None: the slot's own); `reasoning_tokens`: more room for its thinking (the turn's
    reservation and the model's output cap grow by it); `slot`: another model slot for them (the research
    model, `places.research`), its spend counted under the conversation's slot."""

    name: str = "a"
    effort: str | None = None
    reasoning_tokens: int = 0
    slot: str | None = None

    def settings(self) -> dict[str, Any] | None:
        """The model settings of a pick's run (Anthropic's effort; ignored by other providers)."""
        out: dict[str, Any] = {}
        if self.effort:
            out["anthropic_effort"] = self.effort
        if self.reasoning_tokens:
            out["max_tokens"] = MAX_OUTPUT_TOKENS + self.reasoning_tokens
        return out or None


RESEARCH_ARMS = {
    "a": ResearchArm(),  # today: the conversation's model at its own effort
    "b": ResearchArm("b", effort="high", reasoning_tokens=6144),  # the same model thinking harder
    "c": ResearchArm("c", slot="places.research"),  # the research slot's stronger model, picks only
}


class ConversationService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        gateway: ModelGateway,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        deps: ConversationDeps | None = None,
    ) -> None:
        # every collaborator, one typed set (its tools' families, what it reads itself)
        self.deps = deps or ConversationDeps()
        self._tools = self.deps.tools  # what its turns' tools read, one service per family
        self._pool = pool
        self._skills = self.deps.skills or SkillsService(pool)  # without embeddings, the trigram index
        self.unasked = self.deps.unasked  # what Pisa said unasked, per device (newest wins)
        self._saving: set[asyncio.Future[Any]] = set()  # cancelled replies being saved
        self._last_turn: dict[uuid.UUID, TurnState] = {}  # each device's latest turn, for `continues`
        # a talk's turns: (device, trace id) → the stored reply's message id, which `spoken` may replace
        self._reply_rows: OrderedDict[tuple[uuid.UUID, str], int] = OrderedDict()
        # each device's latest reply as it was said (every path's words, a quick answer's too): a turn the
        # user went on with never says them again
        self._reply_said: dict[uuid.UUID, list[str]] = {}
        self._previous: dict[uuid.UUID, TurnWatch] = {}  # each device's last turn (its kind: a follow-up)
        self.hold_after_s = HOLD_AFTER_S  # tests that check exact replies set it high
        self._last_hold: dict[uuid.UUID, str] = {}  # each device's last holding phrase (never twice in a row)
        # when each device last heard a holding phrase, and Pisa's last whole reply: one holding
        # phrase per wait, across the utterances said while Pisa works ("Allora… Vediamo…" again and again)
        self._held_at: dict[uuid.UUID, float] = {}
        self._replied_at: dict[uuid.UUID, float] = {}
        self.latencies: deque[tuple[str, float | None]] = deque(maxlen=500)  # (reply kind, first audio s)
        self._gateway = gateway
        self._owner = owner
        self._clock = clock
        self.objects: dict[uuid.UUID, dict[str, Shown]] = {}  # each device's objects Pisa showed (here)
        # a tap's element, for its turn (keyed by its words, so two quick taps never
        # swap their labels and an outside-drawn label is always quoted)
        self._taps: dict[tuple[uuid.UUID, str], visual_rules.Tap] = {}
        # the device's word on a show whose turn hadn't stored it yet: present_id → (outcome, at)
        self._held_outcomes: dict[str, tuple[str, datetime]] = {}
        # how a pick's turn thinks; today's by default (the depth job's
        # arms set it on their throwaway passes only)
        self.research_arm = ResearchArm()
        self._voice_read: dict[uuid.UUID, TurnState] = {}  # the turn Pisa read the morning or the trip in
        # the knock reading's last part, per device, for that reading only (a bare "ok" after
        # "dammi tutto" never answers a miss's reason)
        self._read_last: dict[uuid.UUID, str | None] = {}
        self._late: deque[bool] = deque(maxlen=SLOW_WINDOW)  # recent turns that asked: a call came too late
        self._slow_until: datetime | None = None
        self._rebuilds: set[asyncio.Future[Any]] = set()

    async def _understand(
        self, device_id: uuid.UUID, text: str, lang: str, trace_id: str
    ) -> rules.Result | None:
        """The understanding step: fillers, repairs and names against the user's lexicon; the
        small model only for doubts. Never fails the turn: on any error the words go to the model as said.
        A user's "no, ho detto X" right after Pisa fixed a name keeps the pair (heard, meant) on the server,
        and their words stand as said."""
        if self.deps.lexicon is None:
            return None
        started = time.monotonic()
        try:
            # The lexicon's query and a rebuild count in the 300 ms too: past it, the last lexicon
            # built is used and the rebuild finishes in the background; with none yet, the words stay as said.
            current = asyncio.ensure_future(self.deps.lexicon.current())
            self._rebuilds.add(current)
            current.add_done_callback(self._rebuild_done)
            try:
                lex, _, _ = await asyncio.wait_for(
                    asyncio.shield(current), timeout=understanding.UNDERSTAND_S
                )
            except TimeoutError:
                built = self.deps.lexicon.latest()
                _log.info("understand_lexicon_slow", trace_id=trace_id, stale=built is not None)
                if built is None:
                    return None
                lex = built[0]
            fixed_by_owner = rules.owner_fix(text)
            async with self._pool.connection() as conn:
                last = await get_setting(conn, f"{LAST_FIXED_KEY}:{device_id}")
                await delete_setting(conn, f"{LAST_FIXED_KEY}:{device_id}")
            recent = last and self._clock() - datetime.fromisoformat(last["at"]) < LAST_UNDERSTOOD
            if fixed_by_owner and recent:
                await self._keep_owner_fix(last["corrections"], fixed_by_owner, lang)

            async def ask(utterance: str, doubt: rules.Doubt) -> bool:
                async def call(ctx: CallContext) -> bool:
                    return await understanding.confirm(ctx.model, utterance, doubt, ctx.usage.update)

                return await self._gateway.run(
                    understanding.SLOT, call, prompt_chars=len(rules.MODEL_PROMPT) + 2 * len(utterance) + 200,
                    max_output_tokens=understanding.CONFIRM_TOKENS, trace_id=trace_id,
                )  # fmt: skip

            slow = self._slow_until is not None and self._clock() < self._slow_until
            left = max(0.0, understanding.UNDERSTAND_S - (time.monotonic() - started))
            done = await understanding.understand(
                text, lex, lang, None if slow else ask, names=fixed_by_owner is None, budget_s=left
            )
            self._note_late(done, trace_id)
        except Exception:
            _log.exception("understand_failed", trace_id=trace_id)
            return None
        res = done.result
        if res.corrections:
            fixes = [{"from": c.frm, "to": c.to, "via": c.via} for c in res.corrections]
            async with self._pool.connection() as conn:
                await set_setting(
                    conn,
                    f"{LAST_FIXED_KEY}:{device_id}",
                    Jsonb({"at": self._clock().isoformat(), "corrections": fixes}),
                )
            await self.deps.lexicon.note_fixed([c.to for c in res.corrections])
        _log.info(
            "understood", trace_id=trace_id, changed=res.changed, corrections=len(res.corrections),
            by_model=sum(c.via == "model" for c in res.corrections), model_calls=done.asked, ask=res.ask,
            repaired=res.repaired, owner_fix=fixed_by_owner is not None, ms=round(done.seconds * 1000, 1),
        )  # fmt: skip
        return res

    def _rebuild_done(self, task: "asyncio.Future[Any]") -> None:
        """A lexicon (re)build that finished after the turn went on: its failure is logged here, never left as
        "Task exception was never retrieved"."""
        self._rebuilds.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _log.warning("lexicon_rebuild_failed", cause=exc.__class__.__name__)

    def _note_late(self, done: understanding.Understood, trace_id: str) -> None:
        if not done.asked:
            return
        self._late.append(done.late > 0)
        if sum(self._late) >= SLOW_LATE:
            self._slow_until = self._clock() + SLOW_PAUSE
            self._late.clear()
            _log.warning("understand_model_slow", trace_id=trace_id, pause_min=SLOW_PAUSE.seconds // 60)

    async def _keep_owner_fix(self, fixes: list[dict[str, str]], meant: str, lang: str) -> None:
        """The user undid Pisa's fix: "heard X, meant Y" pairs, on the server only (a real test set).
        Only the fixes the user's words are about."""
        fixes = [c for c in fixes if rules.corrects(meant, c["from"], c.get("to", c["from"]))]
        if not fixes:
            return
        async with self._pool.connection() as conn:
            for c in fixes:
                await conn.execute(
                    "INSERT INTO understand_corrections (id, heard, meant, lang, via, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), c["from"][:200], meant[:200], lang, c["via"], self._clock()),
                )
        _log.info("understand_owner_fix", corrections=len(fixes))

    async def respond(
        self,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        emit: Emit,
        *,
        phone_maps: PhoneMaps | None = None,
        **rest: Any,
    ) -> str:
        """Answer one final utterance (`_answer`). `phone_maps`: the phone's own maps for this turn (contract
        2.3, a socket that granted `pisa.maps`): the places service asks them first while the turn runs
        (`services.phone_maps.current`); None (a run over HTTP, no grant): never asked."""
        with phone_maps_turn(phone_maps):
            return await self._answer(device_id, text, lang, emit, **rest)

    async def _answer(
        self,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        emit: Emit,
        *,
        hold: SendHolding | None = None,
        spoken: bool = True,
        origin: str = "words",
        **rest: Any,
    ) -> str:
        """Answer one final utterance (see `_respond`). The first audio within about a second. When no
        sentence is ready HOLD_AFTER_S after the utterance arrived, `hold` gets a holding phrase (the app
        plays it from its cache); after PROGRESS_AFTER_S of silence on long work, a line naming the step."""
        tapped = origin == "drawing"  # a tap in a drawing, never their own words
        if not spoken and not tapped:
            # typed words ask their answer in their own language (a research question's lead and sheet
            # too); a voice transcript keeps the turn's: it can mishear one (§32 D2: "Ciao, come va?" typed
            # on an en-US device was answered half in English, half in Italian); a language they ask for
            # ("Can you answer in English?") wins
            asked_for = memoria_rules.style([{"id": 0, "text": text}]).lang
            lang = asked_for or research_rules.asked_lang(text, lang)
        if (
            self._tools.autopilot is not None and not tapped
        ):  # their correction of Pisa becomes a private test
            self._tools.autopilot.hear(device_id, text, lang)
        watch = TurnWatch(arrived=time.monotonic(), device=device_id)
        lock = asyncio.Lock()  # one `say` at a time: the timer and the answer share the stream

        async def said(delta: str, final: bool) -> None:
            async with lock:
                if delta.strip():
                    watch.spoke = time.monotonic()
                watch.finished = watch.finished or final
                if final:
                    self._replied_at[device_id] = time.monotonic()
                await emit(delta, final)

        async def timer() -> None:
            await asyncio.sleep(self.hold_after_s)
            if hold is not None:
                async with lock:
                    held = self._held_at.get(device_id)
                    waiting = (
                        held is not None
                        and time.monotonic() - held < HOLD_ONCE_PER_S
                        and self._replied_at.get(device_id, 0.0) < held
                    )  # the user heard one already in this wait, and no reply came since
                    if watch.spoke is None and not watch.finished and not waiting:
                        self._held_at[device_id] = time.monotonic()
                        phrase = holding.pick(
                            lang, "lookup" if watch.tool else "any", self._last_hold.get(device_id)
                        )
                        self._last_hold[device_id] = phrase.id
                        await hold(phrase.id, phrase.text)
                        watch.held = time.monotonic() - watch.arrived
            await asyncio.sleep(
                max(
                    0.0,
                    self.hold_after_s + PROGRESS_AFTER_S - HOLD_AFTER_S - (time.monotonic() - watch.arrived),
                )
            )
            while not watch.finished:  # a step that starts after the window still gets its line, once
                async with lock:
                    quiet = (
                        watch.spoke is None
                        or time.monotonic() - watch.spoke >= PROGRESS_AFTER_S - HOLD_AFTER_S
                    )
                    if not watch.finished and not watch.progress and watch.tool is not None and quiet:
                        # the model's own words for the step ("Leggo le recensioni del
                        # secondo"); else the template by tool name when it gave none, or only filler.
                        line = form.keep(watch.doing, first=False, greet=False) if watch.doing else ""
                        if line and line.rstrip()[-1:] not in ".!?\u2026":  # a cut step line ("pizzerie Ho")
                            line = line.rstrip() + "."
                        await emit((line or holding.progress_line(lang, watch.tool)) + " ", False)
                        watch.progress = True
                        return
                await asyncio.sleep(PROGRESS_POLL_S)

        async def budget() -> None:
            """A voice turn past its budget with no answer said yet: Pisa says it is taking long,
            once, as a holding phrase (`use` slow: not the answer's first audio, not in the reply's text); the
            turn goes on (a correct but slow turn is never cut here). After a step's progress line, or while
            Pisa is still speaking, nothing more is said; a typed turn hears nothing (it reads the answer)."""
            if hold is None or not spoken:
                return
            await asyncio.sleep(TURN_BUDGET_S)
            async with lock:
                quiet = watch.spoke is None or time.monotonic() - watch.spoke >= TURN_BUDGET_S / 2
                if not watch.finished and not watch.progress and quiet:
                    phrase = holding.slow_phrase(lang)
                    await hold(phrase.id, phrase.text)
                    watch.progress = True  # one line of the kind per turn: no step line after it
                    _log.info("turn_over_budget", budget_s=TURN_BUDGET_S, trace_id=rest.get("trace_id"))

        # heard in 1 s: a search with something unusual in it is said back at once ("Pizza al
        # taglio da guida, non la più vicina… cerco."): never a generic holding phrase when there is that
        if not tapped and (line := restate.heard(text, lang)) is not None:
            await said(line + " ", False)
        ticking = asyncio.ensure_future(timer())
        over = asyncio.ensure_future(budget())
        with decisions.collecting(
            text, self._tools.data.today() if self._tools.data is not None else None
        ) as moves:
            try:
                return await self._respond(
                    device_id, text, lang, said, watch=watch, spoken=spoken, origin=origin, **rest
                )
            finally:
                ticking.cancel()
                over.cancel()
                if moves:  # what Pisa did stays done after a cancel: so does its record
                    await asyncio.shield(self._decided(moves, device_id, rest.get("trace_id")))

    async def _respond(
        self,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        emit: Emit,
        *,
        watch: "TurnWatch",
        trace_id: str,
        card: SendCard | None = None,
        places_card: SendPlaces | None = None,
        sources_card: SendPlaces | None = None,
        weather_card: SendPlaces | None = None,
        directions: SendDirections | None = None,
        speaker: Speaker | None = None,
        compose: SendToPhone | None = None,
        call_phone: SendToPhone | None = None,
        understood: SendUnderstood | None = None,
        steps: SendStep | None = None,
        continued: bool = False,
        device: Device | None = None,
        reply_to: str | None = None,
        yes_by_code: bool = True,
        voice: bool = False,
        heard: Any = None,
        spoken: bool = True,
        origin: str = "words",
        phone_grants: Collection[str] | None = tool_registry.PHONE_EXTENSIONS,
        app_map: AppMap | None = None,
        app_action: AppAction | None = None,
    ) -> str:
        """Answer one final utterance. Sentences go to `emit` as they complete; the last chunk has final=True.
        When the answer was about one place, `card` gets that place's card after the reply. `speaker`: the
        reply's audio, fed by the caller's `emit`; the turn ends when it has spoken.

        Cancelling the task stops the model stream; nothing more is emitted (no final chunk either).
        `continued`: the words join the device's previous turn (`continues`); what that turn already did stays
        done and isn't done again. `phone_grants`: the extensions the client's hello granted; each phone
        action is offered only on its own (`tools.PHONE_ACT_EXTENSION`: a hello granting only `pisa.call` gets
        only the call tools). A run over HTTP granted none (None), so "what can you do" and the overclaim rail
        count no phone action for it. `app_map`: the app's screen map from its latest run (contract 2.2),
        untrusted data for app_help; `app_action`: opens one of the app's own screens and returns the app's
        report (`pisa.app`), None when the client didn't pick that extension.
        """
        # what this client's app opens (maps, compose, call), as "what can you do" counts it
        granted = None if phone_grants is None else frozenset(phone_grants)
        phone_senders = (
            compose if granted is not None and "pisa.compose" in granted else None,
            call_phone if granted is not None and "pisa.call" in granted else None,
            directions if granted is not None and "pisa.directions" in granted else None,
        )
        foreign_titles = getattr(self._tools.data, "foreign_titles", None)
        titles: list[str] | None = []
        if foreign_titles is not None:  # any of Pisa's lists naming one of them is outside text
            try:
                titles = await foreign_titles()
            except UNREACHABLE:
                unreachable(_log, "foreign_titles")
                titles = None  # unknown: every list this turn is outside text
        open_turn(titles)  # the turn's taint opens here; each run of it watches its own (`watch_turn`)
        # their standing preferences (Memoria), read once a turn, after the turn's taint opens: the block the
        # model reads, and the reply controls code recognises in them (a brevity's word cap, a reply language)
        memoria = self._tools.memoria if experimental.on("note", self.deps.enabled) else None
        standing: list[dict[str, Any]] = []
        kept_notes = 0
        if memoria is not None:
            standing, kept_notes = await memoria.for_turn()
        style = memoria_rules.style(standing)
        from_wrist = wrist.current() == "watch"
        if from_wrist and (style.words is None or style.words > memoria_rules.BRIEF_WORDS):
            # a run from the watch is a short spoken answer by code, the cap "rispondimi breve" sets (25
            # words, two sentences): what it cuts goes to the phone's Dettagli. No prompt change.
            style = replace(style, words=memoria_rules.BRIEF_WORDS, sentences=memoria_rules.BRIEF_SENTENCES)
        if style.lang is not None and speaker is None and not voice:  # a typed reply (a voice keeps its own)
            lang = style.lang
        # what the cut reply said, for a continuation; this reply's words are kept the same way
        said_earlier = form.split_sentences("".join(self._reply_said.get(device_id, []))) if continued else []
        said_now: list[str] = []
        self._reply_said[device_id] = said_now
        emit = _keeping(emit, said_now, said_earlier)
        noted: dict[str, PlaceCard] = {}
        tapped = origin == "drawing"
        # a tap in a drawing is answered by the model, read-only: no reflex
        # of their phrases, no yes or no by code ("sì" on a drawing approves nothing)
        reflexless = continued or tapped
        # "Pisa, ferma tutto" / "stop everything" stops Pisa by code, as the app's
        # /v1/stop does: never a model round that may say the switch isn't there. Their own words only (never
        # a tap's label); a continuation's new words are theirs too. A question about stopping ("how do I stop
        # everything?") is not the request: it goes to the model, which can still call stop_everything.
        if (
            not tapped
            and self._tools.autopilot is not None
            and stop_rules.asks_to_stop(text)
            and not text.rstrip().endswith("?")
        ):
            said = await self._stop_heard(device_id, text, lang)
            watch.kind = "stop"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # their "Ok, puoi ripartire" / "you can resume now" while Pisa is stopped resumes it by code, as
        # stopping does: never a model round that may say nothing was stopped. Only their plain request (a
        # question, or "riprendi la palestra", goes to the model); not stopped, the model answers.
        if (
            not tapped
            and self._tools.autopilot is not None
            and stop_rules.resumes_by_code(text)
            and await self._tools.autopilot.stopped()
        ):
            said = await self._resume_heard(device_id, text, lang)
            watch.kind = "resume"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # "eh?" gets the last sentence again, shorter: deterministic, no model round.
        if not reflexless and form.is_huh(text) and (said := await self._again(device_id, text, lang)):
            watch.kind = "repeat"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # a note for the architect is stored as the user's feedback and said back as
        # noted; never a message to anyone, no model round.
        if not reflexless and form.for_architect(text):
            await self._note_for_architect(device_id, text)
            said = form.NOTED_FOR_ARCHITECT.get(lang, form.NOTED_FOR_ARCHITECT["en-US"])
            watch.kind = "feedback"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # "cosa puoi fare?": by code, from what this install has connected and what this phone's app opens,
        # with what isn't connected yet and where to connect it; said once, no model round to overclaim
        if not reflexless and abilities_rules.asks(text):
            facts = await self._abilities(device_id, *phone_senders)
            # a feature their question names that is off: not on PISA, never "not connected yet"
            facts = replace(facts, asked_off=tuple(experimental.named_off(text, self.deps.enabled)))
            said = abilities_rules.answer(facts, lang)
            async with self._pool.connection() as conn:
                conversation_id = await _conversation_for(conn, device_id, self._clock())
                await _add_message(conn, conversation_id, "user", text, lang, self._clock())
            await self._save_reply(conversation_id, said, lang)
            watch.kind = "abilities"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # their answer to Pisa's cue-question, "e tu, come stai?", a trait's yes: by code, from
        # Pisa's records (no model round, nothing done or sent)
        if (
            not reflexless
            and (said := await self._voice2_heard(device_id, text, lang, yes_by_code)) is not None
        ):
            watch.kind = "voice2"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # "ricordatelo", "non ricordarmelo", "no, era con Nino", only right after recall
        # answered one episode (Pisa's own last thing), by code on that episode (no model round, nothing sent)
        if not reflexless and (said := await self._memory_heard(device_id, text, lang)) is not None:
            watch.kind = "memory"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # the CEO spoken, a closed set of their phrases on the paths Pisa's own last thing
        # opened ("consiglio" after the knock's reading, "avanti"/"basta" inside il consiglio or la prova
        # generale, "sciame su questo" on the idea Pisa just read them), by code from records (nothing sent
        # out)
        if not reflexless and (said := await self._ceo_heard(device_id, text, lang)) is not None:
            watch.kind = "voice4"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # the night by voice, a closed set of their phrases on the paths Pisa's own last thing
        # opened ("cosa hai fatto stanotte?", "facciamo a pugni su questa idea", their p, "scommetto che …",
        # il quarto d'ora), by code from Pisa's records (no model round, nothing done or sent outward)
        if (
            not reflexless
            and (said := await self._night_heard(device_id, text, lang, device, reply_to)) is not None
        ):
            watch.kind = "voice3"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # right after Pisa's reading said a miss's reason, "lascia stare" or "sì, contalo",
        # by code on that reason (a closed set; anything else is the model's)
        if not reflexless and (said := await self._reason_heard(device_id, text, lang)) is not None:
            watch.kind = "voice6"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # "dimmi", "cosa c'è?", "dopo", "basta", "dammi tutto": the knock, the morning and
        # the tragitto, by code from their records (no model round, nothing done or sent).
        if (
            not reflexless
            and (said := await self._voice_heard(device_id, text, lang, yes_by_code, device, reply_to))
            is not None
        ):
            watch.kind = "voice1"
            await emit(said, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # RAIL: a bare yes/no answers Pisa's one question by code, bound to its
        # payload; nothing Pisa read can approve.
        # in a talk, a yes decides only right after Pisa said its question
        # aloud (`yes_by_code`); otherwise the model reads it and asks again.
        if (
            not reflexless
            and yes_by_code
            and (
                (said := await self._answered(device_id, text, lang, spoken)) is not None
                or (said := await self._check_in_answered(device_id, text, lang)) is not None
                or (said := await self._ritual_answered(device_id, text, lang)) is not None
            )
        ):
            watch.kind = "yes"
            await emit(said, True)  # the user's yes or no to Pisa's question, with no model call
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return said
        # what the user meant, before Pisa's answer; the model gets it and the words as recognised.
        step = None if tapped else await self._understand(device_id, text, lang, trace_id)
        prompt = text
        tap = self._taps.pop((device_id, text), None) if tapped else None
        if tap is not None:  # a block's label (quoted when outside text drew it) and the drawing around it
            framed = data_block(
                "sheet-tapped" if tap.outside else "sheet-own", visual_rules.tap_data(tap),
                what="the element of Pisa's drawing they tapped, and the drawing around it",
            )  # fmt: skip
            prompt = f"{visual_rules.tap_head(text, tap, lang)}\n{framed.rule}\n{framed.text}"
        elif tapped:  # the box's label (its values may come from outside), or a tap with no server copy: data
            label = data_block(
                "drawing-label", drawing_rules.ASK_MARK.sub("", text), what="the drawing's text they tapped"
            )
            prompt = f"{text[: text.find(':') + 1]} (their tap on Pisa's drawing)\n{label.rule}\n{label.text}"
        meant: str | None = None  # Pisa's understanding, stored beside their words
        if step is not None and (step.changed or step.ask):
            # what Pisa understood goes back only for words a recogniser wrote: typed words were never
            # heard (the app shows its own bubble, and the user's words never come back to it a second time)
            if understood is not None and spoken:
                await understood(
                    {"meant": step.meant, "raw": text, "ask": step.question,
                     "corrections": [{"from": c.frm, "to": c.to, "via": c.via} for c in step.corrections]}
                )  # fmt: skip
            prompt = rules.marked(step, text, lang) if step.changed else text
            meant = step.meant if step.changed else None
        before = self._last_turn.get(device_id) if continued else None
        async with self._pool.connection() as conn:
            conversation_id = await _conversation_for(conn, device_id, self._clock())
            turns = await _recent_turns(conn, conversation_id)
            greet = await _first_today(conn, self._clock(), self._owner.tz)
            fixes = rules.fixes_of(step) if step is not None and meant is not None else None
            # a tap's label from outside text (the box's, or a block's that
            # outside text drew) is stored quoted: the history gives it back as data, never as their words
            label_outside = tapped and (tap is None or tap.outside)
            await _add_message(
                conn, conversation_id, "user", text, lang, self._clock(), meant=meant, corrections=fixes,
                quoted=label_outside, outside=["drawing-label"] if label_outside else None,
            )  # fmt: skip
        doubt = step.question if step is not None else None  # a hint, the model decides

        async def ask_doubt() -> str:
            """The understanding step's own question, when the model can't answer at all."""
            assert doubt is not None
            await self._save_reply(conversation_id, doubt, lang)
            self._last_turn[device_id] = TurnState(conversation_id, text)  # the newest question wins
            watch.kind = "question"
            await emit(doubt, True)
            if speaker is not None:
                await speaker.done()
            self._log_turn(trace_id, watch, speaker)
            return doubt

        turn = TurnState(
            conversation_id,
            text,
            device=device_id,
            trace_id=trace_id,
            heard_at=self._clock(),
            spoken=spoken,
            origin=origin,
        )
        # a problem, an idea or a question they hand Pisa to keep, or only their thanks or greeting: Pisa's
        # answer asks them nothing of its own (code's one question, a consent or a hunch, still asks)
        hand_off = handed(text) is not None or form.courtesy(text)
        if before is not None:
            turn.added, turn.done = dict(before.added), list(before.done)
        if (previous := self._last_turn.get(device_id)) is not None:  # FACT: from Pisa's own tools, not words
            if (
                not continued
                and not tapped
                and previous.origin != "drawing"  # a drawing's label joins nothing
                and (not previous.answered or previous.questioned)
                and previous.conversation_id == conversation_id
                and time.monotonic() - previous.at < UNANSWERED_JOIN_S
                and not _withdraws(text)
            ):
                # the unanswered turn's own words only, never what it carried itself (no chain past
                # UNANSWERED_JOIN_S), and never when these words withdraw it ("no lascia stare", "annulla",
                # "anzi no")
                turn.unanswered = previous.text[-UNANSWERED_CHARS:]
            turn.named_events = frozenset(previous.events) | (
                previous.named_events if continued else frozenset()
            )
            turn.named_options = frozenset(previous.offered) | (
                previous.named_options if continued else frozenset()
            )
            turn.asked_before = frozenset(previous.proposed)
            turn.asked_order = tuple(previous.proposed)
            turn.before, previous.before = previous, None  # one link only, never a chain
            if (previous.acted or previous.done or previous.proposed) and clean_rules.corrects(text):
                turn.corrects = previous.words[-UNANSWERED_CHARS:]
        self._last_turn[device_id] = turn
        current_turn.set(turn)
        _log.debug("utterance", trace_id=trace_id, lang=lang, chars=len(text))

        # the voice path's turn restores less of the conversation (the user waits on the line)
        past = self._history(turns, history.VOICE_BUDGET_TOKENS if voice else history.BUDGET_TOKENS)
        sent: list[str] = []
        held: list[str] = []  # RAIL: sentences claiming a change, said only once one is made
        screen_claims: list[str] = []  # "it's on your phone", said only once the phone has it
        turn.lately_tools, turn.unverified_lately = await self._acted_lately(device_id)
        # Pisa's ledger, from code: what waits for the user's yes (or waited, and was never answered),
        # with ids, and what Pisa changed in this conversation: never done, never lost.
        waiting = await self._tools.actions.waiting(device_id) if self._tools.actions is not None else []
        changed = (
            await self._tools.actions.changed(device_id, conversation_id)
            if self._tools.actions is not None
            else []
        )
        splitter = SentenceSplitter()
        started = time.monotonic()
        first_say: list[float] = []
        tool_times: list[tuple[str, float]] = []
        round_ms: list[int] = []  # each model round, to its first words or its tool calls
        mark = [time.monotonic()]
        context = await self._tools.data.context() if self._tools.data is not None else None
        foreign_in = getattr(self._tools.data, "foreign_in", None)  # today's invites' titles
        context_foreign = bool(context) and foreign_in is not None and await foreign_in(context)
        # where the user is looking from, and Pisa's open threads: "typed" when they typed
        body = await self._body(device_id, typed=not spoken)
        phone_locked = await self.phone_locked(device_id)  # read-only while locked
        if phone_locked:
            body += locked_rules.instruction(lang)
        latest = await self._latest_question(device_id, turns)
        about_owner = ""
        taste: list[str] = []  # what a places card's `fits` may name
        avoid: list[str] = []  # … and what it may name only with a negation
        said = [
            t.said if t.said is not None else t.content
            for t in turns
            if t.role == "user" and not drawing_rules.ASK_MARK.match(t.content)  # no label
        ]
        # the same talk's earlier turns, never one the user took back since ("no, aspetta", "lascia
        # stare"), and none when these words take back
        cut = max((i + 1 for i, words in enumerate(said) if _withdraws(words)), default=0)
        earlier = () if _withdraws(text) else tuple(said[cut:])[-EARLIER_TURNS:]
        turn.earlier = earlier  # their words earlier in the talk, for the calendar's too
        last_reply = next((t.content for t in reversed(turns) if t.role == "assistant"), None)
        profile_turn = ProfileTurn(
            words=turn.words, device=device_id, lang=lang, earlier=earlier, turn_id=trace_id,
            recent=() if _withdraws(text) else tuple(said[cut:])[-owner_scope.RECENT_OWNER_TURNS :],
            offered=owner_scope.offered_reminder(last_reply),
        )  # fmt: skip
        if self.deps.profile is not None:  # what the user told Pisa, in every turn
            if not tapped:  # a drawing's label tells Pisa nothing about them
                await self.deps.profile.turn_started(text, device_id)
            core = await self.deps.profile.core_profile()
            about_owner = profile_instruction(core)
            taste, avoid = await self.deps.profile.taste()
            talk = await self.deps.profile.talk()
            profile_turn.talk_active = talk.get("state") == "in_progress" and not talk.get("later")
            about_owner += talk_instruction(talk)
        # their standing preferences (Memoria), read whole every turn after the profile, and whether they keep
        # notes (find_note offered at once)
        preferences = ""
        if memoria is not None:
            profile_turn.notes = kept_notes
            preferences = instructions_block(standing, profile_turn.notes)
        # the shared conversation state (the reflex's reads on the partial words, its ready answer,
        # Pisa's voice lines, its promises, the ledger), after the cached prefix: the main thinker's head
        # start
        shared = ""
        reflex_outside: list[str] = []  # the reflex's outside facts, marked after watch_turn
        lists_read_by_code: list[str] = []  # the reflex's reads of their lists for these words
        if self.deps.reflex is not None:
            await self.deps.reflex.settle(device_id, text)
            state = self.deps.reflex.state(device_id)
            state.begin(str(conversation_id), await self._live_promises())
            state.set_waiting([str(r["summary"]) for r in waiting if not r.get("expired")])
            shared = state.for_brain(self._clock(), text, wrap=_own_data, wrap_outside=_outside_data)
            reflex_outside = [f.text for f in state.fresh_facts(self._clock()) if f.outside]
            # only a read for these words: one the reflex made for earlier words (another question, minutes
            # ago, with their lists changed since) is no read of this turn
            lists_read_by_code = [
                f.text
                for f in state.fresh_facts(self._clock())
                if f.read in claim_rules.CODE_LIST_READS and follows(f.words, text)
            ]
            watch.head = bool(state.facts) and "Pisa's own data, read for these words" in shared
            state.brain_read()
        # the skills for these words (and the night's), after the cached prefix
        local_hour = self._owner.local(self._clock()).hour
        plain = await self._words_only(text, tapped)  # "disegna meno" holds
        # the conversation's last drawing (a redraw), and the drawing skill's pin
        kept_drawing = await self._kept_drawing(conversation_id)
        draws = (
            not plain
            and drawing_box_on()
            and (  # the box off (until the device checks): no drawing skill either
                tapped
                or drawing_rules.asks(text)
                or (kept_drawing is not None and drawing_rules.redraws(text))
            )
        )
        # the blocks' skill (box on or off: blocks are data) joins a turn whose words ask to
        # see a structure, a flow or numbers, and a tap on a drawing; never while they want words only
        blocks = not plain and (tapped or visual_rules.asks(text))
        skills_found = await self._skills.find(text, device_id, local_hour, drawing=draws, blocks=blocks)
        if skills_found:
            _log.debug("skills", trace_id=trace_id, names=[s.name for _, s in skills_found])
        since = await self._different_now() if places_card is not None else None
        different = since is not None
        if different:
            about_owner += DIFFERENT_LINE.format(since=since)
        context_s = time.monotonic() - started

        offer_asked: list[str] = []  # a tool's result asked Pisa to offer a starter skill (its name)
        now_time: list[str] = []  # the time the "now" tool told the model ("18:40")
        offers: list[str] = []  # an offer question streamed last, held to the end
        # a closing offer an earlier round held: closing for that round only. Another round's words never
        # follow it (quality audit §22 m1: "Vuoi che ne metta una nel piano?  Oggi hai tre cose…", an
        # orphan question, or glued between two answers); said at the end only when no later round said
        # anything (`stale_at`: the sentences said when it was set aside)
        stale_offers: list[str] = []
        stale_at = [0]
        # RAIL: what the model wrote before calling a tool in the same response (its questions, its last
        # words not yet said): the next round answers from the result, so they never open or join that
        # answer (quality audit §23 m1: "Te la metto nel piano? Oggi non hai eventi."); said only when no
        # later round says anything (`before_at`: the sentences said when they were set aside)
        before_tools: list[str] = []
        before_at = [0]
        # a spoken turn streams each round as it comes (A1: the owner's first complaint was slow answers on
        # the phone); what earlier rounds said aloud, so a later round never says it again (§32 D2)
        earlier_rounds: list[str] = []
        # typed words wait for each round's end: a round that went on to a tool is said only when no later
        # round says anything (`before_tools`); spoken words (economy and a talk) stream as they come
        streams = voice or (spoken and origin != "drawing")
        # … under a standing "rispondimi breve", a round's statements written before a tool it went on to:
        # said with the next round's words, the cut choosing among them (`round_end`)
        carried: list[str] = []
        # … without the cap, a round's last words (no space after them) written before a tool, a statement on
        # the turn's subject (their words, a tool's result): said before the next sentence of the reply,
        # unless that sentence says the same; at the end when nothing follows (an answer written beside the
        # tool was dropped when the next round said something else)
        last_words: list[str] = []
        replied = [False]  # the model's reply (its text with finish) was said this turn

        # each tool call by its id while it runs (name, start, target), its index in turn.calls, the ids seen
        calls: dict[str, tuple[str, float, str]] = {}
        call_index: dict[str, int] = {}
        seen_calls: set[str] = set()

        async def record_call(part: ToolCallPart) -> None:
            """A tool call into the turn's record (what it called, with what), once."""
            if part.tool_call_id in seen_calls:
                return
            seen_calls.add(part.tool_call_id)
            args = part.args_as_dict()
            calls[part.tool_call_id] = (part.tool_name, time.monotonic(), _target(args, part.tool_name))
            turn.tools.append(part.tool_name)
            turn.calls.append((part.tool_name, args))
            call_index[part.tool_call_id] = len(turn.calls) - 1
            watch.tool = part.tool_name
            turn.loop.research = turn.loop.research or part.tool_name in RESEARCH_TOOLS
            await _step(steps, "started", part.tool_call_id, part.tool_name)

        async def record_result(tool_call_id: str, part: Any) -> None:
            """A tool's result into the turn's record (what it returned, what failed or was done)."""
            if tool_call_id not in calls:
                return
            name, at, target = calls.pop(tool_call_id)
            await _step(steps, "finished", tool_call_id, name)
            if isinstance(part, RetryPromptPart) and "unknown tool" in str(part.content).lower():
                turn.refused.append(call_index.pop(tool_call_id, -1))
            tool_times.append((name, time.monotonic() - at))
            # Pisa's record of what wasn't done, for the truth said in place of a claim;
            # per call target ("move A" failed stays failed when "move B" is done)
            returned = str(part.content) if isinstance(part, ToolReturnPart) else ""
            said_back.append(" ".join(returned.split()))  # what a sentence may echo
            if name == "remember_to" and isinstance(part, ToolReturnPart):  # its own result, by its call
                reminders_set.append(" ".join(returned.split()))
            if name == FINISH and returned == NOT_READY and (n := call_index.get(tool_call_id)) is not None:
                # RAIL: finish beside a read: its sentence, written before the read, is held like any words
                # written before a tool, said only when no later round says anything (quality audit §24 m1:
                # get_tasks, get_agenda and finish, then nothing: the reply was empty or the no-read line)
                say = str(turn.calls[n][1].get("say") or "").strip()
                if say and not say.endswith("?"):
                    if not before_tools:
                        before_at[0] = len(sent)
                    before_tools.append(say + " ")
            if name in TRACED:  # the outcome's kind, for the turn's trace (never its titles)
                traced.append((name, outcome_of(returned)))
            if name == "what_now" and (told := form.time_told(returned)):
                now_time.append(told)
            if form.asks_to_offer(returned, OFFER_ONCE):  # Pisa's own words only
                offer_asked.append(form.offered_name(returned))
            if name in claim_rules.PROPOSES and (planned := _PLANNED.search(returned)) is not None:
                turn.proposals.append((planned.group(1), planned.group(2)))  # proposed, never done
            elif name not in results.WRITE_TOOLS:
                pass  # a read, `finish` or a show changes nothing: never a failed change
            elif (failed := claim_rules.failure(name, returned)) is not None:
                _settle(turn.failures, name, target, failed)
            elif isinstance(part, ToolReturnPart):
                _settle(turn.failures, name, target, None)  # the same change, done on a retry
                if results.unverified(returned):  # ran, not confirmed
                    turn.unverified.add(name)
                elif results.status_of(returned) == results.DONE:
                    turn.done_tools.add(name)

        async def record_unstreamed(messages: list[Any]) -> None:
            """RAIL: the calls of a response that wrote text and called tools. The stream run ends at its text
            (pydantic-ai's `run_stream`) and runs those tools without their events, so the record missed
            them: a read the turn did was taken for none, and the answer it backs for one from memory
            (quality audit §23 m1, "Cosa devo fare oggi?" with get_tasks written beside an offer)."""
            response = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
            if response is None:
                return
            for part in response.parts:
                if isinstance(part, ToolCallPart) and part.tool_call_id not in seen_calls:
                    await record_call(part)
                    for m in messages[messages.index(response) + 1 :]:
                        for got in getattr(m, "parts", []):
                            if isinstance(got, ToolReturnPart | RetryPromptPart) and (
                                got.tool_call_id == part.tool_call_id
                            ):
                                await record_result(part.tool_call_id, got)

        async def on_events(_: Any, events: AsyncIterable[Any]) -> None:
            tools_node = False
            async for event in events:
                tools_node = tools_node or isinstance(event, FunctionToolCallEvent | FunctionToolResultEvent)
                if isinstance(event, FunctionToolCallEvent):
                    await record_call(event.part)
                elif isinstance(event, FunctionToolResultEvent):
                    await record_result(event.tool_call_id, event.part)
            # a model round lasts from the previous node's end to its first words or its tool calls
            now = time.monotonic()
            if not tools_node:
                round_ms.append(int((now - mark[0]) * 1000))
            mark[0] = now

        async def say_now(line: str) -> None:
            """A long research turn says so at once ("Cerco, un attimo"), before the answer."""
            if not sent and not first_say and watch.held is None:  # a holding phrase already said it
                first_say.append(time.monotonic() - started)
                await emit(line, False)
                sent.append(line + " ")

        earlier = self._previous.get(device_id)
        # a find, recommend or compare request has the research skill from its
        # first request (with its tools, `tool_choice`); a follow-up to a research turn too; the rest once a
        # research tool is called (build_agent)
        guide = self._research_follow_up(earlier) or research_rules.asks(text)
        prompt_chars = (
            len(system_prompt(self._owner.tag))
            + len(RESEARCH_GUIDE if guide else "")
            + len(prompt)
            + history_chars(past)
        )

        kept_evidence: list[str] = []  # the kept findings' text, filled once they are read
        # what this turn shows, on this device; an app without `present` gets today's cards.
        presenter = Presenter(
            device=device, words="" if tapped else text, lang=lang, today=self._today(),
            objects=self.objects.setdefault(device_id, {}), reply_to=reply_to,
            cards=self._cards(device, card), noted=lambda: noted, facts=lambda: research.places,
            sources=lambda: research.sources(), pages=lambda: research.sources(read_only=True),
            page_texts=lambda: list(research.pages.values()), results=lambda: drawing.reads(),
            # the kept findings' evidence counts too
            corpus=lambda: [*drawing.corpus(), *kept_evidence], plain=plain,
            clock=self._clock, stored_options=self._stored_options,
            has_plan=self._has_plan, loop=turn.loop,
        )  # fmt: skip

        async def show_places(card: dict[str, Any], focus: str, purpose: str) -> str:
            """The places object (or card), then its outcome log (S3/S7): shown, and a critique of the
            last one."""
            card_id = uuid.uuid4()

            async def fallback() -> bool:
                return places_card is not None and await places_card(card | {"card_id": str(card_id)})

            data = {"title": card["title"], "request": card["request"], "places": card["places"]}
            unusual = "something different" if any(p.get("different") for p in card["places"]) else None
            ack = await presenter.show(
                "show_places", "places", f"places:{card_id}", purpose, cast(Focus, focus), data=data,
                fallback=fallback, rail=unusual,
            )  # fmt: skip
            if ack.status in TERMINAL:
                # A critique: never sensitive, never an off-limits topic.
                allowed = self.deps.profile is not None and await self.deps.profile.critique_allowed(text)
                async with self._pool.connection() as conn:
                    await outcomes.shown(
                        conn, card_id=card_id, places=[p["place_id"] for p in card["places"]],
                        device_id=device_id, words=text, now=self._clock(), critique_allowed=allowed,
                    )  # fmt: skip
            return ack.status

        async def show_findings_sheet(data: dict[str, Any], focus: str, purpose: str) -> str:
            """The research answer's sheet on the phone (the table, why, sources)."""
            ack = await presenter.show(
                "show_findings", "sheet", f"sheet:{uuid.uuid4()}", purpose, cast(Focus, focus), data=data
            )
            return ack.status

        def answered_on_screen() -> bool:
            """This turn's complete answer is on the phone (show_findings, shown)."""
            return research.findings is not None and research.shown

        lead_from = [0]  # where the lead starts: what was said once the answer was on the screen
        early: list[str] = []  # a pick's words before its answer reached the screen

        def own_words() -> list[str]:
            """The lead's sentences said so far: since the answer went on the screen (an interim line before
            it never counts), without Pisa's own "Cerco, un attimo"."""
            return [x for x in sent[lead_from[0] :] if x.strip() and x.strip() not in WORKING.values()]

        def answer_shown() -> None:
            lead_from[0] = len(sent)

        drawing = DrawingTurn(
            lang=lang, words=turn.words, origin=origin, plain=plain,
            names=lambda: [str(f.get("name") or "") for f in research.places.values()],
            kept=kept_drawing[0] if kept_drawing else None, kept_at=kept_drawing[1] if kept_drawing else "",
        )  # fmt: skip
        earlier_findings = await self._kept_findings(conversation_id)  # the last research's
        if earlier_findings:  # a fact the last research read is still a sourced one
            # never Pisa's own words
            kept_evidence.append(findings_rules.sourced(earlier_findings[0]))
        research = ResearchTurn(
            lang=lang, prompt_chars=prompt_chars, say_now=say_now,
            show=show_places, said="" if tapped else action_rules.without_label(text),
            taste=taste, avoid=avoid, loop=turn.loop,
            different_allowed=different, researched=guide,
            on_doing=lambda line: setattr(watch, "doing", line),
            save_menu=self._tools.places.save_menu if self._tools.places is not None else None,
            show_sheet=show_findings_sheet,
            on_shown=answer_shown,
            earlier=earlier_findings[0] if earlier_findings else None,
            earlier_at=earlier_findings[1] if earlier_findings else "",
            pick=research_rules.asks(text),
            local_now=lambda: self._owner.local(self._clock()),
        )  # fmt: skip
        if research.pick:  # their situation before ranking: the time, their next event
            research.now, research.event = await self._situation()
        # who is with them, from their own words (this turn's, the research's request)
        # `research.said` is their words (never a tap's label, never a drawing's label)
        asked_first = str((earlier_findings[0] if earlier_findings else {}).get("request") or "")
        research.company = research_rules.company(" ".join([research.said, asked_first]))
        overflow: list[str] = []  # what a short spoken answer puts on the screen
        misses: list[tuple[str, str]] = []  # Pisa's own notes, held back to be rewritten
        rewrote: list[bool] = []  # … the one rewrite round, once it ran
        corrected: list[str] = []  # a correction is said once, in one line
        # a "no" or an estimate by eye before any web search, held for one
        # search round (SEARCH_LINE); said as it was only when that round says nothing
        unlooked: list[tuple[str, str]] = []
        # "already there", "not on your list", "nothing matches" with no read of their lists this
        # turn, held for one read round (READ_LINE)
        unread_lists: list[tuple[str, str]] = []
        unread_at: dict[str, int] = {}  # … each one's place: the sentences said when it was set aside
        said_back: list[str] = []  # this turn's tool results, their spacing folded (a sentence echoing one)
        reminders_set: list[str] = []  # remember_to's own results this turn, never another tool's (E1 RM-1)
        # … and on its own topic: their words, or Pisa's answer just before (the one a follow-up may correct)
        previous_reply = next((t.content for t in reversed(turns) if t.role == "assistant"), "")

        def research_talk() -> bool:
            """A research conversation's turn (a find or a follow-up, or research called) that changes nothing
            and asks no change: Pisa's "nothing changed" notes are no news there."""
            return (guide or bool(research.calls)) and asked_nothing()

        def asked_nothing() -> bool:
            """A turn that changes nothing and asks no change: "nothing changed" is no news there.
            every such turn, not only a research conversation's; the claim rail's truth line goes back to the
            model."""
            return (
                not action_rules.commands(text)
                and not claim_rules.asks_act(text)  # "mi ricordi…?", "remind me", "scrivi a…"
                and not claim_rules.asks_what_was_done(text)
                and not turn.approvals
                and not turn.failures
                and not turn.proposals
                and not turn.done_tools
                and not set(turn.tools) & results.WRITE_TOOLS
            )

        def known_names() -> str:
            """What a name Pisa says may come from: this turn's results and pages, the
            kept findings, their words, their own situation (their next event, who is with them, their
            tastes)."""
            facts = [str(v) for f in research.places.values() for v in f.values() if isinstance(v, str)]
            situation = [str(research.event[0])] if research.event else []
            return research_rules.plain(" ".join([
                *drawing.corpus(), *kept_evidence, *research.pages.values(), *research.snippets.values(),
                *research.reviews.values(), *research.urls.values(), *facts, *situation, *research.company,
                *research.taste, text, research.said,
            ]))  # fmt: skip

        def own_line(sentence: str) -> str | None:
            """Why a sentence never reaches them (None: it may): "" for a stray correction (left
            out at once), else why it is Pisa's own note (rewritten once)."""
            if (why := claim_rules.internal(sentence, asked_change=not research_talk())) is not None:
                return why
            if research.calls and (names := research_rules.misnamed(sentence, known_names())):
                # a name said otherwise than any result wrote it
                return f"names {', '.join(names)} as no result wrote it: say each name as a result wrote it"
            heard = " ".join([*drawing.corpus(), *kept_evidence, *research.pages.values(),
                              *research.snippets.values(), *research.reviews.values()])  # fmt: skip
            if claim_rules.stray_correction(sentence, text, heard, previous_reply) is not None or (
                claim_rules.corrects(sentence) and corrected
            ):
                _log.info("correction_left_out", trace_id=trace_id)
                return ""
            if claim_rules.corrects(sentence):
                corrected.append(sentence)
            return None

        asks_day = not tapped and claim_rules.asks_day(text)  # their day asked: the answer is their lists
        # where something is in the app asked (§27 a1): naming a tab says nothing of what is on their lists
        app_nav = claim_rules.asks_where_in_app(text)
        # a talk's reply is said as it is: what a rail corrects in it is one line of its own after it
        # (`claim_rules.correction`), never spliced in; the clauses of that line
        apart: list[str] = []

        async def grounded(sentence: str) -> str | None:
            """RAIL: what to say instead of a sentence about a task that the turn's record or the store
            contradicts (None: it may be said): "Non ci sono riuscito" after the task was closed in this turn,
            "il latte è ancora da comprare" with it closed in the store."""
            if claim_rules.false_failure(sentence, turn.record()):
                closed = [m.group(1) for r in said_back for m in _CLOSED.finditer(r)]
                return claim_rules.worked_truth(lang, closed[-1:])
            if (
                claim_rules.said_state(sentence) is not None
                and (wrong := claim_rules.wrong_state(sentence, await self._task_states())) is not None
            ):
                return claim_rules.state_truth(lang, *wrong)
            return None

        def unread(sentence: str) -> str | None:
            """RAIL: why a sentence that refers to their lists waits for a read of
            them (None: a list was read or written this turn, `LIST_TOUCH`, or it refers to none). A refused
            call touched nothing. A false positive costs one read round."""
            read = bool(lists_read_by_code) or any(
                name in claim_rules.LIST_TOUCH and n not in turn.refused
                for n, (name, _) in enumerate(turn.calls)
            )
            # narrowed: a sentence that is a piece of this turn's own tool results is said from the record,
            # never from memory (a plan's "task: …" line, a result's own words)
            folded = " ".join(sentence.split())
            if folded and any(folded in r for r in said_back):
                return None
            return claim_rules.unread_state(
                sentence, read, " ".join(said_back), day=asks_day, app_nav=app_nav
            )

        def read_since() -> None:
            """RAIL: a read that happened in the turn counts before any no-read line decides, however it went
            on the record (its events, or `record_unstreamed`). A sentence held for a read the turn then made
            (the read round, or a later round of the same run) is a sentence written before a read: said only
            when no later round says anything (`before_tools`), never replaced by "Non ho riletto…" (quality
            audit §24 m1: get_tasks and get_agenda, with finish, and nothing written after them)."""
            now_read = [one for one, _ in unread_lists if unread(one) is None]
            if not now_read:
                return
            unread_lists[:] = [x for x in unread_lists if x[0] not in now_read]
            at = min(unread_at.get(one, len(sent)) for one in now_read)
            before_at[0] = min(before_at[0], at) if before_tools else at
            before_tools[:0] = [one + " " for one in now_read]  # written first, said first

        def short_talk() -> bool:
            """This turn's spoken answer is a research conversation's (their words ask
            to find, recommend or compare, or follow up a research turn: `guide`; a plain place fact, "dov'è
            da Mario?", is none), not yet the sheet's lead, and holds no change for their yes (asked whole).
            the profile's consent question doesn't lift the rule (a follow-up "mi piace di più la
            seconda" that notes a taste was read aloud whole): code says its question, whole, after it.
            a quick request or a choice among things to buy too; a turn that is no pick and
            calls a tool beyond looking up and showing (their agenda, their mail, their tasks) is not cut."""
            if answered_on_screen() or turn.approvals:
                return False
            if research.pick or style.words is not None:  # a standing "rispondimi breve" cuts every answer
                return True
            return (guide or short_asked) and not set(turn.tools) - research_rules.LOOKUPS

        short_asked = research_rules.short_ask(research.said)
        # the short answer's caps: the research lead's, or the ones their standing instruction sets
        caps: dict[str, int] = {
            "word_cap": style.words or research_rules.ANSWER_WORDS,
            "sentence_cap": style.sentences or (research_rules.LEAD_SENTENCES if style.words is None else 99),
        }

        async def show_rest(rest: list[str]) -> bool:
            """The details a short answer left unsaid, as a note on the phone:
            a rest longer than the note holds goes on a sheet of text blocks, never dropped."""
            lines: list[str] = []
            # short pieces (a streamed list, one item a line) share a line, so the note's six lines
            # hold up to 1,200 characters of the rest, never six list items and the price dropped after them
            for one in (" ".join(x.split()) for x in rest if x.strip()):
                if lines and len(lines[-1]) + 1 + len(one) <= REST_LINE:
                    lines[-1] = f"{lines[-1]} {one}"
                    continue
                while one:
                    cut = (
                        one if len(one) <= REST_LINE else one[:REST_LINE].rsplit(" ", 1)[0] or one[:REST_LINE]
                    )
                    lines.append(cut)
                    one = one[len(cut) :].strip()
            if not lines:
                return False
            title = REST_TITLE.get(lang, REST_TITLE["en-US"])
            links = research.sources()[:6]
            if len(lines) > REST_LINES:
                blocks = [{"type": "text", "text": x} for x in lines][: sheet_rules.SHEET_BLOCKS]
                sheet: dict[str, Any] = {
                    "title": title,
                    "blocks": blocks,
                    **({"sources": links} if links else {}),
                }
                ack = await presenter.show(
                    "show_note", "sheet", f"sheet:{uuid.uuid4()}", title, "offer", data=sheet
                )
                return ack.status in TERMINAL
            data: dict[str, Any] = {"title": title, "lines": lines, "kind": "info"}
            if links:
                data["sources"] = links
            note = presenter.cards.note

            async def fallback() -> bool:
                return note is not None and await note(title, lines, "info")

            ack = await presenter.show(
                "show_note", "note", f"note:{uuid.uuid4()}", title, "offer", data=data, fallback=fallback
            )
            return ack.status in TERMINAL

        # Pisa's traits they approved (their yes only), read into the instructions' fixed part
        character = await self.deps.perceive.approved() if self.deps.perceive is not None else []
        turn.character = tuple(character)
        folded_items: list[list[tuple[str, bool, list[str]]]] = []
        turn_read: list[Taint] = []  # what each run of this turn read (the last run's last)
        parts: list[tuple[str, str]] = []  # the last run's dynamic instructions, by part
        traced: list[tuple[str, str]] = []  # the kind of each traced tool's result (`TRACED`)
        read_from: list[int] = []  # where each run's own outside text starts (after what Pisa retold)

        turn.loop.lang = lang
        # off means off: a connector switched off feeds no tool this turn
        # an experimental feature that is off: its single tools are never offered (its families: `families`)
        enabled = self.deps.enabled
        withheld: frozenset[str] = experimental.tools_off(enabled)
        # a client is offered only the phone actions whose extension its hello granted (a button it can show)
        withheld |= tool_registry.phone_acts_withheld(granted)
        # what no maps or web provider can do yet: the model says so in one line (`UNSET_LINE`)
        unset = tuple(await self.deps.places_unset()) if self.deps.places_unset is not None else ()
        unconnected: dict[str, str] = {}  # tool -> the connector they haven't connected
        states: dict[str, Any] = {}
        if self.deps.connectors is not None:
            states = (
                await self.deps.connectors.states()
            )  # decided once, as the connectors service decides them
            withheld |= connector_rules.tools_off(states)
            unconnected = connector_rules.tools_unconnected(states)
        # a skill's tools a supported connector would feed once connected still count as there (its skill
        # stays: "connect Google" is the honest answer); only an off feature's tools are gone
        could_connect = connector_rules.tools_off(states) - connector_rules.tools_off(
            {c: "on" for c in experimental.CONNECTORS if experimental.connector_on(c, enabled)}
        )
        # RAIL: what this install can't do is never said as something Pisa can (`abilities_rules.overclaim`)
        google_on = self.deps.connectors is None or states.get("google") in (
            "on",
            "error",
        )  # unknown: no hold
        phone = experimental.on("phone_actions", enabled)
        can_do = abilities_rules.Facts(
            google=google_on and experimental.on("google", enabled),
            messages=phone and phone_senders[0] is not None,
            calls=phone and phone_senders[1] is not None,
            off=abilities_rules.hidden(lambda feature: experimental.on(feature, enabled)),
            # a feature off here is never sent to Settings for, nor offered to set up ("Collegamenti… banca")
            unavailable=frozenset(f.id for f in experimental.REGISTRY if not experimental.on(f.id, enabled)),
            asked_off=tuple(experimental.named_off(text, enabled)),
        )
        overclaimed: list[str] = []  # what a held sentence claimed (the turn ends with the honest line)
        reminders_on = "reminders" not in can_do.off

        def promised(sentence: str) -> str | None:
            """What the sentence promises that this turn's writes or screen must back: a reminder or "I'll
            take care of it" for later (reminders on; off, `overclaim` holds it), or a button to tap."""
            if reminders_on and abilities_rules.promises_later(sentence):
                return "promise"
            if abilities_rules.says_tap(sentence):
                return "tap"
            return None

        def unkept(sentence: str) -> str | None:
            """RAIL: what the sentence promises that the turn, as it ends, doesn't back (None: backed, or
            nothing promised). A promise for later needs a write that fires at its time and succeeded
            (remember_to, keep_intention): a task on the list, timed or not, sends no alert. "Tap Send"
            needs a button this turn put on the phone (a handoff's sheet shown, the compose screen, the call
            offered)."""
            what = promised(sentence)
            if what is not None and not _backed(sentence, turn, lang):
                return None  # a claimed change with none made: the claim rail's truth replaces it
            if what == "promise":
                # backed by a write that succeeded this turn (its result's `done` line, `turn.done_tools`),
                # never by a call: "Not set: …" backs nothing; a clock time it names is the one the
                # reminder was set for
                writes = abilities_rules.PROMISE_WRITES & turn.done_tools
                if not writes:
                    return what
                if writes == {"remember_to"} and not abilities_rules.promised_time_set(
                    sentence, reminders_set, self._clock(), self._owner.zone
                ):
                    return what
                # set, but it can't reach their phone at its time: never "te lo ricordo alle …"
                if "remember_to" in writes and abilities_rules.promised_unreached(sentence, reminders_set):
                    return "promise_unreached"
            if what == "tap" and not any(any(mark in r for mark in HANDED_OFF) for r in said_back):
                return what
            return None

        rail_only = False  # the rail's line is the whole reply: a lone "Dunque…" said before it goes

        turn.loop.answer_now = lambda: research.searched_past_stop

        tools = families(self.deps.tools, enabled)
        if tapped:  # only their own utterance writes their Memoria (a tap on a drawing's label never)
            tools["memoria"] = None

        async def call(ctx: CallContext) -> None:
            research.grow = ctx.grow
            models_used.append(ctx.model)
            agent = build_agent(
                Loop(ctx.model, turn.loop),  # the loop; finish(say=…) said at once when all is done
                TurnContext(
                    **tools,
                    owner=self._owner,
                    clock=self._clock,
                    note_place=lambda place: noted.__setitem__(place.place_id, place),
                    weather=weather_card,
                    directions=directions,
                    compose=compose,
                    call=call_phone,
                    research=research,
                    profile_turn=profile_turn,
                    decided=self._recent_moves,
                    screen=lambda: presenter,
                    app_map=app_map,
                    app_action=app_action,
                    voice=voice,
                    locked=lambda: phone_locked,
                    team=lambda words: self._note_for_architect(device_id, words),
                    character=character,
                    withheld=withheld,
                    unconnected=unconnected,
                    unset=unset,
                    enabled=enabled,
                    drawing=drawing,
                    loop=turn.loop,  # `finish` and the shows write to the turn's loop
                ),
            )
            # RAIL: a skill whose every tool this turn lacks (an off feature's) is never guidance
            have = {n for t in agent.toolsets if isinstance(t, FunctionToolset) for n in t.tools}
            followed = skills_rules.offered(skills_found, have | could_connect)
            if dropped := sorted({s.name for _, s in skills_found} - {s.name for _, s in followed}):
                _log.debug("skills_without_tools", trace_id=trace_id, names=dropped)
            how_pisa_does_it = skills_block(followed, self._owner.tag)
            try:
                # From here any untrusted block marks the turn, the day's context too (event titles can come
                # from anyone's invite: verification): profile writes then wait for the user's yes.
                taint = watch_turn(self._owner.day(self._clock()))
                turn_read.append(taint)
                taint.read_past(history.outside_kinds(past))  # outside text restored is read now
                taint.retold(history.outside_texts(past))  # and its words are outside text (owner_scope)
                # the outside values this conversation read still taint (stored)
                taint.restore_values(await self._kept_values(conversation_id))
                taint.read_past([k for t in turns for k in t.outside])  # read by a turn still in the history
                taint.retold([t.content for t in turns if t.outside])  # what Pisa retold of it
                if tapped and (
                    tap is None or tap.outside
                ):  # a label outside text drew is outside text (the box's always: its values may come from a
                    # page; and a tap with no server copy at hand, which can't be told apart)
                    taint.read("drawing-label" if tap is None else "sheet-tapped")
                    taint.retold([tap.label if tap is not None else text])
                if reflex_outside:  # `shared` was built before this turn's watch began
                    taint.read("invite")
                    taint.retold(reflex_outside)
                read_from.append(len(taint.texts()))  # this run's own reads start here
                # what attend folded into this talk, after watch_turn: an email's words in it
                # mark the turn as having read outside text.
                if not folded_items:  # read once a turn: a fallback's rerun says them too
                    folded_items.append(await self._folded_items(self._clock()))
                folded = self._folded(folded_items[0])
                # Dynamic: the turn's system prompt, after its fixed part, the same bytes on every request
                # of the turn (`pisa.brain.prefix`).
                # each part named, for the turn's trace (`trace_turn`: names and lengths, never the text)
                parts[:] = [
                    ("language", language_instruction(lang)),
                    ("clock", clock_line(self._clock(), self._owner.tz)),
                    ("body", body),
                    ("folded", folded),
                    ("context", context_instruction(context, foreign=context_foreign)),
                    ("shared", shared),
                    ("about_owner", about_owner),
                    ("skills", how_pisa_does_it),
                    ("done", done_line(turn)),
                    ("ledger", ledger_line(waiting, changed, self._owner.tz)),
                    ("doubt", doubt_instruction(doubt)),
                    # their standing preferences near the end, where they carry weight (quality audit §22
                    # m1: in the middle, "rispondimi breve" was saved and not followed); only the turn's own
                    # lines follow, and Pisa's latest question stays last, the one a bare "sì" answers
                    ("preferences", preferences),
                    ("asked", asked_instruction(latest)),
                    ("handed", handed_instruction(text)),
                    ("guard", guard_instruction(guard)),
                ]
                instructions = InstructionPart("".join(part for _, part in parts), dynamic=True)
                done_usage = RunUsage()  # the finished runs' usage (a follow-up run adds its own)

                async def take(raw: str) -> None:
                    """One streamed sentence through the rails, in order."""
                    if last_words and raw.strip():  # a round's last words, held: said first, once
                        pending = list(last_words)
                        last_words.clear()
                        if not form.said_before(raw, pending) and not form.near_duplicate(raw, pending):
                            for one in pending:
                                await take(one)
                        else:
                            _log.info("last_words_said_later", trace_id=trace_id)
                    if replied[0] and not form.adds(raw, sent, held, ask=bool(turn.approvals)):
                        return  # one reply, once: a reply already said is never said again
                    # no filler opening, no greeting but the day's first, no
                    # closing "C'è altro?".
                    # form filters on each streamed sentence (no filler, no
                    # closing question).
                    sentence = form.keep(raw, first=not sent, greet=greet)
                    if not sentence:
                        return
                    if answered_on_screen():
                        if research.lead or research_rules.past_lead(sentence, own_words()):
                            # the screen has the answer; the words are a lead.
                            # Pisa says the sheet's lead itself, the model's words go
                            return
                    elif research.pick and not research.shown:
                        # on a pick, the model's words before the
                        # answer is on the screen wait; said at the end only if nothing went on the screen
                        early.append(raw)
                        return
                    if research.calls:  # a clause naming what no result wrote goes
                        sentence = research_rules.without_misnamed(sentence, known_names()) or sentence
                    if (left := own_line(sentence)) is not None:
                        # Pisa's own note or a stray correction never reaches them; a
                        # note goes back to the model to rewrite, once (REWRITE_LINE)
                        if left and not rewrote:
                            misses.append((sentence.strip(), left))
                        return
                    if (why := unread(sentence)) is not None:
                        # their lists, stated from memory: read first (once); after that round, never said
                        if not rewrote:
                            unread_lists.append((sentence.strip(), why))
                            unread_at.setdefault(sentence.strip(), len(sent))
                        else:
                            _log.info("unread_state_left_out", trace_id=trace_id)
                        return
                    if (truth := await grounded(sentence)) is not None:
                        # the task's state from the record and the store, never from memory
                        _log.info("task_claim_grounded", trace_id=trace_id)
                        turn.unbacked += 1
                        if voice:
                            apart.append(claim_rules.as_clause(truth))
                            return
                        sentence = truth
                    if (claimed := abilities_rules.overclaim(sentence, can_do)) is not None:
                        _log.info("ability_overclaim_held", what=claimed, trace_id=trace_id)
                        overclaimed.append(claimed)
                        return
                    if overclaimed and abilities_rules.asks_about_it(sentence):
                        # "Want that?" / "Ti va?" / "Lo preparo?" after a held offer: about it (§14)
                        return
                    if not rewrote and (
                        why := research_rules.unsearched(
                            sentence, text, "web_search" in turn.tools or research.web_tried
                        )
                    ):
                        unlooked.append((sentence.strip(), why))
                        return
                    if not sent:  # "hai sbagliato…": owned first
                        sentence = form.owned(sentence, text, lang)
                    if form.said_before(sentence, sent) or form.near_duplicate(sentence, sent):  # never twice
                        return
                    own_said = [x for x in sent if x.strip() not in WORKING.values()]
                    if form.small_talk_again(sentence, own_said):  # one greeting, one "sto bene", one "e tu?"
                        return
                    if own_said:  # a greeting opens the reply, never its middle (§32 D2)
                        sentence = form.without_greeting(sentence)
                    if said_earlier and form.repeats(sentence, said_earlier):  # nor after a continuation
                        return
                    if streams and earlier_rounds and form.repeats(sentence, earlier_rounds):
                        return  # said aloud by an earlier round of this turn: never again (§32 D2)
                    if sentence.rstrip().endswith("?") and form.repeats(
                        sentence, [x for x in sent if x.rstrip().endswith("?")]
                    ):  # one offer once ("Vuoi che gli trovi un momento?" … "Vuoi che lo metta oggi?")
                        return
                    if text.rstrip().endswith("?") and form.closing_offer(sentence):
                        offers.append(sentence)  # said only if something follows it
                        return
                    for offer in offers:  # not the closing one: said, in its place
                        working = frozenset(WORKING.values())
                        if short_talk() and not research_rules.brief(offer, sent, working, **caps)[0]:
                            continue  # an offer mid-answer keeps the two sentences too
                        await emit(offer, False)
                        sent.append(offer)
                    offers.clear()
                    if profile_turn.asked and sentence.rstrip().endswith("?"):
                        return  # code asks the one question, in its exact words
                    if hand_off and not turn.approvals and sentence.rstrip().endswith("?"):
                        return  # what they hand Pisa to keep is kept, not asked about
                    if ON_SCREEN.search(sentence):  # said once the phone has it
                        sentence, on_phone = split_screen_claim(sentence)  # only its clause
                        screen_claims.append(on_phone)
                        if not sentence:
                            return
                    if short_talk() and overflow:  # once a sentence went on the screen, what
                        overflow.append(sentence)  # follows it goes too (never said out of its order)
                        return
                    if short_talk():  # two sentences, ≤ 35 words, the rest shown
                        # a held sentence (said at the end) counts as said
                        kept, more = research_rules.brief(
                            sentence, [*sent, *held], frozenset(WORKING.values()), **caps
                        )
                        if more:
                            overflow.append(more)
                        sentence = f"{kept} " if kept else ""  # a bracketed address out
                        if not sentence:
                            return
                    # RAIL: no claimed change without one. Held until the
                    # round's writes are back, and what follows a held sentence waits after it.
                    if held or not _backed(sentence, turn, lang) or promised(sentence) is not None:
                        # a promise for later or a "tap Send" waits too: what backs it may come after it
                        held.append(sentence)
                        return
                    if not first_say:
                        first_say.append(time.monotonic() - started)
                    turn.claimed = turn.claimed or claim(sentence) is not None
                    await emit(sentence, False)
                    sent.append(sentence)

                async def round_end(waiting: list[str], messages: list[Any], unread_from: int) -> None:
                    """A round's words that waited for its end. Written before a tool the model went on to
                    (the next round answers from its result): its questions and its last words are held,
                    said only when no later round says anything (`before_tools`); its other sentences were
                    said as they came (with `finish` beside the tool, its statements are the reply, said now).
                    Else said in order, or, under a standing "rispondimi breve", the answer first
                    (`research_rules.answer_first`)."""
                    if went_on_to_tools(messages):
                        tail = splitter.rest()
                        asked = [x for x in waiting if x.rstrip().endswith("?")]
                        # under a standing "rispondimi breve" (no finish beside the tool) the round's
                        # statements wait for the round that reads the result: the cut picks the answer among
                        # them all
                        # (quality audit §24 m1: said here, they took the cap, and the answer went to the
                        # screen)
                        capped = style.words is not None and short_talk() and not spoke_and_finished(messages)
                        if capped and tail.strip() and not tail.rstrip().endswith("?"):
                            waiting, tail = [*waiting, tail if tail[-1:].isspace() else tail + " "], ""
                        written: list[str] = []  # its questions, and (not in a talk) its statements, in order
                        for one in waiting:
                            if one in asked:
                                written.append(one)
                                continue
                            if capped:
                                carried.append(one)
                            elif streams or spoke_and_finished(messages):  # as they would have been
                                await take(one)
                            else:
                                written.append(one)
                        # what it said of their lists before the read it went on to: the read's round says
                        # it from the result (never a read round of its own, never said twice)
                        before_read = [one for one, _ in unread_lists[unread_from:] if unread(one) is None]
                        unread_lists[unread_from:] = [
                            x for x in unread_lists[unread_from:] if x[0] not in before_read
                        ]
                        if spoke_and_finished(messages):
                            # finish beside them: its words are the reply, the read only checks it (o1-02a);
                            # never a question
                            for one in [*before_read, tail]:
                                if one.strip() and not one.rstrip().endswith("?"):
                                    await take(one if one[-1:].isspace() else one + " ")
                            before_read, tail = [], tail if tail.rstrip().endswith("?") else ""
                        if (
                            not capped
                            and streams
                            and tail.rstrip()[-1:] in (".", "!", "…")
                            and not form.closing_offer(tail)
                            and claim_rules.on_topic(tail, text, *said_back)
                        ):
                            # a statement on the turn's subject: the reply carries it (never a stray
                            # fragment: "It said it would laugh at it later.", f12-en-3)
                            last_words.append(tail if tail[-1:].isspace() else tail + " ")
                            tail = ""
                        pieces = [x for x in [*written, *before_read, tail] if x.strip()]
                        if pieces:
                            before_tools.extend(x if x[-1:].isspace() else x + " " for x in pieces)
                            before_at[0] = len(sent)
                        return
                    if carried:  # the statements a capped round wrote before its tools, first, never twice
                        waiting = [
                            *carried,
                            *(x for x in waiting
                              if not form.said_before(x, carried) and not form.near_duplicate(x, carried)),
                        ]  # fmt: skip
                        carried.clear()
                    if style.words is None or not short_talk():
                        for one in waiting:
                            await take(one)
                        return
                    if (
                        messages
                        and (tail := splitter.pending()).strip()
                        and tail.rstrip()[-1:] in ".!?…"
                        and not cut_short(messages, MAX_OUTPUT_TOKENS + pick_tokens)
                    ):
                        splitter.rest()
                        waiting = [*waiting, tail if tail[-1].isspace() else tail + " "]
                    kept, unsaid = research_rules.answer_first(
                        waiting, " ".join(said_back), [*sent, *held], frozenset(WORKING.values()), **caps
                    )
                    if len(kept) < len(waiting):
                        _log.info("brief_answer_first", trace_id=trace_id, kept=len(kept), left=len(unsaid))
                    spoke = len(sent) + len(held)
                    redo = len(unread_lists) + len(misses) + len(unlooked)
                    for one in kept:
                        await take(one)
                    if (
                        len(sent) + len(held) == spoke
                        and len(unread_lists) + len(misses) + len(unlooked) > redo
                    ):
                        # RAIL: nothing of the round was said, a rail held it for a round of its own (their
                        # lists unread, Pisa's own note, a search not made): that round answers in its place,
                        # so what the cut left over never goes to the screen and never takes that answer with
                        # it (quality audit §24 m1: "Non ho riletto la tua giornata…" after get_tasks)
                        return
                    overflow.extend(
                        x.strip() for x in unsaid if x.strip() and not x.rstrip().endswith("?")
                    )  # the screen's, never said; a question left out goes

                async def speak(user_prompt: str | None, past: list[Any], extra: str = "") -> list[Any]:
                    mark[0] = time.monotonic()
                    # a run's last words
                    # end their sentence before the next run speaks, never glued to its first; and the
                    # reply the model gave with finish (its text and finish in one response) is the turn's
                    # one reply
                    if user_prompt is None and (tail := splitter.rest()).strip():
                        # a space ends its sentence
                        await take(tail if tail[-1].isspace() else tail + " ")
                    replied[0] = replied[0] or (
                        user_prompt is None and bool(sent or held) and spoke_and_finished(past)
                    )
                    if offers:  # the round before held its closing offer: it never opens this round's words
                        stale_offers[:] = offers
                        stale_at[0] = len(sent)
                        offers.clear()
                    async with agent.run_stream(
                        user_prompt,
                        message_history=past,
                        instructions=[instructions, InstructionPart(extra, dynamic=True)]
                        if extra
                        else instructions,
                        event_stream_handler=on_events,
                        model_settings=cast(Any, pick_settings),
                        # a follow-up run gets what the turn has left, never a fresh limit
                        usage_limits=UsageLimits(
                            request_limit=max(1, MAX_MODEL_REQUESTS - done_usage.requests)
                        ),
                    ) as run:
                        # RAIL: one reply is one model output (quality audit §23 m1, "Te la metto nel piano?
                        # Oggi non hai eventi."). A question waits for the next sentence of its round, and
                        # under a standing "rispondimi breve" the whole round waits: only the round's end
                        # says whether the model went on to a tool (its words then came before the result)
                        waiting: list[str] = []
                        unread_from = len(unread_lists)
                        # … and outside a talk every round waits whole too (§32 D2: "Hi! I'm good,
                        # thanks.", get_tasks, then "Ciao, tutto bene. Oggi…": the first round's words were
                        # said as they streamed, before its tool call, and the next round answered again): a
                        # round that goes on to a tool is said only when no later round says anything
                        # (`before_tools`)
                        whole_round = (style.words is not None and short_talk()) or not streams
                        earlier_rounds[:] = [x for x in sent if x.strip() not in WORKING.values()]
                        async for delta in run.stream_text(delta=True, debounce_by=None):
                            ctx.usage.update(done_usage + run.usage)
                            for raw in splitter.feed(delta):
                                if whole_round or raw.rstrip().endswith("?"):
                                    waiting.append(raw)
                                    continue
                                for one in waiting:  # a question in the middle of its round: in its place
                                    await take(one)
                                waiting.clear()
                                await take(raw)
                        done_usage.incr(run.usage)
                        ctx.usage.update(done_usage)
                        cached.append((run.usage.cache_read_tokens, run.usage.cache_write_tokens))
                        turn.tokens = (done_usage.input_tokens, done_usage.output_tokens)
                        ran.append(run.all_messages())
                        await record_unstreamed(ran[-1])
                        await round_end(waiting, ran[-1], unread_from)
                        return ran[-1]

                messages = await speak(prompt, past)
                # the model spoke and showed in one response; the run ends there unless what it
                # showed isn't in front of the user (unavailable, unsupported, held): it says it in words.
                last = messages[-1] if messages else None
                returns = [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(
                    last, ModelRequest
                ) else []  # fmt: skip
                # RAIL: a call the model got wrong (its retry prompt) or a result
                # that isn't a show (a refusal, a search, a question) is never left unsaid: one more round,
                # templated results said from their sentence without a provider call.
                retried = isinstance(last, ModelRequest) and any(
                    isinstance(p, RetryPromptPart) for p in last.parts
                )
                again = retried or (returns and turn.loop.unseen(returns))
                if again or any(p.tool_name not in PRESENTATION for p in returns):
                    turn.loop.shows.clear()
                    if ctx.grow is not None:  # RAIL: the follow-up's request is reserved before it runs
                        await ctx.grow(history_chars(messages))
                    messages = await speak(None, messages)
                if cut_short(messages, MAX_OUTPUT_TOKENS + pick_tokens):  # arm b's own cap
                    # RAIL: a reply cut at the output cap
                    #  is never said cut: its unfinished
                    # sentence is dropped and one more round says it whole, from the same results.
                    fragment = splitter.rest()
                    _log.warning("reply_cut", trace_id=trace_id, said=len(sent), fragment_chars=len(fragment))
                    if ctx.grow is not None:
                        await ctx.grow(history_chars(messages))
                    cut = data_block("history", fragment.strip()[-300:] or "-", what="your own reply, cut")
                    await speak(
                        None, as_continuation(messages), CUT_LINE.format(fragment=f"{cut.rule}\n{cut.text}")
                    )
                if (
                    not rewrote
                    and (tail := splitter.pending()).strip()
                    and any(
                        claim_rules.internal(x, asked_change=not research_talk())
                        or (asked_nothing() and claim(x) is not None and not _backed(x, turn, lang))
                        or research_rules.unsearched(
                            x, text, "web_search" in turn.tools or research.web_tried
                        )
                        or (research.calls and research_rules.misnamed(x, known_names()))
                        or unread(x) is not None
                        for x in form.split_sentences(tail)
                    )
                ):
                    splitter.rest()
                    await take(tail if tail[-1].isspace() else tail + " ")  # its notes join the rewrite
                if not rewrote and asked_nothing():
                    # a claimed change they never asked for, with none made, is a line
                    # to rewrite too ("Non l'ho messo: non ho cambiato niente."), never the rail's own
                    # words
                    for n, one in enumerate(list(held)):
                        if (claim(one) is not None and claim_rules.team_claim(one) is None
                                and not claim_rules.bare(one) and not _backed(one, turn, lang)
                                and not claim_rules.waits_for_yes(one, held[n + 1 :])):  # fmt: skip
                            held.remove(one)
                            misses.append((one.strip(), "says Pisa changed something; nothing was changed"))
                if carried:  # a capped round's statements no later round took up: said now, within the cap
                    await round_end([], [], len(unread_lists))
                read_since()
                if unread_lists and not rewrote:
                    # their lists read before what is or isn't there is said, one round, tools
                    # offered (the claim rail and hold in it as in any round)
                    rewrote.append(True)
                    if ctx.grow is not None:
                        await ctx.grow(history_chars(ran[-1]))
                    unsaid = data_block(
                        "history",
                        "\n".join(
                            f"«{one[:200]}»: {why}" for one, why in [*unread_lists, *unlooked, *misses]
                        ),
                        what="your own sentences, not said",
                    )
                    await speak(
                        None,
                        as_continuation(ran[-1]),
                        READ_LINE.format(lines=f"{unsaid.rule}\n{unsaid.text}"),
                    )
                if unlooked and not rewrote:
                    # the general tool before declining or estimating, one round, tools
                    # offered (the claim rail and hold in it as in any round)
                    rewrote.append(True)
                    if ctx.grow is not None:
                        await ctx.grow(history_chars(ran[-1]))
                    unsaid = data_block(
                        "history", "\n".join(f"«{one[:200]}»: {why}" for one, why in [*unlooked, *misses]),
                        what="your own sentences, not said",
                    )  # fmt: skip
                    await speak(
                        None,
                        as_continuation(ran[-1]),
                        SEARCH_LINE.format(lines=f"{unsaid.rule}\n{unsaid.text}"),
                    )
                if misses and not rewrote:
                    # the miss goes back to the model as
                    # the result of its own reply, once; what it says then is said, its notes left out
                    rewrote.append(True)
                    if ctx.grow is not None:
                        await ctx.grow(history_chars(ran[-1]))
                    unsaid = data_block(
                        "history", "\n".join(f"«{one[:200]}»: {why}" for one, why in misses),
                        what="your own sentences, not said",
                    )  # fmt: skip
                    await speak(
                        None,
                        as_continuation(ran[-1]),
                        REWRITE_LINE.format(lines=f"{unsaid.rule}\n{unsaid.text}"),
                    )
                read_since()
                if before_tools:  # words written before a tool: said only when nothing came after them
                    if (
                        len(sent) == before_at[0]
                        and not held
                        and not early
                        and not splitter.pending().strip()
                    ):
                        for one in before_tools:
                            await take(one)
                    else:
                        _log.info("before_tools_dropped", trace_id=trace_id, sentences=len(before_tools))
                    before_tools.clear()
                if last_words:  # nothing came after them: the reply ends on them
                    pending = list(last_words)
                    last_words.clear()
                    for one in pending:
                        await take(one)
                turn.model_calls = done_usage.requests
            except UsageLimitExceeded:
                # A model that keeps calling tools after the research budget: the user gets an answer, not an
                # error.
                _log.warning("turn_request_limit", trace_id=trace_id)
                if not early and all(said.strip() in WORKING.values() for said in sent):  # only "Cerco…"
                    sentence = NO_ANSWER.get(lang, NO_ANSWER["en-US"])
                    await emit(sentence, False)
                    sent.append(sentence)

        # a pick's model settings under the A/B's arm (None: as every turn)
        pick_settings = self.research_arm.settings() if research.pick else None
        pick_tokens = self.research_arm.reasoning_tokens if research.pick else 0
        guard = await self._cheap_today(lang, prompt_chars)  # past CHEAP_AT of a cap, the fallback
        cheap = guard[0] if guard is not None else None
        cached: list[tuple[int, int]] = []  # (read, written) prompt-cache tokens, from the provider's usage
        ran: list[list[Any]] = []  # each run's messages (the last one holds the whole turn)
        models_used: list[Any] = []  # the turn's models (a fallback's too)
        model_started = time.monotonic()
        try:
            arm = self.research_arm if research.pick else RESEARCH_ARMS["a"]  # picks only
            await self._gateway.run(
                arm.slot or SLOT,
                call,
                prompt_chars=prompt_chars,
                max_output_tokens=MAX_OUTPUT_TOKENS + arm.reasoning_tokens,
                ledger_slot=SLOT if arm.slot else None,
                trace_id=trace_id,
                tools=True,
                can_fallback=lambda: not sent,  # once the user heard part of a reply, don't restart it
                cheap=cheap is not None,
            )
        except PisaError:
            if doubt is None or sent:
                raise
            return await ask_doubt()  # no model (a cap, a provider down): Pisa's own one question
        except asyncio.CancelledError:
            if sent:
                turn.answered = True  # part of a reply was said: these words were answered
                # Shielded, and tracked: a continuation's drop_after waits for it.
                # a reply cut by a barge-in keeps the taint too
                outside = sorted(
                    {r.kind for r in (turn_read[-1].readings() if turn_read else []) if r.outside_now}
                )
                save = asyncio.ensure_future(
                    self._save_reply(
                        conversation_id, "".join(sent), lang, quoted=bool(outside), outside=outside
                    )
                )
                self._saving.add(save)
                save.add_done_callback(self._saving.discard)
                await asyncio.shield(save)
            raise
        model_s = time.monotonic() - model_started - sum(seconds for _, seconds in tool_times)
        if research.pending is not None and not research.shown:
            research.grow = None  # the model's run is over: no more model requests to reserve for
            try:  # the pick held for the guides, never shown again: shown now, as it was
                await research.pending()
            except PisaError:
                _log.warning("pending_pick_failed", trace_id=trace_id)
        tail = splitter.rest()
        if early and not answered_on_screen():  # nothing went on the screen: said as written
            tail = "".join(x if x[-1:].isspace() else x + " " for x in early) + tail
        rest = form.keep(tail, first=not sent, greet=greet)
        if rest.strip():  # the last words too: Pisa's notes and stray corrections out
            pieces = form.split_sentences(rest)
            if research.calls:  # only the clause naming what no result wrote goes
                cut = [research_rules.without_misnamed(x, known_names()) or x for x in pieces]
                if cut != pieces:
                    rest, pieces = " ".join(x.strip() for x in cut), cut
            kept_pieces = [x for x in pieces if own_line(x) is None and unread(x) is None]
            grounded_pieces: list[str] = []
            for x in kept_pieces:  # RAIL: the last words' task states too
                if (truth := await grounded(x)) is None:
                    grounded_pieces.append(x)
                    continue
                _log.info("task_claim_grounded", trace_id=trace_id)
                turn.unbacked += 1
                if voice:
                    apart.append(claim_rules.as_clause(truth))
                else:
                    grounded_pieces.append(truth)
            changed_pieces = grounded_pieces != kept_pieces
            kept_pieces = grounded_pieces
            # RAIL: the last words claim nothing this install can't do either
            overclaimed += [c for x in kept_pieces if (c := abilities_rules.overclaim(x, can_do)) is not None]
            kept_pieces = [x for x in kept_pieces if abilities_rules.overclaim(x, can_do) is None]
            # RAIL: a promise for later with no timed write, a "tap Send" with no button shown
            overclaimed += [c for x in kept_pieces if (c := unkept(x)) is not None]
            kept_pieces = [x for x in kept_pieces if unkept(x) is None]
            if overclaimed:  # a short question about a held offer goes with it
                kept_pieces = [x for x in kept_pieces if not abilities_rules.asks_about_it(x)]
            if len(kept_pieces) < len(pieces) or changed_pieces:
                rest = " ".join(x.strip() for x in kept_pieces)
        if answered_on_screen() and research.lead:  # the sheet's lead, Pisa's words
            rest = research.lead
        elif answered_on_screen():  # a short lead, never a list read aloud
            lead: list[str] = []
            for one in form.split_sentences(rest):
                if not research_rules.past_lead(one, own_words() + lead):
                    lead.append(one)
            rest = " ".join(x.strip() for x in lead)
        if replied[0]:  # the reply was said; a later run's last words only when they add
            rest = " ".join(s.strip() for s in form.split_sentences(rest)
                            if form.adds(s, sent, held, ask=bool(turn.approvals)))  # fmt: skip
        rest = form.owned(rest, text, lang) if not sent else rest
        rest = "" if form.said_before(rest, sent) else rest
        own_said = [x for x in sent if x.strip() not in WORKING.values()]
        if rest.strip() and own_said:  # small talk once, a greeting only as the reply's first words (§32 D2)
            kept_rest: list[str] = []
            for one in form.split_sentences(rest):
                if not form.small_talk_again(one, [*own_said, *kept_rest]):
                    kept_rest.append(form.without_greeting(one))
            rest = " ".join(x.strip() for x in kept_rest)
        if short_talk() and rest.strip():  # the last words too, at a sentence
            spoken_now: list[str] = []
            for one in form.split_sentences(rest):
                if overflow:  # after a sentence on the screen, the rest follows it there
                    overflow.append(one)
                    continue
                kept, more = research_rules.brief(
                    one, [*sent, *held, *spoken_now], frozenset(WORKING.values()), **caps
                )
                spoken_now += [f"{kept} "] if kept else []
                overflow += [more] if more else []
            rest = "".join(spoken_now).strip()
        if rest and (held or not _backed(rest, turn, lang)):
            held.append(rest)
            rest = ""
        turn.claimed = turn.claimed or (bool(rest) and claim(rest) is not None)
        # the record this turn's claims stand on, for the replay's check of every claim
        turn.kept = turn.record()
        if held:  # RAIL: the promises and the "tap Send"s the turn's end still doesn't back go
            overclaimed += [c for x in held if (c := unkept(x)) is not None]
            held = [x for x in held if unkept(x) is None]
        if held:  # RAIL: a held "non ci sono riuscito", a task's state, on the turn's whole record
            regrounded: list[str] = []
            for x in held:
                if (truth := await grounded(x)) is None:
                    regrounded.append(x)
                    continue
                turn.unbacked += 1
                if voice:
                    apart.append(claim_rules.as_clause(truth))
                else:
                    regrounded.append(truth)
            held = regrounded
        if held:
            # RAIL: "Li sposto al 30." said with no change made is replaced by the truth (once);
            # said as written once a change backs it (the tool ran after the sentence was streamed).
            # in the order they were said.
            # `others`: the reply says something besides what was held (a bare "Fatto." alone keeps its truth)
            others = bool(sent) or bool(rest.strip()) or any(claim(s) is None for s in held)
            unbacked: list[tuple[str, bool, bool]] | None = [] if voice else None
            kept, false = _released(
                held, turn, lang, others=others, said=" ".join([*sent, rest]), apart=unbacked
            )
            if unbacked:  # a talk: one clause for every claim left out, first in the correction line
                apart.insert(0, claim_rules.unbacked_clause(
                    [x for x, _, _ in unbacked], lang, waiting=bool(turn.approvals),
                    failed=[f for _, f, _ in unbacked], unverified=[u for _, _, u in unbacked],
                ))  # fmt: skip
            turn.claimed = turn.claimed or any(claim(s) is not None and _backed(s, turn, lang) for s in held)
            if false:
                turn.unbacked += false
                _log.warning("claim_without_change", trace_id=trace_id, sentences=false)
            rest = f"{kept} {rest}".strip()
        # RAIL: every web fact has its links on the phone.
        # Sent before the reply is said, and "it's on your phone"
        # said only when the phone has it (the card delivered, or an object it showed);
        # before code's question, which stays last
        if overflow and not await show_rest(overflow):  # no screen took it: said, not lost
            if style.words is None:
                rest = " ".join([rest, *overflow]).strip()
            else:  # their standing "rispondimi breve" asked for less: the answer within it, the rest unsaid
                _log.info("brief_rest_unshown", trace_id=trace_id, sentences=len(overflow))
        rest, last = _screen_sentences(rest)  # the final sentence through the same check
        screen_claims.extend(last)
        delivered = False
        if sources_card is not None and not research.shown and (links := research.sources()):
            lines = [f"{s['title']} · {s['host']}"[:200] for s in links]
            title = SOURCES_TITLE.get(lang, SOURCES_TITLE["en-US"])
            delivered = bool(await sources_card({"title": title, "lines": lines, "sources": links}))
        if screen_claims and not (delivered or presenter.on_screen):
            turn.unbacked += len(screen_claims)  # a claim with no record
        if short_talk() and screen_claims and (delivered or presenter.on_screen):
            # "it's on your phone" appended after the cut: only within the words (the phone has it)
            # nor past the two sentences (a third sentence after an offer was said)
            words_said = research_rules.spoken_words(" ".join([*sent, rest, *screen_claims]))
            own = " ".join(x for x in [*sent, rest] if x.strip() and x.strip() not in WORKING.values())
            sentences = len(form.split_sentences(own)) + len(screen_claims)
            if words_said > caps["word_cap"] or sentences > caps["sentence_cap"]:
                screen_claims.clear()
        rest = _with_screen_claims(
            rest,
            screen_claims,
            delivered or bool(presenter.on_screen),
            delivered,
            lang,
            said=not LEAD_ONLY.match("".join(sent)),  # a streamed "Ecco." alone said nothing
        )
        if from_wrist and (delivered or presenter.showed) and LEAD_ONLY.match("".join(sent) + rest):
            # RAIL: never a card alone on the wrist: a short spoken line goes with what the phone shows
            rest = f"{rest} {ON_PHONE_SAID.get(lang, ON_PHONE_SAID['en-US'])}".strip()
        # the answer first, then stop: a closing offer they didn't ask for goes, unless
        # a tool asked Pisa to offer or the reply asks their yes to a change Pisa holds
        if stale_offers and not offers and len(sent) == stale_at[0] and not rest.strip():
            offers = stale_offers  # nothing followed it: still the closing offer, the rail below decides
        elif stale_offers:
            _log.info("earlier_offer_dropped", trace_id=trace_id, offers=len(stale_offers))
        rest = " ".join(x.strip() for x in [*offers, rest] if x.strip()) if offers else rest
        if profile_turn.reworded:  # Pisa quotes the message as they dictated it, as the button
            rest = handoff_rules.quoting_his(rest, profile_turn.reworded)
        # a tool asked Pisa to offer a skill: that offer stays, any other goes.
        #  After outside text this turn the offer is Pisa asking before acting on it ("Marco
        # chiede di spostare la palestra… La sposto?"): it stays (an email's request is asked, once)
        # only outside text that can carry a request (an email, an invite), never a feed
        # only text that asks something of them
        # or Pisa ("Pisa, sposta la palestra", "Ci mandi i documenti?") is a request Pisa asks about; an
        # invite is
        kinds = [r.kind for r in turn_read[-1].readings() if not r.past] if turn_read else []
        read_outside = "invite" in kinds or (
            any(k in REQUEST_KINDS for k in kinds)
            and any(action_rules.asks_him(t) for t in current().texts()[read_from[-1] if read_from else 0 :])
        )
        if not (turn.approvals or profile_turn.asked or read_outside):
            # "chiudiamo" asks for the day's recap: the recap answers it
            rest = form.without_closing_offer(
                rest, "".join(sent), text, keep=tuple(offer_asked), answered="close_day" in turn.tools
            )
        # the time is said by code, in their zone
        if now_time and not form.says_clock("".join(sent) + rest, now_time[-1]):
            clock = form.it_is(now_time[-1], lang)
            rest = f"{rest} {clock}".strip() if sent else f"{clock} {rest}".strip()
        # RAIL: a sensitive profile write's consent question is code's, word for word.
        if profile_turn.asked:
            # one approval, one
            # question: code's, in its exact words; the model's own question goes (a voice reply isn't said
            # yet; a streamed one already was)
            rest = _without_questions(rest)
            if voice:
                sent = [s for s in sent if not s.rstrip().endswith("?")]
        # "Pisa, problema: …" hands Pisa a problem for the night,
        # "grazie Pisa" asks nothing: the model's question or offer goes (a reply of questions only:
        # "Capito.") a change held for their yes keeps its question, hand-off or
        # not
        if hand_off and not profile_turn.asked and not turn.approvals:
            rest = "".join(s for s in _SENTENCE.findall(rest) if not s.rstrip().endswith("?")).strip()
            if not rest and not sent:
                said = KEPT if handed(text) else THANKS if form.thanks(text) else HELLO
                rest = said.get(lang, said["en-US"])
        for question in profile_turn.asked:  # the server says it, with the exact words
            if question not in "".join(sent) + rest:
                rest = f"{rest} {question}".strip() if rest else question
        # a hunch noted this turn is asked in its own words, as the profile's
        # consent question is: the model's paraphrase ("il venerdì sera è ormai il tuo rito col sushi?") left
        # their "sì" answering nothing (the yes binds only a question the reply really said)
        rituals = self._tools.rituals[0] if self._tools.rituals is not None else None
        hunch = rituals.question_in_turn(device_id, turn) if rituals is not None else None
        if (
            rituals is not None
            and hunch
            and not profile_turn.asked
            and not turn.approvals  # a held change's question never goes
            and not rituals.said(hunch, "".join(sent) + rest)
        ):
            rest = _without_questions(rest)
            if voice:
                sent = [s for s in sent if not s.rstrip().endswith("?")]
            rest = f"{rest} {hunch}".strip()
        # their words say a habit and the reply asked its guess in words without
        # note_hunch: that question is the hunch Pisa asked (asked, never stated, and their yes alone makes
        # it theirs), so their next "sì" answers it. Never in a turn that read anything, asked a yes or wrote.
        if (
            rituals is not None
            and not hunch
            and not profile_turn.asked
            and not turn.approvals
            and not turn.done_tools
            and "note_hunch" not in turn.tools
            and not any(r.kind != "day-context" for r in (turn_read[-1].readings() if turn_read else []))
            and "note_hunch" in tool_choice.tools_for(text)
            and (guess := ritual_rules.asked_guess("".join(sent) + rest, text)) is not None
        ):
            await rituals.note_hunch(guess[0], [text[:200]], device_id, guess[1])
        # a residency count is closed by code's own words, never left to the model
        closing = NOT_ADVICE.get(lang, NOT_ADVICE["en-US"])
        if "residency_count" in turn.tools and form.flat(closing) not in form.flat("".join(sent) + rest):
            rest = f"{rest} {closing}".strip()
        # RAIL: their tasks for the day asked are never left out: a turn that read them (get_tasks, the plan)
        # and answered "non hai eventi" / "oggi niente" while the store has some gets code's line naming them
        # (quality: "never 'non hai eventi' while tasks exist")
        read_tasks = any(
            name in TASK_DAY_READS and n not in turn.refused for n, (name, _) in enumerate(turn.calls)
        )
        # never on their small talk ("Ciao, come va?"): the model's reply is the answer, code adds no line to
        # it (§32 D2: "Ciao, bene! … ci sono tre cose aperte: … You have 2 things to do: …")
        if read_tasks and not tapped and not (form.small_talk(text) and not asks_day):
            said_all = "".join(sent) + rest
            tomorrow = bool(_TOMORROW.search(text))
            titles, unnamed, undated = await self._day_tasks(tomorrow=tomorrow)
            whole_day = not claim_rules.asks_now(text)  # "cosa faccio adesso?": the next one answers
            if claim_rules.omits_tasks(
                said_all, titles, asked_day=asks_day, total=len(titles) + unnamed, whole_day=whole_day
            ) or (unnamed and not titles and claim_rules.says_nothing_to_do(said_all)):
                line = claim_rules.tasks_line(lang, titles, unnamed, tomorrow=tomorrow, undated=undated)
                _log.info("tasks_omitted_said", trace_id=trace_id, tasks=len(titles) + unnamed)
                rest = f"{rest} {line}".strip()
        # the knock or the tragitto opened by the model's answer_my_question is said by
        # code after the reply, in its own words; the model only had a receipt
        if rituals is not None and (opened := rituals.code_says(device_id, turn)):
            rest = f"{rest} {opened}".strip()
        rest = _proposal_said(rest, "".join(sent), turn, lang)  # a proposal asks their yes
        if cheap:  # said once a day, never silent: the user knows why Pisa may sound simpler
            rest = f"{rest} {cheap}".strip()
        # at most one line of Pisa's own at the end: a cue-question with Pisa's own reason,
        # else its view once on a decision or a habit. Never after outside text, a question, or a floor item.
        if self.deps.perceive is not None and not voice:
            line = await self.deps.perceive.after_turn(
                device_id,
                text,
                lang,
                "".join(sent) + rest,
                turn=turn,
                heard=heard,
                outside=any(r.outside_now for run in turn_read for r in run.readings()),  # every run's reads
                open_question=bool(turn.approvals or profile_turn.asked or hunch or hand_off),
                floor=bool(folded_items and folded_items[0]),
                moves=list(decisions.turn() or []),  # type: ignore[call-overload]
            )
            if line:
                rest = f"{rest} {line}".strip()
        # their question Pisa couldn't answer is a gap: one line, once a day, when the
        # answer could change something for them; never with a question of Pisa's open.
        # Never after any outside text the talk read (a mail, an invite, a page, a search result; an earlier
        # turn's restored by the history included). Pure checks first, the DB only on a gap.
        # The row is written after the reply (`after_reply`), off the talk path.
        if self.deps.prurito is not None:
            so_far = "".join(sent) + rest
            curious = await self._curious(
                device_id,
                conversation_id,
                text,
                so_far,
                lang,
                # a tool's error Pisa wrote itself is Pisa's own words (its block's own kind, `OWN_ERROR`);
                # computed inside `_curious`
                outside=lambda: bool(voice6_outside([r.kind for run in turn_read for r in run.readings()])),
                # never after a question, a proposal, an approval asked, an act or a recap
                open_question=lambda: bool(
                    turn.approvals or profile_turn.asked or hunch or hand_off or turn.proposals
                    or turn.done or turn.acted or turn.added or turn.done_tools
                    or RECAP_TOOLS & set(turn.tools)
                )
                or voice6_rules.quiet_after(so_far) is not None,
            )  # fmt: skip
            if curious:
                rest = f"{rest} {curious}".strip()
        if (
            (misses or unlooked or unread_lists)
            and not rest.strip()
            and not "".join(s for s in sent if s.strip() not in WORKING.values()).strip()
        ):
            # the rewrite said nothing and nothing else was said: the shortest safe
            # line, the canned truth only as the last resort
            changed_miss = any(why.startswith("says Pisa changed") for _, why in misses)
            rest = claim_rules.truth(lang, waiting=False) if changed_miss else rest
            if misses and all(why.startswith("names ") for _, why in misses) and not unlooked:
                # every sentence named what no result wrote, twice: never silence, never the names
                rest = NO_ANSWER.get(lang, NO_ANSWER["en-US"])
            if unread_lists and not rest.strip():  # the read round said nothing: never the unread claim
                rest = claim_rules.unread_truth(lang, day=asks_day)
            if unlooked and not rest.strip():  # the search round said nothing: their answer as it was
                rest = " ".join(one for one, _ in unlooked)
        if overclaimed:  # a held claim of what isn't connected: what is not, and where to connect it, once
            facts = await self._abilities(device_id, *phone_senders)
            said_words = "".join(sent) + " " + rest
            # only what the held sentences claimed, never an item the reply already says is off (audit §13)
            line = abilities_rules.not_connected(facts, lang, claimed=overclaimed, said=said_words)
            if not line and "booking" in overclaimed:
                line = abilities_rules.never(lang)
            if "reminders" in overclaimed:  # an experimental feature that is off: not available, said plainly
                if profile_turn.dated:  # kept for a time: what was kept, and that no reminder comes
                    small = in_small_hours(self._clock().astimezone(self._owner.zone))
                    today = self._clock().astimezone(self._owner.zone).date()
                    kept_line = " ".join(
                        abilities_rules.kept_no_alert(
                            action_rules.clean(title),
                            action_rules.spoken_when(at, today, lang, self._owner.tz, small_hours=small),
                            lang,
                        )
                        for title, at in profile_turn.dated[:2]
                    )
                    line = f"{line} {kept_line}".strip()
                else:
                    line = f"{line} {abilities_rules.unavailable('reminders', lang)}".strip()
            # a feature off on this install: "<label> isn't available on PISA.", once each
            off_line = abilities_rules.unavailable_lines(overclaimed, lang, said=said_words)
            line = f"{line} {off_line}".strip()
            if (
                "promise" in overclaimed
                and (claim_rules.LIST_WRITES - abilities_rules.PROMISE_WRITES) & turn.done_tools
            ):
                line = f"{line} {abilities_rules.listed_no_alert(lang)}".strip()  # on the list, no alert
            # a held promise of a reminder that was set: never the action left unconfirmed (§32 f10-en-1);
            # its line says it is set and for when, from its result, and that it shows in the app if so
            set_now = "remember_to" in turn.done_tools and (
                "promise" in overclaimed or "promise_unreached" in overclaimed
            )
            moments = (
                abilities_rules.set_moments(reminders_set, self._clock(), self._owner.zone) if set_now else []
            )
            if moments:
                small = in_small_hours(self._clock().astimezone(self._owner.zone))
                today = self._clock().astimezone(self._owner.zone).date()
                in_app = abilities_rules.unreached(reminders_set)
                for at in moments[:2]:
                    when = action_rules.spoken_when(at, today, lang, self._owner.tz, small_hours=small)
                    line = f"{line} {abilities_rules.set_line(when, lang, in_app=in_app)}".strip()
            elif "promise_unreached" in overclaimed:  # the reminder is set; it shows when they open the app
                line = f"{line} {abilities_rules.seen_in_app(lang)}".strip()
            if "tap" in overclaimed:
                line = f"{line} {abilities_rules.no_button(lang)}".strip()
            if line and line not in "".join(sent) + rest:
                own = [*sent, *form.split_sentences(rest)]
                if all(_filler(x) or x.strip() in WORKING.values() for x in own):  # the line is it all
                    rest = ""
                    rail_only = True
                rest = f"{rest} {line}".strip()
        # the user's own model server read only part of what Pisa sent (Ollama past its context, silently):
        # said once, plainly, after the answer
        notice = next((n for m in models_used if (n := cut_notice_once(m, lang)) is not None), None)
        if notice is not None:
            rest = f"{rest} {notice}".strip()
        if rest and not first_say:  # a one-sentence reply is said with the final chunk
            first_say.append(time.monotonic() - started)
        # the interim "Cerco, un attimo." is for the
        # ear only: the reply's text (the screen, the history) starts with the answer, never with the filler
        # never starting or ending on a space (a held sentence keeps the space that ended it when said)
        # the said part and the rest as they were written (a line break between them stays one: a data
        # block's lines), with no space left at either edge ("Nothing tomorrow. ")
        reply = (
            "".join(
                s for s in sent if s.strip() not in WORKING.values() and not (rail_only and _filler(s))
            ).lstrip()
            + rest
        ).strip()
        # a talk's correction: its own line after the reply (stored and said apart); with no reply
        # around it, it is the reply, said plainly
        fix = claim_rules.correction(lang, apart)
        if fix and not reply.strip():
            reply = rest = claim_rules.correction(lang, apart, lead=False)
            fix = ""
        if fix:
            _log.warning("talk_correction_apart", trace_id=trace_id, clauses=len(apart))
        turn.correction = fix
        turn.profile_asked = bool(profile_turn.asked)
        turn.asked = self.unasked.mark()
        if self._tools.actions is not None and turn.approvals:
            # the one yes approves exactly what this reply's one question names.
            turn.proposed = list(turn.approvals)
            turn.approvals = await self._tools.actions.bundle(device_id, turn.approvals, reply, turn.spoken)
        # Saved before the final chunk, so the device's next utterance always sees this reply in history, and
        # its moves ("perché?" right after). With the turn's tool calls and results.
        model = history_kept(ran[-1], said=turn.loop.finished) if ran else None
        turn.answered = True
        turn.questioned = reply.rstrip().endswith("?") and not (
            turn.approvals or turn.proposed or turn.acted or turn.done or turn.added or turn.profile_asked
        )
        # what this turn read from outside stays with the reply (and the reply is
        # data, like a write's): every later turn that still has it in the history is tainted by it
        outside = sorted({r.kind for r in (turn_read[-1].readings() if turn_read else []) if r.outside_now})
        reply_row = await self._save_reply(
            conversation_id,
            reply,
            lang,
            quoted=bool(WRITES & set(turn.tools)) or bool(outside),
            model=model,
            outside=outside,
        )
        if voice:  # a talk's reply may go in silently: what Pisa's voice said then takes its place (`spoken`)
            self._reply_rows[(device_id, trace_id)] = reply_row
            while len(self._reply_rows) > REPLY_ROWS_KEPT:
                self._reply_rows.popitem(last=False)
        if fix:  # what Pisa said, then its correction: two messages, never one spliced (its taint too)
            await self._save_reply(
                conversation_id,
                fix,
                lang,
                quoted=bool(WRITES & set(turn.tools)) or bool(outside),
                outside=outside,
            )
        if research.calls or research.stopped:  # ids and counts only
            _log.info("research_turn", trace_id=trace_id, calls=research.calls, rounds=research.rounds,
                      stopped=research.stopped, places=len(research.places), pages=len(research.read),
                      shown=research.findings is not None)  # fmt: skip
        if self.deps.prurito is not None:  # the gap's row, with what is left of its own allowance
            self.deps.prurito.after_reply(device_id, research)
        if self.deps.reflex is not None and not voice:  # Pisa said it (a talk's voice files its own words)
            self.deps.reflex.state(device_id).pisa_said(
                reply, "brain", self._clock(), backed=self.claimed(device_id)
            )
        if folded_items:  # what attend folded counts as said only once the turn has spoken
            await self._folded_said([i for _, _, ids in folded_items[0] for i in ids])
        # the sentences each rail held, by kind: counts only, never their words
        held_by = {k: overclaimed.count(k) for k in dict.fromkeys(overclaimed)}
        _log.debug("turn_trace", trace_id=trace_id, held=held_by, **trace_turn(lang, turn, [
            *parts, ("history", " ".join(t.content for t in turns)), ("words", prompt),
        ], traced, step))  # fmt: skip
        if moves := decisions.take():
            await self._decided(moves, device_id, trace_id)
        await emit(rest, True)
        # kept for their deeper questions;: after their last words, never failing the turn
        # (the next turn's history still holds this turn's results whole)
        # the kept request is their own words (never a tap's or a drawing's label: the
        # next turn reads who is with them from it)
        # a tapped turn has no words of theirs; the earlier request is kept, never emptied
        asked = research.said or str((research.earlier or {}).get("request") or "")
        if research.calls and (found := findings_rules.snapshot(research, asked)) is not None:
            await self._keep_findings(conversation_id, found)
        if drawing.shown is not None:  # kept for a redraw, with its conversation
            await self._keep_drawing(conversation_id, drawing.shown)
        if drawing.refused or drawing.shown is not None or tapped:  # ids and counts only
            _log.info("drawing_turn", trace_id=trace_id, shown=drawing.shown is not None,
                      refused=drawing.refused, tapped=tapped)  # fmt: skip
        only = next(iter(noted.values())) if len(noted) == 1 else None
        if (
            card is not None
            and only is not None
            and not research.shown
            and not presenter.showed
            and form.names(text, only.title)
        ):
            # the model chooses to show a place (show_place); code shows it only when
            # the turn ended showing nothing and the user named that one place.
            await card(only)
        if speaker is not None:
            await speaker.done()
        watch.kind = _reply_kind(turn, research)
        turn.parts = turn_parts(
            trace_id, context_s, round_ms, ran[-1][len(past) :] if ran else [], tool_times
        )
        self._log_turn(
            trace_id,
            watch,
            speaker,
            context_s=round(context_s, 3),  # the open check-in and the options, before the model
            model_s=round(model_s, 3),  # the model alone: its calls, without the tools' time
            tools=[{"name": name, "s": round(seconds, 3)} for name, seconds in tool_times],
            first_say_s=round(first_say[0] + started - watch.arrived, 3) if first_say else None,
            # the loop's measures (model calls, tool calls, the provider's tokens)
            rounds=turn.loop.rounds,
            tool_calls=len(turn.tools),
            input_tokens=turn.tokens[0],
            output_tokens=turn.tokens[1],
            cached_tokens=cached[-1][0] if cached else None,  # the prefix the provider served from its cache
            cache_write_tokens=cached[-1][1] if cached else None,
            head=watch.head,  # the reflex's reads were ready for these words
        )
        _log.debug("reply", trace_id=trace_id, chars=len(reply))
        return reply

    async def _abilities(
        self, device_id: uuid.UUID, compose: Any, call_phone: Any, directions: Any
    ) -> abilities_rules.Facts:
        """What this install can do now for this device (`abilities_rules.Facts`): the connectors' states, the
        place and web keys, push, the voice mode, and what the phone's app opens (the turn's senders: None
        when its app didn't pick that extension). An unreadable part counts as not there."""
        states: dict[str, Any] = {}
        if self.deps.connectors is not None:
            try:
                states = await self.deps.connectors.states()
            except UNREACHABLE:
                unreachable(_log, "abilities_connectors")
        keys: set[str] = set()
        push = False
        voice, talker = "natural", None
        try:
            async with self._pool.connection() as conn:
                rows = await (
                    # a paid provider counts once the user acknowledged it (Collegamenti): it is never
                    # called before
                    await conn.execute(
                        "SELECT p.provider FROM place_providers p WHERE p.ciphertext IS NOT NULL AND "
                        "(p.provider <> ALL(%s) OR EXISTS (SELECT 1 FROM paid_acks a WHERE a.provider = "
                        "p.provider))",
                        (sorted(ACKED),),
                    )
                ).fetchall()
                keys = {r[0] for r in rows}
                if await (await conn.execute("SELECT 1 FROM server_secrets WHERE name = 'maps'")).fetchone():
                    keys.add("apple")
                push = (
                    await (
                        await conn.execute(
                            "SELECT 1 FROM devices d WHERE d.id = %s AND (d.apns_token IS NOT NULL OR "
                            "(%s AND EXISTS (SELECT 1 FROM device_push_relay r WHERE r.device_id = d.id)))",
                            (device_id, experimental.on("push_relay", self.deps.enabled)),
                        )
                    ).fetchone()
                    is not None
                )
                conversation = (await self._owner.settings(conn)).get("conversation") or {}
                voice, talker = str(conversation.get("mode") or "natural"), conversation.get("talker")
        except UNREACHABLE:
            unreachable(_log, "abilities_read")
        on = {k for k, v in states.items() if v in ("on", "error")}  # "error": connected, a sync failing
        # an experimental feature that is off doesn't exist: never counted, never named as not connected
        hidden = abilities_rules.hidden(lambda feature: experimental.on(feature, self.deps.enabled))
        facts = abilities_rules.Facts(
            google="google" in on,
            tasks=self._tools.data is not None,
            maps=bool(keys & {"apple", "google"}),
            details="google" in keys,
            web="tavily" in keys,
            directions=directions is not None,
            messages=compose is not None,
            calls=call_phone is not None,
            health=bool(on & set(connector_rules.HEALTH_SOURCES)),
            bank=bool(on & set(connector_rules.BANKS)),
            push=push,
            voice=voice,
            talker=talker,
            off=hidden,
        )
        return abilities_rules.without(facts)

    async def _stop_heard(self, device_id: uuid.UUID, text: str, lang: str) -> str:
        """Their "ferma tutto": the stop (idempotent, its own ledger line), and their words with Pisa's line
        in the conversation's history, so the next turn's model knows Pisa stopped."""
        assert self._tools.autopilot is not None
        await self._tools.autopilot.stop(lang)
        said = stop_rules.stopped_line(lang)
        async with self._pool.connection() as conn:
            conversation_id = await _conversation_for(conn, device_id, self._clock())
            await _add_message(conn, conversation_id, "user", text, lang, self._clock())
        await self._save_reply(conversation_id, said, lang)
        _log.info("stopped_by_words", device_id=str(device_id))
        return said

    async def _resume_heard(self, device_id: uuid.UUID, text: str, lang: str) -> str:
        """Their "ok, puoi ripartire" while stopped: the resume (its own ledger line), and their words with
        Pisa's line in the conversation's history, so the next turn's model knows Pisa resumed."""
        assert self._tools.autopilot is not None
        await self._tools.autopilot.resume(lang)
        said = stop_rules.resumed_line(lang)
        async with self._pool.connection() as conn:
            conversation_id = await _conversation_for(conn, device_id, self._clock())
            await _add_message(conn, conversation_id, "user", text, lang, self._clock())
        await self._save_reply(conversation_id, said, lang)
        _log.info("resumed_by_words", device_id=str(device_id))
        return said

    async def _note_for_architect(self, device_id: uuid.UUID, words: str) -> None:
        """The user's note for the architect, kept 90 days (report.yml part `feedback`)."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO owner_feedback (id, device_id, said_at, words) VALUES (%s, %s, %s, %s)",
                (uuid.uuid4(), device_id, self._clock(), words[:2000]),
            )
        _log.info("owner_feedback", device_id=str(device_id), chars=len(words))

    async def _latest_question(self, device_id: uuid.UUID, turns: list[Turn]) -> str | None:
        """Pisa's newest question to this device: a check-in Pisa asked after its last reply (newest
        wins), else its last reply when that was a question."""
        before = self._last_turn.get(device_id)
        newer = before is None or before.asked is None or self.unasked.since(device_id, before.asked)
        if self._tools.data is not None and newer and (check_in := await self._tools.data.open_check_in()):
            return check_in
        last = next((t for t in reversed(turns) if t.role == "assistant"), None)
        return last.content if last is not None and last.content.rstrip().endswith("?") else None

    async def object_event(
        self,
        device_id: uuid.UUID,
        object_id: str,
        event: str,
        option_id: str | None = None,
        present_id: str | None = None,
        element_id: str | None = None,
    ) -> tuple[str, str] | None:
        """The device's word on an object Pisa showed (an `object_event`, or a late `presented` status, which
        names its present_id): the outcome of that show in Pisa's decision log, keyed by its present_id
        (a choice or an act is never overwritten). A show whose turn is still running holds
        the event until its row is stored. A tap on a choice's option is the user's own next message: (its
        words, the language), else None."""
        shown = self.objects.get(device_id, {}).get(object_id)
        key = present_id or (shown.present_id if shown is not None else None)
        if key:
            now = self._clock()
            async with self._pool.connection() as conn:
                if not await decision_log.outcome(conn, key, event, now):
                    self._held_outcomes[key] = (event, now)  # stored with the turn's moves (_decided)
        if event == "closed" and shown is not None:
            shown.closed = True  # a tap on a closed sheet answers nothing
        if event == "asked" and element_id is not None:
            return self.tap(device_id, object_id, element_id)
        if event != "chosen" or option_id is None:
            return None
        label = shown.options.get(option_id) if shown is not None else None
        if shown is None or label is None:
            _log.warning("choice_unknown", device_id=str(device_id))  # not Pisa's, or from before a restart
            return None
        if shown.expires_at is not None and self._clock() > shown.expires_at:
            _log.info("choice_expired", device_id=str(device_id))  # RAIL: a late tap answers nothing
            return None
        # the user's own act, grounded as their words for the object tapped only; a
        # pending approval is still decided only by its own answer: "[tap …]" is never a yes.
        return f"[tap {shown.link or object_id}] {option_id}: {label}", shown.lang

    def tap(self, device_id: uuid.UUID, object_id: str, element_id: str) -> tuple[str, str] | None:
        """A tap on an element of a sheet Pisa showed this device: the turn's words, "Sul
        disegno: …" around the element's label from the server's copy of that revision (the caller marks the
        turn `origin: drawing`), or None when it is gone (superseded, unknown, closed or expired): "Non è più
        aperto.", no model call. The label and the drawing around it wait for the turn (`_taps`)."""
        shown = self.objects.get(device_id, {}).get(object_id)
        now = self._clock()
        if (
            shown is None
            or shown.closed
            or shown.data is None
            or (shown.at is not None and now - shown.at > OBJECTS_KEPT)
        ):
            _log.info("tap_gone", device_id=str(device_id), known=shown is not None)
            return None
        found = visual_rules.element(shown.data, element_id)
        if found is None:
            _log.info("tap_unknown_element", device_id=str(device_id))
            return None
        label, context = found
        words = visual_rules.tap_words(label, shown.lang)
        self._taps[(device_id, words)] = visual_rules.Tap(label, tuple(context), shown.outside)
        while len(self._taps) > TAPS_KEPT:  # a tap never answered (their own words came first) goes in time
            self._taps.pop(next(iter(self._taps)))
        return words, shown.lang

    def tap_lang(self, device_id: uuid.UUID, object_id: str) -> str:
        """The language "Non è più aperto." is said in: the object's, else the device's last object's."""
        objects = self.objects.get(device_id, {})
        shown = objects.get(object_id) or next(reversed(objects.values()), None)
        return shown.lang if shown is not None else self._owner.tag

    async def _situation(self) -> tuple[datetime, tuple[str, datetime] | None]:
        """Their situation for a pick's "why for you now": the time where they are and their
        next timed event today (its title and start), from Pisa's own agenda; None when there is none or it
        can't be read (the pick goes on without it)."""
        tz = self._owner.zone
        now = self._clock().astimezone(tz)
        if self._tools.data is None:
            return now, None
        view = None  # stated default: no next event (the pick goes on without it)
        with degrades(_log, "pick_situation"):
            try:
                view = await self._tools.data.agenda_day(now.date())
            except PisaError as err:
                _log.info("pick_situation_refused", code=err.code)
        if view is None:
            return now, None
        upcoming = []
        for e in view.get("events") or []:
            if e.get("all_day") or not e.get("start"):
                continue
            try:
                start = datetime.fromisoformat(str(e["start"])).astimezone(tz)
            except ValueError:
                continue
            if start > now:
                # a title someone else wrote (an invite's, a shared calendar's) or a
                # sensitive one (health, worship, politics: in the title or the place) is never said: Pisa's
                # own words for it ("il tuo prossimo impegno"), the time kept
                title = str(e.get("title") or "")
                if (e.get("invited") or e.get("foreign") or sensitive_place(title, None)
                        or sensitive_place(e.get("location"), None)):  # fmt: skip
                    title = ""
                upcoming.append((start, title))
        if not upcoming:
            return now, None
        start, title = min(upcoming)
        return now, (title, start)

    def _today(self) -> date:
        return self._tools.data.today() if self._tools.data is not None else self._owner.day(self._clock())

    def _cards(self, device: Device | None, card: SendCard | None) -> Cards:
        """Today's cards for an app without `present` (the fallback): a place, a day's plan, a note."""

        async def plan(day: date) -> bool:
            if device is None or self._tools.data is None:
                return False
            view, tz = await self._tools.data.agenda_day(day), self._owner.zone
            lines = [
                e["title"]
                if e["all_day"]
                else f"{datetime.fromisoformat(e['start']).astimezone(tz):%H:%M} {e['title']}"
                for e in view["events"]
            ]
            return await device.card("plan", f"{day:%a %d %b}", [line[:200] for line in lines][:10])

        async def note(title: str, lines: list[str], kind: str) -> bool:
            return device is not None and await device.card(kind, title, lines)

        return Cards(place=card, plan=plan, note=note)

    async def asked_aloud(self, device_id: uuid.UUID) -> None:
        """In a talk, the talker just said Pisa's question aloud: the cards that turn bound are
        read out now (a spoken yes counts within 10 s of this moment)."""
        turn = self._last_turn.get(device_id)
        if self._tools.actions is None or turn is None or not turn.approvals:
            return
        try:
            await self._tools.actions.read_out_now(turn.approvals)
        except UNREACHABLE:
            unreachable(_log, "read_out", device_id=str(device_id))

    async def _acted_lately(self, device_id: uuid.UUID) -> tuple[frozenset[str], frozenset[str]]:
        """The changes Pisa made for this device in the last two hours (its decision log, by tool): a "sì,
        l'ho spostata" about one of them is true: only about a change of that kind.
        (confirmed, unconfirmed): an act whose read-back didn't confirm it (logged `unverified`, or its
        approval `not_verified`) never backs one."""
        try:
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT DISTINCT d.tool, (d.outcome = 'unverified' OR coalesce(a.verified, '') = "
                        "'not_verified') FROM pisa_decisions d LEFT JOIN approvals a ON a.id::text = d.ref "
                        "WHERE d.device_id = %s AND d.move = 'act' AND d.at > %s "
                        "AND (d.outcome IS NULL OR d.outcome IN ('done', 'unverified'))",
                        (device_id, self._clock() - ACTED_LATELY),
                    )
                ).fetchall()
            # an act they approved by a yes or a tap logs no `act`
            # row; its approval's own read-back says whether it is confirmed
            async with self._pool.connection() as conn:
                approved = await (
                    await conn.execute(
                        "SELECT DISTINCT kind FROM approvals WHERE device_id = %s AND executed_at > %s "
                        "AND verified = 'not_verified'",
                        (device_id, self._clock() - ACTED_LATELY),
                    )
                ).fetchall()
        except UNREACHABLE:  # fail closed: nothing backs an "I did it"
            unreachable(_log, "acted_lately", device_id=str(device_id))
            return frozenset(), frozenset()
        unconfirmed = {t for t, bad in rows if bad} | {
            yescard.KINDS[k].tool for (k,) in approved if k in yescard.KINDS
        }
        return frozenset(t for t, bad in rows if not bad) - unconfirmed, frozenset(unconfirmed)

    async def phone_locked(self, device_id: uuid.UUID) -> bool:
        """The phone's presence says locked (fresh). False if unknown."""
        try:
            async with self._pool.connection() as conn:
                return await presence.phone_locked(conn, device_id, self._clock())
        except UNREACHABLE as exc:
            fails_closed(_log, "phone_locked", exc, device_id=str(device_id))
            return True  # RAIL: unknown while the database is away: nothing sensitive (nothing works anyway)

    async def _body(self, device_id: uuid.UUID, *, typed: bool = False) -> str:
        """Pisa's body line and open threads: the device's state, the day's budget left, what Pisa
        waits for, showed or holds for this device. Read-only, a few small queries; '' if the database is
        unreachable (the turn goes on without them)."""
        now = self._clock()
        body = ""  # stated default: no body line this turn
        with degrades(_log, "body_line", device_id=str(device_id)):
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT app, glasses, earbuds, companion, state FROM device_presence "
                        "WHERE device_id = %s",
                        (device_id,),
                    )
                ).fetchone()
                spend = await ledger.spend_summary(conn, now, self._owner.tz)
                waiting = await (
                    await conn.execute(
                        "SELECT kind, created_at FROM approvals WHERE status = 'pending' AND expires_at > %s "
                        "AND device_id = %s ORDER BY created_at DESC LIMIT 3",
                        (now, device_id),
                    )
                ).fetchall()
                shown = await (
                    await conn.execute(
                        "SELECT tool, at FROM pisa_decisions WHERE device_id = %s AND move = 'show' AND at > "
                        "%s ORDER BY n DESC LIMIT 2",
                        (device_id, now - THREADS_SHOWN),
                    )
                ).fetchall()
                later = await (
                    await conn.execute(
                        "SELECT count(*) FROM notifications WHERE delivered_via IS NULL AND not_before > %s "
                        "AND (device_id = %s OR device_id IS NULL)",
                        (now, device_id),
                    )
                ).fetchone()
            presence = (
                body_rules.Presence(row[0], row[1], row[2], row[3], body_rules.fresh(row[4], now))
                if row is not None
                else None
            )
            line = body_rules.body_line(presence, spend.caps.daily_eur - spend.today_eur, typed=typed)
            threads = body_rules.Threads(
                waiting=[(k, at) for k, at in waiting], shown=[(t, at) for t, at in shown],
                later=int(later[0]) if later else 0,
            )  # fmt: skip
            body = line + body_rules.threads_block(threads, self._owner.zone)
        return body

    async def _has_plan(self, day: date) -> bool:
        async with self._pool.connection() as conn:
            row = await (await conn.execute("SELECT 1 FROM plans WHERE day = %s LIMIT 1", (day,))).fetchone()
        return row is not None

    async def _stored_options(self, options_id: str) -> dict[str, tuple[str, str]] | None:
        """Pisa's plan options by id, while they last: {option id: (summary, why)} (ask_choice's labels)."""
        try:
            key = uuid.UUID(options_id)
        except ValueError:
            return None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT body FROM plan_options WHERE id = %s AND expires_at > %s", (key, self._clock())
                )
            ).fetchone()
        if row is None:
            return None
        return {o["id"]: (str(o.get("summary") or ""), str(o.get("why") or "")) for o in row[0]["options"]}

    async def _folded_items(self, now: datetime) -> list[tuple[str, bool, list[str]]]:
        """What attend folded into this talk: (words, from outside text, ids), read once a
        turn; none when the database is unreachable."""
        if self.deps.attend is None:
            return []
        items: list[tuple[str, bool, list[str]]] = []  # stated default: say nothing (closed)
        with degrades(_log, "folded"):
            async with self._pool.connection() as conn:
                if await stopped_now(conn):  # nothing of Pisa's own said while stopped
                    return []
                items = await self.deps.attend.folded(conn, now)
        return items

    async def _folded_said(self, ids: list[str]) -> None:
        if self.deps.attend is None or not ids:
            return
        with degrades(_log, "folded_said"):  # stated default: brought up again next turn (twice, not never)
            async with self._pool.connection() as conn:
                await self.deps.attend.said(conn, ids, self._clock())

    def _folded(self, items: list[tuple[str, bool, list[str]]]) -> str:
        """The folded items for the instructions: Pisa's own words, and an email's as data (built in the run,
        after watch_turn: the block marks the turn). '' when there is none."""
        own = [text for text, outside, _ in items if not outside]
        outside = [text for text, outside, _ in items if outside]
        out = ""
        if own:  # its words can be an invite's or a task's title: data, and they taint
            block = data_block(
                "follow-up", "\n".join(own), what="what Pisa noted to bring up; titles may be others'"
            )
            out += f"\nTo bring up when it fits (Pisa's own, said once):\n{block.rule}\n{block.text}"
        if outside:
            block = data_block(
                "email", "\n".join(outside), what="what emails ask, in Pisa's words from the email"
            )
            out += f"\nTo bring up when it fits, from email:\n{block.rule}\n{block.text}"
        return out

    async def _cheap_today(self, lang: str, prompt_chars: int) -> tuple[str, str] | None:
        """Budget guard. RAIL: once this call would take the spend past CHEAP_AT of the day's or the
        month's AI cap (what is spent, what calls in flight reserved, and this call's own reservation), the
        conversation uses its slot's fallback model for the rest of the day (latched: it doesn't flap back
        when a reservation settles lower); never a fallback that costs more. None: the usual model. Else
        (what to say, why): the sentence the first time today, "" after it (the user hears it once), and
        "day" or "month", the cap it passed."""
        if ledger.budget() == "eval":
            return None  # an eval measures the usual model and never latches the user's day
        size = {"prompt_chars": prompt_chars, "max_output_tokens": MAX_OUTPUT_TOKENS, "tools": True}
        usual = await self._gateway.estimate_eur(SLOT, **size)
        cheaper = await self._gateway.estimate_eur(SLOT, **size, fallback=True)
        if usual is None or cheaper is None:
            return None  # nothing cheaper to go to: the caps themselves still hold
        if cheaper >= usual:
            _log.warning(
                "budget_guard_dearer_fallback"
            )  # the slot's fallback costs more: never chosen for this
            return None
        now = self._clock()
        day = now.astimezone(self._owner.zone).date().isoformat()
        async with self._pool.connection() as conn:
            said = await get_setting(conn, CHEAP_SAID_KEY)
            if said == day:  # before: the day alone
                return "", "day"
            if isinstance(said, dict) and said.get("day") == day:
                return "", str(said.get("why", "day"))  # latched for the day
            day_start, month_start = period_starts(now, self._owner.tz)
            today = await ledger.spent_since(conn, await ledger.cap_day_start(conn, day_start)) + usual
            month = await ledger.spent_since(conn, month_start) + usual
            caps = await ledger.get_caps(conn)
            if today < caps.daily_eur * CHEAP_AT and month < caps.monthly_eur * CHEAP_AT:
                return None
            why = "day" if today >= caps.daily_eur * CHEAP_AT else "month"
            await set_setting(conn, CHEAP_SAID_KEY, Jsonb({"day": day, "why": why}))
        _log.info(
            "budget_guard",
            day_share=round(float(today / caps.daily_eur), 2) if caps.daily_eur else None,
            month_share=round(float(month / caps.monthly_eur), 2) if caps.monthly_eur else None,
        )
        return CHEAP_LINE.get(lang, CHEAP_LINE["en-US"]), why

    @staticmethod
    def _history(turns: list[Turn], budget: int = history.BUDGET_TOKENS) -> list[Any]:
        """The last exchanges word for word; the older ones as Pisa's summary, made again from the turns
        each time (code, no cost: nothing to keep), so a long conversation costs about the same per call as a
        short one. Each of Pisa's replies comes with its turn's tool calls and results, whole for the
        last two turns, as receipts before them."""
        keep = 2 * history.KEEP_EXCHANGES
        older, recent = turns[:-keep] if len(turns) > keep else [], turns[-keep:]
        out: list[Any] = []
        if older:
            out.append(history.summary_message(history.digest([(t.role, _readable(t)) for t in older])))
        replies = [i for i, t in enumerate(recent) if t.role == "assistant"]
        # within the budget: the last two turns' results whole, else the last one's, else
        # receipts only
        for keep_whole in (history.KEEP_FULL_TURNS, 1, 0):
            whole = set(replies[-keep_whole:]) if keep_whole else set()
            rebuilt = list(out)
            for i, turn in enumerate(recent):
                if turn.role == "assistant" and turn.model:
                    rebuilt.extend(history.restored(turn.model, whole=i in whole))
                rebuilt.extend(build_history([turn]))
            if history.tokens(rebuilt) <= budget:
                break
        return rebuilt

    async def _live_promises(self) -> list[str] | None:
        """What the reminders still to come hold (neither fired nor cancelled), as stored; None when the
        database is unreachable (Pisa's promised lines then stay as said)."""
        with degrades(_log, "live_promises"):
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT what FROM pisa_promises WHERE fired_at IS NULL AND cancelled_at IS NULL"
                    )
                ).fetchall()
            return [str(what) for (what,) in rows]
        return None

    async def _recent_moves(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            moves = await decision_log.recent(conn)
            # Pisa's Sunday proposals still waiting are
            # Pisa's own moves too; the model looked for the one their yes answers here
            proposed = await (
                await conn.execute(
                    "SELECT type, proposed_level, proposed_at FROM autonomy_levels "
                    "WHERE proposed_level IS NOT NULL AND proposed_level <> level AND proposed_at > %s",
                    (self._clock() - autonomy_rules.Thresholds().proposal_valid,),
                )
            ).fetchall()
        waiting = [
            {"at": at, "move": f"proposed to stop asking for {type_} (level {level})",
             "tool": "accept_autonomy",
             "purpose": f"their yes to it: accept_autonomy(type={type_}, level={level})",
             "provenance": "pisa_initiative", "outcome": "waiting for their yes", "own": True}
            for type_, level, at in proposed
        ]  # fmt: skip
        return sorted([*moves, *waiting], key=lambda r: r["at"], reverse=True)

    async def _decided(
        self, moves: list[decisions.Decision], device_id: uuid.UUID, trace_id: str | None
    ) -> None:
        """The turn's moves in `pisa_decisions`. A move without a purpose is a defect the eval counts."""
        try:
            async with self._pool.connection() as conn:
                await decision_log.store(
                    conn, moves, at=self._clock(), trace_id=trace_id, device_id=device_id,
                    outside_kinds=current().tainting(),
                )  # fmt: skip
                for move in moves:  # what the device said before the row existed (a quick seen, a tap)
                    if move.key and (held := self._held_outcomes.pop(move.key, None)) is not None:
                        await decision_log.outcome(conn, move.key, *held)
        except UNREACHABLE:
            unreachable(_log, "decisions", trace_id=trace_id)
            return
        _log.info(
            "turn_decisions", trace_id=trace_id, moves=len(moves),
            purpose_missing=sum(1 for m in moves if m.purpose is None),
        )  # fmt: skip

    @staticmethod
    def _research_follow_up(earlier: "TurnWatch | None") -> bool:
        return earlier is not None and earlier.kind == "research"

    def _log_turn(self, trace_id: str, watch: "TurnWatch", speaker: Speaker | None, **more: Any) -> None:
        if watch.device is not None:
            self._previous[watch.device] = watch
        """Close → first audio per kind of reply (a holding phrase counts: the app plays it now)."""
        audio = (
            speaker.first_audio_at - watch.arrived if speaker and speaker.first_audio_at is not None else None
        )
        first = min(x for x in (audio, watch.held) if x is not None) if audio or watch.held else None
        self.latencies.append((watch.kind, first))
        _log.info(
            "turn_latency",
            trace_id=trace_id,
            reply_kind=watch.kind,
            total_s=round(time.monotonic() - watch.arrived, 3),
            holding_s=round(watch.held, 3) if watch.held is not None else None,
            progress=watch.progress or None,
            first_audio_s=round(first, 3) if first is not None else None,
            **more,
        )

    async def _again(self, device_id: uuid.UUID, text: str, lang: str) -> str | None:
        """ "Eh?": Pisa's last reply to this device, said again shorter; None when Pisa hasn't said anything
        yet."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT m.content, m.outside_kinds FROM messages m JOIN conversations c "
                    "ON c.id = m.conversation_id WHERE c.device_id = %s AND m.role = 'assistant' "
                    "AND m.created_at > %s "
                    "ORDER BY m.id DESC LIMIT 1",
                    (device_id, self._clock() - NEW_CONVERSATION_AFTER),
                )
            ).fetchone()
        if row is None or not str(row[0]).strip():
            return None
        said = form.again(str(row[0]))
        # the repeat retells what the reply did: it carries its taint
        await self.remember(device_id, text, lang, said, lang, outside=list(row[1] or []))
        return said

    async def _answered(self, device_id: uuid.UUID, text: str, lang: str, spoken: bool = True) -> str | None:
        """A short yes or no right after Pisa asked an approval in this device's previous turn is
        decided here, before any model call (nothing Pisa read can approve anything); None when it isn't
        one."""
        before = self._last_turn.get(device_id)
        if self._tools.actions is None or before is None or before.profile_asked:
            return None
        if not before.approvals and not before.acted:
            return None
        # Newest wins: a check-in, or anything Pisa said unasked since, takes the yes.
        if before.asked is None or self.unasked.since(device_id, before.asked):
            return None
        now = TurnState(before.conversation_id, text, device=device_id, spoken=spoken)  # one yes answers one
        token = current_turn.set(now)
        try:
            said = await self._tools.actions.answer(
                device_id, text, lang, before.approvals, before.acted, before.proposed, spoken=spoken
            )
        finally:
            current_turn.reset(token)
        if said is None:
            return None
        now.asked = self.unasked.mark()
        self._last_turn[device_id] = now  # a correction's new question is answered in the next turn
        await self.remember(device_id, text, lang, said, lang, quoted=True)
        _log.info("approval_answered", device_id=str(device_id))
        return said

    async def _voice2_heard(
        self, device_id: uuid.UUID, text: str, lang: str, yes_by_code: bool = True
    ) -> str | None:
        """`pisa.services.voice2`: their answer to Pisa's open cue-question or trait, "come stai?",
        "com'è il tuo carattere?", answered by code. None: not one of these (the conversation goes on)."""
        if self.deps.perceive is None:
            return None
        before = self._last_turn.get(device_id)
        said = await self.deps.perceive.heard(
            device_id,
            text,
            lang,
            before,
            # newest wins: a check-in, a reminder or an approval Pisa said since takes their "sì"
            newer=before is not None
            and before.asked is not None
            and self.unasked.since(device_id, before.asked),
            # Pisa's last reply asked them something else ("Cosa gli scrivo?"): their words answer that
            asked_else=before is not None
            and (before.questioned or bool(before.asked_order) or before.profile_asked),
            yes_by_code=yes_by_code,
        )
        if said is None:
            return None
        now = TurnState(before.conversation_id if before else uuid.uuid4(), text)
        now.asked = self.unasked.mark()
        self._last_turn[device_id] = now
        self.deps.perceive.bind(device_id, now)  # Pisa's question in this reply is answered in the next turn
        await self.remember(device_id, text, lang, said, lang, quoted=True)
        _log.info("voice2_heard", device_id=str(device_id))
        return said

    async def _voice_heard(
        self,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        yes_by_code: bool = True,
        device: Device | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        """`pisa.domain.voice1`: their words to the knock, the morning and the tragitto, by code.

        - an open offer is Pisa's newest question to this device (newest wins): "dimmi" (or a bare yes, in
          the yes chain) opens it, "dopo" or "basta" closes it, "dammi tutto" opens the night's drawer;
        - no offer open, only when Pisa's last thing to them was its own reading or today's briefing, and
          never after a turn that asked them something or holds a change for their yes ("go ahead" to
          "Mando la mail a Marco?" is the approval's): "dimmi"/"cosa c'è?" in the morning speaks the morning;
          "dammi tutto" opens the night's drawer; "dopo"/"basta" right after Pisa's reading ends it.
        None: not one of these (the conversation goes on)."""
        voice = self.deps.knock
        if voice is None:
            return None
        dimmi, tutto = voice_rules.is_dimmi(text), voice_rules.is_tutto(text)
        later, enough = voice_rules.is_later(text), voice_rules.is_enough(text)
        # a bare yes or no answers an open offer only (in a talk, right after Pisa said it aloud)
        yes = yes_by_code and is_short_yes(text) and not action_rules.is_tap(text)
        no = yes_by_code and action_rules.is_short_no(text) and not action_rules.is_tap(text)
        if not (dimmi or tutto or later or enough or yes or no):
            return None
        before = self._last_turn.get(device_id)
        ask_id = (
            self._tools.rituals[0].newest_ask(device_id, before) if self._tools.rituals is not None else None
        )
        kind = await voice.open_kind(ask_id) if ask_id is not None else None
        said: str | None = None
        reading = False
        knock_read = False  # this reading is the knock's (its last part answers "ok"/"no" to a miss's reason)
        if self.deps.knock is not None:
            self.deps.knock.read_last = None
        if kind is not None and ask_id is not None:
            if dimmi or yes:
                said, reading = await self._tools.rituals[0].answer(ask_id, True, "voice"), True  # type: ignore[index]
                knock_read = kind == "knock"
                if dimmi:
                    await voice.welcome()
            elif tutto:
                await voice.close(ask_id, "tutto")
                said, reading = await self._drawer(device_id, text, lang, device, reply_to), True
            elif later:
                await voice.close(ask_id, "later")
                said = voice_rules.word(voice_rules.LATER, lang)
            else:  # "basta", "no": closed (theirs to decline); "basta" pauses that kind until their "dimmi"
                said = await self._tools.rituals[0].answer(ask_id, False, "voice")  # type: ignore[index]
                if enough:
                    await voice.hush(kind)
        elif before is not None and (before.approvals or before.proposed or before.questioned):
            return None  # their words answer Pisa's question or its held change, never the morning
        else:
            after_reading = before is not None and self._voice_read.get(device_id) is before
            # right after Pisa's night-shift story, "dammi tutto" opens the drawer it comes from
            after_story = (
                tutto
                and self.deps.night is not None
                and (self.deps.night.opened(device_id, before) or ("",))[0] == "story"
            )
            if not (after_reading or after_story or await voice.briefed_last(device_id)):
                return None  # Pisa's last thing wasn't its own reading or briefing
            if tutto:
                said, reading = await self._drawer(device_id, text, lang, device, reply_to), True
            elif dimmi and await voice.in_morning():
                said = await voice.morning(lang) or voice_rules.word(voice_rules.NOTHING, lang)
                reading = knock_read = True
                await voice.welcome()
            elif (later or enough) and after_reading:
                said = voice_rules.word(voice_rules.LATER if later else voice_rules.ENOUGH, lang)
        if said is None:
            return None
        now = TurnState(before.conversation_id if before else uuid.uuid4(), text)
        now.asked = self.unasked.mark()
        self._last_turn[device_id] = now
        if reading or (kind is not None and later):
            # Pisa's reading, or its "dopo" to the knock: their next "basta" or "dimmi" is about it
            self._voice_read[device_id] = now
            self._read_last[device_id] = (
                getattr(self.deps.knock, "read_last", None)
                if knock_read and self.deps.knock is not None
                else None
            )
        else:
            self._voice_read.pop(device_id, None)
            self._read_last.pop(device_id, None)
        if ask_id is not None and kind is not None:
            self.unasked.said(device_id, None)  # answered: a later yes isn't this one's
        await self.remember(device_id, text, lang, said, lang, quoted=True)
        _log.info("voice1_heard", device_id=str(device_id), offer=kind, reading=reading)
        return said

    async def _curious(
        self, *args: Any, outside: Callable[[], bool], open_question: Callable[[], bool]
    ) -> str | None:
        """Il prurito's line, or None: nothing of it ever fails a reply the model made (voice6 merge): its
        checks are computed here, inside the guard, and its DB work too."""
        assert self.deps.prurito is not None
        try:
            return await self.deps.prurito.decide(*args, outside=outside(), open_question=open_question())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the line is optional: the reply goes out without it
            _log.warning("voice6_decide_failed", reason=type(exc).__name__)
            return None

    async def _reason_heard(self, device_id: uuid.UUID, text: str, lang: str) -> str | None:
        """Their wave or their yes to the miss's reason Pisa's reading just said (`Prurito.heard`)."""
        if self.deps.prurito is None:
            return None
        before = self._last_turn.get(device_id)
        after_reading = before is not None and self._voice_read.get(device_id) is before
        # a bare "ok" answers the reason only when it was the reading's last part
        last = after_reading and self._read_last.get(device_id) == "perche"
        said = await self.deps.prurito.heard(text, lang, after_reading=after_reading, last=last)
        if said is None:
            return None
        now = TurnState(before.conversation_id if before else uuid.uuid4(), text)
        now.asked = self.unasked.mark()
        self._last_turn[device_id] = now
        self._voice_read.pop(device_id, None)
        await self.remember(device_id, text, lang, said, lang, quoted=True)
        return said

    async def _drawer(
        self, device_id: uuid.UUID, text: str, lang: str, device: Device | None, reply_to: str | None
    ) -> str:
        """ "Dammi tutto" by voice (voice1's word, one drawer): the night's whole drawer as a
        sheet on the phone, and one line. Pull has no limit; nothing of it enters a prompt."""
        drawer = await self.deps.night.drawer(lang) if self.deps.night is not None else None
        if drawer is None:
            return voice_rules.word(voice_rules.NO_TUTTO, lang)
        line = voice3_rules.drawer_line(drawer, lang)
        if not drawer.items():
            return line  # nothing to show: the line says so
        sheet, _ = voice3_rules.drawer_sheet(drawer, lang)
        SheetData.model_validate(sheet)  # the limits, before it leaves
        presenter = Presenter(
            device=device, words=text, lang=lang, today=self._today(),
            objects=self.objects.setdefault(device_id, {}), reply_to=reply_to, clock=self._clock,
        )  # fmt: skip
        ack = await presenter.show("dammi_tutto", "sheet", f"sheet:{uuid.uuid4()}", line, "take", data=sheet)
        if ack.status in ("unavailable", "unsupported"):
            return voice_rules.word(voice3_rules.NO_SCREEN, lang)
        return line

    async def _memory_heard(self, device_id: uuid.UUID, text: str, lang: str) -> str | None:
        """`pisa.domain.episodes.heard`: their closed phrases about what recall just answered, by code,
        only when Pisa's last turn on this device was the one whose recall told ONE episode, never while a
        question, an approval or a held change is open, never when Pisa's last reply read outside text; None
        (and `_last_turn` untouched): the model's."""
        memory = self.deps.episodes
        found = episode_rules.heard(text) if memory is not None else None
        if memory is None or found is None:
            return None
        before = self._last_turn.get(device_id)
        last = memory.recalled(device_id)
        if (
            before is None
            or last is None
            or not before.trace_id
            or last.turn_id != before.trace_id
            or last.answer not in ("one", "list")
            or len(last.ids) != 1
        ):
            return None  # not right after recall answered one episode: the path is not open
        if before.approvals or before.proposed or before.questioned:
            return None  # Pisa asked something, or holds a change: their words answer that
        if before.asked is None or self.unasked.since(device_id, before.asked):
            return None  # Pisa said something unasked since: "it" may be that
        if self._tools.rituals is not None and self._tools.rituals[0].newest_ask(device_id, before):
            return None
        if self._tools.actions is not None and any(
            not r.get("expired") for r in await self._tools.actions.waiting(device_id)
        ):
            return None
        if await memory.read_outside(before.conversation_id):
            return None  # Pisa's last reply read outside text: "it" may be that text
        kind, name = found
        said = await memory.heard(kind, name, last.ids[0], lang, None)
        if said is None:
            return None
        now = TurnState(before.conversation_id, text)
        now.asked = self.unasked.mark()
        self._last_turn[device_id] = now
        memory.done_with(device_id)
        await self.remember(device_id, text, lang, said, lang)
        await memory.link_message(kind, last.ids[0], before.conversation_id)
        if kind == "forget":  # the turn that recalled it (their question, Pisa's retelling) goes too
            await memory.forget_turn(before.conversation_id, before.text, text, device_id)
        _log.info("memory_heard", device_id=str(device_id), path=kind)
        return said

    async def _night_heard(
        self, device_id: uuid.UUID, text: str, lang: str, device: Device | None, reply_to: str | None
    ) -> str | None:
        """`pisa.domain.voice3`: their closed phrases about the night, by code, only on the path Pisa's
        own last thing opened, never while another question, an approval or a held change is open; None (and
        `_last_turn` untouched): the model's."""
        night = self.deps.night
        kind = voice3_rules.heard(text) if night is not None else None
        if night is None or kind is None:
            return None
        before = self._last_turn.get(device_id)
        # Pisa's own question on this path (their p) is the one exception
        mine = night.opened(device_id, before)
        if before is not None and (
            before.approvals or before.proposed or (before.questioned and mine is None)
        ):
            return None  # their words answer Pisa's question or its held change
        if (
            mine is None
            and self._tools.rituals is not None
            and self._tools.rituals[0].newest_ask(device_id, before)
        ):
            return None  # a ritual's or the knock's question is Pisa's newest: its answer, never this
        if self._tools.actions is not None and any(
            not r.get("expired") for r in await self._tools.actions.waiting(device_id)
        ):
            return None  # an approval waits for their yes: nothing here takes their turn
        after_reading = before is not None and self._voice_read.get(device_id) is before
        story_open = kind == "story" and (
            after_reading
            or (self.deps.knock is not None and await self.deps.knock.briefed_last(device_id))
            or await night.idle(device_id)
        )  # the story's path is Pisa's own reading or briefing, or a talk idle for hours
        reply = await night.heard(
            device_id, kind, text, lang, before, after_reading=after_reading, story_open=story_open
        )
        if reply is None:
            return None
        now = TurnState(before.conversation_id if before else uuid.uuid4(), text)
        now.asked = self.unasked.mark()
        now.questioned = reply.asks
        self._last_turn[device_id] = now
        self._voice_read.pop(device_id, None)
        night.mark(device_id, now, reply.opens)
        await self.remember(device_id, text, lang, reply.said, lang, quoted=True)
        if reply.bet_id is not None:
            await night.link_message(device_id, reply.bet_id)
        _log.info(
            "voice3_heard", device_id=str(device_id), path=kind, opens=reply.opens[0] if reply.opens else None
        )
        return reply.said

    async def _ceo_heard(self, device_id: uuid.UUID, text: str, lang: str) -> str | None:
        """`pisa.domain.voice4`: their closed phrases to il consiglio, la prova generale and "sciame su
        questo", by code, only on the path Pisa's own last thing opened; never while another question, an
        approval or a held change is open. None (and `_last_turn` untouched): the next reflex's or the
        model's."""
        ceo = self.deps.ceo
        kind = voice4_rules.heard(text) if ceo is not None else None
        if ceo is None or kind is None:
            return None
        before = self._last_turn.get(device_id)
        talk = ceo.talking(device_id) if kind in ("avanti", "basta") else None
        if talk is not None and not ceo.said_last(device_id, before):
            # another path answered since this path's last turn (the model, voice1, voice3, a yes
            # that wasn't la prova's own): the talk is over, and these words are that path's
            await ceo.drop(device_id)
            return None
        if kind in ("avanti", "basta") and talk is None:
            return None
        mine = ceo.opened(device_id, before)
        ask = (
            self._tools.rituals[0].newest_ask(device_id, before) if self._tools.rituals is not None else None
        )
        if before is not None and (before.approvals or before.proposed):
            return None  # their words answer Pisa's held change
        if (
            before is not None
            and before.questioned
            and not (mine is not None or ceo.said_last(device_id, before))
        ):
            return None  # Pisa's last reply asked them something else: their words answer it
        if ask is not None and not (talk == "prova" and ceo.prova_ask(device_id, ask)):
            return None  # a ritual's question (never la prova's own) is Pisa's newest: its answer, never this
        if ask is not None and voice4_rules.yes_like(text):
            return None  # "sì, avanti" to la prova's open fix question is a yes: the model's
        if self._tools.actions is not None and any(
            not r.get("expired") for r in await self._tools.actions.waiting(device_id)
        ):
            return None  # an approval waits for their yes: nothing here takes their turn
        after_reading = before is not None and self._voice_read.get(device_id) is before
        reply = await ceo.heard(device_id, kind, lang, before, after_reading=after_reading)
        if reply is None:
            return None
        now = TurnState(before.conversation_id if before else uuid.uuid4(), text)
        now.asked = self.unasked.mark()
        now.questioned = reply.asks
        self._last_turn[device_id] = now
        self._voice_read.pop(device_id, None)
        ceo.mark(device_id, now, reply.opens)
        if self.deps.night is not None:
            self.deps.night.mark(device_id, now, ("p", reply.p_ref) if reply.p_ref else None)
        await self.remember(device_id, text, lang, reply.said, lang, quoted=True)
        if reply.ask is not None and self._tools.rituals is not None:
            ceo.asked(device_id, reply.ask[0])
            # their bare yes: the Ghost Run's
            self._tools.rituals[0].bind(device_id, reply.ask[0], now, reply.ask[1])
        _log.info("voice4_heard", device_id=str(device_id), path=kind, talk=talk)
        return reply.said

    async def _ritual_answered(self, device_id: uuid.UUID, text: str, lang: str) -> str | None:
        """A bare yes or no when Pisa's newest question is one of the rituals'
        (tomorrow's fix, an idea, a hunch) answers it by code: the fix runs on the yes, nothing on a no."""
        yes = is_short_yes(text)
        if (
            self._tools.rituals is None
            or not (yes or action_rules.is_short_no(text))
            or action_rules.is_tap(text)
        ):
            return None
        before = self._last_turn.get(device_id)
        ask = self._tools.rituals[0].newest_ask(device_id, before)  # la prova's own question, or not
        said = await self._tools.rituals[0].answer_newest(device_id, yes, before)
        if said is None:
            return None
        now = TurnState(before.conversation_id if before else uuid.uuid4(), text)
        now.asked = self.unasked.mark()
        self._last_turn[device_id] = now
        if self.deps.ceo is not None and ask is not None:
            # their yes to la prova's own fix question keeps its talk
            self.deps.ceo.answered(device_id, ask, now)
        await self.remember(device_id, text, lang, said, lang)
        _log.info("ritual_answered_by_code", device_id=str(device_id), yes=yes)
        return said

    async def _check_in_answered(self, device_id: uuid.UUID, text: str, lang: str) -> str | None:
        """A bare yes or no when Pisa's newest question is a yes/no check-in
        ("Sei passato in farmacia?") is that check-in's answer, by code: never an older question's (the
        newest wins, the same newest as `_latest_question`). None when it isn't one (the model goes on)."""
        yes = is_short_yes(text)
        if (
            self._tools.data is None
            or not (yes or action_rules.is_short_no(text))
            or action_rules.is_tap(text)
        ):
            return None
        before = self._last_turn.get(device_id)
        if before is not None and before.asked is not None:
            if not self.unasked.since(device_id, before.asked):
                return None  # Pisa said something itself after the check-in: that one takes the yes
        check_in = await self._tools.data.latest_check_in()
        if check_in is None or {c["id"] for c in check_in["choices"]} != {"done", "not_done"}:
            return None  # RAIL: only a yes/no check-in; "missed" has five answers, the model asks which
        if self.unasked.newest_check_in(device_id) != str(check_in["id"]):
            # RAIL: only when this check-in is the newest thing Pisa said to this
            # device unasked: not after a reminder or an approval, not one never delivered, not after a
            # restart (nothing known): then the model reads the conversation.
            return None
        choice = "done" if yes else "not_done"
        try:
            await self._tools.data.answer_check_in(check_in["id"], choice)
        except PisaError:
            return None  # answered or closed meanwhile: the model goes on
        said = follow_rules.answered(choice, lang)
        now = TurnState(before.conversation_id if before else uuid.uuid4(), text)
        now.asked = self.unasked.mark()
        self._last_turn[device_id] = now
        await self.remember(device_id, text, lang, said, lang)
        _log.info("check_in_answered_by_code", device_id=str(device_id), choice=choice)
        return said

    def parts_of(self, device_id: uuid.UUID, trace_id: str) -> dict[str, Any] | None:
        """Where the time of the device's turn `trace_id` went (None: code answered it)."""
        turn = self._last_turn.get(device_id)
        parts = turn.parts if turn is not None else None
        return parts if parts is not None and parts.get("trace") == trace_id else None

    def record_of(self, device_id: uuid.UUID, trace_id: str) -> tuple[int, int]:
        """The device's turn `trace_id`: (tools that said it wasn't done, claims rewritten into
        what happened); (0, 0) for a turn code answered."""
        turn = self._last_turn.get(device_id)
        if turn is None or turn.trace_id != trace_id:
            return 0, 0
        return len(turn.failures), turn.unbacked

    async def spoken(self, device_id: uuid.UUID, trace_id: str, text: str) -> bool:
        """A talk's turn whose reply went in silently (Pisa's voice had answered by itself): its stored reply
        becomes what the voice said (`text`), so the chat holds what was heard. Its correction line, if any,
        stays its own message. False when the turn's reply isn't known here (another path stored it, or it is
        long gone)."""
        message_id = self._reply_rows.get((device_id, trace_id))
        if message_id is None or not text.strip():
            return False
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE messages SET content = %s WHERE id = %s AND role = 'assistant'",
                (text.strip()[:SPOKEN_MAX], message_id),
            )
        _log.info("talk_reply_stored_as_said", trace_id=trace_id, replaced=cur.rowcount)
        return cur.rowcount == 1

    def correction_of(self, device_id: uuid.UUID, trace_id: str) -> str:
        """The device's turn `trace_id`'s correction line, said after its reply ("" for none)."""
        turn = self._last_turn.get(device_id)
        return turn.correction if turn is not None and turn.trace_id == trace_id else ""

    def claimed(self, device_id: uuid.UUID) -> bool:
        """The device's latest reply claimed a change Pisa's record backs (or answered Pisa's question with
        a change made by code): the talker may confirm it (`Thought.claimed_done`)."""
        turn = self._last_turn.get(device_id)
        if turn is None:
            return False
        if turn.acted or turn.done:
            return True  # this turn's own change
        # A claim that stood on Pisa's acts of the last 2 h only backs the talker when Pisa read its record
        # back, or the user asked about a past change ("l'hai messo davvero?"); never a new ask answered
        # "fatto" ("sposta la cena" → "Fatto, ho spostato la cena" with nothing changed).
        return turn.claimed and (bool(READ_BACK & set(turn.tools)) or _asks_what_was_done(turn.text))

    def asks_owner(self, device_id: uuid.UUID) -> bool:
        """The device's latest turn asked the user a yes (an approval) or read a draft
        back ("Lo mando?"): Pisa's voice says that reply whole, never shortened."""
        turn = self._last_turn.get(device_id)
        # a write held for their yes ("Vuoi che ricordi…?") is Pisa's question too: said whole
        return turn is not None and bool(
            turn.approvals or turn.proposed or turn.profile_asked or DRAFT_TOOLS & set(turn.tools)
        )

    def asked_nothing(self, device_id: uuid.UUID) -> None:
        """An exchange answered outside a turn (the briefing phrase): an earlier question is no longer the
        newest thing Pisa said."""
        self._last_turn.pop(device_id, None)

    async def _different_now(self) -> int | None:
        """Row 18: "something different" is the model's choice (about 1 places card in 5), while
        the user leaves it on (RAIL: settings profile.something_different). The places cards shown since
        the last different one, or None when it is off."""
        async with self._pool.connection() as conn:
            if not (await self._owner.settings(conn))["profile"]["something_different"]:
                return None
            row = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_decisions WHERE move = 'show' AND tool = 'show_places' AND n "
                    "> coalesce((SELECT max(n) FROM pisa_decisions WHERE rail = 'something different'), 0)"
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def remember(
        self,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        reply: str,
        reply_lang: str,
        *,
        quoted: bool = False,
        outside: list[str] | None = None,
    ) -> None:
        """An exchange answered without the agent (the briefing phrase), kept in history for what follows.
        `quoted`: Pisa's reply carries titles from its tools (the yes by code), kept as data.
        `outside`: the outside text it retells, which taints later
        turns while it is in the history."""
        async with self._pool.connection() as conn:
            conversation_id = await _conversation_for(conn, device_id, self._clock())
            await _add_message(conn, conversation_id, "user", text, lang, self._clock())
            await _add_message(
                conn, conversation_id, "assistant", reply, reply_lang, self._clock(), quoted or bool(outside),
                None, outside,
            )  # fmt: skip

    async def mark(self, device_id: uuid.UUID) -> int:
        """Where the device's history is now (its newest message id), before a turn (continuation)."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT coalesce(max(m.id), 0) FROM messages m JOIN conversations c "
                    "ON c.id = m.conversation_id WHERE c.device_id = %s",
                    (device_id,),
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def new_conversation(self, device_id: uuid.UUID) -> uuid.UUID:
        """The app's "Nuova conversazione": the device's next turn starts fresh for the model, nothing
        deleted. A new `conversations` row now (the latest, so `_conversation_for` answers in it: no earlier
        turn reaches the model), and the device's short-lived state forgotten, as a new conversation after
        NEW_CONVERSATION_AFTER of silence starts without it: its last turn (`continues`), what Pisa last said,
        the objects it showed and their taps, the reading in progress, the holding phrases, and the reflex's
        shared state (its lines, promises, facts). Long-term memory (Memoria, episodes, tasks, reminders)
        stays. Twice in a row: the empty conversation just started is kept, not a second one."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            latest = await (
                await conn.execute(
                    "SELECT c.id, c.last_message_at, EXISTS (SELECT 1 FROM messages m "
                    "WHERE m.conversation_id = c.id) FROM conversations c WHERE c.device_id = %s "
                    "ORDER BY c.last_message_at DESC "
                    "LIMIT 1",
                    (device_id,),
                )
            ).fetchone()
            if latest is not None and not latest[2] and now - latest[1] <= NEW_CONVERSATION_AFTER:
                conversation_id = latest[0]  # already a fresh one
            else:
                conversation_id = uuid.uuid4()
                # the latest by last_message_at, even on a clock that hasn't moved since the last message
                at = max(now, latest[1] + timedelta(microseconds=1)) if latest is not None else now
                await conn.execute(
                    "INSERT INTO conversations (id, device_id, started_at, last_message_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (conversation_id, device_id, at, at),
                )
        self.forget_short_lived(device_id)
        _log.info("conversation_new", device_id=str(device_id))
        return conversation_id

    def forget_short_lived(self, device_id: uuid.UUID) -> None:
        """The device's short-lived conversation state, forgotten: its last turn (`continues`), what Pisa last
        said, the objects it showed and their taps, the reading in progress, the holding phrases, and the
        reflex's shared state (its lines, promises, facts). A new conversation (`new_conversation`) and a
        deleted one (`DELETE /v1/conversations/{threadId}`, security review CD-1) both run it, through
        `pisa.api.conversations.start_fresh`."""
        for kept in (self._last_turn, self._reply_said, self._previous, self._last_hold, self._held_at,
                     self._replied_at, self.objects, self._voice_read, self._read_last):  # fmt: skip
            kept.pop(device_id, None)
        for tap in [k for k in self._taps if k[0] == device_id]:
            del self._taps[tap]
        if self.deps.reflex is not None:
            self.deps.reflex.forget(device_id)

    async def settle(self) -> None:
        """Cancelled replies still being saved have landed (before a delete, never after it)."""
        if self._saving:
            await asyncio.gather(*list(self._saving), return_exceptions=True)

    async def drop_after(self, device_id: uuid.UUID, mark: int) -> int:
        """A turn the user went on with (`continues`): its words and the reply it got so far leave the
        history; the joined words are answered as one turn."""
        await self.settle()  # a cancelled reply still being saved lands before the delete, never after it
        async with self._pool.connection() as conn, conn.transaction():
            # a bet or a swarm request linked during that turn goes with it (0090 made the link SET NULL)
            await retention.forget_cited(conn, await retention.device_messages(conn, device_id, mark))
            cur = await conn.execute(
                "DELETE FROM messages m USING conversations c WHERE c.id = m.conversation_id "
                "AND c.device_id = %s AND m.id > %s",
                (device_id, mark),
            )
        return cur.rowcount

    async def _day_tasks(self, *, tomorrow: bool) -> tuple[list[str], int, int]:
        """Their open tasks for today (overdue ones too, and those with no due date: get_tasks(today)'s set,
        `today_split`) or tomorrow (dated only), as the store holds them, the most urgent first, low priority
        left out (as get_tasks): the titles Pisa may name (the dated ones, then UNDATED_LISTED with no date at
        most), how many more it counts without naming (a title a stranger wrote: an email's; with no date,
        past the cap), and how many of all have no date."""
        now = self._clock()
        tz = ZoneInfo(self._owner.tz)
        day = now.astimezone(tz).date() + timedelta(days=1 if tomorrow else 0)
        start = datetime.combine(day, datetime.min.time(), tz)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT title, source_kind FROM tasks WHERE status = 'open' AND deleted_at IS NULL "
                    "AND priority <> %s AND due_at IS NOT NULL AND due_at < %s AND (%s OR due_at >= %s) "
                    "ORDER BY priority, due_at, created_at, title LIMIT 50",
                    (LOW, start + timedelta(days=1), not tomorrow, start),
                )
            ).fetchall()
            undated: list[Any] = []
            if not tomorrow:  # one timed to start on a later day isn't today's (as `today_split`)
                undated = await (
                    await conn.execute(
                        "SELECT title, source_kind, count(*) OVER () FROM tasks WHERE status = 'open' "
                        "AND deleted_at IS NULL AND priority <> %s AND due_at IS NULL "
                        "AND (expected_from IS NULL OR expected_from < %s) "
                        "ORDER BY priority, created_at, title LIMIT %s",
                        (LOW, start + timedelta(days=1), UNDATED_LISTED),
                    )
                ).fetchall()
        named = [str(r[0]) for r in rows if r[1] != "email"]
        loose = [str(r[0]) for r in undated if r[1] != "email"]
        many = int(undated[0][2]) if undated else 0
        return [*named, *loose], len(rows) - len(named) + many - len(loose), many

    async def _task_states(self) -> list[tuple[str, str]]:
        """Their open and done tasks as the store holds them now ((title, status)), the latest first."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT title, status FROM tasks WHERE status IN ('open', 'done') "
                    "ORDER BY updated_at DESC LIMIT %s",
                    (TASK_STATES,),
                )
            ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    async def _save_reply(
        self,
        conversation_id: uuid.UUID,
        reply: str,
        lang: str,
        *,
        quoted: bool = False,
        model: list[Any] | None = None,
        outside: list[str] | None = None,
    ) -> int:
        """The reply stored; its message id."""
        async with self._pool.connection() as conn:
            message_id = await _add_message(
                conn, conversation_id, "assistant", reply, lang, self._clock(), quoted, model, outside
            )
            await _keep_values(conn, conversation_id, self._clock())
        return message_id

    async def _kept_findings(self, conversation_id: uuid.UUID) -> tuple[dict[str, Any], str] | None:
        """The conversation's last research findings and when (their zone, "14:05")."""
        kept: tuple[dict[str, Any], str] | None = None  # stated default: none kept
        with degrades(_log, "kept_findings", conversation_id=str(conversation_id)):
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT data, created_at FROM research_findings WHERE conversation_id = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (conversation_id,),
                    )
                ).fetchone()
            if row is None:
                return None
            kept = dict(row[0]), f"{row[1].astimezone(self._owner.zone):%a %H:%M}"
        return kept

    async def _keep_findings(self, conversation_id: uuid.UUID, found: dict[str, Any]) -> None:
        """Keeping them never fails a turn that was answered (a log line, ids only)."""
        try:
            await self._keep_findings_now(conversation_id, found)
        except (*UNREACHABLE, DatabaseError, PisaError) as err:  # a row refused too
            _log.warning("findings_not_kept", conversation_id=str(conversation_id), error=type(err).__name__)

    async def _keep_findings_now(self, conversation_id: uuid.UUID, found: dict[str, Any]) -> None:
        """What a research turn found, with its conversation: its retention, its erase family (messages);
        the conversation's last KEPT_ROWS only."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO research_findings (id, conversation_id, created_at, data) "
                "VALUES (%s, %s, %s, %s)",
                (uuid.uuid4(), conversation_id, self._clock(), Jsonb(findings_rules.bounded(found))),
            )
            await conn.execute(
                "DELETE FROM research_findings WHERE conversation_id = %s AND id NOT IN (SELECT id FROM "
                "research_findings WHERE conversation_id = %s ORDER BY created_at DESC LIMIT %s)",
                (conversation_id, conversation_id, findings_rules.KEPT_ROWS),
            )

    async def _words_only(self, text: str, tapped: bool) -> bool:
        """Whether this turn shows no drawing (code turns a drawing
        block into its table or list, and the box is refused). "Disegna meno" / "solo testo" is kept until
        "disegna pure"; while it holds, only words that ask for a drawing draw. A tap's label sets nothing."""
        words_only = not tapped and visual_rules.plain(text)
        once = not tapped and visual_rules.plain_once(text)  # this turn only, no setting
        answer = words_only or once  # stated default: this turn's own words
        with degrades(_log, "words_only"):
            async with self._pool.connection() as conn:
                if words_only:
                    await set_setting(conn, WORDS_ONLY_KEY, Jsonb({"since": self._clock().isoformat()}))
                    return True
                if not tapped and visual_rules.draw_again(text):
                    await delete_setting(conn, WORDS_ONLY_KEY)
                    return False
                held = await get_setting(conn, WORDS_ONLY_KEY) is not None
            answer = once or (held and (tapped or not visual_rules.asks(text)))
        return answer

    async def _kept_drawing(self, conversation_id: uuid.UUID) -> tuple[dict[str, Any], str] | None:
        """The conversation's last drawing (its code, data, kind) and when."""
        kept: tuple[dict[str, Any], str] | None = None  # stated default: none kept
        with degrades(_log, "kept_drawing", conversation_id=str(conversation_id)):
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT id, title, summary, kind, html, data, created_at, revision, basis "
                        "FROM drawings WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1",
                        (conversation_id,),
                    )
                ).fetchone()
            if row is None:
                return None
            drawing = {"drawing_id": str(row[0]), "title": row[1], "summary": row[2], "kind": row[3],
                       "html": row[4], "data": row[5], "revision": row[7], "basis": row[8]}  # fmt: skip
            kept = drawing, f"{row[6].astimezone(self._owner.zone):%a %H:%M}"
        return kept

    async def _keep_drawing(self, conversation_id: uuid.UUID, shown: dict[str, Any]) -> None:
        """A drawing Pisa showed, with its conversation (its retention, its erase family: messages); the last
        drawing_rules.KEPT_ROWS. A redraw keeps its id: the row becomes the new revision.
        Keeping it never fails a turn that was answered (a log line, ids only)."""
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO drawings (id, conversation_id, created_at, title, summary, kind, html, "
                    "data, revision, basis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET created_at = EXCLUDED.created_at, "
                    "title = EXCLUDED.title, summary = EXCLUDED.summary, kind = EXCLUDED.kind, "
                    "html = EXCLUDED.html, data = EXCLUDED.data, revision = EXCLUDED.revision, "
                    "basis = EXCLUDED.basis "
                    "WHERE drawings.conversation_id = EXCLUDED.conversation_id",
                    (uuid.UUID(shown["drawing_id"]), conversation_id, self._clock(), shown["title"],
                     shown["summary"], shown["kind"], shown["html"], Jsonb(shown["data"]),
                     int(shown.get("revision") or 0), shown.get("basis")),
                )  # fmt: skip
                await conn.execute(
                    "DELETE FROM drawings WHERE conversation_id = %s AND id NOT IN (SELECT id FROM drawings "
                    "WHERE conversation_id = %s ORDER BY created_at DESC LIMIT %s)",
                    (conversation_id, conversation_id, drawing_rules.KEPT_ROWS),
                )
        except (*UNREACHABLE, DatabaseError, PisaError) as err:
            _log.warning("drawing_not_kept", conversation_id=str(conversation_id), error=type(err).__name__)

    async def drawing_ask(self, device_id: uuid.UUID, drawing_id: str, label: str) -> tuple[str, str] | None:
        """ "ask Pisa about this", a tap in a drawing Pisa showed this device: their turn's
        words ("Sul disegno: <label>", marked `origin: drawing` by the caller), or None when the drawing isn't
        one Pisa showed it (or the label is empty). The label is the page's text (a `data-pisa-ask` value or
        what it passed to Pisa.ask): quarantined, never their command."""
        shown = self.objects.get(device_id, {}).get(f"drawing:{drawing_id}")
        label = drawing_rules.ask_label(label)
        if shown is None or not label:
            _log.info("drawing_ask_unknown", device_id=str(device_id), known=shown is not None)
            return None
        return drawing_rules.ask_words(label, shown.lang), shown.lang

    async def _kept_values(self, conversation_id: uuid.UUID) -> list[tuple[str, str, str]]:
        """The outside values the conversation's earlier turns read."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT what, value, source FROM outside_values WHERE conversation_id = %s",
                    (conversation_id,),
                )
            ).fetchall()
        return [(str(w), str(v), str(s)) for w, v, s in rows]


async def _keep_values(conn: AsyncConnection, conversation_id: uuid.UUID, now: datetime) -> None:
    """The turn's outside values kept with its conversation (normalised values
    and where each came from: what taints later acts; no text)."""
    known = current().values()
    if not known:
        return
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO outside_values (conversation_id, what, value, source, noted_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (conversation_id, what, value) DO NOTHING",
            [
                (conversation_id, what, value[:300], source[:120], now)
                for (what, value), source in known.items()
            ],
        )


def _changed(turn: TurnState) -> bool:
    """This turn made a change (an act this turn, or one its first part made)."""
    return decisions.acted() or bool(turn.done)


ACTED_LATELY = timedelta(hours=2)  # a past-tense claim may be about a change Pisa made this long ago

# The tools that read Pisa's own record back ("cosa ho fatto?", "perché l'hai spostata?"): what they say Pisa
# did is true, and said in the past.
READ_BACK = frozenset({"what_did_i_do", "my_recent_decisions"})
# outside text that can carry someone's request to Pisa (a closing question then stays)
REQUEST_KINDS = frozenset({"email", "email-results", "invite"})
DRAFT_TOOLS = frozenset({"draft_reply", "edit_draft", "send_draft"})  # a draft read back to the user


_asks_what_was_done = claim_rules.asks_what_was_done  # the domain's (the replay reads it too)


def _backed(sentence: str, turn: TurnState, lang: str) -> bool:
    """RAIL: a claimed change is backed by code's record of that change
    (`claims.backed`): this turn's own, or one Pisa made for this device lately (the user asks "l'hai
    messo?") when this turn neither asked a yes nor failed, or Pisa's record read back."""
    return claim_rules.backed(sentence, lang, turn.record())


_PLANNED = re.compile(r"Planned: (.+?) at (\d{2}:\d{2}) today\.")
TASK_STATES = 500  # the tasks a sentence's task state is checked against (the latest touched)
REPLY_ROWS_KEPT = 64  # a talk's latest turns whose stored reply `spoken` may still replace
SPOKEN_MAX = 8000  # what Pisa's voice said for one turn, as stored
# a task this turn closed, by its receipt ("task done: «…»", complete_task's), as the store titles it
_CLOSED = re.compile(r"task done: «([^»]+)»")


# a view's save by its name, so its save again with other paths clears the refused one; only
# there (a skill scheduled at 8, refused, then at 9 is another change, never the same one)
_BY_NAME = frozenset({"save_view"})


def _target(args: dict[str, Any], tool: str = "") -> str:
    """What a call changes (the event, the task, the title…), so a failure is cleared only by the same
    change done."""
    if tool in _BY_NAME and (name := args.get("name")):
        return " ".join(str(name).casefold().split())[:120]
    for key in ("event", "event_id", "task_ref", "task", "title", "approval", "draft", "to", "place"):
        if value := args.get(key):
            return " ".join(str(value).casefold().split())[:120]
    return json.dumps(args, sort_keys=True, default=str)[:120]


def _settle(failures: dict[tuple[str, str], str], tool: str, target: str, failed: str | None) -> None:
    """The turn's record of a write's outcome: the final one of the same call wins. A failure is kept per
    target; a success clears its own target's failure, and the same tool's earlier failures of a call that
    named no object of its own (`_target`'s arguments, `{…}`): a retry with other arguments ("phone Anna",
    refused, then "call Anna", kept) is the same call, done. A named object's failure stays beside another's
    success ("move A" failed, "move B" done)."""
    if failed is not None:
        failures[(tool, target)] = failed
        return
    failures.pop((tool, target), None)
    for key in [k for k in failures if k[0] == tool and k[1].startswith("{")]:
        del failures[key]


# the tools whose result's kind the turn's trace keeps, and the kinds: the result's own lead words, never
# what follows them (a title)
TRACED = frozenset({"mark_done", "complete_task", "add_task", "what_now", "get_plan"})
_OUTCOME = re.compile(
    r"\b(Already done today|Already done|Done|Several match|Nothing open matches|Not marked done|Not added|"
    r"Added|Marked done|No open task matches|No plan|Now|Next|To do today|Nothing planned today)\b"
)


def outcome_of(result: str) -> str:
    """A tool result's kind by its lead words ("Done", "Nothing open matches"); else "other" with a coarse
    hint from code's own words, never the result's text (audit §14: "add_task:other" had no reason)."""
    found = _OUTCOME.search(result)
    if found is not None:
        return found.group(1)
    low = result.casefold()
    if "yes" in low and ("wait" in low or "held" in low or "not done" in low):
        return "other:held"
    if low.startswith("not ") or "nothing changed" in low:
        return "other:refused"
    if "call " in low and " again" in low:
        return "other:retry"
    if "is data" in low or low.startswith("the text between"):
        return "other:data"
    return "other:unknown"


def trace_turn(
    lang: str,
    turn: TurnState,
    parts: list[tuple[str, str]],
    results_seen: list[tuple[str, str]] = [],  # noqa: B006 (read only)
    step: rules.Result | None = None,
) -> dict[str, Any]:
    """The turn's trace, logged at DEBUG only (`PISA_LOG_LEVEL=debug`): what the model was given, by part
    name and length; for each task it added, which parts held that title word for word; the kind of each
    traced tool's result; whether the understanding step changed their words. Names, lengths, kinds and
    flags only: never a title, their words or a part's text."""
    added = []
    for name, args in turn.calls:
        if name != "add_task":
            continue
        title = " ".join(str(args.get("title") or "").split())
        added.append({
            "args": sorted(k for k, v in args.items() if v is not None),
            "title_chars": len(title),
            "title_other_language": tongue.foreign(title, lang),  # reads as another language than the turn's
            "title_in": [part for part, text in parts if title and title in " ".join(text.split())],
        })  # fmt: skip
    understood = (
        {"changed": step.changed, "fixes": len(step.corrections),
         "by_model": sum(c.via == "model" for c in step.corrections)}
        if step is not None else None
    )  # fmt: skip
    return {"lang": lang, "parts": {part: len(text) for part, text in parts if text}, "added": added,
            "tools": list(turn.tools), "outcomes": [f"{n}:{k}" for n, k in results_seen],
            "understood": understood}  # fmt: skip


def ledger_line(rows: list[dict[str, Any]], changed: list[dict[str, Any]], timezone: str) -> str:
    """Pisa's ledger, written by code: what waits for the user's yes (with ids, NOT done), what was
    asked and never answered, and what Pisa changed in this conversation; never what the model said it did.
    Its titles may come from an email or an invite: data, Pisa's own list."""
    if not rows and not changed:
        return ""
    tz = ZoneInfo(timezone)

    def line(r: dict[str, Any], at: datetime) -> str:
        return f"«{r['summary']}» (id {results.short(r['id'])}, {at.astimezone(tz):%H:%M})"

    live = [line(r, r["created_at"]) for r in rows if not r["expired"]]
    lapsed = [line(r, r["created_at"]) for r in rows if r["expired"]]
    done = [line(r, r["executed_at"]) + (" (undone since)" if r.get("undone_at") else "") for r in changed]
    lines = []
    if live:
        lines.append("Waiting for the user's yes, NOT done yet: " + "; ".join(live) + ".")
    if lapsed:
        lines.append("Asked earlier and never answered (expired), NOT done: " + "; ".join(lapsed) + ".")
    if done:
        lines.append("Done by Pisa in this conversation: " + "; ".join(done) + ".")
    block = data_block("action", "\n".join(lines), what="Pisa's ledger: its open questions and its changes")
    return (
        f"\n\nPisa's ledger (code's record, not anyone's words):\n{block.rule}\n{block.text}\nNever say a "
        "waiting or expired one is done; to do an expired one now, call its tool again. When the user "
        'corrects one ("per oggi", "alle 5 invece"), call the same tool again with the change: it replaces '
        "the question."
    )


# a turn whose reply is the day's recap never gets il prurito's line after it
RECAP_TOOLS = frozenset({"close_day", "what_did_i_do", "walk_recap"})


def _proposal_said(rest: str, said: str, turn: TurnState, lang: str) -> str:
    """A task Pisa only proposed (`plan_task`) is asked
    for their yes in code's words when the reply asks nothing: the sentence that names its time becomes the
    proposal (or the proposal closes the reply when that sentence was already said). Only a sentence naming
    the task at its clock time ("alle 10", "ore 10", "10:00"), never a day ("al 10 novembre"), never one that
    claims a change."""
    if not turn.proposals:
        return rest
    title, at = turn.proposals[0]
    line = claim_rules.proposal_truth(lang, title, at)
    if line in said + rest or "?" in said + rest:
        return rest
    hour, minute = int(at[:2]), at[3:5]
    bare = rf"|\b(?:alle|ore|at)\s+0?{hour}\b(?![:.]\d)" if minute == "00" else ""  # "alle 10" is 10:00
    clock = rf"\b0?{hour}[:.]{minute}\b{bare}"
    sentences = _SENTENCE.findall(rest)
    for n, sentence in enumerate(sentences):
        if (
            re.search(clock, sentence, re.IGNORECASE)
            and action_rules.names_item(title, sentence)
            and claim_rules.claim(sentence) is None
        ):
            return "".join([*sentences[:n], line + " ", *sentences[n + 1 :]]).strip()
    return f"{rest} {line}".strip()


# a tool's result that says a button is on the phone now (`handoff_tools.show`, compose_message, call_place)
HANDED_OFF = (
    "Prepared on the phone as a sheet of buttons",
    "compose screen is open on the phone",
    "The phone offers to call",
)


def _released(
    held: list[str],
    turn: TurnState,
    lang: str,
    *,
    others: bool = True,
    said: str = "",
    apart: list[tuple[str, bool, bool]] | None = None,
) -> tuple[str, int]:
    """The sentences held back while the turn ran, in the order they were said: each claim the turn's record
    now backs as written, the rest replaced by the truth, once. (What to say, how many claims were false.)
    `said`: the reply's other words; a claim whose change they already say wasn't made ("I couldn't add
    lunch with Sara…") goes without the rail's line ("I haven't put it: it didn't work." would repeat it).
    `apart` (a talk's reply): a false claim is left out and kept there (the sentence, whether its change
    failed, whether it is unverified) for the one correction line said after the reply, never a truth line
    inside it."""
    out: list[str] = []
    general: set[str] = set()  # the lines with no verb to name ("Non l'ho ancora fatto: …")
    false = named = 0
    for n, sentence in enumerate(held):
        # "Sposto la call alle 16 … Dimmi sì e lo faccio.": the act the next sentence asks
        # their yes for is a proposal (its times said, for their yes to bind), never a claim
        asked = claim_rules.waits_for_yes(sentence, held[n + 1 :])
        if asked or _backed(sentence, turn, lang):
            out.append(sentence)
            continue
        false += 1
        if claim_rules.bare(sentence) and not action_rules.commands(turn.text) and others:
            # a bare "Segnato."/"Fatto." to words that asked no change names nothing to rewrite: it goes,
            # never turned into the rail's words beside the rest of the reply
            continue
        # each one rewritten into what really happened, never dropped ("Non l'ho spostato: …")
        if turn.proposals:  # what Pisa did is a proposal, said as one
            title, at = turn.proposals[0]
            line = claim_rules.proposal_truth(lang, title, at)
        elif (
            not turn.approvals
            and not claim_rules.unverified_for(sentence, lang, turn.record())
            and claim_rules.says_not_done(sentence, " ".join([said, *held[:n], *held[n + 1 :]]), lang)
        ):
            # the reply already says it wasn't done: no line repeats it. A promise beside it ("Connect
            # Google and I'll put it on your calendar") is what comes next, said as written; a "done" goes
            if claim_rules.claim(sentence) == "doing" and sentence not in out:
                out.append(sentence)
            continue
        elif apart is not None:  # a talk: the one correction line after the reply says it
            record = turn.record()
            apart.append(
                (
                    sentence,
                    claim_rules.failed_for(sentence, lang, record),
                    claim_rules.unverified_for(sentence, lang, record),
                )
            )
            continue
        else:
            line = claim_rules.truth_for(
                sentence,
                lang,
                waiting=bool(turn.approvals),
                failed=claim_rules.failed_for(sentence, lang, turn.record()),
                changed=bool(turn.done_tools),
                unverified=claim_rules.unverified_for(sentence, lang, turn.record()),
            )
        if claim_rules.verb_of(sentence, lang) is None:
            general.add(line)
        else:
            named += 1
        if line not in out:
            out.append(line)
    if named:  # a line that names the change says it all: the general one beside it is redundant
        out = [line for line in out if line not in general]
    return " ".join(out), false


def _keeping(out: Emit, into: list[str], earlier: Iterable[str] = ()) -> Emit:
    """`out`, keeping each delta it is given in `into` (what a reply said, for a continuation). RAIL: a
    sentence already said in this turn (`earlier`: the cut reply's, for a continuation), the same once case,
    punctuation and spaces are set aside, is never said again, whatever path emits it (F6: the refusal said
    before "Ci sto ancora lavorando" and again after it; a short one escaped the fuzzy check)."""
    seen = {k for x in earlier for one in form.split_sentences(x) if (k := form.sentence_key(one))}

    async def emit(delta: str, final: bool) -> None:
        kept: list[str] = []
        dropped = False
        for one in form.split_sentences(delta) if delta else []:
            key = form.sentence_key(one)
            if key and key in seen:
                dropped = True
                continue
            if key:
                seen.add(key)
            kept.append(one)
        new = delta  # nothing said again: the delta as given, its spaces too
        if dropped:
            new = "".join(kept)
            if not new and not final:
                return  # all of it said already
            if new and delta[:1].isspace() and not new[:1].isspace():
                new = " " + new
        into.append(new)
        await out(new, final)

    return emit


def done_line(turn: TurnState) -> str:
    """For a continued turn: what its first part already did, so the joined turn won't do it again."""
    if not turn.done:
        return ""
    return (
        "\n\nAlready done in this turn, before the user went on (it stays done; don't do it again, and say "
        "it was done only if it matters): " + "; ".join(turn.done) + "."
    )


def turn_parts(
    trace_id: str,
    context_s: float,
    round_ms: list[int],
    messages: list[Any],
    tool_times: list[tuple[str, float]],
) -> dict[str, Any]:
    """One turn's time by part (ms) and each model round's tokens (names and numbers)."""
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    rounds: list[dict[str, int | None]] = []
    for i, ms in enumerate(round_ms):
        r = responses[i] if i < len(responses) else None
        # the prompt cache's share of each round's input (read from it, written to it)
        rounds.append(
            {
                "ms": ms,
                "in": r.usage.input_tokens if r else None,
                "out": r.usage.output_tokens if r else None,
                "cr": r.usage.cache_read_tokens if r else None,
                "cw": r.usage.cache_write_tokens if r else None,
            }
        )
    return {
        "trace": trace_id,
        "context_ms": int(context_s * 1000),
        "rounds": rounds,
        "model_ms": sum(round_ms),
        "tools": [{"name": name, "ms": int(seconds * 1000)} for name, seconds in tool_times],
        "tools_ms": sum(int(seconds * 1000) for _, seconds in tool_times),
    }


# never a sentence end between digits ("3.99", "15.30")
_SENTENCE = re.compile(r"(?:[^.!?…]|(?<=\d)\.(?=\d))+[.!?…]*\s*")


# a question that asks which one ("quale dei due?", "intendi il dentista?") is needed to act
_WHICH = re.compile(
    r"\b(?:quale|quali|qual|intendi|which|do you mean)\b"
    # an either/or ("quella di sabato o quella di domenica?", "the 10 or the 11?"), never "o no?"
    r"|\b\w+(?:\s+\w+){0,4}\s+(?:o|oppure|or)\s+(?!no\b|non\b|not\b|altro\b|anything\b)\w+",
    re.IGNORECASE,
)


# an either/or whose other side holds back ("lo sposto o preferisci di no?", "o aspettiamo?") is
# the yes/no code already asks, never a choice
_HOLDING = re.compile(
    r"\b(?:di no|lasci\w*|aspett\w*|cambiar\w*|leave|wait|hold off|not yet|as it is)\b", re.IGNORECASE
)


def _without_questions(text: str) -> str:
    """The reply without its own questions (code asks the one question): a "which
    one?" stays, and decimals and times are never split ("3.99", "15.30")."""
    return "".join(
        s
        for s in _SENTENCE.findall(text)
        if not s.rstrip().endswith("?") or (_WHICH.search(s) and not _HOLDING.search(s))
    ).strip()


def _screen_sentences(text: str) -> tuple[str, list[str]]:
    """The reply's own "it's on your phone" claims, out of the text (the last sentence too): only
    the claim's clause when the sentence says more."""
    kept: list[str] = []
    claims: list[str] = []
    for sentence in _SENTENCE.findall(text):
        rest, claim = split_screen_claim(sentence)
        if rest:
            kept.append(rest if rest.endswith(" ") or not sentence.endswith(" ") else rest + " ")
        if claim:
            claims.append(claim.strip())
    return "".join(kept).strip(), claims


# a watch run's reply that was only a card: the spoken line with it
ON_PHONE_SAID = {"it-IT": "Te l'ho messo sul telefono.", "en-US": "I've put it on your phone."}
SOURCES_SAID = {"it-IT": "Ti ho messo i link sul telefono.", "en-US": "I've put the links on your phone."}


def _with_screen_claims(
    rest: str,
    claims: list[str],
    on_screen: bool,
    delivered: bool,
    lang: str,
    *,
    said: bool = False,
) -> str:
    """The model's "it's on your phone" only when the phone has something this turn; Pisa's own line for
    the sources card when the card was delivered and the model didn't say it. When the phone has nothing, the
    claim's clause goes and the rest is said (whether or not something was to be shown); only a reply
    that was nothing but the claim says what happened, the last resort."""
    if on_screen and claims:
        return f"{rest} {' '.join(claims)}".strip()
    if delivered:
        return f"{rest} {SOURCES_SAID.get(lang, SOURCES_SAID['en-US'])}".strip()
    if not claims:
        return rest
    not_shown = NOT_SHOWN.get(lang, NOT_SHOWN["en-US"])
    if LEAD_ONLY.match(rest) and not said:
        return not_shown  # a reply that was only the claim never goes silent (nor "Ecco.")
    # the claim's clause goes and the answer stands; Pisa never confesses its plumbing
    return rest  # never "… Non sono riuscito a mostrartelo sul telefono."
