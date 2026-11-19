"""Places: opening hours, distances, location-text keys, map links. Pure rules, no I/O.

Opening hours are a week of periods ("Monday 12:00-15:00"), possibly crossing midnight ("Friday 19:00-02:00"),
plus dated periods for the coming days when a provider gives them (holidays, special hours). "Open now" and
the next change are computed in the user's time zone on real dates, so a DST night is 23 or 25 hours long.
"""

import datetime as dt
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from pisa.domain import locale
from pisa.domain.text import fold_words

Mode = Literal["drive", "transit", "walk", "bike"]
MODES: tuple[Mode, ...] = ("drive", "transit", "walk", "bike")
SAME_PLACE_M = 50  # two results closer than this with the same name are one place


@dataclass(frozen=True)
class Period:
    """Open from `open` to `close` (HH:MM, local). `day` is 1 = Monday … 7 = Sunday. A `date` makes it a
    one-off period for that day (special hours): a day with dated periods ignores the weekly ones. `close`
    None = open around the clock from `open`; a close at or before the open time is on the next day."""

    day: int
    open: str
    close: str | None
    date: dt.date | None = None


def _hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def _closing(day: date, opens: datetime, close: str | None, tz: ZoneInfo) -> datetime:
    if close is None:
        return opens + timedelta(days=7)
    if close == "24:00":
        return datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    closes = datetime.combine(day, _hhmm(close), tzinfo=tz)
    return closes if closes > opens else datetime.combine(day + timedelta(days=1), _hhmm(close), tzinfo=tz)


def _intervals(
    periods: list[Period], start_day: date, days: int, tz: ZoneInfo, closed: frozenset[date]
) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    dated = {p.date for p in periods if p.date is not None}
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        if day in closed:
            continue
        todays = [p for p in periods if p.date == day] if day in dated else [
            p for p in periods if p.date is None and p.day == day.isoweekday()
        ]  # fmt: skip
        for p in todays:
            opens = datetime.combine(day, _hhmm(p.open), tzinfo=tz)
            # Compared in UTC: aware datetimes sharing one tzinfo compare by wall clock, wrong on DST nights.
            out.append((opens.astimezone(UTC), _closing(day, opens, p.close, tz).astimezone(UTC)))
    out.sort()
    merged: list[tuple[datetime, datetime]] = []
    for opens, closes in out:  # back-to-back periods (23:00-24:00 then 00:00-02:00) are one opening
        if merged and opens <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], closes))
        else:
            merged.append((opens, closes))
    return merged


def open_state(
    periods: list[Period], now: datetime, timezone: str, closed: frozenset[date] = frozenset()
) -> tuple[bool | None, datetime | None]:
    """(open now, the next opening or closing). (None, None) when the hours are unknown; a place open around
    the clock has no next change. `closed`: special days without any opening (a holiday)."""
    if not periods:
        return None, None
    if any(p.close is None for p in periods):
        return True, None  # open around the clock (a period without a close)
    tz = ZoneInfo(timezone)
    now = now.astimezone(UTC)
    intervals = _intervals(periods, now.astimezone(tz).date() - timedelta(days=1), 9, tz, closed)
    for opens, closes in intervals:
        if opens <= now < closes:
            return True, closes.astimezone(tz)
        if opens > now:
            return False, opens.astimezone(tz)
    return False, None  # closed for the whole coming week


def closes_between(
    periods: list[Period],
    start: datetime,
    end: datetime,
    timezone: str,
    closed: frozenset[date] = frozenset(),
) -> datetime | None:
    """When the place closes for the last time during [start, end): the latest moment to be there. None when
    it is still open at `end`, has no opening in between, or its hours are unknown."""
    if not periods or any(p.close is None for p in periods):
        return None
    tz = ZoneInfo(timezone)
    lo, hi = start.astimezone(UTC), end.astimezone(UTC)
    days = (hi - lo).days + 3
    overlapping = [
        (o, c) for o, c in _intervals(periods, lo.astimezone(tz).date() - timedelta(days=1), days, tz, closed)
        if o < hi and c > lo
    ]  # fmt: skip
    if not overlapping or overlapping[-1][1] >= hi:
        return None
    return overlapping[-1][1].astimezone(tz)


def open_windows(
    periods: list[Period],
    start: datetime,
    end: datetime,
    timezone: str,
    closed: frozenset[date] = frozenset(),
) -> list[tuple[datetime, datetime]] | None:
    """The opening times within [start, end), clipped, in the user's zone. None when the hours are unknown
    (any time will do); [] when it is closed all that time."""
    if not periods:
        return None
    if any(p.close is None for p in periods):
        return [(start, end)]
    tz = ZoneInfo(timezone)
    lo, hi = start.astimezone(UTC), end.astimezone(UTC)
    days = (hi - lo).days + 3
    out = []
    for opens, closes in _intervals(periods, lo.astimezone(tz).date() - timedelta(days=1), days, tz, closed):
        a, b = max(opens, lo), min(closes, hi)
        if a < b:
            out.append((a.astimezone(tz), b.astimezone(tz)))
    return out


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> int:
    """Great-circle distance in metres (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 6_371_000 * math.asin(math.sqrt(h)))


def text_key(text: str) -> str:
    """Lowercase, no accents, no punctuation, single spaces: "Via Roma, 1 - Milano" → "via roma 1 milano"."""
    return fold_words(text)


def category_key(text: str | None) -> str | None:
    """Provider categories as snake case: Apple "Restaurant", "PublicTransport" → "restaurant",
    "public_transport"; Google's are already like that."""
    if not text:
        return None
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text).lower()


def map_links(name: str, lat: float | None, lon: float | None, google_place_id: str | None) -> dict[str, str]:
    q = quote(name)
    apple = f"https://maps.apple.com/?q={q}" + (f"&ll={lat},{lon}" if lat is not None else "")
    google = f"https://www.google.com/maps/search/?api=1&query={q}"
    if google_place_id:
        google += f"&query_place_id={quote(google_place_id)}"
    elif lat is not None:
        google = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    return {"apple_maps": apple, "google_maps": google}


# --- a task's place ------------------------------------------------------------------------------

PLACE_MATCH = 60  # token-set similarity (percent) a found place needs with the task's text: name or address
_DOMAIN = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|it|nl|eu|app|dev)\b)", re.IGNORECASE)


# Above this a walk is no plan (a briefing once offered a three-hour walk to the next town): transit instead.
WALK_MAX_MIN = 45


def physical_place(text: str | None, sender: str | None) -> str | None:
    """A task's place text from an email, or None when it isn't somewhere to go: a website or domain, or the
    sender's own organisation ("GitHub" in a mail from notifications@github.com is the company, not its
    Amsterdam office)."""
    if not text or not text.strip():
        return None
    if _DOMAIN.search(text):
        return None
    words = set(text_key(text).split())
    domain = sender.rpartition("@")[2] if sender and "@" in sender else ""
    labels = {w for part in domain.casefold().split(".") for w in text_key(part).split()}
    if words and words <= labels:
        return None
    return text.strip()


def matches_text(text: str, name: str | None, address: str | None) -> bool:
    """A found place is the one a task means only when its name or address matches the text (token set ≥
    60%): a search near Amsterdam for an Italian parcel point returns some shop that isn't it."""
    key = text_key(text)
    return any(
        fuzz.token_set_ratio(key, text_key(candidate)) >= PLACE_MATCH
        for candidate in (name, address)
        if candidate
    )


CITY_LANGS = locale.WIRE_TAGS  # a city's names are compared in both: "Roma" is "Rome"


def city_names_key(names: frozenset[str] | None) -> str | None:
    """A city's names (text keys) as one stored value: "roma|rome"."""
    return "|".join(sorted(names)) if names else None


def names_city(text: str, city: str | None) -> bool:
    """The text names this city (its words in order, as whole words)."""
    if not city:
        return False
    return f" {text_key(city)} " in f" {text_key(text)} " and bool(text_key(city))
