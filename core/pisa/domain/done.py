"""Done by being there. Pure rules, no I/O.

- An open task with a place, or today's event with a place, **planned or not**, is done when the user stays
  there at least 10 minutes: the stays Pisa already detects, at the place. A stay linked to a known place
  counts only at that place (home or the café next door isn't the shop); a stay linked to
  no place counts within max(75 m, its accuracy) of it, as the visit detection does.
- One stay closes one occurrence: a task made after the stay began (a repeat's next occurrence, made by
  closing the last one) waits for another visit.
- When: a task within its day (today for a task due today, overdue or undated; a task due on a later day waits
  for that day); an event within its time ± 1 hour.
- An ongoing stay counts up to now while its last fix is at most 2 hours old, else up to that fix.
- Pisa says it in one line ("Fatto: BOBBY'S Bitterballen ✓") with Undo for 24 hours.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pisa.domain.follow import Item, Seen, at_place
from pisa.domain.places import distance_m
from pisa.domain.stays import STAY_RADIUS_M
from pisa.domain.tasks import title_key

# FACT: ten minutes at the place is done (reversible for 24 hours).
DONE_STAY = timedelta(minutes=10)
EVENT_MARGIN = timedelta(hours=1)
UNDO_FOR = timedelta(hours=24)
FRESH = timedelta(hours=2)  # an ongoing stay's last fix this recent: it still runs until now


@dataclass(frozen=True)
class Stayed:
    """A stay, as this rule needs it."""

    arrived_at: datetime
    departed_at: datetime | None
    last_seen_at: datetime
    lat: float
    lon: float
    accuracy_m: float
    place_id: str | None
    place: tuple[float, float] | None = None  # the linked place's position


def _until(stay: Stayed, now: datetime) -> datetime:
    if stay.departed_at is not None:
        return stay.departed_at
    return now if now - stay.last_seen_at <= FRESH else stay.last_seen_at


def stayed_there(
    stays: Sequence[Stayed],
    place_id: str | None,
    place: tuple[float, float] | None,
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    made_at: datetime | None = None,
) -> Stayed | None:
    """The first stay at the place with at least 10 minutes inside [start, end) (and before now). `made_at`:
    when the task was made; a stay that began before it doesn't count."""
    if place_id is None and place is None:
        return None
    for s in stays:
        if made_at is not None and s.arrived_at < made_at:
            continue
        if s.place_id is not None:
            if s.place_id != place_id and not _next_door(s, place):
                continue
        elif not at_place(
            Seen(s.arrived_at, s.departed_at, s.lat, s.lon, s.accuracy_m, None),
            Item("task", start, end, None, place),
        ):
            continue
        inside = min(_until(s, now), end, now) - max(s.arrived_at, start)
        if inside >= DONE_STAY:
            return s
    return None


NEXT_DOOR_M = 20  # the linked place is nearer than the item's by less than this: it may be either (13B)


def _next_door(s: Stayed, place: tuple[float, float] | None) -> bool:
    """A stay links to the nearest known place; in a dense centre a stay in the shop can sit nearer the café
    30 m away. The item's place still counts when it is within the stay's reach and the
    linked one is less than 20 m nearer; home or a café 100 m away never does."""
    if place is None or s.place is None:
        return False
    to_item = distance_m((s.lat, s.lon), place)
    return (
        to_item <= max(STAY_RADIUS_M, s.accuracy_m)
        and to_item - distance_m((s.lat, s.lon), s.place) < NEXT_DOOR_M
    )


def waits(due_day: date | None, today: date) -> bool:
    """A task due on a later day isn't done by being there today (its day is that day)."""
    return due_day is not None and due_day > today


def event_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    return start - EVENT_MARGIN, end + EVENT_MARGIN


def done_text(title: str, lang: str) -> str:
    """ "Fatto: BOBBY'S Bitterballen ✓" """
    return f"{'Fatto' if lang == 'it-IT' else 'Done'}: {title} ✓"


_STOP = frozenset({
    "the", "and", "for", "con", "per", "del", "della", "dal", "alla", "allo", "una", "uno", "sono", "stato",
    "stata", "fatto", "fatta", "done", "been", "went", "there", "het", "een", "van", "voor",
})  # fmt: skip


def _words(text: str) -> list[str]:
    return [w for w in title_key(text).split() if len(w) >= 3 and w not in _STOP]


def matches(what: str, items: Sequence[tuple[str, str | None]]) -> list[int]:
    """The items (title, place name) the user's words name, best first: every word of theirs (3 letters or
    more) found at the start of a word of the title or the place counts. Only the best-scoring ones are
    returned: one is the item, more than one is a question."""
    asked = _words(what)
    if not asked:
        return []
    scored: list[tuple[float, int]] = []
    for i, (title, place) in enumerate(items):
        words = _words(f"{title} {place or ''}")
        hit = sum(
            1 for w in asked if any(x.startswith(w) or (len(x) >= 4 and w.startswith(x)) for x in words)
        )
        if hit:
            scored.append((hit / len(asked), i))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [i for s, i in scored if s == best]
