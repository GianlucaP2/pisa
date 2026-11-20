"""`pisa_decisions`: Pisa's moves, stored at the end of each turn, read back as "perché".

The purpose is the model's line and stays in the database (like an approval's summary); logs carry counts
only. For a move that made an approval, the approval's status is its outcome.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from pisa.domain import wrist
from pisa.domain.decisions import Decision

RECENT = 8  # what `my_recent_decisions` reads back


async def store(
    conn: AsyncConnection,
    moves: Sequence[Decision],
    *,
    at: datetime,
    trace_id: str | None,
    device_id: uuid.UUID | None,
    outside_kinds: Sequence[str] | None = None,
) -> None:
    """`outside_kinds`: the outside text the move came from (kinds only), the night's taint:
    `[]` a turn code knows was clean, a list what it read; None (NULL) unknown, never learned from (a
    caller that doesn't say is never "known clean")."""
    kinds = sorted(set(outside_kinds)) if outside_kinds is not None else None
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO pisa_decisions (at, trace_id, device_id, move, tool, ref, purpose, provenance, "
            "rail, outcome, outcome_at, present_id, outside_kinds, origin) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (at, trace_id, device_id, m.move, m.tool, m.ref, m.purpose, m.provenance, m.rail, m.outcome,
                 at if m.outcome else None, m.key, kinds, wrist.current())  # phone or watch
                for m in moves
            ],
        )  # fmt: skip


async def why(conn: AsyncConnection, refs: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The purpose and provenance of the moves that made these approvals (Changes: "perché")."""
    if not refs:
        return {}
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT DISTINCT ON (ref) ref, purpose, provenance FROM pisa_decisions WHERE ref = ANY(%s) "
        "ORDER BY ref, n",
        (list(refs),),
    )
    return {r["ref"]: {"purpose": r["purpose"], "provenance": r["provenance"]} for r in await cur.fetchall()}


async def recent(conn: AsyncConnection, limit: int = RECENT) -> list[dict[str, Any]]:
    """Pisa's latest moves, newest first, each with what became of it."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT d.at, d.move, d.tool, d.purpose, d.provenance, d.rail, "
        "COALESCE(CASE WHEN a.undone_at IS NOT NULL THEN 'undone' ELSE a.status END, d.outcome) AS outcome "
        "FROM pisa_decisions d LEFT JOIN approvals a ON a.id::text = d.ref "
        "ORDER BY d.n DESC LIMIT %s",
        (limit,),
    )
    return list(await cur.fetchall())


async def outcome(conn: AsyncConnection, present_id: str, what: str, at: datetime) -> bool:
    """A show's outcome from the device (an `object_event`, a late `presented`), by its present_id; a choice
    or an act is never overwritten. False when the show's row isn't stored yet (the turn is still running)."""
    cur = await conn.execute(
        "UPDATE pisa_decisions SET outcome = CASE WHEN outcome IN ('chosen', 'acted') THEN outcome ELSE %s "
        "END, outcome_at = %s WHERE present_id = %s",
        (what, at, present_id),
    )
    return cur.rowcount > 0
