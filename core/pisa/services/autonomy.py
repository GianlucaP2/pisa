"""Earned autonomy: kept and undone per action type, read from what Pisa already records
(approvals, done_by_presence, the decision log, email drafts); the level per type (`autonomy_levels`, every
type at `ask` until the user says otherwise); Pisa's proposals; the gate the approvals read.

A level changes only by the user: the phone's signature after Face ID (`set_signed`) or their spoken yes to
the proposal Pisa made on Sunday (`accept_spoken`). Lowering one (the ledger's Undo, a "chiedimelo di nuovo")
is always allowed: asking more is never a risk.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import autonomy as rules
from pisa.domain import unlocks as unlock_rules
from pisa.domain.decisions import Decision
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services.phone_keys import PhoneKeys
from pisa.services.stop import stopped_now

_log = get_logger("pisa.autonomy")
PROPOSAL_ASKED = timedelta(minutes=10)  # Pisa's reply that put the proposal, for a plain "sì" right after
_LEVEL_WORDS = {
    "it-IT": {
        "ask": "ti chiedo prima",
        "tell": "lo faccio e te lo dico",
        "silent": "lo faccio senza dirtelo",
        "ledger": "lo faccio e lo vedi la domenica",
    },
    "en-US": {
        "ask": "I ask first",
        "tell": "I do it and tell you",
        "silent": "I do it without telling you",
        "ledger": "I do it and you see it on Sunday",
    },
}
_TYPE_WORDS = {
    "it-IT": {
        "calendar_create": "Nuovi eventi",
        "calendar_move": "Spostare eventi",
        "calendar_cancel": "Annullare eventi",
        "task_done": "Chiudere attività",
        "task_close": "Togliere attività ferme",
        "email_draft": "Bozze di email",
        "email_send": "Inviare email",
    },
    "en-US": {
        "calendar_create": "New events",
        "calendar_move": "Moving events",
        "calendar_cancel": "Cancelling events",
        "task_done": "Closing tasks",
        "task_close": "Clearing stale tasks",
        "email_draft": "Email drafts",
        "email_send": "Sending email",
    },
}


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Check the request")


def level_line(type_: str, level: str, lang: str) -> str:
    key = "it-IT" if lang == "it-IT" else "en-US"
    return f"{_TYPE_WORDS[key].get(type_, type_)}: {_LEVEL_WORDS[key].get(level, level)}."


class AutonomyService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: PhoneKeys,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        thresholds: rules.Thresholds = rules.THRESHOLDS,
    ) -> None:
        self._pool = pool
        self._keys = keys
        self._owner = owner
        self._clock = clock
        self.thresholds = thresholds

    # --- the evidence -------------------------------------------------------------------------------------

    async def outcomes(self, conn: AsyncConnection) -> dict[str, list[tuple[datetime, bool, str | None]]]:
        """Each type's actions in the evidence window (Pisa keeps its records 90 days), newest first:
        (when, undone, what it was), from the records Pisa already keeps."""
        now = self._clock()
        since = now - rules.EVIDENCE
        out: dict[str, list[tuple[datetime, bool, str | None]]] = {t: [] for t in rules.TYPES}
        rows = await (
            await conn.execute(
                "SELECT kind, executed_at, undone_at IS NOT NULL, summary FROM approvals "
                "WHERE status = 'done' AND executed_at >= %s",
                (since,),
            )
        ).fetchall()
        rows += [
            ("task_done", *r)
            for r in await (
                await conn.execute(
                    "SELECT d.done_at, d.undone_at IS NOT NULL, t.title FROM done_by_presence d "
                    "LEFT JOIN tasks t ON t.id = d.task_id WHERE d.task_id IS NOT NULL AND d.done_at >= %s",
                    (since,),
                )
            ).fetchall()
        ]
        rows += [
            ("task_close", *r)
            for r in await (
                await conn.execute(
                    "SELECT d.at, t.status <> 'dismissed', t.title FROM pisa_decisions d JOIN tasks t "
                    "ON t.id::text = d.ref WHERE d.move = 'act' AND d.tool = 'aging' AND d.at >= %s",
                    (since,),
                )
            ).fetchall()
        ]
        rows += [  # a draft sent is kept; one left unsent a day later, undone; a fresh one, not yet either
            ("email_draft", *r)
            for r in await (
                await conn.execute(
                    "SELECT created_at, sent_at IS NULL, subject FROM email_drafts WHERE created_at >= %s "
                    "AND (sent_at IS NOT NULL OR created_at < %s - interval '1 day')",
                    (since, now),
                )
            ).fetchall()
        ]
        for kind, at, undone, what in rows:
            if kind in out and at is not None:
                out[kind].append((at, bool(undone), what))
        for actions in out.values():
            actions.sort(key=lambda a: a[0], reverse=True)
        return out

    async def counts(self, conn: AsyncConnection) -> dict[str, tuple[int, int]]:
        """(kept, undone) per type over the app's window (the last 30 days)."""
        since = self._clock() - self.thresholds.window
        return {
            t: (
                sum(1 for at, u, _ in a if at >= since and not u),
                sum(1 for at, u, _ in a if at >= since and u),
            )
            for t, a in (await self.outcomes(conn)).items()
        }

    async def _last_stop(self, conn: AsyncConnection) -> datetime | None:
        row = await (await conn.execute("SELECT max(stopped_at) FROM pisa_stops")).fetchone()
        return row[0] if row else None

    def _evidence(
        self,
        type_: str,
        level: str,
        since: datetime,
        actions: list[tuple[datetime, bool, str | None]],
        stopped_at: datetime | None,
    ) -> dict[str, Any] | None:
        """Pisa's proposal for this type, with its numbers (the actions at this level), or None."""
        now = self._clock()
        # At ask every action counts (Pisa asked for each one; a stop or a demotion keeps the history); at a
        # higher level, those since it was set. Observed: from the oldest counted (at ask), else since the
        # level.
        mine = actions if level == "ask" else [a for a in actions if a[0] >= since]
        if not mine:
            return None
        recent = sum(1 for a in actions if a[0] >= now - rules.RECENT)
        rung = rules.RUNGS.get(type_, {}).get(rules.next_level(level) or "")
        observed = min(a[0] for a in mine[: rung.last]) if level == "ask" and rung is not None else since
        up = rules.earned(
            type_, level, [u for _, u, _ in mine], observed, now, recent=recent, stopped_at=stopped_at
        )
        if up is None:
            return None
        tz = self._owner.zone
        undone = sum(1 for _, u, _ in mine if u)
        last = next(((at, what) for at, u, what in actions if u), None)
        first = min(at for at, _, _ in mine)
        upper = rules.bound(len(mine), undone)
        return {
            "level": up,
            "kept": len(mine) - undone,
            "undone": undone,
            "days": max(1, (now - first).days),
            "since_on": first.astimezone(tz).date().isoformat(),
            "bound_pct": round(upper * 100) if upper is not None else None,
            "last_error": {
                "on": last[0].astimezone(tz).date().isoformat(),
                "what": " ".join((last[1] or "").split())[:120] or None,
            }
            if last is not None
            else None,
        }

    async def _demote(self, conn: AsyncConnection, only: str | None = None) -> None:
        """An action Pisa did at a type's level that they undid takes the
        type down one rung (never all the way, the history kept); a ledger line says so. So does an undo made
        since the level was set of an action done before it (undone after a re-raise)."""
        rows = await self._rows(conn)
        actions = await self.outcomes(conn)
        now = self._clock()
        lang = await self._owner.lang(conn)
        for type_, (level, since, _, _) in rows.items():
            # a draft left unsent is no undo: nothing left (the research demotes drafts on rewrites only)
            if level == "ask" or type_ == "email_draft" or (only is not None and type_ != only):
                continue
            undone_here = any(u and at >= since for at, u, _ in actions.get(type_, []))
            if not undone_here and not await self._undone_since(conn, type_, since):
                continue
            down = rules.LADDER[rules.LADDER.index(level) - 1]
            await conn.execute(
                "UPDATE autonomy_levels SET level = %s, since = %s, changed_by = 'default', proposed_level = "
                "NULL, proposed_at = NULL WHERE type = %s",
                (down, now, type_),
            )
            await decision_log.store(
                conn,
                [Decision("act", "autonomy_level", "pisa_initiative", ref=f"{type_}:{level}:{down}",
                          purpose=level_line(type_, down, lang), outcome="done")],
                at=now, trace_id=None, device_id=None,
            )  # fmt: skip
            _log.info("autonomy_demoted", type=type_, level=down)

    async def _undone_since(self, conn: AsyncConnection, type_: str, since: datetime) -> bool:
        """An action of this type they undid at or after `since`, whenever it was done (90 days of
        records)."""
        if type_ == "task_done":
            query = "SELECT 1 FROM done_by_presence WHERE task_id IS NOT NULL AND undone_at >= %s LIMIT 1"
            args: tuple[Any, ...] = (since,)
        elif type_ == "task_close":  # an aged task reopened (or done) after Pisa cleared it
            query = (
                "SELECT 1 FROM pisa_decisions d JOIN tasks t ON t.id::text = d.ref WHERE d.move = 'act' AND "
                "d.tool = 'aging' AND t.status <> 'dismissed' AND t.updated_at >= %s LIMIT 1"
            )
            args = (since,)
        else:
            query = "SELECT 1 FROM approvals WHERE kind = %s AND status = 'done' AND undone_at >= %s LIMIT 1"
            args = (type_, since)
        return await (await conn.execute(query, args)).fetchone() is not None

    # --- the three metrics -----------------------------------------------------------------------------

    async def metrics(self, start: datetime, end: datetime) -> dict[str, Any]:
        """The week's three metrics: the undo rate per type per rung (with the bound), the rubber-stamp rate
        (their yeses to Pisa's asks given in under 3 s), the surprise rate (undos of what Pisa did silently,
        per 100)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT kind, CASE WHEN decided_auth LIKE 'autonomy:%%' THEN "
                    "split_part(decided_auth, ':', 2) ELSE 'ask' END, count(*) FILTER (WHERE undone_at IS "
                    "NULL), count(*) FILTER (WHERE undone_at IS NOT NULL) FROM approvals "
                    "WHERE status = 'done' AND executed_at >= %s AND executed_at < %s "
                    "GROUP BY 1, 2 ORDER BY 1, 2",
                    (start, end),
                )
            ).fetchall()
            fast, asked = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE decided_at - created_at < %s), count(*) FROM approvals "
                    "WHERE decided_auth IN ('tap', 'voice', 'words', 'biometric') AND status IN ('approved', "
                    "'done', 'failed') AND decided_at >= %s AND decided_at < %s",
                    (rules.RUBBER_STAMP, start, end),
                )
            ).fetchone() or (0, 0)
        undo = []
        for kind, rung, kept, undone in rows:
            upper = rules.bound(kept + undone, undone)
            undo.append({
                "type": kind, "rung": rung, "kept": kept, "undone": undone,
                "rate_pct": round(100 * undone / (kept + undone)) if kept + undone else 0,
                "bound_pct": round(upper * 100) if upper is not None else None,
            })  # fmt: skip
        silent = [u for u in undo if u["rung"] in ("silent", "ledger")]
        actions = sum(u["kept"] + u["undone"] for u in silent)
        surprises = sum(u["undone"] for u in silent)
        return {
            "undo": undo,
            "rubber_stamp": {
                "fast": fast,
                "asked": asked,
                "rate_pct": round(100 * fast / asked) if asked else 0,
            },
            "surprise": {
                "undone": surprises,
                "actions": actions,
                "per_100": round(100 * surprises / actions, 1) if actions else 0.0,
            },
        }

    async def _rows(
        self, conn: AsyncConnection
    ) -> dict[str, tuple[str, datetime, str | None, datetime | None]]:
        now = self._clock()
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO autonomy_levels (type, level, since) VALUES (%s, 'ask', %s) ON CONFLICT DO "
                "NOTHING",
                [(t, now) for t in rules.TYPES],
            )
        rows = await (
            await conn.execute("SELECT type, level, since, proposed_level, proposed_at FROM autonomy_levels")
        ).fetchall()
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}

    async def listed(self) -> dict[str, Any]:
        tz, now = self._owner.zone, self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await self._demote(conn)
            rows, actions = await self._rows(conn), await self.outcomes(conn)
            stopped, last_stop = await stopped_now(conn), await self._last_stop(conn)
        since_window = now - self.thresholds.window
        out = []
        for t in rules.TYPES:
            level, since, _, _ = rows[t]
            recent = [u for at, u, _ in actions[t] if at >= since_window]
            item: dict[str, Any] = {
                "type": t,
                "level": level,
                "kept": sum(1 for u in recent if not u),
                "undone": sum(1 for u in recent if u),
                "since": since.astimezone(tz).isoformat(),
                "locked": t in rules.LOCKED,
            }
            if (found := self._evidence(t, level, since, actions[t], last_stop)) is not None:
                item["proposal"] = found
            out.append(item)
        frozen = last_stop + rules.STOP_FREEZE if last_stop is not None else None
        return {
            "types": out,
            "stopped": stopped,
            # after "ferma tutto", no proposal until then
            "proposals_from": frozen.astimezone(tz).isoformat()
            if frozen is not None and frozen > now
            else None,
        }

    async def proposals(self, lang: str) -> list[dict[str, Any]]:
        """Pisa's proposals now, each with its question (the Sunday card's)."""
        out = []
        for i in (await self.listed())["types"]:
            if "proposal" not in i:
                continue
            p = i["proposal"]
            out.append({
                "type": i["type"], **p,
                "text": rules.proposal_text(i["type"], p["level"], p["kept"], p["days"], lang),
            })  # fmt: skip
        return out

    async def unlocks(self, week: str, end: datetime, lang: str) -> list[dict[str, Any]]:
        """The promotions this card announces: not announced yet (or by this week's
        card), made before the week's end, still the type's level now (lowered since: no ceremony); the
        latest per type."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT DISTINCT ON (u.type) u.id, u.type, u.to_level, u.at FROM autonomy_unlocks u "
                    "JOIN autonomy_levels l ON l.type = u.type AND l.level = u.to_level "
                    "WHERE u.at < %s AND (u.announced_week IS NULL OR u.announced_week = %s) "
                    "ORDER BY u.type, u.at DESC",
                    (end, week),
                )
            ).fetchall()
        rows = sorted(rows, key=lambda r: r[3])
        return [
            {
                "id": str(i),
                "type": t,
                "level": lvl,
                "at": at.isoformat(),
                "text": unlock_rules.text(t, lvl, lang),
            }
            for i, t, lvl, at in rows
        ]

    async def mark_announced(self, unlocks: list[dict[str, Any]], week: str) -> None:
        """The Sunday card went out with them: never announced again. Only the ones the card
        showed (`MAX_SHOWN`), and each shown type's older promotions up to the shown one; the rest wait for
        the next card."""
        shown = unlocks[: unlock_rules.MAX_SHOWN]
        if not shown:
            return
        async with self._pool.connection() as conn:
            for u in shown:
                await conn.execute(
                    "UPDATE autonomy_unlocks SET announced_week = %s WHERE announced_week IS NULL "
                    "AND (id = %s OR (type = %s AND at <= %s))",
                    (week, uuid.UUID(u["id"]), u["type"], datetime.fromisoformat(u["at"])),
                )

    async def mark_proposed(self, proposals: list[dict[str, Any]]) -> None:
        """The proposals Pisa made (the Sunday card): only these can take their spoken yes."""
        if not proposals:
            return
        now = self._clock()
        async with self._pool.connection() as conn:
            for p in proposals:
                await conn.execute(
                    "UPDATE autonomy_levels SET proposed_level = %s, proposed_at = %s WHERE type = %s",
                    (p["level"], now, p["type"]),
                )

    # --- the gate -----------------------------------------------------------------------------------------

    async def verdict(self, type_: str, *, outside: bool, others: bool, early: bool = False) -> rules.Verdict:
        """What Pisa may do with a change it would otherwise propose (the approvals read this). Any failure:
        ask."""
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await self._demote(conn, only=type_)  # an undo at this level takes it down first
                row = await (
                    await conn.execute("SELECT level FROM autonomy_levels WHERE type = %s", (type_,))
                ).fetchone()
                stopped = await stopped_now(conn)
        except Exception:
            _log.exception("autonomy_unreadable")
            return "ask"
        level = row[0] if row else "ask"
        return rules.gate(level, type_=type_, outside=outside, others=others, stopped=stopped, early=early)

    # --- changing a level ---------------------------------------------------------------------------------

    async def set_signed(
        self,
        type_: str,
        level: str,
        nonce: str,
        signature: str,
        key_id: str,
        lang: str,
        *,
        device_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """A level signed by a Face ID key, or by the asking device's own key (`device_id`)."""
        self._check(type_, level)
        await self._keys.verify(
            "autonomy", {"type": type_, "level": level}, nonce, signature, key_id, device_id=device_id
        )
        await self._set(type_, level, "signed", lang)
        return next(i for i in (await self.listed())["types"] if i["type"] == type_)

    async def accept_spoken(self, type_: str, level: str, words: str, *, outside: bool, lang: str) -> str:
        """Their spoken yes to the proposal Pisa made for this type and level (one yes): the sentence for
        the model. RAIL: never after outside text (this turn's or the talk's), never without Pisa's own
        proposal of it, never on a "no" anywhere; the yes is to the proposal itself (their words say "stop
        asking", or Pisa's last reply put it to them); the evidence still holds now."""
        if type_ not in rules.TYPES or level not in rules.LADDER:
            return "Not changed: unknown type or level."
        if outside:
            return "Not changed: a level changes only on the user's own words, never after outside text."
        if not rules.accepts(words):
            return "Not changed: the user's words this turn are no yes to your proposal. Ask it in one line."
        now = self._clock()
        async with self._pool.connection() as conn:
            row = (await self._rows(conn))[type_]
            actions = (await self.outcomes(conn))[type_]
            last_stop = await self._last_stop(conn)
            last = await (
                await conn.execute(
                    "SELECT content FROM messages WHERE role = 'assistant' AND created_at >= %s "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (now - PROPOSAL_ASKED,),
                )
            ).fetchone()
        current, since, proposed, proposed_at = row
        if proposed != level or proposed_at is None or now - proposed_at > self.thresholds.proposal_valid:
            return (
                "Not changed: Pisa never proposed this level for this type (only the "
                "Sunday card's proposals)."
            )
        if not (rules.binds(words) or (last is not None and rules.proposed_in(str(last[0])))):
            return (
                "Not changed: their yes isn't clearly to this proposal. Ask it in one line, naming what Pisa "
                "would stop "
                "asking."
            )
        found = self._evidence(type_, current, since, actions, last_stop)
        if found is None or found["level"] != level:
            return "Not changed: the evidence no longer holds (an undo, or too few kept). Say so."
        await self._set(type_, level, "voice", lang)
        return f"Changed: {level_line(type_, level, lang)}"

    async def ask_signless(self, type_: str, lang: str) -> dict[str, Any]:
        """The app's "chiedimelo di nuovo": back to ask, no signature."""
        if type_ not in rules.TYPES:
            raise PisaError("not_found", "There is no such action type.", "Check the type")
        await self.lower(type_, lang)
        return next(i for i in (await self.listed())["types"] if i["type"] == type_)

    async def ask_again(self, type_: str, words: str, *, outside: bool, lang: str) -> str:
        """Their words take a kind back to `ask` (always allowed, no Face ID)."""
        if outside or not rules.asks_again(words):
            return 'Not changed: only their own words this turn ("chiedimelo di nuovo") take a level back.'
        if type_ not in rules.TYPES:
            return "Not changed: unknown type."
        await self.lower(type_, lang)
        return f"Changed: {level_line(type_, 'ask', lang)}"

    async def lower(self, type_: str, lang: str) -> None:
        """Back to `ask` (always allowed)."""
        if type_ in rules.TYPES:
            await self._set(type_, "ask", "voice", lang)

    def _check(self, type_: str, level: str) -> None:
        if type_ not in rules.TYPES:
            raise PisaError("not_found", "There is no such action type.", "Check the type")
        if level not in rules.LADDER:
            raise _invalid("level must be ask, tell, silent or ledger.")
        if type_ in rules.LOCKED and level != "ask":
            raise _invalid("A send to other people stays at ask.")

    async def _set(self, type_: str, level: str, by: str, lang: str) -> None:
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            previous = (await self._rows(conn))[type_][0]
            await conn.execute(
                "UPDATE autonomy_levels SET level = %s, since = %s, changed_by = %s, proposed_level = NULL, "
                "proposed_at = NULL WHERE type = %s",
                (level, now, "voice" if by == "voice" else "signed", type_),
            )
            if unlock_rules.promoted(previous, level):  # announced once, on Sunday
                await conn.execute(
                    "INSERT INTO autonomy_unlocks (id, type, from_level, to_level, at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (uuid.uuid4(), type_, previous, level, now),
                )
            provenance = "pisa_proposal" if by == "voice" and level != "ask" else "owner_words"
            await decision_log.store(
                conn,
                [
                    Decision(
                        "act",
                        "autonomy_level",
                        provenance,
                        ref=f"{type_}:{previous}:{level}",
                        purpose=level_line(type_, level, lang),
                        outcome="done",
                    )
                ],
                at=now,
                trace_id=None,
                device_id=None,
            )
        _log.info("autonomy_level_set", type=type_, level=level, by=by)
