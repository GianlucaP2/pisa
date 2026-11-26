"""What kind of event it is, for the plan. Pure rules, no I/O.

- **fixed**: a reservation, a meeting, a flight, a dinner: Pisa never moves it; arriving after its start is
  late.
- **window**: a festival, a market, an exhibition day: the user may arrive any time between its start and its
  end minus a minimum stay (`min_stay_min`); arriving later than the start isn't late.
- **soft**: a plan the user made in Pisa (an event Pisa created for them): Pisa may move it.

Pisa infers the kind from the title's words, the length and whether Pisa made the event; the user changes it
with one tap (`kind_source: owner`). Google Calendar is never edited: the kind and the arrival live in Pisa.
A long event isn't a window by length alone unless nothing in its title says it is fixed (a 3-hour dinner is
fixed, "options considered")."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pisa.domain import geo_rules as geo
from pisa.domain.home import Home
from pisa.domain.places import distance_m
from pisa.domain.profile import plain

EventKind = Literal["fixed", "window", "soft"]
KINDS: tuple[EventKind, ...] = ("fixed", "window", "soft")

# Words (as prefixes) that make an event a window: you come and go.
WINDOW_STEMS = (
    "festival", "fest", "fiera", "sagra", "mercat", "market", "markt", "mostra", "esposiz", "exhibit",
    "expo", "tentoonstel", "museo", "museum", "musea", "open day", "giornata", "fair", "kermis", "carneval",
    "carnival", "parade", "parata", "pride", "beach", "spiaggia", "strand", "biennal", "salone", "rassegna",
)  # fmt: skip
# Words (as prefixes) that make it fixed whatever its length: it starts when it starts.
FIXED_STEMS = (
    "volo", "flight", "vlucht", "treno", "train", "trein", "traghetto", "ferry", "imbarco", "boarding",
    "cena", "dinner", "diner", "pranzo", "lunch", "colazione", "breakfast", "ontbijt", "aperitiv", "drinks",
    "meeting", "riunion", "call", "appuntament", "appointment", "afspraak", "visita", "esame", "exam",
    "colloquio", "interview", "prenotaz", "reservation", "booking", "reservering", "concert", "show",
    "spettacol", "teatro", "theat", "cinema", "film", "partita", "match", "wedstrijd", "lezion", "lesson",
    "class", "corso", "lavoro", "work", "werk", "ufficio", "office", "turno", "shift", "dentist", "medico",
    "doctor", "check in", "checkin",
)  # fmt: skip
# a wait the user spends at home (a delivery, the groceries, a technician): a window, and what
# the user does meanwhile overlaps it on purpose; it ends when the delivery is confirmed.
# only clear at-home cues ("Consegna tesi", "Colloquio tecnico", "Aspettare Marco in
# stazione" are not waits), and no fixed word outside the cue ("Consegna cena" is a delivery; "Cena, aspetto
# il corriere" a dinner).
WAIT_STEMS = (
    "consegna spesa", "consegna della spesa", "consegna pacc", "consegna del pacco", "consegna cena",
    "arriva il tecnico", "arrivo del tecnico", "visita del tecnico", "aspetto il tecnico", "resta a casa",
    "restare a casa", "corriere", "aspetto il corriere", "aspetto la consegna", "aspetto il pacco",
    "ritiro a domicilio", "grocery delivery", "groceries delivery", "parcel delivery", "package delivery",
    "food delivery", "courier", "technician visit", "technician coming", "stay home", "stay at home",
    "bezorging", "monteur komt", "thuis blijven",
)  # fmt: skip
WAIT_MIN_MIN = 30
WINDOW_MIN_MIN = 90  # a window shorter than this is just an event
LONG_MIN = 6 * 60  # this long, and nothing fixed in its title: a window (a day at a fair)
MIN_STAY_DEFAULT = 120  # minutes, at most half the event


@dataclass(frozen=True)
class Kind:
    kind: EventKind
    source: Literal["inferred", "owner"]
    min_stay_min: int | None  # window only


def _has(words: str, stems: tuple[str, ...]) -> bool:
    text = f" {words} "
    return any(f" {s}" in text for s in stems)


def waits(title: str, *, at_home: bool) -> bool:
    """The event is a wait at home (a delivery, a technician): a window that ends when it's confirmed.
    an at-home cue, no fixed word besides it, and no place or the home's."""
    words = plain(title)
    if not at_home or not _has(words, WAIT_STEMS):
        return False
    rest = f" {words} "
    for stem in WAIT_STEMS:
        rest = re.sub(rf" {stem}[a-z0-9]*(?= )", " ", rest)
    return not _has(rest.strip(), FIXED_STEMS)


def at_home(place_id: str | None, coord: tuple[float, float] | None, home: Home | None) -> bool:
    """An event with no place, or at the user's home (its place, or within `geo_rules.HOME_M`)."""
    if place_id is None:
        return True
    if home is None:
        return False
    if home.place_id is not None and place_id == home.place_id:
        return True
    return coord is not None and distance_m(coord, home.coord) <= geo.HOME_M


def default_min_stay(minutes: int) -> int:
    return max(15, min(MIN_STAY_DEFAULT, minutes // 2))


def infer(
    title: str, start: datetime, end: datetime, *, made_by_pisa: bool = False, home: bool = True
) -> Kind:
    """The kind Pisa infers. Fixed words win over window words ("cena al festival" is a dinner). `home`: the
    event has no place or the home's (a wait is spent there)."""
    minutes = max(0, int((end - start).total_seconds() // 60))
    if made_by_pisa:
        return Kind("soft", "inferred", None)
    words = plain(title)
    if minutes >= WAIT_MIN_MIN and waits(title, at_home=home):
        return Kind("window", "inferred", default_min_stay(minutes))
    if _has(words, FIXED_STEMS):
        return Kind("fixed", "inferred", None)
    if minutes >= WINDOW_MIN_MIN and (_has(words, WINDOW_STEMS) or minutes >= LONG_MIN):
        return Kind("window", "inferred", default_min_stay(minutes))
    return Kind("fixed", "inferred", None)


def resolve(
    title: str,
    start: datetime,
    end: datetime,
    *,
    made_by_pisa: bool,
    owner_kind: str | None,
    owner_min_stay: int | None,
    home: bool = True,
) -> Kind:
    """The user's kind when they set one, else Pisa's inference. A window's minimum stay never exceeds the
    event's length."""
    minutes = max(0, int((end - start).total_seconds() // 60))
    if owner_kind in KINDS:
        stay = None
        if owner_kind == "window":
            stay = min(owner_min_stay if owner_min_stay is not None else default_min_stay(minutes), minutes)
        return Kind(owner_kind, "owner", stay)  # type: ignore[arg-type]
    return infer(title, start, end, made_by_pisa=made_by_pisa, home=home)
