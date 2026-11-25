"""Reminder wording (no model): fixed templates in Italian and English.

The minutes are counted from the moment the reminder goes out, so a reminder sent late (the catch-up after a
restart, or an event added a few minutes before its reminder time) still says the truth.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain.lexicon.travel import MODE_WORDS
from pisa.domain.tasks import day_named
from pisa.domain.text import fold

Kind = Literal["event", "task", "leave"]


def minutes_until(start: datetime, now: datetime) -> int:
    return max(0, round((start - now).total_seconds() / 60))


def reminder_text(kind: Kind, title: str, place: str | None, minutes: int, lang: str) -> str:
    """ "Tra 15 minuti: Dentista (Via Roma 1)." / "In 15 minutes: Dentist (Via Roma 1).\""""
    where = f" ({place})" if place else ""
    it = lang == "it-IT"
    if minutes == 0:
        if kind == "event":
            lead = "Ora" if it else "Now"
        else:
            lead = "Scade ora" if it else "Due now"
    elif it:
        unit = "minuto" if minutes == 1 else "minuti"
        lead = f"Tra {minutes} {unit}" if kind == "event" else f"Scade tra {minutes} {unit}"
    else:
        unit = "minute" if minutes == 1 else "minutes"
        lead = f"In {minutes} {unit}" if kind == "event" else f"Due in {minutes} {unit}"
    return f"{lead}: {title}{where}."


def reminder_summary(kind: Kind, title: str, start: datetime, lang: str, timezone: str) -> str:
    """The form a held reminder takes in the quiet-mode summary, read hours later: "Dentista alle 9:00",
    "Pagare la bolletta entro le 18:00" / "Dentist at 9:00", "Pay the bill by 18:00"."""
    local = start.astimezone(ZoneInfo(timezone))
    at = f"{local.hour}:{local.minute:02d}"
    if lang == "it-IT":
        return f"{title} alle {at}" if kind == "event" else f"{title} entro le {at}"
    return f"{title} at {at}" if kind == "event" else f"{title} by {at}"


def leave_text(title: str, minutes: int, mode: str, lang: str) -> str:
    """The leave-now reminder: "Esci ora: 18 minuti con i mezzi per Dentista." / "Leave now: 18 min by
    public transport to Dentist."""
    how = MODE_WORDS["it-IT" if lang == "it-IT" else "en-US"][mode]
    if lang == "it-IT":
        return f"Esci ora: {minutes} {'minuto' if minutes == 1 else 'minuti'} {how} per {title}."
    return f"Leave now: {minutes} min {how} to {title}."


def leave_summary(title: str, leave_at: datetime, lang: str, timezone: str) -> str:
    """The quiet-mode summary form: "Partenza per Dentista alle 8:42" / "Leave for Dentist at 8:42"."""
    local = leave_at.astimezone(ZoneInfo(timezone))
    at = f"{local.hour}:{local.minute:02d}"
    return f"Partenza per {title} alle {at}" if lang == "it-IT" else f"Leave for {title} at {at}"


# --- a timed reminder's moment (`remember_to`'s `when`) ---------------------------------------------------

_AMOUNT = r"(\d{1,4}|un|uno|una|un'|a|an|one)"
_UNIT = r"(minut[oi]|minutes?|mins?|m|or[ae]|hours?|hrs?|h)"
# "tra 2 minuti", "fra un'ora", "in 10 minutes", "10 min", "+5m"
_RELATIVE = re.compile(rf"^(?:tra|fra|in|\+)?\s*{_AMOUNT}\s*{_UNIT}$")
_HALF_HOUR = re.compile(r"^(?:tra|fra|in)?\s*(?:mezz'?\s?ora|mezza\s+ora|half\s+an\s+hour)$")
_QUARTER = re.compile(
    r"^(?:tra|fra|in)?\s*(?:un\s+quarto\s+d'?\s?ora|a\s+quarter\s+of\s+an\s+hour|a\s+quarter\s+hour)$"
)
# "domani alle 9", "oggi alle 18:30", "alle 9", "at 9am", "lunedì alle 9.15"
_AT_CLOCK = re.compile(r"(?:\balle\b|\ball'|\bore\b|\bat\b)\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?")
RELATIVE_MAX = timedelta(days=30)


def _relative(plain: str) -> timedelta | None:
    if _HALF_HOUR.fullmatch(plain):
        return timedelta(minutes=30)
    if _QUARTER.fullmatch(plain):
        return timedelta(minutes=15)
    found = _RELATIVE.fullmatch(plain)
    if found is None:
        return None
    amount = int(found.group(1)) if found.group(1).isdigit() else 1
    unit = found.group(2)
    delta = timedelta(hours=amount) if unit.startswith(("or", "h")) else timedelta(minutes=amount)
    return delta if timedelta(0) < delta <= RELATIVE_MAX else None


def fire_time(when: str | None, now: datetime, tz: ZoneInfo) -> datetime | None:
    """A timed reminder's moment from `when` (None: no moment it can name, or one already past):
    - a delay from now, "tra 2 minuti" / "fra un'ora" / "in 10 minutes" / "mezz'ora": now plus it, at the
      start of its minute (asked at 12:26:40, it fires at the 12:28 check, the minute it was promised for);
    - a day and a clock time, "domani alle 9", "oggi alle 18:30", "lunedì alle 9.15";
    - a clock time alone, "alle 9", "18:30": its next occurrence (today, or tomorrow when it has passed);
    - an ISO date-time (local when it has no zone)."""
    if not when or not when.strip():
        return None
    local = now.astimezone(tz)
    plain = " ".join(fold(when.replace("\u2019", "'")).split())
    if (delta := _relative(plain)) is not None:
        return (now + delta).astimezone(tz).replace(second=0, microsecond=0)
    if re.fullmatch(r"\d{1,2}[:.]\d{2}", plain):
        plain = f"alle {plain}"
    clock = _AT_CLOCK.search(plain)
    if clock is not None:
        hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
        if clock.group(3) == "pm" and hour < 12:
            hour += 12
        elif clock.group(3) == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        day = day_named(plain[: clock.start()] + " " + plain[clock.end() :], local.date())
        moment = datetime(
            (day or local.date()).year,
            (day or local.date()).month,
            (day or local.date()).day,
            hour,
            minute,
            tzinfo=tz,
        )
        if day is None and moment <= now:
            moment += timedelta(days=1)  # "alle 9" said at 10: tomorrow's
        return moment if moment > now else None
    try:
        moment = datetime.fromisoformat(when.strip())
    except ValueError:
        return None
    moment = moment if moment.tzinfo else moment.replace(tzinfo=tz)
    return moment if moment > now else None


# --- the moments a reply names (the reminder-time rail, security review TK-2) -----------------------------
#
# The rail checks that "te lo ricordo alle X" names the moment `remember_to` stored. The reply's time words
# are read with `fire_time`'s grammar (`_AT_CLOCK`, `_relative`, `day_named`), plus the spoken forms a reply
# uses: hours in words ("alle nove", "at nine"), "e mezza" / "e un quarto" / "meno un quarto", "half past",
# "mezzogiorno" / "noon", "di sera" / "pm".

_NUMBER_WORDS = {
    "una": 1, "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8,
    "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "tredici": 13, "quattordici": 14, "quindici": 15,
    "sedici": 16, "diciassette": 17, "diciotto": 18, "diciannove": 19, "venti": 20, "ventuno": 21,
    "ventidue": 22, "ventitre": 23, "venticinque": 25, "trenta": 30, "trentacinque": 35, "quaranta": 40,
    "quarantacinque": 45, "cinquanta": 50, "cinquantacinque": 55,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "fifty": 50,
}  # fmt: skip
_NUMBER = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
# a number word where a clock or a delay takes a number: after its preposition, or "e"/"meno" after an hour
_WORD_AFTER = re.compile(rf"\b(alle|ore|at|tra|fra|in|e|meno|past|to)\s+({_NUMBER})\b")
_NAMED_HOURS = (
    (re.compile(r"\b(?:a\s+|at\s+)?(?:mezzogiorno|noon|midday)\b"), "alle 12:00 "),
    (re.compile(r"\b(?:a\s+|at\s+)?(?:mezzanotte|midnight)\b"), "alle 0:00 "),
    (re.compile(r"\ball'\s?una\b"), "alle 1"),
)
# "alle 9", "alle 9:30", "at 9am", "alle 9 e mezza", "alle 9 meno un quarto", "alle 9 di sera"
_SAID_CLOCK = re.compile(
    r"(?:\balle\b|\ball'|\bore\b|\bat\b)\s*(?:ore\s+)?(\d{1,2})(?:[:.](\d{2}))?"
    r"(?:\s*([ap])\.?\s?m\b\.?)?"
    r"(?:\s+e\s+(mezza|mezzo|un\s+quarto|3\s+quarti|\d{1,2})\b|\s+meno\s+(un\s+quarto|\d{1,2})\b)?"
    r"(?:\s+(di\s+mattina|del\s+mattino|di\s+notte|di\s+sera|del\s+pomeriggio|in\s+the\s+morning"
    r"|in\s+the\s+afternoon|in\s+the\s+evening|at\s+night)\b)?"
)
_HALF_PAST = re.compile(r"\b(?:at\s+)?(half|a\s+quarter|quarter|\d{1,2})\s+(past|to)\s+(\d{1,2})\b")
_BARE_CLOCK = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_DELAY = re.compile(
    r"\b(?:tra|fra|in)\s+(?:\d{1,4}|un'|un|uno|una|a|an|one)\s*(?:minut[oi]|minutes?|mins?|or[ae]|hours?|hrs?)\b"
    r"|\b(?:tra|fra|in)\s+(?:mezz'?\s?ora|mezza\s+ora|half\s+an\s+hour|un\s+quarto\s+d'?\s?ora"
    r"|a\s+quarter\s+of\s+an\s+hour|a\s+quarter\s+hour)\b"
)
_CLAUSE = re.compile(r"[,;:!?]|\.(?!\d)")
_PM = ("p", "di sera", "del pomeriggio", "in the afternoon", "in the evening", "at night")
_AM = ("a", "di mattina", "del mattino", "in the morning", "di notte")
_MINUTES = {"mezza": 30, "mezzo": 30, "un quarto": 15, "3 quarti": 45}  # "tre" is a number word by then


@dataclass(frozen=True)
class SaidMoment:
    """A moment a reply names: a clock time (`hours` the hour may be: "alle 9" is 9 or 21, "alle 9 di sera"
    21 only; with `day` when the words name one), or a delay resolved to its minute (`at`)."""

    hours: frozenset[int] = frozenset()
    minute: int = 0
    day: date | None = None
    at: datetime | None = None


def _number_words(plain: str) -> str:
    for named, clock in _NAMED_HOURS:
        plain = named.sub(clock, plain)
    return _WORD_AFTER.sub(lambda m: f"{m.group(1)} {_NUMBER_WORDS[m.group(2)]}", plain)


def _hours(hour: int, meridiem: str | None) -> frozenset[int]:
    if meridiem in _PM:
        return frozenset({hour + 12 if hour < 12 else hour})
    if meridiem in _AM:
        return frozenset({0 if hour == 12 and meridiem == "a" else hour})
    return frozenset({hour, hour + 12}) if 0 < hour < 12 else frozenset({hour})


def _day_around(plain: str, start: int, end: int, today: date | None) -> date | None:
    """The day the words of the clock's own clause name ("domani alle 9", "alle 9 di lunedì"), if any."""
    if today is None:
        return None
    before = _CLAUSE.split(plain[:start])[-1]
    after = _CLAUSE.split(plain[end:])[0]
    return day_named(f"{before} {after}", today)


def said_moments(sentence: str, now: datetime | None = None, tz: ZoneInfo | None = None) -> list[SaidMoment]:
    """Every moment the sentence names, read as `fire_time` reads `when`. Without `now` (and `tz`) a delay
    names no moment and a day isn't read."""
    plain = _number_words(" ".join(fold(sentence.replace("\u2019", "'")).split()))
    local = now.astimezone(tz) if now is not None and tz is not None else None
    today = local.date() if local is not None else None
    found: list[SaidMoment] = []
    taken: list[tuple[int, int]] = []
    for m in _SAID_CLOCK.finditer(plain):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if m.group(4) is not None:
            minute = _MINUTES.get(" ".join(m.group(4).split()), None) or int(m.group(4))
        elif m.group(5) is not None:
            back = 15 if not m.group(5).isdigit() else int(m.group(5))
            hour, minute = (hour - 1) % 24, 60 - back
        meridiem = m.group(3) or (" ".join(m.group(6).split()) if m.group(6) else None)
        if hour > 23 or not 0 <= minute <= 59:
            continue
        taken.append(m.span())
        day = _day_around(plain, *m.span(), today)
        found.append(SaidMoment(_hours(hour, meridiem), minute, day))
    for m in _HALF_PAST.finditer(plain):
        if any(s <= m.start() < e for s, e in taken):
            continue
        hour = int(m.group(3))
        what = m.group(1)
        minute = 30 if what == "half" else 15 if "quarter" in what else int(what)
        if m.group(2) == "to":
            hour, minute = (hour - 1) % 24, 60 - minute
        if hour > 23 or not 0 <= minute <= 59:
            continue
        taken.append(m.span())
        found.append(SaidMoment(_hours(hour, None), minute, _day_around(plain, *m.span(), today)))
    for m in _BARE_CLOCK.finditer(plain):
        if any(s <= m.start() < e for s, e in taken):
            continue
        found.append(SaidMoment(_hours(int(m.group(1)), None), int(m.group(2)),
                                _day_around(plain, *m.span(), today)))  # fmt: skip
    if now is not None and tz is not None:
        for m in _DELAY.finditer(plain):
            delta = _relative(m.group(0))
            if delta is not None:
                found.append(SaidMoment(at=(now + delta).astimezone(tz).replace(second=0, microsecond=0)))
    return found
