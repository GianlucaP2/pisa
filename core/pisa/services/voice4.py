"""`pisa.domain.voice4`: the CEO, spoken.

- **Il consiglio** (`consiglio`, off by default): offered **only as a part of the knock's reading** on Sunday
  (`consiglio_part`, once a week), started by their "consiglio" right after that reading, spoken by code in
  four parts from Pisa's records (`pisa_ideas`, `pisa_bets`, `pisa_desk_dossiers`, `pisa_desk_windows`,
  `pisa_idea_cemetery`); its one decision is a `pisa_desk_decisions` row (domain `consiglio`, question
  `consiglio:<idea id>`, Pisa's p the idea's own `p_keep`), their p kept by voice3's p path; `v4_resolve`
  settles it by the idea's fate. "Avanti" moves on, "basta" stops it, a fixed end 15 minutes after they start.
- **La prova generale** (`prova`, off by default): offered as a knock part on Sunday, or at il consiglio's
  close; next week's collisions from **their own** calendar and tasks only (no shared calendar, no invite, no
  task from a mail: `services.foreign`'s predicate), one a day by the Ghost Run's own finder, spoken one at a
  time. A collision Pisa can fix alone is asked as a `ghost_fix` ritual ask (the Ghost Run's approval path:
  their yes moves their own event through `ActionsService.move_on_yes`, stored as approved, with its Undo);
  nothing changes without their yes.
- Logs carry ids and counts only. Nothing here enters a prompt.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import rituals as ritual_rules
from pisa.domain import voice1 as voice1_rules
from pisa.domain import voice4 as rules
from pisa.domain.owner import Owner
from pisa.domain.schedule import on_sunday
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger
from pisa.services.foreign import EVENTS, FOREIGN_EVENT, FOREIGN_TASK
from pisa.services.rituals import ritual_on
from pisa.services.voice3 import settle_by_idea

_log = get_logger("pisa.voice4")
WEEK = timedelta(days=7)
FIX_KEPT = timedelta(hours=20)  # a collision's question lapses with the Ghost Run's own rule
# their own calendars and tasks only: never a shared or subscribed calendar, an invite, an event
# Gmail made, one Pisa made after outside text, a task made from a mail. Constant SQL, no input.
OWN_EVENTS = (
    "SELECT e.id, e.calendar_id, e.title, e.starts_at, e.ends_at, e.place_id::text, p.name, "  # noqa: S608
    f"{FOREIGN_EVENT} FROM {EVENTS} LEFT JOIN places p ON p.id = e.place_id "
    "WHERE NOT e.all_day AND e.status <> 'cancelled' AND coalesce(e.accepted, true) "
    "AND e.starts_at < %s AND e.ends_at > %s ORDER BY e.starts_at, e.id"
)
OWN_TASKS = (
    "SELECT t.id::text, t.title, t.due_at, t.due_kind, t.duration_min, t.expected_from, t.expected_until, "  # noqa: S608
    f"t.place_id::text, p.name FROM tasks t LEFT JOIN places p ON p.id = t.place_id WHERE t.status = 'open' "
    f"AND t.priority < 3 AND NOT {FOREIGN_TASK} AND t.deleted_at IS NULL AND ((t.expected_from >= %s AND "
    "t.expected_from < %s) OR (t.due_at >= %s AND t.due_at < %s)) ORDER BY t.due_at, t.id"
)


@dataclass
class Reply:
    """What Pisa says by code; the path it opens; Pisa's own question (their p), a collision's ask."""

    said: str
    opens: tuple[str, str] | None = None  # ("idea", id): il consiglio's decision; ("prova_after", "")
    p_ref: str | None = None  # "decision:<id>": their bare p answers it (voice3's path)
    asks: bool = False
    ask: tuple[uuid.UUID, str] | None = None  # a collision's ghost_fix ask: (id, its question)


class CeoVoice:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        night: Any = None,  # NightVoice: the gate, the speakable ideas, the dead ones, outside lineage
        rehearsal: Any = None,  # DayRehearsal: travel times between their places (the Ghost Run's)
        names: Callable[[AsyncConnection], Awaitable[Sequence[str]]]
        | None = None,  # the night's filter's names
    ) -> None:
        self._names = names
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._night = night
        self._rehearsal = rehearsal
        self._opened: dict[uuid.UUID, tuple[Any, str, str]] = {}  # device → (Pisa's turn, path, ref)
        self._talks: dict[uuid.UUID, tuple[str, uuid.UUID, datetime]] = {}  # device → (kind, id, end)
        self._last: dict[uuid.UUID, Any] = {}  # device → the latest turn this service answered
        self._asks: dict[uuid.UUID, uuid.UUID] = {}  # device → la prova's latest collision question
        # la prova generale's collisions as spoken (its fixes by collision), kept while it runs
        self._fixes: dict[tuple[date, str, tuple[tuple[str, str], ...]], ritual_rules.Fix] = {}
        self._said_prova: dict[uuid.UUID, list[rules.Collision]] = {}

    def _today(self, now: datetime | None = None) -> date:
        return (now or self._clock()).astimezone(self._owner.zone).date()

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {"v4_resolve": self.resolve}

    # --- the paths Pisa's own last thing opened ------------------------------------------------------------

    def opened(self, device_id: uuid.UUID, turn: Any) -> tuple[str, str] | None:
        got = self._opened.get(device_id)
        return (got[1], got[2]) if got is not None and turn is not None and got[0] is turn else None

    def mark(self, device_id: uuid.UUID, turn: Any, opens: tuple[str, str] | None) -> None:
        self._last[device_id] = turn
        if opens is None:
            self._opened.pop(device_id, None)
        else:
            self._opened[device_id] = (turn, *opens)

    def said_last(self, device_id: uuid.UUID, turn: Any) -> bool:
        """This device's latest turn is one this service answered (its own question, if it asked one)."""
        return turn is not None and self._last.get(device_id) is turn

    def asked(self, device_id: uuid.UUID, ask_id: uuid.UUID) -> None:
        self._asks[device_id] = ask_id

    def answered(self, device_id: uuid.UUID, ask_id: uuid.UUID, turn: Any) -> None:
        """Their bare yes or no (the Ghost Run's path) answered la prova's own collision question: that turn
        keeps la prova's talk ("avanti" next)."""
        if self.prova_ask(device_id, ask_id):
            self._last[device_id] = turn

    async def drop(self, device_id: uuid.UUID) -> None:
        """Another path answered since this service's last turn: the talk ends now (its row too)."""
        talk = self._talks.pop(device_id, None)
        self._asks.pop(device_id, None)
        if talk is None:
            return
        kind, rid, ends = talk
        async with self._pool.connection() as conn:
            await self._end(
                conn, "pisa_prove" if kind == "prova" else "pisa_consigli", rid, min(self._clock(), ends)
            )
        _log.info("voice4_talk_dropped", kind=kind)

    def prova_ask(self, device_id: uuid.UUID, ask_id: uuid.UUID) -> bool:
        """The open ritual ask is la prova generale's own collision question on this device's talk."""
        return self.talking(device_id) == "prova" and self._asks.get(device_id) == ask_id

    def talking(self, device_id: uuid.UUID) -> str | None:
        """Il consiglio or la prova generale running on this device's talk (its kind), else None."""
        talk = self._talks.get(device_id)
        return talk[0] if talk is not None else None

    async def _switch(self, conn: AsyncConnection, name: str) -> bool:
        return await ritual_on(conn, name)

    async def _gate(self, conn: AsyncConnection) -> bool:
        return self._night is not None and bool(await self._night.gate(conn))

    async def heard(
        self,
        device_id: uuid.UUID,
        kind: rules.Heard,
        lang: str,
        before: Any,
        *,
        after_reading: bool = False,
    ) -> Reply | None:
        """Their closed phrase, answered by code, or None (the model's): each only on its path."""
        if kind == "consiglio":
            return await self.consiglio_start(device_id, lang) if after_reading else None
        if kind == "prova":
            mine = self.opened(device_id, before)
            if after_reading or (mine is not None and mine[0] == "prova_after"):
                return await self.prova_start(device_id, lang)
            return None
        if kind in ("avanti", "basta"):
            talk = self.talking(device_id)
            if talk == "consiglio":
                return await self.consiglio_next(device_id, kind, lang)
            if talk == "prova":
                return await self.prova_next(device_id, kind, lang)
        return None

    # --- il consiglio ------------------------------------------------------------------------------------

    async def board(self, conn: AsyncConnection, now: datetime, lang: str) -> rules.Board:
        """This week's board from Pisa's records: every number il consiglio says is a count of these ids."""
        since = now - WEEK
        night = self._night
        live_ideas: list[rules.Live] = []
        for idea, words in await night.ideas(conn, lang) if night is not None else []:
            live_ideas.append(rules.Live(idea.id, words, await self._p_keep(conn, idea.id)))
        live = tuple(live_ideas)
        bets = await self._ids(conn, "SELECT id FROM pisa_bets WHERE state = 'open' ORDER BY id")
        dossiers = await self._ids(
            conn, "SELECT id FROM pisa_desk_dossiers WHERE created_at >= %s ORDER BY id", since
        )
        windows = await self._ids(
            conn,
            "SELECT id FROM pisa_desk_windows WHERE passed AND first_seen >= %s ORDER BY id",
            since.astimezone(self._owner.zone).date(),
        )
        week_dead = set(
            await self._ids(conn, "SELECT idea_id FROM pisa_idea_cemetery WHERE killed_at >= %s", since)
        )
        killed: tuple[tuple[str, str], ...] = ()
        if night is not None and week_dead:
            killed = tuple(
                (item.id, item.text.rstrip("."))
                for item in await night.dead(conn, lang)
                if item.id in week_dead
            )
        return rules.Board(live, bets, dossiers, windows, killed)

    async def _ids(self, conn: AsyncConnection, sql: Any, *args: Any) -> tuple[str, ...]:
        return tuple(str(r[0]) for r in await (await conn.execute(sql, args)).fetchall())

    async def _p_keep(self, conn: AsyncConnection, idea_id: str) -> float | None:
        row = await (await conn.execute("SELECT p_keep FROM pisa_ideas WHERE id = %s", (idea_id,))).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def consiglio_part(
        self, conn: AsyncConnection, now: datetime, lang: str, *, speaking: bool = False
    ) -> voice1_rules.Part | None:
        """The knock's part (the one way il consiglio is offered): Sunday, their `consiglio` on, the night's
        gate open, none this week yet (or the one offered today), and the board has something to say."""
        day = self._today(now)
        if not on_sunday(day) or not await self._switch(conn, "consiglio"):
            return None
        if not await self._gate(conn):
            return None
        week = rules.week_of(day)
        if speaking:  # one offer a week: the count and the offer's row under one lock
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa_consigli'))")
        rows = await (
            await conn.execute("SELECT offered_at FROM pisa_consigli WHERE week = %s", (week,))
        ).fetchall()
        start, _ = local_day_bounds(day, self._owner.tz)
        if rows and not any(r[0] >= start for r in rows):
            return None  # once a week
        if (await self.board(conn, now, lang)).empty():
            return None
        if speaking and not rows:
            await conn.execute(
                "INSERT INTO pisa_consigli (id, week, offered_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (week) DO NOTHING",
                (uuid.uuid4(), week, now),
            )
            _log.info("voice4_consiglio_offered")
        return voice1_rules.Part("consiglio", rules.consiglio_offer(lang), "pisa_consigli", week.isoformat())

    async def consiglio_start(self, device_id: uuid.UUID, lang: str) -> Reply | None:
        """Their "consiglio" right after the knock's reading that offered it: the fixed end is set now; the
        talk is this device's; the first part is said."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await self._close_stale(conn, now)
            row = await (
                await conn.execute(
                    "UPDATE pisa_consigli SET state = 'talking', started_at = %s, ends_at = %s WHERE id = "
                    "(SELECT id FROM pisa_consigli WHERE state = 'offered' AND offered_at >= %s "
                    "ORDER BY offered_at DESC LIMIT 1) AND state = 'offered' RETURNING id, ends_at",
                    (now, now + timedelta(minutes=rules.MINUTES), now - rules.START_WITHIN),
                )
            ).fetchone()
            if row is None:
                return None
            cid, ends = row
            self._talks[device_id] = ("consiglio", cid, ends)
            said, reply = await self._consiglio_say(conn, cid, lang, now)
        opening = rules.word(rules.ON, lang, ends=rules.local_hm(ends.astimezone(self._owner.zone)))
        _log.info("voice4_consiglio_started", consiglio_id=str(cid))
        reply.said = f"{opening} {said}"
        return reply

    async def consiglio_next(self, device_id: uuid.UUID, kind: rules.Heard, lang: str) -> Reply | None:
        talk = self._talks.get(device_id)
        if talk is None:
            return None
        _, cid, ends = talk
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT part FROM pisa_consigli WHERE id = %s AND state = 'talking' FOR UPDATE", (cid,)
                )
            ).fetchone()
            if row is None:
                self._talks.pop(device_id, None)
                return None
            if now >= ends or kind == "basta":
                await self._end(conn, "pisa_consigli", cid, min(now, ends))
                self._talks.pop(device_id, None)
                _log.info("voice4_consiglio_ended", consiglio_id=str(cid), timed_out=now >= ends)
                return Reply(rules.word(rules.TIMED_OUT if now >= ends else rules.STOPPED, lang))
            said, reply = await self._consiglio_say(conn, cid, lang, now)
        reply.said = said
        return reply

    async def _consiglio_say(
        self, conn: AsyncConnection, cid: uuid.UUID, lang: str, now: datetime
    ) -> tuple[str, Reply]:
        """The next part, from the records now; the decision part ends il consiglio and asks their p."""
        row = await (
            await conn.execute("SELECT part, refs FROM pisa_consigli WHERE id = %s", (cid,))
        ).fetchone()
        at = int(row[0]) if row else 0
        refs = list(row[1] or []) if row else []
        board = await self.board(conn, now, lang)
        part = rules.PARTS[min(at, len(rules.PARTS) - 1)]
        claims = rules.board_part(board, part, lang)
        refs += [{"table": c.table, "id": i} for c in claims for i in c.ids]
        said = rules.board_said(claims)
        reply = Reply(said)
        if part != "decision":
            await conn.execute(
                "UPDATE pisa_consigli SET part = %s, refs = %s WHERE id = %s", (at + 1, Jsonb(refs), cid)
            )
            reply.said = f"{said} {rules.word(rules.NEXT, lang)}"
            return reply.said, reply
        best = board.decision()
        decision = None
        if best is not None and best.p is not None:
            decision = uuid.uuid4()
            await conn.execute(
                "INSERT INTO pisa_desk_decisions (id, dossier_id, domain, question, pisa_p, resolves_by, "
                "created_at) "
                "VALUES (%s, NULL, 'consiglio', %s, %s, %s, %s)",
                (decision, f"consiglio:{best.id}", min(0.99, max(0.01, round(best.p, 2))),
                 self._today(now) + rules.DECISION_RESOLVES, now),
            )  # fmt: skip
            reply.p_ref, reply.asks, reply.opens = f"decision:{decision}", True, ("idea", best.id)
        await conn.execute(
            "UPDATE pisa_consigli SET part = %s, refs = %s, idea_id = %s, decision_id = %s, state = 'ended', "
            "ended_at = %s WHERE id = %s",
            (at + 1, Jsonb(refs), best.id if decision and best else None, decision, now, cid),
        )
        self._talks = {d: t for d, t in self._talks.items() if t[1] != cid}
        _log.info("voice4_consiglio_decision", consiglio_id=str(cid), decision_id=str(decision))
        if decision is None:  # nothing to ask: il consiglio closes, and la prova generale may follow
            reply.said = f"{said} {rules.word(rules.CLOSED, lang)}"
            if await self._switch(conn, "prova"):
                reply.said += " " + rules.word(rules.PROVA_AFTER, lang)
                reply.opens = ("prova_after", "")
        return reply.said, reply

    async def _end(self, conn: AsyncConnection, table: str, rid: uuid.UUID, at: datetime) -> None:
        sql = "UPDATE pisa_consigli SET state = 'ended', ended_at = %s WHERE id = %s"
        if table == "pisa_prove":
            sql = "UPDATE pisa_prove SET state = 'ended', ended_at = %s WHERE id = %s"
        await conn.execute(sql, (at, rid))  # type: ignore[arg-type]

    async def _close_stale(self, conn: AsyncConnection, now: datetime) -> None:
        """A talk nobody closed ends at its fixed end."""
        for sql in (
            "UPDATE pisa_consigli SET state = 'ended', ended_at = ends_at "
            "WHERE state = 'talking' AND ends_at <= %s",
            "UPDATE pisa_prove SET state = 'ended', ended_at = ends_at WHERE state = 'talking' "
            "AND ends_at <= %s",
        ):
            await conn.execute(sql, (now,))  # type: ignore[arg-type]

    # --- la prova generale -------------------------------------------------------------------------------

    async def collisions(
        self, conn: AsyncConnection, now: datetime, lang: str, *, travel: bool = False
    ) -> list[rules.Collision]:
        """Next week's collisions, one a day at most (the Ghost Run's finder), from their own data only. A fix
        whose slot meets anything else of their calendar (an invite included) is dropped: only said."""
        out: list[rules.Collision] = []
        for day in rules.next_week(self._today(now)):
            own, others, due = await self._day(conn, day)
            if not own and not due:
                continue
            minutes: dict[tuple[str, str], int] = {}
            if travel and self._rehearsal is not None:
                try:
                    minutes, _ = await self._rehearsal.travel(own)
                except Exception as exc:  # a provider never stops the rehearsal
                    _log.info("voice4_travel_skipped", cause=exc.__class__.__name__)
            problem = ritual_rules.find_problem(
                own, travel=minutes, due=due, rain_at=None, bike=False, lang=lang
            )
            if problem is None:
                continue
            fix, question = problem.fix, problem.question
            if fix is not None and any(fix.start < b.end and b.start < fix.end for b in others):
                fix, question = None, None
            refs = _refs(problem, own, due)
            out.append(rules.Collision(day, problem.kind, problem.line, refs, question if fix else None))
            if fix is not None:
                self._fixes[(day, problem.kind, refs)] = fix
        return out

    async def _day(
        self, conn: AsyncConnection, day: date
    ) -> tuple[list[ritual_rules.Block], list[ritual_rules.Block], list[ritual_rules.DueTask]]:
        """Their own blocks that day (movable: they are theirs alone), everything else in their calendars as
        busy (never said, never moved), and their own tasks due that day."""
        tz = self._owner.zone
        lo, hi = local_day_bounds(day, self._owner.tz)
        own: list[ritual_rules.Block] = []
        others: list[ritual_rules.Block] = []
        seen: set[str] = set()
        for eid, cal, title, starts, ends, place_id, place, foreign in await (
            await conn.execute(OWN_EVENTS, (hi, lo))
        ).fetchall():
            if eid in seen:
                continue
            seen.add(eid)
            block = ritual_rules.Block(
                "event", eid, str(title or ""), starts.astimezone(tz), ends.astimezone(tz), place_id, place,
                movable=not foreign, calendar_id=cal,
            )  # fmt: skip
            (others if foreign or not str(title or "").strip() else own).append(block)
        due: list[ritual_rules.DueTask] = []
        for tid, title, due_at, due_kind, minutes, start, until, place_id, place in await (
            await conn.execute(OWN_TASKS, (lo, hi, lo, hi))
        ).fetchall():
            length = timedelta(minutes=minutes or ritual_rules.TASK_BLOCK_MIN)
            if start is not None and lo <= start < hi:
                end = until if until is not None and until > start else start + length
                own.append(ritual_rules.Block("task", tid, title, start.astimezone(tz), end.astimezone(tz),
                                              place_id, place, movable=True))  # fmt: skip
            elif due_at is not None:
                deadline = hi if due_kind == "date" else due_at
                due.append(ritual_rules.DueTask(tid, title, deadline.astimezone(tz),
                                                minutes or ritual_rules.TASK_BLOCK_MIN))  # fmt: skip
        return own, others, due

    async def prova_part(
        self, conn: AsyncConnection, now: datetime, lang: str, *, speaking: bool = False
    ) -> voice1_rules.Part | None:
        """The knock's part: Sunday, their `prova` on, none this week yet (or today's), and next week has a
        collision in their own data."""
        day = self._today(now)
        if not on_sunday(day) or not await self._switch(conn, "prova"):
            return None
        week = rules.week_of(day)
        if speaking:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa_prove'))")
        rows = await (
            await conn.execute("SELECT offered_at FROM pisa_prove WHERE week = %s", (week,))
        ).fetchall()
        start, _ = local_day_bounds(day, self._owner.tz)
        if rows and not any(r[0] >= start for r in rows):
            return None
        if not await self.collisions(conn, now, lang):
            return None
        if speaking and not rows:
            await self._offer_prova(conn, week, now)
        return voice1_rules.Part("prova", rules.prova_offer(lang), "pisa_prove", week.isoformat())

    async def _offer_prova(self, conn: AsyncConnection, week: date, now: datetime) -> None:
        await conn.execute(
            "INSERT INTO pisa_prove (id, week, offered_at) VALUES (%s, %s, %s) ON CONFLICT (week) DO NOTHING",
            (uuid.uuid4(), week, now),
        )
        _log.info("voice4_prova_offered")

    async def prova_start(self, device_id: uuid.UUID, lang: str) -> Reply | None:
        """Their "prova generale" right after the knock's reading that offered it, or il consiglio's close."""
        now = self._clock()
        week = rules.week_of(self._today(now))
        async with self._pool.connection() as conn, conn.transaction():
            await self._close_stale(conn, now)
            if not await self._switch(conn, "prova") or not on_sunday(self._today(now)):
                return None
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa_prove'))")
            offered = await (
                await conn.execute(
                    "SELECT id FROM pisa_prove WHERE state = 'offered' AND offered_at >= %s "
                    "ORDER BY offered_at DESC LIMIT 1",
                    (now - rules.START_WITHIN,),
                )
            ).fetchone()
            if offered is None:
                any_this_week = await (
                    await conn.execute("SELECT 1 FROM pisa_prove WHERE week = %s", (week,))
                ).fetchone()
                if any_this_week is not None:
                    return None  # once a week
                await self._offer_prova(conn, week, now)  # il consiglio's close offered it
                offered = await (
                    await conn.execute(
                        "SELECT id FROM pisa_prove WHERE week = %s AND state = 'offered'", (week,)
                    )
                ).fetchone()
                if offered is None:
                    return None
            pid = offered[0]
            claimed = await (
                await conn.execute(
                    "UPDATE pisa_prove SET state = 'talking' WHERE id = %s AND state = "
                    "'offered' RETURNING id",
                    (pid,),
                )
            ).fetchone()
            if claimed is None:
                return None  # someone else started it
            found = await self.collisions(conn, now, lang, travel=True)
            items = [
                {"day": c.day.isoformat(), "kind": c.kind, "refs": [{"table": t, "id": i} for t, i in c.refs],
                 "ask_id": None}
                for c in found
            ]  # fmt: skip
            ends = now + timedelta(minutes=rules.MINUTES)
            await conn.execute(
                "UPDATE pisa_prove SET state = 'talking', started_at = %s, ends_at = %s, items = %s "
                "WHERE id = %s",
                (now, ends, Jsonb(items), pid),
            )
            if not found:
                await self._end(conn, "pisa_prove", pid, now)
                return Reply(rules.word(rules.PROVA_NONE, lang))
            self._talks[device_id] = ("prova", pid, ends)
            self._said_prova[pid] = found
            reply = await self._collision(conn, pid, 0, found, lang, now)
        _log.info("voice4_prova_started", prova_id=str(pid), collisions=len(found))
        reply.said = f"{rules.prova_start(len(found), lang)} {reply.said}"
        return reply

    async def _collision(
        self,
        conn: AsyncConnection,
        pid: uuid.UUID,
        at: int,
        found: list[rules.Collision],
        lang: str,
        now: datetime,
    ) -> Reply:
        """Collision `at`, spoken; one Pisa can fix alone becomes a `ghost_fix` ask (their yes, the Ghost
        Run's path); the last one ends la prova generale."""
        c = found[at]
        said = rules.collision_words(c, lang)
        reply = Reply(said)
        fix = self._fixes.get((c.day, c.kind, c.refs))
        if fix is not None and c.question:
            ask_id = uuid.uuid4()
            expires = min(fix.start, fix.was_start or fix.start, now + FIX_KEPT)
            await conn.execute(
                "INSERT INTO ritual_asks (id, kind, question, payload, created_at, expires_at) "
                "VALUES (%s, 'ghost_fix', %s, %s, %s, %s)",
                (ask_id, c.question, Jsonb(fix.json()), now, expires),
            )
            await conn.execute(
                "UPDATE pisa_prove SET items = jsonb_set(items, %s, to_jsonb(%s::text)) WHERE id = %s",
                ([str(at), "ask_id"], str(ask_id), pid),
            )
            reply.ask, reply.asks = (ask_id, c.question), True
        last = at + 1 >= len(found)
        await conn.execute("UPDATE pisa_prove SET at = %s WHERE id = %s", (at + 1, pid))
        if last:
            await self._end(conn, "pisa_prove", pid, now)
            self._talks = {d: t for d, t in self._talks.items() if t[1] != pid}
            if not reply.asks:
                reply.said = f"{said} {rules.word(rules.PROVA_END, lang)}"
        elif not reply.asks:
            reply.said = f"{said} {rules.word(rules.PROVA_NEXT, lang)}"
        return reply

    async def prova_next(self, device_id: uuid.UUID, kind: rules.Heard, lang: str) -> Reply | None:
        talk = self._talks.get(device_id)
        if talk is None:
            return None
        _, pid, ends = talk
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT at FROM pisa_prove WHERE id = %s AND state = 'talking' FOR UPDATE", (pid,)
                )
            ).fetchone()
            found = self._said_prova.get(pid)
            if row is None or found is None:
                self._talks.pop(device_id, None)
                return None
            await self._lapse(conn, pid)  # "avanti" over an open question: that question is let go
            if now >= ends or kind == "basta" or int(row[0]) >= len(found):
                await self._end(conn, "pisa_prove", pid, min(now, ends))
                self._talks.pop(device_id, None)
                _log.info("voice4_prova_ended", prova_id=str(pid), timed_out=now >= ends)
                return Reply(rules.word(rules.PROVA_STOPPED if kind == "basta" else rules.PROVA_END, lang))
            return await self._collision(conn, pid, int(row[0]), found, lang, now)

    async def _lapse(self, conn: AsyncConnection, pid: uuid.UUID) -> None:
        await conn.execute(
            "UPDATE ritual_asks SET status = 'lapsed' WHERE status = 'open' AND kind = 'ghost_fix' "
            "AND id::text IN "
            "(SELECT x->>'ask_id' FROM pisa_prove p, jsonb_array_elements(p.items) x WHERE p.id = %s)",
            (pid,),
        )

    # --- the night: il consiglio's decision settled ------------------------------------------------------

    async def resolve(self, night: date) -> int:
        """Il consiglio's decisions by the idea's fate in Pisa's records (kept: true; killed or past its day:
        false); the journal's step (d4) scores them. Talks nobody closed end at their fixed end."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            done = await settle_by_idea(conn, "pisa_consigli", night, now)
            await self._close_stale(conn, now)
        _log.info("voice4_resolved", resolved=done)
        return done


def _refs(
    problem: ritual_rules.Problem, blocks: list[ritual_rules.Block], due: list[ritual_rules.DueTask]
) -> tuple[tuple[str, str], ...]:
    """The records a collision is about (the Ghost Run's own order: the first clash, else the first short
    transfer, else the first task with no block): (table, id)."""

    def ref(b: ritual_rules.Block) -> tuple[str, str]:
        return ("calendar_events" if b.kind == "event" else "tasks", b.ref)

    timed = sorted(blocks, key=lambda b: b.start)
    pairs = list(pairwise(timed))
    if problem.kind == "clash":
        a, b = next(((a, b) for a, b in pairs if b.start < a.end), (None, None))
        return (ref(a), ref(b)) if a and b else ()
    if problem.kind == "transfer":
        mover = problem.fix.ref if problem.fix else None
        a, b = next(((a, b) for a, b in pairs if b.ref == mover), pairs[0] if pairs else (None, None))
        return (ref(a), ref(b)) if a and b else ()
    blocked = {b.ref for b in blocks if b.kind == "task"}
    task = next((t for t in due if t.task_id not in blocked), None)
    return (("tasks", task.task_id),) if task else ()
