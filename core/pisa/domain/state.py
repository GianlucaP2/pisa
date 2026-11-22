"""The shared conversation state. One per device, and every part of Pisa reads and
writes it: the talker (Pisa's voice), the reflex on the user's partial words, the main thinker on the final
ones.

- **The user's goal now**: what the reflex read in the words being said (or the last final words).
- **What Pisa said**, by voice or by its brain, and whether code backed it (a `done`); **what Pisa promised**
  ("ti avviso alle 5", "I'll check"), kept two hours.
- **What waits for the user's yes**: Pisa's ledger (code's, from the approvals).
- **Pisa's own data read for these words**: the reflex's prefetch (the calendar, tasks, the plan), with the
  reflex's **ready answer** drafted from it.

RAIL: the talker's facts come only from this state, the user's words or a whisper:
`supports()` is what grounding checks Pisa's words against, besides the whispers. Nothing here is the user's
words stored: they live in the state only while the conversation goes on (memory, never the database).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pisa.domain.speculation import follows

SAID_KEPT = 8  # Pisa's latest lines kept
FACTS_FOR = timedelta(minutes=10)  # Pisa's data read for some words: old after this (the calendar moves)
PROMISED_FOR = timedelta(hours=2)
STATE_FOR = timedelta(minutes=30)  # as NEW_CONVERSATION_AFTER: a new conversation starts a fresh state
WAITING_KEPT = 4
LINE = 240  # each line, at most
By = Literal["voice", "brain"]

# Pisa's promise: something it said it will do later (it tells, reminds, checks): the conversation keeps it.
_PROMISE = re.compile(
    r"\b(?:ti (?:avviso|avviserò|ricordo|ricorderò|dico|dirò|faccio sapere|farò sapere|scrivo|richiamo|"
    r"aggiorno)|ci penso io|lo controllo|controllo e ti|te lo (?:dico|ricordo|mando|faccio sapere)|"
    r"i'?ll (?:remind|let you know|check|tell you|send|look into|call|get back)|i will (?:remind|let you "
    r"know|check|tell you|send|get back))\b",
    re.IGNORECASE,
)


def one_line(text: str) -> str:
    return " ".join(text.split())


def promises(text: str) -> bool:
    return bool(_PROMISE.search(text))


@dataclass(frozen=True)
class Said:
    text: str
    by: By
    at: datetime
    backed: bool = False  # a `done` code backed (Pisa's record)


# a read's text starting with this holds a stranger's words (an
# invite's or an email task's title, a provider's place): the fact is outside text
OUTSIDE_MARK = "\x1f"


@dataclass(frozen=True)
class Fact:
    read: str  # agenda, tasks, plan, waiting, travel
    text: str
    at: datetime
    words: str  # the user's words it was read for
    outside: bool = False  # it holds a title or a place a stranger wrote


@dataclass(frozen=True)
class Ready:
    """The reflex's obvious answer, drafted from Pisa's own data only, for the words `words`."""

    text: str
    words: str
    at: datetime


@dataclass
class ConversationState:
    goal: str = ""
    goal_words: str = ""  # the words the goal was read from
    said: list[Said] = field(default_factory=list)
    promised: list[Said] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    ready: Ready | None = None
    brain_seen: int = 0  # how many of Pisa's voice lines (`said` by voice, ever) the brain has read
    voice_lines: int = 0
    touched: datetime | None = None  # last used; past STATE_FOR the conversation is a new one

    # --- writes ---------------------------------------------------------------------------------------------

    def read_goal(self, goal: str, words: str) -> None:
        self.goal, self.goal_words = one_line(goal)[:LINE], words

    def add_fact(self, fact: Fact) -> None:
        """One fact per read and words: a newer read of the same kind replaces the older."""
        self.facts = [*[f for f in self.facts if f.read != fact.read][-5:], fact]

    def set_ready(self, ready: Ready | None) -> None:
        self.ready = ready

    def pisa_said(
        self, text: str, by: By, at: datetime, *, backed: bool = False, retried: bool = False
    ) -> None:
        """Pisa said `text`. `retried`: the talker's retry of its own `interrupted` line in the same turn:
        only then does a line that grows the last one replace it; otherwise two lines, as said."""
        line = Said(one_line(text)[:LINE], by, at, backed)
        if not line.text:
            return
        last = self.said[-1] if self.said else None
        if retried and last is not None and last.by == by and grows(last.text, line.text):
            self.said = [*self.said[:-1], line]  # the retry's whole line replaces its cut one
            return
        self.said = [*self.said, line][-SAID_KEPT:]
        if by == "voice":
            self.voice_lines += 1
        if promises(line.text):
            self.promised = [*self.promised, line][-4:]

    def set_waiting(self, summaries: list[str]) -> None:
        self.waiting = [one_line(s)[:LINE] for s in summaries][:WAITING_KEPT]

    def brain_read(self) -> None:
        """The brain read the state: Pisa's voice lines so far are no longer news to it."""
        self.brain_seen = self.voice_lines

    # --- reads ----------------------------------------------------------------------------------------------

    def fresh_facts(self, now: datetime) -> list[Fact]:
        return [f for f in self.facts if now - f.at <= FACTS_FOR]

    def kept_promises(self, now: datetime) -> list[Said]:
        return [p for p in self.promised if now - p.at <= PROMISED_FOR]

    def voice_since_brain(self) -> list[str]:
        """Pisa's voice lines the brain hasn't read (the talker answered something by itself, a filler, a
        correction): the brain knows what Pisa already said."""
        new = self.voice_lines - self.brain_seen
        voice = [s.text for s in self.said if s.by == "voice"]
        return voice[-new:] if new > 0 else []

    def supports(self, now: datetime) -> list[str]:
        """RAIL: what Pisa's voice may state besides the whispers and the user's words: Pisa's own data read
        for these words, the ready answer, the ledger."""
        out = [f.text for f in self.fresh_facts(now)] + list(self.waiting)
        if self.ready is not None and now - self.ready.at <= FACTS_FOR:
            out.append(self.ready.text)
        return out

    def for_brain(
        self,
        now: datetime,
        words: str,
        *,
        wrap: Callable[[str], str] = lambda t: t,
        wrap_outside: Callable[[str], str] | None = None,
    ) -> str:
        """The state for the main thinker's dynamic instructions (after the cached prefix). `wrap` marks
        Pisa's data (titles may come from an invite) as data."""
        lines: list[str] = []
        if self.goal and follows(self.goal_words, words):
            lines.append(f"- The user's goal, as Pisa's reflex read it: {self.goal}")
        if voice := self.voice_since_brain():
            lines.append(
                "- Pisa's voice already said, since your last answer: " + " / ".join(f'"{v}"' for v in voice)
            )
        if promised := self.kept_promises(now):
            lines.append("- Pisa promised: " + " / ".join(f'"{p.text}"' for p in promised))
        if self.waiting:
            lines.append("- Waits for the user's yes: " + "; ".join(self.waiting))
        facts = [f for f in self.fresh_facts(now) if follows(f.words, words)]
        ready = self.ready
        if ready is not None and not (now - ready.at <= FACTS_FOR and follows(ready.words, words)):
            ready = None
        if facts:
            data = [f"{f.read}: {f.text}" for f in facts]
            if ready is not None:
                data.append(f'Pisa\'s reflex drafted from it: "{ready.text}"')
            lines.append(
                "- Pisa's own data, read for these words just now (the same as the tools' results: don't "
                "read it again unless you need more). If the reflex's draft answers the user, say it in your "
                "words; if they asked for more or a change, do that:\n"
                + (wrap_outside or wrap if any(f.outside for f in facts) else wrap)("\n".join(data))
            )
        if not lines:
            return ""
        return (
            "\n\nThe conversation now (shared with Pisa's voice; Pisa's own record, not the user's words):\n"
            + ("\n".join(lines))
        )

    def for_talker(self, now: datetime) -> str:
        """The state for Pisa's voice (a SILENT whisper when it changes): what it may say besides Pisa's
        answers."""
        lines: list[str] = []
        # what Pisa said, what waits and Pisa's data are data in «…» for the talker, never its rules
        if promised := self.kept_promises(now):
            lines.append("You promised: " + " / ".join(f"«{p.text}»" for p in promised))
        if self.waiting:
            lines.append(
                "Waiting for the user's yes (NOT done): " + "; ".join(f"«{w}»" for w in self.waiting)
            )
        for f in self.fresh_facts(now):
            lines.append(f"Pisa's data ({f.read}): «{f.text}»")
        return "\n".join(lines)


def numbers(text: str) -> set[str]:
    """The times and numbers a line states ("17:30" → "17:30", "alle 5" → "5")."""
    return set(re.findall(r"\d{1,2}[:.]\d{2}|\d+", text))


# A ready answer is said before Pisa's brain checks it, so it says only what Pisa's data shows: no negation
# ("non hai niente": the reads may have failed), no change claimed.
_NEGATED = re.compile(r"\b(?:non|niente|nessun\w*|nulla|no|nothing|none|not|never|mai|nope)\b", re.IGNORECASE)
_NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-ZÀ-Ý][\w'\u2019-]+")


def grounded(text: str, data: list[str]) -> bool:
    """RAIL: a ready answer states at least one time or number, and every one of them is in Pisa's own data;
    every name in it (a capitalised word past a sentence's start) is in that data; it negates nothing and
    claims no change (a draft with no number passed by itself, "Sì, domani hai il
    dentista" with no dentist in Pisa's data)."""
    from pisa.domain.claims import claims_change

    said = numbers(text)
    if not said or _NEGATED.search(text) or claims_change(text):
        return False
    have: set[str] = set()
    for d in data:
        have |= numbers(d)
        have |= {n.replace(".", ":") for n in numbers(d)}
    if not all(n in have or n.replace(".", ":") in have for n in said):
        return False
    joined = " ".join(data).casefold()
    return all(name.casefold() in joined for name in _NAME.findall(text.strip()))


def grows(cut: str, whole: str) -> bool:
    """`whole` is the line `cut` began (Gemini's `interrupted` then its retry: "Sono le" / "Sono le
    19:59.")."""
    head = cut.strip().rstrip(".…!? ").casefold()
    return bool(head) and len(whole.strip()) > len(cut.strip()) and whole.strip().casefold().startswith(head)
