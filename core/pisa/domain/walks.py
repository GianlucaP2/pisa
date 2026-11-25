"""The end-of-walk recap ("the day as a story"). Pure rules, no I/O.

- **A walk** is the stretch before a stay began (the location stream): at least 15 minutes of
  fixes moving at a walker's pace (the median between 0.4 and 2.5 m/s), counted from after the last stretch
  faster than a run (a bus, a car or a train before it is no walk), the fixes never more than 10 minutes
  apart. It ends when they stopped (the stay's arrival) at a known place: one of their places, or home.
- **The recap** says only what their own records hold from the walk's window: the tasks they added by voice,
  the notes they pinned to a place. Nothing is invented: no record, no recap. About 20
  seconds spoken (`WORDS_MAX`), the titles as they are.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Literal

from pisa.domain import geo_rules as geo
from pisa.domain.places import distance_m
from pisa.domain.stays import Fix

WALK_MIN = timedelta(minutes=15)
GAP_MAX = timedelta(minutes=10)  # fixes further apart than this: the stream is broken, no walk is told
FOOT_MAX_MPS = 2.5  # a brisk walk is ~1.7 m/s; above 2.5 at the median, they weren't on foot
FAST_MPS = 4.0  # a stretch faster than a run: a vehicle (the walk is what came after the last one)
FIXES_MIN = 4
RECAP_AFTER = timedelta(minutes=20)  # the recap goes in the first 20 minutes of the stop, else never
WORDS_MAX = 55  # ~20 seconds spoken
ITEMS_MAX = 6
TITLE_MAX = 60


@dataclass(frozen=True)
class Walk:
    start: datetime
    end: datetime
    metres: int

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def _speed(a: Fix, b: Fix) -> float:
    seconds = (b.at - a.at).total_seconds()
    return distance_m((a.lat, a.lon), (b.lat, b.lon)) / seconds if seconds > 0 else 0.0


def walk_before(fixes: Sequence[Fix], since: datetime, stopped_at: datetime) -> Walk | None:
    """The walk that ended at `stopped_at`, from the fixes after `since` (the previous stay's end): only its
    trailing run of stretches under 4 m/s (a car or a metro leg before it is no walk), and None
    when that run is shorter than 15 minutes, broken, or too slow or too fast to be on foot."""
    points = sorted(
        (f for f in fixes if since <= f.at <= stopped_at and f.accuracy_m <= geo.PACE_ACCURACY_MAX_M),
        key=lambda f: f.at,
    )
    for n in range(len(points) - 1, 0, -1):  # the last stretch faster than a run: the walk starts after it
        if _speed(points[n - 1], points[n]) > FAST_MPS:
            points = points[n:]
            break
    if len(points) < FIXES_MIN:
        return None
    speeds: list[float] = []
    metres = 0
    for a, b in pairwise(points):
        if b.at - a.at > GAP_MAX:
            return None
        if b.at > a.at:
            metres += distance_m((a.lat, a.lon), (b.lat, b.lon))
            speeds.append(_speed(a, b))
    if not speeds or points[-1].at - points[0].at < WALK_MIN:
        return None
    if not geo.FOOT_MIN_MPS <= median(speeds) <= FOOT_MAX_MPS:
        return None
    return Walk(points[0].at, points[-1].at, metres)


@dataclass(frozen=True)
class Item:
    """One record made during the walk, from their own words."""

    kind: Literal["task", "note"]
    text: str


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1].rstrip() + "…"


def _named(texts: list[str]) -> str:
    return ", ".join(_short(t) for t in texts)


def recap(items: Sequence[Item], minutes: int, lang: str) -> str | None:
    """The recap in about 20 seconds: what they said, by kind, in their records' own words. None with
    nothing."""
    if not items:
        return None
    kept = list(items[:ITEMS_MAX])
    tasks = [i.text for i in kept if i.kind == "task"]
    notes = [i.text for i in kept if i.kind == "note"]
    it = lang == "it-IT"
    parts: list[str] = []
    if tasks:
        head = (
            ("una cosa da fare" if len(tasks) == 1 else f"{len(tasks)} cose da fare")
            if it
            else ("one task" if len(tasks) == 1 else f"{len(tasks)} tasks")
        )
        parts.append(f"{head}: {_named(tasks)}")
    if notes:
        head = (
            ("una nota su un posto" if len(notes) == 1 else f"{len(notes)} note sui posti")
            if it
            else ("a place note" if len(notes) == 1 else f"{len(notes)} place notes")
        )
        parts.append(f"{head}: {_named(notes)}")
    joined = "; ".join(parts)
    line = (
        f"Nella camminata di {minutes} minuti mi hai lasciato {joined}."
        if it
        else f"On your {minutes}-minute walk you left me {joined}."
    )
    words = line.split()
    if len(words) > WORDS_MAX:
        line = " ".join(words[:WORDS_MAX]).rstrip(",;:") + "…"
    if len(items) > ITEMS_MAX:
        line += " E altro: è tutto nelle tue liste." if it else " And more: it's all in your lists."
    return line
