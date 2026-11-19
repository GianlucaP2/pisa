"""Tasks in bulk: one change for a set of open tasks the user named by a closed filter, said back
as a count, done on one yes, undone at once. Pure rules, no I/O.

- **Filters** (closed: never free text): all, overdue, low priority, from a sender, older than N days, a
  source, by ids.
- **Actions**: done, dismiss, delete (delete: gone from every list, restorable for 24 h).
- RAIL: a set changed by Pisa is never larger than the count Pisa said; one Undo restores the whole set for 24
  h.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain.tasks import LOW, Task

Filter = Literal["all", "overdue", "low_priority", "from_sender", "older_than", "source", "ids"]
Action = Literal["done", "dismiss", "delete"]
FILTERS: tuple[Filter, ...] = ("all", "overdue", "low_priority", "from_sender", "older_than", "source", "ids")
ACTIONS: tuple[Action, ...] = ("done", "dismiss", "delete")
SOURCES = ("voice", "email", "manual", "calendar")  # calendar, the date reminders
UNDO_KEPT = timedelta(hours=24)
IDS_MAX = 500  # one REST call
OLDER_THAN_MAX_DAYS = 3650
STATUS = {"done": "done", "dismiss": "dismissed", "delete": "deleted"}


def overdue(task: Task, now: datetime, timezone: str) -> bool:
    if task.due_at is None:
        return False
    if task.due_kind == "date":
        today: date = now.astimezone(ZoneInfo(timezone)).date()
        return task.due_at.astimezone(ZoneInfo(timezone)).date() < today
    return task.due_at < now


Group = Literal["overdue", "today", "week", "later", "undated"]
GROUPS: tuple[Group, ...] = ("overdue", "today", "week", "later", "undated")


def group_of(task: Task, now: datetime, timezone: str) -> Group:
    """Where an open task sits in the user's lists: overdue, today, the next 7 days, later, no date."""
    if task.due_at is None:
        return "undated"
    if overdue(task, now, timezone):
        return "overdue"
    tz = ZoneInfo(timezone)
    days = (task.due_at.astimezone(tz).date() - now.astimezone(tz).date()).days
    return "today" if days <= 0 else "week" if days <= 7 else "later"


def select(
    tasks: Sequence[Task],
    which: Filter,
    *,
    now: datetime,
    timezone: str,
    days: int | None = None,
    source: str | None = None,
    ids: Sequence[str] = (),
    senders: dict[str, str] | None = None,  # task id → the email's sender (name and address), for from_sender
    sender: str | None = None,
) -> list[Task]:
    """The open tasks a filter names (the closed set only; an unusable value names none)."""
    open_tasks = [t for t in tasks if t.status == "open"]
    if which == "all":
        return open_tasks
    if which == "overdue":
        return [t for t in open_tasks if overdue(t, now, timezone)]
    if which == "low_priority":
        return [t for t in open_tasks if t.priority == LOW]
    if which == "older_than":
        if days is None or not 1 <= days <= OLDER_THAN_MAX_DAYS:
            return []
        return [t for t in open_tasks if t.created_at < now - timedelta(days=days)]
    if which == "source":
        return [t for t in open_tasks if source in SOURCES and t.source_kind == source]
    if which == "from_sender":
        needle = " ".join((sender or "").casefold().split())
        if len(needle) < 3:
            return []
        return [t for t in open_tasks if needle in (senders or {}).get(t.id, "").casefold()]
    wanted = set(ids)
    return [t for t in open_tasks if t.id in wanted]


def count_text(n: int, action: Action, lang: str) -> str:
    """The count said back before the yes ("Sono 23: li tolgo tutti?")."""
    if lang == "it-IT":
        verb = {"done": "li segno fatti", "dismiss": "li tolgo", "delete": "li cancello"}[action]
        return f"Sono {n}: {verb} tutti?" if n != 1 else f"È uno solo: {verb.replace('li ', 'lo ')}?"
    verb = {"done": "mark them all done", "dismiss": "drop them all", "delete": "delete them all"}[action]
    return f"That's {n}: {verb}?"
