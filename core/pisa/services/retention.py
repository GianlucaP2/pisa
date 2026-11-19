"""The user's `messages_retention` applied to conversation `messages` (the 03:30 prune, inside
`maintenance.prune_history`) and its preview (GET /v1/settings/messages_retention/preview).

What Pisa learned from their words is untouched: profile claims carry their quoted words in their evidence,
not a message id; a row that cites a message by id (a topic they named, an unknown it opened, a problem or an
idea fragment of theirs, a bet, a swarm request) keeps its own text when the message goes (`ON DELETE SET
NULL`, 0090). A message is kept only while a **live** row cites it (`LIVE`): a topic queued, active or
monthly, an unknown still open, a problem with a live idea. `CITING` names every foreign key to `messages`; a
test checks it against the database. Deletes go in batches (`rules.BATCH`). Logs carry counts only, and the
change's device id.
"""

import uuid
from datetime import datetime
from typing import Any, LiteralString

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.db.settings import get_setting, set_setting
from pisa.domain import retention as rules
from pisa.logging import get_logger

# app_settings: their last change, {value, previous, at, device_id}
CHANGED_KEY = "messages_retention_changed_at"
# (table, column) -> what the prune does to it: "set_null" (the row keeps its text and loses the pointer; a
# live one keeps the message). Every foreign key to `messages` is SET NULL since 0090.
CITING: dict[tuple[str, str], str] = {
    ("pisa_topics", "message_id"): "set_null",
    ("pisa_unknowns", "message_id"): "set_null",
    ("pisa_idea_problems", "message_id"): "set_null",
    ("pisa_idea_fragments", "message_id"): "set_null",
    ("pisa_explicit", "message_id"): "set_null",
    ("pisa_bets", "message_id"): "set_null",
    ("pisa_swarm_asks", "message_id"): "set_null",
}
# the citing rows that keep their message while they live, written out (constant SQL)
LIVE: dict[str, LiteralString] = {
    "pisa_topics": (
        "SELECT 1 FROM pisa_topics c WHERE c.message_id = m.id AND c.state IN ('queued', 'active', 'monthly')"
    ),
    "pisa_unknowns": "SELECT 1 FROM pisa_unknowns c WHERE c.message_id = m.id AND c.state = 'open'",
    "pisa_idea_problems": (
        "SELECT 1 FROM pisa_idea_problems c JOIN pisa_ideas i ON i.problem_id = c.id "
        "WHERE c.message_id = m.id AND i.state <> 'killed'"
    ),
}
_OLD: LiteralString = "m.created_at < %s"
_KEPT: LiteralString = " OR ".join(f"EXISTS ({q})" for q in LIVE.values())
GOING: LiteralString = _OLD + " AND NOT (" + _KEPT + ")"
_log = get_logger("pisa.retention")


def _at(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("at")
    return datetime.fromisoformat(value) if isinstance(value, str) else None


async def changed_at(conn: AsyncConnection) -> datetime | None:
    return _at(await get_setting(conn, CHANGED_KEY))


async def db_now(conn: AsyncConnection) -> datetime:
    """The database's clock (the change's moment is the database's, never the app's)."""
    row = await (await conn.execute("SELECT now()")).fetchone()
    assert row is not None
    return row[0]


async def mark_changed(
    conn: AsyncConnection, value: str, previous: str, device_id: uuid.UUID | None
) -> datetime:
    """Their change, kept (`{value, previous, at, device_id}`) and logged: the grace counts from `at`."""
    at = await db_now(conn)
    device = str(device_id) if device_id else None
    record = {"value": value, "previous": previous, "at": at.isoformat(), "device_id": device}
    await set_setting(conn, CHANGED_KEY, Jsonb(record))
    _log.info("messages_retention_changed", device_id=device, **{"from": previous, "to": value})
    return at


async def _batched(conn: AsyncConnection, sql: LiteralString, params: tuple[Any, ...]) -> tuple[int, int]:
    """Run a `... LIMIT %s` delete until it takes less than a batch. Returns (rows, statements)."""
    rows = batches = 0
    while True:
        cur = await conn.execute(sql, (*params, rules.BATCH))
        rows += cur.rowcount
        batches += 1
        if cur.rowcount < rules.BATCH:
            return rows, batches


async def delete_batched(conn: AsyncConnection, sql: LiteralString, params: tuple[Any, ...]) -> int:
    return (await _batched(conn, sql, params))[0]


async def prune_messages(conn: AsyncConnection, now: datetime, value: str, tz: str) -> dict[str, int]:
    """Delete the messages older than their setting, never one a live row cites, never before the first 03:30
    a day after a change. Returns how many went and how many the live citations kept."""
    cutoff = rules.messages_cutoff(value, await changed_at(conn), now, tz)
    if cutoff is None:
        return {"messages": 0, "messages_kept_cited": 0, "talk_timelines": 0, "research_findings": 0,
                "turns": 0, "drawings": 0}  # fmt: skip
    kept_sql: LiteralString = "SELECT count(*) FROM messages m WHERE " + _OLD + " AND (" + _KEPT + ")"  # noqa: S608
    row = await (await conn.execute(kept_sql, (cutoff,))).fetchone()
    kept = int(row[0]) if row else 0
    gone, batches = await _batched(
        conn,
        "DELETE FROM messages WHERE id IN (SELECT m.id FROM messages m WHERE " + GOING + " LIMIT %s)",  # noqa: S608
        (cutoff,),
    )
    # what their talks' research found, by the same setting and cutoff
    findings, _ = await _batched(
        conn,
        "DELETE FROM research_findings WHERE id IN (SELECT f.id FROM research_findings f "
        "WHERE f.created_at < %s LIMIT %s)",
        (cutoff,),
    )
    # the drawings Pisa showed in their talks, by the same setting and cutoff
    drawings, _ = await _batched(
        conn,
        "DELETE FROM drawings WHERE id IN (SELECT d.id FROM drawings d WHERE d.created_at < %s LIMIT %s)",
        (cutoff,),
    )
    # REST turns (Pisa's words; their sheets, a research answer, cascade)
    turns, _ = await _batched(
        conn,
        "DELETE FROM turns WHERE id IN (SELECT t.id FROM turns t WHERE t.created_at < %s LIMIT %s)",
        (cutoff,),
    )
    # their words in a talk's timeline: the same setting, the same cutoff; the session stays
    timelines, _ = await _batched(
        conn,
        "UPDATE talk_sessions SET timeline = NULL WHERE id IN (SELECT t.id FROM talk_sessions t "
        "WHERE t.started_at < %s AND t.timeline IS NOT NULL LIMIT %s)",
        (cutoff,),
    )
    _log.info(
        "messages_pruned", retention=value, count=gone, kept_cited=kept, batches=batches, timelines=timelines,
        findings=findings, turns=turns, drawings=drawings,
    )  # fmt: skip
    return {"messages": gone, "messages_kept_cited": kept, "talk_timelines": timelines,
            "research_findings": findings, "turns": turns, "drawings": drawings}  # fmt: skip


async def device_messages(conn: AsyncConnection, device_id: uuid.UUID, after: int = 0) -> list[int]:
    """The ids of a device's messages (newer than `after`)."""
    rows = await (
        await conn.execute(
            "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.device_id = %s AND m.id > %s",
            (device_id, after),
        )
    ).fetchall()
    return [int(r[0]) for r in rows]


async def forget_cited(conn: AsyncConnection, message_ids: list[int]) -> None:
    """What came from these messages goes with them (SET NULL alone would keep each citing row
    with the pointer nulled): a device's own deletion, and a turn they went on with (
    `ConversationService.drop_after`: a bet or a swarm request linked during that turn must not outlive
    it)."""
    if not message_ids:
        return
    for sql in (
        "DELETE FROM pisa_unknowns WHERE message_id = ANY(%s)",
        "DELETE FROM pisa_topics WHERE message_id = ANY(%s)",
        "DELETE FROM pisa_idea_fragments WHERE message_id = ANY(%s)",
        "DELETE FROM pisa_idea_problems WHERE message_id = ANY(%s)",
        "DELETE FROM pisa_bets WHERE message_id = ANY(%s)",
        "DELETE FROM pisa_swarm_asks WHERE message_id = ANY(%s)",
    ):
        await conn.execute(sql, (message_ids,))


async def preview(conn: AsyncConnection, value: str, current: str, tz: str) -> dict[str, Any]:
    """How many messages `value` would remove today, and the prune that first may (the first 03:30 a day
    after the change)."""
    now = await db_now(conn)
    keep = rules.MESSAGES_RETENTION[value]
    count = 0
    if keep is not None:
        sql: LiteralString = "SELECT count(*) FROM messages m WHERE " + GOING  # noqa: S608
        row = await (await conn.execute(sql, (now - keep,))).fetchone()
        count = int(row[0]) if row else 0
    after = rules.messages_applies_after(value, current, await changed_at(conn), now, tz)
    return {"value": value, "messages": count, "applies_after": after.isoformat()}
