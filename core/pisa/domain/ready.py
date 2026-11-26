"""Ready answers: Pisa keeps answers ready for the likely questions (what's next and when to
leave, today's changes, the pool's top items, what Pisa is looking into), so a question that matches one is
answered at once; the brain's turn only checks that nothing changed since.

RAIL: never for a change ("spostala", "mettimi…": the brain acts on those); built only from Pisa's own data
(the plan, Pisa's own record of its changes, the user's tasks, Pisa's promises), never from outside text (an
email's task, a change made after outside text), so nothing unread this turn is said.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Topic = Literal["next", "changes", "pool", "research"]
NOT_NEXT = frozenset({"skipped", "done", "visited"})  # a plan item's statuses that are no "next"
MAX_AGE = timedelta(minutes=5)  # refreshed on every change, and an answer older than this is stale

_TOPICS: tuple[tuple[Topic, re.Pattern[str]], ...] = (
    ("next", re.compile(
        r"\b(?:cosa|che)\s+(?:ho|c'è|c'e|faccio)\s+(?:dopo|adesso|ora)\b|\b(?:il\s+)?prossimo\s+impegno\b|"
        r"\bquando\s+(?:devo\s+)?(?:partire|uscire|parto|esco|mi\s+muovo)\b|\bwhat'?s\s+next\b|"
        r"\bwhat\s+(?:do\s+i\s+have\s+)?next\b|\bwhen\s+(?:do|should)\s+i\s+leave\b", re.I)),
    ("changes", re.compile(
        r"\bcosa\s+(?:hai|è|e')\s+(?:cambiato|spostato|fatto|cambiat[oa])\s+oggi\b|\bcos'?è\s+cambiato\b|"
        r"\bche\s+cambiamenti\b|\bwhat\s+(?:did\s+you\s+)?(?:change|changed)\b(?:\s+today)?|"
        r"\bwhat\s+have\s+you\s+(?:changed|done)\s+today\b", re.I)),
    ("pool", re.compile(
        r"\bcosa\s+(?:dovrei|devo|posso)\s+fare\b|\bcosa\s+c'è\s+da\s+fare\b|\bcosa\s+ho\s+da\s+fare\b|"
        r"\bwhat\s+should\s+i\s+(?:do|work\s+on)\b|\bwhat'?s\s+on\s+my\s+list\b|\bmy\s+top\s+tasks\b", re.I)),
    ("research", re.compile(
        r"\b(?:cosa|che)\s+stai\s+(?:cercando|facendo|guardando|studiando)\b|\ba\s+cosa\s+stai\s+lavorando\b|"
        r"\bwhat\s+are\s+you\s+(?:researching|working\s+on|looking\s+into|doing)\b", re.I)),
)  # fmt: skip
# Words that ask for a change: never answered from a ready answer (the brain acts on them). Every
# form of the verb ("rimandalo", "spostiamolo", "mettilo", "segnami", "posticipa"), never its past
# participle ("cosa hai cambiato oggi?" asks what changed)
_CHANGE_STEM = re.compile(
    r"^(?:spost|mett|aggiung|cancell|annull|togl|elimin|rimand|posticip|anticip|segn|fiss|prenot|mand|invi|"
    r"cambi|modific|sposti|ricord)\w*$"
)
_PARTICIPLE = re.compile(r"(?:at|ut|it|ess|olt)[oaie]$")
_EN_CHANGE = re.compile(
    r"\b(?:move|add|cancel|delete|remove|book|send|schedule|reschedule|push|postpone|undo|put|change|"
    r"drop|clear|mark)\b(?!\s+(?:today|so far)\b)",
    re.IGNORECASE,
)


def _asks_change(text: str) -> bool:
    words = re.findall(r"[a-zàèéìòù]+", text.casefold())
    if any(_CHANGE_STEM.match(w) and not _PARTICIPLE.search(w) for w in words):
        return True
    return bool(_EN_CHANGE.search(text))


def topic_of(words: str) -> Topic | None:
    """The ready answer's topic these words ask about, or None (a change is never one)."""
    text = " ".join(words.replace("\u2019", "'").split())
    if not text or _asks_change(text):
        return None
    return next((topic for topic, pattern in _TOPICS if pattern.search(text)), None)


@dataclass(frozen=True)
class Answer:
    topic: Topic
    text: str
    at: datetime
    version: tuple[str, ...]  # what Pisa's data looked like when it was made (`ReadyService.version`)


@dataclass(frozen=True)
class Hit:
    """A question a ready answer matched: `hit` (said at once) or not (`stale`: Pisa's data changed since, or
    the answer is older than MAX_AGE; the brain answers)."""

    topic: Topic
    text: str
    age_s: int
    hit: bool


def judge(answer: Answer, now: datetime, version: tuple[str, ...]) -> Hit:
    age = now - answer.at
    fresh = version == answer.version and age <= MAX_AGE
    return Hit(answer.topic, answer.text, max(0, int(age.total_seconds())), fresh)


LINES = {
    "it-IT": {
        "changes": "Oggi {items}.",
        "no_changes": "Oggi non ho cambiato niente.",
        "pool": "In cima alla lista: {items}.",
        "no_pool": "La lista è vuota.",
        "research": "Sto guardando: {items}.",
        "no_research": "Non sto guardando niente per te adesso.",
    },
    "en-US": {
        "changes": "Today {items}.",
        "no_changes": "I haven't changed anything today.",
        "pool": "At the top of your list: {items}.",
        "no_pool": "Your list is empty.",
        "research": "I'm looking into: {items}.",
        "no_research": "I'm not looking into anything for you right now.",
    },
}


def line(topic: Topic, items: list[str], lang: str) -> str:
    """A ready answer's words in Pisa's voice (first person), from Pisa's own data."""
    table = LINES.get(lang, LINES["en-US"])
    if not items:
        return table[f"no_{topic}"]
    return table[topic].format(items="; ".join(i.strip().rstrip(".") for i in items[:3]))
