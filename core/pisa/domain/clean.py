"""The clean turn. A turn is clean when Pisa did what the user asked (no tool said it wasn't
done), claimed nothing without a record (no claim rewritten into what really happened), and the user didn't
have to say it again: their next words neither correct Pisa ("no aspetta, intendevo sabato") nor repeat their
own.

Ids and counts only: the words are read here, never stored or logged.
"""

import re
from collections.abc import Iterable

from rapidfuzz import fuzz

from pisa.domain.text import fold_apostrophe

# Their next words correct what Pisa just did or said (the same words ground the replacement)
_CORRECTION = re.compile(
    r"^(?:no|nope|aspetta|wait)\b(?:\W+(?:no|aspetta|wait))*\W+"
    r"(?:intendevo|volevo dire|ho detto|non ho detto|dicevo|i meant|i said|i didn't say|not that|"
    r"non quell[oaie])\b"
    r"|^(?:no\W+)?(?:aspetta\W+)?(?:intendevo|volevo dire|i meant|no wait|non intendevo)\b"
    r"|\b(?:hai sbagliato|sbagliato|non hai capito|non era quello|you got it wrong|that's wrong|"
    r"that is wrong|that's not what i|non e quello che)\b"
)
REPEAT_RATIO = 88  # rapidfuzz ratio of the folded words: "sposta la cena alle 21" said twice
REPEAT_WORDS = 3  # "sì", "ok", "grazie" said twice are no repeat


def _plain(text: str) -> str:
    kept = fold_apostrophe(text)
    return " ".join(re.sub(r"[^\w':]+", " ", kept).split())


def corrects(words: str) -> bool:
    """The user's words correct Pisa's last turn ("no aspetta, intendevo sabato", "I meant Saturday")."""
    return bool(_CORRECTION.search(_plain(words)))


TOKEN_RATIO = 85  # "spostala"/"spostalo", a transcription's slip: the same word; "Marco"/"Mario" isn't
_FILLER = frozenset("dai ok okay allora please per favore grazie thanks pure su".split())


def _content(text: str) -> set[str]:
    return {w for w in text.split() if len(w) >= 3 and w not in _FILLER}


def _within(a: set[str], b: set[str]) -> bool:
    """Every content word of `a` is one of `b`'s, allowing a transcription's slip."""
    return all(any(w == v or fuzz.ratio(w, v) >= TOKEN_RATIO for v in b) for w in a)


def repeats(before: str, now: str) -> bool:
    """The user says their last words again, nearly the same (Pisa didn't do it, or didn't hear it).
    The same content words (one set within the other), never a different name, day or thing ("latte" →
    "pane", "Marco" → "Mario", "domani" → "dopodomani")."""
    a, b = _plain(before), _plain(now)
    if len(a.split()) < REPEAT_WORDS or len(b.split()) < REPEAT_WORDS or fuzz.ratio(a, b) < REPEAT_RATIO:
        return False
    first, second = _content(a), _content(b)
    return bool(first and second) and (_within(first, second) or _within(second, first))


def redone(before: str, now: str) -> bool:
    """Their next final makes the turn before not clean: a correction or a near-repeat."""
    return corrects(now) or repeats(before, now)


def clean(*, failed: int, unbacked: int, redone: bool) -> bool:
    return failed == 0 and unbacked == 0 and not redone


def rate(turns: Iterable[tuple[int, int, bool]]) -> tuple[int, int]:
    """(clean turns, all turns) of (failed, unbacked, redone) rows."""
    rows = list(turns)
    return sum(1 for f, u, r in rows if clean(failed=f, unbacked=u, redone=r)), len(rows)
