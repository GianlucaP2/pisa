"""The day as a story: the rituals' rules, pure (with the hunches).

- **Settings**: `with_defaults`, `apply_patch` (each ritual can be turned off in Settings and by voice).
- **Ghost Run**: tomorrow's blocks rehearsed; `find_problem` keeps at most one problem worth fixing
  (a clash, an impossible transfer, a deadline with no block, rain on the bike day) and at most one fix, which
  changes only what the user alone is in (their own event with nobody invited, or a pool task's block).
- **One thing**: the morning's single sentence, from deadlines, the calendar and the pool.
- **Chiudiamo** and **the week's shape**: sheets from Pisa's own records; numbers by code
  only.
- **Place notes**: radius 50-500 m, one fire a visit (6 h cool-down), the note in their words.
- **Something different**: a free evening or weekend slot, never twice the same idea, a "no"
  remembered for that kind.
- **Hunches**: a guess with its evidence, asked once, never acted on until they confirm.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any, Literal

from pisa.domain import surprise, voice1
from pisa.domain.lexicon.weekdays import DAY_NAMES
from pisa.domain.talk import is_meeting
from pisa.domain.text import fold

DEFAULTS: dict[str, dict[str, Any]] = {
    "ghost_run": {"on": True, "at": "22:00"},
    "one_thing": {"on": True},
    "closing": {"on": True, "at": "21:30"},
    "week_poster": {"on": True},
    "something_different": {"on": True, "per_week": 2},
    "errands": {"on": True},  # one errand combo a day at most
    "walk_recap": {"on": True},  # what they said on a walk, once a day when it ends
    "life_map": {"on": True},  # the Map of the Life, each season
    # the morning sheet "Stanotte"; off until code opens it after 7 nights in shadow (with
    # an idea made and 100% of the shown evidence passing). They can switch it off; code never turns it back
    # on.
    "stanotte": {"on": False},
    # "Posso dirti una cosa?" (once a morning) and "Vuoi il tragitto?" (once a trip); they can turn
    # either off. Code never turns them off: their "basta" and 3 waved-away offers pause them (voice1_quiet)
    "knock": {"on": True},
    "tragitto": {"on": True},
    # il quarto d'ora, opt-in (off until they turn it on): offered only inside the knock, on a walk,
    # at most twice a week, a fixed end 15 minutes after they start (pisa.domain.voice3)
    "quarto": {"on": False},
    # il consiglio (a 15-minute spoken board meeting) and la prova generale (next week's
    # collisions), opt-in (off until they turn them on): on Sunday, only inside the knock (pisa.domain.voice4)
    "consiglio": {"on": False},
    "prova": {"on": False},
}
NAMES = tuple(DEFAULTS)
WEEK_POSTER_AT = time(19, 0)  # Sunday, local
DUE_WINDOW = timedelta(minutes=30)  # a ritual missed by the worker is still sent this long after its hour
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def with_defaults(raw: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out = {name: dict(value) for name, value in DEFAULTS.items()}
    for name, value in (raw or {}).items():
        if name in out and isinstance(value, dict):
            out[name].update({k: v for k, v in value.items() if k in out[name]})
    return out


def apply_patch(current: dict[str, dict[str, Any]], patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The user's change (Settings or their words); ValueError names the field. Unknown rituals are
    refused."""
    out = with_defaults(current)
    for name, value in patch.items():
        if name not in DEFAULTS:
            raise ValueError(f"unknown ritual {name!r} (known: {', '.join(NAMES)})")
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
        for key, item in value.items():
            if key not in DEFAULTS[name]:
                raise ValueError(f"{name}.{key} is not a setting")
            if key == "on" and not isinstance(item, bool):
                raise ValueError(f"{name}.on must be true or false")
            if key == "at" and not (isinstance(item, str) and _HHMM.match(item)):
                raise ValueError(f"{name}.at must be HH:MM")
            if key == "per_week" and (
                isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 7
            ):
                raise ValueError(f"{name}.per_week must be 0 to 7")
            out[name][key] = item
    return out


def at_time(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def due_at(local_now: datetime, at: time) -> datetime | None:
    """The ritual's moment today when it has come within DUE_WINDOW, else None."""
    moment = local_now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return moment if moment <= local_now < moment + DUE_WINDOW else None


# --- the user's words: turning a ritual on or off ---------------------------------------------------------

_OFF = re.compile(
    r"\b(spegni|disattiva|togli|basta|stop|niente pi[uù]|non voglio pi[uù]|turn off|disable|stop)\b"
)
_ON = re.compile(r"\b(accendi|attiva|riattiva|rimetti|turn on|enable)\b")
_RITUAL_WORDS: dict[str, tuple[str, ...]] = {
    "ghost_run": ("ghost run", "ghostrun", "prova di domani", "domani in 20 secondi"),
    "one_thing": ("one thing", "una cosa sola", "la cosa del mattino"),
    "closing": ("chiudiamo", "chiusura", "riepilogo della sera", "closing"),
    "week_poster": ("poster", "forma della settimana", "shape of your week", "settimana in un poster"),
    "something_different": ("qualcosa di diverso", "something different", "idee nuove"),
    "errands": ("commissioni", "commissione", "giro delle commissioni", "errands", "errand"),
    "walk_recap": (
        "riepilogo della camminata",
        "riepilogo della passeggiata",
        "recap della camminata",
        "dopo la camminata",
        "dopo la passeggiata",
        "walk recap",
    ),
    "life_map": ("mappa della vita", "mappa della mia vita", "map of the life", "map of my life"),
}


plain = fold


def said_switch(words: str, ritual: str, on: bool) -> bool:
    """RAIL: a ritual is switched only by the user's own words this turn naming it and the switch."""
    text = plain(words)
    named = any(plain(w) in text for w in _RITUAL_WORDS.get(ritual, ()))
    return named and bool((_ON if on else _OFF).search(text))


# --- Ghost Run -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """One thing tomorrow: an event or a pool task's block."""

    kind: Literal["event", "task"]
    ref: str  # event id | task id
    title: str
    start: datetime
    end: datetime
    place_id: str | None = None
    place_name: str | None = None
    movable: bool = False  # their own event with nobody invited (or a task's block): a fix may move it
    calendar_id: str | None = None


@dataclass(frozen=True)
class DueTask:
    task_id: str
    title: str
    due_at: datetime
    minutes: int  # its duration, or the default block


@dataclass(frozen=True)
class Fix:
    kind: Literal["move_event", "task_block"]
    ref: str
    title: str
    start: datetime
    end: datetime
    calendar_id: str | None = None
    was_start: datetime | None = None  # where it stood when Pisa proposed it (refused once it moved)

    def json(self) -> dict[str, Any]:
        return {
            "was_start": self.was_start.isoformat() if self.was_start else None,
            "kind": self.kind,
            "ref": self.ref,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "calendar_id": self.calendar_id,
        }


@dataclass(frozen=True)
class Problem:
    kind: Literal["clash", "transfer", "deadline", "rain"]
    line: str  # what is wrong, one sentence
    fix: Fix | None = None
    question: str | None = None  # the one fix as a question ("Sposto la palestra alle 19?")


DAY_FROM, DAY_TO = time(8, 0), time(22, 0)  # a fix never lands outside these hours
TASK_BLOCK_MIN = 30
STEP = timedelta(minutes=15)


def _hm(moment: datetime) -> str:
    return f"{moment:%H:%M}".removeprefix("0") if moment.minute else f"{moment.hour}"


def _round_up(moment: datetime) -> datetime:
    extra = (-(moment.minute % 15)) % 15
    return (moment + timedelta(minutes=extra)).replace(second=0, microsecond=0)


def free_slot(
    blocks: list[Block], minutes: int, after: datetime, *, ignore: str | None = None, travel_pad: int = 0
) -> datetime | None:
    """The first start ≥ `after` (on 15-minute steps, inside DAY_FROM-DAY_TO of that day) where `minutes` fit
    with no other block, `travel_pad` minutes clear on each side."""
    day = after.date()
    start = max(
        _round_up(after), after.replace(hour=DAY_FROM.hour, minute=DAY_FROM.minute, second=0, microsecond=0)
    )
    last = after.replace(hour=DAY_TO.hour, minute=DAY_TO.minute, second=0, microsecond=0)
    others = [b for b in blocks if b.ref != ignore]
    pad = timedelta(minutes=travel_pad)
    length = timedelta(minutes=minutes)
    while start + length <= last and start.date() == day:
        if all(start + length + pad <= b.start or start >= b.end + pad for b in others):
            return start
        start += STEP
    return None


_LINES: dict[str, dict[str, str]] = {
    "it-IT": {
        "clash": "{a} alle {at} si sovrappone a {b}.",
        "transfer": "Da {a} a {b} servono {need} minuti, ne hai {have}.",
        "deadline": "{task} scade domani e non ha un momento.",
        "rain": "Domani piove alle {at} e vai in bici.",
        "move": "Sposto {title} alle {at}?",
        "block": "Ti tengo {title} alle {at}?",
        "push": "Domani, in 20 secondi.",
        "push_fix": "Domani, in 20 secondi: {line}",
        "title": "Domani",
        "fine": "Domani fila tutto.",
    },
    "en-US": {
        "clash": "{a} at {at} overlaps {b}.",
        "transfer": "From {a} to {b} takes {need} minutes; you have {have}.",
        "deadline": "{task} is due tomorrow and has no time set aside.",
        "rain": "Tomorrow it rains at {at} and you go by bike.",
        "move": "Shall I move {title} to {at}?",
        "block": "Shall I hold {at} for {title}?",
        "push": "Tomorrow, in 20 seconds.",
        "push_fix": "Tomorrow, in 20 seconds: {line}",
        "title": "Tomorrow",
        "fine": "Tomorrow runs smoothly.",
    },
}


def line(lang: str, key: str, **values: Any) -> str:
    return _LINES.get(lang, _LINES["en-US"])[key].format(**values)


def _low(title: str) -> str:
    return title[:1].lower() + title[1:] if title[:2] != title[:2].upper() else title


def find_problem(
    blocks: list[Block],
    *,
    travel: dict[tuple[str, str], int],
    due: list[DueTask],
    rain_at: datetime | None,
    bike: bool,
    lang: str,
) -> Problem | None:
    """At most one problem worth saying, the most serious first (a clash, an impossible transfer, a deadline
    with no block, rain on the bike day), and at most one fix: a move of the user's own event with nobody
    invited, or a block for a task, to a free slot (never one that makes a new clash). `travel`: minutes
    between two blocks' refs that both have a place."""
    timed = sorted(blocks, key=lambda b: b.start)
    for a, b in pairwise(timed):
        if b.start < a.end:  # a clash: the later-starting one moves, if it is theirs alone, else the other
            mover = b if b.movable else a if a.movable else None
            text = line(lang, "clash", a=b.title, at=_hm(b.start), b=_low(a.title))
            if mover is None:
                return Problem("clash", text)
            fixed = _moved(timed, mover, after=(a if mover is b else b).end, lang=lang)
            return Problem("clash", text, *fixed) if fixed else Problem("clash", text)
    for a, b in pairwise(timed):
        need = travel.get((a.ref, b.ref))
        have = int((b.start - a.end).total_seconds() // 60)
        if need is not None and need > have:
            text = line(lang, "transfer", a=_low(a.title), b=_low(b.title), need=need, have=max(have, 0))
            if not b.movable:
                return Problem("transfer", text)
            fixed = _moved(timed, b, after=a.end + timedelta(minutes=need), lang=lang)
            return Problem("transfer", text, *fixed) if fixed else Problem("transfer", text)
    for task in due:
        if any(b.kind == "task" and b.ref == task.task_id for b in timed):
            continue
        text = line(lang, "deadline", task=task.title)
        day = task.due_at - timedelta(minutes=1)  # a `date` deadline is the next midnight: its own day
        start = free_slot(
            timed, task.minutes, day.replace(hour=DAY_FROM.hour, minute=0, second=0, microsecond=0)
        )
        if start is None or start + timedelta(minutes=task.minutes) > task.due_at:
            return Problem("deadline", text)
        end = start + timedelta(minutes=task.minutes)
        fix = Fix("task_block", task.task_id, task.title, start, end)
        return Problem("deadline", text, fix, line(lang, "block", title=_low(task.title), at=_hm(start)))
    if bike and rain_at is not None:
        return Problem("rain", line(lang, "rain", at=_hm(rain_at)))
    return None


def _moved(timed: list[Block], mover: Block, *, after: datetime, lang: str) -> tuple[Fix, str] | None:
    minutes = int((mover.end - mover.start).total_seconds() // 60)
    start = free_slot(timed, minutes, after, ignore=mover.ref)
    if start is None:
        return None
    fix = Fix(
        "move_event" if mover.kind == "event" else "task_block",
        mover.ref,
        mover.title,
        start,
        start + timedelta(minutes=minutes),
        mover.calendar_id,
        mover.start,
    )
    return fix, line(lang, "move", title=_low(mover.title), at=_hm(start))


def ghost_blocks(
    lang: str, blocks: list[Block], problem: Problem | None, leave: dict[str, datetime], weather: str | None
) -> dict[str, Any]:
    """Tomorrow as a sheet (blocks): a timeline table, the leave-at times, the weather, the one problem
    and its fix as the sheet's question."""
    it = lang == "it-IT"
    rows = []
    for b in sorted(blocks, key=lambda b: b.start)[:20]:
        left = leave.get(b.ref)
        rows.append(
            [
                f"{b.start:%H:%M}-{b.end:%H:%M}",
                b.title[:80],
                (b.place_name or "")[:80],
                f"{left:%H:%M}" if left else "",
            ]
        )
    out: list[dict[str, Any]] = []
    if rows:
        cols = ["Quando", "Cosa", "Dove", "Esci"] if it else ["When", "What", "Where", "Leave"]
        out.append({"type": "table", "columns": cols, "rows": rows})
    if weather:
        out.append({"type": "fact", "label": "Meteo" if it else "Weather", "value": weather[:120]})
    if problem is not None:
        out.append({"type": "section", "title": "Da sistemare" if it else "To fix"})
        out.append({"type": "text", "text": problem.line[:600]})
        if problem.question:
            out.append({"type": "text", "text": problem.question[:600]})
    else:
        out.append({"type": "text", "text": line(lang, "fine")})
    return {"title": line(lang, "title"), "blocks": out}


# --- One thing -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Thing:
    title: str
    at: datetime | None = None  # its time today, when it has one


def one_thing(
    lang: str, *, overdue: list[Thing], due_today: list[Thing], events: list[Thing], todo: list[Thing]
) -> str | None:
    """The single thing that matters today, in one sentence: a deadline already passed, else one due today,
    else the next timed event, else the pool's first task; an empty day says so."""
    it = lang == "it-IT"
    if overdue:
        t = overdue[0]
        return (
            f"Una cosa sola oggi: {_low(t.title)}, è già in ritardo."
            if it
            else (f"One thing today: {t.title}, it's already overdue.")
        )
    if due_today:
        t = due_today[0]
        when = f" entro le {_hm(t.at)}" if it and t.at else ""
        when_en = f" by {t.at:%H:%M}" if t.at else ""
        return (
            f"Una cosa sola oggi: {_low(t.title)}, scade oggi{when}."
            if it
            else (f"One thing today: {t.title}, due today{when_en}.")
        )
    timed = [e for e in events if e.at is not None]
    if timed:
        t = timed[0]
        assert t.at is not None
        return (
            f"Una cosa sola oggi: {_low(t.title)} alle {_hm(t.at)}."
            if it
            else (f"One thing today: {t.title} at {t.at:%H:%M}.")
        )
    if todo:
        return f"Una cosa sola oggi: {_low(todo[0].title)}." if it else f"One thing today: {todo[0].title}."
    return None  # an empty day: the briefing's own sentence


# --- Chiudiamo -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DoneLine:
    text: str
    undo: dict[str, str] | None = None  # {"do": "undo_approval" | "undo_done" | "undo_task", "ref": id}


def closing_sheet(
    lang: str, did: list[DoneLine], moved: list[DoneLine], still_open: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """(the sheet's data, its actions with their effects), or None when the day left nothing to say. Each
    line is in one list only; an Undo button for each line that has one (8 at most)."""
    if not (did or moved or still_open):
        return None
    it = lang == "it-IT"
    blocks: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if did:
        blocks.append({"type": "section", "title": "Oggi ho fatto" if it else "What I did today"})
        blocks.append({"type": "list", "items": [d.text[:200] for d in did[:8]]})
    if moved:
        blocks.append({"type": "section", "title": "Cosa si è mosso" if it else "What moved"})
        blocks.append({"type": "list", "items": [m.text[:200] for m in moved[:8]]})
    for d in [*did[:8], *moved[:8]]:
        if d.undo is not None and len(actions) < 8:
            n = len(actions) + 1
            text = "".join(c for c in d.text if c.isprintable())  # no control characters
            label = (f"Annulla: {text}" if it else f"Undo: {text}")[:40]
            actions.append({"id": f"a{n}", "label": label, "style": "undo", **d.undo})
    if still_open:
        blocks.append({"type": "section", "title": "Aperto per domani" if it else "Open for tomorrow"})
        blocks.append({"type": "list", "items": [s[:200] for s in still_open[:8]]})
    return {"title": "Chiudiamo?" if it else "Shall we close the day?", "blocks": blocks}, actions


# --- the week's shape ------------------------------------------------------------------------------

AWAKE_HOURS_A_DAY = 16
_GYM = re.compile(
    r"\b(palestra|gym|allenamento|workout|corsa|run|running|nuoto|swim|yoga|pilates|crossfit)\b"
)
_TRAVEL = re.compile(r"\b(volo|flight|treno|train|viaggio|trip|aeroporto|airport|transfer)\b")


def kind_of(title: str, attendees: int) -> Literal["meetings", "gym", "travel", "other"]:
    text = plain(title)
    if _GYM.search(text):
        return "gym"
    if _TRAVEL.search(text):
        return "travel"
    if attendees > 0 or is_meeting(title):
        return "meetings"
    return "other"


@dataclass(frozen=True)
class WeekEvent:
    title: str
    start: datetime
    end: datetime
    attendees: int = 0


@dataclass
class Shape:
    hours: dict[str, float] = field(default_factory=dict)
    gym_weeks: int = 0  # of the last 4 (this one included), the weeks with the gym
    gym_now: int = 0  # gym sessions this week
    done: int = 0
    busiest: date | None = None
    busiest_hours: float = 0.0


def week_shape(events: list[WeekEvent], gym_by_week: list[int], done: int) -> Shape:
    """Hours by kind (meetings, gym, travel, other, free: the waking hours left), the gym streak (weeks with
    the gym of the last 4, this one last in `gym_by_week`), the tasks done, the busiest day. Code's numbers
    only (no model ever writes one)."""
    shape = Shape(hours={"meetings": 0.0, "gym": 0.0, "travel": 0.0, "other": 0.0}, done=done)
    per_day: dict[date, float] = {}
    for e in events:
        hours = max(0.0, (e.end - e.start).total_seconds() / 3600)
        shape.hours[kind_of(e.title, e.attendees)] += hours
        per_day[e.start.date()] = per_day.get(e.start.date(), 0.0) + hours
    busy = sum(shape.hours.values())
    shape.hours["free"] = max(0.0, 7 * AWAKE_HOURS_A_DAY - busy)
    shape.hours = {k: round(v, 1) for k, v in shape.hours.items()}
    recent = gym_by_week[-4:]
    shape.gym_weeks = sum(1 for n in recent if n > 0)
    shape.gym_now = recent[-1] if recent else 0
    if per_day:
        shape.busiest, shape.busiest_hours = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))
        shape.busiest_hours = round(shape.busiest_hours, 1)
    return shape


def poster(lang: str, shape: Shape, weeks: int) -> dict[str, Any]:
    """The week as a poster-like sheet: one stat per kind, the streak, one highlight."""
    it = lang == "it-IT"
    names = (
        {
            "meetings": "Riunioni",
            "gym": "Palestra",
            "travel": "Spostamenti",
            "other": "Altro",
            "free": "Libero",
        }
        if it
        else {"meetings": "Meetings", "gym": "Gym", "travel": "Travel", "other": "Other", "free": "Free"}
    )
    blocks: list[dict[str, Any]] = [{"type": "section", "title": "Le ore" if it else "The hours"}]
    for kind in ("meetings", "gym", "travel", "other", "free"):
        value = shape.hours.get(kind, 0.0)
        blocks.append({"type": "stat", "label": names[kind], "value": f"{value:g}", "unit": "h"})
    blocks.append({"type": "section", "title": "Le costanti" if it else "The streaks"})
    blocks.append(
        {
            "type": "stat",
            "label": "Palestra" if it else "Gym",
            "value": f"{shape.gym_weeks} {'su' if it else 'of'} {weeks}",
            "unit": "sett." if it else "wk",
            "trend": "up" if shape.gym_now > 0 else "down",
        }
    )
    blocks.append(
        {
            "type": "stat",
            "label": "Fatti" if it else "Done",
            "value": str(shape.done),
            "unit": "attività" if it else "tasks",
        }
    )
    if shape.busiest is not None:
        day = DAY_NAMES.get(lang, DAY_NAMES["en-US"])[shape.busiest.weekday()]
        text = (
            f"Il giorno più pieno: {day}, {shape.busiest_hours:g} ore di impegni."
            if it
            else f"The fullest day: {day}, {shape.busiest_hours:g} hours booked."
        )
        blocks.append({"type": "text", "text": text})
    return {"title": "La forma della tua settimana" if it else "The shape of your week", "blocks": blocks}


# --- place notes ------------------------------------------------------------------------------

RADIUS_MIN, RADIUS_MAX, RADIUS_DEFAULT = 50, 500, 150
COOL_DOWN = timedelta(hours=6)  # one fire a visit
NOTES_MAX = 100
REGIONS = 20  # the iPhone's region monitors: the phone registers the 20 nearest
_STOP = frozenset(
    "il lo la i gli le un una uno di da a al alla allo ai agli alle del della dei che e ed per con in su "
    "mi ti ricordami ricordarmi quando sono vado passo arrivo the a an of to at when i remind me am".split()
)


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", plain(text)) if len(w) > 2 and w not in _STOP}


def in_owner_words(note: str, words: str) -> bool:
    """RAIL: a note is the user's own words: most of its words were said by them this turn."""
    mine, said = content_words(note), content_words(words)
    return bool(mine) and len(mine & said) * 3 >= len(mine) * 2


_HERE = frozenset({"qui", "here"})


def place_in_words(place: str, words: str) -> bool:
    """RAIL: the note's place is one they named this turn ("Esselunga", "qui"), every
    word of it said."""
    if plain(place).strip() in _HERE:
        return bool(_HERE & set(re.findall(r"[a-z]+", plain(words))))
    named = content_words(place)
    return bool(named) and named <= content_words(words)


def may_fire(last: datetime | None, now: datetime) -> bool:
    return last is None or now - last >= COOL_DOWN


def note_line(lang: str, place: str | None, text: str) -> str:
    where = place or ("qui" if lang == "it-IT" else "here")
    return f"Sei da {where}: {text}." if lang == "it-IT" else f"You're at {where}: {text}."


def task_line(lang: str, title: str) -> str:
    return (
        f"{title[:1].upper() + title[1:]}: è qui, 2 minuti?"
        if lang == "it-IT"
        else f"{title}: it's here, 2 minutes?"
    )


# --- something different --------------------------------------------------------------------

IDEA_FREE_FOR = timedelta(hours=3)
EVENING = (time(18, 0), time(21, 0))
WEEKEND = (time(10, 0), time(18, 0))


def idea_slot(local_now: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    """A free evening (18-21 on a weekday) or weekend slot (10-18): nothing booked now or in the next 3 hours
    (never during a work block)."""
    window = WEEKEND if local_now.weekday() >= 5 else EVENING
    if not window[0] <= local_now.time() < window[1]:
        return False
    until = local_now + IDEA_FREE_FOR
    return all(end <= local_now or start >= until for start, end in busy)


def idea_question(lang: str, idea: str) -> str:
    return (
        f"Vuoi provare qualcosa di diverso oggi? Avevo in mente: {idea}."
        if lang == "it-IT"
        else f"Fancy something different today? I had in mind: {idea}."
    )


def idea_key(kind: str, ref: str) -> str:
    return f"{kind}:{plain(ref).strip()}"[:200]


# --- hunches ---------------------------------------------------------------------------------------

HUNCH_ASK_KEPT = timedelta(hours=12)
HUNCH_KINDS = ("like", "dislike", "routine", "style")  # the claim's kind once they confirm (never sensitive)


def hunch_key(text: str) -> str:
    return " ".join(sorted(content_words(text)))[:200] or plain(text)[:200]


def hunch_question(text: str) -> str:
    """A hunch is asked as a question, once ("ti piace il sushi il venerdì?").
    One question, its first; the turn's one question is the one their yes answers."""
    text = text.strip().rstrip(".!")
    first = text.find("?")
    if 0 <= first < len(text) - 1:
        text = text[: first + 1]
    # only the sentence that asks, never a statement before it (no fact about them)
    text = re.split(r"(?<=[.!?…])\s+", text)[-1]
    return text if text.endswith("?") else text + "?"


def hunch_states(text: str) -> bool:
    """The guess says something about them as a fact before (or instead of) asking it
    ("Ti sei lasciato con Giulia. Il venerdì esci da solo?"): Pisa never stores or says that."""
    head = text.strip().split("?", 1)[0]
    return re.search(r"[.!…;:]\s+\S", head) is not None


# their words say a habit and a model may ask its guess in words, never calling
# note_hunch, so their "sì" answered nothing. The reply's own yes-or-no question about their habit or taste
# counts as the hunch it is (Pisa perceives and asks; only their yes makes it theirs). Never an offer ("vuoi
# che ti cerchi un posto?"), never a question asking what, where or when, never one about anything but their
# habits.
_WH_QUESTION = re.compile(
    r"^\W*(?:e\s+)?(?:dove|quando|quale|quali|cosa|che\s+cosa|come|chi|perch[eé]|quant[oaie]|"
    r"where|what|which|when|how|who|why)\b",
    re.IGNORECASE,
)
_OFFER_QUESTION = re.compile(
    r"\b(?:vuoi|volete|ti\s+va|preferisci|devo|posso|faccio|cerco|ti\s+cerco|ti\s+trovo|ti\s+segno|"
    r"ti\s+metto|lo\s+aggiungo|la\s+aggiungo|shall\s+i|should\s+i|do\s+you\s+want|want\s+me|"
    r"would\s+you\s+like|can\s+i)\b",
    re.IGNORECASE,
)
_HABIT_QUESTION = re.compile(
    r"\b(?:abitudin\w*|tradizion\w*|rit[oi]|rituale|solit[oaie]|sempre|ogni|spesso|di\s+solito|ormai|"
    r"preferit[oaie]|ti\s+piace|ti\s+piacciono|habit|usually|always|every|often|tradition|ritual|routine|"
    r"favou?rite|you\s+like|you\s+love)\b",
    re.IGNORECASE,
)


# about them (second person), never Pisa's own act or offer ("Prenoto da Sushi Ko come
# sempre?", "Lo sposto alle 17 come sempre?", "Ordino il solito?"), never their feelings, people, drink, money
# or health (those reach them only as a question they can wave away, never as a claim their "sì" confirms).
_ABOUT_HIM = re.compile(
    r"\b(?:ti|te|tu|tuo|tua|tuoi|tue|sei|hai|vai|fai|prendi|mangi|esci|ami|preferisci|scegli|ordini|torni|"
    r"you|your|you're|you've)\b",
    re.IGNORECASE,
)
_PISA_ACTS = re.compile(
    r"\b(?:prenoto|ordino|sposto|segno|metto|aggiungo|cerco|trovo|chiamo|scrivo|mando|preparo|ricordo|tengo|"
    r"faccio|organizzo|riservo|fisso|programmo|imposto|tolgo|cancello|"
    r"te\s?l[oaie]|gliel[oaie]|ce\s?l[oaie]|l[oaie]\s+\w+o|i'll|i\s+will|let\s+me|shall\s+i|should\s+i|"
    r"can\s+i|book|order|move|add|put|call)\b",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"\b(?:gi[uù]|trist\w*|stress\w*|ansi\w*|ansios\w*|depress\w*|da\s+sol[oaie]|solitud\w*|litig\w*|ex|"
    r"lasciat\w*|fidanzat\w*|ragazz[oa]|moglie|marito|partner|single|bev\w*|alcol\w*|vino|birr\w*|drink\w*|"
    r"spend\w*|spes[oaie]\s+troppo|soldi|debit\w*|money|salute|malat\w*|dolor\w*|medic\w*|farmac\w*|"
    r"peso|dieta|sonno|dorm\w*|health|sad|down|lonely|anxious|angry|arrabbiat\w*|fight\w*|broke|"
    r"girlfriend|boyfriend|wife|husband|ill|sick|"
    # the body and health in plain words, never a hunch (Pisa never diagnoses)
    r"mal\s+di\s+\w+|male\s+a\w*|emicrani\w*|febbr\w*|influenz\w*|raffreddat\w*|nause\w*|stanc\w*|"
    r"esaust\w*|insonni\w*|pillol\w*|terapi\w*|ansiolitic\w*|headache\w*|migraine\w*|pain|tired|"
    r"exhausted|insomnia|pills?|therapy)\b",
    re.IGNORECASE,
)


def sensitive(text: str) -> bool:
    """The text touches their feelings, people, drink, money or health (keeps such a line
    off the morning sheet)."""
    return bool(_SENSITIVE.search(text))


# A guess that names another person ("Esci spesso con Marta…?") is never stored:
# what they do with whom is theirs to say, not Pisa's to keep from a yes.
_NAMES_SOMEONE = re.compile(r"(?<!^)(?<![.!?:]\s)\b[A-ZÀ-Ý][a-zà-ÿ]+\b")
_EITHER = re.compile(r"\b(?:o|oppure|or)\b", re.IGNORECASE)
_SENTENCES = re.compile(r"[^.!?…]+[.!?…]*")


def asked_guess(reply: str, words: str = "") -> tuple[str, str] | None:
    """The reply's one question when it is a yes-or-no guess about their habit or taste: (the question, its
    kind), or None. When in doubt, None: exactly one "?" in the reply; about them in the second
    person; never Pisa's act or offer, never an either/or, never a what/where/when; nothing sensitive in it or
    in their words; and no sentence of the reply states their habit as a fact."""
    if reply.count("?") != 1 or _SENSITIVE.search(words) or _SENSITIVE.search(reply):
        return None
    if _NAMES_SOMEONE.search(reply.split("?")[0].rsplit(".", 1)[-1].strip()):
        return None
    parts = [p.strip() for p in _SENTENCES.findall(reply) if p.strip()]
    asked = next((p for p in parts if p.endswith("?")), "")
    question = asked.rsplit(":", 1)[-1].strip()  # "Ti chiedo solo una cosa: il venerdì …?": what it asks
    if (
        not question
        or _WH_QUESTION.search(question)
        or _OFFER_QUESTION.search(question)
        or _PISA_ACTS.search(question)
        or _EITHER.search(question)
        or not _ABOUT_HIM.search(question)
        or not _HABIT_QUESTION.search(question)
    ):
        return None
    stated = [p for p in parts if not p.endswith("?")] + [asked.rsplit(":", 1)[0]] * (":" in asked)
    if any(_ABOUT_HIM.search(p) and _HABIT_QUESTION.search(p) for p in stated):
        return None  # "È ormai la tua tradizione. Ti piace …?" states it first
    taste = re.search(r"\b(?:ti\s+piac\w*|you\s+(?:like|love))\b", question, re.IGNORECASE)
    return question, "like" if taste else "routine"


# --- the answers to Pisa's questions
# --------------------------------------------------------------------------


def answered(kind: str, yes: bool, lang: str, result: str | None = None) -> str:
    it = lang == "it-IT"
    if kind == "thought":  # the morning thought's question (yes asks for the step, nothing changes)
        return surprise.answered(yes, lang, result)
    if kind in (
        "knock",
        "tragitto",
    ):  # their yes opens it; their no closes it (never asked again today)
        if not yes:
            return voice1.word(voice1.DECLINED, lang)
        return result or voice1.word(voice1.NOTHING if kind == "knock" else voice1.NO_TRIP, lang)
    if kind == "hunch":
        return (
            ("Segnato, grazie." if yes else "Ok, me lo tolgo dalla testa.")
            if it
            else ("Noted, thanks." if yes else "Okay, I'll drop it.")
        )
    if kind == "idea":
        if yes:
            return result or ("Bene, divertiti." if it else "Great, enjoy.")
        return (
            "Va bene, per questo genere non te lo chiedo più."
            if it
            else "Fine, I won't suggest that kind again."
        )
    if yes:
        return result or ("Fatto." if it else "Done.")
    return "Va bene, lascio così." if it else "Okay, I'll leave it."


# What Pisa says when the user's yes to a fix couldn't be done (the event changed meanwhile);
# the brain's tool reads it as not done
COULDNT = {
    "it-IT": "Non ci sono riuscito: è cambiato qualcosa.",
    "en-US": "I couldn't: something changed.",
}
