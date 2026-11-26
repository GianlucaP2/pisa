"""Where the user is, from the phone (`hello`, `device_context`), stored and loaded into every process.

A zone change re-stamps what was stored as a local day in the old zone: all-day events (their first and last
day) and `date` deadlines (23:59 of their day). Everything else is an instant (timestamptz) and stays where it
is: a 15:00 meeting in Rome is 09:00 in New York."""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import zone as rules
from pisa.domain.owner import Owner
from pisa.domain.stays import Fix
from pisa.domain.tasks import END_OF_DAY
from pisa.logging import get_logger

_log = get_logger("pisa.zone")


@dataclass(frozen=True)
class Report:
    """What a device itself said (only the primary phone's reports count; a report without a
    region says nothing about the country)."""

    device_id: uuid.UUID | None
    tz: str | None
    region: str | None


async def primary_phone(conn: AsyncConnection) -> uuid.UUID | None:
    """The user's primary phone, the most recently paired iPhone still paired: paired
    with a code made on the host (the platform is the device's own claim) and, once Pisa has Face ID keys, the
    newest such iPhone that registered one."""
    row = await (
        await conn.execute(
            "SELECT d.id FROM devices d WHERE d.platform = 'ios' AND d.revoked_at IS NULL "
            "AND d.paired_via = 'host' ORDER BY EXISTS (SELECT 1 FROM phone_keys k WHERE k.device_id = d.id) "
            "DESC, d.created_at DESC LIMIT 1"
        )
    ).fetchone()
    return row[0] if row else None


async def located_phone(conn: AsyncConnection) -> uuid.UUID | None:
    """The phone whose fixes say where they are: the primary phone, else the device that sent the last fix."""
    if (primary := await primary_phone(conn)) is not None:
        return primary
    row = await (
        await conn.execute("SELECT device_id FROM device_locations ORDER BY measured_at DESC LIMIT 1")
    ).fetchone()
    return row[0] if row else None


async def fixes(conn: AsyncConnection, device: uuid.UUID, since: datetime, until: datetime) -> list[Fix]:
    """A device's fixes from `since` to `until` (both included), oldest first."""
    rows = await (
        await conn.execute(
            "SELECT lat, lon, accuracy_m, measured_at FROM device_locations WHERE device_id = %s "
            "AND measured_at >= %s AND measured_at <= %s ORDER BY measured_at",
            (device, since, until),
        )
    ).fetchall()
    return [Fix(a, b, acc, at) for a, b, acc, at in rows]


FLIP_S = 600  # back to the zone before within 10 minutes is a border, never a trip


Observer = Callable[[rules.PhoneContext | None, rules.PhoneContext, datetime, Report], Awaitable[object]]


async def load(pool: AsyncConnectionPool, owner: Owner) -> rules.PhoneContext | None:
    """The stored context into this process's Owner (the runtime at start, before its services are built)."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT tz, region, hour_cycle FROM owner_context WHERE id")
        ).fetchone()
    context = rules.PhoneContext(row[0], row[1], row[2]) if row and rules.loadable(row[0]) else None
    owner.place = context
    return context


class ZoneService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        owner: Owner,
        *,
        on_change: Callable[[], None] | None = None,
        observers: Sequence[Observer] = (),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._default = owner.home
        self.on_change = on_change  # Pisa's ready answers follow a new zone
        # who hears each report of the phone, after it is stored (the landing, the residency day)
        self.observers: list[Observer] = list(observers)

    async def load(self) -> rules.PhoneContext | None:
        """The stored context into this process (each process at start; the worker's steps each start one)."""
        return await load(self._pool, self._owner)

    async def update(
        self,
        *,
        tz: str | None,
        region: str | None,
        hour_cycle: rules.HourCycle | None,
        device_id: uuid.UUID | None,
        now: datetime,
    ) -> rules.PhoneContext | None:
        """The phone said where it is. Absent fields keep the last known; a zone Pisa can't load is ignored.
        Only the user's primary phone (the most recently paired iPhone) says it; another device's
        report (an iPad in New York, a web client) moves nothing. A flip back to the zone before
        within FLIP_S of the change is ignored (a phone on the border). A report without `region` never
        changes the country."""
        if tz is not None and not rules.loadable(tz):
            _log.warning("device_tz_unknown", tz=tz[:64])
            tz = None
        async with self._pool.connection() as conn, conn.transaction():
            if device_id is not None and device_id != await primary_phone(conn):
                _log.info("device_context_ignored", reason="not the primary phone")
                return self._owner.place
            row = await (
                await conn.execute(
                    "SELECT tz, region, hour_cycle, previous_tz, tz_changed_at FROM owner_context WHERE id "
                    "FOR UPDATE"
                )
            ).fetchone()
            old = rules.PhoneContext(row[0], row[1], row[2]) if row else None
            before = old.tz if old is not None else self._default
            if row is not None and tz is not None and tz != before and tz == row[3] and row[4] is not None:
                if now - row[4] < timedelta(seconds=FLIP_S):
                    _log.info("device_context_ignored", reason="flip")
                    tz = None  # back to the zone of a minute ago: the border, not a trip
            new = rules.PhoneContext(
                tz or before,
                region or (old.region if old else None),
                hour_cycle or (old.hour_cycle if old else None),
            )
            if old == new:
                self._set(new)
                await self._tell(old, new, now, Report(device_id, tz, region))
                return new
            await conn.execute(
                "INSERT INTO owner_context (id, tz, previous_tz, region, hour_cycle, device_id, updated_at, "
                "tz_changed_at) VALUES (true, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "tz = EXCLUDED.tz, "
                "previous_tz = CASE WHEN owner_context.tz <> EXCLUDED.tz THEN owner_context.tz "
                "ELSE owner_context.previous_tz END, "
                "tz_changed_at = CASE WHEN owner_context.tz <> EXCLUDED.tz THEN EXCLUDED.tz_changed_at "
                "ELSE owner_context.tz_changed_at END, "
                "region = EXCLUDED.region, hour_cycle = EXCLUDED.hour_cycle, device_id = EXCLUDED.device_id, "
                "updated_at = EXCLUDED.updated_at",
                (new.tz, before, new.region, new.hour_cycle, device_id, now, now),
            )
            if new.tz != before:
                await restamp(conn, before, new.tz)
                _log.info("owner_zone_changed", tz_from=before, tz_to=new.tz)
        self._set(new)
        await self._tell(old, new, now, Report(device_id, tz, region))
        if self.on_change is not None:
            self.on_change()
        return new

    def _set(self, context: rules.PhoneContext | None) -> None:
        """Where the phone says the user is, into this process's Owner."""
        self._owner.place = context

    async def _tell(
        self, old: rules.PhoneContext | None, new: rules.PhoneContext, now: datetime, report: Report
    ) -> None:
        """Each observer hears the report (and who made it); one that fails never fails the phone's hello."""
        for observer in self.observers:
            try:
                await observer(old, new, now, report)
            except Exception:
                _log.exception("zone_observer_failed")


async def restamp(conn: AsyncConnection, old: str, new: str) -> None:
    """What was a local day in `old` becomes the same local day in `new` (all-day events, date deadlines)."""
    await conn.execute(
        "UPDATE calendar_events SET starts_at = start_date::timestamp AT TIME ZONE %(new)s, "
        "ends_at = coalesce(end_date, start_date + 1)::timestamp AT TIME ZONE %(new)s "
        "WHERE all_day AND start_date IS NOT NULL",
        {"new": new},
    )
    await conn.execute(
        # the day (`due_date`, else read in the old zone) at 23:59 in the new one; the tasks'
        # trigger keeps every later write to the same rule
        "UPDATE tasks SET due_date = coalesce(due_date, (due_at AT TIME ZONE %(old)s)::date), "
        "due_at = (coalesce(due_date, (due_at AT TIME ZONE %(old)s)::date) + %(eod)s::time) "
        "AT TIME ZONE %(new)s, "
        "updated_at = now() WHERE due_kind = 'date' AND due_at IS NOT NULL",
        {"old": old, "new": new, "eod": END_OF_DAY.isoformat()},
    )
