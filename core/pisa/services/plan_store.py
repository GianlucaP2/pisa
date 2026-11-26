"""The stored plan as their lists are now: the one check every reader of the stored plan goes through.

A plan is made from the open tasks of the moment (`open_tasks`: each task's id and last change) and kept with
them (`plans.open_tasks`). A plan read after one of them was added, closed, deleted or changed is not their
day any more: `PlannerService.current` makes it again at once; a reader that cannot make one
(`stored_current`) reads none. The minute's re-plan catches up within a minute, too late for a turn, Today or
a notice.
"""

from datetime import UTC, date
from typing import Any

from psycopg import AsyncConnection


async def open_tasks(conn: AsyncConnection) -> dict[str, str]:
    """The open tasks now, each with its last change: {task id: updated_at}. A plan is made from these."""
    rows = await (
        await conn.execute("SELECT id::text, updated_at FROM tasks WHERE status = 'open'")
    ).fetchall()
    return {task_id: at.astimezone(UTC).isoformat() for task_id, at in rows}


def made_from_now(built_from: dict[str, str] | None, now: dict[str, str]) -> bool:
    """The plan was made from the open tasks as they are now: the same tasks open, none changed since.
    `built_from` None: a plan stored without its open tasks (before they were kept), taken as current."""
    return built_from is None or built_from == now


async def latest_row(conn: AsyncConnection, day: date) -> tuple[dict[str, Any], dict[str, str] | None] | None:
    """The latest stored plan of `day` and the open tasks it was made from."""
    row = await (
        await conn.execute(
            "SELECT plan, open_tasks FROM plans WHERE day = %s ORDER BY generated_at DESC LIMIT 1", (day,)
        )
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


async def stored_current(conn: AsyncConnection, day: date) -> dict[str, Any] | None:
    """`PlannerService.current` of today for a reader that cannot make a plan (a voice line, a ready
    answer): the latest plan of `day` when it was made from the open tasks as they are now, else None (no
    plan to read: it is made again within the minute)."""
    row = await latest_row(conn, day)
    if row is None or not made_from_now(row[1], await open_tasks(conn)):
        return None
    return row[0]
