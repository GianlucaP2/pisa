"""The morning sheet and "Dammi tutto", Pisa's diary and the open-endedness meter.

- **The night's last step** (`s1_sheet`, after the frame's and the other engines' steps, under the same stop):
  the sheet is built from the rows the night wrote, **code only** (no model call, no search: €0, nothing
  outward), and stored in `pisa_sheets`. Every shown line names its row and the check it passed
  (`domain/stanotte.Line`); an idea whose evidence fails stays off. Pisa's diary lines and the month's meter
  are written (shown in).
- **The gate** (`stanotte`, off by default): code counts the nights built in shadow and opens it once (7
  nights, at least one idea made, 100% of the lines shown passing, rechecked on their rows). They can switch
  it off in Settings; code never switches it back on.
- **Sent** only with the gate open, and only as part of what they already get: the first automatic morning
  briefing's notice carries the sheet (`ritual: stanotte`, `sheet_id`), and the Sunday poster gains a part.
- **Read** by no turn, intake, profile, grounding or style path: only this file names `pisa_sheets`,
  `pisa_diary` and `pisa_meter` (corollary discharge). Logs carry ids and counts only.

The curiosity's tables (0079: `pisa_horizon`, `pisa_unknowns`) and the desk's (0080: `pisa_desk_windows`,
`pisa_desk_arbitrage`, `pisa_desk_money`) are read when their tables exist; else those parts are left out.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from typing import Any, LiteralString

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import stanotte as rules
from pisa.domain.lexicon.advice import NOT_ADVICE_MATCH
from pisa.domain.owner import Owner
from pisa.domain.sheet import RitualSheetData
from pisa.infra import ledger
from pisa.logging import get_logger
from pisa.services.rituals import ritual_on, stanotte_said

_log = get_logger("pisa.stanotte")
Switch = Callable[[dict[str, Any]], Awaitable[Any]]
WINDOW_AHEAD = timedelta(days=90)  # the window calendar's horizon read for the sheet
KILLED_DAYS = 7  # "Dammi tutto" shows the week's dead ideas; the sheet the newest three


class StanotteService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        switch: Switch | None = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._switch = switch  # RitualsService.put: code opens the gate through the setting

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {"s1_sheet": self.build}

    # --- the night: build and store (shadow) ------------------------------------------------------------

    async def build(self, night: date) -> int:
        """The night's sheet, stored, never sent from here. Returns the lines shown."""
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            counts = await self._counts(conn, night)
            ideas = await self._ideas(conn, lang)
            killed = await self._killed(conn, night)
            windows = await self._windows(conn, night)
            gaps = await self._gaps(conn, night)
            questions = await self._questions(conn, night)
        story = rules.story(lang, counts)
        diary = rules.diary(lang, counts)
        built = rules.build(
            lang, night, ideas=ideas, killed=killed, windows=windows, gaps=gaps, questions=questions,
            story=story, diary=diary, counts=counts,
        )  # fmt: skip
        RitualSheetData.model_validate(built.data)  # the contract's limits, before anything is stored
        RitualSheetData.model_validate(built.everything)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO pisa_sheets (night, built_at, data, everything, lines, shown, passed, idea_id, "
                "story, "
                "gate) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (night) DO NOTHING",
                (
                    night, now, Jsonb(built.data), Jsonb(built.everything),
                    Jsonb([line.json() for line in built.lines]), built.shown, built.passed,
                    uuid.UUID(built.idea_id) if built.idea_id else None, story,
                    "open" if await self.open(conn) else "shadow",
                ),
            )  # fmt: skip
            await conn.execute("DELETE FROM pisa_diary WHERE night = %s", (night,))
            for line in diary:
                await conn.execute(
                    "INSERT INTO pisa_diary (id, night, line, refs, created_at) VALUES (%s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4(),
                        night,
                        line,
                        Jsonb([{"table": "pisa_nights", "id": night.isoformat()}]),
                        now,
                    ),
                )
            await self._meter(conn, night)
        await self._gate(night)
        _log.info("sheet_built", shown=built.shown, passed=built.passed, idea=built.idea_id is not None)
        return built.shown

    async def _counts(self, conn: AsyncConnection, night: date) -> dict[str, float]:
        """Pisa's own records of tonight, counted (never words of theirs)."""
        started = await (
            await conn.execute("SELECT started_at, brier FROM pisa_nights WHERE night = %s", (night,))
        ).fetchone()
        since = started[0] if started else self._clock() - timedelta(hours=12)

        async def one(sql: LiteralString, *args: Any) -> int:
            row = await (await conn.execute(sql, args)).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        out: dict[str, float] = {
            "problems": await one("SELECT count(*) FROM pisa_idea_problems WHERE prepared_on = %s", night),
            "incubated": await one("SELECT count(*) FROM pisa_ideas WHERE night = %s", night),
            "validated": await one(
                "SELECT count(*) FROM pisa_ideas WHERE night = %s AND kills_at IS NOT NULL", night
            ),
            "ready": await one("SELECT count(*) FROM pisa_ideas WHERE night = %s AND state = 'ready'", night),
            "killed": await one("SELECT count(*) FROM pisa_idea_cemetery WHERE killed_at >= %s", since),
            "searches": await one(
                "SELECT count(*) FROM pisa_night_searches WHERE night = %s AND sent", night
            ),
            "replayed": await one("SELECT count(*) FROM pisa_night_replays WHERE night = %s", night),
            "replay_failed": await one(
                "SELECT count(*) FROM pisa_night_replays WHERE night = %s AND NOT passed", night
            ),
            "predicted": await one("SELECT count(*) FROM pisa_night_predictions WHERE night = %s", night),
            "spent_eur": float(await ledger.spent_since(conn, since, "night")),
        }
        if started and started[1] is not None:
            out["brier"] = float(started[1])
        if await _exists(conn, "pisa_unknown_changes"):
            out["answered"] = await one(
                "SELECT count(*) FROM pisa_unknown_changes WHERE night = %s AND change = 'answered'", night
            )
        return out

    async def _evidence(self, conn: AsyncConnection, iid: uuid.UUID) -> tuple[rules.Evidence, ...]:
        rows = await (
            await conn.execute(
                "SELECT id, rung, claim, check_kind, passed, simulated, url, final_url, number, run "
                "FROM pisa_idea_evidence WHERE idea_id = %s ORDER BY checked_at, id",
                (iid,),
            )
        ).fetchall()
        return tuple(_ev(r) for r in rows)

    async def _ideas(self, conn: AsyncConnection, lang: str) -> list[rules.Idea]:
        """The ready ideas (stops at `ready`), newest night first, then Pisa's P(they keep it)."""
        rows = await (
            await conn.execute(
                "SELECT id, statement, mechanism, market, kind FROM pisa_ideas WHERE state = 'ready' "
                "ORDER BY night DESC, p_keep DESC NULLS LAST, id LIMIT 8"
            )
        ).fetchall()
        out = []
        for iid, statement, mechanism, market, kind in rows:
            evidence = await self._evidence(conn, iid)
            money = await self._money(conn, iid)
            out.append(
                rules.Idea(
                    str(iid), statement, mechanism, market, kind, _grade(evidence, lang), evidence, money
                )
            )
        return out

    async def _killed(self, conn: AsyncConnection, night: date) -> list[rules.Killed]:
        since = datetime.combine(night - timedelta(days=KILLED_DAYS), time(0), tzinfo=self._owner.zone)
        rows = await (
            await conn.execute(
                "SELECT c.idea_id, i.statement, i.kill_criteria, c.criterion, c.evidence_id FROM "
                "pisa_idea_cemetery c "
                "JOIN pisa_ideas i ON i.id = c.idea_id WHERE c.killed_at >= %s ORDER BY c.killed_at DESC, "
                "c.idea_id "
                "LIMIT 8",
                (since,),
            )
        ).fetchall()
        out = []
        for iid, statement, kills, criterion, eid in rows:
            evidence = None
            if eid is not None:
                row = await (
                    await conn.execute(
                        "SELECT id, rung, claim, check_kind, passed, simulated, url, final_url, number, run "
                        "FROM pisa_idea_evidence WHERE id = %s",
                        (eid,),
                    )
                ).fetchone()
                evidence = _ev(row) if row else None
            kills = kills if isinstance(kills, list) else json.loads(kills or "[]")
            if criterion is not None and 0 <= criterion < len(kills):
                k = kills[criterion]
                metric, op, value = str(k["metric"]), str(k["op"]), float(k["value"])
                out.append(rules.Killed(str(iid), statement, metric, op, value, evidence))
            else:
                out.append(rules.Killed(str(iid), statement, "margin", "<", None, evidence))
        return out

    async def _windows(self, conn: AsyncConnection, night: date) -> list[rules.Window]:
        """The window calendar (0080 `pisa_desk_windows`), when it is there: a window shows only
        with its checked state (`passed`: the date read on its fetched official page)."""
        rows = await _read(
            conn,
            "pisa_desk_windows",
            "SELECT id, title, country, day, url, passed FROM pisa_desk_windows WHERE day >= %s "
            "AND day <= %s ORDER BY day, id LIMIT 8",
            (night, night + WINDOW_AHEAD),
        )
        return [
            rules.Window(str(i), str(t), str(c), d, str(u), p is True)
            for i, t, c, d, u, p in rows
            if isinstance(d, date) and not isinstance(d, datetime) and isinstance(p, bool)
        ]  # a row of the wrong type is left out

    async def _gaps(self, conn: AsyncConnection, night: date) -> list[rules.Gap]:
        """The arbitrage desk (0080 `pisa_desk_arbitrage`), tonight's: its name on its page."""
        rows = await _read(
            conn,
            "pisa_desk_arbitrage",
            "SELECT id, way, has, name, claim, url, passed FROM pisa_desk_arbitrage WHERE night = %s "
            "ORDER BY checked_at DESC, id LIMIT 8",
            (night,),
        )
        return [
            rules.Gap(str(i), str(w), str(h), str(n), str(c), str(u), p is True)
            for i, w, h, n, c, u, p in rows
            if isinstance(p, bool)
        ]

    async def _money(self, conn: AsyncConnection, iid: Any) -> rules.Money | None:
        """The money desk (0080 `pisa_desk_money`) for this idea, when it has a dossier."""
        rows = await _read(
            conn,
            "pisa_desk_money",
            "SELECT d.id, m.stack FROM pisa_desk_money m JOIN pisa_desk_dossiers d ON d.id = m.dossier_id "
            "WHERE d.idea_id = %s",
            (iid,),
        )
        if not rows:
            return None
        did, stack = rows[0]
        stack = stack if isinstance(stack, list) else json.loads(stack)
        ids = [str(s["window"]) for s in stack if s.get("step") == "non_dilutive" and s.get("window")]
        ok = await _read(
            conn,
            "pisa_desk_windows",
            "SELECT id::text FROM pisa_desk_windows WHERE id::text = ANY(%s) AND passed",
            (ids,),
        )
        return rules.Money(str(did), tuple(stack), frozenset(r[0] for r in ok))

    async def _questions(self, conn: AsyncConnection, night: date) -> list[rules.Question]:
        """The horizon scan (0079): a question on one of their dated items, still open; its
        check is that the item is still in their records (and a supported read's link, when there is one)."""
        rows = await _read(
            conn,
            "pisa_horizon",
            "SELECT h.id, h.item, h.question, (SELECT r.url FROM pisa_unknown_reads r WHERE r.unknown_id = "
            "h.unknown_id AND r.supported ORDER BY r.at DESC LIMIT 1) FROM pisa_horizon h LEFT JOIN "
            "pisa_unknowns u "
            "ON u.id = h.unknown_id WHERE h.night >= %s AND (u.id IS NULL OR u.state = 'open') "
            "ORDER BY h.created_at DESC, h.id LIMIT 8",
            (night - timedelta(days=7),),
        )
        out = []
        for hid, item, question, url in rows:
            out.append(rules.Question(str(hid), str(question), await self._still_there(conn, str(item)), url))
        return out

    async def _still_there(self, conn: AsyncConnection, item: str) -> bool:
        kind, _, ref = item.partition(":")
        sql: LiteralString
        if kind == "event":
            cal, _, eid = ref.partition(":")
            sql = (
                "SELECT 1 FROM calendar_events WHERE calendar_id = %s AND id = %s AND status <> 'cancelled' "
                "AND starts_at >= %s"
            )
            args: tuple[Any, ...] = (cal, eid, self._clock())
        elif kind == "task":
            try:
                args = (uuid.UUID(ref),)
            except ValueError:
                return False
            sql = "SELECT 1 FROM tasks WHERE id = %s AND status = 'open'"
        else:
            return False
        return await (await conn.execute(sql, args)).fetchone() is not None

    async def _meter(self, conn: AsyncConnection, night: date) -> None:
        """The Open-Endedness Meter, this month so far (two observers; counts only)."""
        month = night.replace(day=1)
        lo = datetime.combine(month, time(0), tzinfo=self._owner.zone)

        async def one(sql: LiteralString, *args: Any) -> int:
            row = await (await conn.execute(sql, args)).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        liked = await one("SELECT count(*) FROM pisa_ideas WHERE state = 'kept' AND updated_at >= %s", lo)
        answered = 0
        if await _exists(conn, "pisa_unknowns"):
            liked += await one("SELECT count(*) FROM pisa_unknowns WHERE liked AND updated_at >= %s", lo)
            answered = await one(
                "SELECT count(*) FROM pisa_unknown_changes WHERE change = 'answered' AND night >= %s", month
            )
        now = rules.Meter(
            his_liked=liked,
            verified=await one(
                "SELECT count(*) FROM pisa_ideas WHERE state IN ('ready', 'shown', 'kept') AND night >= %s",
                month,
            ),
            map_cells=await one("SELECT count(*) FROM pisa_idea_map WHERE idea_id IS NOT NULL"),
            answered=answered,
        )
        prev = await (
            await conn.execute(
                "SELECT his_liked, verified, map_cells, answered FROM pisa_meter WHERE month = %s",
                ((month - timedelta(days=1)).replace(day=1),),
            )
        ).fetchone()
        flat = rules.flat(now, rules.Meter(*prev) if prev else None)
        await conn.execute(
            "INSERT INTO pisa_meter (month, his_liked, verified, map_cells, answered, flat, computed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (month) DO UPDATE SET his_liked = "
            "excluded.his_liked, "
            "verified = excluded.verified, map_cells = excluded.map_cells, answered = excluded.answered, "
            "flat = excluded.flat, computed_at = excluded.computed_at",
            (month, now.his_liked, now.verified, now.map_cells, now.answered, flat, self._clock()),
        )

    # --- the gate ---------------------------------------------------------------------------------------

    async def open(self, conn: AsyncConnection) -> bool:
        """Sending is allowed: code opened the gate once, and the setting is on (they can switch it off)."""
        if await get_setting(conn, rules.GATE_KEY) is None:
            return False
        return await ritual_on(conn, "stanotte")

    async def recheck(self, conn: AsyncConnection, lines: list[dict[str, Any]]) -> tuple[int, int]:
        """Every shown line's row, read and checked again now: (shown, passed)."""
        passed = 0
        for line in lines:
            passed += await self._line_ok(conn, line)
        return len(lines), passed

    async def _line_ok(self, conn: AsyncConnection, line: dict[str, Any]) -> bool:
        table, ref = line.get("table"), str(line.get("id"))
        try:
            key: Any = uuid.UUID(ref)
        except ValueError:
            return False
        if table == "pisa_idea_evidence":
            row = await (
                await conn.execute(
                    "SELECT id, rung, claim, check_kind, passed, simulated, url, final_url, number, run "
                    "FROM pisa_idea_evidence WHERE id = %s",
                    (key,),
                )
            ).fetchone()
            return row is not None and rules.line_ok(_ev(row))
        if table == "pisa_idea_cemetery":  # the line that killed it, checked again
            got = await (
                await conn.execute(
                    "SELECT e.id, e.rung, e.claim, e.check_kind, e.passed, e.simulated, e.url, e.final_url, "
                    "e.number, e.run FROM pisa_idea_cemetery c JOIN pisa_idea_evidence e ON e.id = "
                    "c.evidence_id WHERE c.idea_id = %s",
                    (key,),
                )
            ).fetchone()
            return got is not None and rules.line_ok(_ev(got))
        if table == "pisa_horizon":  # a cancelled event's (a done task's) question is never sent
            rows = await _read(conn, "pisa_horizon", "SELECT item FROM pisa_horizon WHERE id = %s", (key,))
            return bool(rows) and await self._still_there(conn, str(rows[0][0]))
        args: tuple[Any, ...] = (key,)
        if table == "pisa_desk_windows":
            sql = "SELECT 1 FROM pisa_desk_windows WHERE id = %s AND passed"
        elif table == "pisa_desk_arbitrage":
            sql = "SELECT 1 FROM pisa_desk_arbitrage WHERE id = %s AND passed"
        elif table == "pisa_desk_money":
            sql = "SELECT 1 FROM pisa_desk_money WHERE dossier_id = %s AND stack::text LIKE %s"
            args = (key, f"%{NOT_ADVICE_MATCH}%")
        else:
            return False
        return bool(await _read(conn, str(table), sql, args))

    async def _gate(self, night: date) -> bool:
        """Code counts the nights and opens the gate, once: 7 nights built in shadow, at least
        one idea made, and 100% of the lines shown passing, each rechecked on its row now.
        The count starts after the last sheet with a failing line: one bad night delays, never blocks."""
        async with self._pool.connection() as conn:
            if await get_setting(conn, rules.GATE_KEY) is not None:
                return False  # opened once already: their "off" is never undone by code
            rows = await (
                await conn.execute(
                    "SELECT idea_id, lines FROM pisa_sheets WHERE gate = 'shadow' ORDER BY night"
                )
            ).fetchall()
            nights = with_idea = shown = passed = 0
            for idea, lines in rows:
                got = await self.recheck(conn, lines if isinstance(lines, list) else json.loads(lines))
                if got[1] != got[0]:  # a failing line: the clean run starts again after this night
                    nights = with_idea = shown = passed = 0
                    continue
                nights, with_idea = nights + 1, with_idea + (idea is not None)
                shown, passed = shown + got[0], passed + got[1]
            if not rules.gate_opens(nights, with_idea, shown, passed):
                _log.info("sheet_gate_shut", nights=nights, with_idea=with_idea, shown=shown, passed=passed)
                return False
            await set_setting(
                conn,
                rules.GATE_KEY,
                Jsonb({"opened_on": night.isoformat(), "nights": nights, "lines": shown}),
            )
            his = await stanotte_said(conn)
        if self._switch is not None and his is None:  # their own word, before or after, stands
            await self._switch({"stanotte": {"on": True}})
        _log.info("sheet_gate_opened", nights=nights, lines=shown)
        return True

    # --- the morning: sent with what they already get ------------------------------------------------------

    async def morning(self, conn: AsyncConnection, lang: str) -> tuple[uuid.UUID, str] | None:
        """Called inside the first automatic briefing's transaction: last night's sheet, if the gate is open,
        its lines still pass and it wasn't sent. Returns (the sheet id, the line the briefing ends with)."""
        if not await self.open(conn):
            return None
        now = self._clock()
        row = await (
            await conn.execute(
                "SELECT night, data, everything, lines, idea_id FROM pisa_sheets WHERE sent_at IS NULL "
                "AND built_at >= %s AND shown > 0 ORDER BY night DESC LIMIT 1 FOR UPDATE SKIP LOCKED",
                (now - timedelta(hours=rules.SENT_WITHIN_HOURS),),
            )
        ).fetchone()
        if row is None:
            return None
        night, data, every, lines, idea_id = row
        shown, passed = await self.recheck(conn, lines)
        if passed != shown:  # RAIL: a line that no longer passes withholds the whole sheet
            _log.info("sheet_withheld", shown=shown, passed=passed)
            return None
        day = now.astimezone(self._owner.zone).date()
        sheet_id = await _store(conn, "stanotte", day, data, now)
        tutto_id = await _store(conn, "dammi_tutto", day, every, now)
        if sheet_id is None:
            return None
        await conn.execute(
            "UPDATE pisa_sheets SET sent_at = %s, sheet_id = %s, tutto_id = %s WHERE night = %s",
            (now, sheet_id, tutto_id, night),
        )
        if idea_id is not None:
            await conn.execute(
                "UPDATE pisa_ideas SET state = 'shown', updated_at = %s WHERE id = %s AND state = 'ready'",
                (now, idea_id),
            )
        _log.info("sheet_sent", lines=shown)
        said = "Il foglio di stanotte è pronto." if lang.startswith("it") else "Last night's sheet is ready."
        return sheet_id, said

    async def sunday(self, lang: str) -> list[dict[str, Any]]:
        """The Sunday card's part (the week's poster), only with the gate open: the week's nights, searches
        and ideas made, counted."""
        async with self._pool.connection() as conn:
            if not await self.open(conn):
                return []
            today = self._owner.day(self._clock())
            week = today - timedelta(days=6)
            row = await (
                await conn.execute(
                    "SELECT count(*), count(*) FILTER (WHERE status = 'done') FROM pisa_nights WHERE night "
                    ">= %s",
                    (week,),
                )
            ).fetchone()
            searches = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_night_searches WHERE night >= %s AND sent", (week,)
                )
            ).fetchone()
            made = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_sheets WHERE night >= %s AND sent_at IS NOT NULL AND idea_id "
                    "IS NOT "
                    "NULL",
                    (week,),
                )
            ).fetchone()
        it = lang.startswith("it")
        blocks: list[dict[str, Any]] = [
            {"type": "section", "title": "Stanotte" if it else "Last nights"},
            {"type": "stat", "label": "Notti" if it else "Nights", "value": str(row[0] if row else 0)},
            {
                "type": "stat",
                "label": "Ricerche" if it else "Searches",
                "value": str(searches[0] if searches else 0),
            },
            {
                "type": "stat",
                "label": "Idee fatte" if it else "Ideas made",
                "value": str(made[0] if made else 0),
            },
        ]
        return blocks


def _ev(row: Any) -> rules.Evidence:
    eid, rung, claim, kind, passed, simulated, url, final, number, run = row
    run = run if isinstance(run, dict) or run is None else json.loads(run)
    return rules.Evidence(str(eid), rung, claim, kind, passed, bool(simulated), url, final,
                          float(number) if number is not None else None, run)  # fmt: skip


def _grade(evidence: tuple[rules.Evidence, ...], lang: str) -> str:
    """The evidence grade in words (as `IdeasService.grade`), from the lines that pass."""
    facts: dict[str, float] = {}
    rungs: set[str] = set()
    competitors = 0
    for e in evidence:
        if e.passed is False:
            continue
        rungs.add(e.rung)
        competitors += e.rung == "V0" and e.check_kind == "link"
        if e.rung == "V1" and e.number is not None and "buyers" not in facts:
            facts["buyers"] = e.number
        if e.rung == "V2" and e.run and "revenue_year_eur" in (e.run.get("output") or {}):
            facts["revenue_year_eur"] = float(e.run["output"]["revenue_year_eur"])
    facts["competitors"] = competitors
    return rules.grade_words(lang, sorted(rungs), facts)


async def _exists(conn: AsyncConnection, table: str) -> bool:
    row = await (await conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))).fetchone()
    return bool(row and row[0])


async def _read(conn: AsyncConnection, table: str, sql: LiteralString, args: tuple[Any, ...]) -> list[Any]:
    """Another engine's table, when it is there: a missing table or column
    (or any other database error) leaves that part off the sheet, never fails it."""
    if not await _exists(conn, table):
        return []
    try:
        async with conn.transaction():
            return list(await (await conn.execute(sql, args)).fetchall())
    # any database error leaves the part off, never fails the night
    except psycopg.Error as exc:
        _log.info("sheet_source_missing", table=table, error=exc.__class__.__name__)
        return []


async def _store(
    conn: AsyncConnection, ritual: str, day: date, data: dict[str, Any], now: datetime
) -> uuid.UUID | None:
    """A ritual's sheet (`GET /v1/sheets/{id}`), once per day."""
    RitualSheetData.model_validate(data)
    sheet_id = uuid.uuid4()
    row = await (
        await conn.execute(
            "INSERT INTO ritual_sheets (id, ritual, day, data, actions, created_at) VALUES (%s, %s, %s, %s, "
            "%s, %s) "
            "ON CONFLICT (ritual, day) DO NOTHING RETURNING id",
            (sheet_id, ritual, day, Jsonb(data), Jsonb([]), now),
        )
    ).fetchone()
    return sheet_id if row else None
