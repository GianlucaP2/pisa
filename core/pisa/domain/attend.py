"""Proactivity as decisions: what Pisa could say unasked, and when. Pure
rules.

- The **senses** (reminders, leave-now, stays, check-ins, intake, re-plans, the weather against the plan,
  Pisa's own promises) emit **candidates**: what code would say today (the template), its urgency, its
  deadline and whether it is a floor.
- **`attend.decide`** (a small model, no tools) decides each batch: now, at the next breakpoint, at a time,
  folded into the next conversation, shown on the phone without a voice, or dropped; the channel; the words,
  which may merge several candidates ("Tre cose: …"); and a one-line purpose.
- **The validator** (`validate`, code) holds the rails whatever the model said:
  - a floor is never dropped or folded, and goes before its deadline;
  - only a floor interrupts (`now`): the rest waits for a breakpoint;
  - quiet hours and meetings hold everything without a floor;
  - the voice rate holds: past it, the channel becomes the phone;
  - the words pass `talk.violations` and keep the template's numbers, else the template is said;
  - a candidate with its own payload (a check-in's choices, a done's Undo) keeps its words and goes alone.
- **`rules`** is today's behaviour: what decides on a failure, a timeout or an exhausted budget.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from pisa.domain import talk
from pisa.domain.decisions import PURPOSE_MAX

Source = Literal[
    "reminder", "leave", "stay", "check_in", "intake", "replan", "weather", "promise", "knock", "tragitto",
    "intention",
]  # fmt: skip
Choice = Literal["now", "breakpoint", "at", "fold_next", "show_silently", "drop"]
Channel = Literal["voice", "phone_banner", "phone_object", "voice_and_phone"]
Outcome = Literal["answered", "later", "dismissed", "undone", "ignored", "said_in_talk", "expired"]
SOURCES: tuple[Source, ...] = (
    "reminder", "leave", "stay", "check_in", "intake", "replan", "weather", "promise", "knock", "tragitto",
    "intention",  # an intention's cue, a waiting-for, a promise proposed (never a floor)
)  # fmt: skip
# Pisa's own questions that open a talk ("Posso dirti una cosa?", "Vuoi il tragitto?"): an offer,
# never a floor; at a breakpoint only, dropped in quiet hours (never held for a summary) and past the cap.
ASKS: frozenset[Source] = frozenset({"knock", "tragitto"})
CHOICES: tuple[Choice, ...] = ("now", "breakpoint", "at", "fold_next", "show_silently", "drop")

BATCH_WINDOW = timedelta(minutes=2)  # candidates this close are decided together (one sentence, not three)
DECIDE_TIMEOUT_S = 2.0  # RAIL: the model's answer within this when a floor waits, else today's rules decide
DECIDE_TIMEOUT_NO_FLOOR_S = 3.0  # a batch without a floor
DEADLINE_MARGIN = timedelta(minutes=2)  # an `at` goes at least this long before the deadline
AT_MAX = timedelta(hours=12)  # an `at` further than this is no plan: the next breakpoint
FOLD_KEPT = timedelta(hours=12)  # a folded item not brought up in this long expires
FOLD_MAX = 3  # folded decisions brought up in one turn
BATCH_MAX = 8  # candidates per decision call
# a delivery's outcome: their message this soon after it answered it; nothing this long after, ignored
ANSWERED_WITHIN = timedelta(minutes=5)
IGNORED_AFTER = timedelta(minutes=30)
MERGE_OPEN = {"it-IT": {2: "Due cose: ", 3: "Tre cose: "}, "en-US": {2: "Two things: ", 3: "Three things: "}}
_NUMBER = re.compile(r"\d+(?:[:.]\d+)?")


@dataclass(frozen=True)
class Candidate:
    id: str
    source: Source
    kind: str  # the notification kind it would go out as (reminder, check_in, done, info, replan, alert)
    text: str  # what code says today (the template)
    lang: str
    created_at: datetime
    urgency: talk.Urgency = "breakpoint"  # today's rule
    deadline: datetime | None = None  # it must reach the user before this
    floor: bool = False  # RAIL: never dropped or folded; before its deadline
    outside: bool = False  # its words rest on outside text (an email): data for the model, provenance outside
    alone: bool = False  # it carries its own payload (choices, an Undo): its words are kept, never merged
    must_show: bool = False  # high priority from intake: never dropped silently (shown at least)
    priority: str = "normal"
    # Pisa may say it first by its own voice (leave_now, reply_waiting, approval_lapsing, check_in)
    speak: str | None = None
    # the same day's knock (il prurito's answer, a miss's reason), the one knock past the morning
    same_day: bool = False


@dataclass(frozen=True)
class Decision:
    ids: tuple[str, ...]  # the candidates it covers; more than one: said together
    choice: Choice
    channel: Channel
    text: str
    at: datetime | None = None
    purpose: str | None = None
    by: Literal["model", "rules"] = "model"
    rails: tuple[str, ...] = ()  # the rails that changed what the model said


@dataclass(frozen=True)
class Context:
    """What the validator needs to know about the moment (code's facts, not the model's)."""

    now: datetime
    quiet: bool = False
    meeting: bool = False
    voice_ok: bool = True  # under the unprompted voice rate
    quiet_at: Callable[[datetime], bool] = lambda _: False  # whether quiet hours hold at a moment
    asks_left: bool = True  # under the 2-a-day cap of Pisa's unprompted asks
    morning: bool = True  # before noon, their time (a knock is a morning's)


def rules(candidate: Candidate) -> Decision:
    """Today's behaviour: a floor or a `now` item at once, the rest at the next breakpoint; intake (said
    nothing until) only shown on the phone when it is high, else dropped."""
    if candidate.source == "intake":
        choice: Choice = "show_silently" if candidate.must_show else "drop"
    elif candidate.urgency == "now":
        choice = "now"
    else:
        choice = "breakpoint"
    channel: Channel = "phone_banner" if choice == "show_silently" else "voice_and_phone"
    return Decision((candidate.id,), choice, channel, candidate.text, by="rules")


def merged_text(candidates: Sequence[Candidate], lang: str) -> str:
    """Code's own sentence for several candidates: "Tre cose: a. b. c." (the template of a merge)."""
    if len(candidates) == 1:
        return candidates[0].text
    opening = MERGE_OPEN.get(lang, MERGE_OPEN["en-US"]).get(len(candidates), "")
    return opening + " ".join(_sentence(c.text) for c in candidates)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def parse_at(value: str | None, now: datetime) -> datetime | None:
    """The model's HH:MM as the next such moment (today, or tomorrow when it has passed)."""
    if not value or not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        return None
    hour, minute = (int(x) for x in value.strip().split(":"))
    if hour > 23 or minute > 59:
        return None
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=1)


# RAIL: the model decides when and how; these hold whatever it said.
def validate(decision: Decision, members: Sequence[Candidate], ctx: Context) -> Decision:
    """The decision as it may go out, with the rails that changed it."""
    rails: list[str] = []
    floor = any(c.floor for c in members)
    now_floor = any(c.floor and c.urgency == "now" for c in members)
    deadline = min((c.deadline for c in members if c.deadline), default=None)
    choice, at, channel = decision.choice, decision.at, decision.channel
    if choice in ("drop", "show_silently") and floor:
        choice, rails = _floor_choice(members), [*rails, "floor_kept"]  # shown only is not said
    elif choice == "drop" and any(c.must_show for c in members):
        choice, rails = "show_silently", [*rails, "high_shown"]
    elif choice == "drop" and any(c.kind == "done" for c in members):
        choice, rails = "show_silently", [*rails, "done_shown"]  # its Undo stays within reach
    if choice == "fold_next" and floor:
        choice, rails = _floor_choice(members), [*rails, "floor_not_folded"]
    if choice == "at" and floor:
        # RAIL: a floor is never deferred: its words count down from now ("Tra 15
        # minuti") and its urgency is computed now; at a later `at` both would be wrong and it would wait
        # behind the user's talk and the voice rate.
        choice, at, rails = _floor_choice(members), None, [*rails, "floor_not_deferred"]
    elif choice == "fold_next" and any(c.alone for c in members):
        choice, rails = "breakpoint", [*rails, "payload_needs_notice"]  # its choices or Undo need the notice
    if choice == "at":
        if at is None or at <= ctx.now or at - ctx.now > AT_MAX:
            choice, at, rails = "breakpoint", None, [*rails, "at_unusable"]
        elif deadline is not None and at > deadline - DEADLINE_MARGIN:
            choice, at, rails = (
                (_floor_choice(members) if floor else "breakpoint"),
                None,
                [*rails, "deadline"],
            )
        elif ctx.quiet_at(at):
            # quiet is checked when Pisa decides and when it would speak: an `at` inside quiet
            # hours goes to the next talk (a payload or a floor: today's rule, which quiet holds as today).
            fallback: Choice = (
                _floor_choice(members) if floor or any(c.alone for c in members) else "fold_next"
            )
            choice, at, rails = fallback, None, [*rails, "at_quiet"]
    else:
        at = None
    if now_floor and choice != "now":
        # RAIL: a leave-now or an event about to start is never downgraded: urgency
        # `now`, whatever the model said (a breakpoint would wait while the user talks, past leave_at).
        choice, at, rails = "now", None, [*rails, "floor_now"]
    if any(c.source in ASKS for c in members) and choice == "drop" and decision.choice == "drop":
        # The knock and the tragitto offer are already gated by code
        # (their asks, the day's cap, quiet hours, meetings, "basta"); the model's "drop what they wouldn't
        # miss" saw only "Posso dirti una cosa?" and dropped every one it decided in time. The model picks
        # when, not whether: a drop becomes the next breakpoint, and the rails below still drop it in quiet
        # hours, past the cap or past the morning.
        choice, at, rails = "breakpoint", None, [*rails, "ask_kept_by_code"]
    if any(c.source in ASKS for c in members) and choice != "drop":
        # RAIL: the knock and the tragitto offer are questions Pisa asks unasked: never in quiet
        # hours (a held "Posso dirti una cosa?" would reach them hours later in the summary), never past the
        # day's cap, never at an `at` in quiet hours, never folded into a talk they opened for something else.
        if ctx.quiet or (choice == "at" and at is not None and ctx.quiet_at(at)):
            choice, at, rails = "drop", None, [*rails, "ask_quiet"]
        elif not ctx.asks_left:
            choice, at, rails = "drop", None, [*rails, "ask_cap"]
        elif not ctx.morning and any(c.source == "knock" and not c.same_day for c in members):
            choice, at, rails = "drop", None, [*rails, "knock_past_morning"]
        elif choice in ("fold_next", "show_silently"):
            choice, at, rails = "breakpoint", None, [*rails, "ask_at_breakpoint"]
    if choice == "now" and not any(c.urgency == "now" for c in members):
        choice, rails = "breakpoint", [*rails, "interrupt_floor_only"]
    if (ctx.quiet or ctx.meeting) and not floor and choice in ("now", "breakpoint", "show_silently"):
        # Held by code: quiet mode holds the notification (summary at its end); a meeting defers it.
        rails = [*rails, "quiet" if ctx.quiet else "meeting"]
    voice_floor = any(talk.is_floor(c.kind, c.urgency) for c in members)
    if choice == "show_silently" and channel in ("voice", "voice_and_phone"):
        channel = "phone_banner"
    if floor and not spoken(channel):
        # RAIL: a floor goes by voice (exempts the now-floors from the rate; the
        # rest meet the rate below, like any item).
        channel, rails = "voice_and_phone", [*rails, "floor_voiced"]
    if channel in ("voice", "voice_and_phone") and not ctx.voice_ok and not voice_floor:
        channel, rails = "phone_banner", [*rails, "voice_rate"]
    text = _checked_text(decision.text, members, rails)
    if text is None:
        text, rails = merged_text(members, members[0].lang), [*rails, "template"]
    purpose = " ".join((decision.purpose or "").split())[:PURPOSE_MAX] or None
    return replace(decision, choice=choice, at=at, channel=channel, text=text, purpose=purpose,
                   rails=tuple(dict.fromkeys(rails)))  # fmt: skip


def _floor_choice(members: Sequence[Candidate]) -> Choice:
    return "now" if any(c.urgency == "now" for c in members) else "breakpoint"


def _checked_text(text: str, members: Sequence[Candidate], rails: list[str]) -> str | None:
    """The model's words when they hold (form; a floor's numbers kept; a payload's own words), else None
    (the template)."""
    if any(c.alone for c in members):
        return members[0].text  # its choices or its Undo are about these words
    if any(c.outside for c in members):
        return None  # an email's words only as code's template, with who asks
    words = " ".join(text.split())
    if not words or talk.violations(words, members[0].lang):
        return None
    for c in members:
        if c.floor and not _in_order(_NUMBER.findall(c.text), _NUMBER.findall(words)):
            return (
                None  # FACT: a floor's times and minutes, in their order, never change in the model's words
            )
    return words


def _in_order(needed: Sequence[str], said: Sequence[str]) -> bool:
    """Each needed number appears in `said`, in the same order (a subsequence)."""
    rest = iter(said)
    return all(any(n == s for s in rest) for n in needed)


def batch_due(
    created: Sequence[datetime], floors: Sequence[bool], now: datetime, alone: Sequence[bool] = ()
) -> bool:
    """Decide the pending batch now: a floor waits for nothing, nor a batch that can't merge (all `alone`);
    the rest once the oldest has waited the window (what comes within it is decided with it)."""
    if not created:
        return False
    return any(floors) or (bool(alone) and all(alone)) or now - min(created) >= BATCH_WINDOW


def cover(decisions: Sequence[Decision], candidates: Sequence[Candidate]) -> list[Decision]:
    """Each candidate in exactly one decision: unknown ids gone, a candidate already covered left out of a
    later one, a merge with a payload candidate split (it goes alone), the uncovered by today's rules."""
    by_id = {c.id: c for c in candidates}
    seen: set[str] = set()
    out: list[Decision] = []
    for decision in decisions:
        asked = tuple(dict.fromkeys(decision.ids))
        ids = tuple(i for i in asked if i in by_id and i not in seen)
        if not ids:
            continue
        seen.update(ids)
        if len(ids) == 1:
            # what is left of a merge (an id decided earlier or unknown) never keeps the
            # merge's words, which describe the others too: code's template (text "").
            out.append(replace(decision, ids=ids, text=decision.text if len(asked) == 1 else ""))
            continue
        # a payload item goes alone on its template; an email's item is never merged with Pisa's
        # own. What stays together keeps the choice, in code's words when the group changed (text "").
        alone = [i for i in ids if by_id[i].alone]
        groups = [g for g in ([i for i in ids if not by_id[i].alone and not by_id[i].outside],
                              [i for i in ids if not by_id[i].alone and by_id[i].outside]) if g]  # fmt: skip
        out += [rules(by_id[i]) for i in alone]
        for g in groups:
            whole = len(g) == len(asked)
            out.append(replace(decision, ids=tuple(g), text=decision.text if whole else ""))
    out += [rules(c) for c in candidates if c.id not in seen]
    return out


def move(choice: Choice) -> Literal["speak", "defer", "show", "drop"]:
    """The decision's move in `pisa_decisions`."""
    if choice in ("now", "breakpoint"):
        return "speak"
    if choice in ("at", "fold_next"):
        return "defer"
    return "show" if choice == "show_silently" else "drop"


def spoken(channel: Channel) -> bool:
    return channel in ("voice", "voice_and_phone")


# --- the senses' templates (code's words, form) -------------------------------------------------------


def weather_text(hour: datetime, mode: str, lang: str) -> str:
    vehicle = {"bike": ("hai la bici", "you're cycling"), "walk": ("vai a piedi", "you're walking")}
    it, en = vehicle.get(mode, ("sei fuori", "you're out"))
    if lang == "it-IT":
        return f"Pioggia alle {hour:%H:%M}, {it}: un ombrello?"
    return f"Rain at {hour:%H:%M} and {en}: an umbrella?"


def replan_text(title: str, lang: str) -> str:
    if lang == "it-IT":
        return f"Il piano è cambiato: {title} oggi non ci sta più."
    return f"The plan changed: {title} no longer fits today."


def promise_text(what: str, lang: str) -> str:
    return f"Ti avevo detto che te lo ricordavo: {what}." if lang == "it-IT" else f"As promised: {what}."
