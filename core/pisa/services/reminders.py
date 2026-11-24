"""Reminders (no model): timed events at start - event_lead_min, tasks with a due date-time at
due_at - task_lead_min, and "leave now" for each trip of the latest plan at leave_at - leave_lead_min (when
planner.leave_now is on).

- The worker checks every minute (one run at a time: its step's job lock). Anything whose reminder time
  fell in the last 5 minutes goes out (the catch-up after a restart); older misses since the previous check
  are skipped and logged.
- Each (kind, ref, fire_at) fires at most once (`reminders_sent`). A moved event or task has a new fire_at,
  so it gets a new reminder; a cancelled event (gone from the calendar) or a done task gets none.
- An event shown in two calendars has one id: one reminder. Reminders are `normal`: quiet mode holds them.
- A task that got its time inside its lead ("tra 5 minuti" with a 15-minute lead: its lead moment was
  already past when it was written) is reminded at its due time, never skipped.
- A timed reminder (`remember_to`, condition time: a `pisa_promises` row) fires at its exact minute, with no
  lead, once (its `fired_at`), as a floor: the push when the app is closed.
- "Leave now" is one per (target, day): a re-plan that moves the trip (a move while
  travelling, a new traffic slot) sends it again only when the leave time moved at least 10 minutes earlier,
  none was sent within the trip's length, and the user isn't already getting closer to the target.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import talk as form
from pisa.domain.attend import Candidate, promise_text
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.domain.reminders import (
    Kind,
    leave_summary,
    leave_text,
    minutes_until,
    reminder_summary,
    reminder_text,
)
from pisa.logging import get_logger
from pisa.services import foreign, guardian
from pisa.services.attend import deliver_now

if TYPE_CHECKING:
    from pisa.services.attend import AttendService

CATCH_UP = timedelta(minutes=5)
MAX_LOOKBACK = timedelta(days=1)  # how far back misses are looked for after a long outage
CHECKED_KEY = "reminders_checked_at"  # app_settings: the previous check
LEAVE_RESEND_EARLIER = timedelta(minutes=10)  # a re-sent "leave now" needs a leave time this much earlier
LEAVE_RESEND_LATER = timedelta(minutes=30)  # or the trip's length, whichever is longer
ARRIVED_M = 100  # closer than this to the target: no "leave now"
CLOSER_M = 100  # nearer the target than at the last "leave now" by this much: on the way, no repeat
FLOOR_BEFORE = timedelta(minutes=5)  # an event's reminder reaches the user by its start minus this
_log = get_logger("pisa.reminders")


@dataclass(frozen=True)
class Due:
    kind: Kind
    ref: str
    title: str
    place: str | None
    start: datetime
    fire_at: datetime
    minutes: int | None = None  # leave: the trip
    mode: str | None = None
    to_place_id: str | None = None  # leave: where the trip goes
    # its title or place is a stranger's (an invite, Gmail's, a shared calendar's, Pisa's
    # after outside text, an email's task): the attend model reads it as outside text, never Pisa's own words
    outside: bool = False


@dataclass
class ReminderReport:
    sent: int = 0
    skipped: int = 0
    texts: list[str] = field(default_factory=list)


class RemindersService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attend: "AttendService | None" = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._attend = attend  # a candidate to decide; None: today's rules at once

    # RAIL: a fixed event's reminder and leave-now always go, at their lead times; the
    # planner's leave_at is a FACT.
    async def run(self) -> ReminderReport:
        """The worker's `reminders` step (one run at a time: the step's own lock)."""
        async with self._pool.connection() as conn, conn.transaction():
            return await self._check(conn, self._clock())

    async def _check(self, conn: AsyncConnection, now: datetime) -> ReminderReport:
        settings = await self._owner.settings(conn)
        lang = settings["language"]
        recent = now - CATCH_UP
        last = await get_setting(conn, CHECKED_KEY)
        since = min(recent, max(datetime.fromisoformat(last), now - MAX_LOOKBACK)) if last else recent
        report = ReminderReport()
        found = await _candidates(conn, settings["reminders"], since, now)
        if settings["planner"]["leave_now"]:
            found += await _leaves(conn, settings["planner"]["leave_lead_min"], since, now)
        lead = timedelta(minutes=settings["planner"]["leave_lead_min"])
        for due in sorted(found, key=lambda d: d.fire_at):
            if due.kind == "leave" and not await _leave_again(conn, due, lead, now):
                continue
            outcome = "sent" if (due.start if due.kind == "leave" else due.fire_at) > recent else "skipped"
            inserted = await (
                await conn.execute(
                    "INSERT INTO reminders_sent (kind, ref, fire_at, outcome, created_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1",
                    (due.kind, due.ref, due.fire_at, outcome, now),
                )
            ).fetchone()
            if inserted is None:
                continue  # already sent (or skipped) for this fire_at
            if outcome == "skipped":
                report.skipped += 1
                _log.warning(
                    "reminder_skipped",
                    kind=due.kind,
                    ref=due.ref,
                    late_min=int((now - due.fire_at).total_seconds() // 60),
                )
                continue
            if due.kind == "leave":
                text = leave_text(due.title, due.minutes or 0, due.mode or "transit", lang)
                summary = leave_summary(due.title, due.start, lang, self._owner.tz)
                urgency = form.urgency("leave_now")  # time-critical, said at once
            else:
                left = minutes_until(due.start, now)
                text = reminder_text(due.kind, due.title, due.place, left, lang)
                summary = reminder_summary(due.kind, due.title, due.start, lang, self._owner.tz)
                urgency = form.urgency("reminder", minutes_left=left)
            # RAIL: a floor: never dropped, before the event (start - 5 min) or the leave time.
            candidate = Candidate(
                str(uuid.uuid4()), "leave" if due.kind == "leave" else "reminder", "reminder", text, lang,
                now,
                urgency, deadline=_deadline(due), floor=True,
                speak="leave_now" if due.kind == "leave" else None,
                outside=due.outside,  # a stranger's title is outside text for the model
            )  # fmt: skip
            delivery = {"summary": summary, "event_at": due.start.isoformat()}
            if due.kind == "event" and (person := await guardian.event_person(conn, due.ref)) is not None:
                delivery["person_id"] = str(person)  # their "always for Marta"
            if self._attend is not None:
                await self._attend.propose(
                    conn, candidate, f"{due.kind}:{due.ref}:{due.fire_at.isoformat()}", delivery
                )
            else:
                await deliver_now(conn, candidate, delivery, timezone=self._owner.tz)
            report.sent += 1
            report.texts.append(text)
        await self._promises(conn, lang, recent, now, report)
        await set_setting(conn, CHECKED_KEY, Jsonb(now.isoformat()))
        if report.sent or report.skipped:
            _log.info("reminders_checked", sent=report.sent, skipped=report.skipped)
        return report

    async def _promises(
        self, conn: AsyncConnection, lang: str, recent: datetime, now: datetime, report: ReminderReport
    ) -> None:
        """Timed reminders (`remember_to`) whose minute has come: each once, at its exact time (no lead), a
        floor like a task's reminder. One older than the catch-up is skipped and logged, as a task's is."""
        rows = await (
            await conn.execute(
                "SELECT id, what, lang, provenance, due_at FROM pisa_promises WHERE condition = 'time' "
                "AND fired_at IS NULL AND cancelled_at IS NULL AND due_at <= %s ORDER BY due_at "
                "FOR UPDATE SKIP LOCKED",
                (now,),
            )
        ).fetchall()
        for promise_id, what, said_in, origin, due_at in rows:
            await conn.execute("UPDATE pisa_promises SET fired_at = %s WHERE id = %s", (now, promise_id))
            if due_at <= recent:
                report.skipped += 1
                _log.warning(
                    "reminder_skipped", kind="promise", ref=str(promise_id),
                    late_min=int((now - due_at).total_seconds() // 60),
                )  # fmt: skip
                continue
            text = promise_text(what, said_in or lang)
            candidate = Candidate(
                str(uuid.uuid4()), "reminder", "reminder", text, said_in or lang, now,
                form.urgency("reminder", minutes_left=0), deadline=now + CATCH_UP, floor=True,
                outside=origin == "outside_text",
            )  # fmt: skip
            delivery = {"summary": what, "event_at": due_at.isoformat()}
            if self._attend is not None:
                await self._attend.propose(conn, candidate, f"promise:{promise_id}", delivery)
            else:
                await deliver_now(conn, candidate, delivery, timezone=self._owner.tz)
            report.sent += 1
            report.texts.append(text)


def _deadline(due: Due) -> datetime:
    """Until when a floor is still news. A leave-now: the trip's start; another reminder: its start minus
    FLOOR_BEFORE. Never before its own fire time plus the catch-up, so attend_floors, a few seconds later in
    the same minute, delivers it and never expires it (a near task fires at its due time: its start minus
    FLOOR_BEFORE was already past)."""
    until = due.start if due.kind == "leave" else due.start - FLOOR_BEFORE
    return max(until, due.fire_at + CATCH_UP)


async def _leaves(conn: AsyncConnection, lead_min: int, since: datetime, now: datetime) -> list[Due]:
    """ "Leave now" (PL-08): each travel item of the latest plan, at leave_at - lead, once per (travel,
    leave_at). The trip leads to the task or event right after it."""
    rows = await (
        await conn.execute(
            "SELECT DISTINCT ON (day) plan FROM plans WHERE generated_at > %s "
            "ORDER BY day, generated_at DESC",
            (now - MAX_LOOKBACK,),
        )
    ).fetchall()
    lead = timedelta(minutes=lead_min)
    out: list[Due] = []
    for (plan,) in rows:
        items = plan.get("items") or []
        for i, item in enumerate(items):
            if item.get("kind") != "travel" or not item.get("leave_at"):
                continue
            leave_at = datetime.fromisoformat(item["leave_at"])
            # Due from leave_at - lead until the leave time passes: a plan made after that moment (already
            # late, or a re-plan) still announces its trip.
            if not (since < leave_at and leave_at - lead <= now):
                continue
            target = next((t for t in items[i + 1 :] if t.get("kind") in ("task", "event")), None)
            if target is None:
                continue
            target_id = target.get("task_id") or target.get("event_id") or ""
            out.append(
                Due(
                    "leave",
                    f"{target_id}@{plan['date']}",  # one per (target, day)
                    target["title"],
                    None,
                    leave_at,
                    leave_at - lead,
                    item["minutes"],
                    item["mode"],
                    item.get("to_place_id"),
                    outside=await foreign.item_foreign(conn, target),
                )
            )
    return out


async def _leave_again(conn: AsyncConnection, due: Due, lead: timedelta, now: datetime) -> bool:
    """Whether this "leave now" may go out: the first for its (target, day), or a trip that moved at least
    10 minutes earlier, or later by more than max(trip, 30 min) (a task moved to the afternoon), more than a
    trip's length after the last one, while the user isn't on the way or already there."""
    last = await (
        await conn.execute(
            "SELECT fire_at, created_at FROM reminders_sent WHERE kind = 'leave' AND ref = %s "
            "AND outcome = 'sent' ORDER BY created_at DESC LIMIT 1",
            (due.ref,),
        )
    ).fetchone()
    if last is None:
        return True
    fire_at, sent_at = last
    previous = fire_at + lead  # the leave time of the last "leave now"
    earlier = due.start <= previous - LEAVE_RESEND_EARLIER
    later = due.start >= previous + max(timedelta(minutes=due.minutes or 0), LEAVE_RESEND_LATER)
    if not (earlier or later):
        return False  # the same trip, drifting while the user travels
    if now - sent_at < timedelta(minutes=due.minutes or 0):
        return False  # the last one is still on: the user may be travelling
    if due.to_place_id is None:
        return True
    row = await (
        await conn.execute(
            "SELECT lat, lon FROM places WHERE id = %s AND lat IS NOT NULL", (due.to_place_id,)
        )
    ).fetchone()
    target = (row[0], row[1]) if row else None
    then, here = await _location_at(conn, sent_at), await _location_at(conn, now)
    if target is None or then is None or here is None:
        return True
    if distance_m(here, target) < ARRIVED_M:
        return False  # already there
    closer = distance_m(then, target) - distance_m(here, target)
    return closer < CLOSER_M  # getting nearer the target: already on the way


async def _location_at(conn: AsyncConnection, moment: datetime) -> tuple[float, float] | None:
    row = await (
        await conn.execute(
            "SELECT lat, lon FROM device_locations WHERE measured_at <= %s ORDER BY measured_at DESC LIMIT 1",
            (moment,),
        )
    ).fetchone()
    return (row[0], row[1]) if row else None


async def _candidates(
    conn: AsyncConnection, leads: dict[str, int], since: datetime, now: datetime
) -> list[Due]:
    """Reminders whose time is in (since, now]: start in (since + lead, now + lead]."""
    event_lead = timedelta(minutes=leads["event_lead_min"])
    task_lead = timedelta(minutes=leads["task_lead_min"])
    events = await (
        await conn.execute(
            "SELECT DISTINCT ON (e.id) e.id, e.title, e.location, e.starts_at, "  # noqa: S608 (constants)
            f"bool_or(coalesce({foreign.FOREIGN_EVENT}, true)) OVER (PARTITION BY e.id) "
            f"FROM {foreign.EVENTS} "
            "WHERE NOT e.all_day AND e.starts_at > %s AND e.starts_at <= %s ORDER BY e.id, e.calendar_id",
            (since + event_lead, now + event_lead),
        )
    ).fetchall()
    tasks = await (
        await conn.execute(
            f"SELECT t.id, t.title, t.place_text, t.due_at, {foreign.FOREIGN_TASK} FROM tasks t "  # noqa: S608
            "WHERE t.status = 'open' AND t.due_kind = 'datetime' "
            "AND t.priority <> 3 AND t.due_at > %s AND t.due_at <= %s",  # low is never reminded
            (since + task_lead, now + task_lead),
        )
    ).fetchall()
    # a task that got its time inside its lead (its lead moment already past when it was written or moved)
    # and was never reminded for it: at its due time, never skipped
    near = await (
        await conn.execute(
            f"SELECT t.id, t.title, t.place_text, t.due_at, {foreign.FOREIGN_TASK} FROM tasks t "  # noqa: S608
            "WHERE t.status = 'open' AND t.due_kind = 'datetime' AND t.priority <> 3 "
            "AND t.due_at > %s AND t.due_at <= %s AND t.due_at - %s < t.updated_at "
            "AND NOT EXISTS (SELECT 1 FROM reminders_sent r WHERE r.kind = 'task' AND r.ref = t.id::text "
            "AND r.fire_at >= t.due_at - %s)",
            (since, now, task_lead, task_lead),
        )
    ).fetchall()
    out = [
        Due("event", ref, title, place, start, start - event_lead, outside=bool(strangers))
        for ref, title, place, start, strangers in events
    ]
    out += [
        Due("task", str(ref), title, place, due, due - task_lead, outside=bool(from_mail))
        for ref, title, place, due, from_mail in tasks
    ]
    leading = {d.ref for d in out if d.kind == "task"}
    out += [
        Due("task", str(ref), title, place, due, due, outside=bool(from_mail))
        for ref, title, place, due, from_mail in near
        if str(ref) not in leading
    ]
    return sorted(out, key=lambda d: d.fire_at)
