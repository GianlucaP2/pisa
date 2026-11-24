"""When something periodic is due, on the user's clock: their zone now, so a phone that moves keeps their
local schedule (Sunday 18:00 is Sunday 18:00 where they are).

A `Period` is every minute, daily at a time, weekly on a weekday at a time, or monthly (on the 1st) at a
time. Its key is the day it started (ISO), so keys sort in time: a key at or before the last one recorded was
already run (a flight west that brings back yesterday never runs it twice). `open_for` bounds how long after
its start a missed run (a worker down, a stop) may still start; past it the period waits for the next.

The worker's scheduler (`services/step_runs.py`) asks a step's period whether it is due and records the key;
the services keep only the work. Pure: no I/O."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

# date.weekday(): the user's week ends on Sunday (the card, the weekly check, the poster, il consiglio)
SUNDAY = 6

Kind = Literal["minute", "daily", "weekly", "monthly"]


def on_sunday(day: date) -> bool:
    return day.weekday() == SUNDAY


@dataclass(frozen=True)
class Period:
    kind: Kind = "minute"
    at: time = time(0, 0)  # daily, weekly, monthly: its local start time
    weekday: int = SUNDAY  # weekly: its day (date.weekday())
    open_for: timedelta | None = None  # how long after its start a run may start; None: the whole period
    retry: bool = True  # a failed run is tried again in its period; False: spent once it starts

    @property
    def every_minute(self) -> bool:
        return self.kind == "minute"

    def start(self, local: datetime) -> datetime:
        """The latest start at or before `local` (wall clock, no zone): every minute, its minute."""
        wall = local.replace(tzinfo=None)
        if self.kind == "minute":
            return wall.replace(second=0, microsecond=0)
        if self.kind == "daily":
            start = datetime.combine(wall.date(), self.at)
            return start if start <= wall else start - timedelta(days=1)
        if self.kind == "weekly":
            day = wall.date() - timedelta(days=(wall.weekday() - self.weekday) % 7)
            start = datetime.combine(day, self.at)
            return start if start <= wall else start - timedelta(days=7)
        start = datetime.combine(wall.date().replace(day=1), self.at)
        if start <= wall:
            return start
        return datetime.combine((start.date() - timedelta(days=1)).replace(day=1), self.at)

    def due(self, local: datetime) -> str | None:
        """The key of the period `local` is in (the day it started, ISO; every minute, the minute) while a run
        may start in it, else None."""
        start = self.start(local)
        if self.open_for is not None and local.replace(tzinfo=None) - start >= self.open_for:
            return None
        return start.isoformat(timespec="minutes") if self.every_minute else start.date().isoformat()

    def ran(self, key: str, last: str | None) -> bool:
        """Whether the period `key` was already run, `last` being the last key recorded."""
        return last is not None and last >= key


MINUTE = Period()


def daily(at: time, *, open_for: timedelta | None = None, retry: bool = True) -> Period:
    return Period("daily", at, open_for=open_for, retry=retry)


def weekly(weekday: int, at: time, *, open_for: timedelta | None = None, retry: bool = True) -> Period:
    return Period("weekly", at, weekday, open_for=open_for, retry=retry)


def monthly(at: time, *, open_for: timedelta | None = None, retry: bool = True) -> Period:
    return Period("monthly", at, open_for=open_for, retry=retry)


# The worker's periodic steps (`services/worker_steps.py`).
# Sunday from 18:00, the week with Pisa; on Monday still the week just ended (a stop over Sunday evening,
# or a worker down then, never skips the card), never from Tuesday.
SUNDAY_CARD = weekly(SUNDAY, time(18, 0), open_for=timedelta(hours=30))
# Sunday 18:00 to Monday 18:00, the weekly check (the clocks never change in between).
WEEKLY_CHECK = weekly(SUNDAY, time(18, 0), open_for=timedelta(days=1))
# The night's learning: from 03:30 to 09:00 (a worker down at 03:30 still learns that morning; never in
# their day or evening). Spent once it starts: a failing night is logged once, never retried every minute.
NIGHT_LEARNING = daily(time(3, 30), open_for=timedelta(hours=5, minutes=30), retry=False)
