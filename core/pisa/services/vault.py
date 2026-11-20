"""The life-admin vault: documents with an expiry (`vault_documents`), their reminders
as tasks in the pool, and "you'll be abroad then" from the calendar's trips.

- **Never a full number**: the last 4 only (the table's CHECK and `domain.vault.last4`); notes with a run of
  5+ digits are refused. Their photos of documents are out of scope.
- **Reminders**: `tick` (every minute; waits while Pisa is stopped) puts a renewal task in the pool when a
  reminder is due ("Rinnovare la carta d'identità", the deadline the expiry, the document's link in
  `source_ref`), marks it done, and raises the task's priority at a later one. Adding a document runs its own
  at once (their request).
- **Logs** carry counts and kinds, never a number, a note or a date of theirs.
"""

import json
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import countries
from pisa.domain import vault as rules
from pisa.domain.decisions import Decision
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.tasks import end_of_day
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services.notifications import enqueue
from pisa.services.tasks import insert_task

_log = get_logger("pisa.vault")
REMINDERS_MAX = 6
AIRE_KEY = "vault.aire"  # {registered: bool | None, asked_on, said_on}; never assumed
STARTERS_KEY = "vault.starters"  # the research's first reminders already put in the pool (their keys)
REDACT_KEY = "vault.redact_since"  # an add_document turn's time: its stored words lose any number
REDACT_FOR = timedelta(minutes=15)
TRIP_DAYS = 2  # an all-day or multi-day event of at least this long is a trip


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Check the document")


def _not_found() -> PisaError:
    return PisaError("not_found", "There is no such document in the vault.", "Check the id")


def _reminders(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= REMINDERS_MAX:
        raise _invalid(f"reminders must be 1 to {REMINDERS_MAX} items.")
    out = []
    for r in value:
        days = r.get("days_before") if isinstance(r, dict) else None
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 730:
            raise _invalid("each reminder needs days_before between 1 and 730.")
        out.append({"days_before": days, "done": bool(r.get("done", False))})
    return sorted(out, key=lambda r: -r["days_before"])


def checked(fields: dict[str, Any]) -> dict[str, Any]:
    """RAIL: a document's fields as stored; a full number is refused, never kept."""
    if "number" in fields:
        raise _invalid("Pisa keeps only the last 4 characters of a document number: send number_last4.")
    kind = fields.get("kind")
    if kind is not None and kind not in rules.KINDS:
        raise _invalid("kind is not one Pisa knows.")
    if fields.get("country") is not None and not countries.is_code(fields["country"]):
        raise _invalid("country must be an ISO 3166-1 alpha-2 code (IT, DE, ...).")
    if not rules.valid_last4(fields.get("number_last4")):
        raise _invalid("number_last4 must be exactly 4 letters or digits.")
    if not rules.notes_ok(fields.get("notes")):
        raise _invalid(f"notes are at most {rules.NOTES_MAX} characters and never hold a document number.")
    out = dict(fields)
    for key in ("expires_on", "issued_on"):
        if isinstance(out.get(key), str):
            try:
                out[key] = rules.parse_expiry(out[key])
            except ValueError as err:
                raise _invalid(f"{key}: {err}") from None
    if "reminders" in out and out["reminders"] is not None:
        out["reminders"] = _reminders(out["reminders"])
    return out


class VaultService:
    def __init__(self, pool: AsyncConnectionPool, *, owner: Owner, clock: Callable[[], datetime]) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    async def _trips(self, conn: AsyncConnection, start: date, end: date) -> list[tuple[str, date, date]]:
        """Their calendar's trips in [start, end]: events of two days or more (their title and place)."""
        tz = self._owner.zone
        rows = await (
            await conn.execute(
                "SELECT title, coalesce(location, ''), starts_at, ends_at FROM calendar_events "
                "WHERE status <> 'cancelled' AND starts_at < %s AND ends_at > %s AND ends_at - starts_at >= "
                "%s",
                (
                    end_of_day(end, self._owner.tz),
                    end_of_day(start - timedelta(days=1), self._owner.tz),
                    timedelta(days=TRIP_DAYS) - timedelta(hours=1),
                ),
            )
        ).fetchall()
        return [(f"{t} {loc}", s.astimezone(tz).date(), e.astimezone(tz).date()) for t, loc, s, e in rows]

    async def _json(self, conn: AsyncConnection, row: dict[str, Any]) -> dict[str, Any]:
        tz = self._owner.zone
        start, end = rules.window(row["reminders"], row["expires_on"])
        where = rules.abroad(await self._trips(conn, start, end), row["country"], start, end)
        out = {
            "id": str(row["id"]),
            "kind": row["kind"],
            "country": row["country"],
            "number_last4": row["number_last4"],
            "expires_on": row["expires_on"].isoformat(),
            "issued_on": row["issued_on"].isoformat() if row["issued_on"] else None,
            "notes": row["notes"],
            "reminders": row["reminders"],
            "task_id": str(row["task_id"]) if row["task_id"] else None,
            "abroad": {"where": where[0], "on": where[1].isoformat()} if where else None,
            # a paper ID card no longer takes them abroad (Reg. UE 2019/1157, since 3 Aug 2026)
            "warning": rules.paper_line(await self._owner.lang(conn))
            if rules.paper_id(row["kind"], row["notes"])
            else None,
            "created_at": row["created_at"].astimezone(tz).isoformat(),
            "updated_at": row["updated_at"].astimezone(tz).isoformat(),
        }
        return out

    # --- AIRE, a fact of theirs Pisa asks once and never assumes
    # --------------------------------------------------

    async def aire(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            value = await get_setting(conn, AIRE_KEY) or {}
        return {"registered": value.get("registered"), "asked_on": value.get("asked_on")}

    async def set_aire(self, registered: bool | None, by: str) -> dict[str, Any]:
        """Their answer (their words, or the app); None forgets it (Pisa may ask again)."""
        async with self._pool.connection() as conn, conn.transaction():
            value = dict(await get_setting(conn, AIRE_KEY) or {})
            said_on = self._owner.day(self._clock()).isoformat() if registered is not None else None
            value.update(registered=registered, said_on=said_on, by=by)
            if registered is None:
                value["asked_on"] = None
            await set_setting(conn, AIRE_KEY, Jsonb(value))
            # the open CIE renewal tasks follow the answer (never a country they set themselves)
            for task_id, kind, country in await (
                await conn.execute(
                    "SELECT t.id, d.kind, d.country FROM vault_documents d JOIN tasks t ON t.id = d.task_id "
                    "WHERE d.kind = 'cie' AND t.status = 'open' AND NOT t.country_by_owner"
                )
            ).fetchall():
                await conn.execute(
                    "UPDATE tasks SET country = %s, country_checked = true WHERE id = %s",
                    (rules.bound_country(kind, country, registered, self._owner.country), task_id),
                )
        _log.info("vault_aire_set", known=registered is not None, by=by)
        return await self.aire()

    async def ask_aire(self, conn: AsyncConnection) -> bool:
        """True once: AIRE is unknown and Pisa never asked; marks it asked (the caller asks it now). AIRE is
        Italy's registry of its citizens abroad: never asked unless the user is in Italy (PISA_COUNTRY)."""
        if not self._owner.in_country("IT"):
            return False
        value = dict(await get_setting(conn, AIRE_KEY) or {})
        if value.get("registered") is not None or value.get("asked_on"):
            return False
        value["asked_on"] = self._owner.day(self._clock()).isoformat()
        await set_setting(conn, AIRE_KEY, Jsonb(value))
        return True

    async def aire_asked_recently(self) -> bool:
        """Pisa asked it within the last day (a bare "sì"/"no" right after answers it)."""
        asked = (await self.aire())["asked_on"]
        return bool(asked) and self._owner.day(self._clock()) - date.fromisoformat(asked) <= timedelta(days=1)

    # --- the research's first reminders that stand without a document ---------------------------------------

    async def starters(self) -> int:
        """In the tick (an initiative step: it waits while Pisa is stopped): each starter due today goes in
        the pool once (a yearly one once a year), as a task with its deadline and a ledger line; the paper ID
        check
        only while no CIE is in the vault."""
        today, now = self._owner.day(self._clock()), self._clock()
        made = 0
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.vault.starters'))")
            done = set(await get_setting(conn, STARTERS_KEY) or [])
            lang = await self._owner.lang(conn)
            has_cie = await (
                await conn.execute("SELECT 1 FROM vault_documents WHERE kind = 'cie' LIMIT 1")
            ).fetchone()
            for starter in rules.STARTERS:
                found = rules.starter_due(starter, today)
                if found is None or found[0] in done:
                    continue
                key, due_on = found
                done.add(key)
                if starter.key == "paper_id" and has_cie:
                    continue
                it = lang == "it-IT"
                task = await insert_task(
                    conn,
                    title=starter.title[0 if it else 1],
                    now=now,
                    source_kind="manual",
                    notes=starter.notes[0 if it else 1],
                    due_at=end_of_day(due_on, self._owner.tz),
                    due_kind="date",
                    priority=2,
                    source_ref=f"vault:starter:{key}",
                    source_label="vault",
                    deadline_source="pisa",
                    priority_reason=None,
                )
                if task is None:
                    continue
                await decision_log.store(
                    conn,
                    [Decision("act", "vault_reminder", "pisa_initiative", ref=task.id,
                              purpose=starter.title[0 if it else 1][:120], outcome="done")],
                    at=now, trace_id=None, device_id=None,
                )  # fmt: skip
                made += 1
            await set_setting(conn, STARTERS_KEY, Jsonb(sorted(done)))
        if made:
            _log.info("vault_starters_placed", tasks=made)
        return made

    async def documents(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            rows = await (
                await cur.execute("SELECT * FROM vault_documents ORDER BY expires_on, created_at")
            ).fetchall()
            return [await self._json(conn, r) for r in rows]

    async def get(self, document_id: str) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            row = await self._row(conn, document_id)
            return await self._json(conn, row)

    async def _row(self, conn: AsyncConnection, document_id: str) -> dict[str, Any]:
        try:
            key = uuid.UUID(document_id)
        except ValueError:
            raise _not_found() from None
        cur = conn.cursor(row_factory=dict_row)
        row = await (await cur.execute("SELECT * FROM vault_documents WHERE id = %s", (key,))).fetchone()
        if row is None:
            raise _not_found()
        return row

    async def add(self, fields: dict[str, Any], *, provenance: str, lang: str) -> dict[str, Any]:
        doc = checked(fields)
        if doc.get("kind") is None or doc.get("expires_on") is None:
            raise _invalid("A document needs its kind and its expiry.")
        kind = doc["kind"]
        now = self._clock()
        document_id = uuid.uuid4()
        async with self._pool.connection() as conn, conn.transaction():
            count = (await (await conn.execute("SELECT count(*) FROM vault_documents")).fetchone() or (0,))[0]
            if count >= rules.DOCUMENTS_MAX:
                raise _invalid(f"The vault keeps at most {rules.DOCUMENTS_MAX} documents.")
            await conn.execute(
                "INSERT INTO vault_documents (id, kind, country, number_last4, expires_on, issued_on, notes, "
                "reminders, provenance, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s)",
                (
                    document_id,
                    kind,
                    doc.get("country") or rules.default_country(kind, self._owner.country),
                    (doc.get("number_last4") or "").upper() or None,
                    doc["expires_on"],
                    doc.get("issued_on"),
                    doc.get("notes"),
                    Jsonb(doc.get("reminders") or rules.default_reminders(kind)),
                    provenance,
                    now,
                    now,
                ),
            )
        _log.info("vault_document_added", kind=kind, provenance=provenance)
        if provenance == "said":  # their turn's words lose any number (the next minute's step)
            async with self._pool.connection() as conn:
                await set_setting(conn, REDACT_KEY, Jsonb(now.isoformat()))
        await self.remind(str(document_id), lang)  # their request: its due reminder now, even while stopped
        return await self.get(str(document_id))

    async def update(
        self, document_id: str, patch: dict[str, Any], lang: str | None = None
    ) -> dict[str, Any]:
        lang = lang or self._owner.tag
        changes = checked({k: v for k, v in patch.items()})
        allowed = {"kind", "country", "number_last4", "expires_on", "issued_on", "notes", "reminders"}
        values = {k: v for k, v in changes.items() if k in allowed}
        if "reminders" in values:
            values["reminders"] = Jsonb(values["reminders"])
        if values.get("number_last4"):
            values["number_last4"] = values["number_last4"].upper()
        renewed = False
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._row(conn, document_id)
            if values.get("expires_on") is not None and values["expires_on"] != row["expires_on"]:
                renewed = True
                if "reminders" not in values:  # a new expiry: its reminders again
                    values["reminders"] = Jsonb([r | {"done": False} for r in row["reminders"]])
                if row["task_id"] is not None:  # the old renewal task goes (restorable)
                    await conn.execute(
                        "UPDATE tasks SET status = 'dismissed', updated_at = %s WHERE id = %s AND status = "
                        "'open'",
                        (self._clock(), row["task_id"]),
                    )
                    values["task_id"] = None
            if values:
                sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in values)
                await conn.execute(
                    sql.SQL("UPDATE vault_documents SET {}, updated_at = %s WHERE id = %s").format(sets),
                    (*values.values(), self._clock(), row["id"]),
                )
        if renewed:
            await self.remind(document_id, lang)  # a reminder already due for the new expiry, now
        return await self.get(document_id)

    async def delete(self, document_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._row(conn, document_id)
            if row["task_id"] is not None:
                await conn.execute(
                    "UPDATE tasks SET status = 'dismissed', updated_at = %s WHERE id = %s AND status = "
                    "'open'",
                    (self._clock(), row["task_id"]),
                )
            await conn.execute("DELETE FROM vault_documents WHERE id = %s", (row["id"],))

    # --- reminders ------------------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Every minute (an initiative step: it waits while Pisa is stopped): the documents with a reminder
        due."""
        today = self._owner.day(self._clock())
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT id::text, reminders, expires_on FROM vault_documents")
            ).fetchall()
            lang = await self._owner.lang(conn)
        made = 0
        for document_id, reminders, expires_on in rows:
            if rules.due(reminders, expires_on, today):
                made += await self.remind(document_id, lang)
        return made

    async def remind(self, document_id: str, lang: str) -> int:
        """A due reminder: the renewal task in the pool (made, or raised), the reminder marked done."""
        now, today = self._clock(), self._owner.day(self._clock())
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.vault'))")
            row = await self._row(conn, document_id)
            due = rules.due(row["reminders"], row["expires_on"], today)
            if not due:
                return 0
            reminders = [r | {"done": True} if i in due else r for i, r in enumerate(row["reminders"])]
            open_task = None
            if row["task_id"] is not None:
                open_task = await (
                    await conn.execute(
                        "SELECT id FROM tasks WHERE id = %s AND status = 'open'", (row["task_id"],)
                    )
                ).fetchone()
            start, end = rules.window(row["reminders"], row["expires_on"])
            where = rules.abroad(await self._trips(conn, start, end), row["country"], start, end)
            said = (
                rules.abroad_line(row["kind"], row["expires_on"], where[0], where[1], lang) if where else None
            )
            if open_task is None:
                task = await insert_task(
                    conn,
                    title=rules.task_title(row["kind"], lang),
                    now=now,
                    source_kind="manual",
                    notes=said,
                    due_at=end_of_day(row["expires_on"], self._owner.tz),
                    due_kind="date",
                    priority=1 if row["expires_on"] - today <= timedelta(days=30) else 2,
                    source_ref=f"vault:{row['id']}",
                    source_label="vault",
                    deadline_source="owner",
                    priority_reason=None,
                )
                task_id = task.id if task is not None else None
                if task_id is not None:  # an in-person renewal is bound to its country
                    aire = (await get_setting(conn, AIRE_KEY) or {}).get("registered")
                    await conn.execute(
                        "UPDATE tasks SET country = %s, country_checked = true WHERE id = %s",
                        (
                            rules.bound_country(row["kind"], row["country"], aire, self._owner.country),
                            task_id,
                        ),
                    )
            else:
                task_id = str(open_task[0])
                await conn.execute(  # a later reminder: the task rises
                    "UPDATE tasks SET priority = 1, priority_source = 'pisa', updated_at = %s WHERE id = %s",
                    (now, task_id),
                )
            await conn.execute(
                "UPDATE vault_documents SET reminders = %s, task_id = %s, updated_at = %s WHERE id = %s",
                (Jsonb(reminders), task_id, now, row["id"]),
            )
            why = _why(row["kind"], row["expires_on"], lang)
            await decision_log.store(
                conn,
                [
                    Decision(
                        "act", "vault_reminder", "pisa_initiative", ref=task_id, purpose=why, outcome="done"
                    )
                ],
                at=now,
                trace_id=None,
                device_id=None,
            )
            if said is not None:  # Pisa says so (in the user's quiet hours, held like any other message)
                await enqueue(
                    conn,
                    kind="info",
                    text=said,
                    lang=lang,
                    now=now,
                    timezone=self._owner.tz,
                    path="vault.renewal",
                )
            # where they renew depends on AIRE: asked once, at the first document it matters for
            if rules.aire_matters(row["kind"], row["country"], self._owner.country) and await self.ask_aire(
                conn
            ):
                question = rules.AIRE_QUESTION["it-IT" if lang == "it-IT" else "en-US"]
                await enqueue(
                    conn,
                    kind="info",
                    text=question,
                    lang=lang,
                    now=now,
                    timezone=self._owner.tz,
                    path="vault.aire",
                )
        _log.info("vault_reminder_due", kind=row["kind"], abroad=where is not None)
        return 1


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k not in ("timestamp", "tool_call_id") for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


async def redact_turns(pool: AsyncConnectionPool, now: datetime) -> int:
    """Every minute (it runs while stopped: bookkeeping): after an add_document turn, the user's stored words
    and the turn's tool calls of the next 15 minutes lose every document-number-like stretch."""
    async with pool.connection() as conn:
        since = await get_setting(conn, REDACT_KEY)
        if not since:
            return 0
        start = datetime.fromisoformat(since) - timedelta(minutes=2)
        rows = await (
            await conn.execute(
                "SELECT id, content, model_messages::text, meant, corrections::text FROM messages "
                "WHERE created_at >= %s",
                (start,),
            )
        ).fetchall()
        changed = 0
        for message_id, content, model, meant, fixes in rows:
            clean = rules.redact(content)
            # Pisa's `meant` beside their words loses the same stretches; the corrections
            # are dropped whole when they hold one
            clean_meant = rules.redact(meant) if meant is not None else None
            drop_fixes = bool(fixes) and any(rules.numbers_in(s) for s in _strings(json.loads(fixes)))
            # the turn's tool calls (history) are dropped whole when they hold one: never half-edited
            # JSON
            drop = bool(model) and any(rules.numbers_in(s) for s in _strings(json.loads(model)))
            if clean != content or drop or clean_meant != meant or drop_fixes:
                await conn.execute(
                    "UPDATE messages SET content = %s, model_messages = CASE WHEN %s THEN NULL "
                    "ELSE model_messages END, meant = %s, corrections = CASE WHEN %s THEN NULL "
                    "ELSE corrections END WHERE id = %s",
                    (clean, drop, clean_meant, drop_fixes, message_id),
                )
                changed += 1
        if now - start > REDACT_FOR:
            await set_setting(conn, REDACT_KEY, Jsonb(None))
    if changed:
        _log.info("vault_turn_redacted", messages=changed)
    return changed


def _why(kind: str, expires_on: date, lang: str) -> str:
    what = rules.label(kind, lang)
    month = rules.month_name(expires_on, lang)
    if lang == "it-IT":
        return f"{what[0].upper()}{what[1:]} scade a {month} {expires_on.year}."[:120]
    return f"{what[0].upper()}{what[1:]} expires in {month} {expires_on.year}."[:120]
