"""Learning from what the user does. Pure rules, no I/O.

**Evidence** comes only from the user's own behaviour: stays (where the phone stayed), the app's actions on
a places card (a "go" or a visit, never a card shown or tapped), check-in answers. Never from a
review, an email, a page or an event's text: an event gives only its time, its place and how many
others attend.

**One rule for every signal**: each piece of evidence has a weight that halves every half-life (per subject);
support and against are their sums; confidence = support / (support + against + 2). An observed claim is used
(in prompts) at confidence ≥ 0.6 with support ≥ 3; below that it is a candidate. A used claim that the
evidence turns against is never flipped silently: it goes to the weekly check ("this seems to have changed").

**Scopes**: day (weekday / weekend), company (alone / others), where (home / travel), time of day. A claim is
learned for the narrowest scope with enough support, and generalised only when the scopes agree; travel
evidence never makes or changes a home claim (`where` always splits).

**Statements** are fixed sentences in the user's language, built from the subject (no model), so the same
evidence always gives the same sentence: a forgotten claim's tombstone (an HMAC of kind and statement) blocks
it from coming back, and an off-limits topic is matched against it.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Literal

from pisa.domain.health import health_place
from pisa.domain.lexicon.weekdays import DAY_NAMES
from pisa.domain.places import WALK_MAX_MIN
from pisa.domain.profile import STATEMENT_MAX, confidence, plain, time_of_day

WeeklyKind = Literal["confirm", "changed", "new", "expiring", "buffer"]
EventKind = Literal["social", "work", "appointment", "travel", "other"]

USE_CONFIDENCE = 0.6  # a claim is used from here (with USE_SUPPORT)
USE_SUPPORT = 3.0
SHOW_LOW = 0.4  # from here a candidate may be shown as "I think…" in the weekly check
CHANGED_RUN = 3  # contrary items in a row within CHANGED_DAYS: the claim seems to have changed
CHANGED_DAYS = 14
EXPIRING_SOON = timedelta(days=7)

# The signals and their weights.
STAY_MIN = timedelta(minutes=20)  # a stay this long at a place counts
RETURN_WEIGHT = 1.0  # each return after the first…
RETURNS_PER_MONTH = 3  # … at most this many a month per place
NEAR_HOME_M = 300.0  # a place this close to home or work counts half (convenience, not taste)
LATE_ACCURACY_M = 50.0  # only stays this precise measure lateness
LATE_REACH_M = 150.0  # the stay is at the event's place
LATE_SAMPLES = 5  # reported only with this many samples…
LATE_LATEST = 20  # … the median of the latest 20
LATE_MIN = 3  # minutes after the start that count as late
BUFFER_MIN = 5  # a buffer is offered for a median lateness of at least this many minutes
PICK_FIRST = 0.5  # the first slot of a card (position bias)…
PICK_OTHER = 1.0  # … any other slot
IGNORED = 0.1  # a card dismissed: a little against each place on it
REJECTED = 0.3  # "find another place" without a reason: against that place only
INTEREST = "interest"  # a tap on a card: kept as interest, never a like
ROUTINE_MIN = 3  # at least 3 times in 4 weeks, same place, weekday and time
ROUTINE_WINDOW = timedelta(weeks=4)
TRIP_LATEST = 30  # the 80th percentile of the latest 30 walks
WALK_KMH = 6.5  # a trip between stays this slow (or slower) was on foot…
WALK_MIN_KMH = 2.0  # … and at least this fast (straight line): slower is a stop somewhere, not a walk
TRAVEL_KM = 50.0  # a night this far from home: travelling
# A trip that never ends is a move: this many nights of the last MOVED_DAYS in one place far from home make it
# home again.
MOVED_NIGHTS = 30
MOVED_DAYS = 35

HALF_LIFE_DAYS = {  # how long a claim lasts without new evidence
    "place": 90, "routine": 21, "mobility": 60, "lateness": 3650,
}  # fmt: skip

# Places that are never evidence: health, worship, politics,
# in the place's category AND its name, Italian, English and Dutch. Prefixes of words…
SENSITIVE_PLACE_STEMS = (
    # health
    "ospedal", "clinic", "clinica", "kliniek", "ambulator", "poliambulator", "dentist", "odontoiatr", "medic",
    "farmac", "pharmac", "drugstore", "apothe", "fisioterap", "physiotherap", "fysiotherap", "psic", "psych",
    "ortoped", "orthop", "chiropr", "osteopat", "logoped", "dermatolog", "ginecolog", "gynaecolog",
    "cardiolog", "oculist", "pediatr", "radiolog", "terapi", "therap", "riabilit", "rehab", "dialisi",
    "soccors", "hospital", "hospice", "doctor", "physician", "health", "salute", "ziekenhuis", "huisarts",
    "tandarts", "gezondheid", "sanitar",
    # worship
    "chiesa", "chiese", "parrocch", "parish", "basilic", "cattedral", "cathedral", "duomo", "santuari",
    "sanctuar", "oratori", "convent", "monaster", "abbazi", "abbey", "moschea", "mosque", "moskee", "sinagog",
    "synagog", "tempio", "temple", "church", "kerk", "chapel", "worship", "diocesi", "curia", "seminari",
    "cimiter", "cemetery", "begraafplaats", "funer",
    # … more health and faiths by name (generic categories let these through)
    "poliklin", "fysio", "fisio", "consultori", "diagnost", "analisi", "oncolog", "neurolog", "urolog",
    "endocrin", "gastroenter", "nefrolog", "otorin", "trasfus", "fertil", "islam", "geova", "jehovah",
    "gurdwar", "mandir", "evangel", "pentecost", "mormon", "buddh", "hindu", "rabbin", "chabad", "joods",
    # politics, unions, the law
    "partito", "partij", "politic", "politiek", "sindacat", "government", "governo", "polizia", "police",
    "politie", "carabinier", "questura", "tribunal", "courthouse", "avvocat", "lawyer", "advocaat",
    "rifondaz",
)  # fmt: skip
# … and whole words (party initials, unions, "sede" of a party).
SENSITIVE_PLACE_WORDS = frozenset({
    "asl", "ats", "pd", "m5s", "fdi", "lega", "cgil", "cisl", "uil", "sede", "vvd", "pvv", "d66", "cda",
    "msi", "anpi", "fi", "avs", "psi", "pvda", "groenlinks", "glpvda", "sp", "cu", "sgp", "bbb", "fvd", "nsc",
    "ggz", "avis", "sikh",
})  # fmt: skip
# … and names of more than one word (after `plain`: "Fratelli d'Italia" is "fratelli d italia").
SENSITIVE_PLACE_PHRASES = (
    "movimento 5 stelle", "movimento cinque stelle", "fratelli d italia", "forza italia", "italia viva",
    "kingdom hall",
)  # fmt: skip

EVENT_WORDS: dict[str, tuple[str, ...]] = {
    "travel": ("volo", "flight", "treno", "train", "aereo", "imbarco", "boarding", "partenza", "departure"),
    "appointment": (
        "dentist", "medic", "doctor", "dottor", "visita", "appuntament", "appointment", "parrucch", "barbier",
        "haircut", "notai", "commercialist", "banca", "bank",
    ),
    "work": (
        "riunion", "meeting", "call", "standup", "stand", "sync", "review", "workshop", "colloqui",
        "interview", "demo", "lavor", "work", "uffici", "office", "client", "presentazion", "presentation",
        "team",
    ),
    "social": (
        "cena", "dinner", "pranz", "lunch", "aperitiv", "aperitif", "drink", "festa", "party", "compleann",
        "birthday", "colazion", "breakfast", "brunch", "caff", "coffee", "cinema", "concert", "amic",
        "friend", "partita", "match",
    ),
}  # fmt: skip
EVENT_KIND_NAMES = {
    "social": ("gli incontri con altri", "plans with others"),
    "work": ("gli impegni di lavoro", "work meetings"),
    "appointment": ("gli appuntamenti", "appointments"),
    "travel": ("le partenze", "departures"),
    "other": ("gli impegni", "plans"),
}


@dataclass(frozen=True)
class Ev:
    """One piece of evidence as consolidation sees it."""

    signal: str
    kind: str
    subject: str
    weight: float
    direction: int  # 1 for, -1 against
    scope: dict[str, str]
    at: datetime
    value: dict[str, Any] | None = None


@dataclass
class Tally:
    support: float = 0.0
    against: float = 0.0
    count: int = 0
    last_at: datetime | None = None
    contrary_run: float = 0.0  # contrary items in a row, newest first, within CHANGED_DAYS, by weight
    values: list[dict[str, Any]] = field(default_factory=list)


def subject_type(subject: str) -> str:
    return subject.split(":", 1)[0]


def subject_place(subject: str) -> str | None:
    """The place a subject is about ("place:<id>", "routine:<id>:<weekday>:<hour>"), else None."""
    kind, _, rest = subject.partition(":")
    if kind == "place":
        return rest
    if kind == "routine":
        return rest.rsplit(":", 2)[0]
    return None


def half_life(subject: str) -> float:
    return float(HALF_LIFE_DAYS.get(subject_type(subject), 90))


def decayed(weight: float, at: datetime, now: datetime, half_life_days: float) -> float:
    age = max(0.0, (now - at).total_seconds() / 86400)
    return weight * math.pow(0.5, age / half_life_days)


def matches(ev_scope: dict[str, str], scope: dict[str, str]) -> bool:
    return all(ev_scope.get(k) == v for k, v in scope.items())


def tally(evs: list[Ev], now: datetime, scope: dict[str, str] | None = None) -> Tally:
    t = Tally()
    chosen = sorted((e for e in evs if scope is None or matches(e.scope, scope)), key=lambda e: e.at)
    for e in chosen:
        w = decayed(e.weight, e.at, now, half_life(e.subject))
        if e.direction > 0:
            t.support += w
        else:
            t.against += w
        t.count += 1
        t.last_at = e.at
        if e.value is not None:
            t.values.append(e.value)
    for e in reversed(chosen):  # newest first: contrary items in a row, recent ones only
        if e.direction > 0 or now - e.at > timedelta(days=CHANGED_DAYS):
            break
        # By weight: "another place" (0.3) counts as one item, a dismissed card (0.1)
        # as a third of one.
        t.contrary_run += min(1.0, e.weight / REJECTED)
    return t


def qualifies(t: Tally) -> bool:
    return t.support >= USE_SUPPORT and confidence(t.support, t.against, False) >= USE_CONFIDENCE


def choose_scope(evs: list[Ev], now: datetime) -> dict[str, str]:
    """The narrowest scope with enough support, generalised where the scopes agree: per dimension, when the
    evidence holds for exactly one of its values and not for the others seen, the claim is for that value;
    when it holds for all of them (they agree), or for none alone, the dimension stays open. `where` always
    splits (the caller groups by it): travel never makes a home claim."""
    scope: dict[str, str] = {}
    for dim in ("day", "company", "time"):
        seen = {v for e in evs if (v := e.scope.get(dim))}
        holding = [v for v in sorted(seen) if qualifies(tally(evs, now, {dim: v}))]
        if len(holding) == 1 and (len(seen) == 1 or len(holding) < len(seen)):
            scope[dim] = holding[0]
    return scope


def claim_state(
    t: Tally,
    *,
    previous: str | None,
    used_before: bool,
    needs_confirmation: bool,
    confirmed: bool,
    everything: Tally | None = None,
    retired_for_good: bool = True,
) -> tuple[str, bool]:
    """(state, changed): `changed` when a used claim's evidence turned against it (never a silent flip),
    judged on all its evidence (`everything`: the caller gives only what came after the user's last "right",
    so a confirmation sticks), whatever scope it was learned for. Retired for good only by
    the user; an expired claim comes back with new evidence."""
    if previous == "retired":
        if retired_for_good:
            return "retired", False
        previous, used_before = None, False
    whole = everything or t
    if used_before and (whole.against > whole.support or whole.contrary_run >= CHANGED_RUN):
        return "asking", True
    if previous == "asking":
        return "asking", False
    if confirmed:  # the user said it's right: used, at least 0.8, until the evidence turns against it
        return "active", False
    if qualifies(t) and not needs_confirmation:
        return "active", False
    return "candidate", False


def scope_at(local: datetime, *, company: str | None = None, where: str | None = None) -> dict[str, str]:
    scope = {"day": "weekend" if local.isoweekday() >= 6 else "weekday", "time": time_of_day(local)}
    if company:
        scope["company"] = company
    if where:
        scope["where"] = where
    return scope


def event_kind(title: str, attendees: int | None) -> str:
    """What kind of event, for lateness: from the title's words (never its text into evidence), else "with
    others" when it has guests."""
    words = plain(title).split()
    for kind, stems in EVENT_WORDS.items():
        if any(w.startswith(stems) for w in words):
            return kind
    return "social" if attendees else "other"


# RAIL: nothing is learned or said aloud at a sensitive place.
def sensitive_place(name: str | None, category: str | None) -> bool:
    """Health, worship or politics in the place's name or category (case, accents and "_" don't matter).
    every path where Pisa speaks of their stays by itself checks this one (health: `health_place`)."""
    if health_place(name, category):
        return True
    text = plain(f"{name or ''} {(category or '').replace('_', ' ')}")
    if any(f" {phrase} " in f" {text} " for phrase in SENSITIVE_PLACE_PHRASES):
        return True
    return any(w.startswith(SENSITIVE_PLACE_STEMS) or w in SENSITIVE_PLACE_WORDS for w in text.split())


def taste_place(name: str | None, category: str | None) -> bool:
    """A place may be evidence (a like, a routine): its category is known (no category: stricter, never) and
    nothing in its name or category is health, worship or politics."""
    return bool(category) and not sensitive_place(name, category)


def trip_mode(metres: float, minutes: float) -> str | None:
    """Walk, bike or motor from a trip's speed; None when it isn't a trip worth counting. A walk is at most
    WALK_MAX_MIN minutes at 2 km/h or more in a straight line: a slow 2-hour gap between
    two stays is a stop somewhere unseen, not a walk."""
    if metres < 300 or not 3 <= minutes <= 180:
        return None
    kmh = metres / 1000 / (minutes / 60)
    if kmh <= WALK_KMH:
        return "walk" if kmh >= WALK_MIN_KMH and minutes <= WALK_MAX_MIN else None
    return "bike" if kmh <= 20 else "motor"


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def lateness_minutes(values: list[dict[str, Any]]) -> tuple[int, int, int] | None:
    """(median minutes late of the latest 20, how many were late, how many samples), with ≥ 5 samples."""
    latest = [float(v["late_min"]) for v in values[-LATE_LATEST:] if "late_min" in v]
    if len(latest) < LATE_SAMPLES:
        return None
    late = sum(1 for m in latest if m >= LATE_MIN)
    return round(median(latest)), late, len(latest)


NAME_MAX = 60
_UNSAFE = re.compile(r"[\[\]{}<>«»\"`|\\]")


def clean_name(name: str | None) -> str:
    """A place name from a map provider, as data in a sentence: no brackets, quotes,
    newlines or control characters, at most 60 characters, inside «»."""
    text = "".join(c if c.isprintable() else " " for c in (name or ""))
    text = " ".join(_UNSAFE.sub(" ", text).split())[:NAME_MAX].strip()
    return f"«{text or '?'}»"


def statement(subject: str, value: dict[str, Any] | None, names: dict[str, str], lang: str) -> str:
    """The claim's fixed sentence in the user's language (no model): the same evidence, the same words.
    Place names are cleaned (`clean_name`); at most STATEMENT_MAX characters."""
    return _statement(subject, value, names, lang)[:STATEMENT_MAX]


def _statement(subject: str, value: dict[str, Any] | None, names: dict[str, str], lang: str) -> str:
    it = lang == "it-IT"
    kind, _, rest = subject.partition(":")
    value = value or {}
    if kind == "place":
        name = clean_name(names[rest]) if rest in names else ("un posto" if it else "a place")
        return f"Gli piace {name}" if it else f"Likes {name}"
    if kind == "lateness":
        it_name, en_name = EVENT_KIND_NAMES.get(rest, EVENT_KIND_NAMES["other"])
        minutes = int(value.get("minutes", 0))
        if minutes < LATE_MIN:
            return f"Per {it_name} arriva puntuale" if it else f"For {en_name}, arrives on time"
        if it:
            return f"Per {it_name} arriva di solito circa {minutes} minuti dopo l'inizio"
        return f"For {en_name}, usually arrives about {minutes} minutes after the start"
    if kind == "routine":
        place, dow, hour = rest.rsplit(":", 2)
        name = clean_name(names[place]) if place in names else ("lo stesso posto" if it else "the same place")
        day = DAY_NAMES["it-IT" if it else "en-US"][int(dow)]
        if it:
            return f"Di solito va a {name} il {day} verso le {int(hour):02d}"
        return f"Usually goes to {name} on {day}s around {int(hour):02d}:00"
    if kind == "mobility":
        minutes = int(value.get("minutes", 0))
        return (
            f"A piedi fa di solito fino a circa {minutes} minuti"
            if it
            else (f"Usually walks up to about {minutes} minutes")
        )
    return rest


def evidence_line(count: int, weeks: int, lang: str) -> str:
    if lang == "it-IT":
        return f"Visto {count} volte nelle ultime {weeks} settimane"
    return f"Seen {count} times in the last {weeks} weeks"


@dataclass(frozen=True)
class WeeklyCandidate:
    kind: WeeklyKind
    claim_id: str | None
    statement: str
    why: str
    evidence: list[dict[str, str]]
    value: dict[str, Any] | None
    rank: float  # within a kind: higher first


WEEKLY_ORDER: tuple[WeeklyKind, ...] = ("changed", "buffer", "confirm", "new", "expiring")
WEEKLY_MAX = 3


def pick_weekly(candidates: list[WeeklyCandidate]) -> list[WeeklyCandidate]:
    """At most 3, in this order: a used claim that seems to have changed, a
    buffer (lateness, once), a hunch to confirm ("I think…"), a new claim about to be used, one about to
    expire. Empty most weeks is fine."""
    ordered = sorted(candidates, key=lambda c: (WEEKLY_ORDER.index(c.kind), -c.rank))
    return ordered[:WEEKLY_MAX]


WEEKLY_TEXT = {
    "it-IT": "Hai un minuto? Ho {n} cose da chiederti su di te, nell'app.",
    "en-US": "Got a minute? I have {n} things to check with you, in the app.",
}
