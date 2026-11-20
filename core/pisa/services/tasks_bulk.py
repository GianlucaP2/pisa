"""Tasks in bulk: Pisa's `tasks_bulk` and Today's edit mode (`POST /v1/tasks/bulk`) change a set of
open tasks at once, with one Undo for the whole set (24 h; `task_bulk`). Each task changes through
`TasksService` (a repeating task's next one, its labels), so a bulk change is the same as the user's one
by one.

Logs carry counts and ids only, never titles.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import tasks_bulk as rules
from pisa.domain.errors import PisaError
from pisa.domain.tasks import Task
from pisa.logging import get_logger
from pisa.services.tasks import TasksService

_log = get_logger("pisa.tasks_bulk")


class TasksBulkService:
    def __init__(self, pool: AsyncConnectionPool, tasks: TasksService) -> None:
        self._pool = pool
        self._tasks = tasks

    async def match(
        self,
        which: rules.Filter,
        *,
        days: int | None = None,
        source: str | None = None,
        ids: Sequence[str] = (),
        sender: str | None = None,
    ) -> list[Task]:
        """The open tasks the filter names, in the lists' own order."""
        senders = await self._senders() if which == "from_sender" else None
        return rules.select(
            await self._tasks.list("open"), which, now=self._tasks.now(), timezone=self._tasks.tz, days=days,
            source=source, ids=ids, senders=senders, sender=sender,
        )  # fmt: skip

    async def _senders(self) -> dict[str, str]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT t.id::text, concat_ws(' ', m.from_name, m.from_address) FROM tasks t "
                    "JOIN email_messages m ON m.id = t.source_ref "
                    "WHERE t.status = 'open' AND t.source_kind = 'email'"
                )
            ).fetchall()
        return {task_id: who for task_id, who in rows}

    async def apply(self, ids: Sequence[str], action: rules.Action, *, origin: str) -> dict[str, Any]:
        """Change the open tasks among `ids`; one Undo for them all. Returns {bulk_id, count, changed (ids),
        undo_until}."""
        if action not in rules.ACTIONS:
            raise PisaError(
                "validation_failed", f"The action {action!r} is not done, dismiss or delete.", "Pick one"
            )
        if len(ids) > rules.IDS_MAX:
            raise PisaError("validation_failed", f"At most {rules.IDS_MAX} tasks at once.", "Send fewer ids")
        now = self._tasks.now()
        targets = rules.select(
            await self._tasks.list("open"), "ids", now=now, timezone=self._tasks.tz, ids=ids
        )
        changed: list[dict[str, str]] = []
        for task in targets:
            try:
                if action == "delete":
                    await self._tasks.soft_delete(task.id)
                else:
                    await self._tasks.update(task.id, {"status": rules.STATUS[action]})
            except PisaError:  # gone meanwhile: the rest still change
                continue
            changed.append({"id": task.id, "was": task.status})
        bulk_id = uuid.uuid4()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO task_bulk (id, action, items, origin, created_at) VALUES (%s, %s, %s, %s, %s)",
                (bulk_id, action, Jsonb(changed), origin, now),
            )
        _log.info("tasks_bulk", bulk_id=str(bulk_id), action=action, count=len(changed), origin=origin)
        # which changed, so a partial change says what did not
        return {"bulk_id": str(bulk_id), "count": len(changed), "changed": [c["id"] for c in changed],
                "undo_until": now + rules.UNDO_KEPT}  # fmt: skip

    async def undo(self, bulk_id: str | None = None) -> dict[str, Any]:
        """Restore a bulk change (the latest one when no id), within 24 h, once. Returns {bulk_id, count}."""
        now = self._tasks.now()
        async with self._pool.connection() as conn, conn.transaction():
            if bulk_id is None:
                query, args = (
                    "SELECT id, action, items FROM task_bulk WHERE undone_at IS NULL AND created_at > %s "
                    "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                    (now - rules.UNDO_KEPT,),
                )
            else:
                try:
                    key = uuid.UUID(bulk_id)
                except ValueError:
                    raise PisaError("not_found", "No bulk change has that id.", "Check the id") from None
                query, args = (
                    "SELECT id, action, items FROM task_bulk WHERE id = %s AND undone_at IS NULL "
                    "AND created_at > %s FOR UPDATE",
                    (key, now - rules.UNDO_KEPT),
                )
            row = await (await conn.execute(query, args)).fetchone()
            if row is None:
                raise PisaError(
                    "not_found", "No bulk change to undo (24 hours, once).", "Change the tasks one by one"
                )
            key, action, items = row
            await conn.execute("UPDATE task_bulk SET undone_at = %s WHERE id = %s", (now, key))
        restored = 0
        for item in items:
            try:
                if action == "delete":
                    await self._tasks.undelete(item["id"])
                else:
                    await self._tasks.update(item["id"], {"status": item["was"]})
                restored += 1
            except PisaError:  # removed for good meanwhile
                continue
        _log.info("tasks_bulk_undone", bulk_id=str(key), count=restored)
        return {"bulk_id": str(key), "count": restored}
