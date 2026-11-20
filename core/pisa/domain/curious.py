"""The curiosity engine's rules. Pure.

- **The ignorance map**: rows from their own words ("Pisa, tienimelo aperto: …"), from a confident miss of
  N9's predictions ("perché mi sono sbagliato?"), from a topic they name, and from the horizon scan.
- **The interestingness score**: `value x progress x tractability x novelty x (1 - noisy)`. Code computes
  value, progress, tractability and the noisy flag; a cheap model scores novelty and the cognitive part. Value
  is weighted by their *liked* acts on past finds, never by opens (wanting is not liking, C §C5).
- **Three gaits**: hunter ~70%, busybody ~20%, dancer ~10% of the night's reads.
- **The shelf**: up to 20 questions held for days, each looked at again on a schedule set by its score.
"""

import re
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from pisa.domain.night import brier_term
from pisa.domain.text import fold_words

NIGHT_SEARCHES = 8  # the curiosity's share of the night's 40: ideas 26, curiosity 8, desk 6
NIGHT_CAP_EUR = Decimal("0.05")  # the curiosity's share of the night's €0.25 (it typically spends ≈ €0.02)
EST_CALL_EUR = Decimal("0.003")  # one call's cost assumed before it runs, until the night's dearest is known
SHELF_MAX = 20  # FACT: "up to about 20 at a time"
GAITS = (("hunter", 0.7), ("busybody", 0.2), ("dancer", 0.1))  # FACT: 70/20/10
# a miss "with confidence": p ≥ 0.75 that did not happen, or ≤ 0.25 that did
CONFIDENT = 0.75
NOISY_READS = 4  # FACT: a source that changes without learning progress after N reads
DEBATE_READS = 3  # a debated question with no convergence after 3 reads is parked as open for the experts
FORAGE_STOP = 2  # FACT: two reads that add nothing end the patch
LATERAL_DAYS = 90  # FACT: a lateral check per new domain, cached for 90 days
HORIZON_WEEK = 2  # FACT (ProactiveBench): at most two a week, each a question
HORIZON_DAYS = 60  # the dated items the horizon scan looks at: the next 60 days
TOPIC_QUESTIONS = 5  # FACT: five pre-questions from five perspectives (STORM)
TEST_MIN, TEST_MAX = 10, 20  # FACT: 10-20 questions written from the sources
REFRESH_DAYS = 30  # FACT: flat progress and liking → a monthly refresh
ANSWER_MAX = 5  # the shelf's rows read a night at most (the rest wait for their next look)
OPEN_FOR_EXPERTS = "è un tema ancora aperto anche per gli esperti"  # FACT: the parked line

# Tractability (C §D2): can a source type Pisa may read at night answer it? Asking them is not a night's read
# (no quizzing, nothing reaches them); their own data are not on the web, and never searched for.
TRACTABLE = {"web": 1.0, "public_source": 1.0, "his_data": 0.0, "ask_him": 0.0, "none": 0.0}
SEARCHED = frozenset({"web", "public_source"})  # RAIL: only these are ever searched

# --- their words ---

_LEAD = r"^\s*(?:(?:hey|ehi|ok)\s+)?pisa\s*[,:]?\s*"
_DASH = r"\s*[:\-\u2013\u2014]\s*"
_SHELF = re.compile(
    _LEAD + r"(?:tienimel[oa]\s+apert[oa]|tienimi\s+apert[oa](?:\s+questa)?|keep\s+(?:this|it)\s+open)"
    + _DASH + r"(?P<text>.+)$",
    re.I | re.S,
)  # fmt: skip
_TOPIC = re.compile(
    _LEAD + r"(?:voglio\s+capire(?:\s+meglio)?|vorrei\s+capire(?:\s+meglio)?|i\s+want\s+to\s+understand)"
    r"\s*[:\-\u2013\u2014]?\s*(?P<text>.+)$",
    re.I | re.S,
)  # fmt: skip
WORDS_MIN = 2


def his_question(text: str) -> str | None:
    """Their "Pisa, tienimelo aperto: …" (or "Pisa, keep this open: …"): a question they hand Pisa to hold."""
    found = _SHELF.match(text or "")
    said = " ".join(found.group("text").split()) if found else ""
    return said if len(said.split()) >= WORDS_MIN else None


def topic(text: str) -> str | None:
    """Their "Pisa, voglio capire X" (or "I want to understand X"): the topic, in their words."""
    found = _TOPIC.match(text or "")
    said = " ".join(found.group("text").split()).rstrip(" .!?") if found else ""
    return said if len(said.split()) >= 1 and len(said) >= 3 else None


# --- the miss becomes a question ---

_MISS = {
    "talks": ("che mi avresti scritto", "che non mi avresti scritto"),
    "early": ("che mi avresti scritto prima delle 9", "che non mi avresti scritto prima delle 9"),
    "evening": ("che mi avresti scritto dopo le 21", "che non mi avresti scritto dopo le 21"),
    "busy": ("che sarebbe stata una giornata piena", "che sarebbe stata una giornata tranquilla"),
    "task_done": ("che avresti chiuso un'attività", "che non avresti chiuso nessuna attività"),
    "event_kept": ("che l'impegno in agenda sarebbe rimasto", "che l'impegno in agenda sarebbe saltato"),
}


def confident_miss(p: float, outcome: bool) -> bool:
    """A prediction that missed with confidence: the purest known unknown. Read as the
    Brier score marks it: its own term is at least `CONFIDENT²` (p ≥ 0.75 that did not happen,
    or ≤ 0.25 that did); a hit, a lucky guess near 0.5 or a timid miss scores less and opens nothing."""
    return brier_term(round(p, 4), outcome) >= CONFIDENT**2 - 1e-9


def miss_question(kind: str, p: float, outcome: bool, day: date) -> str | None:
    """ "Perché mi sono sbagliato?", in Pisa's words, from a confident miss; else None."""
    if kind not in _MISS or not confident_miss(p, outcome):
        return None
    said = _MISS[kind][0] if p >= CONFIDENT else _MISS[kind][1]
    sure = round((p if p >= CONFIDENT else 1 - p) * 100)
    when = day.strftime("%d/%m")
    return f"Perché mi sono sbagliato? Ero sicuro al {sure}% {said} il {when}, e non è andata così."


# --- the interestingness score (C §D2) ---


def like_weight(liked: int, shown: int) -> float:
    """Their liked acts on past finds of this source: 1.0 with none; up to 1.5 if they like them, down to 0.5
    if they never do. Opens never count (C §C5)."""
    if shown <= 0:
        return 1.0
    rate = (liked + 1) / (shown + 2)  # Laplace, as N9's base rates
    return round(min(max(rate * 2, 0.5), 1.5), 3)


def instrumental(deadline: date | None, night: date) -> float:
    """It changes a dated decision: the nearer the date, the more (0 with no date or a past one)."""
    if deadline is None or deadline < night:
        return 0.0
    days = (deadline - night).days
    return 0.6 if days <= 7 else 0.4 if days <= 30 else 0.2


def value(instrumental: float, cognitive: float, explicit: bool, weight: float = 1.0) -> float:
    """value_to_him (0-1): instrumental + cognitive + their explicit interest, weighted by their liked acts. A
    hedonic-negative row is never lowered for being unpleasant (it takes a neutral title instead)."""
    raw = instrumental + 0.4 * max(min(cognitive, 1.0), 0.0) + (0.4 if explicit else 0.0)
    return round(min(max(raw, 0.05) * weight, 1.0), 4)


def progress(reads: int, adds: int) -> float:
    """learning_progress for a row: its reads that added something, smoothed (a new row: 0.5)."""
    return round((adds + 1) / (reads + 2), 4)


def topic_progress(scores: Sequence[float]) -> float:
    """learning_progress for a topic's row: the change in Pisa's self-test score (C §B6), mapped to 0.1-1; no
    test yet: 0.5 (or the inherited value of a similar topic, the caller's)."""
    if len(scores) < 2:
        return 0.5
    return round(min(max(0.5 + (scores[-1] - scores[-2]), 0.1), 1.0), 4)


def noisy(reads: int, adds: int) -> bool:
    """RAIL (the noisy-TV filter, C §D2): a question whose reads keep changing and teach nothing."""
    return reads >= NOISY_READS and adds == 0


def open_for_experts(debated: bool, reads: int, converged: bool) -> bool:
    """A debated question with no convergence after DEBATE_READS reads is parked, not chased."""
    return debated and reads >= DEBATE_READS and not converged


def interest(v: float, p: float, tractability: float, novelty: float, is_noisy: bool) -> float:
    """interest = value x progress x tractability x novelty x (1 - noisy)."""
    if is_noisy:
        return 0.0
    return round(v * p * tractability * max(min(novelty, 1.0), 0.0), 5)


def next_look(night: date, score: float) -> date:
    """The shelf's schedule, set by the score: tomorrow, in three days, or in a week."""
    return night + timedelta(days=1 if score >= 0.1 else 3 if score >= 0.02 else 7)


def gaits(n: int) -> list[str]:
    """The night's n reads in the three gaits, hunter first: 10 → 7 hunter, 2 busybody, 1 dancer."""
    if n <= 0:
        return []
    counts = {g: int(n * share) for g, share in GAITS}
    if n >= 3:  # each gait at least once when there is room
        for g in ("dancer", "busybody"):
            counts[g] = max(counts[g], 1)
    counts["hunter"] = n - counts["busybody"] - counts["dancer"]
    return ["hunter"] * counts["hunter"] + ["busybody"] * counts["busybody"] + ["dancer"] * counts["dancer"]


# --- checks by code ---


def fold(text: str) -> str:
    return fold_words(text, r"[^\W_]+")


def quoted(quote: str, page: str, claim: str) -> bool:
    """RAIL ("every claim Pisa shows is checkable by code"): the claim's quote is on the fetched page, word
    for word (case, accents and punctuation folded), says something (four words at least), and supports the
    claim: every number of the claim and most of its key words are in the quote."""
    q = fold(quote or "")
    if len(q.split()) < 4 or q not in fold(page or ""):
        return False
    keys, have = _keys(claim), _keys(quote)
    digits = {k for k in keys if any(c.isdigit() for c in k)}
    return bool(keys) and digits <= have and len(keys & have) / len(keys) >= 0.6


def _keys(text: str) -> set[str]:
    return {w for w in fold(text).split() if len(w) >= 4 or any(c.isdigit() for c in w)}


def correct(expected: str, got: str) -> bool:
    """Pisa's self-test answer, scored by code: most of the expected answer's key words (and every number of
    it) are in Pisa's answer from the map alone."""
    keys, have = _keys(expected), _keys(got)
    if not keys:
        return False
    digits = {k for k in keys if any(c.isdigit() for c in k)}
    return digits <= have and len(keys & have) / len(keys) >= 0.6


def score(results: Sequence[bool]) -> float:
    return round(sum(results) / len(results), 4) if results else 0.0


def flat(scores: Sequence[float], liked: bool | None) -> bool:
    """Boredom (C §C7): the last two self-tests show no progress and they haven't liked it: a monthly
    refresh."""
    return len(scores) >= 3 and scores[-1] <= scores[-2] <= scores[-3] and not liked


def already_there(candidate: str, his_items: Sequence[str]) -> bool:
    """The horizon scan's code check: a candidate whose key words are mostly in one of their items is already
    there (a task, an event, a note of theirs), so it is not a gap."""
    keys = _keys(candidate)
    if not keys:
        return True
    return any(len(keys & _keys(item)) / len(keys) >= 0.5 for item in his_items)


def host(url: str) -> str | None:
    found = re.match(r"https://([^\s/@:]+)", url or "")
    return found.group(1).casefold() if found else None
