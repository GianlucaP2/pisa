"""The riepilogo (the day's briefing as the app's Oggi shows it) as a view of the current plan. Pure: no I/O.

The morning's briefing was composed by a model once; what is left of the day changes with every task closed or
added and every event moved. So the riepilogo is derived, by code and with no model call, on every read: the
morning's tone (its summary line: the first of its sentences that names nothing of the day's items) and then
what the current plan still holds for today, in time order, and the open tasks it couldn't place.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ITEMS_MAX = 10
ALSO_MAX = 5
TITLE_MIN = 3  # a title this short never decides whether a sentence names an item
_ENDS = ".!?…"


@dataclass(frozen=True)
class Item:
    kind: str  # event | task
    title: str
    start: datetime | None
    ref: str | None = None  # the task's or the event's id


def sentences(text: str) -> list[str]:
    out, current = [], ""
    for ch in text:
        current += ch
        if ch in _ENDS:
            out.append(current.strip())
            current = ""
    if current.strip():
        out.append(current.strip())
    return [s for s in out if s]


def summary_line(morning: str, titles: Iterable[str]) -> str | None:
    """The morning's first sentence that names none of `titles` (the day's items, the ones still there and the
    ones gone since): its tone, never an item it listed then. None when every sentence names one."""
    names = [t.casefold() for t in titles if len(t.strip()) >= TITLE_MIN]
    for sentence in sentences(morning):
        folded = sentence.casefold()
        if not any(name in folded for name in names):
            return sentence
    return None


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def plan_items(plan: dict[str, Any], now: datetime) -> tuple[list[Item], list[Item]]:
    """What the plan still holds for today (its events and tasks not over yet, in its order, then the day's
    to-do with no time) and the open tasks it couldn't place."""
    left: list[Item] = []
    for item in plan.get("items") or []:
        kind = item.get("kind")
        if kind not in ("event", "task"):
            continue
        end = _at(item.get("end"))
        if end is not None and end <= now:
            continue
        ref = item.get("task_id") if kind == "task" else item.get("event_id")
        left.append(Item(kind, str(item.get("title") or "?"), _at(item.get("start")), ref))
    placed = {i.ref for i in left if i.ref}
    # the day's to-do with no time of its own (the pool's head: what Oggi lists), after the timed items
    left += [
        Item("task", str(t.get("title") or "?"), None, t.get("task_id"))
        for t in plan.get("todo") or []
        if t.get("task_id") not in placed
    ]
    also = [
        Item("task", str(u.get("title") or "?"), None, u.get("task_id")) for u in plan.get("unplanned") or []
    ]
    return left[:ITEMS_MAX], also[:ALSO_MAX]


def _time(at: datetime, zone: ZoneInfo, hour_cycle: str) -> str:
    local = at.astimezone(zone)
    if hour_cycle == "h12":
        return local.strftime("%I:%M %p").lstrip("0")
    return local.strftime("%H:%M")


def text(
    lang: str,
    summary: str | None,
    left: Sequence[Item],
    also: Sequence[Item],
    zone: ZoneInfo,
    hour_cycle: str,
) -> str:
    """The riepilogo's words: the morning's summary line, then what is left today."""
    it = lang == "it-IT"
    parts = [summary] if summary else []
    if left:
        listed = "; ".join(
            f"{_time(i.start, zone, hour_cycle)} {i.title}" if i.start is not None else i.title for i in left
        )
        parts.append(("Ancora oggi: " if it else "Still today: ") + listed + ".")
    if also:
        parts.append(("Anche: " if it else "Also: ") + "; ".join(i.title for i in also) + ".")
    if not left and not also:
        parts.append("Per oggi non resta altro." if it else "Nothing else is left for today.")
    return " ".join(parts)


def item_json(item: Item) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": item.kind, "title": item.title,
                           "start": item.start.isoformat() if item.start else None}  # fmt: skip
    if item.ref:
        out["task_id" if item.kind == "task" else "event_id"] = item.ref
    return out
