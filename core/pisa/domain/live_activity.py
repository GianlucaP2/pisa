"""The lock screen's Live Activity "Next": the next stop of today's plan, as the APNs
content-state. Pure rules, no I/O.

- **The next item**: the first event, task or free time of the plan that hasn't ended (an event under way is
  still the next stop), leaving out what is skipped, done or missed; the trip into it gives `leave_at`,
  `minutes` and `mode`, and how late the arrival is. Nothing left: kind `none`.
- **Priority**: 10 when the next stop or its leave time changes (the phone shows it at once), else 5.
"""

from datetime import datetime
from typing import Any

ATTRIBUTES_TYPE = "NextStopAttributes"  # the app's ActivityAttributes type (no fields), for push-to-start
GONE = ("skipped", "done", "missed")
_FREE = {"it-IT": "Tempo libero", "en-US": "Free time"}
_NONE = {"it-IT": "Niente altro oggi", "en-US": "Nothing else today"}


def _at(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def content_state(
    items: list[dict[str, Any]], now: datetime, places: dict[str, str], lang: str, day_end: datetime
) -> dict[str, Any]:
    """The content-state for the plan's items (with statuses). `places`: place id → the line to
    show (its address, else its name)."""
    previous: dict[str, Any] | None = None
    for item in items:
        if item["kind"] == "travel":
            previous = item
            continue
        if item["kind"] not in ("event", "task", "free") or _at(item["end"]) <= now:
            previous = None
            continue
        if item.get("status") in GONE:
            previous = None
            continue
        trip = previous if previous is not None and _at(previous["end"]) <= _at(item["end"]) else None
        late = 0
        if trip is not None and item["kind"] != "free":
            late = max(0, int((_at(trip["end"]) - _at(item["start"])).total_seconds() // 60))
        title = _FREE[lang] if item["kind"] == "free" else item.get("title") or ""
        return {
            "kind": item["kind"],
            "title": title,
            "start": _at(item["start"]).isoformat(),
            "end": _at(item["end"]).isoformat(),
            "place": places.get(item.get("place_id") or ""),
            "leave_at": _at(trip["leave_at"]).isoformat() if trip and trip.get("leave_at") else None,
            "minutes": trip.get("minutes") if trip else None,
            "mode": trip.get("mode") if trip else None,
            "status": item.get("status", "planned"),
            "late_min": late,
        }
    return {
        "kind": "none", "title": _NONE[lang], "start": day_end.isoformat(), "end": day_end.isoformat(),
        "place": None, "leave_at": None, "minutes": None, "mode": None, "status": "planned", "late_min": 0,
    }  # fmt: skip


def priority(before: dict[str, Any] | None, after: dict[str, Any]) -> int:
    """10 when the next stop or its leave time changed, else 5."""
    if before is None:
        return 10
    keys = ("kind", "title", "start", "leave_at")
    return 10 if any(before.get(k) != after.get(k) for k in keys) else 5


def stale_at(content: dict[str, Any] | None) -> int | None:
    """The `stale-date` for a content-state (Unix seconds): its leave time, else the end of the stop; past it
    the widget shows "late" without another push."""
    if not content:
        return None
    moment = content.get("leave_at") or content.get("end")
    return int(_at(moment).timestamp()) if moment else None


RELAY_FIELDS = frozenset({"token", "event", "priority", "timestamp", "attributesType", "dismissalDate"})


def relay_activity(token: str, event: str, priority: int, stamp: int) -> dict[str, Any]:
    """A Live Activity update through the relay (CONTRACT-2 §9): nothing of the user's
    day. No progress, no time and no stale date (one from the plan would give away the leave time): only the
    token, the event, the priority and the moment of sending, plus `attributesType` to start and
    `dismissalDate` (now) to end. The relay sends APNs an empty content-state; the app fills in the next stop
    and its times when it is open. Direct mode sends the real content."""
    activity: dict[str, Any] = {"token": token, "event": event, "priority": priority, "timestamp": stamp}
    if event == "start":
        activity["attributesType"] = ATTRIBUTES_TYPE
    if event == "end":
        activity["dismissalDate"] = stamp
    return activity
