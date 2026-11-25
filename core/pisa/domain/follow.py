"""What really happened: each plan item's status from where the user was, and the check-in Pisa asks
when the plan and reality differ. Pure functions, no I/O.

- `done`: an event's place was visited for at least half its length or 20 minutes, whichever is shorter
  (and it is over, or the user has left), or the user said so (a check-in answer).
- `ongoing`: it is under way and the user is there.
- `visited`: a task's place was visited today, for less than the 10 minutes that close it by themselves
  (pisa.domain.done), and the user has left: Pisa asks whether the task is done (a minute at the post
  office isn't collecting the parcel).
- `missed`: 15 minutes after its start, no stay at its place since 30 minutes before the start, the user more
  than 150 m away, and either it has ended or the trip from here takes more than half of what is left of it.
  "Away" needs a location of 150 m accuracy or better, more than 150 m plus its accuracy from the place;
  without one the location is unknown and nothing is missed.
- `planned` otherwise; `skipped` comes from an adjustment. Items without a place stay `planned`.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pisa.domain import geo_rules as geo
from pisa.domain.places import distance_m
from pisa.domain.stays import STAY_RADIUS_M

Status = Literal["planned", "ongoing", "done", "visited", "missed", "skipped"]
CheckInKind = Literal["missed", "confirm"]

MISSED_AFTER = timedelta(minutes=15)
STAY_BEFORE = timedelta(minutes=30)
AWAY_M = 150
DONE_MAX = timedelta(minutes=20)


@dataclass(frozen=True)
class Seen:
    """A stay, as status needs it."""

    arrived_at: datetime
    departed_at: datetime | None
    lat: float
    lon: float
    accuracy_m: float
    place_id: str | None


@dataclass(frozen=True)
class Item:
    kind: Literal["event", "task"]
    start: datetime
    end: datetime
    place_id: str | None
    place: tuple[float, float] | None  # the place's position


def at_place(stay: Seen, item: Item) -> bool:
    if item.place_id is not None and stay.place_id == item.place_id:
        return True
    if item.place is None:
        return False
    return distance_m((stay.lat, stay.lon), item.place) <= max(STAY_RADIUS_M, stay.accuracy_m)


def _overlap(stay: Seen, start: datetime, end: datetime, now: datetime) -> timedelta:
    a = max(stay.arrived_at, start)
    b = min(stay.departed_at or now, end, now)
    return max(timedelta(0), b - a)


def status(
    item: Item,
    stays: Sequence[Seen],
    *,
    now: datetime,
    here: tuple[float, float] | None,
    trip_min: int | None,
    day_start: datetime,
    here_accuracy_m: float = 0.0,
) -> Status:
    """The item's status now. `here`: a location of 150 m accuracy or better (None otherwise), with its
    accuracy; `trip_min`: minutes from there to the item's place (None when unknown)."""
    if item.place is None and item.place_id is None:
        return "planned"
    there = [s for s in stays if at_place(s, item)]
    staying = any(s.departed_at is None for s in there)
    if item.kind == "event":
        visited = sum((_overlap(s, item.start, item.end, now) for s in there), timedelta(0))
        need = min((item.end - item.start) / 2, DONE_MAX)
        if item.start <= now < item.end and staying:
            return "ongoing"
        if visited >= need and visited > timedelta(0):
            return "done"
    else:
        if item.start <= now < item.end and staying:
            return "ongoing"
        # A stay that has ended: while the user is still there, 10 minutes close the task by themselves,
        # so Pisa doesn't ask first.
        if any(s.departed_at is not None and _overlap(s, day_start, now, now) > timedelta(0) for s in there):
            return "visited"
    if _missed(item, there, now=now, here=here, accuracy_m=here_accuracy_m, trip_min=trip_min):
        return "missed"
    return "planned"


def _missed(
    item: Item,
    there: Sequence[Seen],
    *,
    now: datetime,
    here: tuple[float, float] | None,
    accuracy_m: float,
    trip_min: int | None,
) -> bool:
    if (
        now < item.start + MISSED_AFTER
        or item.place is None
        or here is None
        or accuracy_m > geo.PLACE_ACCURACY_MAX_M
    ):
        return False
    if any(_overlap(s, item.start - STAY_BEFORE, now, now) > timedelta(0) for s in there):
        return False
    if distance_m(here, item.place) <= AWAY_M + accuracy_m:
        return False
    if now >= item.end:
        return True
    left = (item.end - now).total_seconds() / 60
    return trip_min is not None and trip_min > left / 2


# --- check-ins --------------------------------------------------------------------------------------------

CHOICES: dict[CheckInKind, tuple[str, ...]] = {
    "missed": ("reschedule", "alternative", "skip", "on_my_way", "did_it"),
    "confirm": ("done", "not_done"),
}
_LABELS = {
    "it-IT": {
        "reschedule": "Sposta", "alternative": "Alternativa", "skip": "Salta", "on_my_way": "Sto arrivando",
        "did_it": "Fatto", "done": "Fatto", "not_done": "Non ancora",
    },
    "en-US": {
        "reschedule": "Move", "alternative": "Another place", "skip": "Skip", "on_my_way": "On my way",
        "did_it": "Done", "done": "Done", "not_done": "Not yet",
    },
}  # fmt: skip


# what Pisa says when code takes the user's bare "sì" or "no" to a yes/no check-in (no model call).
ANSWERED = {
    "it-IT": {"done": "Bene, segnato.", "not_done": "Ok, allora resta da fare."},
    "en-US": {"done": "Good, noted.", "not_done": "OK, it stays on the list."},
}


def answered(choice: str, lang: str) -> str:
    return ANSWERED["it-IT" if lang == "it-IT" else "en-US"][choice]


def choices(kind: CheckInKind, lang: str) -> list[dict[str, str]]:
    labels = _LABELS["it-IT" if lang == "it-IT" else "en-US"]
    return [{"id": c, "label": labels[c]} for c in CHOICES[kind]]


def check_in_text(kind: CheckInKind, title: str, place: str | None, lang: str) -> str:
    """ "Sembra che tu non sia andato a Pranzo. Lo sposto, cerco un altro posto o lo salto?" / "Sei passato da
    Poste Italiane: hai fatto «Ritirare il pacco»?" """
    it = lang == "it-IT"
    if kind == "missed":
        if it:
            return f"Sembra che tu non sia andato a {title}. Lo sposto, cerco un altro posto o lo salto?"
        return f"Looks like you didn't make it to {title}. Move it, find another place, or skip it?"
    where = place or title
    if it:
        return f"Sei passato da {where}: hai fatto «{title}»?"
    return f"You were at {where}: did you do “{title}”?"


_DIRECTIONS = re.compile(
    r"\b(?:portami|accompagnami|indicazioni|naviga\w*|percorso|strada|come (?:ci )?arrivo|andiamo|"
    r"directions?|navigate|take me|route|how do i get|get me to|let'?s go)\b",
    re.IGNORECASE,
)


def asks_directions(owner_words: str) -> bool:
    """The user's own words ask to be taken somewhere ("portami al Spiraal")."""
    return _DIRECTIONS.search(owner_words) is not None


# --- a reminder named to cancel -----------------------------------------------------------------------------

# words that name no reminder in particular (cancelling, the word reminder, a day: "annulla il promemoria
# di domani" is matched by its other words)
_PROMISE_STOP = frozenset(
    "annul cance elimi togli rimuo promem ricor remin alert avvis quell quest della delle dello degli "
    "cancel delet remov forge about never mind lasci perde domani today oggi tomor stase".split()
)


def _stems(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return {w[:5] for w in re.findall(r"[a-z0-9]+", plain) if len(w) >= 4} - _PROMISE_STOP


def promises_named(words: str, promises: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The live reminders their words name ("il promemoria per Anna" → «chiamare Anna»): those sharing the
    most words of weight with the reminder's text (or its place), none when no word is shared."""
    said = _stems(words)
    scored = [(len(said & _stems(f"{p['what']} {p.get('place') or ''}")), p) for p in promises]
    best = max((n for n, _ in scored), default=0)
    return [p for n, p in scored if best and n == best]
