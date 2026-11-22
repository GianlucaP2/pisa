"""Tasks and the agenda. Days are the user's (PISA_TIMEZONE, or where the phone is)."""

import builtins
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, LiteralString
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection, sql
from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import aging, countries
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.reasons import retriage_kind, task_reason
from pisa.domain.tasks import (
    DELETED_KEPT,
    DURATION_MAX,
    DURATION_MIN,
    LOW,
    TITLE_MAX,
    TITLE_MIN,
    DueKind,
    SourceKind,
    Task,
    check_repeat,
    end_of_day,
    latest_due,
    local_day_bounds,
    next_due,
    rfc3339,
    sort_key,
    title_key,
)
from pisa.logging import get_logger
from pisa.services import foreign
from pisa.services.settings import load as load_settings

AGENDA_DEFAULT_DAYS = 2
AGENDA_MAX_DAYS = 14
NOTES_MAX, PLACE_MAX = 2_000, 200
TIMED_DEFAULT_MIN = 30  # a task said with its own time and no length ("alle 19 mi preparo")
PRIORITY_REASON_MAX = 300
_COLUMNS: LiteralString = (
    "id::text, title, notes, status, due_at, due_kind, duration_min, place_text, priority, source_kind, "
    "source_ref, source_label, confidence, created_at, updated_at, place_id::text, place_elsewhere, repeat, "
    "place_by_owner, priority_reason, priority_source, expected_from, expected_until, "
    "deadline_source, country, country_by_owner, private, "
    # its email's category and notice, for the reason's check on read (`task_json`)
    "(SELECT m.intake_category FROM email_messages m WHERE tasks.source_kind = 'email' "
    "AND m.id = tasks.source_ref) AS email_category, "
    "(SELECT m.intake_notice FROM email_messages m WHERE tasks.source_kind = 'email' "
    "AND m.id = tasks.source_ref) AS email_notice"
)
_SELECT: LiteralString = "SELECT " + _COLUMNS + " FROM tasks"  # noqa: S608 (constants only)
_RETURNING: LiteralString = " RETURNING " + _COLUMNS
_INSERT: LiteralString = (
    "INSERT INTO tasks (id, title, notes, status, due_at, due_kind, duration_min, place_text, priority, "
    "source_kind, source_ref, source_label, title_key, confidence, created_at, updated_at, repeat, "
    "series_id, previous_id, place_by_owner, priority_reason, expected_from, expected_until, "
    "deadline_source) "
    "VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s) "
    # The same title from the same email (tasks_email_dedupe), or a second next occurrence (tasks_one_next).
    "ON CONFLICT DO NOTHING"
)
_log = get_logger("pisa.tasks")


async def feedback(
    conn: AsyncConnection, kind: str, subject: str, before: str | None, after: str | None, now: datetime
) -> None:
    """The user changed one of Pisa's decisions: kept as a label. Ids and values only."""
    await conn.execute(
        "INSERT INTO feedback_events (id, kind, subject, from_value, to_value, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), kind, subject, before, after, now),
    )


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Fix the task and try again")


def clean_title(title: str) -> str:
    title = " ".join(title.split())
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        raise _invalid(f"A task title must be {TITLE_MIN}-{TITLE_MAX} characters long.")
    return title


def parse_due(value: str, kind: DueKind, timezone: str, now: datetime) -> datetime:
    """`date`: that local day at 23:59 (YYYY-MM-DD or any date-time on that local day). `datetime`: an
    RFC 3339 date-time; without an offset it is local time."""
    text = value.strip()
    try:
        if kind == "date":
            if len(text) == 10:
                day = date.fromisoformat(text)
            else:
                moment = datetime.fromisoformat(text)
                tz = ZoneInfo(timezone)
                day = (moment if moment.tzinfo else moment.replace(tzinfo=tz)).astimezone(tz).date()
            due = end_of_day(day, timezone)
        else:
            moment = datetime.fromisoformat(text)
            due = moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo(timezone))
    except ValueError:
        raise _invalid(f"The due date {text[:40]!r} is not an RFC 3339 date or date-time.") from None
    if due > latest_due(now):
        raise _invalid("A due date can be at most 2 years ahead.")
    return due


def task_json(task: Task, timezone: str, lang: str) -> dict[str, Any]:
    """A task as the phone reads it; its reason in their Settings language (`reasons.task_reason`)."""
    return {
        "id": task.id,
        "title": task.title,
        "notes": task.notes,
        "status": task.status,
        "due_at": rfc3339(task.due_at, timezone) if task.due_at else None,
        "due_kind": task.due_kind,
        "duration_min": task.duration_min,
        "place_text": task.place_text,
        "place_id": task.place_id,
        "priority": task.priority,
        "priority_reason": task_reason(task, lang),
        "priority_source": task.priority_source,
        "deadline_source": (task.deadline_source or _default_source(task.source_kind))
        if task.due_at
        else None,
        "repeat": task.repeat,
        "private": task.private,  # never named on the wrist
        "country": task.country,  # bound to one country (out of the plan in the other)
        # when the user said to do it (a block at that time); a delivery's window start too
        "start_at": rfc3339(task.expected_from, timezone) if task.expected_from else None,
        "source": {"kind": task.source_kind, "ref": task.source_ref, "label": task.source_label},
        "confidence": task.confidence,
        "created_at": rfc3339(task.created_at, timezone),
        "updated_at": rfc3339(task.updated_at, timezone),
    }


def current_country_by_owner(task: Task) -> bool:
    return task.country_by_owner


def _default_source(source_kind: str) -> str:
    return "email" if source_kind == "email" else "owner"


async def insert_task(
    conn: AsyncConnection,
    *,
    title: str,
    now: datetime,
    source_kind: SourceKind,
    notes: str | None = None,
    due_at: datetime | None = None,
    due_kind: DueKind = "none",
    duration_min: int | None = None,
    place_text: str | None = None,
    priority: int = 2,
    source_ref: str | None = None,
    source_label: str | None = None,
    confidence: float | None = None,
    repeat: dict[str, Any] | None = None,
    series_id: str | None = None,
    previous_id: str | None = None,
    place_by_owner: bool = False,
    priority_reason: str | None = None,
    expected_from: datetime | None = None,
    expected_until: datetime | None = None,
    deadline_source: str | None = None,
) -> Task | None:
    """Insert one task. For email tasks, a task with the same normalised title from the same message is not
    inserted again, nor a second next occurrence of a repeating one (returns None)."""
    cur = conn.cursor(row_factory=class_row(Task))
    return await (
        await cur.execute(
            _INSERT + _RETURNING,
            (
                uuid.uuid4(),
                title,
                notes,
                due_at,
                due_kind,
                duration_min,
                place_text,
                priority,
                source_kind,
                source_ref,
                source_label,
                title_key(title),
                confidence,
                now,
                now,
                Jsonb(repeat) if repeat else None,
                series_id,
                previous_id,
                place_by_owner,
                priority_reason[:PRIORITY_REASON_MAX] if priority_reason else None,
                expected_from,
                expected_until,
                # the deadline's source: given, else the email's for an email task, else the user's
                None if due_at is None else deadline_source or _default_source(source_kind),
            ),
        )
    ).fetchone()


class TasksService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    @property
    def owner(self) -> Owner:
        """The user's context (the assistant's reads use the tasks' Owner)."""
        return self._owner

    @property
    def tz(self) -> str:
        """Their zone now (the Owner's): the bulk changes and the assistant's reads use the tasks' day."""
        return self._owner.tz

    def now(self) -> datetime:
        return self._clock()

    async def list(self, status: str = "open", *, low: bool | None = None) -> list[Task]:
        """`low`: True only the low ones, False none of them (what plans, reminds and briefs), None
        all."""
        if status not in ("open", "done", "dismissed", "all"):
            raise _invalid(f"The status {status!r} is not one of open, done, dismissed or all.")
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(Task))
            if status == "all":  # a deleted task is gone for every list
                await cur.execute(_SELECT + " WHERE status <> 'deleted'")
            else:
                await cur.execute(_SELECT + " WHERE status = %s", (status,))
            tasks = await cur.fetchall()
        if low is not None:
            tasks = [t for t in tasks if (t.priority == LOW) == low]
        now = self.now()
        return sorted(tasks, key=lambda t: sort_key(t, now))

    async def get(self, task_id: str) -> Task:
        try:
            parsed = uuid.UUID(task_id)
        except ValueError:
            raise PisaError(
                "not_found", f"There is no task with id {task_id[:40]!r}.", "Refresh the task list"
            ) from None
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(Task))
            task = await (await cur.execute(_SELECT + " WHERE id = %s", (parsed,))).fetchone()
        if task is None or task.status == "deleted":  # deleted by voice: gone, unless restored
            raise PisaError("not_found", f"There is no task with id {task_id}.", "Refresh the task list")
        return task

    async def _known_place(self, place_id: str) -> tuple[uuid.UUID, str]:
        """(id, name) of a place Pisa knows, or not_found."""
        try:
            parsed = uuid.UUID(place_id)
        except ValueError:
            parsed = None
        row = None
        if parsed is not None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute("SELECT name FROM places WHERE id = %s", (parsed,))
                ).fetchone()
        if parsed is None or row is None:
            raise PisaError(
                "not_found", f"There is no place with id {place_id[:40]!r}.", "Search for the place again"
            )
        return parsed, row[0]

    async def _reread(self, conn: AsyncConnection, task_id: str) -> Task:
        cur = conn.cursor(row_factory=class_row(Task))
        task = await (await cur.execute(_SELECT + " WHERE id = %s", (task_id,))).fetchone()
        assert task is not None
        return task

    async def create(
        self,
        *,
        title: str,
        source_kind: SourceKind,
        notes: str | None = None,
        due_at: str | None = None,
        due_kind: DueKind | None = None,
        duration_min: int | None = None,
        place_text: str | None = None,
        priority: int | None = None,
        source_ref: str | None = None,
        repeat: dict[str, Any] | None = None,
        place_id: str | None = None,
        start_at: str | None = None,
    ) -> Task:
        """`place_id`: a place Pisa knows (the places card's "Plan it"): the task is at that
        place, and its name is the place text unless one is given. `start_at`: when the user said
        to do it ("dalle 19 per 45 minuti"): a block at that time in the plan, for its duration, never a
        deadline the planner may put earlier."""
        now = self.now()
        if start_at and not due_at:
            due_at, due_kind = start_at, "datetime"
        kind: DueKind = due_kind or (_implied_kind(due_at) if due_at else "none")
        if kind != "none" and not due_at:
            raise _invalid(f"A task with due_kind {kind!r} needs a due_at.")
        due = parse_due(due_at, kind, self._owner.tz, now) if due_at and kind != "none" else None
        starts = parse_due(start_at, "datetime", self._owner.tz, now) if start_at else None
        ends = starts + timedelta(minutes=_duration(duration_min) or TIMED_DEFAULT_MIN) if starts else None
        place, place_name = await self._known_place(place_id) if place_id is not None else (None, None)
        async with self._pool.connection() as conn:
            task = await insert_task(
                conn,
                title=clean_title(title),
                now=now,
                source_kind=source_kind,
                notes=_text(notes, NOTES_MAX, "notes"),
                due_at=due,
                due_kind=kind if due else "none",
                duration_min=_duration(duration_min),
                place_text=_text(place_text, PLACE_MAX, "place"),
                priority=_priority(priority),
                source_ref=source_ref,
                repeat=check_repeat(repeat),
                # The user's own place always holds, even on "buy…"; an email's doesn't.
                place_by_owner=source_kind != "email" and bool(place_text or place_id),
                expected_from=starts,
                expected_until=ends,
            )
            assert task is not None
            if place_id is not None:
                await conn.execute(
                    "UPDATE tasks SET place_id = %s, place_text = coalesce(place_text, %s) WHERE id = %s",
                    (place, place_name, task.id),
                )
                task = await self._reread(conn, task.id)
        _log.info("task_created", task_id=task.id, source=source_kind)
        return task

    async def update(self, task_id: str, changes: Mapping[str, Any], *, label: bool = True) -> Task:
        """Apply any subset of title, notes, status, due_at, due_kind, duration_min, place_text, priority,
        repeat. Completing or dismissing a repeating task makes its next occurrence (dismissing
        ends only this one); reopening it removes that next one if it is untouched; repeat null stops the
        series. A status change applies once: a concurrent second one makes no second next occurrence."""
        current = await self.get(task_id)
        now = self.now()
        values: dict[str, Any] = {}
        if "title" in changes:
            values["title"] = clean_title(changes["title"] or "")
            values["title_key"] = title_key(values["title"])
            if not current_country_by_owner(current) and values["title"] != current.title:
                values["country"], values["country_checked"] = None, False  # read again
        if "country" in changes:  # their word on where it can be done (null: anywhere)
            if changes["country"] is not None and not countries.is_code(changes["country"]):
                raise _invalid("The country must be an ISO 3166-1 alpha-2 code (IT, DE, ...) or null.")
            values["country"], values["country_checked"] = changes["country"], True
            values["country_by_owner"] = True
        if "private" in changes:  # their "private" (the wrist shows "Impegno")
            if not isinstance(changes["private"], bool):
                raise _invalid("private must be true or false.")
            values["private"] = changes["private"]
        if "notes" in changes:
            values["notes"] = _text(changes["notes"], NOTES_MAX, "notes")
        if "place_text" in changes:
            values["place_text"] = _text(changes["place_text"], PLACE_MAX, "place")
            values["place_by_owner"] = values["place_text"] is not None  # the user set it
        if "duration_min" in changes:
            values["duration_min"] = _duration(changes["duration_min"])
        if "priority" in changes:
            if changes["priority"] is None:
                raise _invalid("The priority must be 1 (high), 2 (normal) or 3 (low), not empty.")
            values["priority"] = _priority(changes["priority"])
            if values["priority"] != current.priority:  # the user raised or lowered it
                values["priority_reason"] = None
                # A change from outside text the user said yes to is not theirs, even on a task
                # whose priority they had set before.
                values["priority_source"] = "owner" if label else "pisa"
        if "status" in changes:
            if changes["status"] not in ("open", "done", "dismissed"):
                raise _invalid("The status must be open, done or dismissed.")
            values["status"] = changes["status"]
        if "repeat" in changes:
            repeat = check_repeat(changes["repeat"])
            values["repeat"] = Jsonb(repeat) if repeat else None
        if "due_at" in changes or "due_kind" in changes:
            kind = changes.get("due_kind") or (
                _implied_kind(changes["due_at"])
                if changes.get("due_at")
                else (
                    current.due_kind if current.due_kind != "none" or "due_at" not in changes else "datetime"
                )
            )
            raw = (
                changes["due_at"]
                if "due_at" in changes
                else (current.due_at.isoformat() if current.due_at else None)
            )
            if changes.get("due_kind") == "none" or raw is None:
                if kind != "none" and "due_kind" in changes and raw is None:
                    raise _invalid(f"A task with due_kind {kind!r} needs a due_at.")
                values["due_at"], values["due_kind"] = None, "none"
            else:
                values["due_at"], values["due_kind"] = parse_due(raw, kind, self._owner.tz, now), kind
            # who set this deadline: the user (the app, their words), else what the caller names (a page
            # Pisa read, an email, Pisa's own estimate)
            source = changes.get("deadline_source") or ("owner" if label else "pisa")
            values["deadline_source"] = source if values["due_at"] is not None else None
        if "start_at" in changes:  # when to DO it ("spostalo alle 15"): a block, never a deadline
            if changes["start_at"] in (None, ""):
                values["expected_from"] = values["expected_until"] = None
            else:
                starts = parse_due(str(changes["start_at"]), "datetime", self._owner.tz, now)
                minutes = values.get("duration_min", current.duration_min) or TIMED_DEFAULT_MIN
                values["expected_from"] = starts
                values["expected_until"] = starts + timedelta(minutes=minutes)
        if not values:
            return current
        values["updated_at"] = now
        status = values.get("status")
        async with self._pool.connection() as conn, conn.transaction():
            task = await self._write(conn, current.id, values)
            if task is None:  # a concurrent update set the same status first: it made the next one, if any
                del values["status"]
                task = await self._write(conn, current.id, values) if len(values) > 1 else None
                status = None
            task = task or await self._get(conn, current.id)
            if status is not None and task.repeat and current.status == "open" and status != "open":
                # Done or dismissed: the series goes on (dismissing ends only this one).
                await self._next_occurrence(conn, task, now)
            elif status == "open" and current.status != "open":
                await self._drop_untouched_next(conn, task.id)
            if values.get("priority_source") == "owner":  # a label for the learning
                await feedback(
                    conn, "priority", f"task:{task.id}", str(current.priority), str(task.priority), now
                )
        _log.info("task_updated", task_id=task.id, fields=sorted(k for k in values if k != "updated_at"))
        return task

    async def _write(self, conn: AsyncConnection, task_id: str, values: dict[str, Any]) -> Task | None:
        """The UPDATE; a status change applies only to a task not already in that status (None then)."""
        guard = " AND status <> %s" if "status" in values else ""
        query = sql.SQL("UPDATE tasks SET {} WHERE id = %s" + guard + _RETURNING).format(  # noqa: S608
            sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(column)) for column in values)
        )
        params = (*values.values(), uuid.UUID(task_id), *((values["status"],) if guard else ()))
        cur = conn.cursor(row_factory=class_row(Task))
        return await (await cur.execute(query, params)).fetchone()

    async def _get(self, conn: AsyncConnection, task_id: str) -> Task:
        cur = conn.cursor(row_factory=class_row(Task))
        task = await (await cur.execute(_SELECT + " WHERE id = %s", (uuid.UUID(task_id),))).fetchone()
        if task is None:
            raise PisaError("not_found", f"There is no task with id {task_id}.", "Refresh the task list")
        return task

    async def _next_occurrence(self, conn: AsyncConnection, task: Task, now: datetime) -> None:
        assert task.repeat is not None
        due, kind = next_due(task.due_at, task.due_kind, task.repeat, now, self._owner.tz)
        row = await (
            await conn.execute(
                "SELECT coalesce(series_id, id)::text FROM tasks WHERE id = %s", (uuid.UUID(task.id),)
            )
        ).fetchone()
        nxt = await insert_task(
            conn, title=task.title, now=now, source_kind=task.source_kind, notes=task.notes, due_at=due,
            due_kind=kind, duration_min=task.duration_min, place_text=task.place_text, priority=task.priority,
            source_label=task.source_label, repeat=task.repeat, series_id=row[0] if row else task.id,
            previous_id=task.id, place_by_owner=task.place_by_owner,
        )  # fmt: skip
        if nxt is not None:
            _log.info("task_repeated", task_id=task.id, next_id=nxt.id)

    async def _drop_untouched_next(self, conn: AsyncConnection, task_id: str) -> None:
        """Reopened (undo, restore): the next occurrence it made goes, unless the user already changed it."""
        cur = await conn.execute(
            "DELETE FROM tasks WHERE previous_id = %s AND status = 'open' AND updated_at = created_at",
            (uuid.UUID(task_id),),
        )
        if cur.rowcount:
            _log.info("task_next_dropped", task_id=task_id)

    async def aged_ids(self) -> set[str]:
        """The open tasks overdue for days that the plan and the briefing leave out (aging)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id::text, due_at, kept_at FROM tasks WHERE status = 'open' AND due_at IS NOT NULL"
                )
            ).fetchall()
        now = self.now()
        return {tid for tid, due, kept in rows if aging.aged(due, kept, now)}

    async def soft_delete(self, task_id: str) -> Task:
        """Deleted at once, by the user's words, with no "Sicuro?": gone from every list, and back
        with restore within 24 h (`DELETED_KEPT`); then the maintenance job removes it."""
        current = await self.get(task_id)
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE tasks SET status = 'deleted', deleted_at = %s, updated_at = %s WHERE id = %s",
                (self.now(), self.now(), uuid.UUID(current.id)),
            )
        _log.info("task_soft_deleted", task_id=current.id)
        return current

    async def deleted_recently(self) -> "builtins.list[Task]":
        """The tasks deleted by voice that can still be restored (newest first)."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(Task))
            await cur.execute(
                _SELECT + " WHERE status = 'deleted' AND deleted_at > %s ORDER BY deleted_at DESC",
                (self.now() - DELETED_KEPT,),
            )
            return await cur.fetchall()

    async def undelete(self, task_id: str) -> Task:
        """A task deleted by voice back as open, within 24 h."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE tasks SET status = 'open', deleted_at = NULL, updated_at = %s "
                    "WHERE id = %s AND status = 'deleted' AND deleted_at > %s RETURNING id",
                    (self.now(), uuid.UUID(task_id), self.now() - DELETED_KEPT),
                )
            ).fetchone()
        if row is None:
            raise PisaError("not_found", "That task can't be restored any more (24 hours).", "Add it again")
        _log.info("task_restored", task_id=task_id)
        return await self.get(task_id)

    async def delete(self, task_id: str) -> None:
        """Permanently: the task is gone, not dismissed."""
        current = await self.get(task_id)
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM tasks WHERE id = %s", (uuid.UUID(current.id),))
        _log.info("task_deleted", task_id=current.id)

    async def agenda(self, start: date | None = None, days: int = AGENDA_DEFAULT_DAYS) -> dict[str, Any]:
        if not 1 <= days <= AGENDA_MAX_DAYS:
            raise _invalid(f"An agenda covers 1-{AGENDA_MAX_DAYS} days.")
        now = self.now()
        today = now.astimezone(self._owner.zone).date()
        start = start or today
        window_start = local_day_bounds(start, self._owner.tz)[0]
        window_end = local_day_bounds(start + timedelta(days=days - 1), self._owner.tz)[1]
        async with self._pool.connection() as conn:
            lang = str((await self._owner.settings(conn))["language"])
            events = await (
                await conn.execute(
                    "SELECT e.id, e.title, e.starts_at, e.ends_at, e.all_day, e.location, c.name, "  # noqa: S608 (constant fragments)
                    "e.start_date, e.end_date, e.place_id::text, coalesce(e.attendees, 0) > 0, "
                    f"coalesce({foreign.FOREIGN_EVENT}, false) "
                    "FROM calendar_events e "
                    "JOIN calendars c ON c.id = e.calendar_id "
                    "WHERE e.ends_at > %s AND e.starts_at < %s ORDER BY c.name, c.id",
                    (window_start, window_end),
                )
            ).fetchall()
        open_tasks = await self.list("open", low=False)  # Today: low is quiet
        tz = self._owner.zone
        out: list[dict[str, Any]] = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            lo, hi = local_day_bounds(day, self._owner.tz)
            day_events = []
            seen: set[str] = set()
            for (
                eid,
                title,
                starts,
                ends,
                all_day,
                location,
                calendar,
                start_date,
                end_date,
                place_id,
                invited,
                strangers,
            ) in events:
                # One event shown in two calendars (an invitation in a shared calendar too) has the same
                # Google event id in both: list it once, so `id` identifies an agenda event.
                if eid in seen:
                    continue
                if all_day:
                    if not (start_date <= day < end_date):
                        continue
                elif not (ends > lo and starts < hi):
                    continue
                seen.add(eid)
                day_events.append(
                    {
                        "id": eid,
                        "title": title,
                        "start": starts.astimezone(tz).isoformat(),
                        "end": ends.astimezone(tz).isoformat(),
                        "all_day": all_day,
                        "location": location,
                        "calendar": calendar,
                        "place_id": place_id,
                        "invited": invited,  # others' event, a title they wrote
                        # a title a stranger controls (an invite, Gmail's, a shared calendar's)
                        "foreign": strangers,
                    }
                )
            day_events.sort(
                key=lambda e: (not e["all_day"], datetime.fromisoformat(e["start"]), e["title"])
            )  # by time, not by string: the repeated DST hour has two offsets
            due = [
                t
                for t in open_tasks
                if t.due_at is not None and (lo <= t.due_at < hi or (day == today and t.due_at < lo))
            ]
            out.append(
                {
                    "date": day.isoformat(),
                    "events": day_events,
                    "tasks_due": [task_json(t, self._owner.tz, lang) for t in due],
                }
            )
        return {"timezone": self._owner.tz, "days": out}


def _text(value: str | None, limit: int, what: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > limit:
        raise _invalid(f"The {what} can be at most {limit} characters long.")
    return value or None


def _implied_kind(due_at: str) -> DueKind:
    """A bare YYYY-MM-DD means "by the end of that day"; anything with a time is a date-time."""
    return "date" if len(due_at.strip()) == 10 else "datetime"


def _duration(value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or not DURATION_MIN <= value <= DURATION_MAX):
        raise _invalid(f"duration_min must be a whole number from {DURATION_MIN} to {DURATION_MAX} minutes.")
    return value


def _priority(value: int | None) -> int:
    if value is None:
        return 2
    if value not in (1, 2, 3):
        raise _invalid("The priority must be 1 (high), 2 (normal) or 3 (low).")
    return value


async def resay_reasons(conn: AsyncConnection, *, apply: bool, language: str) -> dict[str, int]:
    """Once: the open tasks whose stored reason fails the check on read (`task_reason`, in
    their Settings language), counted by kind. `apply`: each such reason is rewritten as the line they already
    see (not `updated_at`: an untouched next occurrence stays untouched). Counts only."""
    lang = str((await load_settings(conn, language))["language"])
    cur = conn.cursor(row_factory=class_row(Task))
    await cur.execute(_SELECT + " WHERE status = 'open' AND priority_reason IS NOT NULL")
    counts: dict[str, int] = {}
    for task in await cur.fetchall():
        said = task_reason(task, lang)
        if said == task.priority_reason:
            continue
        kind = (
            "retriage"
            if task.priority_reason and retriage_kind(task.priority_reason)
            else "account_breach"
            if task.email_notice == "account_breach"
            else task.email_category or task.source_kind
        )
        counts[kind] = counts.get(kind, 0) + 1
        if apply:
            await conn.execute(
                "UPDATE tasks SET priority_reason = %s WHERE id = %s AND priority_reason = %s "
                "AND status = 'open'",
                (said, uuid.UUID(task.id), task.priority_reason),
            )
    _log.info("task_reasons_resaid", apply=apply, total=sum(counts.values()), **counts)
    return counts
