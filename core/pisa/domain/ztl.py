"""Skill: "posso entrare nella ZTL adesso?" — Rome's limited-traffic zones as a reviewed static
table (the hours aren't machine-readable anywhere: research §3).

SOURCE: Roma Servizi per la Mobilità, «ZTL in Centro» (https://romamobilita.it/muoversi-a-roma/ztl-in-centro/,
"Aggiornato il: 28/07/2026"), read and encoded on 3 Nov 2026; the zones' gates and borders are RSM's
open data (CC BY 4.0). Pisa says the zone is active or not for a car without a permit and names the source and
its date; it never says they may enter (permits, CUDE, electric cars, motorbikes are theirs to know). Re-check
the page when RSM updates it. No I/O."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from pisa.domain.holidays import holiday

# source: https://romamobilita.it/muoversi-a-roma/ztl-in-centro/ (reviewed 2026-11-03)
SOURCE_DATE = "28/07/2026"


@dataclass(frozen=True)
class Window:
    days: frozenset[int]  # weekday the window starts on (Monday 0)
    start: time
    end: time  # past midnight when earlier than start (23:00-03:00)
    holidays: bool  # active on a public holiday too ("anche se festivi") or not ("esclusi i festivi")
    months: frozenset[int] = frozenset(range(1, 13))  # the months it runs in (the start day's)


@dataclass(frozen=True)
class Zone:
    key: str
    name: str
    windows: tuple[Window, ...]


_WEEK = frozenset({0, 1, 2, 3, 4})
_NO_AUGUST = frozenset(range(1, 13)) - {8}
_MAY_OCT = frozenset({5, 6, 7, 8, 9, 10}) - {8}  # "da maggio a ottobre", August excepted

ZONES: tuple[Zone, ...] = (
    Zone("centro_diurna", "ZTL Centro Storico (diurna)", (
        Window(_WEEK, time(6, 30), time(18, 0), holidays=False),
        Window(frozenset({5}), time(14, 0), time(18, 0), holidays=False),
    )),
    Zone("centro_notturna", "ZTL Centro Storico (notturna)", (
        Window(frozenset({4, 5}), time(23, 0), time(3, 0), holidays=False, months=_NO_AUGUST),
    )),
    Zone("tridente", "ZTL Tridente (A1)", (
        Window(_WEEK, time(6, 30), time(19, 0), holidays=False),
        Window(frozenset({5}), time(10, 0), time(19, 0), holidays=False),
    )),
    Zone("trastevere_diurna", "ZTL Trastevere (diurna)", (
        Window(frozenset({0, 1, 2, 3, 4, 5}), time(6, 30), time(10, 0), holidays=False),
    )),
    Zone("trastevere_notturna", "ZTL Trastevere (notturna)", (
        Window(frozenset({4, 5}), time(21, 30), time(3, 0), holidays=True, months=_NO_AUGUST),
        Window(frozenset({2, 3}), time(21, 30), time(3, 0), holidays=True, months=_MAY_OCT),
    )),
    Zone("san_lorenzo", "ZTL San Lorenzo (notturna)", (
        Window(frozenset({4, 5}), time(21, 30), time(3, 0), holidays=True, months=_NO_AUGUST),
        Window(frozenset({2, 3}), time(21, 30), time(3, 0), holidays=True, months=_MAY_OCT),
    )),
    Zone("testaccio", "ZTL Testaccio (notturna)", (
        Window(frozenset({4, 5}), time(23, 0), time(3, 0), holidays=False, months=_NO_AUGUST),
    )),
)  # fmt: skip
_WORDS = {
    "centro_diurna": ("centro", "storico", "centro storico"), "centro_notturna": ("centro", "storico"),
    "tridente": ("tridente", "a1", "spagna", "popolo", "corso"), "trastevere_diurna": ("trastevere",),
    "trastevere_notturna": ("trastevere",), "san_lorenzo": ("san lorenzo", "lorenzo"),
    "testaccio": ("testaccio",),
}  # fmt: skip


def _window_at(w: Window, moment: datetime) -> tuple[datetime, datetime] | None:
    """The window's span containing `moment` (local, naive-compatible), if any: today's, or yesterday's
    running past midnight."""
    for back in (0, 1):
        day: date = moment.date() - timedelta(days=back)
        if day.weekday() not in w.days or day.month not in w.months:
            continue
        if not w.holidays and holiday(day, "IT") is not None:
            continue
        start = datetime.combine(day, w.start, moment.tzinfo)
        end = datetime.combine(day + timedelta(days=1 if w.end <= w.start else 0), w.end, moment.tzinfo)
        if start <= moment < end:
            return start, end
    return None


def status(zone: Zone, moment: datetime) -> tuple[bool, datetime | None]:
    """(active now, until when) for a car without a permit; or (False, the next start within 7 days)."""
    for w in zone.windows:
        span = _window_at(w, moment)
        if span is not None:
            return True, span[1]
    starts: list[datetime] = []
    for w in zone.windows:
        for ahead in range(8):
            day = moment.date() + timedelta(days=ahead)
            if day.weekday() not in w.days or day.month not in w.months:
                continue
            if not w.holidays and holiday(day, "IT") is not None:
                continue
            start = datetime.combine(day, w.start, moment.tzinfo)
            if start > moment:
                starts.append(start)
                break
    return False, min(starts) if starts else None


def named(words: str) -> list[Zone]:
    """The zones their words name ("Trastevere", "il centro"); none named: all of them."""
    text = " ".join(words.casefold().split())
    found = [z for z in ZONES if any(w in text for w in _WORDS[z.key])]
    return found or list(ZONES)


def lines(zones: list[Zone], moment: datetime) -> list[str]:
    """One line per zone, for the model (English): active or not, until or from when."""
    out: list[str] = []
    for zone in zones:
        on, when = status(zone, moment)
        if on:
            out.append(f"{zone.name}: ACTIVE now (no entry without a permit) until {when:%a %H:%M}")
        elif when is not None:
            out.append(f"{zone.name}: not active now; next active from {when:%a %H:%M}")
        else:
            out.append(f"{zone.name}: not active now")
    return out
