"""How long Pisa keeps what it doesn't learn from. Pure rules, no I/O.

- Conversation `messages`: the user's setting (`messages_retention`): forever (the default), a year or 90
  days. What Pisa learned from them (claims, their topics, their ideas) stays either way. A change
  waits a day (`MESSAGES_GRACE`) before the 03:30 prune acts on it: nothing goes the night they change it.
- Everything else about them is kept until they delete it; only the closed list goes by age
  (`maintenance.prune_history`, `prune_places`), and their own deletes (`services/erase.py`).
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

MESSAGES_RETENTION: dict[str, timedelta | None] = {
    "forever": None,
    "1y": timedelta(days=365),
    "90d": timedelta(days=90),
}
MESSAGES_DEFAULT = "forever"
MESSAGES_GRACE = timedelta(days=1)  # a change applies from the first prune a full day after it
PRUNE_AT = time(3, 30)  # the prune's schedule, in Pisa's zone (`workers/main.py`, prune-history)
PRUNE_EARLY = timedelta(minutes=30)  # the scheduled run may start a little before its 03:30
CLOCK_SKEW = timedelta(hours=1)  # the app's clock this far from the database's: no prune runs
BATCH = 5000  # rows a prune deletes a statement

PAIR_TOKENS_KEPT = timedelta(days=7)  # a pairing token, a week past its expiry (security material)
CURIOUS_SOURCES_KEPT = timedelta(days=90)  # its lateral check is cached 90 days (curious.LATERAL_DAYS)


def shortens(value: str, current: str) -> bool:
    """`value` keeps less than `current` (an irreversible change: their signature, like a device's
    deletion)."""
    keep, now_keep = MESSAGES_RETENTION[value], MESSAGES_RETENTION[current]
    return keep is not None and (now_keep is None or keep < now_keep)


def first_prune_after(changed_at: datetime, tz: str) -> datetime:
    """The first 03:30 (Pisa's zone) at least a day after a change: when the prune first acts on it (UTC)."""
    zone = ZoneInfo(tz)
    earliest = (changed_at + MESSAGES_GRACE).astimezone(zone)
    at = datetime.combine(earliest.date(), PRUNE_AT, zone)
    if at < earliest:
        at = datetime.combine(earliest.date() + timedelta(days=1), PRUNE_AT, zone)
    return at.astimezone(UTC)


def messages_cutoff(value: str, changed_at: datetime | None, now: datetime, tz: str) -> datetime | None:
    """Messages created before this go tonight; None: nothing goes (forever, or before the first 03:30 a full
    day after a change; a run may start a little early, never less than a day after the change)."""
    keep = MESSAGES_RETENTION[value]
    if keep is None:
        return None
    if changed_at is not None:
        opens = max(changed_at + MESSAGES_GRACE, first_prune_after(changed_at, tz) - PRUNE_EARLY)
        if now < opens:
            return None
    return now - keep


def messages_applies_after(
    value: str, current: str, changed_at: datetime | None, now: datetime, tz: str
) -> datetime:
    """The prune that first deletes under `value`: the first 03:30 at least a day after the change (a change
    made now, or the one that set the value already set); now for a value never changed."""
    if value != current:
        return first_prune_after(now, tz)
    if changed_at is not None:
        return first_prune_after(changed_at, tz)
    return now
