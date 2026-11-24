"""The hard requirements their words carry, checked
against a source before a pick is shown. Pure rules, no I/O.

a05: Pisa picked an osteria closed at the asked time ("domani a pranzo"), and found the €35 budget only
when they asked for the menu. Here code reads their words for what a pick must meet (open at the time they
asked, not "now"; the budget; the party; "seduto"; "lavorare"), and judges each finalist from this turn's
evidence: a finalist that fails is excluded with the reason and its source ("apre alle 9:00", "chiuso il
mercoledì a pranzo"); one no source can confirm is marked "non verificato". Never the model's word for it.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pisa.domain.lexicon.numbers import comma_float
from pisa.domain.lexicon.weekdays import EN_WEEKDAYS, IT_WEEKDAYS_PLAIN, weekday_numbers
from pisa.domain.text import fold_apostrophe

# --- what their words ask
# ------------------------------------------------------------------------------------

_DAY_OFFSETS = {"oggi": 0, "today": 0, "domani": 1, "tomorrow": 1, "dopodomani": 2}
# Monday is 1 here (ISO), as the hours a source writes count it
_WEEKDAYS = weekday_numbers(IT_WEEKDAYS_PLAIN, EN_WEEKDAYS, start=1)
# A part of the day, and the time it means when they name no hour (the contract's "stasera" → 20:00,
# "sabato a pranzo" → Saturday 13:00)
_PARTS: tuple[tuple[str, int, int, str], ...] = (
    (r"stasera|tonight", 20, 0, "dinner"),
    (r"stamattina|this\s+morning", 9, 0, "morning"),
    (r"stanotte", 22, 0, "dinner"),
    (r"(?:a\s+|per\s+(?:il\s+)?|il\s+)?pranz\w*|(?:for\s+)?lunch", 13, 0, "lunch"),
    # "dove ceniamo sabato?", "cenare", "cenone" are dinner too
    (r"(?:a\s+|per\s+(?:la\s+)?|la\s+)?cen[aeio]\w*|(?:for\s+)?dinner|(?:for\s+)?supper", 20, 0, "dinner"),
    (r"aperitiv\w*|(?:for\s+)?drinks", 19, 0, "evening"),
    (r"(?:di\s+|in\s+|la\s+)?sera|(?:in\s+the\s+)?evening", 20, 0, "evening"),
    (r"(?:di\s+|nel\s+|il\s+)?pomeriggio|(?:in\s+the\s+)?afternoon", 16, 0, "afternoon"),
    (r"(?:di\s+|in\s+|la\s+)?mattin\w*|(?:in\s+the\s+)?morning", 9, 0, "morning"),
    (r"colazion\w*|breakfast|brunch", 9, 0, "morning"),
)
_NOW = re.compile(r"\b(?:adesso|ora|subito|al\s+volo|now|right\s+now)\b", re.IGNORECASE)
# a figure with a unit or decimals is never an hour ("rated at 4.5", "around 15 minutes", "around 20
# euros", "at 4 or more")
_CLOCK = re.compile(
    r"\b(?:alle|per\s+le|verso\s+le|dalle|at|by|around)\s+(\d{1,2})(?:[:.](\d{2}))?(?![\d.,]\d|\d)"
    r"(?!\s*-?\s*(?:km|m\b|metri|meters|metres|min\w*|stell\w*|stars?|euro|€|eur\b|%|persone|people|recension\w*|"
    r"reviews?|or\s+more|o\s+pi[uù]|\+))\s*(am|pm|a\.m\.|p\.m\.)?",
    re.IGNORECASE,
)
LATE_HOUR = 6  # an hour up to this with no am/pm, no morning and no night is the afternoon's
_NIGHT = re.compile(r"\b(?:notte|stanotte|night|di\s+mattina|del\s+mattino|in\s+the\s+morning)\b")
_MONEY = r"(?:€|euro\b|eur\b)"
_AMOUNT = r"(\d+(?:[.,]\d{1,2})?)"
# "sotto i 35 €", "meno di 35 euro", "entro 35€", "max 35 euro", "under €35", "less than 35 euros"
# their words are read accent-free ("non piu di"), and a cap a head may name no currency ("sotto i 35
# a testa")
_HEAD = r"(?:a\s+testa|a\s+persona|per\s+persona|each|a\s+head|per\s+head|per\s+person)\b"
_BUDGET = re.compile(
    r"\b(?:sotto(?:\s+(?:i|ai|gli|a))?|meno\s+di|entro(?:\s+(?:i|gli))?|max(?:imo)?|non\s+pi[uù]\s+di|"
    r"non\s+oltre(?:\s+(?:i|gli))?|fino\s+a|under|below|less\s+than|up\s+to|within|no\s+more\s+than|max)\s*"
    rf"(?:{_MONEY}\s*{_AMOUNT}|{_AMOUNT}\s*{_MONEY}|{_AMOUNT}\s*(?={_HEAD}))",
    re.IGNORECASE,
)
_NUMBERS = {
    "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8, "dieci": 10,
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10,
}  # fmt: skip
_PARTY = re.compile(
    r"\b(?:per|in|siamo\s+in|tavolo\s+per|for|party\s+of|table\s+for|we\s+are)\s+"
    r"(\d{1,2}|due|tre|quattro|cinque|sei|sette|otto|dieci|two|three|four|five|six|seven|eight|ten)"
    r"(?![\d.,:])(?!\s*(?:min\w*|km|m\b|metri|meters|euro|€|eur|ore|hours?|anni|years|stelle|stars))"
    r"(?:\s+(?:persone|people|of\s+us))?\b",
    re.IGNORECASE,
)
_SEATED = re.compile(
    r"\b(?:sedut[oaie]|seder(?:mi|ci|si|e)|a\s+sedere|con\s+(?:i\s+)?tavol\w*|sit\s+down|seated|a\s+seat|"
    r"somewhere\s+to\s+sit|with\s+(?:a\s+)?tables?)\b",
    re.IGNORECASE,
)
_WORK = re.compile(
    r"\b(?:lavorar\w*|per\s+lavoro|col\s+portatile|con\s+il\s+portatile|portatile|to\s+work|work\s+from|"
    r"laptop|wi-?fi|prese|sockets?|outlets?)\b",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    return fold_apostrophe(text or "")


@dataclass(frozen=True)
class Asked:
    """What a pick must meet, from their words: the time (`at`, and whether they named one: `named`), a
    budget a head (euros), the party, seated, a place to work."""

    at: datetime
    named: bool = False
    meal: str = ""  # lunch, dinner, morning, evening, afternoon: the part of the day they asked about
    budget: float | None = None
    party: int | None = None
    # a day named with no hour nor meal ("sabato"): open that day at all, never at the hour they spoke
    timed: bool = True
    seated: bool = False
    work: bool = False


def asked(words: str, now: datetime) -> Asked:
    """Their words' hard requirements. The asked time is the one they named ("domani a pranzo" → tomorrow
    13:00, "stasera" → 20:00, "sabato alle 21" → Saturday 21:00), else now (a breakfast asked at 8:15 is
    8:15)."""
    text = _plain(words)
    day = 0
    named_day = False
    for word, offset in _DAY_OFFSETS.items():
        if re.search(rf"\b{word}\b", text):
            day, named_day = offset, True
            break
    weekday = False
    if not named_day:
        for word, iso in _WEEKDAYS.items():
            if re.search(rf"\b{word}\b", text):
                day, named_day, weekday = (iso - now.isoweekday()) % 7, True, True
                break
    hour = minute = None
    meal = ""
    for pattern, h, m, kind in _PARTS:
        if re.search(rf"\b(?:{pattern})\b", text):
            hour, minute, meal = h, m, kind
            if (
                pattern.startswith("stasera")
                or pattern.startswith("stamattina")
                or pattern.startswith("stanotte")
            ):
                named_day = True
            break
    if (clock := _CLOCK.search(text)) is not None:
        h, m = int(clock.group(1)), int(clock.group(2) or 0)
        suffix = (clock.group(3) or "").replace(".", "")
        if suffix == "pm" and h < 12:
            h += 12
        elif not suffix and meal in ("dinner", "evening", "afternoon") and h < 12:
            h += 12  # "stasera alle 8" is 20:00
        elif not suffix and meal != "morning" and 1 <= h <= LATE_HOUR and not _NIGHT.search(text):
            # "lunch tomorrow at 1", "pranzo alle 2", "domani alle 3" are the afternoon's, never
            # 1:00 (every place closed then: all excluded, "chiuso a pranzo", a false fact on the sheet)
            h += 12
        if 0 <= h <= 23 and 0 <= m <= 59:
            hour, minute = h, m
    at = now
    named = False
    if (hour is not None or named_day) and not (_NOW.search(text) and not named_day and hour is None):
        base = now + timedelta(days=day)
        if hour is None:
            # a day named with no hour ("sabato") is checked for the whole day, from its midnight
            at = base.replace(hour=0, minute=0, second=0, microsecond=0) if day else now
        else:
            at = base.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
        named = True
        if weekday and at < now:  # "sabato a cena" said on Saturday at 22:30 is next Saturday
            at += timedelta(days=7)
        if not named_day and at < now:
            # "dove pranzo?" at 13:40: the lunch they ask about is now, never one already past
            at, named = now, False
    if meal == "morning" and not named_day and _CLOCK.search(text) is None:
        at, named = now, False  # a breakfast with no day is the one now
    budget = None
    if (found := _BUDGET.search(text)) is not None:
        raw = found.group(1) or found.group(2) or found.group(3)
        budget = comma_float(raw)
    party = None
    if (who := _PARTY.search(text)) is not None:
        raw = who.group(1)
        party = int(raw) if raw.isdigit() else _NUMBERS.get(raw)
        if party is not None and not 2 <= party <= 30:
            party = None
    return Asked(
        at=at, named=named, meal=meal, budget=budget, party=party, timed=hour is not None or not named,
        seated=bool(_SEATED.search(text)), work=bool(_WORK.search(text)),
    )  # fmt: skip


# --- the checks --------------------------------------------------------------------------------------------

GROUP_FROM = 5  # a party this big needs a place that takes groups (the map's "good for groups")
# a place closed at the asked time that opens within this many minutes is said "apre alle 9:00"; past it, at
# a meal, "chiuso il mercoledì a pranzo" (it opens at 18:30, for dinner)
OPENS_SOON_MIN = 90


@dataclass
class Outcome:
    """One requirement for one finalist: ok (True, False, None: no source said), why it fails or what it is
    (in their language), and the source ("map" or a url read this turn)."""

    need: str  # open, budget, party, seated, work
    ok: bool | None
    why: str = ""
    source: str = ""


@dataclass
class Verdict:
    """A finalist against every hard requirement: its outcomes; failed when one is False."""

    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def failed(self) -> Outcome | None:
        return next((o for o in self.outcomes if o.ok is False), None)

    @property
    def unknown(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.ok is None]

    def of(self, need: str) -> Outcome | None:
        return next((o for o in self.outcomes if o.need == need), None)


_WORDS = {
    "it-IT": {
        "until": "aperto fino alle {at}", "open": "aperto", "opens": "apre alle {at}",
        "closed_day": "chiuso {day}{meal}",
        "closed": "chiuso a quell'ora", "unknown": "orari non verificati",
        "lunch": " a pranzo", "dinner": " a cena", "morning": " la mattina", "evening": " la sera",
        "afternoon": " il pomeriggio",
        "over": "{price} a persona, sopra i {budget}", "within": "{price} a persona",
        "budget_unknown": "prezzo non verificato",
        "groups_no": "non adatto ai gruppi", "groups_yes": "adatto ai gruppi",
        "seated_no": "niente posti a sedere", "seated_yes": "posti a sedere",
        "work_no": "non adatto per lavorare", "work_yes": "wifi e prese",
        "not_checked": "non verificato: {what}",
        "what": {"open": "gli orari", "budget": "il prezzo", "party": "i gruppi",
                 "seated": "i posti a sedere",
                 "work": "wifi e prese"},
        "days": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
        "the_day": "il {day}",
    },
    "en-US": {
        "until": "open until {at}", "open": "open", "opens": "opens at {at}",
        "closed_day": "closed {day}{meal}",
        "closed": "closed at that time", "unknown": "hours not checked",
        "lunch": " at lunch", "dinner": " at dinner", "morning": " in the morning",
        "evening": " in the evening",
        "afternoon": " in the afternoon",
        "over": "{price} a head, over {budget}", "within": "{price} a head",
        "budget_unknown": "price not checked",
        "groups_no": "not for groups", "groups_yes": "takes groups",
        "seated_no": "no seating", "seated_yes": "seating",
        "work_no": "not for working", "work_yes": "wifi and sockets",
        "not_checked": "not checked: {what}",
        "what": {"open": "the hours", "budget": "the price", "party": "groups", "seated": "seating",
                 "work": "wifi and sockets"},
        "days": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        "the_day": "on {day}",
    },
}  # fmt: skip


def words(lang: str) -> dict[str, Any]:
    return _WORDS.get(lang, _WORDS["en-US"])


def euros(value: float, lang: str) -> str:
    """A price as they read it: "36 €", "7,99 €" (Italian); "€36", "€7.99" (English)."""
    whole = value == int(value)
    if lang == "it-IT":
        return f"{int(value)} €" if whole else f"{value:.2f}".replace(".", ",") + " €"
    return f"€{int(value)}" if whole else f"€{value:.2f}"


def _hm(at: datetime) -> str:
    """A time as they read it: "9:00", "18:30" (the comparison's "apre alle 9:00")."""
    return f"{at.hour}:{at:%M}"


def opening(state: dict[str, Any] | None, ask: Asked, lang: str, source: str = "map") -> Outcome:
    """Open at the asked time, from an `open_at` answer ({known, open, next_change}) for that time."""
    w = words(lang)
    if state is None or not state.get("known", True) or state.get("open") is None:
        return Outcome("open", None, w["unknown"], "")
    change = state.get("next_change")
    if isinstance(change, str):
        try:
            change = datetime.fromisoformat(change)
        except ValueError:
            change = None
    if not ask.timed:  # the whole day they named: open at some time of it, or closed that day
        local_change = change.astimezone(ask.at.tzinfo) if isinstance(change, datetime) else None
        if state["open"] or (local_change is not None and local_change.date() == ask.at.date()):
            when = _hm(local_change) if local_change is not None and not state["open"] else ""
            return Outcome("open", True, w["opens"].format(at=when) if when else w["open"], source)
        if local_change is None:
            return Outcome("open", False, w["closed"], source)
        day = w["days"][ask.at.isoweekday() - 1]
        return Outcome(
            "open", False, w["closed_day"].format(day=w["the_day"].format(day=day), meal=""), source
        )
    if state["open"]:
        at = _hm(change.astimezone(ask.at.tzinfo)) if isinstance(change, datetime) else ""
        return Outcome("open", True, w["until"].format(at=at) if at else w["open"], source)
    if not isinstance(change, datetime):
        return Outcome("open", False, w["closed"], source)
    local = change.astimezone(ask.at.tzinfo) if ask.at.tzinfo else change
    soon = local - ask.at <= timedelta(minutes=OPENS_SOON_MIN)
    if local.date() == ask.at.date() and (soon or ask.meal not in ("lunch", "dinner")):
        return Outcome("open", False, w["opens"].format(at=_hm(local)), source)
    day = w["days"][ask.at.isoweekday() - 1]
    the_day = w["the_day"].format(day=day)
    meal = w.get(ask.meal, "") if ask.meal else ""
    return Outcome("open", False, w["closed_day"].format(day=the_day, meal=meal), source)


# A figure a head: a menu's, the bill's, what a guide or a review says one spends
_PER_HEAD = re.compile(
    r"\b(?:menu|menù|degustazion\w*|conto|a\s+testa|a\s+persona|per\s+persona|spesa|circa|intorno|"
    r"per\s+person|a\s+head|each|per\s+head|tasting|set\s+menu|about|around)\b",
    re.IGNORECASE,
)
_EUROS = re.compile(rf"(?:€\s*{_AMOUNT}|{_AMOUNT}\s*{_MONEY})", re.IGNORECASE)
_CLAUSES = re.compile(r"[^.;:!?\n]+(?:[.;:!?]|\n|$)")


def _segments(text: str, name: str) -> list[str]:
    """The sentences of a text that name the place (all of it when `name` is "": a place's own page)."""
    if not name:
        return [s for s in re.split(r"(?<=[.!?])\s+|\n", text or "") if s.strip()]
    wanted = _plain(name)
    return [s for s in re.split(r"(?<=[.!?])\s+|\n", text or "") if wanted and wanted in _plain(s)]


# a figure for a group, a total, a day or a night is no figure a head ("abbiamo speso circa 90 euro in
# due", "parcheggio circa 40 euro al giorno")
_NOT_A_HEAD = re.compile(
    r"\b(?:in|per|for|siamo)\s+(?:\d+|due|tre|quattro|cinque|sei|two|three|four|five|six)\b(?!\s*(?:€|euro))"
    r"|\b(?:in\s+totale|totale|in\s+total|total|al\s+giorno|a\s+notte|per\s+notte|per\s+night|a\s+night|"
    r"a\s+day|per\s+day|parcheggi\w*|parking|the\s+two\s+of\s+us|in\s+coppia)\b",
    re.IGNORECASE,
)


def per_head(text: str) -> list[float]:
    """The figures a head in a text: an amount in euros in a clause that says it is a menu, the bill, what
    one spends ("circa 32 euro a testa", "menu degustazione 36 €"); a dish's price is none, nor a figure
    for a group, a total, a day or a night."""
    out: list[float] = []
    for clause in _CLAUSES.findall(text or ""):
        if not _PER_HEAD.search(clause) or _NOT_A_HEAD.search(clause):
            continue
        for m in _EUROS.finditer(clause):
            raw = m.group(1) or m.group(2)
            try:
                out.append(comma_float(raw))
            except ValueError:
                continue
    return out


def budget(ask: Asked, evidence: list[tuple[str, str, str]], lang: str) -> Outcome:
    """Within their budget a head, from this turn's evidence about the place: (text, the place's name to find
    in it or "" when the whole text is about it, source). The cheapest figure a head decides (a menu within
    the budget meets it); none found: not checked."""
    w = words(lang)
    assert ask.budget is not None
    found: list[tuple[float, str]] = []
    for text, name, source in evidence:
        for segment in _segments(text, name):
            found += [(v, source) for v in per_head(segment)]
    if not found:
        return Outcome("budget", None, w["budget_unknown"], "")
    value, source = min(found)
    if value > ask.budget:
        over = w["over"].format(price=euros(value, lang), budget=euros(ask.budget, lang))
        return Outcome("budget", False, over,
                       source)  # fmt: skip
    return Outcome("budget", True, w["within"].format(price=euros(value, lang)), source)


_SEATS_NO = re.compile(
    r"\b(?:niente\s+tavol\w*|senza\s+tavol\w*|poco\s+posto\s+a\s+sedere|nessun\s+posto\s+a\s+sedere|solo\s+(?:al\s+)?banco|"
    r"meglio\s+al\s+banco|in\s+piedi|no\s+seat\w*|no\s+tables|standing\s+only|takeaway\s+only|solo\s+asporto)\b",
    re.IGNORECASE,
)
_SEATS_YES = re.compile(
    r"\b(?:tavol\w*|posti\s+a\s+sedere|sedersi|seduti|divani\w*|cortile|sala|tables?|seating|sofas?)\b",
    re.IGNORECASE,
)
_WORK_NO = re.compile(
    r"\b(?:wi-?fi\s+(?:lento|cala|scarso|assente)|niente\s+(?:wi-?fi|prese)|senza\s+(?:wi-?fi|prese)|"
    r"no\s+(?:wi-?fi|laptops?|sockets?|outlets?)|vietat\w+\s+(?:il\s+)?portatil\w*|laptops?\s+(?:not\s+allowed|banned)|"
    r"slow\s+wi-?fi)\b",
    re.IGNORECASE,
)
_WORK_YES = re.compile(
    r"\b(?:wi-?fi|prese|sockets?|outlets?|portatil\w*|laptops?|coworking)\b", re.IGNORECASE
)


def _judged(
    need: str,
    evidence: list[tuple[str, str, str]],
    no: re.Pattern[str],
    yes: re.Pattern[str],
    lang: str,
) -> Outcome:
    w = words(lang)
    for text, name, source in evidence:
        for segment in _segments(text, name):
            if no.search(segment):
                return Outcome(need, False, w[f"{need}_no"], source)
    for text, name, source in evidence:
        for segment in _segments(text, name):
            if yes.search(segment) and not no.search(segment):
                return Outcome(need, True, w[f"{need}_yes"], source)
    return Outcome(need, None, w["not_checked"].format(what=w["what"][need]), "")


def seated(attributes: dict[str, Any], evidence: list[tuple[str, str, str]], lang: str) -> Outcome:
    """Somewhere to sit: the map's attributes first (dine-in, tables outside), then what the reviews and pages
    read this turn say of the place ("poco posto a sedere, meglio al banco")."""
    w = words(lang)
    judged = _judged("seated", evidence, _SEATS_NO, _SEATS_YES, lang)
    if judged.ok is False:
        return judged
    if attributes.get("dineIn") is False:
        return Outcome("seated", False, w["seated_no"], "map")
    if attributes.get("dineIn") is True or attributes.get("outdoorSeating") is True:
        return Outcome("seated", True, w["seated_yes"], "map")
    return judged


def work(evidence: list[tuple[str, str, str]], lang: str) -> Outcome:
    """A place to work: wifi and sockets, from the reviews and the pages read this turn."""
    return _judged("work", evidence, _WORK_NO, _WORK_YES, lang)


def party(ask: Asked, attributes: dict[str, Any], lang: str) -> Outcome | None:
    """A big party (GROUP_FROM and more) needs a place the map says takes groups; a smaller one asks
    nothing."""
    w = words(lang)
    if ask.party is None or ask.party < GROUP_FROM:
        return None
    if attributes.get("goodForGroups") is True:
        return Outcome("party", True, w["groups_yes"], "map")
    if attributes.get("goodForGroups") is False:
        return Outcome("party", False, w["groups_no"], "map")
    return Outcome("party", None, w["not_checked"].format(what=w["what"]["party"]), "")


def unchecked(verdict: Verdict, lang: str) -> str:
    """ "non verificato: gli orari, il prezzo": what no source confirmed, in their language ("" when all
    were)."""
    w = words(lang)
    what = [w["what"][o.need] for o in verdict.unknown]
    return w["not_checked"].format(what=", ".join(what)) if what else ""
