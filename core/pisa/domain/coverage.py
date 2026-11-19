"""The user's stated changes are made in one turn. A model can answer "six changes in one breath" with
three of them and say nothing of the rest, each said with its own time and none of them written.

Pure rules: the clauses of the user's words that carry a time of their own ("alle 14 ho la corsa", "palestra
dalle 15 alle 16") and that no write of the turn covers (no write holds that time). `finish` hands them back
once, so the model makes them or says why not; it never writes anything itself.
"""

import re
from collections.abc import Iterable
from typing import Any

from pisa.domain import actions as rules

# The writes that put a time of theirs in the day (an event, a task's start, a reminder, a booking, a message
# that says the time): a clause whose time one of them holds is covered.
_TIME = re.compile(r"T([01]\d|2[0-3]):([0-5]\d)|\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_QUESTION = re.compile(r"\?\s*$")


def _times(value: Any) -> set[tuple[int, int]]:
    """Every time a write's arguments hold: ISO ("…:00"), "16:00", or said ("alle 9", "dalle quattro")."""
    found: set[tuple[int, int]] = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= _times(v)
    elif isinstance(value, list | tuple):
        for v in value:
            found |= _times(v)
    elif isinstance(value, str):
        for m in _TIME.finditer(value):
            hour, minute = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            found.add((int(hour), int(minute)))
        for slot in rules.said_slots(rules.with_minutes(value), events=True):
            found |= set(slot.starts) | ({slot.end} if slot.end else set())
    return found


def _same(said: tuple[int, int], held: set[tuple[int, int]]) -> bool:
    hour, minute = said
    early = 1 <= hour <= rules.EARLY_HOUR
    return any(m == minute and (h == hour or (early and h == hour + 12)) for h, m in held)


def unsaid(owner_words: str, writes: Iterable[tuple[str, dict[str, Any]]]) -> list[str]:
    """The user's timed clauses no write of the turn covers, in their order (their words, as code split them).
    Nothing for a question, a negated or taken-back clause, someone else's plans, or a bare deadline."""
    if not owner_words.strip() or _QUESTION.search(owner_words):
        return []
    held: set[tuple[int, int]] = set()
    for _, args in writes:
        held |= _times(args)
    clauses = rules.said_slots(rules.with_minutes(owner_words), events=True)
    said = _as_said(owner_words, clauses)
    out = []
    for n, slot in enumerate(clauses):
        if not slot.starts:
            continue  # "entro le 19" (a deadline) or no time at all
        words = set(re.findall(r"[a-z0-9']+", slot.text))
        if words & (rules.CLAUSE_NO | rules.THEIRS) or rules.others(slot) or rules.voids(slot):
            continue
        if any(rules.voids(c) for c in clauses[n + 1 :]):
            continue  # "…, anzi no"
        if not all(_same(start, held) for start in slot.starts):
            out.append(said[n])
    return out


def _as_said(owner_words: str, clauses: list[rules.Slot]) -> list[str]:
    """Each clause's text as they said it (writing the times out, "alle 9" → "alle 09:00", may change nothing
    else), so a quote of it stands in their words; the written-out text when the split differs."""
    raw = rules.said_slots(owner_words, events=True)
    return [s.text for s in raw] if len(raw) == len(clauses) else [s.text for s in clauses]


def clause_at(owner_words: str, start: str) -> str | None:
    """The clause of their words that says this start ("…:00" → "domani alle 9 chiamo il commercialista"),
    for the event a timed task becomes (07); None when no clause of their says that time."""
    at = _times(start)
    if len(at) != 1:
        return None
    clauses = rules.said_slots(rules.with_minutes(owner_words), events=True)
    for slot, text in zip(clauses, _as_said(owner_words, clauses), strict=True):
        if slot.starts and _same(slot.starts[0], at):
            return text
    return None


# "sono in ritardo di un'ora, sistemami il pomeriggio" leaves no choice: their words say how late and to fix
# it; a model may put three options on the phone (keep all, skip the call, skip the gym)
_NUMBERS = {"un": 1, "una": 1, "uno": 1, "due": 2, "tre": 3, "dieci": 10, "quindici": 15, "venti": 20,
            "trenta": 30, "quaranta": 40, "an": 1, "a": 1, "one": 1, "two": 2, "ten": 10, "fifteen": 15,
            "twenty": 20, "thirty": 30}  # fmt: skip
#  2: in English only "I'm 10 minutes late" / "running an hour behind" (never "I'm 10 minutes away")
_AMOUNT = (
    r"(?:(?P<half>mezz'?\s?ora|half\s+an\s+hour)|(?P<quarter>un\s+quarto\s+d'?\s?ora|a\s+quarter\s+of\s+an\s+hour)|"
    r"(?P<n>\d{1,3}|un|una|uno|due|tre|dieci|quindici|venti|trenta|quaranta|an|a|one|two|ten|fifteen|twenty|"
    r"thirty)\s*'?\s*(?P<unit>or[ae]|minut[oi]|min|hours?|minutes?|mins?))"
)
_DELAY = re.compile(
    rf"\b(?:(?:in\s+)?ritardo\s+di|indietro\s+di)\s+{_AMOUNT}|"
    rf"\b(?:i'?m|i\s+am|we'?re|running)\s+{_AMOUNT.replace('?P<', '?P<e_')}\s+(?:late|behind)\b",
    re.IGNORECASE,
)
_FIX = re.compile(
    r"\b(?:sistem\w*|riorganizz\w*|aggiust\w*|spost\w*|slitt\w*|rimedia\w*|ripianific\w*|fix|sort|reorgani[sz]e|"
    r"rearrange|shift|move|push)\b",
    re.IGNORECASE,
)
_HIS_OWN = re.compile(
    r"^\W*(?:(?:ehi\s+|pisa,?\s+)?(?:sono|siamo)\s+(?:in\s+ritardo|indietro)|(?:hey\s+|pisa,?\s+)?(?:i'?m|i\s+am|we'?re|"
    r"i'?m\s+running|we'?re\s+running)\b)",
    re.IGNORECASE,
)
_WHOLE_DAY = re.compile(
    r"\b(?:(?:il|la|lo|tutto\s+il|tutta\s+la)\s+(?:pomeriggio|mattin[ao]|giornata|serata|sera|resto|programma|agenda)|"
    r"tutto|(?:the|my)\s+(?:afternoon|morning|day|evening|rest|schedule)|everything)\b",
    re.IGNORECASE,
)
_LEAVES_IT = re.compile(r"\?|\b(?:o|oppure|or|vedi\s+tu|decidi\s+tu|scegli\s+tu|dimmi\s+tu|up\s+to\s+you)\b",
                        re.IGNORECASE)  # fmt: skip


def delay_min(owner_words: str) -> int | None:
    """How late they say they are ("in ritardo di un'ora" → 60, "di 20 minuti" → 20), or None."""
    found = _DELAY.search(owner_words)
    if found is None:
        return None
    got = {k.removeprefix("e_"): v for k, v in found.groupdict().items() if v}
    if got.get("half"):
        return 30
    if got.get("quarter"):
        return 15
    raw = got["n"].casefold()
    n = int(raw) if raw.isdigit() else _NUMBERS.get(raw, 0)
    unit = got["unit"].casefold()
    minutes = n * 60 if unit.startswith(("or", "hour")) else n
    return minutes if 0 < minutes <= 6 * 60 else None


def no_choice(owner_words: str) -> str | None:
    """What their words already decide, when they leave no real choice to put to them: a delay they
    state with the fix they ask for. None when they leave one open (their question, "o", "vedi tu")."""
    if _LEAVES_IT.search(owner_words):
        return None
    # only their own delay ("sono in ritardo", "I'm late"), never Anna's or a flight's,
    # and only when they ask for the rest of their day, not one event they name ("sposta la call"): then Pisa
    # moves only what they named
    if not _HIS_OWN.search(owner_words) or not _WHOLE_DAY.search(owner_words):
        return None
    late = delay_min(owner_words)
    if late is not None and _FIX.search(owner_words):
        return (
            f"their words already say what to do: they are {late} minutes late and ask you to fix it. Move "
            "each "
            f"event still ahead today {late} minutes later (move_event each, owner_said their words), all in "
            "one round; the ones that wait for a yes go in ONE question."
        )
    return None
