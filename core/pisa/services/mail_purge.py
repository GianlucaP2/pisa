"""What Pisa read from Gmail is kept short, and goes when Google is disconnected.

Nightly (`purge`, run by the worker after the 03:30 prune):
- the mail's text is set to NULL 30 days after it was fetched: a message's `body`, `new_text` and `quoted`,
  an event's, an evidence's and an assertion's `quote`. The rows stay, with their ids, times and keys;
- a thing's keys (`entity_key`) stay while a task linked to it is open and go 30 days after the last such
  task closed (a thing with no task: 30 days after it was made);
- tasks are never touched.

On a Google disconnect (`purge_derived`, inside the disconnect's transaction): everything derived from Gmail
goes, whatever its age; tasks stay, and lose only the link to the thing that went.
"""

from datetime import datetime
from typing import LiteralString

from psycopg import AsyncConnection

from pisa.domain import retention as rules
from pisa.logging import get_logger

_log = get_logger("pisa.mail_purge")

# A task is open while it is not closed and its state (when the machine set one) is not an end.
_OPEN: LiteralString = "t.status = 'open' AND coalesce(t.state, 'open') IN ('open', 'waiting', 'snoozed')"


async def _blank(conn: AsyncConnection, sql: LiteralString, params: tuple[object, ...]) -> int:
    """An UPDATE that nulls text, in batches: `sql` selects `ctid`s with a %s limit last."""
    total = 0
    while True:
        cur = await conn.execute(sql, (*params, rules.BATCH))
        total += cur.rowcount
        if cur.rowcount < rules.BATCH:
            return total


async def purge_text(conn: AsyncConnection, moment: datetime) -> dict[str, int]:
    """Null the mail text older than `MAIL_TEXT_KEPT`. Returns the rows changed per table."""
    cut = moment - rules.MAIL_TEXT_KEPT
    counts = {
        "email_messages": await _blank(
            conn,
            # the snippet is the body's first words: mail text too (never NULL: blank)
            # … and the old intake's gist, its retelling (E1)
            "UPDATE email_messages SET body = NULL, new_text = NULL, quoted = NULL, snippet = '', "
            "intake_gist = NULL WHERE ctid IN (SELECT ctid FROM email_messages WHERE fetched_at < %s "
            "AND (body IS NOT NULL OR new_text IS NOT NULL OR quoted IS NOT NULL OR snippet <> '' "
            "OR intake_gist IS NOT NULL) LIMIT %s)",
            (cut,),
        )
    }
    quoted: tuple[tuple[LiteralString, LiteralString], ...] = (
        ("mail_event", "created_at"),
        ("task_evidence", "created_at"),
        ("assertion", "recorded_at"),
    )
    for table, column in quoted:
        sql: LiteralString = (
            f"UPDATE {table} SET quote = NULL WHERE ctid IN ("  # noqa: S608
            f"SELECT ctid FROM {table} WHERE {column} < %s AND quote IS NOT NULL LIMIT %s)"
        )
        counts[table] = await _blank(conn, sql, (cut,))
    return counts


async def purge_keys(conn: AsyncConnection, moment: datetime) -> int:
    """Delete the keys of the things nothing open needs: no open task is linked, and the last linked task
    closed (or, with none, the thing was made) more than `MAIL_KEYS_KEPT_AFTER_CLOSE` ago."""
    cut = moment - rules.MAIL_KEYS_KEPT_AFTER_CLOSE
    cur = await conn.execute(
        "DELETE FROM entity_key WHERE entity_id IN (SELECT e.id FROM entity e "  # noqa: S608
        f"WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.entity_id = e.id AND {_OPEN}) "
        "AND coalesce((SELECT max(coalesce(t.done_at, t.deleted_at, t.updated_at)) FROM tasks t "
        "WHERE t.entity_id = e.id), e.created_at) < %s)",
        (cut,),
    )
    return cur.rowcount


async def purge(conn: AsyncConnection, moment: datetime) -> dict[str, int]:
    """The nightly purge: the text, then the keys."""
    counts = await purge_text(conn, moment)
    counts["entity_key"] = await purge_keys(conn, moment)
    _log.info("mail_purged", **counts)
    return counts


# what Gmail gave, as one list: (table, WHERE) in an order that leaves no row pointing at a deleted one
_DERIVED: tuple[tuple[LiteralString, LiteralString], ...] = (
    ("task_evidence", "true"),  # a task's reasons are read from mail; the task stays
    ("storyline", "source = 'mail'"),  # its events go with it
    ("mail_event", "true"),
    ("assertion", "source = 'mail'"),
    (
        "assertion",
        "source = 'chat' AND subject_kind = 'storyline_event'",
    ),  # his words on a storyline's events
    ("life_edge", "source = 'mail'"),
    ("entity", "source = 'mail'"),  # its keys go with it; a task's link to it is cleared
    ("orgs", "source = 'mail'"),
    ("email_threads", "true"),
    ("owner_identity", "true"),  # the aliases were learned from his sent mail
)


async def purge_derived(conn: AsyncConnection) -> dict[str, int]:
    """Delete everything read from Gmail, whatever its age (the messages themselves go with the caller's own
    delete). Tasks stay."""
    counts: dict[str, int] = {}
    for table, where in _DERIVED:
        sql: LiteralString = f"DELETE FROM {table} WHERE {where}"  # noqa: S608
        counts[table] = (await conn.execute(sql)).rowcount
    return counts
