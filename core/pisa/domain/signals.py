"""Two of the signals, computed by code. Pure: no I/O. SHADOW ONLY: they are
stored on `attend_candidates` and read by the `pisa_signals` view; nothing in Pisa decides anything from them
yet.

- `plan_change` ∈ [0, 1]: what moved between two consecutive plans of a day: an item dropped, an item's
  start moved more than 10 minutes, a new leave-at, a newly broken buffer (an item now starting before the one
  before it ends, where the previous plan had no such overlap), over the items of both plans. Not a
  with/without counterfactual (two solves differ by noise).
- `seen`: whether they could take in what Pisa delivered: the phone in front and unlocked, Pisa's voice in
  their ears, or the glasses on. Locked, earbuds out and glasses off is `False`; no presence, or none reported
  within `body.STATE_FRESH`, is `None`.
"""

from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from pisa.domain.body import STATE_FRESH, Presence, fresh
from pisa.domain.lexicon.dates import iso_moment_or_none

MOVED = timedelta(minutes=10)
# the audio routes that put Pisa's voice in their ears (body.py's names); the phone's speaker does not
EARS = frozenset({"earbuds", "bluetooth", "car", "glasses", "receiver", "wired"})


def _key(item: dict[str, Any]) -> str | None:
    for field in ("task_id", "event_id", "to_place_id"):
        if item.get(field):
            return f"{item.get('kind')}:{item[field]}"
    return None


def _at(value: Any) -> datetime | None:
    if not value:
        return None
    return iso_moment_or_none(str(value))


def plan_change(previous: dict[str, Any], latest: dict[str, Any]) -> float:
    """What moved from `previous` to `latest` (two `plans.plan` bodies), normalised to [0, 1]."""
    before = {k: i for i in previous.get("items") or [] if (k := _key(i))}
    after = {k: i for i in latest.get("items") or [] if (k := _key(i))}
    keys = set(before) | set(after)
    if not keys:
        return 0.0
    moved = 0
    for key, item in before.items():
        now = after.get(key)
        if now is None:
            moved += 1  # dropped
            continue
        start, was = _at(now.get("start")), _at(item.get("start"))
        if start is not None and was is not None and abs(start - was) > MOVED:
            moved += 1
    moved += sum(1 for k, i in after.items() if k not in before and i.get("leave_at"))  # a new leave-at
    moved += len(_overlaps(after) - _overlaps(before))  # a buffer broken now (one broken before is no news)
    return min(1.0, moved / len(keys))


def _overlaps(items: dict[str, dict[str, Any]]) -> set[tuple[str, str]]:
    """The consecutive pairs of a plan's timed items where the later starts before the earlier ends."""
    timed = sorted(
        (
            (s, e, k)
            for k, i in items.items()
            if (s := _at(i.get("start"))) is not None and (e := _at(i.get("end"))) is not None
        ),
        key=lambda p: (p[0], p[2]),
    )
    return {(a, b) for (_, end, a), (start, _, b) in pairwise(timed) if start < end}


def seen(presence: Presence | None, now: datetime, updated_at: datetime | None = None) -> bool | None:
    """Whether the user could see or hear a delivery now, from the device's latest presence. The presence
    row's own fields (app, earbuds, glasses) count only while `updated_at` is within STATE_FRESH, as the
    state's fields do; with nothing fresh at all: unknown (None)."""
    if presence is None:
        return None
    state = fresh(presence.state, now)
    current = updated_at is not None and now - updated_at <= STATE_FRESH
    if not current and not state:
        return None
    front = state.get("foreground", current and presence.app == "foreground")
    phone = bool(front) and not state.get("locked")
    route = state.get("route") or ("earbuds" if current and presence.earbuds == "connected" else None)
    ears = route in EARS
    glasses = state.get("glasses_worn", current and presence.glasses == "on")
    return bool(phone or ears or glasses)
