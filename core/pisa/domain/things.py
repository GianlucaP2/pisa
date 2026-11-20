"""Things v1: one order, booking or trip, with its emails, its task and its events. Hard keys
only (report 2: 71% linked, no wrong link). Pure rules, no I/O.

- **What links**: the same email thread; an order, booking, ticket, tracking or reference number in both (the
  rules' keys, `pisa.domain.email_keys`); a task's source email; an email's date and a venue word that match a
  calendar event that day.
- **What never links**: amounts and dates alone (every bill has them); a link never merges two things (an item
  belongs to one thing; a second key found elsewhere only adds the item to the first thing found).
- **One task per thing**: a second email about the same order updates its open task, never adds one.
"""

import re
from collections.abc import Sequence
from datetime import date
from typing import Any

from pisa.domain.lexicon.dates import day_or_none

HARD_KINDS = frozenset({"order", "booking", "ticket", "tracking", "reference"})
TITLE_MAX = 200
_DATE = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), (1, 2, 3)),
    (re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b"), (3, 2, 1)),
)
_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def typed_keys(keys: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    """The hard keys with their kind ((kind, value), each value once), for the rail: two keys of the same
    kind (two order numbers) are two things, always."""
    out: list[tuple[str, str]] = []
    for key in keys:
        value = " ".join(str(key.get("value") or "").split()).upper()
        kind = str(key.get("kind"))
        if kind in HARD_KINDS and len(value) >= 5 and all(v != value for _, v in out):
            out.append((kind, value))
    return out


def key_class(kind: str | None) -> str:
    """The kind is a model's label, so an order, a booking, a ticket and a reference
    number are one identity class (`id`); only a tracking number is another. A key kept before kinds were
    (`None`) counts as an `id`: it never bridges two orders."""
    return "tracking" if kind == "tracking" else "id"


def joinable(email: Sequence[tuple[str, str]], held: Sequence[tuple[str | None, str]]) -> bool:
    """An email joins the things its keys name only when it carries one
    key of each class (an order number and its tracking number, never two order-class numbers), and the
    things together would still hold one key of each class. A key of unknown kind is order-class."""
    classes = [key_class(k) for k, _ in email]
    if len(set(classes)) != len(classes):
        return False
    seen: dict[str, str] = {}
    for kind, value in [*email, *held]:
        if seen.setdefault(key_class(kind), value) != value:
            return False
    return True


def anchor(email: Sequence[tuple[str, str]]) -> str | None:
    """An email that names several order-class numbers (an invoice for two orders)
    belongs to the thing of its lowest one, whichever order Pisa reads the emails in; None when it names
    one."""
    ids = sorted(v for k, v in email if key_class(k) == "id")
    return ids[0] if len(ids) > 1 else None


def dates(keys: Sequence[dict[str, Any]]) -> list[date]:
    """The calendar dates an email's date keys name (ISO, or day/month/year)."""
    out: list[date] = []
    for key in keys:
        if key.get("kind") != "date":
            continue
        text = str(key.get("value") or "")
        for pattern, (y, m, d) in _DATE:
            for match in pattern.finditer(text):
                found = day_or_none(int(match.group(y)), int(match.group(m)), int(match.group(d)))
                if found is None:
                    continue
                if found not in out:
                    out.append(found)
    return out


def venue_words(where: str | None) -> set[str]:
    return {w.casefold() for w in _WORD.findall(where or "")}


# words that name a city, a country or any venue: a delivery "a Milano" must never link every
# Milan event that day. A link needs a word of the venue's own name.
VENUE_STOP = frozenset({
    # cities and countries often named
    "milano", "roma", "torino", "napoli", "firenze", "bologna", "venezia", "genova", "verona", "padova",
    "trieste", "bari", "palermo", "catania", "pisa", "brescia", "bergamo", "parma", "modena", "monza",
    "amsterdam", "rotterdam", "utrecht", "haarlem", "leiden", "eindhoven", "london", "londra",
    "paris", "parigi", "berlin", "berlino", "madrid", "barcelona", "barcellona", "bruxelles", "brussels",
    "italia", "italy", "nederland", "netherlands", "olanda", "paesi", "bassi", "europa", "europe",
    # what any address or venue says
    "via", "viale", "vicolo", "piazza", "piazzale", "corso", "largo", "strada", "centro", "città", "citta",
    "street", "road", "avenue", "square", "straat", "plein", "weg", "gracht", "laan", "kade",
    "stazione", "station", "aeroporto", "airport", "hotel", "ristorante", "restaurant", "teatro", "theatre",
    "theater", "cinema", "museo", "museum", "sala", "ufficio", "office", "casa", "home", "sede", "presso",
    "location", "online", "zoom", "meet", "store", "negozio", "punto", "ritiro", "locker", "pickup",
    "centrale", "central", "nord", "sud", "est", "ovest", "north", "south", "east", "west",
})  # fmt: skip


def link_words(where: str | None) -> set[str]:
    """The venue words that may link an event: its own name's, never a city's or a street's."""
    return venue_words(where) - VENUE_STOP


def same_venue(words: set[str], event_title: str, event_location: str | None) -> bool:
    """FACT: one of the email's venue words (4+ letters, not a city or a generic word) in the event's place or
    title."""
    said = venue_words(f"{event_title} {event_location or ''}")
    return bool((words - VENUE_STOP) & said)


def title(subject: str | None, what: str | None) -> str:
    return " ".join((what or subject or "").split())[:TITLE_MAX] or "—"
