"""Pisa's
morning thought.

At most one line a day, said after the first automatic briefing's text, chosen by code from a record they gave
Pisa themselves, never from where they went or how they seem (Pisa never shows it is watching them):

- **welcome_back**: a trip they confirmed brought them home in the last 18 hours, and the phone saw them land
  (the return leg in `landed_keys`, nothing cancelled or moved, their zone no longer the trip's). "Bentornato
  da New York. Com'è andata?" An open question: their answer is a talk like any other, nothing is kept about
  it.
- **stuck_task**: the task of their own (never one made from an email: a stranger wrote its title) waiting
  longest in their pool with no deadline (14+ days, never asked about: the overdue ones have the aging
  check-in). "Stanotte pensavo a «…»: è in lista da 21 giorni. Ti va di trovare un primo passo da dieci
  minuti?" ("Stanotte" only in the morning). A one-yes question (the rituals' `thought` ask): yes asks for the
  step, naming the task (nothing changes until they say it), no stops this kind for 30 days, silence lapses at
  the day's end.

The words are fixed templates (no model call: `MODEL_CALLS` is the cap, and it is 0). Never the same record
twice, never two thoughts a day.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

Kind = Literal["welcome_back", "stuck_task"]
ORDER: tuple[Kind, ...] = ("welcome_back", "stuck_task")  # the rarer, time-bound one first
MODEL_CALLS = 0  # the cost cap, in code: a thought is a template, never a model's words
BACK_WITHIN = timedelta(hours=18)  # "the morning after": home this recently
STUCK_DAYS = 14  # in their list this long, with no deadline
DECLINED_FOR = timedelta(days=30)  # their "no" silences that kind this long
TITLE_MAX = 80
CITY_MAX = 60
MORNING_UNTIL = 12  # "stanotte" is said only before noon, their time


@dataclass(frozen=True)
class Candidate:
    kind: Kind
    ref: str  # the trip's or the task's id
    name: str  # the trip's city, the task's title
    days: int = 0  # stuck_task: days in their list


@dataclass(frozen=True)
class Thought:
    kind: Kind
    ref: str
    text: str
    question: str | None  # the one-yes question in it (stuck_task), else None (an open question)


def _clean(text: str, limit: int) -> str:
    """One line, no guillemets of its own, at most `limit` characters (cut at a word)."""
    flat = " ".join(text.replace("«", "").replace("»", "").replace('"', "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return (cut or flat[:limit]) + "…"


def welcome_back(city: str, lang: str) -> tuple[str, None]:
    place = _clean(city, CITY_MAX)
    if lang == "it-IT":
        return f"Bentornato da {place}. Com'è andata?", None
    return f"Welcome back from {place}. How did it go?", None


def stuck_task(title: str, days: int, lang: str, morning: bool = True) -> tuple[str, str]:
    what = _clean(title, TITLE_MAX)
    if lang == "it-IT":
        question = "Ti va di trovare un primo passo da dieci minuti?"
        head = "Stanotte pensavo a" if morning else "Pensavo a"
        return f"{head} «{what}»: è in lista da {days} giorni. {question}", question
    question = "Shall we find a ten-minute first step?"
    head = "Last night I was thinking about" if morning else "I was thinking about"
    return f"{head} “{what}”: it has been on your list for {days} days. {question}", question


def pick(candidates: list[Candidate], declined: set[str], lang: str, morning: bool = True) -> Thought | None:
    """The first kind in ORDER with a candidate they haven't silenced; None: nothing worth saying
    (silence)."""
    for kind in ORDER:
        if kind in declined:
            continue
        found = next((c for c in candidates if c.kind == kind and c.name.strip()), None)
        if found is None:
            continue
        if kind == "welcome_back":
            text, question = welcome_back(found.name, lang)
        else:
            if found.days < STUCK_DAYS:
                continue
            text, question = stuck_task(found.name, found.days, lang, morning)
        return Thought(kind, found.ref, text, question)
    return None


def answered(yes: bool, lang: str, title: str | None = None) -> str:
    """Their bare yes or no to the stuck task's question: yes asks for the step, naming the task (read again
    at answer time, so the next turn knows which one), and they say it (the task tools take their words); no
    leaves it as it is."""
    what = _clean(title, TITLE_MAX) if title else None
    if lang == "it-IT":
        if not yes:
            return "Va bene, lo lascio com'è."
        if not what:
            return "Bene: qual è il primo passo?"
        return f"Bene: qual è il primo passo per «{what}»? Lo segno."
    if not yes:
        return "Okay, I'll leave it as it is."
    if not what:
        return "Good: what's the first step?"
    return f"Good: what's the first step for “{what}”? I'll note it."
