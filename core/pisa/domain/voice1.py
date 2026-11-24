"""The knock, the morning on "dimmi", il
tragitto. Pure rules, no I/O.

- **The knock** ("Posso dirti una cosa?") is the one way Pisa opens a talk unasked. At most once a morning,
  through `attend.decide` and its rails (a breakpoint, never `now`; quiet hours drop it; meetings and their
  talk to Pisa defer it; "Pisa, ferma tutto" stops the minute), and it counts against the 2-a-day
  unprompted-ask cap. It says nothing itself: only their yes ("sì", "dimmi") opens what it carries.
- **What it carries** (`Part`): the floor item of their day (the next fixed event or leave time, from the plan
  and the calendar), the morning thought (its record), the morning sheet's lead (once the sheet's gate is
  open and its lines still pass). Never invented: each part names its record (`table`, `ref`); no record, no
  part; no part, no knock.
- **The morning on "dimmi"** ("cosa c'è?"): the parts in the brief's order (the day, then the thought, then
  the sheet: the morning briefing's own order), short. They interrupt at any time: "basta" stops, "dopo" keeps
  it for when they ask. "Dammi tutto" opens the night's whole drawer on the phone.
- **Il tragitto**: when the phone is on the move on foot or by transit (the location fixes) towards the
  plan's next place, Pisa offers once "Vuoi il tragitto?"; on their yes, the next items, the next leave time
  and the weather at arrival, from the plan, the calendar and the forecast Pisa already keeps.
- **Everything is a question or an offer**: nothing is done, sent or booked on any of these
  words; Pisa only speaks their own records back.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import pairwise
from statistics import median
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain import geo_rules as geo
from pisa.domain.places import distance_m
from pisa.domain.stays import Fix
from pisa.domain.text import fold

PartKind = Literal[
    "floor", "thought", "sheet", "prurito", "perche", "consiglio", "prova", "quarto",
    # the dossier's own items ride the knock's payload by record (never spoken by `spoken`)
    "provato", "radar", "loop", "draft",
]  # fmt: skip
Offer = Literal["knock", "tragitto"]
OFFERS: tuple[Offer, ...] = ("knock", "tragitto")
ORDER: tuple[PartKind, ...] = (
    "floor",
    "thought",
    "sheet",
    # il prurito's answer that changes something (the only part the same day's knock carries,
    # `SAME_DAY`), then the reason of a confident miss (the morning knock's alone)
    "prurito",
    "perche",
    "consiglio",  # il consiglio and la prova generale, on Sunday, offered only here
    "prova",
    "quarto",  # il quarto d'ora is offered only here, last (pisa.domain.voice3)
)  # the morning briefing's own order

# a knock past the morning carries il prurito's answer only (a miss's reason stays
# with the morning knock and its own once-a-morning limit)
SAME_DAY: frozenset[PartKind] = frozenset({"prurito"})

# RAIL: Pisa's unprompted asks, at most this many a day in code. An
# unprompted ask is a question Pisa sent unasked: a notice carrying one of Pisa's one-yes questions (`ask_id`:
# the morning thought's, a ritual's, the knock's, the tragitto's), or a knock or tragitto still being decided.
ASKS_PER_DAY = 2
WAVED_AWAY = 3  # declined or ignored offers in a row: Pisa stops offering that kind until their "dimmi"
ONE_AT_A_TIME = timedelta(minutes=30)  # no offer within this of another of Pisa's questions
MORNING_END = time(12, 0)  # the knock is a morning's: from the briefing window's start until noon
PART_MAX = 180  # one part, one short sentence (~10 s spoken)
TRAGITTO_ITEMS = 2  # the day's next items said on the way
FLOOR_AHEAD = timedelta(hours=14)  # the floor item: today's next fixed event or leave time, within this

KNOCK = {"it-IT": "Posso dirti una cosa?", "en-US": "Can I tell you something?"}
TRAGITTO = {"it-IT": "Vuoi il tragitto?", "en-US": "Do you want the trip briefing?"}
LATER = {
    "it-IT": "Va bene, te lo dico quando me lo chiedi.",
    "en-US": "Okay, I'll tell you when you ask.",
}
ENOUGH = {"it-IT": "Ok, mi fermo.", "en-US": "Okay, I'll stop."}
NOTHING = {
    "it-IT": "Stamattina non ho niente di nuovo da dirti.",
    "en-US": "I have nothing new for you this morning.",
}
NO_TUTTO = {
    "it-IT": "Della notte non ho ancora niente da mostrarti.",
    "en-US": "I have nothing from the night to show you yet.",
}
NO_TRIP = {"it-IT": "Sul tragitto non ho altro da dirti.", "en-US": "I have nothing more for the trip."}
DECLINED = {"it-IT": "Va bene.", "en-US": "Okay."}

# the location fixes: on foot (walks.py's pace) or by transit, never a plane; a car is driving
MOVING_FOR = timedelta(minutes=4)  # the phone has moved for at least this long, in the last MOVING_WITHIN
MOVING_WITHIN = timedelta(minutes=10)
TRANSIT_MAX_MPS = 25.0  # a metro, a tram, a regional train (not an intercity at 80 m/s)
FIXES_MIN = 3
TRIP_SLACK = timedelta(minutes=20)  # the plan's travel item covers now, give or take this


@dataclass(frozen=True)
class Part:
    """One thing the knock carries, with the record it comes from (never invented)."""

    kind: PartKind
    text: str
    table: str  # the record's table: calendar_events, plans, morning_thoughts, pisa_sheets
    ref: str


def _plain(text: str) -> str:
    words = re.findall(r"[a-z']+", fold(text))
    return " ".join(w.strip("'") for w in words if w.strip("'"))


# Their words. Each is the whole utterance (a sentence that says more is the conversation's, never these).
_DIMMI = {
    "dimmi", "dimmi pure", "si dimmi", "si dimmi pure", "ok dimmi", "dai dimmi", "dimmelo", "dimmi tutto",
    "cosa c'e", "cosa c e", "che c'e", "che c e", "cos'e", "cose", "cosa ce", "che ce", "dimmi cosa c'e",
    "dimmi cosa c e", "dimmi che c'e", "cosa c'e oggi", "cosa c e oggi",
    "tell me", "what is it", "what's up", "whats up", "what's new", "whats new",
}  # fmt: skip
_TUTTO = {
    "dammi tutto", "dimmi tutto quello che hai", "fammi sentire tutto", "fammi vedere tutto",
    "fammi vedere tutto quello che hai pensato", "give me everything", "tell me everything",
    "show me everything", "pisa dammi tutto",
}  # fmt: skip
_LATER = {
    "dopo", "piu tardi", "dopo dai", "non ora", "non adesso", "adesso no", "ora no", "dopo grazie",
    "magari dopo", "later", "not now", "maybe later",
}  # fmt: skip
_ENOUGH = {
    "basta", "basta cosi", "basta grazie", "ok basta", "ok basta cosi", "stop", "fermati", "ferma",
    "ok grazie basta",
    "enough", "that's enough", "thats enough", "ok stop",
}  # fmt: skip


def _strip_pisa(words: str) -> str:
    return words.removeprefix("pisa ").strip()


def is_dimmi(text: str) -> bool:
    """ "Dimmi", "cosa c'è?", "sì, dimmi": they ask Pisa to say what it has (the morning, or the open
    offer)."""
    words = _strip_pisa(_plain(text))
    return words in {_plain(w) for w in _DIMMI}


def is_tutto(text: str) -> bool:
    words = _plain(text)
    return words in {_plain(w) for w in _TUTTO} or _strip_pisa(words) in {_plain(w) for w in _TUTTO}


def is_later(text: str) -> bool:
    return _strip_pisa(_plain(text)) in {_plain(w) for w in _LATER}


def is_enough(text: str) -> bool:
    return _strip_pisa(_plain(text)) in {_plain(w) for w in _ENOUGH}


def word(table: dict[str, str], lang: str) -> str:
    return table.get(lang, table["en-US"])


def one_line(text: str, limit: int = PART_MAX) -> str:
    """One sentence, at most `limit` characters, cut at a word; no quotes of its own."""
    flat = " ".join(text.replace('"', "").split())
    first = re.split(r"(?<=[.!?…])\s", flat, maxsplit=1)[0]
    if len(first) <= limit:
        return first if first.endswith((".", "!", "?", "…")) else first + "."
    cut = first[:limit].rsplit(" ", 1)[0]
    return (cut or first[:limit]) + "…"


def ordered(parts: Sequence[Part]) -> list[Part]:
    """The parts in the brief's order, one of each kind at most."""
    out: list[Part] = []
    for kind in ORDER:
        found = next((p for p in parts if p.kind == kind and p.text.strip()), None)
        if found is not None:
            out.append(found)
    return out


def new_parts(parts: Sequence[Part], said: set[tuple[str, str]]) -> list[Part]:
    """The parts whose record (table, id) was not already said to them today (a briefing they got, a knock
    they opened): the knock is for what is new. By record, never by the words."""
    return [p for p in parts if (p.table, p.ref) not in said]


def waved_away(statuses: Sequence[str]) -> bool:
    """The last WAVED_AWAY offers of a kind that reached them were all declined or let
    lapse (newest first): Pisa stops offering it until their "dimmi"."""
    return len(statuses) >= WAVED_AWAY and all(s in ("no", "lapsed") for s in statuses[:WAVED_AWAY])


def spoken(parts: Sequence[Part]) -> str:
    """What Pisa says on their yes or "dimmi": each part's own sentence, in the brief's order."""
    return " ".join(one_line(p.text) for p in ordered(parts))


def asks_left(asked_today: int) -> bool:
    """RAIL: room for one more unprompted ask today (the 2-a-day cap)."""
    return asked_today < ASKS_PER_DAY


def in_morning(now: datetime, tz: str, window_start: str | None) -> bool:
    """The knock's hours: from the briefing window's start (else 06:00) until noon, their time."""
    local = now.astimezone(ZoneInfo(tz)).time()
    start = time.fromisoformat(window_start) if window_start else time(6, 0)
    return start <= local < MORNING_END


# --- the parts' own words (code's templates, records only) --------------------------------------------------


def _hm(at: datetime, tz: str) -> str:
    local = at.astimezone(ZoneInfo(tz))
    return f"{local.hour}:{local.minute:02d}"


def floor_event(title: str, start: datetime, tz: str, lang: str) -> str:
    what = one_line(title, 80).rstrip(".")
    if lang == "it-IT":
        return f"Oggi alle {_hm(start, tz)} hai {what}."
    return f"Today at {_hm(start, tz)} you have {what}."


def floor_leave(title: str, leave_at: datetime, tz: str, lang: str) -> str:
    what = one_line(title, 80).rstrip(".")
    if lang == "it-IT":
        return f"Per {what} esci alle {_hm(leave_at, tz)}."
    return f"For {what}, leave at {_hm(leave_at, tz)}."


def sheet_lead(statement: str, lang: str) -> str:
    head = "Dal foglio di stanotte" if lang == "it-IT" else "From last night's sheet"
    return f"{head}: {one_line(statement, PART_MAX - len(head) - 2)}"


def tragitto(
    next_items: Sequence[tuple[str, datetime]],
    leave: tuple[str, datetime] | None,
    arrival: tuple[str, float, int] | None,
    tz: str,
    lang: str,
) -> str:
    """The trip, from records: the day's next items (title, start), the next leave time after them, the
    weather at arrival (the forecast's words, °C, rain %). Empty: nothing to say."""
    it = lang == "it-IT"
    out: list[str] = []
    items = [(one_line(t, 60).rstrip("."), s) for t, s in next_items[:TRAGITTO_ITEMS]]
    if items:
        said = (
            ", poi ".join(f"{t} alle {_hm(s, tz)}" for t, s in items)
            if it
            else ", then ".join(f"{t} at {_hm(s, tz)}" for t, s in items)
        )
        out.append(("Hai " if it else "Next: ") + said + ".")
    if leave is not None:
        out.append(("Poi, p" if it else "Then, f") + floor_leave(leave[0], leave[1], tz, lang)[1:])
    if arrival is not None:
        words, temp, rain = arrival
        if it:
            line = f"All'arrivo: {words.lower()}, {round(temp)} gradi"
            line += f", pioggia al {rain}%." if rain >= 30 else "."
        else:
            line = f"When you arrive: {words.lower()}, {round(temp)}°C"
            line += f", {rain}% chance of rain." if rain >= 30 else "."
        out.append(line)
    return " ".join(out)


# --- on the move --------------------------------------------------------------------------------------------


def moving(fixes: Sequence[Fix], now: datetime, route: str | None = None) -> bool:
    """The phone has been on the move on foot or by transit for a few minutes now (never driving: the car's
    route; never a plane's pace)."""
    if route == "car":
        return False
    points = sorted(
        (f for f in fixes if now - MOVING_WITHIN <= f.at <= now and f.accuracy_m <= geo.PACE_ACCURACY_MAX_M),
        key=lambda f: f.at,
    )
    if len(points) < FIXES_MIN or points[-1].at - points[0].at < MOVING_FOR:
        return False
    speeds = []
    for a, b in pairwise(points):
        seconds = (b.at - a.at).total_seconds()
        if seconds > 0:
            speeds.append(distance_m((a.lat, a.lon), (b.lat, b.lon)) / seconds)
    return bool(speeds) and geo.FOOT_MIN_MPS <= median(speeds) <= TRANSIT_MAX_MPS
