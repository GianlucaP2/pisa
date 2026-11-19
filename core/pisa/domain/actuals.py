"""Planned against actual: their own duration and travel error bars, fed back to the planner. Pure rules,
no I/O.

- **Each night** the day that ended is scored: each planned task, event and trip against what really happened,
  from their stays (`stays`: when they arrived at and left a place), the calendar and their done times
  (`tasks.completed_at`). Only what the records show: an item with no stay and no done time is not scored.
  - a task at a place: the stay there that meets its planned time (its length);
  - a task with no place, done that day: from its planned start to its done time;
  - an event at a place: from when they were there (its start, or later if they arrived late) to when they
    left;
  - a trip: from leaving the stay before it to arriving at its place.
- **The band** (`band`): per kind, the ratio actual ÷ planned over the last `WINDOW_DAYS`, its 20th, 50th and
  80th percentile, once there are `MIN_SAMPLES`. The planner reads it: a task's duration and a trip's minutes
  are planned at their median (`estimate`), and each item shows the band.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Kind = Literal["task", "event", "travel"]
KINDS: tuple[Kind, ...] = ("task", "event", "travel")

WINDOW_DAYS = 28  # the band reads the last four weeks
MIN_SAMPLES = 5  # fewer scored items of a kind: no band, the planner's own numbers
Q_LO, Q_HI = 0.2, 0.8  # the band: their 20th to 80th percentile
RATIO_MIN, RATIO_MAX = 0.25, 4.0  # a sample outside this is a matching error, never their spread
FACTOR_MIN, FACTOR_MAX = (
    0.5,
    2.0,
)  # the planner never plans a duration at less than half or over twice its own
MEET_SLACK = timedelta(hours=2)  # a stay "meets" a planned item within this of its planned time
TRIP_LEAVE_SLACK = timedelta(hours=1)  # a trip's departure: the stay before it ended within this of leave_at


@dataclass(frozen=True)
class Stay:
    arrived: datetime
    departed: datetime | None
    place_id: str | None


@dataclass(frozen=True)
class Sample:
    kind: Kind
    ref: str
    planned_min: int
    actual_min: int
    source: Literal["stay", "done"]

    @property
    def ratio(self) -> float:
        return self.actual_min / self.planned_min


@dataclass(frozen=True)
class Band:
    kind: Kind
    n: int
    lo: float
    mid: float
    hi: float

    def json(self) -> dict[str, float | int | str]:
        return {"kind": self.kind, "n": self.n, "lo": self.lo, "mid": self.mid, "hi": self.hi}


def _minutes(a: datetime, b: datetime) -> int:
    return round((b - a).total_seconds() / 60)


def sane(planned_min: int, actual_min: int) -> bool:
    """A sample is their spread only within RATIO_MIN..RATIO_MAX of the plan (else Pisa matched the wrong
    stay)."""
    return planned_min > 0 and actual_min > 0 and RATIO_MIN <= actual_min / planned_min <= RATIO_MAX


def _meets(stay: Stay, start: datetime, end: datetime) -> bool:
    return (
        stay.departed is not None and stay.arrived <= end + MEET_SLACK and stay.departed >= start - MEET_SLACK
    )


def task_actual(
    ref: str,
    start: datetime,
    end: datetime,
    place_id: str | None,
    done_at: datetime | None,
    stays: Sequence[Stay],
) -> Sample | None:
    """A planned task against what happened: its place's stay, else its done time from the planned start."""
    planned = _minutes(start, end)
    if planned <= 0:
        return None
    if place_id is not None:
        met = [s for s in stays if s.place_id == place_id and _meets(s, start, end)]
        if met:
            stay = min(met, key=lambda s: abs((s.arrived - start).total_seconds()))
            assert stay.departed is not None
            actual = _minutes(stay.arrived, stay.departed)
            return Sample("task", ref, planned, actual, "stay") if sane(planned, actual) else None
        return None
    if done_at is None or done_at <= start:
        return None
    actual = _minutes(start, done_at)
    return Sample("task", ref, planned, actual, "done") if sane(planned, actual) else None


def event_actual(
    ref: str, start: datetime, end: datetime, place_id: str | None, stays: Sequence[Stay]
) -> Sample | None:
    """An event at a place against their stay there: from the later of their arrival and its start, to their
    leaving."""
    planned = _minutes(start, end)
    if planned <= 0 or place_id is None:
        return None
    met = [
        s for s in stays
        if s.place_id == place_id and s.departed is not None and s.arrived < end and s.departed > start
    ]  # fmt: skip
    if not met:
        return None
    stay = min(met, key=lambda s: abs((s.arrived - start).total_seconds()))
    assert stay.departed is not None
    actual = _minutes(max(stay.arrived, start), stay.departed)
    return Sample("event", ref, planned, actual, "stay") if sane(planned, actual) else None


def travel_actual(
    ref: str, leave_at: datetime, minutes: int, to_place_id: str | None, stays: Sequence[Stay]
) -> Sample | None:
    """A trip against their stays: from leaving the stay before it to arriving at its place."""
    if minutes <= 0 or to_place_id is None:
        return None
    latest = leave_at + timedelta(minutes=minutes * RATIO_MAX) + TRIP_LEAVE_SLACK
    arrivals = [
        s for s in stays if s.place_id == to_place_id and leave_at - TRIP_LEAVE_SLACK <= s.arrived <= latest
    ]
    if not arrivals:
        return None
    arrival = min(arrivals, key=lambda s: s.arrived)
    before = [
        s for s in stays
        if s.departed is not None and s.departed <= arrival.arrived and s.place_id != to_place_id
    ]  # fmt: skip
    if not before:
        return None
    left = max(before, key=lambda s: s.departed or s.arrived)
    assert left.departed is not None
    if abs(left.departed - leave_at) > TRIP_LEAVE_SLACK:
        return None
    actual = _minutes(left.departed, arrival.arrived)
    return Sample("travel", ref, minutes, actual, "stay") if sane(minutes, actual) else None


def _quantile(values: Sequence[float], q: float) -> float:
    """The q-th quantile, linear between the closest ranks (numpy's default)."""
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def band(kind: Kind, ratios: Sequence[float]) -> Band | None:
    """Their spread for a kind: the 20th, 50th and 80th percentile of actual ÷ planned; none below
    MIN_SAMPLES."""
    kept = [r for r in ratios if RATIO_MIN <= r <= RATIO_MAX]
    if len(kept) < MIN_SAMPLES:
        return None
    lo, mid, hi = (round(_quantile(kept, q), 3) for q in (Q_LO, 0.5, Q_HI))
    return Band(kind, len(kept), lo, mid, hi)


def factor(b: Band | None) -> float:
    """The planner's multiplier: their median, never under half or over twice the plan's own number."""
    return 1.0 if b is None else min(FACTOR_MAX, max(FACTOR_MIN, b.mid))


def estimate(minutes: int, b: Band | None) -> int:
    """A duration planned at their median (the plan's own minutes with no band)."""
    return max(1, round(minutes * factor(b)))


def range_of(minutes: int, b: Band | None) -> tuple[int, int] | None:
    """The band around a duration Pisa planned at their median: (low, high) minutes, or None with no band."""
    if b is None:
        return None
    base = minutes / factor(b)
    return max(1, round(base * b.lo)), max(1, round(base * b.hi))
