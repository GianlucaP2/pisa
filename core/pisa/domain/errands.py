"""Errand combos. Pool tasks whose places are a short walk apart, or a short walk from
where their day already takes them (today's events with a place), make one suggestion: "passi da X e Y in 15
minuti?". At most once a day, never the same pair twice in a week, never in quiet hours; a question, never a
plan change (their yes is a normal request). No I/O."""

from dataclasses import dataclass
from itertools import combinations

from pisa.domain.places import distance_m

PAIR_M = 700  # two errands this close are one outing
ROUTE_M = 500  # an errand this close to a place they are going to today is on the way
WALK_M_PER_MIN = 80
STOP_MIN = 5  # a stop at the counter
TOTAL_MAX_MIN = 30  # a combo longer than this is no "quick" suggestion
REPEAT_DAYS = 7


@dataclass(frozen=True)
class Errand:
    task_id: str
    title: str
    place: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Stopover:
    """A place their day takes them to (an event's place), with the event's title."""

    title: str
    place: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Combo:
    errands: tuple[Errand, ...]
    minutes: int
    near: Stopover | None = None

    @property
    def key(self) -> str:
        return "+".join(sorted(e.task_id for e in self.errands))


def _walk(a: tuple[float, float], b: tuple[float, float]) -> int:
    return round(distance_m(a, b) / WALK_M_PER_MIN)


def best(errands: list[Errand], stops: list[Stopover], skip: set[str]) -> Combo | None:
    """The quickest combo not suggested lately (`skip`: their keys): a pair of errands near each other, else
    one or two errands on the way to a place of today's. None when nothing is quick enough."""
    found: list[Combo] = []
    for a, b in combinations(errands, 2):
        if a.place == b.place or distance_m((a.lat, a.lon), (b.lat, b.lon)) > PAIR_M:
            continue
        minutes = _walk((a.lat, a.lon), (b.lat, b.lon)) + 2 * STOP_MIN
        found.append(Combo((a, b), minutes))
    for stop in stops:
        near = [e for e in errands if distance_m((e.lat, e.lon), (stop.lat, stop.lon)) <= ROUTE_M]
        near.sort(key=lambda e: distance_m((e.lat, e.lon), (stop.lat, stop.lon)))
        if near:
            picked = tuple(near[:2])
            minutes = sum(2 * _walk((e.lat, e.lon), (stop.lat, stop.lon)) + STOP_MIN for e in picked)
            found.append(Combo(picked, minutes, stop))
    found = [c for c in found if c.key not in skip and c.minutes <= TOTAL_MAX_MIN]
    if not found:
        return None
    return min(found, key=lambda c: (c.minutes, -len(c.errands), c.key))


def _round(minutes: int) -> int:
    return max(5, 5 * round(minutes / 5))


def question(combo: Combo, lang: str) -> str:
    names = [e.place for e in combo.errands]
    it = lang == "it-IT"
    joined = (" e " if it else " and ").join(names)
    minutes = _round(combo.minutes)
    if combo.near is not None:
        if it:
            return (
                f"Oggi passi da {combo.near.place}: ti fermi anche da {joined}? Sono circa {minutes} minuti."
            )
        return f"You're at {combo.near.place} today: stop by {joined} too? About {minutes} minutes."
    if it:
        return f"Passi da {joined} in {minutes} minuti? Sono vicini."
    return f"Stop by {joined} in {minutes} minutes? They're close together."
