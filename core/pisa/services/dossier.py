"""`pisa.domain.dossier`: one finite morning.

- **The dossier** is built from the knock's own parts (`KnockService.parts`, never spoken by themselves) and
  four more readers: the admin radar (`ConnectorsService.obligations`: only sources that are on), their open
  loops (`intentions`: a promise waiting for their yes, what they wait for that is due, an intention due), the
  night's drafts (`email_drafts` not sent, made or changed in the last day) and "domani, provato"
  (`day_rehearsals`, a row only when the night found a real conflict). Kept in `dossier_items`, one row a
  record a morning, with the records read for it (`sources`) and their thumb.
- **The knock** (`KnockService.knock`, attend's sense, through attend and the guardian's gate as
  `attend.knock`) also knocks for these readers' items. **Their yes** reads the dossier (`say`): at most three
  items, then how many are on the phone, then "È tutto". The phone reads the whole list (`today`,
  `GET /v1/dossier`) and sends their thumbs (`thumb`).
- **The night** (`rehearse`, step `p2_provato`): the day it opens rehearsed with the Ghost Run's own readers;
  a row only for an overlap or a trip that doesn't fit.
- Nothing is done, sent or booked here: Pisa only speaks their records back. Logs carry ids and counts
  only.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import dossier as rules
from pisa.domain import voice1 as knock_rules
from pisa.domain import wrist
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger

# a block whose title a stranger controls (an invite's event, an email's task): voice1's own
from pisa.services.voice1 import EVENT_FOREIGN, TASK_FOREIGN

_log = get_logger("pisa.dossier")

Parts = Callable[[AsyncConnection, datetime, str], Awaitable[list[knock_rules.Part]]]


class DossierService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        sources: Any = None,  # ConnectorsService: the admin radar's reader (only sources that are on)
        rehearsal: Any = None,  # DayRehearsal: the Ghost Run's day readers ("domani, provato")
        parts: Parts | None = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._sources = sources
        self._rehearsal = rehearsal
        self.parts = parts  # KnockService.parts: the knock's own parts

    def _day(self, now: datetime) -> date:
        return now.astimezone(self._owner.zone).date()

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {"p2_provato": self.rehearse}

    # --- the readers ----------------------------------------------------------------------------------------

    async def own_items(self, conn: AsyncConnection, now: datetime, lang: str) -> list[rules.Item]:
        """The dossier's own items, beside the knock's parts: the radar, their loops, the drafts, the
        rehearsal. A reader's failure never fails the morning."""
        out: list[rules.Item] = []
        for read in (self._provato, self._radar, self._loops, self._drafts):
            try:
                async with conn.transaction():
                    out += await read(conn, now, lang)
            except Exception as exc:
                _log.warning("dossier_reader_skipped", reader=read.__name__, cause=exc.__class__.__name__)
        return out

    async def _radar(self, conn: AsyncConnection, now: datetime, lang: str) -> list[rules.Item]:
        if self._sources is None:
            return []
        today = self._day(now)
        found = await self._sources.obligations(rules.RADAR_DAYS, lang)
        out = []
        for o in found["items"]:
            due = date.fromisoformat(o["due_on"]) if o.get("due_on") else None
            if due is None or o.get("state") not in ("upcoming", "ready", "overdue"):
                continue  # a date to set, or done: the radar's own screen
            if (due - today).days > rules.RADAR_DAYS:
                continue
            out.append(
                rules.Item(
                    "radar",
                    rules.radar_line(str(o["what"]), due, today, lang),
                    "obligations",
                    str(o["id"]),
                    (rules.Source("obligations", str(o["id"])),),
                    rules.radar_rank(due, today),
                    due,
                )
            )
        return out

    async def _loops(self, conn: AsyncConnection, now: datetime, lang: str) -> list[rules.Item]:
        """A promise of their waiting for their yes; what they wait for, due; an intention of their due (its
        own day or
        its pool task's)."""
        today = self._day(now)
        rows = await (
            await conn.cursor(row_factory=dict_row).execute(
                "SELECT i.id::text AS id, i.kind, i.state, i.what, i.who, i.source, i.source_ref, "
                "i.task_id::text AS task_id, coalesce(i.due_on, (t.due_at AT TIME ZONE %s)::date) AS due "
                "FROM intentions i LEFT JOIN tasks t ON t.id = i.task_id AND t.status = 'open' "
                "AND t.deleted_at IS NULL "
                "WHERE i.what IS NOT NULL AND (i.state = 'proposed' OR (i.state = 'open' AND "
                "coalesce(i.due_on, (t.due_at AT TIME ZONE %s)::date) <= %s)) "
                "ORDER BY i.created_at LIMIT 50",
                (self._owner.tz, self._owner.tz, today),
            )
        ).fetchall()
        out = []
        for r in rows:
            sources = [rules.Source("intentions", r["id"])]
            if r["task_id"]:
                sources.append(rules.Source("tasks", r["task_id"]))
            if r["source_ref"] and r["source"] in ("sent_mail", "received_mail"):
                sources.append(rules.Source("email_messages", str(r["source_ref"])))
            due = r["due"]
            out.append(
                rules.Item(
                    "loop",
                    rules.loop_line(r["kind"], r["state"], str(r["what"]), r["who"], due, today, lang),
                    "intentions",
                    r["id"],
                    tuple(sources),
                    rules.RANK["loop"],
                    due,
                    outside=r["source"] == "received_mail",  # another person's words: shown, never said
                )
            )
        return out

    async def _drafts(self, conn: AsyncConnection, now: datetime, lang: str) -> list[rules.Item]:
        rows = await (
            await conn.execute(
                # a draft whose yes was refused or lapsed is dead: it waits for nothing
                "SELECT d.id::text FROM email_drafts d WHERE d.sent_at IS NULL AND d.updated_at >= %s "
                "AND (d.approval_id IS NULL OR EXISTS (SELECT 1 FROM approvals a WHERE a.id = d.approval_id "
                "AND a.status = 'pending')) "
                "ORDER BY d.updated_at DESC LIMIT 20",
                (now - timedelta(hours=rules.DRAFT_HOURS),),
            )
        ).fetchall()
        if not rows:
            return []
        ids = [str(r[0]) for r in rows]
        return [
            rules.Item(
                "draft",
                rules.drafts_line(len(ids), lang),
                "email_drafts",
                ids[0],
                tuple(rules.Source("email_drafts", i) for i in ids),
                rules.RANK["draft"],
            )
        ]

    async def _provato(self, conn: AsyncConnection, now: datetime, lang: str) -> list[rules.Item]:
        day = self._day(now)
        row = await (
            await conn.execute(
                "SELECT kind, line, refs, outside, need_min FROM day_rehearsals WHERE day = %s", (day,)
            )
        ).fetchone()
        if row is None:
            return []
        kind, line, refs, outside, need = row
        if self._rehearsal is not None:  # the night's conflict, re-checked now (moved, cancelled)
            blocks = await self._rehearsal.day_blocks(day)
            if not rules.still(str(kind), [str(r["id"]) for r in refs or []], need, blocks):
                await conn.execute("DELETE FROM day_rehearsals WHERE day = %s", (day,))
                _log.info("dossier_provato_gone", day=day.isoformat(), kind=kind)
                return []
        sources = [rules.Source("day_rehearsals", day.isoformat())] + [
            rules.Source(str(r["table"]), str(r["id"])) for r in refs or []
        ]
        return [
            rules.Item(
                "provato",
                rules.provato_line(str(line), lang),
                "day_rehearsals",
                day.isoformat(),
                tuple(sources),
                rules.RANK["provato"],
                day,
                outside=bool(outside),
            )
        ]

    # --- the morning ----------------------------------------------------------------------------------------

    async def build(
        self,
        conn: AsyncConnection,
        now: datetime,
        lang: str,
        parts: list[knock_rules.Part],
        *,
        speak: bool,
    ) -> list[rules.Item]:
        """Today's dossier from the knock's parts and the readers, kept (one row a record, their thumbs kept);
        with `speak`, the next items Pisa says are marked spoken (at most three a morning in all)."""
        day = self._day(now)
        items = [rules.from_part(p) for p in parts] + await self.own_items(conn, now, lang)
        # the lock before reading what was said (two yeses never trip the three-a-day trigger)
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('dossier:' || %s))", (day.isoformat(),))
        said = frozenset(
            (str(t), str(r))
            for t, r in await (
                await conn.execute(
                    "SELECT src_table, ref FROM dossier_items WHERE day = %s AND spoken", (day,)
                )
            ).fetchall()
        )
        out = rules.assemble(items, said=said, speak=speak)
        for item in out:
            newly = item.spoken and (item.table, item.ref) not in said
            row = await (
                await conn.execute(
                    "INSERT INTO dossier_items (id, day, kind, rank, src_table, ref, text, sources, outside, "
                    "current, spoken, said_at, built_at, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s) "
                    "ON CONFLICT (day, src_table, ref) DO UPDATE SET kind = EXCLUDED.kind, "
                    "rank = EXCLUDED.rank, text = EXCLUDED.text, sources = EXCLUDED.sources, "
                    "outside = EXCLUDED.outside, current = true, "
                    "spoken = dossier_items.spoken OR EXCLUDED.spoken, "
                    "said_at = coalesce(dossier_items.said_at, EXCLUDED.said_at), "
                    "built_at = EXCLUDED.built_at "
                    "RETURNING id",
                    (
                        uuid.uuid4(),
                        day,
                        item.kind,
                        item.rank,
                        item.table,
                        item.ref,
                        item.text[:400],
                        Jsonb([s.json() for s in item.sources]),
                        item.outside,
                        newly,
                        now if newly else None,
                        now,
                        now,
                    ),
                )
            ).fetchone()
            # the sources read for each item, logged (ids only, never words)
            _log.info(
                "dossier_item",
                item_id=str(row[0]) if row else None,
                kind=item.kind,
                spoken=item.spoken,
                sources=[f"{s.table}:{s.id}" for s in item.sources],
            )
        # a loop the dossier said is nudged: the loops' own sense never says it again
        loops = [i.ref for i in out if i.spoken and i.table == "intentions" and (i.table, i.ref) not in said]
        if loops:
            await conn.execute(
                "UPDATE intentions SET nudged_at = coalesce(nudged_at, %s) WHERE id::text = ANY(%s)",
                (now, loops),
            )
        await conn.execute(
            "UPDATE dossier_items SET current = false WHERE day = %s AND current "
            "AND NOT ((src_table, ref) IN (SELECT * FROM unnest(%s::text[], %s::text[])))",
            (day, [i.table for i in out], [i.ref for i in out]),
        )
        _log.info(
            "dossier_built",
            day=day.isoformat(),
            items=len(out),
            spoken=sum(i.spoken for i in out),
            speak=speak,
        )
        return out

    async def say(
        self, conn: AsyncConnection, now: datetime, lang: str, parts: list[knock_rules.Part]
    ) -> tuple[str, list[rules.Item]]:
        """Their yes to the knock (or "dimmi" in the morning): at most three items said, then "È tutto"."""
        items = await self.build(conn, now, lang, parts, speak=True)
        return rules.spoken(items, lang), items

    async def today(self) -> dict[str, Any]:
        """The phone's dossier (`GET /v1/dossier`): today's items, the spoken ones first, with their sources
        and their thumbs. Built now when the morning has new records (never marks anything spoken)."""
        now = self._clock()
        day = self._day(now)
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            parts = await self.parts(conn, now, lang) if self.parts is not None else []
            await self.build(conn, now, lang, parts, speak=False)
            rows = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT id::text AS id, kind, rank, text, sources, outside, spoken, said_at, thumb "
                    "FROM dossier_items WHERE day = %s AND (current OR spoken) "
                    "ORDER BY NOT spoken, rank, created_at",
                    (day,),
                )
            ).fetchall()
        said = [r for r in rows if r["spoken"]]
        return {
            "day": day.isoformat(),
            "said_at": min(r["said_at"] for r in said).isoformat() if said else None,
            "closing": rules.word(rules.ALL, lang),
            "items": [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "text": r["text"],
                    "spoken": r["spoken"],
                    "outside": r["outside"],
                    "sources": r["sources"],
                    "thumb": r["thumb"],
                }
                for r in rows
            ],
        }

    async def thumb(self, item_id: uuid.UUID, thumb: str | None) -> dict[str, Any]:
        """Their thumb on an item, from the phone (up, down, or none to take it back)."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE dossier_items SET thumb = %s, thumbed_at = CASE WHEN %s::text IS NULL THEN NULL "
                    "ELSE %s END, thumb_origin = %s WHERE id = %s RETURNING id::text, kind",
                    (thumb, thumb, self._clock(), wrist.current(), item_id),
                )
            ).fetchone()
        if row is None:
            raise PisaError(
                "not_found", "There is no such item in the morning's dossier.", "Reload the dossier"
            )
        _log.info("dossier_thumb", item_id=row[0], kind=row[1], thumb=thumb)
        return {"id": row[0], "thumb": thumb}

    # --- the night: "domani, provato"
    # -------------------------------------------------------------------------

    async def rehearse(self, night: date) -> int:
        """The day the night opens, rehearsed: a row only when two blocks overlap or a trip doesn't fit."""
        if self._rehearsal is None:
            return 0
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
        blocks, travel = await self._rehearsal.rehearse_blocks(night)
        found = rules.conflict(blocks, travel, lang)
        if found is None:
            async with self._pool.connection() as conn:  # an earlier run's conflict is gone
                await conn.execute("DELETE FROM day_rehearsals WHERE day = %s", (night,))
            _log.info("dossier_rehearsed", blocks=len(blocks), conflict=None)
            return 0
        refs = [
            {"table": "calendar_events" if b.kind == "event" else "tasks", "id": b.ref} for b in found.blocks
        ]
        a, b = found.blocks
        need = travel.get((a.ref, b.ref)) if found.kind == "transfer" else None
        async with self._pool.connection() as conn, conn.transaction():
            outside = False
            for b in found.blocks:
                sql = EVENT_FOREIGN if b.kind == "event" else TASK_FOREIGN
                row = await (await conn.execute(sql, (b.ref,))).fetchone()
                outside = outside or row is None or row[0] is None or bool(row[0])  # fails closed (voice1's)
            await conn.execute(
                "INSERT INTO day_rehearsals (day, kind, line, refs, outside, need_min, rehearsed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (day) DO UPDATE SET kind = EXCLUDED.kind, "
                "line = EXCLUDED.line, refs = EXCLUDED.refs, outside = EXCLUDED.outside, "
                "need_min = EXCLUDED.need_min, rehearsed_at = EXCLUDED.rehearsed_at",
                (night, found.kind, found.line[:300], Jsonb(refs), outside, need, self._clock()),
            )
        _log.info("dossier_rehearsed", blocks=len(blocks), conflict=found.kind)
        return 1
