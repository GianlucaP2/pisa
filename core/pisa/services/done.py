"""Done by being there (`done`): every minute, an open task with a place or today's event
with a place is closed when the user stayed there 10 minutes (pisa.domain.done), planned or not; by voice,
`mark_done` closes the item the user names. Each one is said in one line ("Fatto: BOBBY'S Bitterballen ✓", a
`done` notification) with Undo for 24 hours.

- A task becomes `done` (a repeating one makes its next occurrence, as any completion); an event, which Pisa
  can't edit in Google, is marked *attended* in Pisa's own data (`calendar_events.attended_at`): its status is
  `done`, so no check-in asks about it.
- A stay closes a task once a day and an event once: an undone one isn't closed again by the same visit.
- A sensitive place (health, worship, politics) closes the item and learns nothing: no place is kept
  with the record, the notification names no place, and the user's undo is no label.
- The user's undo is a label (`feedback_events`), for the learning.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from psycopg_pool import AsyncConnectionPool

from pisa.degrade import degrades
from pisa.domain import done as rules
from pisa.domain import event_kinds, wrist
from pisa.domain.attend import Candidate
from pisa.domain.errors import PisaError
from pisa.domain.learning import sensitive_place
from pisa.domain.owner import Owner
from pisa.domain.tasks import online_task, title_key
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.services import foreign, stays
from pisa.services import home as home_store
from pisa.services.attend import deliver_now
from pisa.services.planner import PlannerService
from pisa.services.tasks import TasksService, feedback

if TYPE_CHECKING:
    from pisa.services.attend import AttendService

_log = get_logger("pisa.done")
Source = Literal["stay", "voice"]

# A stay closes a task once a day, and never one the user reopened today (an Undo, of a stay's close or of a
# voice one).
_TASKS_SQL = (
    "SELECT t.id::text, t.title, t.due_at, t.place_id::text, p.lat, p.lon, p.name, p.category, "
    "t.source_kind, t.place_by_owner, t.created_at FROM tasks t JOIN places p ON p.id = t.place_id "
    "WHERE t.status = 'open' AND NOT EXISTS (SELECT 1 FROM done_by_presence d WHERE d.task_id = t.id "
    "AND d.day = %s AND (d.source = 'stay' OR d.undone_at IS NOT NULL))"
)
# An event is one event whatever the calendars it is in (a shared invite).
WAITS_AHEAD = timedelta(days=3)  # a wait at home this far ahead closes when the thing came
_NOT_ATTENDED = (
    "NOT EXISTS (SELECT 1 FROM calendar_events a WHERE a.id = e.id AND a.attended_at IS NOT NULL) "
)
_EVENTS_SQL = (
    "SELECT DISTINCT ON (e.id) e.calendar_id, e.id, e.title, e.starts_at, e.ends_at, e.place_id::text, "  # noqa: S608
    "p.lat, p.lon, p.name, p.category FROM calendar_events e JOIN places p ON p.id = e.place_id "
    "WHERE NOT e.all_day AND e.status <> 'cancelled' AND " + _NOT_ATTENDED + "AND e.starts_at < %s "
    "AND e.ends_at > %s AND NOT EXISTS (SELECT 1 FROM done_by_presence d WHERE d.event_id = e.id "
    "AND (d.source = 'stay' OR d.undone_at IS NOT NULL)) ORDER BY e.id, e.calendar_id"
)


@dataclass(frozen=True)
class _Item:
    target: dict[str, str]
    title: str
    place_id: str | None
    name: str | None
    category: str | None
    due_day: date | None
    window: tuple[datetime, datetime] | None = None  # an event's: its time ± 1 h (all day: the day)


def _one_occasion(items: list[_Item], today: date, now: datetime) -> bool:
    """A task (at most one) and today's events, all at one place, the task not due on a later day and every
    event happening now (its window): the same visit. Two gym classes at 7 and 19, said at 8, are not
    (M-A)."""
    places = {i.place_id for i in items}
    tasks = [i for i in items if "task_id" in i.target]
    return (
        len(places) == 1
        and None not in places
        and len(tasks) <= 1
        and not any(rules.waits(i.due_day, today) for i in tasks)
        and all(i.window and i.window[0] <= now <= i.window[1] for i in items if "event_id" in i.target)
    )


class DoneService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        tasks: TasksService,
        planner: PlannerService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attend: "AttendService | None" = None,
    ) -> None:
        self._pool = pool
        self._tasks = tasks
        self._planner = planner
        self._owner = owner
        self._clock = clock
        self._attend = attend  # "Fatto: …" is a candidate (it may be folded or shown silently)

    def _day(self, now: datetime) -> tuple[date, datetime, datetime]:
        tz = self._owner.zone
        today = now.astimezone(tz).date()
        start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
        return today, start, datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=tz)

    # --- every minute ---------------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Close what the user stayed at today. Returns how many were closed. One tick at a time (the step's
        own lock); DONE also keeps it apart from their spoken "done" (`mark`)."""
        async with locks.held(self._pool, locks.DONE) as got:
            if not got:
                return 0
            return await self._tick()

    async def _tick(self) -> int:
        now = self._clock()
        today, start, end = self._day(now)
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            seen = [
                rules.Stayed(
                    s["arrived_at"], s["departed_at"], s["last_seen_at"], s["lat"], s["lon"],  # type: ignore[arg-type]
                    s["accuracy_m"], s["place_id"], s["place"],  # type: ignore[arg-type]
                )
                for s in await stays.between(conn, start - rules.EVENT_MARGIN, now)
            ]  # fmt: skip
            if not seen:
                return 0
            tasks = await (await conn.execute(_TASKS_SQL, (today,))).fetchall()
            events = await (await conn.execute(_EVENTS_SQL, (end, start))).fetchall()
            home = await home_store.read(conn)
        moved = await self._moved(today)
        closed = 0
        for task_id, title, due_at, place_id, lat, lon, name, category, source_kind, by_owner, made in tasks:
            if source_kind == "email" and not by_owner and online_task(title):
                continue  # done online: its venue isn't where the user goes (pisa.domain.tasks.task_place_id)
            if rules.waits(due_at.astimezone(tz).date() if due_at else None, today):
                continue
            place = (lat, lon) if lat is not None else None
            stay = rules.stayed_there(seen, place_id, place, start, end, now, made_at=made)
            if stay is not None:
                closed += await self._close(
                    {"task_id": task_id}, title, place_id, name, category, "stay", stay.arrived_at, now
                )
        for calendar_id, event_id, title, begins, ends, place_id, lat, lon, name, category in events:
            place = (lat, lon) if lat is not None else None
            if event_kinds.waits(title, at_home=event_kinds.at_home(place_id, place, home)):
                continue  # being home never ends a wait; the delivery's email does
            # a soft event Pisa moved: its time in the plan
            begins, ends = moved.get(event_id, (begins, ends))
            stay = rules.stayed_there(seen, place_id, place, *rules.event_window(begins, ends), now)
            if stay is not None:
                closed += await self._close(
                    {"calendar_id": calendar_id, "event_id": event_id},
                    title, place_id, name, category, "stay", stay.arrived_at, now,
                )  # fmt: skip
        return closed

    async def _moved(self, today: date) -> dict[str, tuple[datetime, datetime]]:
        """The soft events Pisa moved in today's plan, at their time there."""
        plan = None  # no plan read: nothing moved
        with degrades(_log, "done_moved_read"):
            plan = await self._planner.current(today)
        return {
            i["event_id"]: (datetime.fromisoformat(i["start"]), datetime.fromisoformat(i["end"]))
            for i in (plan or {}).get("items", [])
            if i.get("kind") == "event" and i.get("moved_from") and i.get("event_id")
        }

    async def _close(
        self,
        target: dict[str, str],
        title: str,
        place_id: str | None,
        name: str | None,
        category: str | None,
        source: Source,
        arrived_at: datetime | None,
        now: datetime,
        *,
        say: bool = True,
    ) -> int:
        """Close one item, record it for the Undo, and say it (unless `say` is false: one line for several).
        0 when it is no longer open: another tick, the user or a voice close got there first."""
        today, _, _ = self._day(now)
        learn = not (place_id is not None and sensitive_place(name, category))
        undo_until = now + rules.UNDO_FOR
        record_id = uuid.uuid4()
        task_id = target.get("task_id")
        async with self._pool.connection() as conn, conn.transaction():
            still = await (
                await conn.execute("SELECT 1 FROM tasks WHERE id = %s AND status = 'open'", (task_id,))
                if task_id is not None
                else await conn.execute(
                    "SELECT 1 FROM calendar_events e WHERE e.id = %s AND " + _NOT_ATTENDED + "LIMIT 1",  # noqa: S608
                    (target["event_id"],),
                )
            ).fetchone()
            if still is None:
                return 0
            made = await conn.execute(
                "INSERT INTO done_by_presence (id, task_id, calendar_id, event_id, day, arrived_at, "
                "place_id, learn, title, source, done_at, undo_until, origin) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    record_id, task_id, target.get("calendar_id"), target.get("event_id"), today, arrived_at,
                    place_id if learn else None, learn, title[:300], source, now, undo_until,
                    wrist.current(),  # "l'ho fatto" said on the watch
                ),
            )  # fmt: skip
            if not made.rowcount:
                return 0
            if task_id is None:
                await conn.execute(  # every copy of it
                    "UPDATE calendar_events SET attended_at = %s WHERE id = %s", (now, target["event_id"])
                )
        if task_id is not None:
            try:
                await self._tasks.update(task_id, {"status": "done"})
            except Exception:
                async with self._pool.connection() as conn:  # not closed: it may be closed on the next tick
                    await conn.execute("DELETE FROM done_by_presence WHERE id = %s", (record_id,))
                raise
        if say:  # the place only when Pisa saw the user there (the user's words name no place)
            await self._say(target, title, name if learn and source == "stay" else None, undo_until, now)
        _log.info(
            "done_by_presence", kind="task" if task_id else "event", source=source, learn=learn,
            record_id=str(record_id),
        )  # fmt: skip
        if task_id is not None:  # a voice close leaves the plan too
            await self._replan(task_id)
        return 1

    async def _say(
        self, target: dict[str, str], title: str, place: str | None, undo_until: datetime, now: datetime
    ) -> None:
        tz = self._owner.zone
        key = "task_id" if "task_id" in target else "event_id"
        payload: dict[str, object] = {
            key: target[key], "title": title[:200], "place": place[:200] if place else None,
            "undo_until": undo_until.astimezone(tz).isoformat(),
        }  # fmt: skip
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            text = rules.done_text(title, lang)
            # Held by quiet mode it goes out in the one summary, which has no Undo button.
            undo = "annulla dall'app" if lang == "it-IT" else "undo it in the app"
            later = f"{text} ({undo})"
            # "Fatto: <title>" of an item whose title is a stranger's is outside text
            outside = await foreign.item_foreign(conn, target)
            candidate = Candidate(
                str(uuid.uuid4()), "stay", "done", text, lang, now, alone=True, outside=outside
            )
            delivery = {"done": payload, "summary": later}
            if self._attend is not None:
                ref = f"done:{key}:{target[key]}:{payload['undo_until']}"
                await self._attend.propose(conn, candidate, ref, delivery)
            else:
                await deliver_now(conn, candidate, delivery, timezone=self._owner.tz)

    async def _replan(self, task_id: str) -> None:
        """The task leaves today's plan at once (when it was in it)."""
        try:
            plan = await self._planner.current()  # a plan made before this close is made again here
            if plan and any(i.get("task_id") == task_id for i in plan.get("items", [])):
                await self._planner.plan(reason="done by being there")
        except Exception as exc:  # the next re-plan trigger catches up
            _log.warning("done_replan_failed", cause=exc.__class__.__name__)

    # --- by voice -------------------------------------------------------------------------------------------

    async def mark(self, what: str, pick: int | None = None) -> dict[str, Any]:
        """`mark_done` (the user said "ci sono stato", "fatto", "l'ho comprato"): the open task or today's
        event matching `what` (its title or its place) is closed. {"done": [items]}, {"ask": [titles]} when
        several match (one question; the answer picks one by its title or `pick`, its number in the list), or
        {"none": True}. A task and today's event at the same place, the task not due on a later day, are one
        occasion: both close, in one line (a lunch planned at the snack bar the task names)."""
        now = self._clock()
        today, start, end = self._day(now)
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            tasks = await (
                await conn.execute(
                    "SELECT t.id::text, t.title, p.id::text, p.name, p.category, t.due_at FROM tasks t "
                    "LEFT JOIN places p ON p.id = t.place_id WHERE t.status = 'open' "
                    # one order for the question and the pick: "il secondo" names the same task both times
                    "ORDER BY t.created_at, t.title, t.id"
                )
            ).fetchall()
            events = await (
                await conn.execute(
                    "SELECT DISTINCT ON (e.id) e.calendar_id, e.id, e.title, p.id::text, p.name, p.category, "  # noqa: S608
                    "e.all_day, e.starts_at, e.ends_at FROM calendar_events e LEFT JOIN places p "
                    "ON p.id = e.place_id WHERE e.status <> 'cancelled' AND "
                    + _NOT_ATTENDED
                    + "AND ((NOT e.all_day AND e.starts_at < %s AND "
                    "e.ends_at > %s) OR (e.all_day AND e.start_date <= %s AND e.end_date > %s)) "
                    "ORDER BY e.id, e.calendar_id",
                    (end, start, today, today),
                )
            ).fetchall()
        items: list[_Item] = [
            _Item({"task_id": tid}, title, pid, name, category, due.astimezone(tz).date() if due else None)
            for tid, title, pid, name, category, due in tasks
        ] + [
            _Item(
                {"calendar_id": cid, "event_id": eid},
                title,
                pid,
                name,
                category,
                None,
                (start, end) if all_day else rules.event_window(starts, ends),
            )
            for cid, eid, title, pid, name, category, all_day, starts, ends in events
        ]
        found = rules.matches(what, [(i.title, i.name) for i in items])
        exact = [i for i in found if title_key(items[i].title) == title_key(what)]
        found = exact or found
        if len(found) > 1 and pick is not None and 1 <= pick <= len(found[:3]):
            found = [found[pick - 1]]
        chosen = [items[i] for i in found]
        if len(chosen) > 1 and not _one_occasion(chosen, today, now):
            return {"ask": [i.title for i in chosen[:3]]}
        targets = [c.target for c in chosen]
        chosen += [i for i in await self._waits_too(what, chosen, now) if i.target not in targets]
        if not chosen:
            return {"none": True}
        closed: list[dict[str, str]] = []
        async with locks.held(self._pool, locks.DONE, wait=True):  # never at once with a stay's tick
            for item in chosen:
                if await self._close(
                    item.target, item.title, item.place_id, item.name, item.category, "voice", None, now,
                    say=False,
                ):  # fmt: skip
                    closed.append(item.target | {"title": item.title})
        if closed:
            first = closed[0]  # the task when there is one (it comes first), else the event
            await self._say(first, first["title"], None, now + rules.UNDO_FOR, now)
        return {"done": closed}

    async def _waits_too(self, what: str, chosen: "list[_Item]", now: datetime) -> "list[_Item]":
        """The user's words that close a thing's task close its wait at home too, whatever its
        day (the thing's own events and tasks), and a wait at home in the next days that their words name."""
        tasks = [i.target["task_id"] for i in chosen if "task_id" in i.target]
        async with self._pool.connection() as conn:
            home = await home_store.read(conn)
            rows = await (
                await conn.execute(
                    "SELECT e.calendar_id, e.id, e.title, p.id::text, p.name, p.category, p.lat, p.lon, "  # noqa: S608
                    "e.starts_at, e.ends_at, EXISTS (SELECT 1 FROM thing_items ti JOIN thing_items mine "
                    "ON mine.thing_id = ti.thing_id AND mine.item_kind = 'task' AND mine.item_ref = ANY(%s) "
                    "WHERE ti.item_kind = 'event' AND ti.item_ref = e.id) FROM calendar_events e "
                    "LEFT JOIN places p ON p.id = e.place_id WHERE NOT e.all_day AND e.status <> 'cancelled' "
                    "AND " + _NOT_ATTENDED + "AND e.ends_at > %s AND e.starts_at < %s",
                    (tasks, now - timedelta(hours=12), now + WAITS_AHEAD),
                )
            ).fetchall()
            siblings = await (
                await conn.execute(
                    "SELECT DISTINCT t.id::text, t.title FROM thing_items mine JOIN thing_items ti "
                    "ON ti.thing_id = mine.thing_id AND ti.item_kind = 'task' JOIN tasks t "
                    "ON t.id::text = ti.item_ref WHERE mine.item_kind = 'task' AND mine.item_ref = ANY(%s) "
                    "AND t.status = 'open'",
                    (tasks,),
                )
            ).fetchall() if tasks else []  # fmt: skip
        out: list[_Item] = [_Item({"task_id": tid}, title, None, None, None, None) for tid, title in siblings]
        for cid, eid, title, pid, name, category, lat, lon, starts, ends, linked in rows:
            coord = (lat, lon) if lat is not None and lon is not None else None
            if not event_kinds.waits(title, at_home=event_kinds.at_home(pid, coord, home)):
                continue
            if linked or rules.matches(what, [(title, name)]):
                out.append(_Item({"calendar_id": cid, "event_id": eid}, title, pid, name, category, None,
                                 rules.event_window(starts, ends)))  # fmt: skip
        return out

    # --- undo -----------------------------------------------------------------------------------------------

    async def undo_task(self, task_id: str) -> None:
        """POST /v1/tasks/{id}/undo-done: the task is open again (within 24 h of Pisa closing it)."""
        record = await self._undo({"task_id": task_id}, "task")
        await self._tasks.update(task_id, {"status": "open"})
        await self._label(record, f"task:{task_id}", "done_undone")

    async def undo_event(self, event_id: str) -> None:
        """POST /v1/events/{id}/undo-attended: the event is no longer attended (within 24 h)."""
        record = await self._undo({"event_id": event_id}, "event")
        async with self._pool.connection() as conn:
            await conn.execute(  # every copy of it
                "UPDATE calendar_events SET attended_at = NULL WHERE id = %s", (event_id,)
            )
        await self._label(record, f"event:{event_id}", "attended_undone")

    async def _undo(self, target: dict[str, str], kind: str) -> dict[str, Any]:
        now = self._clock()
        column = "task_id" if kind == "task" else "event_id"
        if kind == "task":
            try:
                uuid.UUID(target["task_id"])
            except ValueError:
                raise PisaError("not_found", "There is no such task.", "Refresh the tasks") from None
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    f"SELECT id, calendar_id, undo_until, learn FROM done_by_presence WHERE {column} = %s "  # noqa: S608
                    "AND undone_at IS NULL ORDER BY done_at DESC LIMIT 1 FOR UPDATE",
                    (target[column],),
                )
            ).fetchone()
            if row is None:
                raise PisaError(
                    "not_found",
                    f"Pisa didn't mark this {kind} "
                    f"{'done' if kind == 'task' else 'attended'}: nothing to undo.",
                    "Refresh",
                )
            record_id, calendar_id, undo_until, learn = row
            if undo_until <= now:
                raise PisaError(
                    "undo_expired",
                    "The 24 hours to undo this are over.",
                    "Reopen the task" if kind == "task" else "Nothing to do",
                )
            await conn.execute("UPDATE done_by_presence SET undone_at = %s WHERE id = %s", (now, record_id))
        _log.info("done_undone", kind=kind, record_id=str(record_id))
        return {"id": record_id, "calendar_id": calendar_id, "learn": learn}

    async def _label(self, record: dict[str, Any], subject: str, kind: str) -> None:
        if not record["learn"]:
            return  # a sensitive place: nothing is learned, not even the undo
        async with self._pool.connection() as conn:
            await feedback(conn, kind, subject, "done", "open", self._clock())
