"""One gate for every word Pisa says or pushes without their words. Pure rules, no I/O,
no model call.

- **The map** (`PATHS`): every way Pisa speaks or pushes unasked, by name. Each names what the item is, whose
  it is and what missing it costs; the service stamps the name on the item (a notification's `path`, an
  approval's request, the lock screen's first start) and nothing is delivered without a verdict.
- **The score** (`score`): the content first, then the moment. Four parts kept apart: what it is, whose it
  is, how soon it matters (its deadline), what it costs to miss (a legal clock, a train leaving, a meeting
  moved, money, or a nice-to-know). Outside text never raises an item: an email's item is "outside".
- **The gate** (`attend`): `now | breakpoint | digest`, with a reason, in this order:
  1. a reply to their own words or taps is not unprompted: `now` (`his_words`);
  2. **their rules win**: "always for X" is `now` through any busy block, "never for Y" is the digest;
  3. the floors (leave now, an event about to start) are `now` (the caps stay the ceiling);
  4. a silent state push (the lock screen's update) is `now`, never counted;
  5. the score against the floor of the moment: `NOW_AT`, or `BUSY_NOW_AT` in a busy block (back-to-back
     meetings, a talk they give, driving, an exchange with Pisa): nothing below it speaks there;
  6. **decay**: three ignored nudges of a kind halve its rate (`decay_level`), each three more halve it again;
  7. at or over `BREAKPOINT_AT`: the next breakpoint (now, when this moment is one); under it: the digest.
- **Breakpoints** (`at_breakpoint`): not moving, in no meeting, not talking to Pisa; named by what just ended
  (a meeting block, a walk, a talk) or `idle`.
- **The record**: the service keeps every verdict (id, kind, decision, reason, the score's numbers), never
  the words; the Sunday card's line (`card_line`) and the user report's measures (`measures`) read it.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain.text import people_name_key

Decision = Literal["now", "breakpoint", "digest"]
Kind = Literal[
    "leave", "event", "reminder", "approval", "alert", "check_in", "done", "replan", "briefing", "ask",
    "ritual", "person", "card", "info", "digest", "state",
]  # fmt: skip
KINDS: tuple[str, ...] = (
    "leave", "event", "reminder", "approval", "alert", "check_in", "done", "replan", "briefing", "ask",
    "ritual", "person", "card", "info", "digest", "state",
)  # fmt: skip
# The kinds their rules may name (not the holds' own digest, nor the lock screen's silent state)
RULE_KINDS: tuple[str, ...] = tuple(k for k in KINDS if k not in ("digest", "state"))
Whose = Literal["his_words", "his", "person", "pisa", "outside"]
DECAYS: frozenset[str] = frozenset({"pisa", "person", "outside"})  # whose items decay (Pisa's own nudges)
Cost = Literal["clock", "leave", "moved", "money", "nice"]
Mode = Literal["always", "never"]

# --- the numbers ---------------------------------------------
NOW_AT = 60  # a score at or over this interrupts now
BUSY_NOW_AT = 80  # in a busy block, only this interrupts (the floor rises; nothing below it speaks there)
BREAKPOINT_AT = 25  # at or over this: the next breakpoint; under it: the next digest
WHAT: dict[str, int] = {
    "leave": 30, "event": 30, "alert": 35, "reminder": 30, "approval": 30, "check_in": 20, "done": 15,
    "replan": 20, "briefing": 25, "ask": 20, "ritual": 20, "person": 15, "card": 15, "info": 10,
    "digest": 0, "state": 0,
}  # fmt: skip
WHOSE: dict[str, int] = {"his_words": 0, "his": 15, "person": 10, "pisa": 5, "outside": 0}
COST: dict[str, int] = {"clock": 25, "leave": 25, "moved": 15, "money": 25, "nice": 0}  # money: a cap, now
SOON: tuple[tuple[timedelta, int], ...] = (
    (timedelta(minutes=15), 30),
    (timedelta(hours=1), 20),
    (timedelta(hours=4), 10),
    (timedelta(hours=24), 5),
)
DECAY_EVERY = 3  # this many ignored nudges of a kind in a row halve its rate
DECAY_MAX = 3  # at most 1 in 8
B2B_GAP = timedelta(minutes=10)  # meetings this close are one block
IDLE_AFTER = timedelta(minutes=2)  # their last words this long ago: no longer in an exchange
JUST_ENDED = timedelta(minutes=15)  # a meeting, walk or talk ended this recently names the breakpoint
RECHECK = timedelta(minutes=10)  # a hold with no known end is looked at again after this
# a notice held while they talk waits at most this long (notifications' `BUSY_HOLD`): the talk's end frees it
TALK_HOLD = timedelta(minutes=2)
MEETING_MAX = timedelta(hours=4)  # longer events (a conference, a trip) are no meeting to keep quiet in
HOLD_MAX = timedelta(hours=2)  # a hold that met no breakpoint in this long goes to the next digest
DEADLINE_MARGIN = timedelta(minutes=5)  # a held item is released this long before its deadline
NEGLECT_AFTER = timedelta(hours=24)  # held this long and never out: the neglect report's
DIGEST_TIMES: tuple[time, ...] = (time(8, 30), time(12, 30), time(18, 30), time(21, 30))  # their local time
DIGEST_PREFIX = {"it-IT": "Nel frattempo: ", "en-US": "Meanwhile: "}
DIGEST_MAX_CHARS = 1000
CARD_LINE = {
    "it-IT": "Interruzioni: trattenute {held} / passate {passed}{never}.",
    "en-US": "Interruptions: held {held} / passed {passed}{never}.",
}
CARD_NEVER = {"it-IT": "; mai uscite {n}", "en-US": "; never out {n}"}
# Reasons that are no unprompted interruption: logged, never counted on the card
NOT_COUNTED = frozenset({"his_words", "silent"})
_MEETING_WORDS = re.compile(
    r"\b(?:meeting|riunione|call|standup|stand-up|sync|1:1|colloquio|incontro|review|interview)\b",
    re.IGNORECASE,
)
_TALK_WORDS = re.compile(
    r"\b(?:talk|keynote|presentazione|presentation|intervento|relatore|speaker|lezione|lecture|conferenza)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Path:
    """One way Pisa speaks or pushes unasked: the map's row."""

    name: str
    kind: Kind
    whose: Whose
    cost: Cost
    where: str  # the module that starts it


def _p(name: str, kind: Kind, whose: Whose, cost: Cost, where: str) -> tuple[str, Path]:
    return name, Path(name, kind, whose, cost, where)


# Every unprompted path, by name. A test reads each production call of
# `notifications.enqueue` and fails when one names no path, or a path not here.
PATHS: dict[str, Path] = dict(
    [
        # attend (C): the senses' candidates, decided, then enqueued (`attend._enqueue`)
        _p("attend.reminder", "reminder", "his", "nice", "services/reminders.py"),
        _p("attend.leave", "leave", "his", "leave", "services/reminders.py"),
        _p("attend.stay", "done", "pisa", "nice", "services/done.py"),
        _p("attend.check_in", "check_in", "pisa", "nice", "services/follow.py, services/handoffs.py"),
        _p("attend.intake", "info", "outside", "nice", "services/intake.py"),
        _p("attend.replan", "replan", "pisa", "moved", "services/attend.py"),
        _p("attend.weather", "info", "pisa", "nice", "services/attend.py"),
        _p("attend.promise", "info", "pisa", "nice", "services/attend.py"),
        _p(
            "attend.intention",
            "reminder",
            "his",
            "nice",
            "services/intentions.py (cues, waiting-for)",
        ),
        _p("attend.knock", "ask", "pisa", "nice", "services/voice1.py (the knock, voice6's same-day knock)"),
        _p("attend.tragitto", "ask", "pisa", "nice", "services/voice1.py"),
        # the briefing: their trigger (presence, their phrase, their time), and the landing's welcome
        _p("briefing", "briefing", "his", "nice", "services/briefing.py"),
        _p("trip.landed", "briefing", "pisa", "nice", "services/trips.py"),
        _p("trip.leaving", "info", "pisa", "nice", "services/trips.py"),
        # the rituals and what rides on their tick
        _p("ritual", "ritual", "his", "nice", "services/rituals.py"),
        _p("walk.recap", "info", "pisa", "nice", "services/walks.py"),
        _p("people.offer", "person", "person", "nice", "services/people_offers.py"),
        _p("errands.combo", "info", "pisa", "nice", "services/errands.py"),
        _p("wishes.scheduled", "info", "his", "nice", "services/wishes.py"),
        _p("place.note", "reminder", "his", "nice", "services/place_notes.py"),
        # Pisa's own records and its state
        _p("vault.renewal", "info", "pisa", "clock", "services/vault.py"),
        _p("vault.aire", "info", "pisa", "nice", "services/vault.py"),
        _p("learning.weekly", "info", "pisa", "nice", "services/learning.py"),
        _p("sunday.card", "card", "pisa", "nice", "services/ledger.py"),
        _p("places.cap", "alert", "pisa", "money", "services/place_providers.py"),
        _p("voice.rested", "alert", "pisa", "nice", "services/voice.py"),
        _p("voice.cap", "alert", "pisa", "money", "services/voice.py"),
        # approvals: the request, the lapse notice, the outcome
        _p(
            "approval.asked",
            "approval",
            "his_words",
            "nice",
            "services/approvals.py (their own words' change)",
        ),
        _p("approval.request", "approval", "pisa", "nice", "services/approvals.py (Pisa's proposal)"),
        _p("approval.lapsing", "approval", "pisa", "nice", "services/approvals.py"),
        _p("approval.outcome", "info", "his_words", "nice", "services/approvals.py"),
        # answers to their own words or taps (logged, never held)
        _p("talk.last_words", "info", "his_words", "nice", "services/talk.py"),
        _p("profile.first_talk", "info", "his_words", "nice", "services/profile.py"),
        # "Conosciamoci" offered once a week at most, after Pisa said it didn't know what they like
        _p("profile.talk_offer", "info", "pisa", "nice", "services/portrait.py"),
        _p("turn.answer", "info", "his_words", "nice", "POST /v1/turns past its wait (removed; queued ones)"),
        # the holds' own releases
        _p("quiet.summary", "digest", "pisa", "nice", "services/notifications.py (release_held)"),
        _p("guardian.digest", "digest", "pisa", "nice", "services/guardian.py"),
        # the lock screen: its first start of the day carries an alert
        _p(
            "live.start",
            "briefing",
            "pisa",
            "nice",
            "services/live_activity.py (the day's first start: an alert)",
        ),
        _p("live.state", "state", "pisa", "nice", "services/live_activity.py"),
    ]
)
# A notice no path names (a test fails on one in production): today's behaviour, the next breakpoint (an info
# of theirs: 25), never silently folded into a digest
UNMAPPED = Path("unmapped", "info", "his", "nice", "-")
# A notice stored with no path (only tests do; a test fails on a production call): its notification kind's
_BY_KIND: dict[str, Path] = {  # all "his": today's behaviour, the next breakpoint, never a digest
    "reminder": Path("unmapped", "reminder", "his", "nice", "-"),
    "briefing": Path("unmapped", "briefing", "his", "nice", "-"),
    "replan": Path("unmapped", "replan", "his", "moved", "-"),
    "alert": Path("unmapped", "alert", "his", "nice", "-"),
    "check_in": Path("unmapped", "check_in", "his", "nice", "-"),
    "done": Path("unmapped", "done", "his", "nice", "-"),
}


@dataclass(frozen=True)
class Item:
    id: str
    path: Path
    deadline: datetime | None = None  # it matters before this (an event, a lapse, an Undo's end)
    floor: bool = False  # the floors: leave now, an event about to start
    person_id: str | None = None  # whose it is, when it is about one of their people
    outside: bool = False  # its words rest on outside text (an email): never raised by it
    must_show: bool = False  # intake's high priority: shown at least, never folded into a digest

    @property
    def kind(self) -> Kind:
        return self.path.kind

    @property
    def whose(self) -> Whose:
        if self.outside:
            return "outside"
        if self.person_id is not None and self.path.whose == "pisa":
            return "person"
        return self.path.whose


def path_of(name: str | None, kind: str | None = None) -> tuple[Path, bool]:
    """The map's row for a stamped name ("attend.stay:outside" is attend.stay's, outside), and whether its
    words rest on outside text. No name: the notification kind's stand-in (`unmapped`)."""
    if not name:
        return _BY_KIND.get(kind or "", UNMAPPED), False
    base, *marks = name.split(":")
    return PATHS.get(base, UNMAPPED), "outside" in marks


def path_marks(name: str | None) -> set[str]:
    """The marks stamped after a path's name: `outside` (outside text), `shown` (intake's must-show)."""
    return set((name or "").split(":")[1:])


@dataclass(frozen=True)
class Score:
    what: int
    whose: int
    soon: int
    cost: int

    @property
    def total(self) -> int:
        return max(0, min(100, self.what + self.whose + self.soon + self.cost))

    def parts(self) -> dict[str, int]:
        return {
            "what": self.what,
            "whose": self.whose,
            "soon": self.soon,
            "cost": self.cost,
            "total": self.total,
        }


def soon_points(deadline: datetime | None, now: datetime) -> int:
    if deadline is None or deadline <= now:
        return 0
    left = deadline - now
    return next((points for within, points in SOON if left <= within), 0)


def score(item: Item, now: datetime) -> Score:
    """The content first (what, whose, what missing it costs), then the moment (how soon)."""
    return Score(
        WHAT.get(item.kind, 0),
        WHOSE[item.whose],
        soon_points(item.deadline, now),
        0 if item.outside else COST[item.path.cost],  # outside text never makes it costlier
    )


@dataclass(frozen=True)
class Rule:
    """Their rule: "always interrupt for X" / "never for Y" (who, what kind)."""

    mode: Mode
    kind: str | None = None
    person_id: str | None = None
    source: Literal["settings", "words"] = "settings"

    @property
    def specificity(self) -> int:
        return (self.kind is not None) + 2 * (self.person_id is not None)


def rule_for(item: Item, rules: Sequence[Rule]) -> Rule | None:
    """The most specific of their rules that names this item (a person before a kind; both before either);
    between two alike, "always" (a missed train costs more than one more word)."""
    found = [
        r
        for r in rules
        if (r.kind is not None or r.person_id is not None)
        and (r.kind is None or r.kind == item.kind)
        and (r.person_id is None or r.person_id == item.person_id)
    ]
    if not found:
        return None
    return max(found, key=lambda r: (r.specificity, r.mode == "always"))


@dataclass(frozen=True)
class Busy:
    reason: Literal["back_to_back", "meeting", "talk", "driving", "talking", "call", "focus"]
    until: datetime


# the phone's breakpoints (`pisa.domain.app_events.BREAKPOINTS`), read by the event's `at`
@dataclass(frozen=True)
class PhoneEvent:
    event: str
    site: str | None
    state: str | None
    at: datetime


@dataclass(frozen=True)
class Phone:
    """What the phone said, as of now: when a busy state began (None: not in it), and the breakpoint its last
    events name (`call_end`, `walk_end`), if one ended within `JUST_ENDED`."""

    call: datetime | None = None
    driving: datetime | None = None
    focus: datetime | None = None
    ended: str | None = None


_ENDS = frozenset({"call_ended", "focus_off"})


def _previous(site: str | None) -> str | None:
    if site is None or not site.startswith("motion.") or site == "motion.unknown":
        return None
    return site.removeprefix("motion.")


def phone_state(events: Sequence[PhoneEvent], now: datetime) -> Phone:
    """The phone's states by each event's own `at` (never its arrival): a call from `call_started` until
    `call_ended`; driving from `activity=automotive` until another state; Focus from `focus_on` until
    `focus_off`. Each expires `HOLD_MAX` after it began when nothing ended it (the phone can miss the end
    while suspended); a later start confirms it. `call_ended`, and an `activity` from walking to stationary,
    are breakpoints only when their `at` is within `JUST_ENDED` of now (a late upload is none)."""
    # the phone dates in whole seconds: an end in the same second as its start comes after it
    seen = sorted((e for e in events if e.at <= now), key=lambda e: (e.at, e.event in _ENDS))

    def last(*names: str) -> PhoneEvent | None:
        return next((e for e in reversed(seen) if e.event in names), None)

    def open_since(event: PhoneEvent | None, start: str, state: str | None = None) -> datetime | None:
        if event is None or event.event != start or (state is not None and event.state != state):
            return None
        return event.at if now - event.at < HOLD_MAX else None

    call = last("call_started", "call_ended")
    moves = [e for e in seen if e.event == "activity"]
    move = moves[-1] if moves else None
    ended: str | None = None
    if call is not None and call.event == "call_ended" and now - call.at <= JUST_ENDED:
        ended = "call_end"
    elif move is not None and move.state == "stationary" and now - move.at <= JUST_ENDED:
        before = _previous(move.site)
        if before is None and move.site == "motion.unknown" and len(moves) > 1:
            before = moves[-2].state  # the phone didn't know: the state it sent before
        if before == "walking":
            ended = "walk_end"
    return Phone(
        call=open_since(call, "call_started"),
        driving=open_since(move, "activity", "automotive"),
        focus=open_since(last("focus_on", "focus_off"), "focus_on"),
        ended=ended,
    )


def _open_until(since: datetime, now: datetime) -> datetime:
    """A state with no known end: looked at again after `RECHECK`, never past its own expiry."""
    return min(now + RECHECK, since + HOLD_MAX)


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    meeting: bool  # others invited, or a meeting word
    talk: bool = False  # a talk they give (a presentation word)


def event(start: datetime, end: datetime, title: str, others: int) -> Event:
    talk = bool(_TALK_WORDS.search(title or ""))
    return Event(start, end, others > 0 or bool(_MEETING_WORDS.search(title or "")) or talk, talk)


def busy(
    events: Sequence[Event], now: datetime, *, driving: bool = False, talking: bool = False,
    phone: Phone | None = None,
) -> Busy | None:  # fmt: skip
    """The day's density now (never said as a state): an exchange with Pisa, a call, driving (the fixes or the
    phone's motion), a talk they give, a meeting, back-to-back meetings (two or more, each within `B2B_GAP` of
    the next: the whole block), or a Focus on (Focus counts as busy) raise the floor."""
    phone = phone or Phone()
    if talking:
        return Busy("talking", now + TALK_HOLD)
    if phone.call is not None:
        return Busy("call", _open_until(phone.call, now))
    if phone.driving is not None:
        return Busy("driving", _open_until(phone.driving, now))
    if driving:
        return Busy("driving", now + RECHECK)
    meetings = sorted((e for e in events if e.meeting), key=lambda e: e.start)
    for e in meetings:
        if e.talk and e.start <= now < e.end:
            return Busy("talk", e.end)
    current = next((e for e in meetings if e.start <= now < e.end), None)
    if current is None:
        return Busy("focus", _open_until(phone.focus, now)) if phone.focus is not None else None
    start, end, count = current.start, current.end, 1
    for e in reversed([m for m in meetings if m.end <= current.start]):
        if start - e.end > B2B_GAP:
            break
        start, count = e.start, count + 1
    for e in (m for m in meetings if m.start >= current.start and m is not current):
        if e.start - end > B2B_GAP:
            break
        end, count = max(end, e.end), count + 1
    # one meeting raises the floor too; back to back, until the block ends
    return Busy("back_to_back", end) if count >= 2 else Busy("meeting", current.end)


def in_meeting(events: Sequence[Event], now: datetime) -> Event | None:
    return next((e for e in events if e.meeting and e.start <= now < e.end), None)


def at_breakpoint(
    events: Sequence[Event],
    now: datetime,
    *,
    moving: bool = False,
    talking: bool | None = None,
    last_words_at: datetime | None = None,
    arrived_at: datetime | None = None,
    phone: Phone | None = None,
) -> str | None:
    """Whether this moment is a breakpoint, and which: a meeting block that just ended (`calendar_gap`), a
    call that just ended (`call_end`), a walk that just ended (`walk_end`: the phone's walking → stationary,
    or a stay begun), a talk with Pisa that just ended (`talk_end`), or `idle`. None while they move, is on a
    call, drives, is in a meeting or is talking to Pisa. `talking`: the live sockets' word (the API's), or
    None where they are not known (the worker): then their words in the last `IDLE_AFTER` mean they are."""
    phone = phone or Phone()
    if moving or talking or in_meeting(events, now) is not None:
        return None
    if phone.call is not None or phone.driving is not None:
        return None
    if talking is None and last_words_at is not None and now - last_words_at < IDLE_AFTER:
        return None
    if any(e.meeting and now - JUST_ENDED <= e.end <= now for e in events):
        return "calendar_gap"
    if phone.ended is not None:
        return phone.ended
    if arrived_at is not None and now - JUST_ENDED <= arrived_at <= now:
        return "walk_end"
    if last_words_at is not None and now - last_words_at <= JUST_ENDED:
        return "talk_end"
    return "idle"


def next_check(events: Sequence[Event], now: datetime, found: Busy | None) -> datetime:
    """When a hold is looked at again: the busy block's end, the meeting's end, else after `RECHECK`."""
    if found is not None:
        return found.until
    meeting = in_meeting(events, now)
    return meeting.end if meeting is not None else now + RECHECK


def decay_level(outcomes: Sequence[str]) -> int:
    """`outcomes`: the kind's latest, newest first (replied, tapped, dismissed, ignored). Each `DECAY_EVERY`
    ignored or dismissed in a row since their last reply or tap halves the kind's rate once more."""
    run = 0
    for outcome in outcomes:
        if outcome in ("replied", "tapped"):
            break
        run += 1
    return min(run // DECAY_EVERY, DECAY_MAX)


@dataclass(frozen=True)
class Moment:
    now: datetime
    rules: Sequence[Rule] = ()
    busy: Busy | None = None
    breakpoint: str | None = "idle"  # None: this moment is no breakpoint
    until: datetime | None = None  # when a hold is looked at again
    decay: int = 0  # the item's kind's decay level
    decayed_since_pass: int = 0  # the kind's items held for decay since one of its own last went out
    digest_at: datetime | None = None  # the next digest
    quiet: bool = False  # a hold's re-check in quiet hours: it waits, its deadline too


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str
    score: Score
    until: datetime | None = None  # a hold: when it is looked at again (the breakpoint's or the digest's)
    released: bool = False  # it goes now (a `now`, or a breakpoint this moment already is)
    rule: Rule | None = None
    parts: dict[str, int] = field(default_factory=dict)
    silent: bool = False  # shown on the phone, no sound and no voice (their "never" over a floor)

    @property
    def override(self) -> Mode | None:
        return self.rule.mode if self.rule is not None else None


def attend(item: Item, moment: Moment) -> Verdict:
    """One gate for every unprompted word: `now | breakpoint | digest`, with its reason (module docstring)."""
    s = score(item, moment.now)

    def verdict(decision: Decision, reason: str, *, rule: Rule | None = None) -> Verdict:
        if decision == "digest" and (
            item.must_show
            or (item.deadline is not None and (moment.digest_at is None or item.deadline <= moment.digest_at))
        ):
            # never digested when its deadline comes before the digest, nor when it must be
            # shown: the next breakpoint (at once when this moment is one), else before its deadline
            decision, reason = "breakpoint", f"{reason}_kept"
        if decision == "now":
            return Verdict("now", reason, s, None, True, rule, s.parts())
        if decision == "digest":
            return Verdict("digest", reason, s, moment.digest_at, False, rule, s.parts())
        if moment.busy is None and moment.breakpoint is not None:
            return Verdict("breakpoint", f"{reason}:{moment.breakpoint}", s, None, True, rule, s.parts())
        # a hold lasts at most HOLD_MAX, and never past the item's deadline (its event, its lapse): then now
        until = min(moment.until or moment.now + HOLD_MAX, moment.now + HOLD_MAX)
        # as the tick, quiet hours hold a deadline too
        if item.deadline is not None and item.deadline - DEADLINE_MARGIN <= until and not moment.quiet:
            return Verdict("now", "deadline", s, None, True, rule, s.parts())
        return Verdict("breakpoint", reason, s, until, False, rule, s.parts())

    if item.whose == "his_words":
        return verdict("now", "his_words")
    rule = rule_for(item, moment.rules)
    if (
        rule is not None
        and rule.mode == "never"
        and item.deadline is not None
        and not item.floor
        and (moment.digest_at is None or item.deadline <= moment.digest_at)
    ):
        # their "never" over an item due before the digest (an approval's lapse, an event): out, but silently
        return Verdict("now", "his_rule_never_silent", s, None, True, rule, s.parts(), silent=True)
    if rule is not None and rule.mode == "never" and item.floor:
        # their "never" over a floor (leave now) shows it silently, never digested
        return Verdict("now", "his_rule_never_floor", s, None, True, rule, s.parts(), silent=True)
    if rule is not None:
        return verdict("now" if rule.mode == "always" else "digest", f"his_rule_{rule.mode}", rule=rule)
    if item.floor:
        return verdict("now", "floor")
    if item.kind == "state":
        return verdict("now", "silent")
    if item.kind == "digest":  # a digest itself (the quiet hours', the guardian's): at a breakpoint only
        return verdict("breakpoint", "digest")
    if moment.quiet:
        # a hold's re-check in quiet hours waits, whatever its
        # score and deadline (as the tick); floors and their rules, above, still go
        return verdict("breakpoint", "quiet")
    floor = BUSY_NOW_AT if moment.busy is not None else NOW_AT
    if s.total >= floor:
        return verdict("now", "score")
    # decay thins Pisa's own nudges only: never their items (their reminders, their briefing, what they
    # scheduled), never an answer to their words, never an item with a deadline
    decays = item.whose in DECAYS and item.deadline is None
    if decays and moment.decay and moment.decayed_since_pass < 2**moment.decay - 1:
        return verdict("digest", f"decay_{moment.decay}")
    if s.total >= BREAKPOINT_AT:
        return verdict("breakpoint", f"busy_{moment.busy.reason}" if moment.busy else "score")
    return verdict("digest", "low_score")


def next_digest(now: datetime, tz: str) -> datetime:
    """The next digest slot in their time (`DIGEST_TIMES`), today's or tomorrow's first."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    for slot in DIGEST_TIMES:
        at = datetime.combine(local.date(), slot, tzinfo=zone)
        if at > local:
            return at
    return datetime.combine(local.date() + timedelta(days=1), DIGEST_TIMES[0], tzinfo=zone)


def last_digest_slot(now: datetime, tz: str) -> datetime:
    """The latest digest slot at or before now (yesterday's last before today's first)."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    for slot in reversed(DIGEST_TIMES):
        at = datetime.combine(local.date(), slot, tzinfo=zone)
        if at <= local:
            return at
    return datetime.combine(local.date() - timedelta(days=1), DIGEST_TIMES[-1], tzinfo=zone)


def digest_text(texts: Sequence[str], lang: str) -> str:
    opening = DIGEST_PREFIX.get(lang, DIGEST_PREFIX["en-US"])
    body = " ".join(t.strip() if t.strip().endswith((".", "!", "?", "…")) else t.strip() + "." for t in texts)
    text = opening + body
    return text if len(text) <= DIGEST_MAX_CHARS else text[: DIGEST_MAX_CHARS - 1].rstrip() + "…"


# --- their rules in words (through the memory tools: a claim they said)
# -----------------------------------------
_ALWAYS = re.compile(r"\b(?:sempre|always|anche\s+se|even\s+if|comunque|whatever)\b", re.IGNORECASE)
# "never" only when the negation governs the interrupting verb ("non disturbarmi", "non mi
# avvisare", "mai interrompermi", "don't bother me"), never a negation elsewhere ("non farmeli perdere")
_NEVER = re.compile(
    r"\b(?:non|mai)\s+(?:mi\s+|me\s+|ti\s+)?(?:interromp|disturb|avvis|chiam|svegli|notific|parlarmi|"
    r"dirmelo)\w*|\b(?:don'?t|do\s+not|never)\s+(?:ever\s+)?(?:interrupt|disturb|bother|ping|notify|wake|"
    r"call)\b",
    re.IGNORECASE,
)
# a sentence whose mode can't be read for certain makes no rule
# (Pisa keeps their words as they are, and asks if it matters): an exception ("tranne per Marta", "se non per
# i treni", unless), a condition ("se non mi avvisi…", "if…"), a complaint ("mi interrompi sempre… basta",
# "you always interrupt me"), a modifier on the moment rather than the kind ("all'ultimo minuto", "tardi",
# "late"), "sempre" under a negation ("non interrompermi sempre"), or both modes in one sentence ("avvisami
# sempre per Marta, non disturbarmi per i rituali")
_UNSURE = re.compile(
    r"\b(?:tranne|eccetto|salvo|a\s+meno\s+che|se\s+non|unless|except|but\s+for|other\s+than|basta|smettila|"
    r"stop\s+it|tardi|late|last[\s-]minute|troppo\s+(?:presto|tardi)|too\s+(?:early|late))\b"
    r"|all['\u2019]ultimo|^\s*(?:se|if)\b"
    r"|\b(?:mi\s+(?:interrompi|disturbi|avvisi|chiami|svegli)|you\s+(?:always\s+|keep\s+)?"
    r"(?:interrupt|bother|disturb|notify|ping|wake))\b"
    r"|\bnon\s+\w+\s+sempre\b",
    re.IGNORECASE,
)
_INTERRUPT = re.compile(
    r"\b(?:interromp\w*|disturb\w*|avvis\w*|chiam\w*|interrupt\w*|ping\w*|notif\w*|bother\w*|tell\s+me|"
    r"dimmelo|dirmelo|parlami|parlarmi|svegli\w*|wake)\b",
    re.IGNORECASE,
)
KIND_WORDS: dict[str, re.Pattern[str]] = {
    "leave": re.compile(r"\b(?:partenz\w*|uscire|tren[oi]|train\w*|leave|leaving|departures?)\b", re.I),
    "reminder": re.compile(r"\b(?:promemoria|reminders?)\b", re.I),
    "approval": re.compile(r"\b(?:approvazion\w*|approvals?|conferme)\b", re.I),
    "check_in": re.compile(r"\b(?:check-?ins?|com'è andata)\b", re.I),
    "briefing": re.compile(r"\b(?:briefing|buongiorno)\b", re.I),
    "ask": re.compile(r"\b(?:domande|questions?|posso dirti)\b", re.I),
    "ritual": re.compile(r"\b(?:ritual\w*)\b", re.I),
    "person": re.compile(r"\b(?:persone|amici|people|friends)\b", re.I),
    "card": re.compile(r"\b(?:scheda|card)\b", re.I),
    "replan": re.compile(r"\b(?:piano|plan|replan\w*|ripianific\w*)\b", re.I),
    "alert": re.compile(r"\b(?:allarm\w*|alerts?|avvisi)\b", re.I),
    "info": re.compile(r"\b(?:novità|news|curiosità|info)\b", re.I),
}
_NAME = re.compile(r"\b(?:per|for|da|from|di|about|su)\s+([A-ZÀ-Ý][\w'\u2019-]+)")


def rule_from_words(statement: str, people: Sequence[tuple[str, str]]) -> Rule | None:
    """Their claim as a rule ("Interrompimi sempre per Marta", "non disturbarmi mai per i rituali"), or None.
    `people`: (id, name_key). The person is one of their people named after "per/for/da/from"; the kind one of
    `KIND_WORDS`. "Never" needs a negation of the interrupting verb itself and no "always"; "always" needs
    "sempre/always" (or "anche se")."""
    if not _INTERRUPT.search(statement) or _UNSURE.search(statement):
        return None
    always = bool(_ALWAYS.search(statement))
    never = bool(_NEVER.search(statement))
    if always == never:  # neither, or both in one sentence: no rule
        return None
    kind = next((k for k, pattern in KIND_WORDS.items() if pattern.search(statement)), None)
    person = None
    keys = {key: pid for pid, key in people}
    for m in _NAME.finditer(statement):
        key = people_name_key(m.group(1))
        found = keys.get(key) or next((pid for k, pid in keys.items() if k.split(" ")[0] == key), None)
        if found is not None:
            person = found
            break
    if kind is None and person is None:
        return None
    return Rule("always" if always else "never", kind, person, "words")


def settings_rules(raw: object) -> list[Rule]:
    """Settings → Pisa decides when to speak: {"rules": [{"mode", "kind", "person_id"}]} (validated on
    write)."""
    rules = raw.get("rules") if isinstance(raw, dict) else None
    out: list[Rule] = []
    for r in rules if isinstance(rules, list) else []:
        if isinstance(r, dict) and r.get("mode") in ("always", "never"):
            out.append(Rule(r["mode"], r.get("kind"), r.get("person_id"), "settings"))
    return out


# --- the record's readers -----------------------------------------------------------------------------------
def counted(reason: str, kind: str = "") -> bool:
    """On the card: an unprompted item; never a reply to their words, a silent state, or a digest itself."""
    return reason not in NOT_COUNTED and kind != "digest"


def card_line(held: int, passed: int, never_out: int, lang: str) -> str:
    """The Sunday card's one line (a line on the existing card)."""
    key = "it-IT" if lang == "it-IT" else "en-US"
    never = CARD_NEVER[key].format(n=never_out) if never_out else ""
    return CARD_LINE[key].format(held=held, passed=passed, never=never)


def measures(
    rows: Sequence[tuple[str, str, str | None, bool, bool, bool]], dismissals: int, days: int
) -> dict[str, object]:
    """The user report's numbers. `rows`: (decision, reason, override, released, lapsed, at_once) for the
    period's counted items; `dismissals`: their swipes on Pisa's notices in it. Held: what waited (a
    breakpoint verdict at a moment that already was one passed at once)."""
    held = [r for r in rows if not r[5]]
    released = sum(1 for r in held if r[3])
    return {
        "items": len(rows),
        "passed": len(rows) - len(held),
        "held": len(held),
        "released": released,
        "never_out": sum(1 for r in held if r[4]),
        "held_to_released": round(len(held) / released, 2) if released else None,
        "overrides": {
            "always": sum(1 for r in rows if r[2] == "always"),
            "never": sum(1 for r in rows if r[2] == "never"),
        },
        "dismissals_per_day": round(dismissals / max(days, 1), 2),
    }
