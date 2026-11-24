"""The v0.1 note of their standing instructions (`pisa.domain.note`), now an archive.

Since v0.1.2 their standing instructions are Memoria's, one a row (`pisa.services.memoria`): migration 0020
copied the note's newest lines there, no turn reads the note and no tool writes it (`rewrite_my_note` is
retired: it refused Pisa's paraphrase as "not their words", and its held rewrite asked their yes to a diff of
lines rather than to one instruction). The note and
its versions stay readable ("Su di te"), and their own `PUT` and restore still keep versions, read by nothing
but this archive.

Logs carry the version, its length and where it came from, never its words.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from pisa.domain import note as rules
from pisa.domain.errors import PisaError
from pisa.infra import locks
from pisa.logging import get_logger

_log = get_logger("pisa.note")
_CURRENT = (
    "SELECT version, text, change, source, restored_from, created_at FROM owner_note_versions "
    "ORDER BY version DESC LIMIT %s"
)


class NoteService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._pool = pool
        self._clock = clock

    # --- reads --------------------------------------------------------------------------------------------

    async def current(self) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            return await self._current(conn)

    async def _current(self, conn: AsyncConnection) -> dict[str, Any] | None:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(_CURRENT, (1,))
        row = await cur.fetchone()
        return _shown(row) if row is not None else None

    async def text(self) -> str:
        """The note's text now ("" before their first telling, or after they emptied it)."""
        now = await self.current()
        return now["text"] if now is not None else ""

    async def view(self) -> dict[str, Any]:
        """Their view ("Su di te"): the note now, the cap, and its versions newest first."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(_CURRENT, (rules.VERSIONS_SHOWN,))
            rows = [_shown(r) for r in await cur.fetchall()]
            total = await (await conn.execute("SELECT count(*) FROM owner_note_versions")).fetchone()
        now = rows[0] if rows else None
        return {
            "text": now["text"] if now else "",
            "version": now["version"] if now else 0,
            "updated_at": now["at"] if now else None,
            "cap": rules.CAP,
            "wishes": rules.autonomy_wishes(now["text"]) if now else [],
            "versions": rows,
            "versions_total": int(total[0]) if total else 0,
        }

    # --- writes: their own, from the app (the archive) --------------------------------------------------

    async def put(self, text: str, device: uuid.UUID | None) -> dict[str, Any]:
        """Their PUT from the app: the note replaced, a version like any other."""
        new = rules.clean(text)
        if (why := rules.over_cap(new)) is not None:
            raise PisaError("validation_failed", f"Note not saved: {why}.", "Shorten the note and save again")
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.NOTE,))
            now = await self._current(conn)
            if now is None or now["text"] != new:
                await self._store(conn, new, "", "app", None, device)
        return await self.view()

    async def restore(self, version: int, device: uuid.UUID | None) -> dict[str, Any]:
        """An earlier version, written again as the newest (nothing is lost: the versions in between stay)."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.NOTE,))
            row = await (
                await conn.execute("SELECT text FROM owner_note_versions WHERE version = %s", (version,))
            ).fetchone()
            if row is None:
                raise PisaError(
                    "not_found", f"There is no version {version} of the note.", "Reload the note and pick one"
                )
            await self._store(conn, row[0], "", "restore", None, device, restored_from=version)
        return await self.view()

    async def _store(
        self,
        conn: AsyncConnection,
        text: str,
        change: str,
        source: str,
        turn_id: str | None,
        device: uuid.UUID | None,
        *,
        restored_from: int | None = None,
    ) -> int:
        row = await (
            await conn.execute("SELECT coalesce(max(version), 0) + 1 FROM owner_note_versions")
        ).fetchone()
        version = int(row[0]) if row else 1
        known = None
        if device is not None:  # a device since revoked keeps no link (SET NULL), never a failed write
            hit = await (await conn.execute("SELECT 1 FROM devices WHERE id = %s", (device,))).fetchone()
            known = device if hit else None
        await conn.execute(
            "INSERT INTO owner_note_versions (version, text, change, source, turn_id, device_id, "
            "restored_from, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (version, text, change, source, turn_id, known, restored_from, self._clock()),
        )
        _log.info("note_version", version=version, chars=len(text), source=source)
        return version


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": row["version"],
        "text": row["text"],
        "change": row["change"],
        "source": row["source"],
        "restored_from": row["restored_from"],
        "at": row["created_at"].isoformat(),
    }
