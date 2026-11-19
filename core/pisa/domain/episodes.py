"""Pisa's memory of their days. Pure
rules: how code cuts their day into episodes, what a digest may say, when recall may answer, how it scores and
which of the four answers it gives, and their closed phrases about what it answered.

- **Segmentation by code**: a stay ends, a talk closes (30 minutes without a message), an event ends, a
  trip leg ends, midnight in their zone (an episode never spans it). No episode at a sensitive place, none of
  sensitive words. Outside text only as a pointer (and an email's sender class).
- **Neighbours** link only through a shared person, place or thread, within 6 hours (a design choice for the
  search radius: never by clock nearness alone).
- **recall** answers only their own question about their past (never unprompted about people and places); its
  keys come only from their words; a retrieval confidence picks one of four answers: the answer; two compete
  ("Martedì o giovedì? Ne ho due."); faded ("È sbiadito: cerco meglio?"); nothing ("Non ne ho traccia." and
  one next step built from pointers). Every line here is Pisa's own template; nothing of the question is said
  back as a fact.
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pisa.domain import health
from pisa.domain.learning import sensitive_place
from pisa.domain.lexicon.months import EN_MONTHS_TITLE, IT_MONTHS
from pisa.domain.lexicon.weekdays import DAY_NAMES, WEEKDAY_NUMBERS
from pisa.domain.profile import is_sensitive, plain

Kind = Literal["stay", "talk", "event", "trip_leg", "day"]
Answer = Literal["one", "list", "two", "faded", "none"]
Heard = Literal["keep", "forget", "correct"]

TALK_GAP = timedelta(minutes=30)  # FACT: a talk closes after 30 minutes
NEAR = timedelta(hours=6)  # FACT: the neighbours' search radius (a design choice, mice ~1.5-6 h)
LOOK_BACK_DAYS = 3  # N1 closes the last 3 local days (a night the worker missed is caught up; keys dedupe)
NOVELTY_DAYS = 30  # a person or place none of the last 30 days' episodes had is new
DIGEST_MAX = 240  # FACT: ≤ 2 lines
DIGEST_LINES = 2
DIGEST_CAP_EUR = "0.02"  # FACT: N3's digests, ≤ €0.02 a night, inside the night's €0.25
EST_CALL_EUR = "0.001"  # one digest's cost assumed before it runs, until the night's dearest is known
DIGEST_NIGHT_MAX = 40  # FACT: ~10-20 episodes a day; a night digests at most 40
DIGEST_WORDS_MAX = 1500  # characters of their words a digest call reads
# DESIGN CHOICE (no source gives a number): automatic recall holds this many
# episodes, renormalised each night; the rest fade (found when they ask to look harder)
RECALL_BUDGET = 300
GAIN = 1.0  # what they retrieved or confirmed since the last night gains this much stability
EMAILS_A_DAY = 20  # a day's episode points at most at 20 of the day's emails (id and sender class)
RESULTS = 5  # FACT: ≤ 5 digests on a turn about the past
COMPETE = 0.9  # two compete when the second scores at least 90% of the best (the margin)
PATH_OPEN = timedelta(minutes=10)  # FACT: their phrase about what recall told, 10 minutes at most

_PARTS = {  # the part of the day their words name, as local hours [from, to)
    "morning": (time(5), time(12)),
    "afternoon": (time(12), time(18)),
    "evening": (time(18), time(23, 59, 59)),
    "night": (time(0), time(5)),
}
_PART_WORDS = {
    "morning": ("mattina", "mattino", "stamattina", "morning"),
    "afternoon": ("pomeriggio", "pranzo", "pranzato", "afternoon", "lunch"),
    "evening": ("sera", "stasera", "serata", "cena", "evening", "tonight", "dinner"),
    "night": ("notte", "stanotte", "night"),
}
# words of a question that carry no key (matched against digests and their own titles, Python-side)
_STOP = frozenset(
    "il lo la i gli le un uno una di a da in con su per tra fra e o ma che chi cosa come dove quando quanto "
    "quale "
    "mi ti ci si me te ho hai ha abbiamo avete hanno sono sei e siamo era ero eri eravamo erano stato stata "
    "stati "
    "avevo avevi aveva fatto detto andata andato andati com al allo alla ai agli alle del dello della dei "
    "degli "
    "delle nel nello nella nei negli nelle sul sullo sulla sui sugli sulle col scorso scorsa scorsi ieri "
    "oggi "
    "ricordi ricordo ricordami ricordarmi pisa settimana mese anno volta ultima ultimo sera mattina "
    "pomeriggio notte "
    "the a an of to in on at with and or but what who how where when which did do does was were i me my you "
    "your we our last week month year time yesterday today ago remember evening morning afternoon night tell "
    "about non no not mai never".split()
)
_ASKS_PAST = re.compile(
    r"\b(?:ricord\w*|ultima\s+volta|l\s+altra\s+volta|scors[oaie]|ieri|l\s+altro\s+ieri|stamattina|"
    r"fa\s*\??$|com\s+e\s+andat\w+|com\s+era|dov\s+ero|dove\s+ero|dove\s+sono\s+stat\w|con\s+chi|chi\s+c\s+era|"
    r"avevo|avevamo|ero|eravamo|"
    r"(?:ho|hai|abbiamo|sono|siamo|avevo|ero)\s+(?:gia\s+|poi\s+)?\w+(?:ato|ata|ati|ate|uto|uta|uti|ito|ita|iti|"
    r"esso|essa|atto|atta|isto|ista|erto|erta)\b|"
    # not "-asta": "abbiamo pasta?"
    r"(?:sono|siamo|sei|e)\s+(?:gia\s+|poi\s+)?rimast[oaie]\b|"
    r"remember|last\s+(?:time|week|month|year|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"yesterday|\bago\b|did\s+i|was\s+i|were\s+we|had\s+i|where\s+was|who\s+was|what\s+did|how\s+did|"
    r"how\s+was|"
    r"ben\s+ik|was\s+ik|heb\s+ik|ik\s+was|gisteren|vorige\s+week|ik\s+ben\s+(?:\w+\s+){0,4}ge\w+(?:n|d|t)\b)",
)


def key_hash(key: str) -> str:
    """An episode's natural key, one way: what a -1 keeps so the night never writes it again."""
    return hashlib.sha256(key.encode()).hexdigest()


def message_key(conversation_id: str, message_id: int) -> str:
    """A message of a forgotten talk (or of the turn that forgot it): no talk holding it is written again."""
    return f"message:{conversation_id}:{message_id}"


def base_key(key: str) -> str | None:
    """A piece's source key without its local day ("stay:<device>:<epoch>"): the same source cut at another
    midnight (they moved zone) is the same memory. None for keys with no day (a talk, a day's frame)."""
    kind, _, rest = key.partition(":")
    if kind not in ("stay", "event", "trip"):
        return None
    head, _, day = rest.rpartition(":")
    return f"{kind}:{head}" if head and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None


_PARTICLES = frozenset(
    "de del della dei degli delle di da dal dalla dalle dai d la le lo li van von der den ter ten al el bin "
    "ben mc mac st san santa".split()
)


def person_said(name: str, words: str) -> bool:
    """Their words name this person: the full name, or the first name (never a particle: "Anna Della Valle"
    is not "della")."""
    if in_words(name, words):
        return True
    first = next((w for w in plain(name).split() if w not in _PARTICLES), "")
    return len(first) >= 3 and f" {first} " in f" {plain(words)} "


def sensitive_words(text: str) -> bool:
    """RAIL: their health, faith, politics… (the profile's stems, on their words but not on names: "fede"
    is no "Federico"), or a sensitive place's name (every word)."""
    starts = {m.end() for m in re.finditer(r"(?:^|[.!?:]\s+|\n)", text)}
    unnamed = " ".join(
        m.group(0) for m in re.finditer(r"\S+", text) if m.start() in starts or not m.group(0)[:1].isupper()
    )
    return is_sensitive("fact", unnamed) or sensitive_place(text, None)


# --- segmentation ------------------------------------------------------------------------------------------


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=tz)
    return start, datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)


def split(start: datetime, end: datetime, tz: ZoneInfo) -> list[tuple[date, datetime, datetime]]:
    """An interval cut at every midnight in their zone: (local day, start, end) pieces, in order."""
    pieces: list[tuple[date, datetime, datetime]] = []
    at = start
    while at < end or (at == end == start and not pieces):
        day = at.astimezone(tz).date()
        _, midnight = day_bounds(day, tz)
        until = min(end, midnight)
        pieces.append((day, at, until))
        if until >= end:
            break
        at = until
    return pieces


@dataclass(frozen=True)
class Message:
    id: int
    conversation_id: str
    role: str
    at: datetime
    words: str  # their words for a user message; "" for Pisa's


@dataclass
class Talk:
    conversation_id: str
    day: date
    started_at: datetime
    ended_at: datetime
    first_id: int
    last_id: int
    his: list[str] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)  # every message of the piece (theirs and Pisa's)

    @property
    def key(self) -> str:
        return f"talk:{self.first_id}"


def talks(messages: Sequence[Message], tz: ZoneInfo, now: datetime) -> list[Talk]:
    """The talks of these messages: one conversation's messages with no gap over 30 minutes, cut at midnight
    in their zone; only the closed ones (30 minutes past their last message)."""
    found: list[Talk] = []
    by_conv: dict[str, list[Message]] = {}
    for m in sorted(messages, key=lambda m: (m.at, m.id)):
        by_conv.setdefault(m.conversation_id, []).append(m)
    for conv, ms in by_conv.items():
        group: list[Message] = []
        for m in [*ms, None]:
            if m is not None and (not group or m.at - group[-1].at <= TALK_GAP):
                group.append(m)
                continue
            if group and group[-1].at + TALK_GAP <= now:  # closed: 30 minutes without a message
                for day in sorted({g.at.astimezone(tz).date() for g in group}):
                    part = [g for g in group if g.at.astimezone(tz).date() == day]
                    found.append(
                        Talk(conv, day, part[0].at, part[-1].at, part[0].id, part[-1].id,
                             [g.words for g in part if g.role == "user" and g.words], [g.id for g in part])
                    )  # fmt: skip
            group = [m] if m is not None else []
    return sorted(found, key=lambda t: (t.started_at, t.first_id))


def near(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    """Within the neighbours' radius: the gap between the two is at most 6 hours."""
    gap = max(b[0] - a[1], a[0] - b[1], timedelta(0))
    return gap <= NEAR


def neighbours(
    episodes: Sequence[tuple[str, datetime, datetime, frozenset[str]]],
) -> list[tuple[str, str]]:
    """RAIL: pairs (id, id) to link: within 6 hours AND sharing an entity (a person, a place, a thread);
    clock nearness alone never links."""
    pairs: list[tuple[str, str]] = []
    for i, (a, a0, a1, ea) in enumerate(episodes):
        for b, b0, b1, eb in episodes[i + 1 :]:
            if ea & eb and near((a0, a1), (b0, b1)):
                pairs.append((a, b) if a < b else (b, a))
    return sorted(set(pairs))


# --- N2, N4: salience and the recall budget ----------------------------------------------------------------


def salience(novel: int, entities: int, explicit: int, outcome_error: float) -> float:
    """Novelty (the share of its people and places none of the last 30 days' episodes had)
    + their explicit (+1, -1) + |outcome_error| (the moves of the talk, Pisa's ledger), each clipped to [0,
    1]."""
    novelty = novel / entities if entities else 0.0
    return round(min(max(novelty, 0.0), 1.0) + float(explicit) + min(abs(outcome_error), 1.0), 3)


def kept(items: Iterable[tuple[str, float, bool]], budget: int = RECALL_BUDGET) -> set[str]:
    """The episodes automatic recall holds: every protected one (linked to a future event or an open
    task), then the strongest (salience + stability) up to the fixed budget; the rest fade."""
    rows = list(items)
    protected = {i for i, _, p in rows if p}
    rest = sorted(((v, i) for i, v, p in rows if not p), key=lambda r: (-r[0], r[1]))
    room = max(budget - len(protected), 0)
    return protected | {i for _, i in rest[:room]}


# --- N3: the digest ----------------------------------------------------------------------------------------

_CAPITAL = re.compile(r"\b[A-ZÀ-Ý][^\W\d_]+")
_DIGIT = re.compile(r"\d+")


def digest_ok(digest: str, his_words: str) -> bool:
    """RAIL (the lure rule): a model's digest is kept only when it is ≤ 2 lines and 240 characters,
    says nothing sensitive, and every name (a capitalised word past a sentence's first) and every number in it
    is in their own words. Anything else is the model's invention: the code digest is kept instead."""
    text = digest.strip()
    if not text or len(text) > DIGEST_MAX or len(text.splitlines()) > DIGEST_LINES or sensitive_words(text):
        return False
    said = f" {plain(his_words)} "
    starts = {m.end() for m in re.finditer(r"(?:^|[.!?:]\s+|\n)", text)}
    for m in _CAPITAL.finditer(text):
        if m.start() in starts:
            continue
        if f" {plain(m.group(0))} " not in said:
            return False
    return all(f" {d} " in said or d in plain(his_words) for d in _DIGIT.findall(text))


def hm(at: datetime, tz: ZoneInfo) -> str:
    return f"{at.astimezone(tz):%H:%M}"


_CODE_DIGEST = {
    "it-IT": {
        "stay_home": "A casa, {a}\u2013{b}.", "stay": "Una sosta, {a}\u2013{b}.",
        "talk": "Una conversazione con Pisa, {n} messaggi tuoi, {a}\u2013{b}.",
        "event": "«{title}», {a}\u2013{b}.", "invite": "Un evento con altre persone, {a}\u2013{b}.",
        "trip_leg": "Una tratta del viaggio, {a}\u2013{b}.",
        "day": "La giornata: {stays} soste, {events} eventi, {talks} conversazioni.",
    },
    "en-US": {
        "stay_home": "At home, {a}\u2013{b}.", "stay": "A stop, {a}\u2013{b}.",
        "talk": "A talk with Pisa, {n} messages of yours, {a}\u2013{b}.",
        "event": "“{title}”, {a}\u2013{b}.", "invite": "An event with other people, {a}\u2013{b}.",
        "trip_leg": "A leg of the trip, {a}\u2013{b}.",
        "day": "The day: {stays} stops, {events} events, {talks} talks.",
    },
}  # fmt: skip


def code_digest(template: str, lang: str, **values: object) -> str:
    """A digest by code, from pointers and their own titles only (never an invite's title)."""
    table = _CODE_DIGEST.get(lang, _CODE_DIGEST["en-US"])
    return table[template].format(**values)[:DIGEST_MAX]


# --- recall: when it may answer, its keys and its four answers ---------------------------------------------


_ASKS = re.compile(
    r"\?|^\s*(?:pisa\s+)?(?:cosa|che\s+cosa|come|com|con\s+chi|chi|dove|quando|quanto|quale|ti\s+ricordi|ricordi|"
    r"dimmi|raccontami|what|who|where|when|how|which|did|was|were|do\s+you\s+remember|remind\s+me|tell\s+me)\b",
)


def asks_past(words: str) -> bool:
    """RAIL (never unprompted): their words this turn ASK about their past: a question (a "?", or
    an asking word first) with a memory word, a past tense or a past day. "Ho finito la presentazione" asks
    nothing; "com'è andata la cena?" does. Anything else: recall says nothing about people and places."""
    folded = plain(words.replace("'", " ").replace("\u2019", " "))
    asking = "?" in words or _ASKS.search(folded) is not None
    return asking and _ASKS_PAST.search(folded) is not None


_MEALS = frozenset({"cena", "pranzo", "pranzato", "cenato", "colazione", "dinner", "lunch", "breakfast"})


def content_words(text: str) -> list[str]:
    """The words of a cue that may match a digest or a title (no stop words, no day, month or part of the
    day: those are the date's, three letters or more)."""
    dated = (
        _STOP
        | set(WEEKDAY_NUMBERS)
        | {plain(m) for m in (*IT_MONTHS, *EN_MONTHS_TITLE)}
        | {w for names in _PART_WORDS.values() for w in names}
    ) - _MEALS  # a meal names the part of the day AND what they did ("la cena con Nino")
    return [w for w in plain(text).split() if len(w) >= 3 and w not in dated and not w.isdigit()]


def matches(word: str, text_words: Sequence[str]) -> bool:
    """A cue's word in a text, Python-side (no pg_trgm): the same word, or the same stem ("pranzato" and
    "pranzo", "presentazioni" and "presentazione": a common start of 5 letters or more, all but 2)."""
    for t in text_words:
        if t == word:
            return True
        n = max(5, min(len(t), len(word)) - 2)
        if len(t) >= 5 and len(word) >= 5 and t[:n] == word[:n]:
            return True
    return False


def in_words(name: str, words: str) -> bool:
    """A name (all its words) said in their words, folded."""
    parts = plain(name).split()
    return bool(parts) and f" {' '.join(parts)} " in f" {plain(words)} "


def when(words: str, today: date) -> tuple[date | None, date | None, str | None]:
    """The days and the part of the day their words name, by code: ieri, l'altro ieri, a weekday (the last one
    before today), la settimana / il mese / l'anno scorso (and English). (None, None, part) when no day."""
    w = f" {plain(words.replace(chr(39), ' '))} "
    part = next((p for p, names in _PART_WORDS.items() if any(f" {n} " in w for n in names)), None)
    if " l altro ieri " in w or " day before yesterday " in w:
        d = today - timedelta(days=2)
        return d, d, part
    if " ieri " in w or " yesterday " in w:
        d = today - timedelta(days=1)
        return d, d, part
    if " stamattina " in w or " stasera " in w or " oggi " in w or " today " in w or " this morning " in w:
        return today, today, part
    for name, wd in WEEKDAY_NUMBERS.items():
        if f" {name} " in w:
            back = (today.weekday() - wd) % 7 or 7
            d = today - timedelta(days=back)
            return d, d, part
    if re.search(r" (?:settimana scorsa|scorsa settimana|last week) ", w):
        monday = today - timedelta(days=today.weekday() + 7)
        return monday, monday + timedelta(days=6), part
    if re.search(r" (?:mese scorso|scorso mese|last month) ", w):
        first = today.replace(day=1)
        last = first - timedelta(days=1)
        return last.replace(day=1), last, part
    if re.search(r" (?:anno scorso|scorso anno|last year) ", w):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), part
    return None, None, part


def in_part(start: datetime, end: datetime, part: str | None, tz: ZoneInfo) -> bool:
    """The episode overlaps the part of the day their words name (no part: always)."""
    if part is None:
        return True
    lo, hi = _PARTS[part]
    s, e = start.astimezone(tz).time(), end.astimezone(tz).time()
    if end.astimezone(tz).date() > start.astimezone(tz).date():
        e = time(23, 59, 59)
    return s < hi and e >= lo


@dataclass(frozen=True)
class Candidate:
    id: str
    day: date
    started_at: datetime
    score: float
    faded: bool


def answer(found: Sequence[Candidate], keyed: bool) -> tuple[Answer, list[Candidate]]:
    """The retrieval confidence (a feeling-of-knowing analogue: the margin between the best and the second
    match) picks the answer: nothing → none; the best faded → faded; a second within 90% of the best → two
    (both, to ask which); else the one. Without a key (only a day): the day's episodes, as a list."""
    ranked = sorted((c for c in found if c.score > 0), key=lambda c: (-c.score, c.started_at, c.id))
    if not ranked:
        return "none", []
    if not keyed:
        live = [c for c in ranked if not c.faded]
        if not live:
            return "faded", ranked[:1]
        return ("one" if len(live) == 1 else "list"), sorted(live[:RESULTS], key=lambda c: c.started_at)
    best = ranked[0]
    if best.faded:
        return "faded", [best]
    rival = next((c for c in ranked[1:] if not c.faded), None)
    if rival is not None and rival.score >= best.score * COMPETE:
        return "two", sorted([best, rival], key=lambda c: c.started_at)
    return "one", [best]


def day_name(day: date, today: date, lang: str) -> str:
    """ "ieri", "martedì", "il 3 novembre": how Pisa names a day of theirs."""
    it = not lang.startswith("en")
    if day == today - timedelta(days=1):
        return "ieri" if it else "yesterday"
    if today - timedelta(days=7) < day < today:
        return DAY_NAMES["it-IT" if it else "en-US"][day.weekday()]
    return f"il {day.day} {IT_MONTHS[day.month - 1]}" if it else f"{EN_MONTHS_TITLE[day.month - 1]} {day.day}"


def say_two(a: Candidate, b: Candidate, today: date, tz: ZoneInfo, lang: str) -> str:
    """Two compete: "Martedì o giovedì? Ne ho due." (the same day: by the hour)."""
    it = not lang.startswith("en")
    if a.day == b.day:
        x, y = hm(a.started_at, tz), hm(b.started_at, tz)
        return f"Alle {x} o alle {y}? Ne ho due." if it else f"At {x} or at {y}? I have two."
    x, y = day_name(a.day, today, lang), day_name(b.day, today, lang)
    return f"{x[0].upper()}{x[1:]} o {y}? Ne ho due." if it else f"{x[0].upper()}{x[1:]} or {y}? I have two."


def say_faded(lang: str) -> str:
    return "È sbiadito: cerco meglio?" if not lang.startswith("en") else "It has faded: shall I look harder?"


NONE_IT, NONE_EN = "Non ne ho traccia.", "I have no record of it."


def say_none(lang: str, step: str | None) -> str:
    """Nothing: "Non ne ho traccia." and, when there is one, a next step built from pointers."""
    head = NONE_IT if not lang.startswith("en") else NONE_EN
    return f"{head} {step}" if step else head


def next_step(
    *,
    lang: str,
    day: date | None,
    today: date,
    home_from: datetime | None,
    place_from: datetime | None,
    emails: int,
    events: int,
    tz: ZoneInfo,
) -> str | None:
    """ONE next step from pointers: where they were (at home from …, or somewhere Pisa knows, never
    which when it isn't their home), then "cerco nelle mail?" when that day holds mail; else an event in their
    agenda; else none. Never a name, never a sensitive place (none is written)."""
    it = not lang.startswith("en")
    when_ = day_name(day, today, lang) if day is not None else None
    cap = f"{when_[0].upper()}{when_[1:]}" if when_ else ""
    fact = None
    if when_ and home_from is not None:
        at = hm(home_from, tz)
        fact = f"{cap} eri a casa dalle {at}" if it else f"{cap} you were home from {at}"
    elif when_ and place_from is not None:
        at = hm(place_from, tz)
        fact = f"{cap} eri fuori casa dalle {at}" if it else f"{cap} you were out from {at}"
    ask = None
    if emails:
        ask = "cerco nelle mail?" if it else "shall I look in your mail?"
    elif events and fact is None and when_:
        ask = f"{cap} avevi un impegno in agenda: guardo lì?" if it else f"{cap} you had plans: shall I look?"
    if fact and ask:
        return f"{fact}: {ask}"
    if fact:
        return f"{fact}."
    return ask[0].upper() + ask[1:] if ask else None


# --- their closed phrases about what recall answered
# ---------------------------------------------------------

_KEEP = frozenset(
    {"ricordatelo", "ricordatelo bene", "questo ricordatelo", "tienilo a mente", "tienitelo a mente",
     "ricordalo", "ricordati questo", "ricordatelo per favore",
     "remember that", "remember this", "remember it", "keep that in mind", "keep it in mind"}
)  # fmt: skip
# "forget it" / "forget that" mean "never mind" on Pisa's other paths: never here (an irreversible delete)
_FORGET = frozenset(
    {"non ricordarmelo", "non ricordartelo", "dimenticalo", "dimenticatelo", "dimentica questo",
     "don t remember that", "don t remember it"}
)  # fmt: skip
_NAME = r"([a-zà-ÿ][a-zà-ÿ'\-]{1,30})"
_CORRECT = re.compile(rf"^(?:no\s+)?(?:era|ero|eravamo)\s+con\s+{_NAME}$")
_CORRECT_EN = re.compile(rf"^(?:no\s+)?(?:it\s+was|i\s+was|we\s+were)\s+with\s+{_NAME}$")


def _bare(raw: str) -> str:
    text = re.sub(r"^\s*(?:ehi\s+)?pisa\s*[,:]\s*", "", raw.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*pisa\s*([.!]*)\s*$", r"\1", text, flags=re.IGNORECASE)
    return text


def heard(text: str) -> tuple[Heard, str | None] | None:
    """Which closed phrase this WHOLE utterance is: "ricordatelo" (keep), "non ricordarmelo" (forget), "no,
    era con Nino" (correct, with the name as they said it); None: anything more or else (the model's). One
    name, one comma at most (after "no"); no "?", "!", ";" or ":"; nothing before or after."""
    raw = text.strip()
    if not raw or len(raw) > 80:
        return None
    body = _bare(raw).rstrip(" .")
    if re.search(r"[?!;:]", body) or body.count(",") > 1:
        return None
    if body.count(",") == 1 and not re.match(r"^\s*no\s*,", body, re.IGNORECASE):
        return None
    words = " ".join(body.replace(",", " ").replace("\u2019", "'").replace("'", " ").lower().split())
    if words in _KEEP:
        return "keep", None
    if words in _FORGET:
        return "forget", None
    m = _CORRECT.match(words) or _CORRECT_EN.match(words)
    if m is not None:
        name = m.group(1)
        if name in {"me", "lui", "lei", "loro", "chi", "him", "her", "them", "who", "te", "you"}:
            return None
        original = next((w for w in re.findall(r"[^\W\d_][\w'\-]*", body) if w.lower() == name), name)
        return "correct", original
    return None


KEPT_SAID = {"it-IT": "Me lo tengo.", "en-US": "I'll keep it."}
FORGOT_SAID = {"it-IT": "Fatto: l'ho dimenticato.", "en-US": "Done: I've forgotten it."}


def corrected_said(name: str, lang: str) -> str:
    return f"Corretto: eri con {name}." if not lang.startswith("en") else f"Corrected: you were with {name}."


HEALTH_LOOK_BACK = timedelta(days=7)  # "sono stato in farmacia?" with no day: the last week
HEALTH_LISTED = 3


def say_health(
    hits: Sequence[tuple[str, datetime, datetime]],
    asked: set[str],
    lo: date,
    hi: date,
    today: date,
    tz: ZoneInfo,
    lang: str,
) -> str:
    """Their own question about a health place, answered from Pisa's stays by code: the place and the
    time, never what they did there or why (never a diagnosis)."""
    it = not lang.startswith("en")
    if not hits:
        kind = health.KIND_SAID[sorted(asked)[0]][0 if it else 1]
        span = (
            day_name(lo, today, lang) if lo == hi
            else ("negli ultimi giorni" if it else "in the last few days")
        )  # fmt: skip
        return f"Non ho traccia di una sosta {kind} {span}." if it else f"I have no stay {kind} {span}."
    told = [
        f"{day_name(a.astimezone(tz).date(), today, lang)} {name}, {hm(a, tz)}\u2013{hm(b, tz)}"
        for name, a, b in hits[-HEALTH_LISTED:]
    ]
    return ("Sì: " if it else "Yes: ") + "; ".join(told) + "."


def told_lines(found: dict[str, Any]) -> list[str]:
    """recall's result as the tool's lines (Pisa's own records, nothing outside): one line per episode."""
    lines = [f"answer: {found['answer']}"]
    if found.get("say"):
        lines.append(f"say: «{found['say']}»")
    for e in found.get("episodes") or []:
        bits = [f"[episode {e['id']}] {e['day']} ({e['date']}) {e['from']}\u2013{e['to']}, {e['kind']}"]
        if e.get("place"):
            bits.append(f"at: {e['place']}")
        if e.get("with"):
            bits.append("with: " + ", ".join(e["with"]))
        if e.get("others"):
            bits.append(f"+{e['others']} other people")
        if e.get("title"):
            bits.append(f"their title: «{e['title']}»")
        if e.get("digest"):
            bits.append(f"«{e['digest']}»")
        lines.append("- " + "; ".join(bits))
    return lines
