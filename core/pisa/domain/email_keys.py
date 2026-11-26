"""What the events call reads of an email, and the rules that find its keys. Pure rules, no I/O.

- **Hidden text out**: zero-width, bidirectional and tag characters (a common way to hide instructions in an
  email) are removed before a model reads it.
- **Datamarking**: the body goes to the model with `ˆ` in place of every space, so text from the email can't
  pass for the prompt's own words (spotlighting, Hines et al. 2024).
- **Keys** (order, booking, ticket and tracking numbers, amounts, dates; schema.org JSON-LD when present):
  the rules here find the labelled ones in the text, word for word (`found_keys`); the events call's own keys
  are checked against the text in `domain/mail_events.py`.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pisa.domain.lexicon.months import EN_MONTHS, IT_MONTHS

KeyKind = Literal["order", "booking", "ticket", "tracking", "amount", "date", "reference"]
KEY_KINDS: tuple[KeyKind, ...] = ("order", "booking", "ticket", "tracking", "amount", "date", "reference")
KEY_MAX = 80
MARK = "ˆ"  # ˆ, between the email's words

_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")

# A label, then the number it names: "Ordine n. 402-1234567", "Booking reference: X7KQ2P", "Tracking #1Z999…".
_LABELS: dict[KeyKind, str] = {
    "order": r"order|ordine|bestelling|purchase",
    "booking": r"booking|prenotazione|reservation|reservering|pnr|confirmation|conferma",
    "ticket": r"ticket|biglietto|e-?ticket",
    "tracking": r"tracking|spedizione|shipment|tracciamento|zending",
    "reference": r"reference|riferimento|codice|code|pratica|invoice|fattura|factuur",
}
_REF = re.compile(
    r"\b(?P<label>{labels})\b[^\n\w]{{0,4}}(?:(?:n(?:o|um(?:ber|ero)?)?|#)\.?\s*)?[:#]?\s*"
    r"(?P<value>(?=[A-Z-]*\d)[A-Z0-9][A-Z0-9-]{{3,}}[A-Z0-9])\b".format(labels="|".join(_LABELS.values())),
    re.IGNORECASE,
)
_AMOUNT = re.compile(
    r"(?:€|EUR|USD|GBP|\$|£)\s?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?(?!\d)"
    r"|\b\d{1,3}(?:[.,]\d{3})*[.,]\d{2}\s?(?:€|EUR\b|euro\b)",
    re.IGNORECASE,
)
_DUTCH_MONTHS = (
    "januari",
    "februari",
    "maart",
    "mei",
    "juni",
    "juli",
    "augustus",
    "oktober",
)  # the others: as English
_MONTHS = "|".join((*IT_MONTHS, *EN_MONTHS, *_DUTCH_MONTHS))
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"
    rf"|\b\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}\b|\b(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}\b",
    re.IGNORECASE,
)
# schema.org properties that are keys (JSON-LD in booking and order emails).
_LD_KEYS: dict[str, KeyKind] = {
    "reservationNumber": "booking", "confirmationNumber": "booking", "orderNumber": "order",
    "trackingNumber": "tracking", "ticketNumber": "ticket", "ticketToken": "ticket",
    "totalPrice": "amount", "price": "amount", "startDate": "date", "startTime": "date",
    "checkinTime": "date", "departureTime": "date", "expectedArrivalUntil": "date",
}  # fmt: skip


@dataclass(frozen=True)
class Key:
    kind: KeyKind
    value: str


def strip_hidden(text: str) -> str:
    return _HIDDEN.sub("", text)


def datamark(text: str) -> str:
    """Every space between the email's words becomes `ˆ` (new lines stay); the marker itself is removed from
    the text first, so the email can't fake it."""
    clean = strip_hidden(text).replace(MARK, "")
    return "\n".join(MARK.join(line.split()) for line in clean.split("\n"))


def plain(text: str) -> str:
    """The text as read: hidden characters out, datamarks back to spaces, spaces collapsed."""
    return " ".join(strip_hidden(text).replace(MARK, " ").split())


_plain = plain


def verbatim(value: str, text: str) -> bool:
    """The value is word for word in the email (spaces aside)."""
    v = _plain(value)
    return bool(v) and v.casefold() in _plain(text).casefold()


def _ld(text: str) -> list[Key]:
    out: list[Key] = []
    for block in re.findall(r"\{[^{}]*schema\.org[\s\S]*?\}\s*(?=\n\n|$)", text):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack: list[Any] = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                for name, value in item.items():
                    if isinstance(value, dict | list):
                        stack.append(value)
                    elif name in _LD_KEYS and isinstance(value, str | int | float) and str(value).strip():
                        out.append(Key(_LD_KEYS[name], str(value).strip()[:KEY_MAX]))
    return out


def found_keys(text: str) -> list[Key]:
    """The keys the rules recognise in an email: labelled numbers, amounts, dates, JSON-LD properties."""
    text = strip_hidden(text)
    out: list[Key] = []
    for m in _REF.finditer(text):
        value = m.group("value")
        if not any(c.isdigit() for c in value):
            continue  # "code: WELCOME" is a word, not a number
        label = m.group("label").lower()
        kind: KeyKind = next(k for k, p in _LABELS.items() if re.fullmatch(p, label, re.IGNORECASE))
        out.append(Key(kind, value))
    out += [Key("amount", " ".join(m.group().split())) for m in _AMOUNT.finditer(text)]
    out += [Key("date", " ".join(m.group().split())) for m in _DATE.finditer(text)]
    return out + _ld(text)
