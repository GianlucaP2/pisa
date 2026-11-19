"""The life-admin vault: documents with an expiry and their reminders. Pure rules.

- **Never a full document number**: only the last 4 letters or digits are kept (`last4`); notes with a run of
  5 or more digits are refused (a number would hide there).
- **Reminders** by kind, in days before the expiry (defaults here, to be
  tuned). When one is due, Pisa puts a renewal task in the pool (the deadline the expiry) and marks it done.
- **Abroad**: when their calendar puts them outside the document's country during the renewal window, Pisa
  says so ("la CIE scade a marzo e a marzo sei a New York: prenoto al consolato?").
- **A document's country** is any ISO 3166-1 alpha-2 code; when they don't say it, the issuer's for a kind
  only one country issues (the CIE, a green card), else the user's own (PISA_COUNTRY), else none.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, get_args

from pisa.domain import countries
from pisa.domain.lexicon.months import EN_MONTHS, IT_MONTHS, MONTH_NAMES, month_numbers

Kind = Literal[
    "cie",
    "passport",
    "driving_licence",
    "health_card",
    "car_revision",
    "car_tax",
    "car_insurance",
    "visa",
    "green_card",
    "us_licence",
    "other",
]
KINDS: tuple[Kind, ...] = get_args(Kind)
NOTES_MAX = 300
DOCUMENTS_MAX = 100

# Days before the expiry, soonest last. The first is the research's lead time:
# the CIE's window opens at 180 days and Rome's and New
# York's slots are scarce (210), a passport 9 months, the patente 120 (a medical visit in Italy), a visa or
# ESTA 90, the green card's I-90 window 6 months, the NY licence's renewal window 12 months, the revisione 60,
# the RC auto 30 then 7; the later ones raise the task.
DEFAULT_REMINDERS: dict[str, tuple[int, ...]] = {
    "cie": (210, 60, 14),
    "passport": (270, 90, 30),
    "driving_licence": (120, 30),
    "us_licence": (365, 60),
    "visa": (90, 30),
    "green_card": (180, 60),
    "car_revision": (60, 14),
    "car_tax": (30, 7),
    "car_insurance": (30, 7),
}
OTHER_REMINDERS = (90, 30)
# AIRE changes where they renew (the consulate or a Comune): Pisa asks it once, at the first of these; never
# assumed.
AIRE_KINDS = frozenset({"cie", "passport", "driving_licence", "health_card"})
_ITALIAN_KINDS = frozenset({"cie"})
_US_KINDS = frozenset({"green_card", "us_licence"})

LABELS: dict[str, tuple[str, str]] = {  # kind: (Italian, English)
    "cie": ("la carta d'identità", "the ID card"),
    "passport": ("il passaporto", "the passport"),
    "driving_licence": ("la patente", "the driving licence"),
    "health_card": ("la tessera sanitaria", "the health card"),
    "car_revision": ("la revisione dell'auto", "the car's inspection"),
    "car_tax": ("il bollo dell'auto", "the car tax"),
    "car_insurance": ("l'assicurazione dell'auto", "the car insurance"),
    "visa": ("il visto", "the visa"),
    "green_card": ("la green card", "the green card"),
    "us_licence": ("la patente americana", "the US licence"),
    "other": ("il documento", "the document"),
}
_TASK_VERB = {
    "car_tax": ("Pagare", "Pay"),
    "car_insurance": ("Rinnovare", "Renew"),
    "car_revision": ("Prenotare", "Book"),
}
# What their words name: a document (or "scadenza", "scade", "rinnovo") when they ask Pisa to keep one.
_DOC_WORDS = re.compile(
    r"\b(?:carta\s+d'?\s*identit[aà]|c\.?i\.?e\.?|passaport\w*|patent\w*|tessera\s+sanitaria|revision\w*|"
    r"bollo|assicurazion\w*|rc\s*auto|visto|green\s*card|id\s+card|identity\s+card|passport"
    r"|driv\w*\s+licen[cs]e|"
    r"licen[cs]e|health\s+card|insurance|car\s+tax|inspection|visa|documento|document)\b",
    re.IGNORECASE,
)
_EXPIRY_WORDS = re.compile(
    r"\b(?:scad\w*|rinnov\w*|valid\w*|expir\w*|renew\w*|aggiungi|segna|salva|tieni|ricorda\w*|add|keep|save|"
    r"remember)\b",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{5,}")
_SEPARATORS = re.compile(r"[\s./\-_,:]")
# a number written with separators ("YA 123 4567", "AB-1234-567", "1234 5678 9012") or a code
# (a codice fiscale): 5+ digits once separators are gone, or 6+ letters and digits with 3+ digits.

_LAST4 = re.compile(r"^[A-Za-z0-9]{4}$")
_MONTHS = month_numbers(IT_MONTHS, EN_MONTHS)

# Where they'll be: a trip's words in their calendar, by country (the other country is "abroad" for a
# document).
PLACES: dict[str, tuple[str, ...]] = {
    "US": (
        "new york",
        "nyc",
        "manhattan",
        "brooklyn",
        "stati uniti",
        "usa",
        "united states",
        "boston",
        "washington",
        "chicago",
        "los angeles",
        "san francisco",
        "miami",
        "jfk",
        "newark",
    ),
    "IT": (
        "roma",
        "rome",
        "italia",
        "italy",
        "milano",
        "milan",
        "firenze",
        "florence",
        "napoli",
        "naples",
        "venezia",
        "venice",
        "torino",
        "turin",
        "bologna",
        "fiumicino",
        "malpensa",
    ),
}
_DISPLAY = {
    "nyc": "New York",
    "jfk": "New York",
    "newark": "New York",
    "usa": "gli Stati Uniti",
    "stati uniti": "gli Stati Uniti",
    "united states": "the United States",
    "fiumicino": "Roma",
    "malpensa": "Milano",
}


def label(kind: str, lang: str) -> str:
    it, en = LABELS.get(kind, LABELS["other"])
    return it if lang == "it-IT" else en


# the renewals done only in person in the document's country (an office, a medical visit, a DMV,
# USCIS biometrics, the car's revisione): their task is bound to it. A passport, a visa, a tax or an
# insurance is renewed from anywhere (a consulate, online) and binds nothing.
IN_PERSON_KINDS = frozenset({"cie", "driving_licence", "us_licence", "green_card", "car_revision"})


def bound_country(kind: str, country: str | None, aire: bool | None, home: str | None) -> str | None:
    """The country a renewal task can only be done in, or None. The residency rule: registered with AIRE, the
    CIE is renewed at the consulate abroad, so it binds nothing."""
    if kind not in IN_PERSON_KINDS:
        return None
    if kind == "cie" and aire is True:
        return None
    where = country or default_country(kind, home)
    return where if countries.is_code(where) else None


def default_country(kind: str, home: str | None) -> str | None:
    """A document's country when they didn't say: the issuer's for a kind only one country issues (the CIE is
    Italy's; a green card and a US licence the US's), else the user's own (PISA_COUNTRY); none for a visa (it
    is another country's) or when no country is set."""
    if kind in _ITALIAN_KINDS:
        return "IT"
    if kind in _US_KINDS:
        return "US"
    return None if kind == "visa" else home


def default_reminders(kind: str) -> list[dict[str, Any]]:
    days = DEFAULT_REMINDERS.get(kind, OTHER_REMINDERS)
    return [{"days_before": d, "done": False} for d in sorted(days, reverse=True)]


def last4(number: str | None) -> str | None:
    """RAIL: the last 4 letters or digits of a number, never more (spaces and dashes dropped)."""
    if not number:
        return None
    kept = re.sub(r"[^A-Za-z0-9]", "", number)
    return kept[-4:].upper() if len(kept) >= 4 else None


def valid_last4(value: str | None) -> bool:
    return value is None or bool(_LAST4.match(value))


def _numberish(token: str) -> bool:
    kept = _SEPARATORS.sub("", token)
    digits = sum(c.isdigit() for c in kept)
    return bool(_LONG_DIGITS.search(kept)) or (len(kept) >= 6 and digits >= 3)


def numbers_in(text: str) -> list[str]:
    """The document-number-like stretches of a text: runs of words that hold a digit (with a 1-2 letter
    prefix, "YA 123 4567"), separators included, longest first."""
    tokens = [(m.start(), m.end(), m.group(0).strip(".,;:)(!?")) for m in re.finditer(r"\S+", text)]
    out: list[str] = []
    run: list[tuple[int, int, str]] = []

    def close() -> None:
        if run and _numberish("".join(t for _, _, t in run)):
            start, end = run[0][0], run[-1][0] + len(run[-1][2])
            out.append(text[start:end])
        run.clear()

    for n, (start, end, word) in enumerate(tokens):
        nxt = tokens[n + 1][2] if n + 1 < len(tokens) else ""
        prefix = word.isalpha() and len(word) <= 2 and any(c.isdigit() for c in nxt)
        if word and (any(c.isdigit() for c in word) or prefix):
            run.append((start, end, word))
        else:
            close()
    close()
    return sorted(out, key=len, reverse=True)


def notes_ok(notes: str | None) -> bool:
    """RAIL: notes never hold a document number (a run of 5+ digits, with or without separators, or a code of
    6+ letters and digits with 3+ digits) and stay short."""
    return notes is None or (len(notes) <= NOTES_MAX and not numbers_in(notes))


def redact(text: str) -> str:
    """A text with every document-number-like stretch replaced (the user's words of an add_document turn)."""
    for stretch in numbers_in(text):
        text = text.replace(stretch, "<numero>")
    return text


def asks_to_add(words: str) -> bool:
    """Their own words ask Pisa to keep a document's expiry: a document named and an expiry or keep verb."""
    return bool(_DOC_WORDS.search(words) and _EXPIRY_WORDS.search(words))


def parse_expiry(text: str) -> date:
    """`2027-03-15`, `2027-03` or "marzo 2027" → a date; a month alone is its first day (reminders come
    earlier, never later). ValueError otherwise."""
    value = text.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", value):
        return date(int(m.group(1)), int(m.group(2)), 1)
    if m := re.fullmatch(r"(?:(\d{1,2})\s+)?([a-z]+)\s+(\d{4})", value):
        month = _MONTHS.get(m.group(2))
        if month is not None:
            return date(int(m.group(3)), month, int(m.group(1) or 1))
    if m := re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", value):
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    raise ValueError("expires_on must be YYYY-MM-DD, YYYY-MM or a month and a year")


def due(reminders: list[dict[str, Any]], expires_on: date, today: date) -> list[int]:
    """The indexes of the reminders due today or earlier and not done yet."""
    return [
        i
        for i, r in enumerate(reminders)
        if not r.get("done") and today >= expires_on - timedelta(days=int(r["days_before"]))
    ]


def window(reminders: list[dict[str, Any]], expires_on: date) -> tuple[date, date]:
    """The renewal window: from the first reminder to the expiry."""
    first = max((int(r["days_before"]) for r in reminders), default=OTHER_REMINDERS[0])
    return expires_on - timedelta(days=first), expires_on


def abroad(
    trips: list[tuple[str, date, date]], country: str | None, start: date, end: date
) -> tuple[str, date] | None:
    """Where they'll be outside the document's country during [start, end] by their calendar (each trip: its
    words, first and last day), the first one: (the place as people say it, its first day in the window). None
    when nowhere, or when the document has no country. The places are the ones `PLACES` knows."""
    if country is None:
        return None
    for text, first, last in sorted(trips, key=lambda t: t[1]):
        if last < start or first > end:
            continue
        lowered = f" {text.lower()} "
        for other, places in PLACES.items():
            if other == country:
                continue
            for place in places:
                if re.search(rf"(?<![a-z]){re.escape(place)}(?![a-z])", lowered):
                    return _DISPLAY.get(place, place.title()), max(first, start)
    return None


def month_name(day: date, lang: str) -> str:
    return MONTH_NAMES["it-IT" if lang == "it-IT" else "en-US"][day.month - 1]


def abroad_line(kind: str, expires_on: date, where: str, on: date, lang: str) -> str:
    """ "la CIE scade a marzo e a marzo sei a New York: prenoto al consolato?" """
    what = label(kind, lang)
    if lang == "it-IT":
        return (
            f"{what[0].upper()}{what[1:]} scade a {month_name(expires_on, lang)} e a {month_name(on, lang)} "
            f"sei a {where}: prenoto al consolato?"
        )
    then, there = month_name(expires_on, lang), month_name(on, lang)
    head = f"{what[0].upper()}{what[1:]} expires in {then} and in {there} you're in {where}"
    return f"{head}: shall I book at the consulate?"


def task_title(kind: str, lang: str) -> str:
    """ "Rinnovare la carta d'identità" / "Renew the ID card"."""
    it_verb, en_verb = _TASK_VERB.get(kind, ("Rinnovare", "Renew"))
    return f"{it_verb if lang == 'it-IT' else en_verb} {label(kind, lang)}"


# --- the paper ID card, AIRE, the first ten reminders -------------------------------------------------------

# Reg. (UE) 2019/1157: a paper ID card stopped being valid for travel abroad on 3 August 2026
# (its use inside Italy is disputed, so Pisa says only the travel part).
PAPER_ID = {
    "it-IT": "Una carta d'identità cartacea non vale più per l'espatrio dal 3 agosto 2026: conviene fare la "
    "CIE.",
    "en-US": "A paper Italian ID card is no longer valid for travel abroad since 3 August 2026: the CIE "
    "replaces it.",
}
_PAPER = re.compile(r"\bcartace\w*|\bdi\s+carta\b|\bpaper\s+(?:id|identity|card)\b|\bpaper\b", re.IGNORECASE)


def paper_id(kind: str, *texts: str | None) -> bool:
    """Their words or the document's notes say the ID card is a paper one."""
    return kind == "cie" and any(t and _PAPER.search(t) for t in texts)


def paper_line(lang: str) -> str:
    return PAPER_ID["it-IT" if lang == "it-IT" else "en-US"]


AIRE_QUESTION = {
    "it-IT": "Sei iscritto all'AIRE? Cambia dove rinnovi i documenti (consolato o Comune).",
    "en-US": "Are you registered with AIRE? It changes where you renew your documents (consulate or Comune).",
}
_AIRE = re.compile(r"\baire\b", re.IGNORECASE)
_AIRE_NO = re.compile(
    r"\b(?:non\s+(?:sono|siamo)\s+(?:iscritt\w*|registrat\w*)|non\s+ho\s+(?:fatto\s+)?l'?aire|"
    r"not\s+registered|i'?m\s+not|i\s+am\s+not|no)\b",
    re.IGNORECASE,
)
_AIRE_YES = re.compile(
    r"\b(?:sono\s+iscritt\w*|sono\s+registrat\w*|iscritto|iscritta|registered|s[iì]|yes|yeah)\b",
    re.IGNORECASE,
)


def aire_matters(kind: str, country: str | None, home: str | None) -> bool:
    """Where an Italian document is renewed depends on AIRE (Italy's registry of its citizens abroad): only
    an Italian document of these kinds, only for a user in Italy (PISA_COUNTRY=IT)."""
    return kind in AIRE_KINDS and country == "IT" and (home or "").upper() == "IT"


def aire_said(words: str, *, asked: bool) -> bool | None:
    """Their own words say whether they are registered with AIRE: they name it, or answer Pisa's question
    about it (`asked`: Pisa's last reply asked it). None when they say neither (never assumed)."""
    text = words or ""
    if not (_AIRE.search(text) or asked):
        return None
    if _AIRE_NO.search(text):
        return False
    if _AIRE_YES.search(text):
        return True
    return None


@dataclass(frozen=True)
class Starter:
    """A reminder that stands without a document: a task in the pool, once (or once a year), with its
    deadline. The document-bound ones are the lead times above."""

    key: str
    title: tuple[str, str]  # (Italian, English)
    notes: tuple[str, str]
    due: tuple[int, int] | None  # (month, day) each year; None: once, due in `days` days
    lead_days: int  # placed in the pool this many days before the due day
    days: int = 30


# No starter ships: reminders that stand without a document depend on one person's country, residence and
# taxes, so an install adds its own. The mechanism (`starter_due`, the vault's `starters` step) places these.
STARTERS: tuple[Starter, ...] = ()


def starter_due(starter: Starter, today: date) -> tuple[str, date] | None:
    """(the key it is kept under, its due day) when the starter belongs in the pool today, else None: a yearly
    one from its lead day to its due day (keyed by year), a one-off at once."""
    if starter.due is None:
        return starter.key, today + timedelta(days=starter.days)
    month, day = starter.due
    for year in (today.year, today.year + 1):
        due_on = date(year, month, day)
        if due_on - timedelta(days=starter.lead_days) <= today <= due_on:
            return f"{starter.key}:{year}", due_on
    return None
