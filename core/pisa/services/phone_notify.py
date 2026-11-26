"""Local notifications on the phone (contract 2.3, `pisa.notify`, the feature `notify.local`), server side.

**What the phone holds** (`desired`): the next `WINDOW` (7 days) of alerts, the soonest `ITEMS_MAX` (50):
timed reminders (`pisa_promises`, condition time, still to come), open tasks with a due time (at their due
time less the reminders' task lead, as the reminders step fires them; low priority never), timed calendar
events (at their start less the event lead), and "time to leave" for each trip of the latest plans (at its
leave time less the leave lead, while leave-now is on). An alert inside the quiet hours is left to the server,
which holds it as it holds every notice. And the places to watch, at most `REGIONS_MAX` (20): a remember_to
with a place (arrive: enter, leave: exit) and the notes pinned to a place in their words (enter), the nearest
to their last position first.

**How it is sent** (`plan`): the schedule upserts by id (the contract's): each sync sends, in one short run
on the notices thread, a `pisa.notify.cancel` for what the phone holds that is no longer wanted (a reminder
cancelled, a task done or deleted, an event gone; a fired or past one `FIRED_GRACE` after its moment, so the
phone shows it first), then one `pisa.notify.schedule` with every wanted item that is new or changed, then
`pisa.notify.regions` (the whole set) when the set changed. On a connection (after the welcome) the whole
schedule and the regions go again, whatever the server thinks the phone holds: a reinstalled app or a
restored phone gets everything back. Every frame takes the device's next `revision` (one counter per device
for the three, kept in `device_notify`); what each item was last sent with is kept in `device_notify_items`.
A change anywhere (the API, the worker, the CLI) is a Postgres NOTIFY (`pisa_notify` for reminders, place
notes and plans; `pisa_today` for tasks and the calendar): `NotifyChanged` re-syncs every connected phone
that granted `pisa.notify`, at most once every `EVERY_S`.

**The epoch** (contract 2.5): a random string kept in `app_meta` (`notify_epoch`), made when first read and
sent in every frame. A restore brings back the revisions of the day of the backup, below what the phone
already applied, so the frames would be dropped as replays: a new epoch tells the phone to forget its last
revision. `reset_epoch` makes a new one (`pisa notify reset-epoch`, run after a restore).

**The phone's word** (`ack`): `pisa.notify.ack` (checked against the schema) marks each id the device
applied `scheduled`, or `failed` with its reason, only when the id is one this device was sent in that very
revision: an id of another device, of an older frame, or never sent is ignored. A `denied` failure marks the
device's notifications denied until an ack schedules again.

**No double alert, no lost one** (`on_phone`): a notice the server fires for an item the phone acked,
within `ACK_FRESH` of the device's last ack and with its notifications not denied, is not pushed to that
device (the phone's own notification is the alert, `delivered_via` `phone`); with no fresh ack the push goes
as before.

**The honesty rail** (`Reach`): a timed reminder reaches the user when a push path exists (direct APNs with
the server's key, or the relay) for a device of theirs, or when it is in the phone's schedule and a device of
theirs acked within `ACK_FRESH` with notifications not denied. Otherwise remember_to's result says it can't
reach their phone and Pisa says they'll see it when they open the app.

Privacy: nothing here logs a title, a time or a coordinate: ids are hashed or UUIDs, logs carry counts, the
device and the revision only.
"""

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import phone_notify as rules
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.domain.settings import is_quiet
from pisa.logging import get_logger
from pisa.services import foreign

NOTIFY_CHANNEL = "pisa_notify"  # 0025's triggers: reminders, place notes, plans changed
EVERY_S = 1.0  # a sync per phone at most this often: a statement's rows, a re-plan, are one
PLANS_BACK = timedelta(days=1)  # the latest plans read for trips (as the reminders step's)
_log = get_logger("pisa.notify")


def phone_notify_on(settings: Any) -> bool:
    """The phone's own notifications (`notify.local`, `pisa.notify`): PISA_PHONE_NOTIFY_ENABLED, never in
    review mode."""
    return bool(getattr(settings, "phone_notify_enabled", True)) and not getattr(
        settings, "review_mode", False
    )


# --- the epoch ---------------------------------------------------------------------------------------------

EPOCH_KEY = "notify_epoch"


async def notify_epoch(conn: AsyncConnection) -> str:
    """The notify state's epoch, made on the first call (two first calls at once keep one)."""
    found = await (await conn.execute("SELECT value FROM app_meta WHERE key = %s", (EPOCH_KEY,))).fetchone()
    if found is not None:
        return str(found[0])
    await conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (EPOCH_KEY, secrets.token_hex(8)),
    )
    found = await (await conn.execute("SELECT value FROM app_meta WHERE key = %s", (EPOCH_KEY,))).fetchone()
    assert found is not None
    return str(found[0])


async def reset_epoch(conn: AsyncConnection) -> str:
    """A new epoch, now: every phone resets its revision at the next frame. Returns it."""
    epoch = secrets.token_hex(8)
    await conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET "
        "value = EXCLUDED.value, updated_at = now()",
        (EPOCH_KEY, epoch),
    )
    return epoch


# --- what the phone should hold -----------------------------------------------------------------------------


async def desired(
    conn: AsyncConnection, now: datetime, owner: Owner, *, details: bool = False
) -> tuple[list[rules.Item], list[rules.Region]]:
    """The schedule (soonest first, at most ITEMS_MAX within WINDOW) and the regions (at most REGIONS_MAX)."""
    settings = await owner.settings(conn)
    lang = str(settings["language"])
    until = now + rules.WINDOW
    found: list[tuple[rules.Item, bool]] = []  # (item, its text a stranger's)

    def add(kind: rules.Kind, ident: str, fire_at: datetime, text: str | None, outside: bool,
            link: str | None) -> None:  # fmt: skip
        if not now < fire_at <= until or is_quiet(fire_at, settings, owner.tz):
            return  # quiet hours: the server holds it, as it holds every notice
        item = rules.Item(
            ident, fire_at, kind, rules.title(kind, lang), rules.body(text, details=details, outside=outside),
            link,
        )  # fmt: skip
        found.append((item, outside))

    for promise_id, what, due_at, provenance in await (
        await conn.execute(
            "SELECT id, what, due_at, provenance FROM pisa_promises WHERE condition = 'time' "
            "AND fired_at IS NULL AND cancelled_at IS NULL AND due_at > %s AND due_at <= %s",
            (now, until),
        )
    ).fetchall():
        add("reminder", rules.reminder_id(promise_id), due_at, what, provenance == "outside_text",
            rules.reminder_link(promise_id))  # fmt: skip

    leads = settings["reminders"]
    task_lead = timedelta(minutes=int(leads["task_lead_min"]))
    tasks = await (
        await conn.execute(
            f"SELECT t.id, t.title, t.due_at, {foreign.FOREIGN_TASK}, EXISTS (SELECT 1 FROM "  # noqa: S608
            "reminders_sent r WHERE r.kind = 'task' AND r.ref = t.id::text AND r.fire_at >= t.due_at - %s) "
            "FROM tasks t WHERE t.status = 'open' AND t.due_kind = 'datetime' AND t.priority <> 3 "
            "AND t.due_at > %s AND t.due_at - %s <= %s",
            (task_lead, now, task_lead, until),
        )
    ).fetchall()
    for ident, task_title, due, from_mail, reminded in tasks:
        fire = due - task_lead
        if fire <= now:  # its lead already past: the reminders step fires it at its due time, once
            if reminded:
                continue
            fire = due
        add("task", rules.task_id(ident), fire, task_title, bool(from_mail), rules.task_link(ident))

    event_lead = timedelta(minutes=int(leads["event_lead_min"]))
    events = await (
        await conn.execute(
            "SELECT DISTINCT ON (e.id) e.id, e.title, e.starts_at, "  # noqa: S608 (constants)
            f"bool_or(coalesce({foreign.FOREIGN_EVENT}, true)) OVER (PARTITION BY e.id) "
            f"FROM {foreign.EVENTS} "
            "WHERE NOT e.all_day AND e.starts_at > %s AND e.starts_at <= %s ORDER BY e.id, e.calendar_id",
            (now + event_lead, until + event_lead),
        )
    ).fetchall()
    for ref, event_title, start, strangers in events:
        add("calendar", rules.event_id(str(ref)), start - event_lead, event_title, bool(strangers),
            rules.RIEPILOGO)  # fmt: skip

    planner = settings["planner"]
    if planner["leave_now"]:
        leave_lead = timedelta(minutes=int(planner["leave_lead_min"]))
        for (plan,) in await (
            await conn.execute(
                "SELECT DISTINCT ON (day) plan FROM plans WHERE generated_at > %s "
                "ORDER BY day, generated_at DESC",
                (now - PLANS_BACK,),
            )
        ).fetchall():
            items = plan.get("items") or []
            for i, step in enumerate(items):
                if step.get("kind") != "travel" or not step.get("leave_at"):
                    continue
                target = next((t for t in items[i + 1 :] if t.get("kind") in ("task", "event")), None)
                if target is None:
                    continue
                ref = f"{target.get('task_id') or target.get('event_id') or ''}@{plan.get('date')}"
                fire = datetime.fromisoformat(step["leave_at"]) - leave_lead
                outside = await foreign.item_foreign(conn, target)
                add("leave", rules.leave_id(ref), fire, target.get("title"), outside, rules.RIEPILOGO)

    chosen: dict[str, rules.Item] = {}
    for item, _ in sorted(found, key=lambda f: (f[0].fire_at, f[0].id)):
        if item.id not in chosen and rules.valid_item(item):
            chosen[item.id] = item
    items = list(chosen.values())[: rules.ITEMS_MAX]
    return items, await _regions(conn, lang)


async def _regions(conn: AsyncConnection, lang: str) -> list[rules.Region]:
    """Their own places: a remember_to with a place, then the notes pinned in their words, nearest first."""
    here = await (
        await conn.execute("SELECT lat, lon FROM device_locations ORDER BY measured_at DESC LIMIT 1")
    ).fetchone()
    promises = await (
        await conn.execute(
            "SELECT p.id, p.condition, pl.lat, pl.lon FROM pisa_promises p "
            "JOIN places pl ON pl.id = p.place_id "
            "WHERE p.condition IN ('arrive', 'leave') AND p.fired_at IS NULL AND p.cancelled_at IS NULL "
            "AND pl.lat IS NOT NULL AND pl.lon IS NOT NULL ORDER BY p.created_at DESC"
        )
    ).fetchall()
    notes = await (
        await conn.execute(
            "SELECT n.id, n.lat, n.lon, n.radius_m FROM place_notes n WHERE NOT EXISTS (SELECT 1 FROM "
            "intentions i WHERE i.place_note_id = n.id AND i.state IN ('done', 'archived')) "
            "ORDER BY n.created_at DESC"
        )
    ).fetchall()
    found = [
        rules.Region(
            rules.region_id(pid), rules.coord(lat), rules.coord(lon), rules.radius(None),
            rules.title("region", lang), "enter" if condition == "arrive" else "exit",
            rules.reminder_link(pid),
        )
        for pid, condition, lat, lon in promises
    ] + [
        rules.Region(rules.note_id(nid), rules.coord(lat), rules.coord(lon), rules.radius(r),
                     rules.title("region", lang), "enter", rules.CHAT)
        for nid, lat, lon, r in notes
    ]  # fmt: skip
    if here is not None:
        point = (float(here[0]), float(here[1]))
        found.sort(key=lambda r: distance_m(point, (r.lat, r.lon)))  # stable: promises before notes at a tie
    return [r for r in found if rules.valid_region(r)][: rules.REGIONS_MAX]


# --- what to send -------------------------------------------------------------------------------------------


@dataclass
class Plan:
    frames: list[tuple[str, dict[str, Any]]]
    later: bool  # a past item waits out FIRED_GRACE: sync again after it


async def plan(
    conn: AsyncConnection,
    device_id: uuid.UUID,
    items: list[rules.Item],
    regions: list[rules.Region],
    now: datetime,
    *,
    connect: bool = False,
) -> Plan:
    """The frames this device needs now, each with its next revision; what each carries is recorded (in the
    caller's transaction). `connect`: the whole schedule and the regions, whatever the phone holds."""
    await conn.execute(
        "INSERT INTO device_notify (device_id, revision) VALUES (%s, 0) ON CONFLICT (device_id) DO NOTHING",
        (device_id,),
    )
    row = await (
        await conn.execute(
            "SELECT revision, regions_sig FROM device_notify WHERE device_id = %s FOR UPDATE", (device_id,)
        )
    ).fetchone()
    assert row is not None
    revision, regions_sig = int(row[0]), row[1]
    epoch = await notify_epoch(conn)
    held = {
        r[0]: (r[1], r[2], r[3])
        for r in await (
            await conn.execute(
                "SELECT id, kind, fire_at, sig FROM device_notify_items WHERE device_id = %s", (device_id,)
            )
        ).fetchall()
    }
    wanted = {i.id for i in items}
    frames: list[tuple[str, dict[str, Any]]] = []
    later = False
    gone: list[str] = []
    for ident, (kind, fire_at, _) in sorted(held.items()):
        if kind == "region" or ident in wanted:
            continue
        if fire_at is not None and fire_at <= now < fire_at + rules.FIRED_GRACE:
            later = True  # fired a moment ago: the phone shows it first
            continue
        gone.append(ident)
    for ids in rules.chunks(gone):
        revision += 1
        frames.append((rules.CANCEL, {"revision": revision, "epoch": epoch, "ids": ids}))
        await conn.execute(
            "DELETE FROM device_notify_items WHERE device_id = %s AND id = ANY(%s)", (device_id, ids)
        )
    send = [i for i in items if connect or i.id not in held or held[i.id][2] != i.sig()]
    if send:
        revision += 1
        frames.append(
            (rules.SCHEDULE, {"revision": revision, "epoch": epoch, "items": [i.value() for i in send]})
        )
        for item in send:
            await conn.execute(
                "INSERT INTO device_notify_items (device_id, id, kind, fire_at, sig, revision, state, "
                "updated_at) VALUES (%s, %s, %s, %s, %s, %s, 'sent', %s) ON CONFLICT (device_id, id) "
                "DO UPDATE SET kind = EXCLUDED.kind, fire_at = EXCLUDED.fire_at, sig = EXCLUDED.sig, "
                "revision = EXCLUDED.revision, state = 'sent', reason = NULL, acked_at = NULL, "
                "updated_at = EXCLUDED.updated_at",
                (device_id, item.id, item.kind, item.fire_at, item.sig(), revision, now),
            )
    sig = rules.digest_all(r.sig() for r in regions)
    if (connect and (regions or regions_sig is not None)) or sig != (regions_sig or rules.digest_all(())):
        revision += 1
        frames.append(
            (rules.REGIONS, {"revision": revision, "epoch": epoch, "regions": [r.value() for r in regions]})
        )
        await conn.execute(
            "DELETE FROM device_notify_items WHERE device_id = %s AND kind = 'region'", (device_id,)
        )
        for region in regions:
            await conn.execute(
                "INSERT INTO device_notify_items (device_id, id, kind, sig, revision, state, updated_at) "
                "VALUES (%s, %s, 'region', %s, %s, 'sent', %s) ON CONFLICT (device_id, id) DO UPDATE SET "
                "kind = 'region', fire_at = NULL, sig = EXCLUDED.sig, revision = EXCLUDED.revision, "
                "state = 'sent', reason = NULL, acked_at = NULL, updated_at = EXCLUDED.updated_at",
                (device_id, region.id, region.sig(), revision, now),
            )
        regions_sig = sig
    if frames:
        await conn.execute(
            "UPDATE device_notify SET revision = %s, regions_sig = %s, sent_at = %s WHERE device_id = %s",
            (revision, regions_sig, now, device_id),
        )
    return Plan(frames, later)


# --- what the phone answers ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AckOutcome:
    applied: int  # ids taken (scheduled or failed)
    ignored: int  # ids this device wasn't sent in that revision
    stale: bool  # a revision this device was never sent


async def ack(conn: AsyncConnection, device_id: uuid.UUID, got: rules.Ack, now: datetime) -> AckOutcome:
    """Record the phone's ack (already checked): each id only when this device was sent it in that very
    revision. A `denied` failure of a notification marks the device's notifications denied; a scheduled one
    clears it."""
    row = await (
        await conn.execute("SELECT revision FROM device_notify WHERE device_id = %s FOR UPDATE", (device_id,))
    ).fetchone()
    if row is None or got.revision > int(row[0]):
        _log.info("notify_ack_unknown_revision", device_id=str(device_id))
        return AckOutcome(0, len(got.scheduled) + len(got.failed), True)
    applied = ignored = 0
    scheduled_kinds: list[str] = []
    denied = False
    for ident in got.scheduled:
        done = await (
            await conn.execute(
                "UPDATE device_notify_items SET state = 'scheduled', reason = NULL, acked_at = %s, "
                "updated_at = %s "
                "WHERE device_id = %s AND id = %s AND revision = %s RETURNING kind",
                (now, now, device_id, ident, got.revision),
            )
        ).fetchone()
        if done is None:
            ignored += 1
            continue
        applied += 1
        scheduled_kinds.append(str(done[0]))
    for ident, reason in got.failed:
        done = await (
            await conn.execute(
                "UPDATE device_notify_items SET state = 'failed', reason = %s, acked_at = %s, "
                "updated_at = %s "
                "WHERE device_id = %s AND id = %s AND revision = %s RETURNING kind",
                (reason, now, now, device_id, ident, got.revision),
            )
        ).fetchone()
        if done is None:
            ignored += 1
            continue
        applied += 1
        denied = denied or (reason == "denied" and done[0] != "region")
    notified = any(k != "region" for k in scheduled_kinds)
    await conn.execute(
        "UPDATE device_notify SET acked_revision = greatest(coalesce(acked_revision, 0), %s), acked_at = %s, "
        "denied_at = CASE WHEN %s THEN %s WHEN %s THEN NULL ELSE denied_at END WHERE device_id = %s",
        (got.revision, now, denied, now, notified and not denied, device_id),
    )
    _log.info(
        "notify_acked", device_id=str(device_id), revision=got.revision, applied=applied, ignored=ignored,
        denied=denied,
    )  # fmt: skip
    return AckOutcome(applied, ignored, False)


# --- the reads: no double alert, the honesty rail -----------------------------------------------------------


async def on_phone(
    conn: AsyncConnection, local_id: str | None, local_at: datetime | None, now: datetime
) -> set[uuid.UUID]:
    """The devices whose phone holds this alert itself (acked, fresh, notifications not denied, at the same
    moment): the server's push for it is skipped on them."""
    if not local_id:
        return set()
    rows = await (
        await conn.execute(
            "SELECT i.device_id FROM device_notify_items i JOIN device_notify d ON d.device_id = i.device_id "
            "JOIN devices v ON v.id = i.device_id WHERE i.id = %s AND i.state = 'scheduled' "
            "AND v.revoked_at IS NULL AND d.acked_at >= %s AND d.denied_at IS NULL "
            "AND (%s::timestamptz IS NULL OR (i.fire_at IS NOT NULL "
            "AND i.fire_at BETWEEN %s::timestamptz - %s AND %s::timestamptz + %s))",
            (
                local_id,
                now - rules.ACK_FRESH,
                local_at,
                local_at,
                rules.SAME_MOMENT,
                local_at,
                rules.SAME_MOMENT,
            ),
        )
    ).fetchall()
    return {r[0] for r in rows}


class PushPaths(Protocol):
    def configured(self) -> bool: ...
    def relay_configured(self) -> bool: ...


async def push_reaches(conn: AsyncConnection, push: PushPaths | None) -> bool:
    """A push path to a device of theirs: the relay (set up, and the device registered) or direct APNs (the
    team, the topic and the key on the server, and the device's token)."""
    if push is None:
        return False
    relay = push.relay_configured()
    direct = (
        push.configured()
        and (await (await conn.execute("SELECT 1 FROM server_secrets WHERE name = 'apns'")).fetchone())
        is not None
    )
    if not (relay or direct):
        return False
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM devices d LEFT JOIN device_push_relay r ON r.device_id = d.id "
            "WHERE d.revoked_at IS NULL AND ((r.device_id IS NOT NULL AND %s) OR (d.apns_token IS NOT NULL "
            "AND %s)))",
            (relay, direct),
        )
    ).fetchone()
    return bool(row and row[0])


async def phone_works(conn: AsyncConnection, now: datetime) -> bool:
    """A device of theirs acked within ACK_FRESH, its notifications not denied: a new item will be
    scheduled."""
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM device_notify d JOIN devices v ON v.id = d.device_id "
            "WHERE v.revoked_at IS NULL AND d.acked_at >= %s AND d.denied_at IS NULL)",
            (now - rules.ACK_FRESH,),
        )
    ).fetchone()
    return bool(row and row[0])


class Reach:
    """Whether a timed reminder will reach the user (the honesty rail for "te lo ricordo alle …")."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        owner: Owner,
        settings: Any,
        push: PushPaths | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool, self._owner, self._settings, self._push, self._clock = pool, owner, settings, push, clock

    async def __call__(self, promise_id: str) -> bool:
        now = self._clock()
        async with self._pool.connection() as conn:
            if await push_reaches(conn, self._push):
                return True
            if not phone_notify_on(self._settings) or not await phone_works(conn, now):
                return False
            items, _ = await desired(conn, now, self._owner)
        return rules.reminder_id(promise_id) in {i.id for i in items}


# --- re-syncing the connected phones on a change ------------------------------------------------------------


class Syncs(Protocol):
    """A connected phone's session (`DeviceSession`)."""

    async def notify_sync(self, *, connect: bool = False) -> bool: ...


class NotifyChanged:
    """Their reminders, tasks, agenda, plans or place notes changed: every connected phone that granted
    `pisa.notify` is synced, at most once every `EVERY_S` (what changes meanwhile goes in the same sync). A
    sync that left a fired item to the grace runs again after it."""

    def __init__(self, sessions: Callable[[], Iterable[Syncs]], every_s: float = EVERY_S) -> None:
        self._sessions = sessions
        self._every_s = every_s
        self._waiting = False
        self._tasks: set[asyncio.Task[None]] = set()

    def changed(self, *_: object) -> None:
        if not self._waiting:
            self._waiting = True
            self._start(self._run(self._every_s))

    def _start(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, wait_s: float) -> None:
        await asyncio.sleep(wait_s)
        self._waiting = False
        later = await sync_all(self._sessions())
        if later:
            self._start(self._again())

    async def _again(self) -> None:
        await asyncio.sleep(rules.FIRED_GRACE.total_seconds())
        self.changed()


async def sync_all(sessions: Iterable[Syncs]) -> bool:
    later = False
    synced = 0
    for session in list(sessions):
        try:
            later = await session.notify_sync() or later
            synced += 1
        except Exception as exc:  # one phone's failure never stops the others
            _log.warning("notify_sync_failed", cause=exc.__class__.__name__)
    _log.info("notify_synced", devices=synced)
    return later
