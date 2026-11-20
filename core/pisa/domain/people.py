"""The people the user loves, as Pisa may know them. Pure rules: reading a date or a relation in
the user's own words, a city's time zone, a person's local hour, a yearly date's next day, the rhythm of their
contact with someone, a wish in a person's email (kept as data), and Pisa's own lines.

Pisa knows about others only what they'd expect it to know; it speaks to them only openly,
as Pisa, and nothing here contacts anyone. The user's zone comes from Pisa's settings; the user-zone
helpers replace `owner_tz` arguments when they are merged.
"""

import itertools
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pisa.domain.lexicon.dates import day_or_28, day_or_none
from pisa.domain.lexicon.months import EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS, IT_MONTHS_SHORT, month_numbers
from pisa.domain.profile import SENSITIVE_STEMS, plain, stems

DateKind = Literal["birthday", "anniversary", "name_day", "other"]
Shares = Literal["location", "calendar_busy", "nothing"]
DEFAULT_REMIND = (7, 1)  # a week before (the pool task appears), the day before (it is due)
REMIND_MAX_DAYS = 60
NAME_MAX = 80
GUESTS_MAX = 8  # a bigger event's guest list makes no one a person
NOTE_MAX = 300
CITY_MAX = 80
RELATION_MAX = 40
CARE_MAX = 20
ORBIT_MIN_DAYS = 30  # never an orbit line for someone heard from within a month
ORBIT_FACTOR = 2.0  # … nor within twice their usual rhythm with them
RHYTHM_MIN_CONTACTS = 4  # a rhythm needs this many contact days
TITLE_MAX = 40  # an event's title in Pisa's line (it may come from someone's invite)
WISH_HOUR = 9  # a wish is proposed for their 9:00
EARLY_HOUR = 8  # before this, where they are, it is too early to send

MONTH_NUMBERS = month_numbers(IT_MONTHS, EN_MONTHS, IT_MONTHS_SHORT, EN_MONTHS_SHORT, extra={"sept": 9})
_MONTH = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))
_DAY = r"(?:([12]?\d|3[01])|(primo|first))(?:°|º|st|nd|rd|th)?"
# "12 marzo", "il 12 di marzo", "1° maggio", "primo maggio", "12 March 1960"
_DAY_MONTH = re.compile(
    rf"\b{_DAY}\s+(?:di\s+|of\s+)?({_MONTH})\b\.?(?:\s+(?:del\s+)?((?:19|20)\d\d))?", re.I
)
# "March 12", "March 12th, 1960"
_MONTH_DAY = re.compile(rf"\b({_MONTH})\s+{_DAY}\b(?:,?\s+((?:19|20)\d\d))?", re.I)
# "12/3", "12.03", "12-3-1960" (day first in Italian; month first in English)
_NUMERIC = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-]((?:19|20)\d\d))?\b")

_KINDS: tuple[tuple[DateKind, tuple[str, ...]], ...] = (
    ("name_day", ("onomastic", "name day", "nameday")),
    ("anniversary", ("anniversar",)),
    ("birthday", ("compleann", "compie", "nato", "nata", "nati", "birthday", "born", "bday")),
)

# The user's word for someone close → (the relation kept, the name Pisa gives them in Italian, in English)
RELATIONS: dict[str, tuple[str, str, str]] = {
    "madre": ("madre", "Mamma", "Mom"),
    "mamma": ("madre", "Mamma", "Mom"),
    "mother": ("madre", "Mamma", "Mom"),
    "mom": ("madre", "Mamma", "Mom"),
    "mum": ("madre", "Mamma", "Mom"),
    "padre": ("padre", "Papà", "Dad"),
    "papa": ("padre", "Papà", "Dad"),
    "father": ("padre", "Papà", "Dad"),
    "dad": ("padre", "Papà", "Dad"),
    "moglie": ("moglie", "Moglie", "Wife"),
    "wife": ("moglie", "Moglie", "Wife"),
    "marito": ("marito", "Marito", "Husband"),
    "husband": ("marito", "Marito", "Husband"),
    "fidanzata": ("fidanzata", "Fidanzata", "Girlfriend"),
    "girlfriend": ("fidanzata", "Fidanzata", "Girlfriend"),
    "fidanzato": ("fidanzato", "Fidanzato", "Boyfriend"),
    "boyfriend": ("fidanzato", "Fidanzato", "Boyfriend"),
    "nonna": ("nonna", "Nonna", "Grandma"),
    "grandma": ("nonna", "Nonna", "Grandma"),
    "grandmother": ("nonna", "Nonna", "Grandma"),
    "nonno": ("nonno", "Nonno", "Grandpa"),
    "grandpa": ("nonno", "Nonno", "Grandpa"),
    "grandfather": ("nonno", "Nonno", "Grandpa"),
    "sorella": ("sorella", "Sorella", "Sister"),
    "sister": ("sorella", "Sorella", "Sister"),
    "fratello": ("fratello", "Fratello", "Brother"),
    "brother": ("fratello", "Fratello", "Brother"),
    "figlia": ("figlia", "Figlia", "Daughter"),
    "daughter": ("figlia", "Figlia", "Daughter"),
    "figlio": ("figlio", "Figlio", "Son"),
    "son": ("figlio", "Figlio", "Son"),
    "zia": ("zia", "Zia", "Aunt"),
    "aunt": ("zia", "Zia", "Aunt"),
    "zio": ("zio", "Zio", "Uncle"),
    "uncle": ("zio", "Zio", "Uncle"),
    "suocera": ("suocera", "Suocera", "Mother-in-law"),
    "suocero": ("suocero", "Suocero", "Father-in-law"),
}
_POSSESSIVE = frozenset(
    {"mia", "mio", "miei", "mie", "my", "la", "il", "lo", "di", "a", "per", "the", "to", "for"}
)

# A city the user names → its zone (no geocoding: an unknown city leaves the zone unknown, and Pisa asks)
CITY_TZ: dict[str, str] = {
    "new york": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York",
    "washington": "America/New_York", "miami": "America/New_York", "philadelphia": "America/New_York",
    "atlanta": "America/New_York", "toronto": "America/Toronto", "montreal": "America/Toronto",
    "chicago": "America/Chicago", "houston": "America/Chicago", "dallas": "America/Chicago",
    "austin": "America/Chicago", "denver": "America/Denver", "phoenix": "America/Phoenix",
    "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "san diego": "America/Los_Angeles", "vancouver": "America/Vancouver",
    "las vegas": "America/Los_Angeles", "mexico city": "America/Mexico_City",
    "citta del messico": "America/Mexico_City", "sao paulo": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires", "rio de janeiro": "America/Sao_Paulo",
    "londra": "Europe/London", "london": "Europe/London", "dublino": "Europe/Dublin",
    "dublin": "Europe/Dublin", "lisbona": "Europe/Lisbon", "lisbon": "Europe/Lisbon",
    "parigi": "Europe/Paris", "paris": "Europe/Paris", "madrid": "Europe/Madrid",
    "barcellona": "Europe/Madrid", "barcelona": "Europe/Madrid", "berlino": "Europe/Berlin",
    "berlin": "Europe/Berlin", "monaco di baviera": "Europe/Berlin", "munich": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam", "bruxelles": "Europe/Brussels", "brussels": "Europe/Brussels",
    "zurigo": "Europe/Zurich", "zurich": "Europe/Zurich", "ginevra": "Europe/Zurich",
    "geneva": "Europe/Zurich", "vienna": "Europe/Vienna", "praga": "Europe/Prague",
    "prague": "Europe/Prague", "atene": "Europe/Athens", "athens": "Europe/Athens",
    "istanbul": "Europe/Istanbul", "mosca": "Europe/Moscow", "moscow": "Europe/Moscow",
    "roma": "Europe/Rome", "rome": "Europe/Rome", "milano": "Europe/Rome", "milan": "Europe/Rome",
    "napoli": "Europe/Rome", "torino": "Europe/Rome", "firenze": "Europe/Rome", "bologna": "Europe/Rome",
    "palermo": "Europe/Rome", "venezia": "Europe/Rome", "genova": "Europe/Rome", "bari": "Europe/Rome",
    "dubai": "Asia/Dubai", "tel aviv": "Asia/Jerusalem", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "bangkok": "Asia/Bangkok", "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai", "pechino": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "tokyo": "Asia/Tokyo", "seoul": "Asia/Seoul", "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland", "il cairo": "Africa/Cairo",
    "cairo": "Africa/Cairo", "johannesburg": "Africa/Johannesburg", "nairobi": "Africa/Nairobi",
}  # fmt: skip

# A person's own wish, in the first person, in an email to the user ("mi piacerebbe tanto…", "I'd love…")
_WISH = re.compile(
    r"\b(mi piacerebbe|mi piacerebbero|vorrei tanto|vorrei proprio|quanto vorrei|sogno di|desidero tanto|"
    r"i'?d (?:really |so )?love|i would (?:really |so )?love|i wish i had|i really want)\b",
    re.I,
)
# RAIL: a sentence about money, a payment or Pisa itself is never a gift idea (a "send 200 euro for the
# birthday" email stays an email)
_NOT_A_GIFT = re.compile(
    r"(€|\$|£|\b(?:euro|eur|usd|dollar\w*|soldi|denaro|contanti|bonific\w*|iban|paypal|satispay|revolut|"
    r"pagament\w*|pagar\w*|pagh\w*|ricaric\w*|money|cash|wire|transfer\w*|pay|payment|venmo|bitcoin|crypto|"
    r"gift ?card|buono regalo|codice|password|link|https?|www)\b|\bJ\b)",
    re.I,
)
SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")

# A calendar event that is a meal or a party with someone: a thank-you the morning after
_THANKS_WORDS = re.compile(
    r"\b(cena|pranzo|aperitivo|apericena|brunch|colazione|festa|compleanno|matrimonio|grigliata|"
    r"dinner|lunch|drinks|party|wedding|bbq|barbecue)\b",
    re.I,
)
# A yearly all-day event named for a date: "Compleanno di Marco", "Marco's birthday", "Anniversario con Anna"
_CAL_BIRTHDAY = (
    re.compile(r"^(?:il\s+)?compleanno\s+di\s+(.+?)$", re.I),
    re.compile(r"^(.+?)\s+compie\s+(?:gli\s+)?anni$", re.I),
    re.compile(r"^(.+?)['\u2019]s\s+birthday$", re.I),
    re.compile(r"^birthday\s*[:\-\u2013]\s*(.+?)$", re.I),  # "Birthday: X", never "Birthday party at…"
)
_CAL_ANNIVERSARY = (
    re.compile(r"^anniversario\s+(?:di\s+matrimonio\s+)?(?:di|con)\s+(.+?)$", re.I),
    re.compile(r"^(.+?)['\u2019]s\s+anniversary$", re.I),
)


@dataclass(frozen=True)
class SaidDate:
    month: int
    day: int
    year: int | None

    @property
    def on(self) -> str:
        return (
            f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
            if self.year
            else f"{self.month:02d}-{self.day:02d}"
        )


def _valid(month: int, day: int, year: int | None) -> bool:
    return day_or_none(year or 2024, month, day) is not None  # 2024: a leap year, so 29 February stands


def said_date(words: str, lang: str) -> SaidDate | None:
    """The day and month (and the year, when said) in the user's own words; None when they hold none, or two
    different ones (Pisa asks, never guesses)."""
    found: list[SaidDate] = []
    for match in _DAY_MONTH.finditer(words):
        day = 1 if match.group(2) else int(match.group(1))
        year = int(match.group(4)) if match.group(4) else None
        found.append(SaidDate(MONTH_NUMBERS[match.group(3).casefold()], day, year))
    for match in _MONTH_DAY.finditer(words):
        day = 1 if match.group(3) else int(match.group(2))
        year = int(match.group(4)) if match.group(4) else None
        found.append(SaidDate(MONTH_NUMBERS[match.group(1).casefold()], day, year))
    if not found:
        for match in _NUMERIC.finditer(words):
            a, b = int(match.group(1)), int(match.group(2))
            day, month = (b, a) if lang == "en-US" else (a, b)
            found.append(SaidDate(month, day, int(match.group(3)) if match.group(3) else None))
    good = {d for d in found if _valid(d.month, d.day, d.year)}
    if len({(d.month, d.day) for d in good}) != 1 or len(good) != len(found):
        return None
    return max(good, key=lambda d: d.year or 0)


def parse_on(text: str) -> SaidDate | None:
    """The API's `MM-DD` or `YYYY-MM-DD`."""
    m = re.fullmatch(r"(?:(\d{4})-)?(\d{2})-(\d{2})", text.strip())
    if not m:
        return None
    year = int(m.group(1)) if m.group(1) else None
    month, day = int(m.group(2)), int(m.group(3))
    if year is not None and not 1900 <= year <= 2100:
        return None
    return SaidDate(month, day, year) if _valid(month, day, year) else None


def date_kind(words: str) -> DateKind:
    """What kind of date the user's words name (a cue word), else `other`: never guessed from the model."""
    text = plain(words)
    for kind, cues in _KINDS:
        if any(f" {cue}" in f" {text}" for cue in cues):
            return kind
    return "other"


def relation_of(person: str) -> tuple[str, str, str] | None:
    """(relation, Italian name, English name) when the user's words for the person are a relation ("mia
    madre", "my sister")."""
    words = [w for w in plain(person).split() if w not in _POSSESSIVE]
    if len(words) == 1 and words[0] in RELATIONS:
        return RELATIONS[words[0]]
    return None


def person_key(person: str) -> str:
    """The user's words for a person, without "mia", "di", "a": what a name is matched against."""
    return " ".join(w for w in plain(person).split() if w not in _POSSESSIVE)


def names(query: str, name: str, relation: str | None) -> bool:
    """The user's words name this person: the relation ("mamma" for a madre), the whole name, or one of its
    words as a whole word ("Laura" for "Laura Bianchi")."""
    key = person_key(query)
    if not key:
        return False
    rel = relation_of(query)
    if rel is not None:
        return relation == rel[0]
    full = plain(name)
    return key == full or (len(key) >= 3 and key in full.split())


def in_said(person: str, owner_said: str) -> bool:
    """The person, as the tool names them, is in the user's own words (a name, or a relation word)."""
    key = person_key(person)
    said = f" {plain(owner_said)} "
    if not key:
        return False
    rel = relation_of(person)
    if rel is not None:
        return any(f" {word} " in said for word, value in RELATIONS.items() if value[0] == rel[0])
    return all(f" {w} " in said for w in key.split())


def sensitive(text: str) -> bool:
    """Health, diet, belief or identity (the list): Pisa keeps nothing of the kind about other people."""
    return any(w.startswith(stem) for w in plain(text).split() for stem in SENSITIVE_STEMS)


def grounded_idea(idea: str, owner_said: str) -> bool:
    """A gift idea or a note stands on the user's words: its content words are in what they said."""
    want = stems(idea)
    return bool(want) and len(want & stems(owner_said)) * 2 >= len(want)


def zone(name: str | None) -> ZoneInfo | None:
    if not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def tz_for_city(city: str) -> str | None:
    return CITY_TZ.get(" ".join(plain(city).split()))


def zone_said(tz: str, owner_said: str) -> bool:
    """A zone the model gives stands only when its city is in their words ("America/Los_Angeles" needs
    "Los Angeles")."""
    place = plain(tz.rsplit("/", 1)[-1].replace("_", " "))
    return bool(place) and f" {place} " in f" {plain(owner_said)} "


_STOP = ("non", "mai", "never", "smetti", "basta", "stop", "dont", "don")
_EVERYONE = ("nessun", "tutti", "nobody", "anyone", "anybody", "everyone", "people", "persone", "chi")


def says_stop(owner_said: str) -> bool:
    """Their words say to stop ("non ricordarmi più…", "basta", "stop", "don't")."""
    return any(w in _STOP for w in plain(owner_said).replace("don t", "dont").split())


def says_everyone(owner_said: str) -> bool:
    """Their words say it's for everyone ("non dirmi più chi non sento", "nessuno")."""
    return any(w.startswith(cue) for w in plain(owner_said).split() for cue in _EVERYONE)


def _looks_named(name: str) -> bool:
    words = name.split()
    return 1 <= len(words) <= 4 and all(w[:1].isupper() and w.replace("'", "").isalpha() for w in words)


def next_day(month: int, day: int, today: date) -> date:
    """The date's next day from today (today included); 29 February is the 28th in other years."""
    for year in (today.year, today.year + 1):
        at = day_or_28(year, month, day)
        if at >= today:
            return at
    raise AssertionError("unreachable")


def remind_days(values: list[int] | tuple[int, ...] | None) -> tuple[int, ...]:
    days = sorted({int(v) for v in values or () if 0 <= int(v) <= REMIND_MAX_DAYS}, reverse=True)
    return tuple(days) or DEFAULT_REMIND


# a wish that asks something of them (a call, a mail, feedback, a contract) is work, never a gift; a
# number or an address is someone's data; the wish needs a thing after it, never "che…", "if…", "to get…"
_ASKS = re.compile(
    r"\b(chiam\w*|telefon\w*|mand\w*|invi\w*|inoltr\w*|scriv\w*|rispond\w*|firm\w*|feedback|riunion\w*|"
    r"meeting|contratt\w*|contract|call|phone|send|forward\w*|email|mail|reply|sign\w*|review|deck|slides?|"
    r"assistant|assistente|ufficio|office|progett\w*|project|report|preventiv\w*|quote|fattur\w*|invoice)\b",
    re.I,
)
_WISH_THING = re.compile(
    r"\s+(?!(?:(?:tanto|molto|davvero|proprio)\s+)?"
    r"(?:che|se|if|it|that|to (?:get|hear|have you|see you)|you|tu|ti|vi)\b)\S+\s+\S+",
    re.I,
)


def wishes(body: str) -> list[str]:
    """The sentences of a person's email that say a wish of theirs in the first person, never one about money,
    a payment, a link or Pisa (an email is data), never one that asks something of them or holds a number
    or an address; each at most NOTE_MAX characters."""
    found: list[str] = []
    for sentence in SENTENCE.findall(body):
        text = " ".join(sentence.split())
        wish = _WISH.search(text)
        if (
            wish is None
            or _NOT_A_GIFT.search(text)
            or _ASKS.search(text)
            or re.search(r"\d|@", text)
            or not _WISH_THING.match(text, wish.end())
            or not 12 <= len(text) <= NOTE_MAX
        ):
            continue
        found.append(text)
    return found[:3]


def calendar_date(title: str) -> tuple[DateKind, str] | None:
    """A yearly all-day event's (kind, person's name) when its title names a birthday or an anniversary."""
    text = " ".join(title.split()).strip(" .!🎂🎉")
    for kind, patterns in (("birthday", _CAL_BIRTHDAY), ("anniversary", _CAL_ANNIVERSARY)):
        for pattern in patterns:
            if (m := pattern.match(text)) is not None:
                name = m.group(1).strip(" -\u2013:")
                if not _looks_named(name):  # a person's name, never "festa al bar"
                    continue
                if 1 <= len(name) <= NAME_MAX and plain(name):
                    return kind, name  # type: ignore[return-value]
    return None


def thanks_event(title: str) -> bool:
    return _THANKS_WORDS.search(title) is not None


def rhythm_days(contacts: list[datetime]) -> float | None:
    """Their usual gap with someone: the median of the gaps between their contact days, with enough of
    them."""
    days = sorted({c.date() for c in contacts})
    if len(days) < RHYTHM_MIN_CONTACTS:
        return None
    return float(median((b - a).days for a, b in itertools.pairwise(days)))


def orbit_gap(last: datetime | None, rhythm: float | None, now: datetime) -> float | None:
    """How far past their usual rhythm someone is (the ratio), or None when it's not an orbit yet."""
    if last is None or rhythm is None or rhythm <= 0:
        return None
    gap = (now - last).total_seconds() / 86400
    if gap < ORBIT_MIN_DAYS or gap < ORBIT_FACTOR * rhythm:
        return None
    return gap / rhythm


def since_words(days: float, lang: str) -> str:
    months = int(days // 30)
    if lang == "en-US":
        return f"{months} months" if months >= 2 else f"{int(days // 7)} weeks"
    return f"{months} mesi" if months >= 2 else f"{int(days // 7)} settimane"


@dataclass(frozen=True)
class TheirHour:
    """Where a person is, now and when Pisa proposes a wish there (their 9:00 on the day, computed when
    shown): `later` (send at `send_at`), `now` (it is their day, past 9), `over` (their day is already
    over)."""

    city: str | None
    their_now: datetime
    send_at: datetime | None
    state: Literal["later", "now", "over"]
    owner_now: datetime

    def _where(self, lang: str) -> str:
        if self.city:
            return f"in {self.city}" if lang == "en-US" else f"a {self.city}"
        return "where they are" if lang == "en-US" else "da loro"  # never a zone's name

    def line(self, lang: str) -> str:
        en, where, now_h = lang == "en-US", self._where(lang), hour_text(self.their_now)
        if self.state == "over":
            return (
                f"{where} the day is already over: write now?"
                if en
                else (f"{where} la giornata è già passata: gli scrivi subito?")
            )
        if self.state == "now" or self.send_at is None:
            return (
                f"{where} it is {now_h}: write now?" if en else f"{where} sono le {now_h}: gli scrivi adesso?"
            )
        mine = his_side(self.send_at, self.owner_now, lang)
        if en:
            return f"{where} it is {now_h}: send it at {hour_text(self.send_at)} their time ({mine} yours)?"
        return f"{where} sono le {now_h}: glielo mandi alle {hour_text(self.send_at)} loro ({mine} tue)?"


def hour_text(at: datetime) -> str:
    """ "9", "15:30": an hour as people say it."""
    return f"{at.hour}" if at.minute == 0 else f"{at.hour}:{at.minute:02d}"


def his_side(send_at: datetime, owner_now: datetime, lang: str) -> str:
    """The time to send, on their clock, with its day: "le 14", "stasera alle 21", "domani alle 17"."""
    mine = send_at.astimezone(owner_now.tzinfo)
    days = (mine.date() - owner_now.date()).days
    en = lang == "en-US"
    if days == 0:
        if mine.hour >= 18:
            return f"tonight at {hour_text(mine)}" if en else f"stasera alle {hour_text(mine)}"
        return f"at {hour_text(mine)}" if en else f"le {hour_text(mine)}"
    if days == 1:
        return f"tomorrow at {hour_text(mine)}" if en else f"domani alle {hour_text(mine)}"
    return f"{mine:%a} at {hour_text(mine)}" if en else f"il {mine.day} alle {hour_text(mine)}"


def nearest_day(month: int, day: int, around: date) -> date:
    """The date's occurrence nearest to `around` (last year's, this year's or next year's)."""
    options = []
    for year in (around.year - 1, around.year, around.year + 1):
        options.append(day_or_28(year, month, day))
    return min(options, key=lambda d: abs((d - around).days))


def their_hour(
    tz: str | None, city: str | None, owner_tz: str, now: datetime, on: tuple[int, int] | None = None
) -> TheirHour | None:
    """A person's local hour, when their zone differs from the user's (`owner_tz`: the zone now). With
    `on` (the date's month and day), the 9:00 is on THEIR day: still ahead there (a day early on their
    side, or later today), now (their day, past 9), or over. None when the zone is unknown or the same."""
    theirs, mine = zone(tz), zone(owner_tz)
    if theirs is None or mine is None:
        return None
    their_now, owner_now = now.astimezone(theirs), now.astimezone(mine)
    if their_now.utcoffset() == owner_now.utcoffset():
        return None
    if on is None:
        send = datetime.combine(their_now.date(), time(WISH_HOUR), tzinfo=theirs)
        if send >= their_now:
            return TheirHour(city, their_now, send, "later", owner_now)
        if their_now.hour < 21:
            return TheirHour(city, their_now, None, "now", owner_now)
        return TheirHour(city, their_now, send + timedelta(days=1), "later", owner_now)
    day = nearest_day(on[0], on[1], their_now.date())
    if day < their_now.date():
        return TheirHour(city, their_now, None, "over", owner_now)
    send = datetime.combine(day, time(WISH_HOUR), tzinfo=theirs)
    if send > their_now:
        return TheirHour(city, their_now, send, "later", owner_now)
    return TheirHour(city, their_now, None, "now", owner_now)


# --- Pisa's own lines (the user's language) ----------------------------------------------------------------

_DATE_HEADS = {
    "birthday": ("Oggi compie gli anni {name}", "It's {name}'s birthday today"),
    "anniversary": ("Oggi è l'anniversario di {name}", "It's {name}'s anniversary today"),
    "name_day": ("Oggi è l'onomastico di {name}", "It's {name}'s name day today"),
    "other": ("Oggi è una data importante per {name}", "Today is a date that matters for {name}"),
}
_DATE_ASK = (": gli scrivi?", ": will you write?")
_WISH_DRAFTS = {
    "birthday": ("Tanti auguri, {first}! Buon compleanno!", "Happy birthday, {first}! Thinking of you."),
    "anniversary": ("Buon anniversario, {first}!", "Happy anniversary, {first}!"),
    "name_day": ("Buon onomastico, {first}!", "Happy name day, {first}!"),
    "other": ("Ti penso, {first}!", "Thinking of you, {first}!"),
}  # fmt: skip


def first_name(name: str) -> str:
    return name.split()[0] if name.split() else name


def date_line(kind: DateKind, name: str, lang: str, hour: TheirHour | None) -> str:
    """ "Oggi compie gli anni Marco: gli scrivi?"; with their hour when it differs: "…: a New York sono le 3:
    glielo mandi alle 9 loro (le 15 tue)?"."""
    en = lang == "en-US"
    head = _DATE_HEADS[kind][1 if en else 0].format(name=first_name(name))
    return head + (f": {hour.line(lang)}" if hour is not None else _DATE_ASK[1 if en else 0])


def wish_draft(kind: DateKind, name: str, lang: str) -> str:
    it, en = _WISH_DRAFTS[kind]
    return (en if lang == "en-US" else it).format(first=first_name(name))


def orbit_line(name: str, days: float, lang: str) -> str:
    since = since_words(days, lang)
    first = first_name(name)
    return (
        f"It's been {since} since you heard from {first}."
        if lang == "en-US"
        else f"Non senti {first} da {since}."
    )


def thanks_line(name: str, title: str, lang: str) -> tuple[str, str]:
    """(the morning line, the draft they may send themselves). "Ieri cena da Marco: gli mandi un grazie?"."""
    first = first_name(name)
    title = " ".join(re.sub(r"[^\w\s'\u2019]", " ", title).split())[:TITLE_MAX]  # an invite's title, short
    what = title[:1].lower() + title[1:] if title[:2] != title[:2].upper() else title
    with_whom = (
        ""
        if first.casefold() in title.casefold()
        else (f" with {first}" if lang == "en-US" else f" con {first}")
    )
    if lang == "en-US":
        return (f"Yesterday: {what}{with_whom}. Send a thank-you? I have a draft.",
                f"Thanks for yesterday, {first}! It was a lovely time.")  # fmt: skip
    return (f"Ieri {what}{with_whom}: gli mandi un grazie? Ho una bozza.",
            f"Grazie per ieri, {first}! È stato bellissimo.")  # fmt: skip


# their words answer one of Pisa's offers of the day ("sì,
# mandagli il grazie su WhatsApp", "mandale gli auguri", "send them the thank-you"): a thank-you or a wish, or
# a pronoun with a send verb, so the brain is told which offer and draft it was
# a send verb with its pronoun, or a yes with the thank-you or the wish; never a bare "grazie"
_ANSWERS_OFFER = re.compile(
    r"\b(?:manda(?:gli|le|glielo|gliela|glieli)|invia(?:gli|le|glielo|gliela)|scrivi(?:gli|le))\b|"
    r"\bsend\s+(?:him|her|them)\b|"
    r"^\W*(?:s[iì]|yes)\b.*\b(?:grazie|ringraziament\w*|auguri|thank[\s-]*you|thanks|wishes)\b",
    re.IGNORECASE,
)


def answers_offer(words: str) -> bool:
    return bool(_ANSWERS_OFFER.search(words or ""))
