"""The stop, "Pisa, ferma tutto": the stop in force and its history (`pisa_stops`). Every initiative path asks
`stopped()` (the minute's steps in `workers.main.run_minute`, the autonomy gate, the Sunday card, the vault's
reminders, bills from email); the user's own requests never do. Each stop and resume is a ledger line."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import stop as rules
from pisa.domain import wrist
from pisa.domain.decisions import Decision
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import decisions as decision_log

By = Literal["app", "voice"]
_log = get_logger("pisa.stop")
_WHY = {
    "it-IT": {"stop": "Hai detto di fermare tutto.", "resume": "Hai detto di riprendere."},
    "en-US": {"stop": "You said to stop everything.", "resume": "You said to resume."},
}


async def _all_to_ask(conn: AsyncConnection, now: datetime, lang: str) -> None:
    """ "ferma tutto" puts every type back on ask, its history kept; a proposal
    waits 14 days (`autonomy.STOP_FREEZE`); they can still raise one themselves, with Face ID. Each change is
    a ledger line, without an Undo (raising a level again takes their Face ID)."""
    changed = await (
        await conn.execute(
            "UPDATE autonomy_levels a SET level = 'ask', since = %s, changed_by = 'default', "
            "proposed_level = NULL, proposed_at = NULL FROM autonomy_levels b WHERE a.type = b.type "
            "AND a.level <> 'ask' RETURNING a.type, b.level",
            (now,),
        )
    ).fetchall()
    if changed:
        why = _WHY["it-IT" if lang == "it-IT" else "en-US"]["stop"]
        await decision_log.store(
            conn,
            [Decision("act", "autonomy_level", "owner_words", ref=f"{t}:{was}:ask", purpose=why,
                      outcome="done") for t, was in changed],
            at=now, trace_id=None, device_id=None,
        )  # fmt: skip


async def stopped_now(conn: AsyncConnection) -> bool:
    row = await (await conn.execute("SELECT 1 FROM pisa_stops WHERE resumed_at IS NULL LIMIT 1")).fetchone()
    return row is not None


class StopService:
    def __init__(self, pool: AsyncConnectionPool, *, owner: Owner, clock: Callable[[], datetime]) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        # what else a stop stops (none today; each a callable the runtime adds)
        self.on_stop: list[Callable[[], Awaitable[object]]] = []

    async def stopped(self) -> bool:
        async with self._pool.connection() as conn:
            return await stopped_now(conn)

    async def state(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT stopped_at, stopped_by FROM pisa_stops WHERE resumed_at IS NULL LIMIT 1"
                )
            ).fetchone()
        if row is None:
            return {"stopped": False, "since": None, "by": None, "paused": []}
        return {
            "stopped": True,
            "since": row[0].astimezone(self._owner.zone).isoformat(),
            "by": row[1],
            "paused": list(rules.PAUSED),
        }

    async def stop(self, by: By, lang: str | None = None) -> dict[str, Any]:
        """Idempotent: a second stop keeps the first one's `since`."""
        lang = lang or self._owner.tag
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.stop'))")
            if not await stopped_now(conn):
                await conn.execute(
                    "INSERT INTO pisa_stops (scope, stopped_at, stopped_by, stopped_origin) "
                    "VALUES ('all', %s, %s, %s)",
                    (now, by, wrist.current()),  # the watch's Control, relayed
                )
                await self._line(conn, "stop_everything", "stop", lang, now)
                await _all_to_ask(conn, now, lang)
                fresh = True
            else:
                fresh = False
        if fresh:
            _log.info("pisa_stopped", by=by)
            for hook in self.on_stop:
                try:
                    await hook()
                except Exception:
                    _log.exception("stop_hook_failed")
        return await self.state()

    async def resume(self, by: By, lang: str | None = None) -> dict[str, Any]:
        lang = lang or self._owner.tag
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.stop'))")
            done = await conn.execute(
                "UPDATE pisa_stops SET resumed_at = %s, resumed_by = %s, resumed_origin = %s "
                "WHERE resumed_at IS NULL",
                (now, by, wrist.current()),
            )
            if done.rowcount:
                await self._line(conn, "resume_everything", "resume", lang, now)
        if done.rowcount:
            _log.info("pisa_resumed", by=by)
        return await self.state()

    async def _line(self, conn: AsyncConnection, tool: str, what: str, lang: str, now: datetime) -> None:
        why = _WHY["it-IT" if lang == "it-IT" else "en-US"][what]
        await decision_log.store(
            conn,
            [Decision("act", tool, "owner_words", purpose=why, outcome="done")],
            at=now,
            trace_id=None,
            device_id=None,
        )
