"""App events from the phone (shared contract): why companion mode was refused, retried, recovered or
stopped, so the operator reads device-side causes without the user's device logs; a notice dismissed or
opened (its id only); and the phone's breakpoints for the guardian (a call, the motion state, a Focus: names,
states and times only). Pure functions, no I/O.

Only the allowlisted fields are kept, each string cut to 64 characters and to a short charset (a function
name, a four-character code, a state name): free text, such as anything the user said or Pisa's replies, has
no field to travel in. An event with an unknown name or no valid time is dropped.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

COMPANION = frozenset({"companion_refused", "companion_retry", "companion_recovered", "companion_stopped"})
NOTIFICATIONS = frozenset({"notification_dismissed", "notification_opened"})
# the phone's breakpoints the guardian reads (`pisa.domain.guardian.phone_state`)
BREAKPOINTS = frozenset({"call_started", "call_ended", "activity", "focus_on", "focus_off"})
# the microphone mode the phone used (`state`) on a route (`site`)
MICROPHONE = frozenset({"microphone_mode"})
EVENTS = COMPANION | NOTIFICATIONS | BREAKPOINTS | MICROPHONE
# Each family its own day (per device, local): a day of drives and calls never crowds out a dismissal
PER_DAY = {"companion": 50, "notifications": 50, "breakpoints": 100, "microphone": 20}
# An event dated more than this after the server's now is dropped on upload (a wrong phone clock)
FUTURE_SLACK = timedelta(minutes=2)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", re.ASCII)
TEXT_FIELDS = ("code", "site", "state")
TEXT_MAX = 64
BATCH_MAX = 200  # one upload: more is refused before it is read
# One pattern per field, ASCII only and no spaces: a function name, a four-character code, a state name. Free
# text (anything the user said) can't fit; a value that doesn't match is dropped.
_PATTERNS = {
    "code": re.compile(r"!?[A-Za-z0-9]{1,8}", re.ASCII),
    "site": re.compile(r"[A-Za-z_][A-Za-z0-9_.:()'\-]{0,63}", re.ASCII),
    "state": re.compile(r"[A-Za-z0-9_]{1,64}", re.ASCII),
}


def _text(name: str, value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    return text if _PATTERNS[name].fullmatch(text) else None


def sanitize(items: list[Any]) -> list[dict[str, Any]]:
    """The events worth keeping, each with the allowlisted fields only."""
    kept: list[dict[str, Any]] = []
    for item in items:
        event = item.get("event") if isinstance(item, dict) else None
        if not isinstance(event, str) or event not in EVENTS:
            continue
        try:
            at = datetime.fromisoformat(str(item.get("at")))
        except ValueError:
            continue
        background = item.get("background")
        notification = item.get("notification_id") if event in NOTIFICATIONS else None
        notification = (
            notification if isinstance(notification, str) and _UUID.fullmatch(notification) else None
        )
        if event in NOTIFICATIONS and notification is None:
            continue  # a dismissal (or a tap) says which notice, or it says nothing
        kept.append({
            "event": item["event"],
            **{name: _text(name, item.get(name)) for name in TEXT_FIELDS},
            "background": background if isinstance(background, bool) else None,
            "at": at if at.tzinfo else at.replace(tzinfo=UTC),
            "notification_id": notification.lower() if notification else None,
        })  # fmt: skip
    return kept


def family(event: str) -> str:
    """The event's own daily cap (`PER_DAY`)."""
    if event in NOTIFICATIONS:
        return "notifications"
    if event in MICROPHONE:
        return "microphone"
    return "breakpoints" if event in BREAKPOINTS else "companion"


def families(event: str) -> tuple[str, ...]:
    """The family's event names, for the day's count."""
    found = {
        "notifications": NOTIFICATIONS,
        "breakpoints": BREAKPOINTS,
        "companion": COMPANION,
        "microphone": MICROPHONE,
    }[family(event)]
    return tuple(sorted(found))
