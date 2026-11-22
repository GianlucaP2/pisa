"""They delete, Pisa never does. Their deletion by family and age: `preview` counts what would go, `start`
records the signed deletion and runs it in the background (the phone never waits on it), once the API has
checked the phone's signature (`device` challenge, op `erase`, the target `<family>:<YYYY-MM-DD>`, like
shortening `messages_retention`). `job` reads its state and counts (no second Face ID for a retry).

one job at a time (a lock held for the whole job), keyset batches, a table finished only when
nothing still qualifies, counts that survive a resume. The preview counts what the delete
removes, the rows that cascade from them ("with their children") and an email-sourced gift idea too; the
cutoff is the date's first instant in the zone Pisa follows (`owner_context`), never later than the moment
they asked (tomorrow: all of it, today's too) and never a date after tomorrow; a guard row (a cap's, a
dedupe's) is never newer than the family's rule allows (`rules.at`).

Only the API's `/v1/data/erase` starts a deletion: no conversation tool, night job or outside text
imports it (a test checks the importers); the 15-minute maintenance only resumes one they signed
(`run_pending`). Logs carry the family, the date and counts, never content.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, date, datetime
from typing import Any, LiteralString

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import erase as rules
from pisa.domain import retention as retention_rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import retention

# app_meta: the off-site copy's last upload (written by the backup recipe, docs-site/self-hosting.md)
OFFSITE_KEY = "offsite_backup_at"
# an email-sourced gift idea goes with its email (the note): the notes the `mail` family takes with it
_MAIL_NOTES: LiteralString = (
    "FROM person_notes n WHERE n.source = 'email' AND NOT EXISTS "
    "(SELECT 1 FROM email_messages m WHERE m.id = split_part(n.source_ref, '#', 1) "
    "AND NOT coalesce(({mail}), false))"
)
# a waiting-for or a proposed promise Pisa read from a mail lives as long as its mail row
# (their own confirmed promise is theirs: it stays)
_MAIL_INTENTIONS: LiteralString = (
    "FROM intentions i WHERE i.source IN ('sent_mail', 'received_mail') AND (i.kind = 'waiting_for' OR "
    "i.state = 'proposed') AND NOT EXISTS (SELECT 1 FROM email_messages m WHERE m.id = i.source_ref "
    "AND NOT coalesce(({mail}), false))"
)
_log = get_logger("pisa.erase")


def _older(part: rules.Part) -> LiteralString:
    """The part's clause; a conversation message also keeps the safe rule (a live row cites it)."""
    return retention.GOING if part.table == "messages" else part.older


def _family(family: str) -> tuple[rules.Part, ...]:
    parts = rules.FAMILIES.get(family)  # type: ignore[call-overload]
    if parts is None:
        raise PisaError(
            "validation_failed",
            f"There is no family {family!r} to delete.",
            "Pick one of: " + ", ".join(rules.FAMILIES),
        )
    return parts


def _mail_part() -> rules.Part:
    return next(p for p in rules.FAMILIES["mail"] if p.table == "email_messages")


def check(before: date, tz: str, now: datetime) -> None:
    """A day after tomorrow in their zone is refused (400); tomorrow means all of it, up to now."""
    if rules.future(before, tz, now):
        raise PisaError(
            "validation_failed",
            f"{before.isoformat()} is after tomorrow: only what is older than a day that came can go "
            "(tomorrow: all of it, today's too).",
            "Pick tomorrow or an earlier day",
        )


# --- the catalog: primary keys and the children that cascade ------------------------------------------------


async def _key(conn: AsyncConnection, table: str) -> list[str]:
    """The table's primary key columns ([] when it has none: the row's ctid then)."""
    rows = await (
        await conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY (i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary "
            "ORDER BY array_position(i.indkey::int2[], a.attnum)",
            (table,),
        )
    ).fetchall()
    return [str(r[0]) for r in rows]


async def _cascades(conn: AsyncConnection, table: str) -> list[tuple[str, list[str], list[str]]]:
    """(child, its columns, the parent's columns) of each foreign key to `table` with ON DELETE CASCADE."""
    rows = await (
        await conn.execute(
            "SELECT c.conrelid::regclass::text, "
            "(SELECT array_agg(a.attname ORDER BY k.n) FROM unnest(c.conkey) WITH ORDINALITY k(num, n) "
            " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.num), "
            "(SELECT array_agg(a.attname ORDER BY k.n) FROM unnest(c.confkey) WITH ORDINALITY k(num, n) "
            " JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.num) "
            "FROM pg_constraint c WHERE c.contype = 'f' AND c.confdeltype = 'c' "
            "AND c.confrelid = %s::regclass AND c.conrelid <> c.confrelid ORDER BY 1",
            (table,),
        )
    ).fetchall()
    return [(str(r[0]), list(r[1]), list(r[2])) for r in rows]


def _cols(alias: str, columns: list[str]) -> sql.Composable:
    return sql.SQL("({})").format(sql.SQL(", ").join(sql.Identifier(alias, c) for c in columns))


async def _children(
    conn: AsyncConnection, table: str, rows: sql.Composable, params: tuple[Any, ...], depth: int = 0
) -> dict[str, int]:
    """How many rows cascade (at any depth) from `table`'s rows that `rows` selects (`SELECT m.* FROM table
    m WHERE ...`), per child table."""
    out: dict[str, int] = {}
    if depth > 6:
        return out
    for child, columns, parent_columns in await _cascades(conn, table):
        alias = f"c{depth}"
        picked = sql.SQL(
            "SELECT {alias}.* FROM {child} {alias} WHERE {mine} IN (SELECT {theirs} FROM ({rows}) p)"
        ).format(
            alias=sql.Identifier(alias),
            child=sql.Identifier(child),
            mine=_cols(alias, columns),
            theirs=sql.SQL(", ").join(sql.Identifier("p", c) for c in parent_columns),
            rows=rows,
        )
        row = await (
            await conn.execute(sql.SQL("SELECT count(*) FROM ({}) x").format(picked), params)
        ).fetchone()
        n = int(row[0]) if row else 0
        if n:
            out[child] = out.get(child, 0) + n
            for grandchild, m in (await _children(conn, child, picked, params, depth + 1)).items():
                out[grandchild] = out.get(grandchild, 0) + m
    return out


def _selected(part: rules.Part) -> sql.Composable:
    return sql.SQL("SELECT m.* FROM {} m WHERE {}").format(sql.Identifier(part.table), sql.SQL(_older(part)))


# --- preview and delete -------------------------------------------------------------------------------------


async def count(
    conn: AsyncConnection, family: str, before: date, tz: str, now: datetime | None = None
) -> tuple[dict[str, int], dict[str, int]]:
    """Per table, how many rows `delete` would remove now; and the rows that go with them (cascading
    children, an email-sourced gift idea), per table."""
    now = now or datetime.now(UTC)
    tables: dict[str, int] = {}
    children: dict[str, int] = {}
    for part in _family(family):
        params = (rules.at(part, before, tz, now),)
        row = await (
            await conn.execute(sql.SQL("SELECT count(*) FROM ({}) x").format(_selected(part)), params)
        ).fetchone()
        tables[part.table] = int(row[0]) if row else 0
        for child, n in (await _children(conn, part.table, _selected(part), params)).items():
            children[child] = children.get(child, 0) + n
    if family == "mail":
        mail = _mail_part()
        notes = sql.SQL(_MAIL_NOTES.replace("{mail}", mail.older))
        row = await (
            await conn.execute(
                sql.SQL("SELECT count(*) {}").format(notes), (rules.at(mail, before, tz, now),)
            )
        ).fetchone()
        children["person_notes"] = int(row[0]) if row else 0
        mine = sql.SQL(_MAIL_INTENTIONS.replace("{mail}", mail.older))
        row = await (
            await conn.execute(sql.SQL("SELECT count(*) {}").format(mine), (rules.at(mail, before, tz, now),))
        ).fetchone()
        children["intentions"] = int(row[0]) if row else 0
    return tables, children


Progress = Callable[[dict[str, int], dict[str, int]], Awaitable[None]]
PASSES = 20  # sweeps of one table before the job gives up (rows that keep qualifying: someone keeps writing)


class EraseIncomplete(Exception):
    """A table whose rows kept qualifying after every sweep (the job fails with this class)."""


def _batch(part: rules.Part, columns: list[str], after: bool) -> sql.Composed:
    """A keyset batch: the next keys after the last one, in key order, locked so the
    children's count and the delete see the same rows. Returns each row's address, then its key."""
    keyed = columns != ["ctid"]
    key = sql.SQL(", ").join(sql.Identifier("m", c) for c in columns) if keyed else sql.SQL("m.ctid")
    shown = key if keyed else sql.SQL("m.ctid::text")
    last = sql.SQL(", ").join(sql.Placeholder() for _ in columns) if keyed else sql.SQL("%s::tid")
    return sql.SQL(
        "SELECT m.ctid::text, {shown} FROM {t} m WHERE ({w}){after} ORDER BY {key} LIMIT %s FOR UPDATE OF m"
    ).format(
        shown=shown,
        t=sql.Identifier(part.table),
        w=sql.SQL(_older(part)),
        after=sql.SQL(" AND ({}) > ({})").format(key, last) if after else sql.SQL(""),
        key=key,
    )


async def _left(conn: AsyncConnection, part: rules.Part, params: tuple[Any, ...]) -> int:
    """How many rows still qualify (a table is finished only when this is 0)."""
    row = await (
        await conn.execute(sql.SQL("SELECT count(*) FROM ({}) x").format(_selected(part)), params)
    ).fetchone()
    return int(row[0]) if row else 0


def _add(into: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    out = dict(into)
    for name, n in more.items():
        out[name] = out.get(name, 0) + n
    return out


async def delete(
    conn: AsyncConnection,
    family: str,
    before: date,
    tz: str,
    now: datetime | None = None,
    progress: Progress | None = None,
    *,
    tables: dict[str, int] | None = None,
    children: dict[str, int] | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    """Delete the family's rows older than `before`, in keyset batches (`retention_rules.BATCH`), until what
    still qualifies counts 0. `tables`/`children`: what a resumed job already took; each
    batch adds to them and `progress` writes them in the batch's own transaction. Per table, how many went
    in all; and the rows that went with them."""
    now = now or datetime.now(UTC)
    tables, children = dict(tables or {}), dict(children or {})
    for part in _family(family):
        params = (rules.at(part, before, tz, now),)
        columns = await _key(conn, part.table) or ["ctid"]
        tables.setdefault(part.table, 0)
        for _ in range(PASSES):
            last: tuple[Any, ...] | None = None
            while True:  # one sweep, keyset: never the same rows twice
                async with conn.transaction():
                    rows = await (
                        await conn.execute(
                            _batch(part, columns, last is not None),
                            (*params, *(last or ()), retention_rules.BATCH),
                        )
                    ).fetchall()
                    if not rows:
                        break
                    last = tuple(rows[-1][1:])
                    tids = [str(r[0]) for r in rows]
                    chosen = sql.SQL("SELECT m.* FROM {} m WHERE m.ctid = ANY (%s::text[]::tid[])").format(
                        sql.Identifier(part.table)
                    )
                    taken = await _children(conn, part.table, chosen, (tids,))
                    if part.table == "mailed_invites":  # their events stay foreign once the UIDs go
                        await conn.execute(
                            "UPDATE calendar_events SET mailed = true WHERE ical_uid IN "
                            "(SELECT uid FROM mailed_invites WHERE ctid = ANY (%s::text[]::tid[]))",
                            (tids,),
                        )
                    # their own deletion is the one that may remove an intention (its trigger)
                    await conn.execute("SELECT set_config('pisa.erase', 'on', true)")
                    cur = await conn.execute(
                        sql.SQL("DELETE FROM {} WHERE ctid = ANY (%s::text[]::tid[])").format(
                            sql.Identifier(part.table)
                        ),
                        (tids,),
                    )
                    went = _add(tables, {part.table: max(cur.rowcount, 0)})
                    with_them = _add(children, taken)
                    if progress is not None:  # counted with the delete: both commit or neither
                        await progress(went, with_them)
                tables, children = went, with_them
            if not await _left(conn, part, params):
                break
        else:
            raise EraseIncomplete(part.table)
    async with conn.transaction():
        if family == "mail":  # the note: an email-sourced gift idea lives as long as its email row
            cur = await conn.execute(
                sql.SQL("DELETE {}").format(sql.SQL(_MAIL_NOTES.replace("{mail}", "false")))
            )
            children = _add(children, {"person_notes": max(cur.rowcount, 0)})
            await conn.execute("SELECT set_config('pisa.erase', 'on', true)")  # their own deletion
            cur = await conn.execute(
                sql.SQL("DELETE {}").format(sql.SQL(_MAIL_INTENTIONS.replace("{mail}", "false")))
            )
            children = _add(children, {"intentions": max(cur.rowcount, 0)})
            if progress is not None:
                await progress(tables, children)
    return tables, children


async def kept(conn: AsyncConnection) -> dict[str, int]:
    """The rows kept per family (the Sunday card's cost of keeping). The planner's
    estimates (`pg_class.reltuples`, `pg_stat_user_tables.n_live_tup` before the first ANALYZE), never a
    count(*) over every table."""
    tables = [p.table for parts in rules.FAMILIES.values() for p in parts]
    rows = await (
        await conn.execute(
            "SELECT c.relname, CASE WHEN c.reltuples >= 0 THEN c.reltuples::bigint "
            "ELSE coalesce(s.n_live_tup, 0) END FROM pg_class c "
            "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
            "WHERE c.relkind = 'r' AND c.relnamespace = 'public'::regnamespace AND c.relname = ANY (%s)",
            (tables,),
        )
    ).fetchall()
    estimate = {str(name): max(int(n), 0) for name, n in rows}
    return {family: sum(estimate.get(p.table, 0) for p in parts) for family, parts in rules.FAMILIES.items()}


async def store_size(conn: AsyncConnection) -> int:
    row = await (await conn.execute("SELECT pg_database_size(current_database())")).fetchone()
    return int(row[0]) if row else 0


async def offsite_backup_at(conn: AsyncConnection) -> datetime | None:
    """When the off-site copy last reached its bucket: the backup job records it (app_meta) after each
    upload (docs-site/self-hosting.md); none while there is no backup job."""
    row = await (await conn.execute("SELECT value FROM app_meta WHERE key = %s", (OFFSITE_KEY,))).fetchone()
    if row is None:
        return None
    try:  # a JSON string (an older script wrote the bare text: still read)
        value = json.loads(str(row[0]))
    except ValueError:
        value = str(row[0])
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _total(tables: dict[str, int], children: dict[str, int]) -> int:
    return sum(tables.values()) + sum(children.values())


# --- the job: one at a time, resumable, never stuck ---------------------------------------------------------

# one deletion at a time, whichever process runs it (the API, the 15-minute maintenance): a
# session lock held for the whole job, so jobs queue
LOCK: LiteralString = "SELECT pg_advisory_lock(hashtext('pisa:erase'))"
TRY_LOCK: LiteralString = "SELECT pg_try_advisory_lock(hashtext('pisa:erase'))"
UNLOCK: LiteralString = "SELECT pg_advisory_unlock(hashtext('pisa:erase'))"


async def _unlock(conn: AsyncConnection) -> None:
    """The lock given back before its connection closes: a closed connection's lock is freed only once its
    server process has exited, a moment later, and the next job (or the next API start) could find it held.
    Called whether or not the lock was taken (a cancel can land as it is granted): unlocking a lock not held
    changes nothing."""
    if not conn.broken and not conn.closed:
        with contextlib.suppress(psycopg.Error):  # a connection that can't answer: the lock goes with it
            await conn.execute(UNLOCK)


LISTED = 20  # GET /v1/data/erase: their latest jobs


def _progress(conn: AsyncConnection, erase_id: uuid.UUID) -> Progress:
    """Writes a batch's counts, in the batch's own transaction."""

    async def write(tables: dict[str, int], children: dict[str, int]) -> None:
        await conn.execute(
            "UPDATE erase_jobs SET tables = %s, children = %s WHERE id = %s",
            (Jsonb(tables), Jsonb(children), erase_id),
        )

    return write


async def _work(conn: AsyncConnection, erase_id: uuid.UUID, clock: Callable[[], datetime]) -> None:
    """One job, the lock held: counted (the first time), deleted from where it stopped, done."""
    job = await (
        await conn.cursor(row_factory=dict_row).execute(
            "SELECT family, before, tz, device_id, created_at, count, tables, children FROM erase_jobs "
            "WHERE id = %s AND state IN ('queued', 'running')",
            (erase_id,),
        )
    ).fetchone()
    if job is None:
        return  # done (or failed) meanwhile
    now = job["created_at"]  # the guards and month cutoffs of the moment they asked
    total = job["count"]
    if total is None:  # the count runs inside the job, not before the 202
        total = _total(*await count(conn, job["family"], job["before"], job["tz"], now))
    await conn.execute("UPDATE erase_jobs SET state = 'running', count = %s WHERE id = %s", (total, erase_id))
    tables, children = await delete(
        conn,
        job["family"],
        job["before"],
        job["tz"],
        now,
        _progress(conn, erase_id),
        tables=job["tables"],
        children=job["children"],
    )
    await conn.execute(
        "UPDATE erase_jobs SET state = 'done', tables = %s, children = %s, finished_at = %s WHERE id = %s",
        (Jsonb(tables), Jsonb(children), clock(), erase_id),
    )
    _log.info(
        "erased",
        family=job["family"],
        before=job["before"].isoformat(),
        count=_total(tables, children),
        device_id=str(job["device_id"]) if job["device_id"] else None,
        erase_id=str(erase_id),
    )


# SQLSTATE classes a resume outlives: the connection (08), a deadlock or serialization (40), the server going
# down (57P01-57P03). The job stays `running`; the 15-minute resume picks it up, no second Face ID
_TRANSIENT = ("08", "40", "57P01", "57P02", "57P03")


def _transient(exc: BaseException) -> bool:
    if state := getattr(exc, "sqlstate", None):
        return str(state).startswith(_TRANSIENT)
    return isinstance(exc, (psycopg.OperationalError, ConnectionError))


async def _failed(
    conninfo: str, erase_id: uuid.UUID, exc: BaseException, clock: Callable[[], datetime]
) -> None:
    """Item 4: whatever raised, the job is `failed` with the exception's class (never its text);
    a transient database error leaves it for the resume."""
    if _transient(exc):
        _log.warning("erase_interrupted", erase_id=str(erase_id), reason=type(exc).__name__)
        return
    _log.error("erase_failed", erase_id=str(erase_id), reason=type(exc).__name__)
    try:
        async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
            await conn.execute(
                "UPDATE erase_jobs SET state = 'failed', failure = %s, finished_at = %s "
                "WHERE id = %s AND state IN ('queued', 'running')",
                (type(exc).__name__, clock(), erase_id),
            )
    except Exception as again:  # the database itself is gone: the 15-minute resume finds it running
        _log.error("erase_failed_unrecorded", erase_id=str(erase_id), reason=type(again).__name__)


async def run(conninfo: str, erase_id: uuid.UUID, clock: Callable[[], datetime]) -> None:
    """The API's job: waits its turn on the lock (a connection of its own; the lock given back), runs.
    Cancelled (shutdown), it stays `queued`/`running` for `run_pending`."""
    try:
        async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
            try:
                await conn.execute(LOCK)
                await _work(conn, erase_id, clock)
            finally:
                await _unlock(conn)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _failed(conninfo, erase_id, exc, clock)


async def run_pending(conninfo: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    """Every queued or interrupted job, oldest first, unless another process holds the lock (it runs them).
    The API's start and the 15-minute maintenance call it. Returns how many it ran."""
    ran = 0
    async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
        try:
            got = await (await conn.execute(TRY_LOCK)).fetchone()
            if not (got and got[0]):
                return 0
            rows = await (
                await conn.execute(
                    "SELECT id FROM erase_jobs WHERE state IN ('queued', 'running') ORDER BY created_at, id"
                )
            ).fetchall()
            for (erase_id,) in rows:
                try:
                    await _work(conn, erase_id, clock)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await _failed(conninfo, erase_id, exc, clock)
                    if conn.broken:
                        break
                ran += 1
        finally:
            await _unlock(conn)
    return ran


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "erase_id": str(row["id"]),
        "family": row["family"],
        "before": row["before"].isoformat(),
        "state": row["state"],
        "count": row["count"],
        "erased": _total(row["tables"], row["children"]),
        "tables": row["tables"],
        "children": row["children"],
        "failure": row["failure"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


_COLUMNS: LiteralString = (
    "id, family, before, state, count, tables, children, failure, created_at, finished_at"
)


class EraseService:
    """Their deletions: the preview, the signed delete as a background job (`erase_jobs`), its state."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner  # the zone Pisa follows (owner_context), not the start-up default
        self._clock = clock
        self._running: set[asyncio.Task[Any]] = set()

    @property
    def _conninfo(self) -> str:
        return str(self._pool.conninfo)

    async def preview(self, family: str, before: date) -> dict[str, Any]:
        _family(family)
        tz, now = self._owner.tz, self._clock()
        check(before, tz, now)
        async with self._pool.connection() as conn:
            tables, children = await count(conn, family, before, tz, now)
        return {
            "family": family,
            "before": before.isoformat(),
            "count": _total(tables, children),
            "tables": tables,
            "children": children,
        }

    async def start(self, family: str, before: date, *, device_id: uuid.UUID | None = None) -> dict[str, Any]:
        """Their deletion, already signed (the API's guard): recorded `queued` and answered at once
        (the count runs inside the job). The same family and day while one is queued or running: that
        job, not a second."""
        _family(family)
        tz, now = self._owner.tz, self._clock()
        check(before, tz, now)
        async with self._pool.connection() as conn:
            for _ in range(3):  # the live one may finish between the conflict and the read
                row = await (
                    await conn.execute(
                        "INSERT INTO erase_jobs (id, family, before, tz, state, device_id, created_at) "
                        "VALUES (%s, %s, %s, %s, 'queued', %s, %s) "
                        "ON CONFLICT (family, before) WHERE state IN ('queued', 'running') "
                        "DO NOTHING RETURNING id",
                        (uuid.uuid4(), family, before, tz, device_id, now),
                    )
                ).fetchone()
                if row is not None:
                    self._spawn(run(self._conninfo, row[0], self._clock))
                    return await self.job(row[0])
                live = await (
                    await conn.execute(
                        "SELECT id FROM erase_jobs WHERE family = %s AND before = %s AND "
                        "state IN ('queued', 'running')",
                        (family, before),
                    )
                ).fetchone()
                if live is not None:
                    return await self.job(live[0])
        raise PisaError("internal", "That deletion is changing right now.", "Try again in a moment")

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(work)
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def resume(self) -> None:
        """At start (and every 15 minutes in the maintenance): a queued or interrupted job runs again, from
        where it stopped."""
        self._spawn(run_pending(self._conninfo, self._clock))

    async def settled(self) -> None:
        """Waits for the jobs this process runs (tests; the shutdown cancels them instead)."""
        while self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def close(self) -> None:
        """Item 4: at shutdown the jobs are cancelled and left for the resume; never waited on."""
        for task in self._running:
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def job(self, erase_id: uuid.UUID) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            row = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT " + _COLUMNS + " FROM erase_jobs WHERE id = %s",  # noqa: S608 (a constant)
                    (erase_id,),
                )
            ).fetchone()
        if row is None:
            raise PisaError("not_found", "There is no such deletion.", "Use the erase_id the delete returned")
        return _view(row)

    async def jobs(self) -> list[dict[str, Any]]:
        """Their latest jobs, newest first (a phone that lost the 202 finds its job)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT " + _COLUMNS + " FROM erase_jobs ORDER BY created_at DESC, id LIMIT %s",  # noqa: S608
                    (LISTED,),
                )
            ).fetchall()
        return [_view(r) for r in rows]
