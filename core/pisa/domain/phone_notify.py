"""Contract 2.3's `pisa.notify` (the feature `notify.local`): Pisa hands the phone its reminders, task and
calendar alerts and "time to leave" alerts to schedule as local notifications, and places to watch, so they
fire even with no connection.

Here, pure (no I/O): the items and regions as the schema allows them (`$defs/NotifyItem`,
`$defs/NotifyRegion`), their ids, their lock-screen text, their deep links, and the phone's `pisa.notify.ack`
checked against `c_pisa_notify_ack`.

- **Ids** are stable per thing, never per moment: `rem-<promise>`, `task-<task>`, `cal-<hash of the event>`,
  `leave-<hash of the trip>`, `reg-<promise>`, `note-<place note>` (1-64 of `[A-Za-z0-9_-]`). A moved event
  keeps its id: the phone's copy is replaced (the schedule upserts by id).
- **The lock screen is minimal** (D: notify.local): the title names the kind only ("Promemoria", "Attività",
  "Evento", "È ora di uscire"; "Reminder", "Task", "Event", "Time to leave"), and the body is absent. Only
  with PISA_PHONE_NOTIFY_DETAILS on does the body carry the item's own words (what to remember, the task's or
  event's title), and never a stranger's (an invite's title, an email's task): those stay off the lock screen
  whatever the setting. A tap opens the item (`pisa://reminder/<uuid>`, `pisa://task/<uuid>`) or the
  riepilogo; nothing else is ever a link.
- **Regions** carry a coordinate rounded to `COORD_DECIMALS` (about 11 m: the radius is 100 m at least) and a
  radius clamped to the schema's 100-5000 m; only the user's own places (a remember_to with a place, a note
  pinned to a place in their words).
- **The ack is untrusted input**: closed objects, bounded lists, ids in their form, the reasons the contract
  names. One outside the bounds is refused whole (`Refused`, the transport's `invalid_frame`), and a refusal
  never quotes a value.
"""

import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

EXTENSION = "pisa.notify"
SCHEDULE = "pisa.notify.schedule"
CANCEL = "pisa.notify.cancel"
REGIONS = "pisa.notify.regions"
ACK = "pisa.notify.ack"
FEATURE = "notify.local"

WINDOW = timedelta(days=7)  # what the schedule holds: the next 7 days
ITEMS_MAX = 50  # in one schedule frame (the schema's), and on the phone at once
IDS_MAX = 50  # in one cancel frame, and in each list of an ack
REGIONS_MAX = 20  # the system's region monitors
RADIUS_MIN, RADIUS_MAX = 100, 5000
COORD_DECIMALS = 4  # ~11 m: enough for a region of 100 m or more, no finer than needed
# the phone's word on a device is good this long: a device that acked within it, its notifications not denied
# since, has what it acked (the honesty rail and the push's skip read it)
ACK_FRESH = timedelta(days=7)
# a reminder that fired (or a task, an event, a trip past its moment) leaves the phone this long after its
# moment: the phone shows it first, then the cancel tidies its list
FIRED_GRACE = timedelta(minutes=2)
SAME_MOMENT = timedelta(seconds=60)  # a server notice and the phone's item are the same alert within this
REVISION_MAX = 9_007_199_254_740_991
REASONS = ("denied", "limit", "past", "invalid", "unsupported", "error")

Kind = Literal["reminder", "task", "calendar", "leave"]
KINDS: tuple[Kind, ...] = ("reminder", "task", "calendar", "leave")

TITLES: dict[str, dict[str, str]] = {
    "it-IT": {"reminder": "Promemoria", "task": "Attività", "calendar": "Evento", "leave": "È ora di uscire"},
    "en-US": {"reminder": "Reminder", "task": "Task", "calendar": "Event", "leave": "Time to leave"},
}

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
DEEP_LINK = re.compile(
    r"pisa://(chat|riepilogo|task/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"reminder/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_ACK_KEYS = frozenset({"revision", "scheduled", "failed"})
_FAILED_KEYS = frozenset({"id", "reason"})


class Refused(ValueError):
    """A `pisa.notify.ack` outside contract 2.3's bounds: refused whole (`invalid_frame`). The message names
    the field, never its value."""


@dataclass(frozen=True)
class Item:
    """One local notification (`$defs/NotifyItem`), with what the server keeps beside it."""

    id: str
    fire_at: datetime  # aware
    kind: Kind
    title: str
    body: str | None = None
    deep_link: str | None = None

    def value(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "fireAt": self.fire_at.isoformat(), "title": self.title, "kind": self.kind,
        }  # fmt: skip
        if self.body:
            out["body"] = self.body
        if self.deep_link:
            out["deepLink"] = self.deep_link
        return out

    def sig(self) -> str:
        """What the phone holds of it: a change is a new frame for it."""
        return _digest(repr(sorted(self.value().items())))


@dataclass(frozen=True)
class Region:
    """One watched place (`$defs/NotifyRegion`)."""

    id: str
    lat: float
    lon: float
    radius_m: int
    title: str
    on: Literal["enter", "exit"]
    deep_link: str | None = None

    def value(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "lat": self.lat, "lon": self.lon, "radiusMeters": self.radius_m,
            "title": self.title, "on": self.on,
        }  # fmt: skip
        if self.deep_link:
            out["deepLink"] = self.deep_link
        return out

    def sig(self) -> str:
        return _digest(repr(sorted(self.value().items())))


@dataclass(frozen=True)
class Ack:
    revision: int
    scheduled: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]  # (id, reason)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def digest_all(sigs: Iterable[str]) -> str:
    """One set's signature (the regions'): its members' in order."""
    return _digest("|".join(sigs))


# --- ids, links, words ------------------------------------------------------------------------------------


def reminder_id(promise_id: uuid.UUID | str) -> str:
    return f"rem-{uuid.UUID(str(promise_id)).hex}"


def task_id(task: uuid.UUID | str) -> str:
    return f"task-{uuid.UUID(str(task)).hex}"


def event_id(ref: str) -> str:
    """An event's id is the calendar's own text: hashed, never sent."""
    return f"cal-{_digest('event:' + ref)}"


def leave_id(ref: str) -> str:
    return f"leave-{_digest('leave:' + ref)}"


def region_id(promise_id: uuid.UUID | str) -> str:
    return f"reg-{uuid.UUID(str(promise_id)).hex}"


def note_id(note: uuid.UUID | str) -> str:
    return f"note-{uuid.UUID(str(note)).hex}"


def reminder_link(promise_id: uuid.UUID | str) -> str:
    return f"pisa://reminder/{str(uuid.UUID(str(promise_id))).lower()}"


def task_link(task: uuid.UUID | str) -> str:
    return f"pisa://task/{str(uuid.UUID(str(task))).lower()}"


RIEPILOGO = "pisa://riepilogo"
CHAT = "pisa://chat"


def title(kind: str, lang: str) -> str:
    """The lock screen's title: the kind only ("region" is a reminder at a place)."""
    words = TITLES.get(lang, TITLES["en-US"])
    return words["reminder" if kind == "region" else kind]


def body(text: str | None, *, details: bool, outside: bool) -> str | None:
    """The lock screen's body: none by default; with details on, the item's own words (one line, no control
    or bidirectional character, at most 200), never a stranger's."""
    if not details or outside or not text:
        return None
    clean = " ".join(_BAD.sub(" ", text).split())[:200].strip()
    return clean or None


def coord(value: float) -> float:
    return round(value, COORD_DECIMALS)


def radius(metres: int | float | None) -> int:
    return int(min(max(int(metres or RADIUS_MIN), RADIUS_MIN), RADIUS_MAX))


def valid_item(item: Item) -> bool:
    """Within the schema: what the server would never send otherwise (a programming error, logged)."""
    return (
        bool(_ID.fullmatch(item.id))
        and item.fire_at.tzinfo is not None
        and item.kind in KINDS
        and 1 <= len(item.title) <= 80
        and not _BAD.search(item.title)
        and (item.body is None or (1 <= len(item.body) <= 200 and not _BAD.search(item.body)))
        and (item.deep_link is None or bool(DEEP_LINK.fullmatch(item.deep_link)))
    )


def valid_region(region: Region) -> bool:
    return (
        bool(_ID.fullmatch(region.id))
        and -90 <= region.lat <= 90
        and -180 <= region.lon <= 180
        and RADIUS_MIN <= region.radius_m <= RADIUS_MAX
        and 1 <= len(region.title) <= 80
        and region.on in ("enter", "exit")
        and (region.deep_link is None or bool(DEEP_LINK.fullmatch(region.deep_link)))
    )


def chunks(ids: list[str], size: int = IDS_MAX) -> list[list[str]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


# --- what the phone answers -------------------------------------------------------------------------------


def _id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):  # fullmatch: no trailing newline either
        raise Refused(f"{what} is not in its form.")
    return value


def parse_ack(value: Any) -> Ack:
    """`pisa.notify.ack`'s value, checked whole (`c_pisa_notify_ack`): a revision, `scheduled` (0-50 unique
    ids) and `failed` (0-50 `{id, reason}`), both required, nothing else. `Refused` otherwise."""
    if not isinstance(value, dict):
        raise Refused("pisa.notify.ack is an object.")
    if set(value) - _ACK_KEYS:
        raise Refused("pisa.notify.ack has a property the contract doesn't allow.")
    if set(value) != _ACK_KEYS:
        raise Refused("pisa.notify.ack carries revision, scheduled and failed.")
    revision = value["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= REVISION_MAX:
        raise Refused("pisa.notify.ack's revision is a whole number from 1.")
    scheduled, failed = value["scheduled"], value["failed"]
    if not isinstance(scheduled, list) or len(scheduled) > IDS_MAX:
        raise Refused(f"pisa.notify.ack's scheduled is a list of at most {IDS_MAX} ids.")
    if not isinstance(failed, list) or len(failed) > IDS_MAX:
        raise Refused(f"pisa.notify.ack's failed is a list of at most {IDS_MAX}.")
    ids = tuple(_id(i, "A scheduled id") for i in scheduled)
    if len(set(ids)) != len(ids):
        raise Refused("pisa.notify.ack's scheduled ids are unique.")
    out: list[tuple[str, str]] = []
    for one in failed:
        if not isinstance(one, dict) or set(one) != _FAILED_KEYS:
            raise Refused("A failure is {id, reason} and nothing else.")
        if one["reason"] not in REASONS:
            raise Refused(f"A failure's reason is one of {', '.join(REASONS)}.")
        out.append((_id(one["id"], "A failed id"), one["reason"]))
    return Ack(revision, ids, tuple(out))
