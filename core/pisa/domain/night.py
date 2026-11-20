"""The night's frame: its caps, the search filter, and the Brier score. Pure rules.

- **Caps in code**: the night spends from its own ledger budget (`night`), at most €0.25 a night, and sends at
  most 40 web searches. N8 replays at most 30 of yesterday's turns; N9 writes 5 to 10 predictions.
- **The search filter**: before any query leaves Pisa, their names, their contacts' names, e-mail
  addresses, street addresses, amounts and dates are stripped. What is left is a typed abstraction ("freelance
  VAT refund Italy"); too little left and the query is not sent at all.
- **Brier**: the mean of (p - outcome)^2 over the predictions scored; 0 is perfect, 0.25 is a coin.
"""

import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal

from pisa.domain.lexicon.scrub import CURRENCY_WORDS, PATTERNS, QUERY_DAYS, QUERY_MONTHS
from pisa.domain.text import fold

NIGHT_CAP_EUR = Decimal("0.25")  # FACT: €0.14 typical, capped in code at €0.25
NIGHT_SEARCHES = 40  # FACT: ≤ 40 web searches a night
# FACT: 40 a night is ~1,200 a month, worst case ≈ $9.6 beyond Tavily's free tier; the
# night's own month on `place_calls` (budget `night`), never the user's places cap
NIGHT_PLACES_MONTH_EUR = Decimal("10.00")
NIGHT_HOURS = (2, 6)  # the night runs only between 02:00 and 06:00 in their zone
REPLAY_MAX = 30  # FACT: ~30 of yesterday's turns
PREDICT_MIN, PREDICT_MAX = 5, 10  # FACT: 5 to 10 predictions for tomorrow
MIN_WORDS = 2  # a query reduced to fewer words than this says nothing: it is not sent

# The steps, in order (counts only in the log).
# The nightly incubation and the ladder, after N9.
# The ignorance map, the score, the three gaits, the Topic Loop and the shelf, after
# the ideas (so the ideas take their ≤ 30 searches first and the curiosity its ≤ 10 of the 40).
# The morning sheet, Pisa's diary and the meter, from the rows above (code only, €0).
STEPS = (
    # Pisa's memory of their days first: yesterday segmented, scored, digested
    # (its own €0.02 of the €0.25), the recall budget renormalised
    "e1_segment",
    "e2_score",
    "e3_digest",
    "e4_downscale",
    # the day's finished conversations read once for their
    # tastes and habits (its own €0.05 of the €0.25), candidates under
    "m1_extract",
    "m2_portrait",  # the portrait in "Su di te", only when their claims changed (same €0.05)
    "m3_offer",  # "Conosciamoci" offered when Pisa said it didn't know what they like
    "n9_score",
    "n8_replay",
    "n9_predict",
    "i1_prepare",
    "i3_incubate",
    "i7_select",
    "i9_validate",
    "i10_choose",
    "c1_map",
    "c2_score",
    "c3_read",
    "c4_topic",
    "c5_shelf",
    "v6_cause",  # a confident miss's cause, from their own records (after the map opened its row)
    "n10_sources",  # the night reads the connectors' three readers (counts only, shadow)
    # the day that ended scored planned against actual (their band for the planner), then the day
    # this night opens rehearsed ("domani, provato": a row only for a real conflict)
    "p1_actuals",
    "p2_provato",
    # the CEO's desk, after the ideas (its dossiers follow the ideas made ready)
    # and taking only the searches left of the 40
    "d1_windows",
    "d2_dossiers",
    "d3_arbitrage",
    # after tonight's windows; bets from their public source, spars by the idea's
    # fate; the journal (d4) scores them next
    "v3_resolve",
    "v4_resolve",  # il consiglio's decision, by the idea's fate; scored by d4 next
    "d4_journal",
    "s1_sheet",  # always last, it reads what the steps before it wrote
)

_NUMBER_WORD = re.compile(
    r"^(?:\w*(?:uno|una|due|tre|quattro|cinque|sei|sette|otto|nove|dieci|undici|dodici|venti|trenta|quaranta|"
    r"cinquanta|sessanta|settanta|ottanta|novanta|cento|mille|mila|milione|milioni|miliardo|miliardi)\w*|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\w+teen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundred|hundreds|thousand|thousands|million|millions|billion|billions|half|"
    r"mezzo|e|and|a|k|mln|mld|mio|mrd|dos|tres|cuatro|cien|mil|deux|trois|cent|mille)$"
)
# Tokens: runs of letters or digits; what is between them is a separator
_TOKEN = re.compile(r"[^\W_]+")
_APOSTROPHE = ("'", "\u2019")


def _latin(token: str) -> bool:
    """Every letter is Latin (accents included): the allow-list's script."""
    return all(c.isascii() or "LATIN" in unicodedata.name(c, "") for c in token)


def name_parts(names: Iterable[str]) -> list[str]:
    """Every name's words of three letters or more ("Ravaglia-Rossi" → "Ravaglia", "Rossi"), folded, plus
    the short words of a name that is only short words ("Bo")."""
    parts: set[str] = set()
    for name in names:
        words = [fold(w) for w in _TOKEN.findall(str(name)) if not any(ch.isdigit() for ch in w)]
        long = [w for w in words if len(w) >= 3]
        parts.update(long or [w for w in words if len(w) >= 2])
    return sorted(parts)


def scrub(query: str, names: Iterable[str]) -> tuple[str, int]:
    """RAIL: what of the query may leave Pisa, and how many things were taken out. An allow-list:
    only words of Latin letters survive, never a token with a digit (a number, a year, a date, an amount, a
    plate, a flight, a codice fiscale, an IBAN, a VAT number, a phone), never a month, a day, a currency or
    the number words next to one, never a word of their names, their contacts' names, their places, their city
    or their work (`names`: matched folded, word by word, a possessive "'s" and an elided "l'" with it), never
    an e-mail, a link or a street address."""
    text = " ".join(query.split())
    stripped = 0
    for pattern in PATTERNS:
        text, n = pattern.subn(" ", text)
        stripped += n
    personal = set(name_parts(names))
    found = list(_TOKEN.finditer(text))
    tokens = [m.group(0) for m in found]
    after = [text[m.end() : found[i + 1].start()] if i + 1 < len(found) else "" for i, m in enumerate(found)]
    folded = [fold(t) for t in tokens]
    drop = [False] * len(tokens)
    for i, (t, f) in enumerate(zip(tokens, folded, strict=True)):
        if any(c.isdigit() for c in t) or not _latin(t):
            drop[i] = True
        elif f in personal or f in QUERY_MONTHS or f in QUERY_DAYS or f in CURRENCY_WORDS:
            drop[i] = True
    # a currency sign or word takes the number words next to it ("duemila euro", "€ two thousand")
    before = [text[: found[0].start()] if found else "", *after[:-1]]

    def numbers(j: int, step: int) -> None:
        while 0 <= j < len(tokens) and _NUMBER_WORD.match(folded[j]):
            drop[j] = True
            j += step

    for i in range(len(tokens)):
        if folded[i] in CURRENCY_WORDS:
            numbers(i - 1, -1)
            numbers(i + 1, 1)
        if any(c in before[i] for c in "€$£"):
            numbers(i, 1)
        if any(c in after[i] for c in "€$£"):
            numbers(i, -1)
    for i in range(len(tokens)):  # "Ornella's" → the "s"; "l'Ornella", "dall'Ornella" → the "l", "dall"
        if drop[i]:
            continue
        if i > 0 and drop[i - 1] and after[i - 1] in _APOSTROPHE and folded[i] == "s":
            drop[i] = True
        elif i + 1 < len(tokens) and drop[i + 1] and after[i] in _APOSTROPHE and len(tokens[i]) <= 5:
            drop[i] = True
    stripped += sum(drop)
    return " ".join(t for t, d in zip(tokens, drop, strict=True) if not d), stripped


def sendable(clean: str) -> bool:
    """A query reduced to almost nothing is not sent: it would say nothing but that they searched."""
    return len(re.findall(r"[^\W\d_]{2,}", clean)) >= MIN_WORDS


def brier_term(p: float, outcome: bool) -> float:
    """One prediction's part of the Brier score: (p - outcome)^2; 0 is right and sure, 1 is wrong and sure."""
    return (p - (1.0 if outcome else 0.0)) ** 2


def brier(pairs: Iterable[tuple[float, bool]]) -> float | None:
    """The mean squared error of the probabilities against what happened; None with nothing scored."""
    scored = [brier_term(p, o) for p, o in pairs]
    return round(sum(scored) / len(scored), 4) if scored else None


def rate(hits: int, n: int) -> float:
    """Their own base rate, smoothed (Laplace): a first night with no history says 0.5, never 0 or 1."""
    return round((hits + 1) / (n + 2), 3)
