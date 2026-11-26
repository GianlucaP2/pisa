"""Pisa's body and open threads (the report's §4.3.1 and §4.5): two short parts of each turn's
dynamic instructions, built from what the device reports and what Pisa itself did. Pure: no I/O.

- The body line says where the user is looking from: whether Pisa's voice is in their ears and through what,
  whether the phone is in front of them, whether the glasses are on, and how much of the day's AI budget is
  left. The model chooses from it (say it, or show it, or both); code never decides that for it.
- The open threads (at most 5 lines) say what Pisa is waiting for and what it showed or will say. RAIL: only
  kinds, counts and times, never a title or a purpose: those can come from other people's invites or emails,
  and these lines are instructions, not data.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

THREADS_MAX = 5
# FACT: a field of the device's state the app stopped reporting is unknown after this: the
# body line never says "earbuds" an hour after the last word about them.
STATE_FRESH = timedelta(minutes=10)
_AT = "_at"  # each field's time, stored next to the fields
_ROUTES = {
    "earbuds": "earbuds", "bluetooth": "bluetooth headset", "car": "the car", "glasses": "the glasses",
    "speaker": "the phone's speaker", "receiver": "the phone at the ear", "wired": "wired headphones",
    "airplay": "AirPlay", "other": "another output",
}  # fmt: skip
_KINDS = {
    "calendar_create": "a new event", "calendar_move": "an event move", "calendar_cancel": "an event cancel",
    "email_send": "an email to send",
}  # fmt: skip


@dataclass(frozen=True)
class Presence:
    """`device_presence`: the events Pisa stored, and the whole state the app sent with the latest one."""

    app: str = "background"
    glasses: str | None = None
    earbuds: str | None = None
    companion: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


def stamped(
    stored: dict[str, Any] | None, sent: dict[str, Any] | None, now: datetime
) -> dict[str, Any] | None:
    """The device's state to store: the fields the app sent now, at `now`; the others as they were, with their
    own time (`fresh` drops them once old)."""
    if not sent:
        return stored
    old = stored or {}
    at = dict(old.get(_AT) or {}) | dict.fromkeys(sent, now.isoformat())
    return {k: v for k, v in old.items() if k != _AT} | sent | {_AT: at}


def fresh(stored: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """The fields of the stored state reported within STATE_FRESH (a field with no time: unknown)."""
    at = (stored or {}).get(_AT) or {}
    return {
        k: v
        for k, v in (stored or {}).items()
        if k != _AT and k in at and now - datetime.fromisoformat(at[k]) <= STATE_FRESH
    }


def body_line(presence: Presence | None, budget_left_eur: Decimal | None, *, typed: bool = False) -> str:
    """One line, about 60 tokens: "Pisa's body now: voice=… · phone=… · glasses=… · budget left today: …".
    `typed` first when they typed the words (they read: the screen is the answer)."""
    parts: list[str] = ["typed"] if typed else []
    if presence is None:
        parts.append("device state unknown")
    else:
        state = presence.state
        companion = state.get("companion", presence.companion == "on")
        route = state.get("route") or ("earbuds" if presence.earbuds == "connected" else None)
        voice = _ROUTES.get(route, "the phone") if route else "the phone"
        parts.append(f"voice={voice}" + (" (companion on)" if companion else ""))
        front = state.get("foreground", presence.app == "foreground")
        locked = state.get("locked")
        if front and not locked:
            parts.append("phone=in front (the user can see the screen)")
        else:
            away = "phone=not in front" + (" (locked)" if locked else "")
            parts.append(away + ": they hear Pisa, they don't see the screen")
        worn = state.get("glasses_worn", presence.glasses == "on" if presence.glasses else None)
        parts.append("glasses=" + ("on" if worn else "off" if worn is False else "unknown"))
    if budget_left_eur is not None:
        parts.append(f"budget left today: €{max(budget_left_eur, Decimal(0)):.2f}")
    return "\nPisa's body now: " + " · ".join(parts)


@dataclass(frozen=True)
class Threads:
    waiting: list[tuple[str, datetime]] = field(default_factory=list)  # (approval kind, asked at)
    shown: list[tuple[str, datetime]] = field(default_factory=list)  # (tool, at)
    later: int = 0  # Pisa's own items held for the next pause


def threads_block(threads: Threads, tz: Any) -> str:
    lines = [
        f"waiting for the user's yes on {_KINDS.get(kind, 'a change')} (asked {at.astimezone(tz):%H:%M})"
        for kind, at in threads.waiting
    ]
    lines += [
        f"showed {'a places card' if tool == 'show_places' else tool} at {at.astimezone(tz):%H:%M}"
        for tool, at in threads.shown
    ]
    if threads.later:
        lines.append(f"{threads.later} thing(s) of Pisa's to say at the next pause")
    if not lines:
        return ""
    return "\nPisa's open threads (Pisa's own state, newest first): " + "; ".join(lines[:THREADS_MAX]) + "."
