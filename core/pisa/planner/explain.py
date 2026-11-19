"""Why each task is where it is, and why a task didn't fit: one short deterministic sentence from
the deciding facts, no model. In their Settings language (Italian for it-IT, else
English)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

Urgency = Literal["overdue", "today", "later"]
CHAIN_MAX_MIN = 10  # two errands this close are "on the way"


@dataclass(frozen=True)
class TaskFacts:
    title: str
    priority: int  # 1 high … 3 low
    urgency: Urgency
    due_at: datetime | None
    due_kind: str  # none | date | datetime
    place: str | None


def _hhmm(moment: datetime, timezone: str) -> str:
    return f"{moment.astimezone(ZoneInfo(timezone)):%H:%M}"


def why(
    task: TaskFacts,
    timezone: str,
    lang: str,
    *,
    closes_at: datetime | None = None,
    next_to: str | None = None,
    next_to_minutes: int | None = None,
) -> str:
    """ "Due today; Poste Italiane closes at 13:35." ("Scade oggi; Poste Italiane chiude alle 13:35.") The
    deciding facts in order: deadline, opening hours, chaining, priority."""
    it = lang == "it-IT"
    parts: list[str] = []
    if task.urgency == "overdue":
        parts.append("Scaduto" if it else "Overdue")
    elif task.due_kind == "datetime" and task.due_at is not None and task.urgency == "today":
        at = _hhmm(task.due_at, timezone)
        parts.append(f"Entro le {at}" if it else f"Due by {at}")
    elif task.urgency == "today":
        parts.append("Scade oggi" if it else "Due today")
    if closes_at is not None and task.place:
        at = _hhmm(closes_at, timezone)
        parts.append(f"{task.place} chiude alle {at}" if it else f"{task.place} closes at {at}")
    if next_to is not None and next_to_minutes is not None and next_to_minutes <= CHAIN_MAX_MIN:
        parts.append(
            f"a {next_to_minutes} min da {next_to}" if it else f"{next_to_minutes} min from {next_to}"
        )
    if task.priority == 1:
        parts.append("priorità alta" if it else "high priority")
    if not parts:
        if task.place is None:
            parts.append("Ci sta nel tempo libero" if it else "Fits the free time")
        else:
            parts.append(
                f"Ci sta nel tempo libero vicino a {task.place}"
                if it
                else f"Fits the free time near {task.place}"
            )
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


def unplanned_reason(
    *,
    duration: int,
    windows: list[tuple[datetime, datetime]] | None,
    longest_free: int,
    deadline: datetime | None,
    plan_start: datetime,
    timezone: str,
    lang: str,
    busy: Sequence[tuple[datetime, datetime, str]] = (),
    solved: bool = True,
    errands_end: datetime | None = None,
) -> str:
    """Why a task is not in today's plan, in their language. `longest_free` and `busy` are the fixed day
    (events and the trips to them) before any task is placed."""
    it = lang == "it-IT"
    # In UTC: datetimes sharing one tzinfo compare by wall clock, which is wrong on DST nights.
    busy = [(s.astimezone(UTC), e.astimezone(UTC), t) for s, e, t in busy]
    if windows is not None:
        windows = [(a.astimezone(UTC), b.astimezone(UTC)) for a, b in windows]
    if not solved:
        return (
            "Pisa non ha finito il piano in tempo; chiedine uno nuovo."
            if it
            else "Pisa couldn't finish the plan in time; ask for a new one."
        )
    if errands_end is not None and _minutes(plan_start, errands_end) < duration:
        return (
            "Niente commissioni dopo mezzanotte: va nel piano del giorno dopo."
            if it
            else "No errands after midnight: it goes in the next day's plan."
        )
    if windows is not None and not windows:
        return "Chiuso per il resto della giornata." if it else "Closed for the rest of the day."
    if windows is not None and all(_minutes(a, b) < duration for a, b in windows):
        longest = max(_minutes(a, b) for a, b in windows)
        return (
            f"Servono {duration} min; è aperto al massimo {longest} min di fila per il resto della giornata."
            if it
            else f"Needs {duration} min; it is open at most {longest} min in a row for the rest of the day."
        )
    if deadline is not None and _minutes(plan_start, deadline) < duration:
        at = _hhmm(deadline, timezone)
        return (
            f"La scadenza ({at}) è troppo vicina per {duration} min."
            if it
            else f"Its deadline ({at}) is too close to fit {duration} min."
        )
    if windows is not None:
        long_enough = [(a, b) for a, b in windows if _minutes(a, b) >= duration]
        if all(_longest_gap(a, b, busy) < duration for a, b in long_enough):
            titles = list(dict.fromkeys(t for a, b in long_enough for s, e, t in busy if s < b and a < e))
            during = (" e " if it else " and ").join(titles)
            if len(long_enough) == 1:
                a, b = long_enough[0]
                span = f"{_hhmm(a, timezone)}-{_hhmm(b, timezone)}"
                return (
                    f"Aperto solo {span}, durante {during}." if it else f"Open only {span}, during {during}."
                )
            return f"Gli orari cadono durante {during}." if it else f"Its opening hours fall during {during}."
    if duration > longest_free:
        return (
            f"Servono {duration} min; il tempo libero più lungo rimasto oggi è {longest_free} min."
            if it
            else f"Needs {duration} min; the longest free time left today is {longest_free} min."
        )
    return (
        "Oggi non ci sta, accanto ai task più urgenti."
        if it
        else "Doesn't fit today next to the more urgent tasks."
    )


def _minutes(a: datetime, b: datetime) -> int:
    return int((b.astimezone(UTC) - a.astimezone(UTC)).total_seconds() // 60)


def _longest_gap(start: datetime, end: datetime, busy: Sequence[tuple[datetime, datetime, str]]) -> int:
    """The longest stretch of [start, end) that no busy span covers, in minutes."""
    longest, cursor = 0, start
    for s, e, _ in sorted(busy):
        if e <= cursor or s >= end:
            continue
        longest = max(longest, _minutes(cursor, min(s, end)))
        cursor = max(cursor, e)
    return max(longest, _minutes(cursor, end)) if cursor < end else longest
