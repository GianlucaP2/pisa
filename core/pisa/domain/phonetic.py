"""How a span of what the user said is compared with a name Pisa knows (research/know/know/phonetic.py).
Pure rules, no I/O.

Two keys for one text, compared by edit distance (1 - distance / longer length, spaces removed so that "gas
fabriek" meets "Knoopfabriek"):

- `orth`: the spelling (lower case, no accents, "'s" and "&" dropped, double letters single);
- `dm`: Double Metaphone (Philips 2000), primary and alternate codes per word.

Report-3 measured Double Metaphone + spelling best on the synthetic set (the it/en/nl rule key of the
prototype added nothing), so CORE keeps those two.
"""

import re
from functools import lru_cache

from metaphone import doublemetaphone  # pyright: ignore[reportMissingTypeStubs]
from rapidfuzz.distance import Levenshtein

from pisa.domain.text import strip_marks

CONNECTORS = frozenset({"and", "e", "en", "&"})


def fold(text: str) -> str:
    """Lower case, no accents, "'s" joined ("bobby's" → "bobbys"), anything but letters, digits and & a
    space."""
    t = strip_marks(text.lower())
    t = re.sub(r"[\u2019']s\b", "s", t)
    t = re.sub(r"[\u2019'`]", " ", t)
    return re.sub(r"[^a-z0-9& ]+", " ", t)


def words(text: str) -> list[str]:
    return fold(text).split()


def _single(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)


def orth(text: str) -> str:
    return _single("".join(w for w in words(text) if w not in CONNECTORS))


def dm(text: str) -> tuple[str, str]:
    prim, alt = [], []
    for w in words(text):
        if w in CONNECTORS:
            continue
        p, a = doublemetaphone(w)  # pyright: ignore[reportUnknownVariableType]
        prim.append(str(p))
        alt.append(str(a or p))
    return "".join(prim), "".join(alt)


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1.0 - Levenshtein.distance(a, b) / max(len(a), len(b))


@lru_cache(maxsize=65536)
# FACT: linking keys; the model only confirms among the candidates.
def keys(text: str) -> tuple[str, tuple[str, str]]:
    return orth(text), dm(text)


def sound(a: tuple[str, str], b: tuple[str, str]) -> float:
    return max(ratio(a[0], b[0]), ratio(a[1], b[1]), ratio(a[0], b[1]), ratio(a[1], b[0]))


def similarity(span: str, name: str) -> tuple[float, str]:
    """How alike a span and a name are (half spelling, half Double Metaphone), and which of the two said more:
    "spelling" or "sound" (the `understood` event's `via`)."""
    so, sd = keys(span)
    no, nd = keys(name)
    o, d = ratio(so, no), sound(sd, nd)
    return 0.5 * o + 0.5 * d, ("spelling" if o >= d else "sound")
