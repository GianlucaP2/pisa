# ruff: noqa: E501
"""Pure rules. No I/O.

- **Moments**: a person's OWN words that they are in a city on a day ("sarò a Roma giovedì", "I'll be in
  Rome on Thursday"), or free on a day ("giovedì sono libera"), read by code from their email to them (only
  their part, never a quoted reply) or from their own calendar shared with them. Never from a location: Pisa
  never infers a moment from where anyone is. The city is one of Pisa's known cities (`people.CITY_TZ`), so no
  word of the email reaches Pisa's line but a city Pisa knows and a day code computed.
- **Thanks for a gift**: the person's own email saying they sent them something ("ti ho spedito un
  pensierino"), or a store's notice naming them by their whole name ("Laura Bianchi ti ha inviato un regalo").
- **Their style**: their greeting, their sign-off and their exclamation marks, from their own sent emails to that
  person; a draft Pisa writes is in it.
- **Their morning**: the first moment from their 9:00 on their day that is outside their quiet hours and
  still before their 21:00.
- **Their clock**: their hour now and whether it is a decent hour to call (9:00-21:30 there; 10:00 on
  a weekend), and when it next is.

Every line and draft here is Pisa's own template in the user's language: Pisa prepares, the user taps.
"""

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain import people as rules
from pisa.domain.lexicon.dates import day_or_none
from pisa.domain.lexicon.weekdays import DAY_NAMES, WEEKDAY_NUMBERS
from pisa.domain.profile import plain

MomentKind = Literal["in_city", "free"]
CallVerdict = Literal["good", "early", "late"]

MOMENT_AHEAD_DAYS = 14  # a moment further away than this waits
MOMENT_EVERY = timedelta(days=7)  # at most one moment a week per person
FREE_FROM, FREE_UNTIL = time(10), time(21)  # their free hour for a coffee is inside this
FREE_MIN = timedelta(minutes=60)
OFFER_FROM, OFFER_UNTIL = time(9), time(20, 30)  # Pisa offers in their daytime only (never a push at night)
THANKS_AT = time(10)  # a thank-you is offered the morning after, from 10
THANKS_KEPT = timedelta(days=2)
WISH_LAST = time(21)  # their evening: past it, a wish waits for the briefing's line
CALL_FROM, CALL_FROM_WEEKEND, CALL_UNTIL = time(9), time(10), time(21, 30)
STEP = timedelta(minutes=15)
SIGN_MAX = 30

_RELATIVE = {"oggi": 0, "stasera": 0, "today": 0, "tonight": 0, "domani": 1, "tomorrow": 1, "dopodomani": 2}
_CITY_ALIASES = {  # the same city in their two languages: one key
    "rome": "roma", "milan": "milano", "london": "londra", "paris": "parigi", "lisbon": "lisbona",
    "dublin": "dublino", "barcelona": "barcellona", "berlin": "berlino", "munich": "monaco di baviera",
    "brussels": "bruxelles", "zurich": "zurigo", "geneva": "ginevra", "prague": "praga", "athens": "atene",
    "moscow": "mosca", "beijing": "pechino", "cairo": "il cairo", "nyc": "new york", "naples": "napoli",
    "turin": "torino", "florence": "firenze", "venice": "venezia", "genoa": "genova",
}  # fmt: skip
_CITY_SHOWN = {  # how Pisa says a city key, in Italian and in English
    "roma": ("Roma", "Rome"), "milano": ("Milano", "Milan"), "londra": ("Londra", "London"),
    "parigi": ("Parigi", "Paris"), "lisbona": ("Lisbona", "Lisbon"), "dublino": ("Dublino", "Dublin"),
    "barcellona": ("Barcellona", "Barcelona"), "berlino": ("Berlino", "Berlin"),
    "monaco di baviera": ("Monaco di Baviera", "Munich"), "bruxelles": ("Bruxelles", "Brussels"),
    "zurigo": ("Zurigo", "Zurich"), "ginevra": ("Ginevra", "Geneva"), "praga": ("Praga", "Prague"),
    "atene": ("Atene", "Athens"), "mosca": ("Mosca", "Moscow"), "pechino": ("Pechino", "Beijing"),
    "il cairo": ("Il Cairo", "Cairo"), "napoli": ("Napoli", "Naples"), "torino": ("Torino", "Turin"),
    "firenze": ("Firenze", "Florence"), "venezia": ("Venezia", "Venice"), "genova": ("Genova", "Genoa"),
}  # fmt: skip


def city_key(text: str | None) -> str | None:
    """A city Pisa knows, as one key in both languages ("Rome", "roma" → "roma"); None for any other words."""
    if not text:
        return None
    words = plain(text)
    if words in rules.CITY_TZ or words in _CITY_ALIASES:
        return _CITY_ALIASES.get(words, words)
    return None


def city_in(text: str | None) -> str | None:
    """The one known city a text names (a calendar title or a location: "Roma", "Rome, Italy"), else None.
    Two different cities: None (never guessed)."""
    if not text:
        return None
    words = f" {plain(text)} "
    found = {
        _CITY_ALIASES.get(k, k) for k in (*rules.CITY_TZ, *_CITY_ALIASES) if len(k) > 3 and f" {k} " in words
    }
    return found.pop() if len(found) == 1 else None


def city_shown(key: str, lang: str) -> str:
    shown = _CITY_SHOWN.get(key)
    if shown is not None:
        return shown[1 if lang == "en-US" else 0]
    return " ".join(w.capitalize() for w in key.split())


# --- what a person said about being in a city, or free, on a day --------------------------------------

_CITIES = "|".join(sorted({*rules.CITY_TZ, *_CITY_ALIASES}, key=len, reverse=True))
_DAY_WORDS = (
    r"(?:(?:il\s+|la\s+|next\s+|on\s+|this\s+)?(?:prossim[oa]\s+)?"
    r"(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)(?:\s+prossim[oa])?|oggi|stasera|domani|dopodomani|today|tonight|"
    r"tomorrow|(?:il\s+|dal\s+)?\d{1,2}(?:\s+(?:al\s+\d{1,2}\s+)?[a-z]+)?)"
)
# "sarò a Roma giovedì", "sono a Milano domani", "arrivo a Roma il 12 marzo", "I'll be in Rome on Thursday"
_IN_CITY = re.compile(
    rf"\b(?:sar[oò]|sono|star[oò]|arrivo|vengo|passo|torno|sar[oò]\s+di\s+passaggio|i'?ll\s+be|i\s+will\s+be|"
    rf"i'?m|i\s+am|i'?m\s+going\s+to\s+be|i'?m\s+coming|i'?ll\s+be\s+visiting|we'?ll\s+be|saremo|siamo)"
    rf"\s+(?:a|in|to|giù\s+a|su\s+a)\s+({_CITIES})\b",
    re.I,
)
# "giovedì sono libera", "sono libero domani sera", "ho la serata libera venerdì", "I'm free on Friday"
_FREE = re.compile(
    r"\b(?:(?:sono|sar[oò])\s+(liber[oaie])|ho\s+(?:la\s+serata|il\s+pomeriggio|la\s+giornata|la\s+sera)\s+"
    r"(liber[oa])|i'?m\s+(free)|i\s+am\s+(free)|i'?ll\s+be\s+(free)|i\s+will\s+be\s+(free))\b",
    re.I,
)
_NEGATED = re.compile(r"\b(?:non|not|n't|never|mai|purtroppo\s+non)\s*(?:\w+\s+){0,1}$", re.I)
_IF = re.compile(r"\b(?:se|if|magari|forse|maybe|perhaps|chissà|non so se)\b", re.I)
# The reply's quoted part, never the person's own words: "> …", "Il … ha scritto:", "On … wrote:"
_QUOTE_START = re.compile(
    r"^(?:>|-{2,}\s*(?:original|messaggio)|on .{3,80} wrote:|il .{3,80} ha scritto:|da:|from:)", re.I | re.M
)


def own_part(body: str) -> str:
    """The sender's own words: everything before the first quoted line of a reply."""
    found = _QUOTE_START.search(body)
    return body[: found.start()] if found else body


@dataclass(frozen=True)
class Moment:
    kind: MomentKind
    city: str | None  # a key of Pisa's cities (in_city); None when free
    first: date
    last: date
    word: str | None = None  # the person's own adjective for free ("libera"), for Pisa's line


def _day_of(text: str, sent: date, lang: str) -> tuple[date, date] | None:
    """The one day (or "dal 12 al 14 marzo" span) a sentence names, from the day it was written."""
    words = plain(text)
    spans: set[tuple[date, date]] = set()
    span = re.search(r"\bdal (\d{1,2}) al (\d{1,2}) ([a-z]+)", words)
    if span:
        month = rules.MONTH_NUMBERS.get(span.group(3))
        if month:
            a, b = (
                day_or_none(sent.year, month, int(span.group(1))),
                day_or_none(sent.year, month, int(span.group(2))),
            )
            if a is None or b is None:
                return None
            if b < sent:
                a, b = a.replace(year=a.year + 1), b.replace(year=b.year + 1)
            if a <= b:
                spans.add((a, b))
    if not spans:
        for word, offset in _RELATIVE.items():
            if re.search(rf"\b{word}\b", words):
                day = sent + timedelta(days=offset)
                spans.add((day, day))
        for word, weekday in WEEKDAY_NUMBERS.items():
            if re.search(rf"\b{word}\b", words):
                day = sent + timedelta(days=(weekday - sent.weekday()) % 7 or 7)  # the coming one
                spans.add((day, day))
        said = rules.said_date(text, lang)
        if said is not None:
            day = rules.next_day(said.month, said.day, sent)
            spans.add((day, day))
        elif (bare := re.search(r"\bil (\d{1,2})\b(?! (?:alle|ore|:))", words)) and not spans:
            n = int(bare.group(1))
            for months in range(0, 2):
                y, m = divmod(sent.month - 1 + months, 12)
                day = day_or_none(sent.year + y, m + 1, n)
                if day is None:
                    continue
                if day >= sent:
                    spans.add((day, day))
                    break
    if len(spans) != 1:
        return None  # none, or two days: never guessed
    return spans.pop()


def moments(body: str, sent: date, lang: str) -> list[Moment]:
    """The moments a person's own email states, sentence by sentence: one city or "free", one day. A
    question, a negation, a "maybe" or a sentence naming two days is nothing."""
    out: list[Moment] = []
    for sentence in rules.SENTENCE.findall(own_part(body)):
        text = sentence.strip()
        if not text or text.endswith("?"):
            continue
        city = _IN_CITY.search(text)
        free = _FREE.search(text)
        if city is None and free is None:
            continue
        match = city or free
        assert match is not None
        if _NEGATED.search(text[: match.start()]) or _IF.search(text[: match.start()]):
            continue  # "non sarò", "se riesco sarò": nothing ("se vuoi" after it is fine)
        days = _day_of(text, sent, lang)
        if days is None:
            continue
        if city is not None:
            key = city_key(city.group(1))
            if key is not None:
                out.append(Moment("in_city", key, days[0], days[1]))
        elif free is not None:
            word = next(g for g in free.groups() if g)
            out.append(Moment("free", None, days[0], days[1], word.lower()))
    return out


def calendar_moment(title: str, location: str | None, first: date, last: date) -> Moment | None:
    """An event in the person's own calendar (shared with them) naming one city Pisa knows: they are there."""
    key = city_in(title) or city_in(location)
    if key is None:
        return None
    return Moment("in_city", key, first, last)


def free_window(
    busy: Sequence[tuple[datetime, datetime]], day: date, tz: str, *, now: datetime | None = None
) -> bool:
    """Whether their day holds a free hour for a coffee (10:00-21:00, an hour with nothing booked)."""
    zone = ZoneInfo(tz)
    start = datetime.combine(day, FREE_FROM, tzinfo=zone)
    end = datetime.combine(day, FREE_UNTIL, tzinfo=zone)
    if now is not None:
        start = max(start, now)
    cursor = start
    for s, e in sorted(busy):
        if e <= cursor or s >= end:
            continue
        if s - cursor >= FREE_MIN:
            return True
        cursor = max(cursor, e)
    return end - cursor >= FREE_MIN


def when_words(day: date, today: date, lang: str) -> str:
    """ "domani", "giovedì", "giovedì 19": the day as they say it."""
    en = lang == "en-US"
    if day == today:
        return "today" if en else "oggi"
    if day == today + timedelta(days=1):
        return "tomorrow" if en else "domani"
    names = DAY_NAMES["en-US" if en else "it-IT"]
    name = names[day.weekday()]
    if (day - today).days < 7:
        return f"on {name}" if en else name
    return f"on {name} {day.day}" if en else f"{name} {day.day}"


def moment_line(name: str, moment: Moment, day: date, today: date, lang: str) -> str:
    """ "Laura è a Roma giovedì, ti va un caffè?" (the question; they answer with the button or their words)."""
    first, when = rules.first_name(name), when_words(day, today, lang)
    if moment.kind == "in_city" and moment.city is not None:
        city = city_shown(moment.city, lang)
        if lang == "en-US":
            return f"{first} is in {city} {when}: fancy a coffee?"
        return f"{first} è a {city} {when}, ti va un caffè?"
    if lang == "en-US":
        return f"{first} said they're free {when}: fancy a coffee?"
    return f"{first} {when} è {moment.word or 'libera'}, ti va un caffè?"


# --- a gift received, and their style ---------------------------------------------------------------------

# The sender's own words: they sent them something ("ti ho spedito un pensierino", "I sent you a little gift")
_SENT_GIFT = re.compile(
    r"\b(?:ti\s+(?:ho|abbiamo)\s+(?:spedito|mandato|inviato|lasciato|fatto\s+arrivare|preso)\s+(?:un|una|il|"
    r"la|qualcosa|un\s+piccolo|un\s+pensier)\w*|ti\s+(?:è|e')\s+arrivat[oa]\s+(?:il|la)\s+(?:mi[oa]|nostr[oa])"
    r"|ti\s+(?:è|e')\s+arrivat[oa]|che\s+ti\s+(?:ho|abbiamo)\s+(?:spedito|mandato|inviato)"
    r"|(?:i|we)\s+(?:sent|mailed|got)\s+you\s+(?:a|an|something|a\s+little))\b",
    re.I,
)
_GIFT_WORD = re.compile(
    r"\b(regal\w*|pensier\w*|pacco|pacchetto|sorpresa|fiori|gift|present|package|parcel|surprise|flowers|"
    r"something)\b",
    re.I,
)
# A store's notice: "Laura Bianchi ti ha inviato un regalo", "A gift from Laura Bianchi", "… sent you a gift"
_STORE_GIFT = (
    re.compile(
        r"(.{3,60}?)\s+ti\s+ha\s+(?:inviato|mandato|regalato|spedito)\s+(?:un|una)\s+(?:regalo|buono)", re.I
    ),
    re.compile(r"\b(?:a\s+)?gift\s+from\s+([^\n.!,]{3,60})", re.I),
    re.compile(r"\bregalo\s+da\s+(?:parte\s+di\s+)?([^\n.!,]{3,60})", re.I),
    re.compile(r"(.{3,60}?)\s+(?:sent|has\s+sent)\s+you\s+a\s+gift", re.I),
)
# FACT: stores and florists whose notices say who sent a gift (their own domain: a stranger's address is no
# store; review the list as stores change)
GIFT_STORES = frozenset({
    "amazon.it", "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.es",
    "zalando.it", "interflora.it", "flowers.com", "1800flowers.com", "smartbox.com", "boxpark.it",
    "regalaunsorriso.it", "uncommongoods.com", "giftcards.apple.com",
})  # fmt: skip


def gift_from_sender(body: str) -> bool:
    """The person's own email says they sent them a gift (their words, never a quoted reply)."""
    for sentence in rules.SENTENCE.findall(own_part(body)):
        text = sentence.strip()
        found = _SENT_GIFT.search(text)
        if found and _GIFT_WORD.search(text) and not _NEGATED.search(text[: found.start()]):
            return True
    return False


def store_domain(address: str) -> bool:
    """The store's own notice domain, exactly: never a subdomain (a buyer-seller relay
    such as marketplace.amazon.it carries a stranger's words)."""
    return address.rsplit("@", 1)[-1].lower() in GIFT_STORES


def gift_giver(subject: str, body: str, names: Sequence[str]) -> str | None:
    """The one known person (by their WHOLE name) a store's gift notice names, else None."""
    text = f"{subject}\n{own_part(body)}"
    found: set[str] = set()
    for pattern in _STORE_GIFT:
        for match in pattern.finditer(text):
            said = plain(match.group(1))
            for name in names:
                key = plain(name)
                if len(key.split()) >= 2 and f" {key} " in f" {said} ":
                    found.add(name)
    return found.pop() if len(found) == 1 else None


@dataclass(frozen=True)
class Style:
    """How they write to someone, from their own sent emails to them."""

    greeting: str  # "Ciao", "Caro", "Hey"
    sign: str | None  # their sign-off line ("Un abbraccio, R."), when they use one
    bang: bool  # they write with exclamation marks
    seen: int  # the emails it comes from


_GREETINGS = ("ciao", "caro", "cara", "carissimo", "carissima", "ehi", "hey", "hi", "hello", "dear",
              "buongiorno", "salve", "ciaoo")  # fmt: skip
_DEFAULT = {"it-IT": Style("Ciao", None, True, 0), "en-US": Style("Hi", None, True, 0)}


def style(bodies: Sequence[str], lang: str) -> Style:
    """Their greeting (the first word of their emails, when it is one), their sign-off (a short last line they used
    at least twice, never an address, a link or a number) and whether they use "!"; Pisa's default otherwise."""
    default = _DEFAULT["en-US" if lang == "en-US" else "it-IT"]
    mine = [own_part(b).strip() for b in bodies if b and b.strip()]
    if not mine:
        return default
    greetings: Counter[str] = Counter()
    signs: Counter[str] = Counter()
    bangs = 0
    for text in mine:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        first = plain(lines[0]).split()[:1] if lines else []
        if first and first[0] in _GREETINGS:
            word = lines[0].split()[0].strip(",!.:")
            greetings[word[:1].upper() + word[1:].lower()] += 1
        if len(lines) >= 2:
            last = lines[-1]
            if len(last) <= SIGN_MAX and not re.search(r"[@\d]|https?:|www\.", last):
                signs[last] += 1
        bangs += "!" in text
    greeting = greetings.most_common(1)[0][0] if greetings else default.greeting
    # used twice at least (one email's last line may be a forwarded third party's)
    sign = next((s for s, n in signs.most_common() if n >= 2), None)
    return Style(greeting, sign, bangs * 2 >= len(mine), len(mine))


def _draft(style_: Style, first: str, body: str, lang: str) -> str:
    end = "!" if style_.bang else "."
    head = (
        f"{style_.greeting} {first}{end if style_.greeting.lower() not in ('caro', 'cara', 'dear') else ','}"
    )
    text = f"{head} {body.replace('!', end) if not style_.bang else body}"
    return f"{text}\n{style_.sign}" if style_.sign else text


def thanks_draft(style_: Style, name: str, what: Literal["event", "gift"], lang: str) -> str:
    """A short thank-you in their style: "Ciao Marco! Grazie ancora per ieri, è stato bellissimo!"."""
    first = rules.first_name(name)
    if lang == "en-US":
        body = (
            "Thanks again for yesterday, it was lovely!"
            if what == "event"
            else "Thank you so much for the gift!"
        )
    else:
        body = (
            "Grazie ancora per ieri, è stato bellissimo!"
            if what == "event"
            else "Grazie mille per il regalo!"
        )
    return _draft(style_, first, body, lang)


def thanks_gift_line(name: str, lang: str) -> str:
    first = rules.first_name(name)
    if lang == "en-US":
        return f"{first} sent you a gift: send a thank-you? I have a draft."
    return f"{first} ti ha mandato un regalo: gli mandi un grazie? Ho una bozza."


def moment_draft(style_: Style, name: str, moment: Moment, day: date, today: date, lang: str) -> str:
    first, when = rules.first_name(name), when_words(day, today, lang)
    if lang == "en-US":
        body = (
            f"I hear you're in {city_shown(moment.city, lang)} {when}: fancy a coffee?"
            if moment.city
            else f"Are you free {when}? Fancy a coffee?"
        )
    else:
        body = (
            f"Ho visto che sei a {city_shown(moment.city, lang)} {when}: ti va un caffè?"
            if moment.city
            else f"Sei libera {when}? Ti va un caffè?"
            if moment.word in ("libera", "libere")
            else f"Sei libero {when}? Ti va un caffè?"
            if moment.word in ("libero", "liberi")
            else f"{when.capitalize()} ti va un caffè?"
        )
    return _draft(style_, first, body, lang)


def wish_draft(style_: Style, kind: rules.DateKind, name: str, lang: str) -> str:
    first = rules.first_name(name)
    if lang == "en-US":
        bodies = {
            "birthday": "Happy birthday!",
            "anniversary": "Happy anniversary!",
            "name_day": "Happy name day!",
        }
    else:
        bodies = {"birthday": "Tanti auguri di buon compleanno!", "anniversary": "Buon anniversario!",
                  "name_day": "Buon onomastico!"}  # fmt: skip
    return _draft(style_, first, bodies.get(kind, bodies["birthday"]), lang)


# --- the wish at their morning -------------------------------------------------------------------------


def wish_moment(
    their_tz: str, on: tuple[int, int], owner_tz: str, now: datetime, quiet: Callable[[datetime], bool]
) -> tuple[datetime, datetime] | None:
    """(when Pisa offers the wish, until when) on THEIR day: the first moment from their 9:00 (or now) that is
    outside their quiet hours and before their 21:00. None: no such moment (their day is past, or wholly in their
    quiet hours: the morning briefing's line says it then)."""
    theirs = ZoneInfo(their_tz)
    their_now = now.astimezone(theirs)
    day = rules.nearest_day(on[0], on[1], their_now.date())
    if day < their_now.date():
        return None
    start = max(datetime.combine(day, time(rules.WISH_HOUR), tzinfo=theirs), now.astimezone(theirs))
    last = datetime.combine(day, WISH_LAST, tzinfo=theirs)
    at = start
    while at < last:
        if not quiet(at) and _daytime(at.astimezone(ZoneInfo(owner_tz))):
            return at, last
        at += STEP
    return None


def _daytime(local: datetime) -> bool:
    return OFFER_FROM <= local.time() <= OFFER_UNTIL


def offer_at(now: datetime, owner_tz: str, earliest: time = OFFER_FROM) -> datetime:
    """The next daytime moment from now on their clock: now, or the next `earliest` (never at night)."""
    local = now.astimezone(ZoneInfo(owner_tz))
    if local.time() < earliest:
        return datetime.combine(local.date(), earliest, tzinfo=local.tzinfo)
    if local.time() > OFFER_UNTIL:
        return datetime.combine(local.date() + timedelta(days=1), earliest, tzinfo=local.tzinfo)
    return now


def daytime(now: datetime, owner_tz: str) -> bool:
    return _daytime(now.astimezone(ZoneInfo(owner_tz)))


def wish_line(
    name: str, kind: rules.DateKind, their_at: datetime, city: str | None, same: bool, lang: str
) -> str:
    """ "A New York sono le 9: vuoi fare gli auguri a Marco?" (their hour, on their day)."""
    first, hour = rules.first_name(name), rules.hour_text(their_at)
    en = lang == "en-US"
    what = {
        "birthday": ("fare gli auguri a", "wish {first} a happy birthday"),
        "anniversary": ("fare gli auguri di anniversario a", "wish {first} a happy anniversary"),
        "name_day": ("fare gli auguri di onomastico a", "wish {first} a happy name day"),
        "other": ("scrivere a", "write to {first}"),
    }[kind]
    if same:
        return (
            f"It's {hour}: {what[1].format(first=first)}?"
            if en
            else f"Sono le {hour}: vuoi {what[0]} {first}?"
        )
    where = (f"in {city}" if city else "where they are") if en else (f"a {city}" if city else "da loro")
    if en:
        return f"{where[:1].upper() + where[1:]} it's {hour}: {what[1].format(first=first)}?"
    return f"{where[:1].upper() + where[1:]} sono le {hour}: vuoi {what[0]} {first}?"


# --- their clock ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    their_now: datetime
    verdict: CallVerdict
    next_good: datetime | None  # when it next is a decent hour there (None: it is now)
    owner_now: datetime

    def line(self, where: str, lang: str) -> str:
        """ "da mamma sono le 23:40: domani?"; "a New York sono le 15:20: è un buon momento per chiamare"."""
        en, hour = lang == "en-US", rules.hour_text(self.their_now)
        if self.verdict == "good":
            return capital(
                f"{where}, it's {hour}: a good time to call."
                if en
                else f"{where} sono le {hour}: puoi chiamare."
            )
        assert self.next_good is not None
        mine = self.next_good.astimezone(self.owner_now.tzinfo)
        same = self.next_good.utcoffset() == self.owner_now.utcoffset()
        theirs_day = (self.next_good.date() - self.their_now.date()).days
        when = ("tomorrow" if en else "domani") if theirs_day >= 1 else ("later" if en else "più tardi")
        if same:
            at = rules.hour_text(mine)
            return capital(
                f"{where}, it's {hour}: {when} from {at}?"
                if en
                else f"{where} sono le {hour}: {when} dalle {at}?"
            )
        his = rules.his_side(self.next_good, self.owner_now, lang)
        theirs = rules.hour_text(self.next_good)
        if en:
            return capital(f"{where}, it's {hour}: {when}, from {theirs} their time ({his} yours)?")
        return capital(f"{where} sono le {hour}: {when}, dalle {theirs} loro ({his} tue)?")


def _call_from(day: date) -> time:
    return CALL_FROM_WEEKEND if day.weekday() >= 5 else CALL_FROM


def clock(their_tz: str, owner_tz: str, now: datetime) -> Clock | None:
    """Their hour now and whether it's a decent hour to call (9:00-21:30 there, 10:00 at the weekend)."""
    theirs, mine = rules.zone(their_tz), rules.zone(owner_tz)
    if theirs is None or mine is None:
        return None
    their_now, owner_now = now.astimezone(theirs), now.astimezone(mine)
    today = their_now.date()
    opens = datetime.combine(today, _call_from(today), tzinfo=theirs)
    closes = datetime.combine(today, CALL_UNTIL, tzinfo=theirs)
    if opens <= their_now <= closes:
        return Clock(their_now, "good", None, owner_now)
    if their_now < opens:
        return Clock(their_now, "early", opens, owner_now)
    tomorrow = today + timedelta(days=1)
    return Clock(
        their_now, "late", datetime.combine(tomorrow, _call_from(tomorrow), tzinfo=theirs), owner_now
    )


def their_place(name: str, relation: str | None, lang: str) -> str:
    """ "da mamma", "da Laura" ("where Mom is"): where the clock is, as they say it."""
    first = rules.first_name(name)
    relation_names = {plain(v[i]) for v in rules.RELATIONS.values() for i in (1, 2)}
    shown = first.lower() if relation and plain(first) in relation_names else first
    if lang == "en-US":
        return f"where {first if shown == first else first.capitalize()} is"
    return f"da {shown}"


def capital(text: str) -> str:
    return text[:1].upper() + text[1:]
