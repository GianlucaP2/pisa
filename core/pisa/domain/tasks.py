"""Tasks: what the user has to do. Pure rules, no I/O."""

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pisa.domain.errors import PisaError
from pisa.domain.lexicon.dates import iso_moment_or_none
from pisa.domain.lexicon.months import EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS, IT_MONTHS_SHORT, month_forms
from pisa.domain.lexicon.weekdays import WEEKDAY_NUMBERS
from pisa.domain.reasons import task_reason
from pisa.domain.text import fold, fold_words

TaskStatus = Literal["open", "done", "dismissed", "deleted"]  # deleted: soft, 24 h to restore
DueKind = Literal["none", "date", "datetime"]
SourceKind = Literal["voice", "email", "manual", "calendar"]  # calendar, a date's reminder

TITLE_MIN, TITLE_MAX = 3, 120
LOW = 3  # low is quiet: never in the plan, reminders, the briefing or Today
DURATION_MIN, DURATION_MAX = 5, 480  # minutes a task takes
MAX_TASKS_PER_EMAIL = 5
MAX_DUE_AHEAD_YEARS = 2
END_OF_DAY = time(23, 59)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    notes: str | None
    status: TaskStatus
    due_at: datetime | None
    due_kind: DueKind
    duration_min: int | None
    place_text: str | None
    priority: int
    source_kind: SourceKind
    source_ref: str | None
    source_label: str | None
    confidence: float | None
    created_at: datetime
    updated_at: datetime
    place_id: str | None = None  # the place it was resolved to, when there is one clear match
    place_elsewhere: str | None = None  # the other city its place text names: not placed today
    repeat: dict[str, Any] | None = None  # {"every": day | week | month, "interval": 1-12}
    place_by_owner: bool = False  # the user set its place: it always holds
    priority_reason: str | None = None  # why this priority, in one line (triage's, as data)
    priority_source: str = "pisa"  # pisa | owner: the user raised or lowered it
    # a delivery's window from its email ("tra le 10 e le 14"): on that day in the plan, at that time
    expected_from: datetime | None = None
    expected_until: datetime | None = None
    # where its deadline (due_at) came from: owner, email, web, pisa
    deadline_source: str | None = None
    country: str | None = None  # the one country it can be done in (IT, US), from its words
    country_by_owner: bool = False  # they set it (a new title never re-reads it)
    # its email's triage category and notice, read with it, so its reason is checked on
    # read in their language (`reasons.task_reason`); None for a task not from an email
    email_category: str | None = None
    email_notice: str | None = None
    private: bool = False  # they marked it private: never named on the wrist


# Things done online or on the phone (a task like "Acquista i biglietti per Nachtlicht…" got the event's venue
# as its place and "Esci ora" at 00:01 and 01:18). A task's first word, as a stem.
ONLINE_VERBS = (
    "acquist", "compr", "prenot", "pag", "rispond", "rinnov", "ordin", "iscriv", "registr", "scriv", "invi",
    "mand", "disdic", "annull", "buy", "purchase", "book", "pay", "reply", "respond", "renew", "order",
    "register", "subscribe", "sign", "write", "send", "email", "cancel",
)  # fmt: skip


def online_task(title: str) -> bool:
    """Buying, booking, paying, replying…: done online, so no travel and no "leave now"."""
    first = title_key(title).split()[:1]
    return bool(first) and first[0].startswith(ONLINE_VERBS)


def task_place_id(task: "Task") -> str | None:
    """Where the task is done: its place, unless it is done online and came from an email (a place the user
    set themselves always holds; an email's venue for "buy the tickets" is not where the user goes)."""
    if task.source_kind == "email" and not task.place_by_owner and online_task(task.title):
        return None
    return task.place_id


def title_key(title: str) -> str:
    """Normalised title for dedupe: case, accents, punctuation and spacing don't matter."""
    return fold_words(title)


def end_of_day(day: date, timezone: str) -> datetime:
    """A `date` due: by the end of that day in the user's time zone (23:59 local, with its offset)."""
    return datetime.combine(day, END_OF_DAY, tzinfo=ZoneInfo(timezone))


def rfc3339(moment: datetime, timezone: str) -> str:
    """`moment` in the user's time zone, as RFC 3339, whose offsets are whole minutes. A zone's local mean
    time before it took standard time (Rome before 1866: +00:49:56) has seconds RFC 3339 can't write: such a
    moment is written in UTC."""
    local = moment.astimezone(ZoneInfo(timezone))
    offset = local.utcoffset()
    if offset is not None and offset.seconds % 60:
        local = moment.astimezone(UTC)
    return local.isoformat()


def latest_due(now: datetime) -> datetime:
    try:
        return now.replace(year=now.year + MAX_DUE_AHEAD_YEARS)
    except ValueError:  # 29 February
        return now.replace(year=now.year + MAX_DUE_AHEAD_YEARS, day=28)


def sort_key(task: Task, now: datetime) -> tuple[int, int, datetime, int, datetime]:
    """Overdue first, then due_at ascending (no due date last), then priority, then created_at."""
    overdue = task.status == "open" and task.due_at is not None and task.due_at < now
    far = datetime.max.replace(tzinfo=now.tzinfo)
    return (
        0 if overdue else 1,
        1 if task.due_at is None else 0,
        task.due_at or far,
        task.priority,
        task.created_at,
    )


def local_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    """[local midnight, next local midnight): 23 or 25 hours long on DST change days."""
    tz = ZoneInfo(timezone)
    return datetime.combine(day, time.min, tzinfo=tz), datetime.combine(
        day + timedelta(days=1), time.min, tzinfo=tz
    )


# --- matching a spoken reference ("the parcel one", "ho ritirato il pacco") to an open task ---------------

_STOPWORDS = frozenset(
    "a an the one that this to of for my and task tasks done il lo la i gli le un uno una di del della dei "
    "delle "
    "al alla ai alle per da con su quello quella quelli ho hai ha fatto finito mio mia miei mie che e".split()
)
MATCH_THRESHOLD = 0.5
CLEAR_MARGIN = 0.25


def _stems(text: str) -> set[str]:
    # A 5-letter prefix is a cheap stem that also matches across Italian forms (ritirare / ritirato / ritiro).
    return {word[:5] for word in title_key(text).split() if word not in _STOPWORDS and len(word) > 2}


def match_score(reference: str, title: str) -> float:
    if title_key(reference) == title_key(title):
        return 1.0
    wanted = _stems(reference)
    if not wanted:
        return 0.0
    return len(wanted & _stems(title)) / len(wanted)


def same_item(one: str, other: str) -> bool:
    """Two titles name the same item, conservatively: the same normalised title, or the same stems
    with nothing left over either way ("Comprare il latte" = "comprare latte"; "Buy milk" is not "Buy oat
    milk")."""
    if title_key(one) == title_key(other):
        return True
    stems = _stems(one)
    return bool(stems) and stems == _stems(other)


class AlreadyListed(Exception):
    """`add_task` found the same item open on their list (`task`): nothing was added."""

    def __init__(self, task: Task) -> None:
        super().__init__(task.title)
        self.task = task


def twin(title: str, tasks: list[Task]) -> Task | None:
    """The open task among `tasks` that is the same item as `title` (None: none is)."""
    return next((t for t in tasks if t.status == "open" and same_item(title, t.title)), None)


@dataclass(frozen=True)
class TaskMatch:
    task: Task | None  # the one task meant, when it is clear
    candidates: tuple[Task, ...]  # several plausible tasks: ask the user; never guess


def resolve_task(reference: str, tasks: list[Task]) -> TaskMatch:
    scored = sorted(((match_score(reference, t.title), t) for t in tasks), key=lambda st: -st[0])
    plausible = [(s, t) for s, t in scored if s >= MATCH_THRESHOLD]
    if not plausible:
        return TaskMatch(None, ())
    best = plausible[0][0]
    close = [t for s, t in plausible if best - s < CLEAR_MARGIN]
    if len(close) == 1:
        return TaskMatch(close[0], ())
    return TaskMatch(None, tuple(close[:3]))


# --- repeating tasks -------------------------------------------------------------------------

REPEAT_EVERY = ("day", "week", "month")
REPEAT_INTERVAL = (1, 12)


STATE_NOTES = 80  # a note's first characters in a write's read-back


def state_line(task: Task, timezone: str) -> str:
    """The task as the store now holds it, in one compact line for a write's
    result, so Pisa says what is there and not what it meant to write. Times in their zone; only fields that
    are set (normal priority, no place, no repeat say nothing)."""
    tz = ZoneInfo(timezone)
    parts = [f"«{task.title}»", task.status]
    if task.due_at is not None:
        local = task.due_at.astimezone(tz)
        parts.append(f"by {local:%a %d %b}" if task.due_kind == "date" else f"due {local:%a %d %b %H:%M}")
    if task.expected_from is not None:
        parts.append(f"to do at {task.expected_from.astimezone(tz):%a %d %b %H:%M}")
    if task.duration_min:
        parts.append(f"{task.duration_min} min")
    if task.priority != 2:
        parts.append("priority high" if task.priority == 1 else "priority low")
    if task.place_text:
        parts.append(f"place «{task.place_text}»")
    if task.country:
        parts.append(f"only in {task.country}")
    if task.repeat:
        n, every = int(task.repeat.get("interval") or 1), str(task.repeat.get("every"))
        parts.append(f"repeats every {every}" if n == 1 else f"repeats every {n} {every}s")
    if task.notes:
        note = " ".join(task.notes.split())
        parts.append(f"notes «{note[:STATE_NOTES]}{'…' if len(note) > STATE_NOTES else ''}»")
    return ", ".join(parts)


def check_repeat(value: Any) -> dict[str, Any] | None:
    """null, or {"every": day | week | month, "interval": 1-12}."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"every", "interval"}
        or value["every"] not in REPEAT_EVERY
        or isinstance(value["interval"], bool)
        or not isinstance(value["interval"], int)
        or not REPEAT_INTERVAL[0] <= value["interval"] <= REPEAT_INTERVAL[1]
    ):
        raise PisaError(
            "validation_failed",
            'The repeat must be null or {"every": "day" | "week" | "month", "interval": 1-12}.',
            "Fix the task and try again",
        )
    return {"every": value["every"], "interval": value["interval"]}


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def next_due(
    due_at: datetime | None, due_kind: DueKind, repeat: dict[str, Any], done_at: datetime, timezone: str
) -> tuple[datetime, DueKind]:
    """The next occurrence's due: the due date moved by the interval (the 31st of a month goes to the last day
    of a shorter one). A task without a due date repeats from the day it was done, due that day."""
    tz = ZoneInfo(timezone)
    base = due_at.astimezone(tz) if due_at is not None else done_at.astimezone(tz)
    kind: DueKind = due_kind if due_at is not None else "date"
    n = repeat["interval"]
    if repeat["every"] == "month":
        day = _add_months(base.date(), n)
    else:
        day = base.date() + timedelta(days=n * (7 if repeat["every"] == "week" else 1))
    if kind == "datetime":
        return datetime.combine(day, base.time(), tzinfo=tz), kind
    return end_of_day(day, timezone), kind


_CLOCK = re.compile(r"(?:alle |at )?([01]?\d|2[0-3])[:.]([0-5]\d)")


def _plain_lower(text: str) -> str:
    return fold(text.strip())


# FACT: until 05:00 the user hasn't slept yet: their "domani" is the day after they wake, today's date
# ("domani alle 10" said at 02:38 on the 30th is the 30th).
SMALL_HOURS_END = 5


def in_small_hours(moment: datetime) -> bool:
    """`moment` (local) is between midnight and SMALL_HOURS_END: "domani" means today's date."""
    return moment.hour < SMALL_HOURS_END


def day_named(text: str, today: date, *, small_hours: bool = False) -> date | None:
    """The day the words name: today/oggi/stasera, domani, dopodomani, a weekday (the next one) or a
    YYYY-MM-DD; None when they name none. `small_hours`: said between midnight and 05:00, when the user's
    "domani" is today's date (and "dopodomani" tomorrow's)."""
    plain = _plain_lower(text)
    words = _CLOCK.sub(" ", plain).split()
    if any(w in ("today", "oggi", "stasera", "stamattina", "stanotte") for w in words):
        return today
    shift = 0 if small_hours else 1
    if "dopodomani" in words or "day after tomorrow" in plain:
        return today + timedelta(days=1 + shift)
    if any(w in ("tomorrow", "domani") for w in words):
        return today + timedelta(days=shift)
    if (weekday := next((WEEKDAY_NUMBERS[w] for w in words if w in WEEKDAY_NUMBERS), None)) is not None:
        if small_hours and weekday == today.weekday():  # "venerdì" at 02:38 on Friday: the day they wake to
            return today
        return today + timedelta(days=(weekday - today.weekday() - 1) % 7 + 1)
    try:
        return date.fromisoformat(next(w for w in words if re.fullmatch(r"\d{4}-\d{2}-\d{2}", w)))
    except (StopIteration, ValueError):
        return None


def clock_times(text: str) -> set[tuple[int, int]]:
    """Every clock time written in the words ("15:30", "alle 9.15"): (hour, minute)."""
    return {(int(h), int(m)) for h, m in _CLOCK.findall(_plain_lower(text))}


def parse_when(text: str, today: date, *, small_hours: bool = False) -> tuple[str | None, DueKind]:
    """When a task is now due, from the user's words (reschedule_task): "none" / "nessuna" clears it; else
    today/oggi, tomorrow/domani, dopodomani, a weekday (the next one after today) or YYYY-MM-DD, with an
    optional time ("domani alle 15:30"); or YYYY-MM-DDTHH:MM. Returns (due_at, due_kind) for TasksService."""
    plain = fold(text.strip())
    if plain in ("", "none", "nessuna", "no", "niente"):
        return None, "none"
    if (moment := iso_moment_or_none(plain.upper())) is not None:
        if "t" in plain:
            return moment.strftime("%Y-%m-%dT%H:%M"), "datetime"
        return moment.date().isoformat(), "date"
    clock = _CLOCK.search(plain)
    day = day_named(plain, today, small_hours=small_hours)
    if day is None:
        raise PisaError(
            "validation_failed",
            f"{text[:60]!r} is not a day: say today, tomorrow, a weekday, YYYY-MM-DD, or none.",
            "Say the day differently",
        )
    if clock:
        return f"{day.isoformat()}T{int(clock.group(1)):02d}:{clock.group(2)}", "datetime"
    return day.isoformat(), "date"


# a task deleted by voice can be restored this long; then the maintenance job removes it.
DELETED_KEPT = timedelta(hours=24)
# A short yes (the answer to Pisa's one pending question): the user's whole message.
SHORT_YES_MAX_WORDS = 4
_YES = frozenset({"si", "yes", "ok", "okay", "confermo", "certo", "sicuro", "sure", "yep", "yeah", "confirm",
                  "procedi", "vai", "esatto", "proceed"})  # fmt: skip
_YES_FILLERS = _YES | frozenset({"va", "bene", "pure", "grazie", "cancellalo", "cancellala", "eliminalo",
                                 "eliminala", "delete", "it", "please", "thanks", "do", "pisa", "cancella",
                                 "elimina", "tutti", "tutte", "all", "them", "cancellali", "eliminali",
                                 "toglili", "toglile", "togli"})  # fmt: skip


def is_short_yes(text: str) -> bool:
    """ "Sì", "ok, cancellalo", "yes please", "va bene": at most four words, the first a yes (or "va bene"),
    the rest fillers. A sentence that says anything else is not a yes."""
    words = re.findall(r"[a-z]+", fold(text))
    if not words or len(words) > SHORT_YES_MAX_WORDS:
        return False
    first_ok = words[0] in _YES or words[:2] == ["va", "bene"]
    return first_ok and all(w in _YES_FILLERS for w in words)


# the pool's order: deadlines first, the nearest first; then the rest by priority; the
# oldest first among equals. Today's `todo` is its head (never a low one).
TODO_MAX = 5
_FAR = datetime.max.replace(tzinfo=UTC)


def pool_key(task: Task) -> tuple[bool, datetime, int, datetime]:
    return (task.due_at is None, task.due_at or _FAR, task.priority, task.created_at)


# as fixed by: the user's words ask Pisa to give THIS task a time:
# a planning verb as a command ("pianificalo", "inseriscilo oggi pomeriggio", "trovami un momento per…") or a
# polite request ("puoi pianificarlo?"), never negated, and tied to the task (a pronoun on the verb, or its
# title). "Aggiungi un task: programmare la lavatrice" adds a task; "puoi dirmi cosa ho in agenda?" asks.
_PLAN_STRONG = re.compile(
    r"(?:pianific|programm|incastr|schedul)(?:a|alo|ala|ali|ale|ami|amelo|amela|ameli|amele)"
)
_PLAN_LOOSE = re.compile(
    r"(?:mett|inserisc|fiss|infil|trov|organizz|piazz)(?:i|a|ilo|ila|ili|ile|alo|ala|ali|ale|ami|imi|"
    r"imelo|imela|ameli|amelo|amela|melo|mela|agli|igli)"
    # "fammelo fare oggi", "fallo oggi"
    r"|fa(?:mmel[oaie]|mmi|ll[oaie])"
)
_PLAN_INFINITIVE = re.compile(
    r"(?:pianificar|programmar|incastrar|inserir|metter|fissar|trovar|organizzar)(?:e|lo|la|li|le|mi|melo|mela)"
)
_PLAN_EN = re.compile(r"\b(?:schedule|plan|fit|slot|find|make|put|book)\b")
_PLAN_EN_CONTEXT = re.compile(
    r"\b(?:schedule (?:it|this|that|them)|plan (?:it|this|that|them)|fit (?:it|this|that|them) in|"
    r"slot (?:it|this|that|them) in|(?:find|make) (?:me )?(?:some )?time|put (?:it|this|that|them) in "
    r"(?:the|my) (?:plan|day|schedule)|schedule|plan)\b"
)
_WHEN_WORDS = frozenset(
    "oggi domani stasera stamattina pomeriggio mattina sera momento attimo tempo piano agenda quando spazio "
    "dopodomani today tomorrow tonight afternoon morning evening time".split()
)
_PLAN_NEGATION = frozenset({"non", "no", "mai", "senza", "not", "don't", "dont", "never", "nope"})
_POLITE = re.compile(r"\b(?:puoi|potresti|riesci a|riusciresti a|can you|could you|would you|kun je)\b")
_CLITIC = re.compile(r"(?:lo|la|li|le|melo|mela|meli|mele|gli)$")
# a short title ("Gym", "Bar") still names its task; these words never do
_TITLE_FILLER = frozenset("the and for per con una uno del della dei gli les una".split())
_EN_PRONOUN = frozenset({"it", "this", "that", "them"})
_TASK_ADD = re.compile(r"\b(?:aggiung\w*|crea\w*|add|nuovo task|new task|un task|a task)\b")


def asks_to_plan(owner_words: str, title: str = "") -> bool:
    """The user asks Pisa to put this task at a time. Pure."""
    plain = fold(owner_words.replace("\u2019", "'"))
    words = re.findall(r"[a-z']+", plain)
    if not words or _PLAN_NEGATION & set(words):
        return False  # "non pianificarlo", "don't schedule it", "no, lascialo nella lista"
    polite = bool(_POLITE.search(plain))
    if "?" in owner_words and not polite:
        return False  # "quando posso farlo?" asks when, it asks for nothing
    when = bool(_WHEN_WORDS & set(words))
    verbs = [w for w in words if _PLAN_STRONG.fullmatch(w) or (_PLAN_LOOSE.fullmatch(w) and when)]
    if polite:
        verbs += [w for w in words if _PLAN_INFINITIVE.fullmatch(w) and (when or _PLAN_STRONG.match(w))]
    english = bool(_PLAN_EN_CONTEXT.search(plain)) and bool(_PLAN_EN.search(plain))
    if not verbs and not english:
        return False
    if _TASK_ADD.search(plain) and not any(_CLITIC.search(w) for w in verbs):
        return False  # "aggiungi un task: programmare la lavatrice": the planning word is the new task's
    named = {w for w in re.findall(r"[a-z]+", fold(title)) if len(w) >= 3 and w not in _TITLE_FILLER} & set(
        words
    )  # "le camicie", "the gym"
    return any(_CLITIC.search(w) for w in verbs) or bool(_EN_PRONOUN & set(words)) or bool(named)


def todo(
    tasks: list[Task],
    now: datetime,
    timed: frozenset[str] | set[str] = frozenset(),
    pinned: frozenset[str] | set[str] = frozenset(),
) -> list[Task]:
    """The few open tasks that matter today, in the pool's order: none already timed in the plan, none
    low (never planned or reminded). A `pinned` task (the user's deadline today) is
    always in it, past TODO_MAX."""
    del now  # the pool's order is by deadline already
    # a delivery waits at its window on its own day (timed there); never in another day's todo
    picked = [
        t
        for t in sorted(tasks, key=pool_key)
        if t.status == "open" and t.id not in timed and t.priority < 3 and t.expected_from is None
    ]
    return [t for n, t in enumerate(picked) if n < TODO_MAX or t.id in pinned]


def todo_json(task: Task, timezone: str, lang: str) -> dict[str, object]:
    """A plan's `todo` item: the task, its deadline and the deadline's source, its priority, why (in their
    language: `reasons.task_reason`)."""
    tz = ZoneInfo(timezone)
    out: dict[str, object] = {"task_id": task.id, "title": task.title, "priority": task.priority}
    if task.due_at is not None:
        out["due_at"] = task.due_at.astimezone(tz).isoformat()
        out["deadline_source"] = task.deadline_source or ("email" if task.source_kind == "email" else "owner")
    if why := task_reason(task, lang):
        out["why"] = why
    return out


_MONTHS = month_forms(IT_MONTHS, EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS_SHORT)


def deadline_source(read: list[str], owner_words: str, due: str, today: date | None = None) -> str:
    """code's: where a deadline Pisa sets came from. The user's words when they hold that date
    (the date's own: its day with its month, "9/11", the date itself, or the day they named;
    never any number they said); else what this turn read: an email, a page or a search; else Pisa's
    estimate."""
    when = date.fromisoformat(due[:10])
    words = _plain_lower(owner_words)
    months = "|".join(_MONTHS[when.month])
    own = (
        due[:10] in words
        or re.search(rf"\b0?{when.day}\s+(?:di\s+)?(?:{months})\b", words) is not None
        or re.search(rf"\b(?:{months})\s+0?{when.day}\b", words) is not None
        or re.search(rf"\b0?{when.day}[/.-]0?{when.month}\b", words) is not None
        or (today is not None and day_named(owner_words, today) == when)
    )
    if own:
        return "owner"
    kinds = set(read)
    if kinds & {"email", "inbox", "mail"}:
        return "email"
    if kinds & {"page", "web", "search", "snippet", "results", "review", "reviews"}:
        return "web"
    return "pisa"
