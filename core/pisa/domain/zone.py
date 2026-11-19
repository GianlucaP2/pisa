"""The user's place in the world, from the phone: Pisa follows its zone, never a fixed one.

`settings.timezone` (PISA_TIMEZONE) is the default only: before any phone said where it is, and for the user's
people at home. What the phone said (`PhoneContext`) is held by the runtime's Owner (`domain/owner.py`): the
api sets it from the phone's `hello` / `device_context` (and stores it), every process reloads it from the
database. Here: the rules on zones and times.

No I/O here: the services load and store it."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pisa.domain.holidays import holiday

HourCycle = Literal["h23", "h12"]


@dataclass(frozen=True)
class PhoneContext:
    """What the primary phone said last: its zone, region and hour cycle."""

    tz: str
    region: str | None = None
    hour_cycle: HourCycle | None = None


def loadable(tz: str) -> bool:
    """A zone Pisa's tz database knows (a typo, or a zone newer than Pisa's database, keeps the last
    known)."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def clock_text(when: datetime | time, cycle: HourCycle) -> str:
    """A time as the user reads it: 15:05 (h23) or 3:05 PM (h12)."""
    hour, minute = when.hour, when.minute
    if cycle == "h23":
        return f"{hour:02d}:{minute:02d}"
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12}:{minute:02d} {suffix}"


NIGHT_FROM, NIGHT_UNTIL = 22, 7


def their_time(now: datetime, owner: str, home: str, cycle: HourCycle) -> str | None:
    """The user is away and writes to someone at home in the middle of their night: their time, to say
    ("da loro sono le 23:00"). None when they are home or it's daytime there."""
    if owner == home:
        return None
    local = now.astimezone(ZoneInfo(home))
    if NIGHT_UNTIL <= local.hour < NIGHT_FROM:
        return None
    return clock_text(local, cycle)


def day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """The user's day in their zone now: [00:00, next 00:00) as aware instants (DST days are 23 or 25 h)."""
    zone = ZoneInfo(tz)
    return datetime.combine(day, time.min, tzinfo=zone), datetime.combine(
        day + timedelta(days=1), time.min, zone
    )


def now_line(now: datetime, tz: str, region_code: str | None, cycle: HourCycle) -> str:
    """What `get_current_time` says: the weekday, the date, the time as they read it, the zone; their
    country's holiday when it is one ("Saturday 2026-11-01 10:00 Europe/Rome · holiday in IT: Ognissanti")."""
    local = now.astimezone(ZoneInfo(tz))
    line = f"{local:%A %Y-%m-%d} {clock_text(local, cycle)} {tz}"
    if region_code and (name := holiday(local.date(), region_code)) is not None:
        line += f" · holiday in {region_code.upper()}: {name}"
    return line
