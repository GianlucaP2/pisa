"""App Store review mode (PISA_REVIEW_MODE=1, docs-site/app-review.md): the demo's synthetic data.

At the runtime's start, a database with no task at all gets a handful of English tasks, every one invented
(no real person or place): two due today with their durations (what today's plan is made from: the planner
reads the open tasks due today, their duration and priority), two due tomorrow, one without a day, and one
done. Seeded once: in one transaction under a lock, and marked in `app_settings` (`review_seed`), so a second
start, the worker's and the API's together, or a reviewer who deleted every task, seeds nothing again.

Review mode runs on a fresh database only: a database that holds someone's data (`USER_TABLES`, or a device
not paired for the review) and no seed marker is refused at start (`not_fresh`), before the API or the worker
starts (`preflight`) and again in `Runtime.start`. A start without the flag on a database where review
devices are left warns (`review_devices_left`), naming how many and `pisa pair --review --revoke`; it revokes
nothing itself.
"""

from datetime import timedelta
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.tasks import DueKind, end_of_day
from pisa.logging import get_logger
from pisa.services.tasks import insert_task

_log = get_logger("pisa.review")

SEED_KEY = "review_seed"
_LOCK = "pisa.review_seed"

# (title, day offset from today or None, duration in minutes, priority, done)
TASKS: tuple[tuple[str, int | None, int | None, int, bool], ...] = (
    ("Water the plants", 0, 10, 2, False),
    ("Call the dentist (demo)", 0, 15, 1, False),
    ("Buy a birthday card (demo)", 1, 20, 2, False),
    ("Take the recycling out (demo)", 1, 5, 2, False),
    ("Plan the weekend (demo)", None, 30, 3, False),
    ("Read the welcome notes (demo)", 0, 10, 2, True),
)


# what a database someone used holds (a fresh one, migrated and set up with `pisa setup`, holds none of it)
USER_TABLES = (
    "tasks", "conversations", "messages", "episodes", "people", "places", "notifications", "email_messages",
    "calendar_events", "connector_accounts", "phone_keys",
)  # fmt: skip


def not_fresh() -> PisaError:
    return PisaError(
        "validation_failed",
        "PISA_REVIEW_MODE=1 is set, but this database already holds data and was never a review server's: "
        "App Store review mode runs on a fresh database only, so the reviewer sees only invented data.",
        "Point PISA_DATABASE_URL at a new, empty database (`pisa migrate`, then start again), or unset "
        "PISA_REVIEW_MODE to serve this one",
    )


async def _marked(conn: Any) -> bool:
    row = await (await conn.execute("SELECT 1 FROM app_settings WHERE key = %s", (SEED_KEY,))).fetchone()
    return row is not None


async def holds_data(conn: Any) -> bool:
    """Whether the database holds someone's data: a row in `USER_TABLES`, or a device not paired for the
    review."""
    for table in USER_TABLES:
        if await (await conn.execute(f"SELECT 1 FROM {table} LIMIT 1")).fetchone() is not None:  # noqa: S608 (a constant)
            return True
    row = await (await conn.execute("SELECT 1 FROM devices WHERE NOT review LIMIT 1")).fetchone()
    return row is not None


async def review_devices(conn: Any) -> int:
    """The review devices not revoked yet."""
    row = await (
        await conn.execute("SELECT count(*) FROM devices WHERE review AND revoked_at IS NULL")
    ).fetchone()
    return int(row[0]) if row else 0


async def check(conn: Any, *, review_mode: bool) -> None:
    """At a start: in review mode, a database with data and no seed marker is refused (`not_fresh`); without
    the flag, a warning while review devices are left (never revoked here)."""
    try:
        if review_mode:
            if not await _marked(conn) and await holds_data(conn):
                _log.error("review_mode_refused", why="the database holds data and no review seed marker")
                raise not_fresh()
            return
        left = await review_devices(conn)
    except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn):  # not migrated yet
        if review_mode:
            raise PisaError(
                "validation_failed",
                "PISA_REVIEW_MODE=1 is set, but this database's schema is not up to date.",
                "Run `pisa migrate`, then start again",
            ) from None
        _log.info("review_check_skipped", why="the schema is not up to date")
        return
    if left:
        _log.warning(
            "review_devices_left", count=left,
            action="review mode is off: revoke them with `pisa pair --review --revoke`",
        )  # fmt: skip


async def preflight(database_url: str, *, review_mode: bool) -> None:
    """`check` before the API or the worker starts, so a refusal is a clean exit, not a traceback. A database
    that can't be reached yet is left to the start itself (which checks again, and the worker retries).
    Without the flag nothing here: `Runtime.start` warns about review devices left."""
    if not review_mode:
        return
    try:
        conn = await psycopg.AsyncConnection.connect(database_url, autocommit=True, connect_timeout=10)
    except psycopg.OperationalError:
        _log.warning("review_preflight_skipped", why="the database can't be reached yet")
        return
    async with conn:
        await check(conn, review_mode=review_mode)


async def seed(pool: AsyncConnectionPool, owner: Owner) -> int:
    """The demo's tasks, once, into a fresh database, which is marked as seeded. Returns how many were made
    (0: already seeded). A database with data and no marker is refused (`not_fresh`)."""
    now = owner.now()
    today = now.date()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LOCK,))
        if await _marked(conn):
            return 0
        if await holds_data(conn):
            _log.error("review_mode_refused", why="the database holds data and no review seed marker")
            raise not_fresh()
        made = 0
        for title, offset, minutes, priority, done in TASKS:
            due = None if offset is None else end_of_day(today + timedelta(days=offset), owner.tz)
            kind: DueKind = "none" if due is None else "date"
            task = await insert_task(
                conn, title=title, now=now, source_kind="manual", due_at=due, due_kind=kind,
                duration_min=minutes, priority=priority,
            )  # fmt: skip
            if task is not None and done:
                await conn.execute(
                    "UPDATE tasks SET status = 'done', done_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, task.id),
                )
            made += task is not None
        await conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, %s)",
            (SEED_KEY, Jsonb({"at": now.isoformat(), "tasks": made})),
        )
    _log.info("review_seeded", tasks=made)
    return made
