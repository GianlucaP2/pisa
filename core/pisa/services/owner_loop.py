"""The user loop. Code only, no model.

- `weekly`: the week's talk report from production rows (`domain.owner_loop.report`): first content p50/p95 by
  route, the clean-turn rate, the user budget's spend per talk turn against the daily cap and its 50% economy
  switch, the replay's latest pass^3. Stored once per ISO week (`pisa_owner_reports`), refreshed while the
  week runs; numbers only.
- `card_line` / `sheet`: the report as one line of the Sunday card and as the built-in view "report" (the
  app's `GET /v1/owner/report`, and `show_view("report")`).
- `scan_candidates`: every personal eval (their corrections and quick undos, `personal_evals`) without a
  candidate becomes a synthetic, anonymised scenario candidate (`pisa_scenario_candidates`): their names,
  places, amounts and dates replaced by placeholders with the night frame's filter. Stored for the operator's
  review; nothing here or anywhere adds it to the replay suite.
- `export`: the candidates (ids and anonymised text) for the maintenance job `scenario-candidates`.
Logs carry counts only.
"""

import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import latency
from pisa.domain import ledger as week_rules
from pisa.domain import owner_loop as rules
from pisa.domain.owner import Owner
from pisa.domain.sheet import SheetData
from pisa.infra import ledger, spend
from pisa.logging import get_logger
from pisa.services.guardian_tick import measures as guardian_measures
from pisa.services.night import PROPER, senders
from pisa.services.trips import trip_names

_log = get_logger("pisa.owner_loop")
SCAN_MAX = 200  # candidates made per minute at most


async def personal_words(conn: AsyncConnection) -> tuple[list[str], list[str]]:
    """What the anonymiser replaces, as the night's filter gathers it (`NightService.names`), split in two:
    (names: their own, their accounts', their contacts' names and e-mails, everyone who wrote to them, the
    proper nouns of their profile; places: their places' names and addresses, their contacts' cities)."""
    names: list[str] = []
    places: list[str] = []
    names += await trip_names(conn)
    for (account,) in await (
        await conn.execute("SELECT account FROM connector_accounts WHERE account IS NOT NULL")
    ).fetchall():
        local = str(account).split("@")[0]
        names += [w for w in re.split(r"[._+\-]", local) if w] + [str(account)]
    for name, emails, city in await (await conn.execute("SELECT name, emails, city FROM people")).fetchall():
        names += [str(name or "")] + [str(e) for e in emails or ()]
        places.append(str(city or ""))
    names += await senders(conn)
    for name, address in await (await conn.execute("SELECT name, address FROM places")).fetchall():
        places += [str(name or ""), str(address or "")]
    for statement, subject in await (
        await conn.execute("SELECT statement, subject::text FROM profile_claims WHERE state <> 'retired'")
    ).fetchall():
        names += PROPER.findall(f"{statement} {subject}")
    return [n for n in names if n], [p for p in places if p]


class OwnerLoop:
    def __init__(self, pool: AsyncConnectionPool, *, owner: Owner, clock: Callable[[], datetime]) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    # --- the weekly talk report ---------------------------------------------------------------------------

    def _bounds(self, monday: date) -> tuple[datetime, datetime]:
        tz = self._owner.zone
        start = datetime.combine(monday, datetime.min.time(), tz)
        return start, datetime.combine(monday + timedelta(days=7), datetime.min.time(), tz)

    def week_now(self) -> str:
        return week_rules.week_of(self._owner.day(self._clock()))

    async def build(self, week: str) -> dict[str, Any]:
        """The week's report from production rows (to the week's end, or now while it runs). Numbers only."""
        monday = week_rules.parse_week(week, self._owner.day(self._clock()))
        start, end = self._bounds(monday)
        end = min(end, self._clock())
        async with self._pool.connection() as conn:
            turns = await (
                await conn.execute(
                    "SELECT route, first_content_ms, failed, unbacked, redone FROM talk_turns "
                    "WHERE owner_end_at >= %s AND owner_end_at < %s",
                    (start, end),
                )
            ).fetchall()
            # each turn's latest reflex read (who, how long) and who said its first words
            reflexed = await (
                await conn.execute(
                    "SELECT reflex_by, reflex_ms, first_content_by FROM talk_turns "
                    "WHERE owner_end_at >= %s AND owner_end_at < %s",
                    (start, end),
                )
            ).fetchall()
            days: list[Decimal] = []
            day = start
            while day < end:
                following = min(day + timedelta(days=1), end)
                row = await (
                    await conn.execute(
                        f"SELECT {spend.SPEND} FROM llm_calls WHERE budget = 'owner' "  # noqa: S608
                        "AND created_at >= %s AND created_at < %s",
                        (day, following),
                    )
                ).fetchone()
                days.append(Decimal(row[0]) if row else Decimal(0))
                day = datetime.combine(day.date() + timedelta(days=1), datetime.min.time(), self._owner.zone)
            caps = await ledger.get_caps(conn)
            interruptions = await guardian_measures(conn, start, end)
            hidden = await (
                await conn.execute(  # the week's hidden text left out, per source
                    "SELECT source, sum(runs)::int FROM pisa_hidden_text WHERE day >= %s AND day <= %s "
                    "GROUP BY source",
                    (start.date(), end.date()),
                )
            ).fetchall()
            run = await (
                await conn.execute(
                    "SELECT at, effort, k, scenarios, passed FROM pisa_replay_runs WHERE NOT personal "
                    "ORDER BY at DESC LIMIT 1"
                )
            ).fetchone()
        replay = (
            {
                "at": run[0].astimezone(self._owner.zone).date().isoformat(),
                "effort": run[1],
                "k": run[2],
                "scenarios": run[3],
                "passed": run[4],
            }
            if run
            else None
        )
        return rules.report(
            week=week_rules.week_of(monday),
            turns=[(r, ms, int(f or 0), int(u or 0), bool(rd)) for r, ms, f, u, rd in turns],
            days=days,
            spent=sum(days, Decimal(0)),
            cap_daily=caps.daily_eur,
            replay=replay,
            reflex=latency.reflex([(by, ms) for by, ms, _ in reflexed], [first for *_, first in reflexed]),
            interruptions=interruptions,
            hidden={str(source): int(runs) for source, runs in hidden},
        )

    async def weekly(self, week: str | None = None) -> dict[str, Any]:
        """Make (or refresh) the week's report and keep it (`pisa_owner_reports`)."""
        report = await self.build(week or self.week_now())
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pisa_owner_reports (week, report, made_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (week) DO UPDATE SET report = EXCLUDED.report, made_at = EXCLUDED.made_at",
                (report["week"], Jsonb(report), self._clock()),
            )
        _log.info("owner_report_made", turns=report["turns"], routes=len(report["routes"]))
        return report

    async def tick(self) -> int:
        """Every minute: the week's report once an hour (numbers only, no push), then new candidates."""
        now = self._clock()
        week = self.week_now()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT made_at FROM pisa_owner_reports WHERE week = %s", (week,))
            ).fetchone()
        if row is None or now - row[0] >= timedelta(hours=1):
            await self.weekly(week)
        return await self.scan_candidates()

    async def latest(self, week: str | None = None) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT report FROM pisa_owner_reports WHERE (%s::text IS NULL OR week = %s) "
                    "ORDER BY week DESC LIMIT 1",
                    (week, week),
                )
            ).fetchone()
        return row[0] if row else None

    async def card_line(self, week: str, lang: str) -> str | None:
        """The Sunday card's one line: the card's week, made fresh."""
        return rules.card_line(await self.weekly(week), lang)

    async def sheet(self, lang: str, week: str | None = None) -> dict[str, Any] | None:
        """The built-in view: the latest (or the named) week's report as a sheet; None before any."""
        report = await self.latest(week)
        if report is None:
            return None
        data = rules.sheet(report, lang)
        SheetData.model_validate(data)  # RAIL: the protocol's own limits
        return data

    # --- their corrections and quick undos as anonymised scenario candidates
    # ---------------------------------

    async def scan_candidates(self) -> int:
        """Every personal eval without a candidate becomes one: anonymised by code, stored for review."""
        made = 0
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT p.id, p.kind, p.scenario FROM personal_evals p "
                    "WHERE p.kind IN ('correction', 'undo') "
                    "AND NOT EXISTS (SELECT 1 FROM pisa_scenario_candidates c WHERE c.eval_id = p.id) "
                    "ORDER BY p.made_at LIMIT %s",
                    (SCAN_MAX,),
                )
            ).fetchall()
            if not rows:
                return 0
            names, places = await personal_words(conn)
            for eval_id, kind, scenario in rows:
                anonymous, replaced = rules.candidate(scenario, names, places, self._owner.tag)
                anonymous = {"id": f"candidate-{uuid.uuid4().hex[:8]}", **anonymous}
                done = await conn.execute(
                    "INSERT INTO pisa_scenario_candidates (id, eval_id, kind, scenario, replaced, made_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (eval_id) DO NOTHING",
                    (uuid.uuid4(), eval_id, kind, Jsonb(anonymous), Jsonb(replaced), self._clock()),
                )
                made += done.rowcount
        if made:
            _log.info("scenario_candidates_made", count=made)
        return made

    async def counts(self) -> dict[str, int]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT kind || CASE WHEN exported_at IS NULL THEN '' ELSE ' (exported)' END, count(*) "
                    "FROM pisa_scenario_candidates GROUP BY 1"
                )
            ).fetchall()
        return {k: n for k, n in rows}

    async def export(self, *, mark: bool) -> list[dict[str, Any]]:
        """The candidates not yet exported, for the operator's review, oldest first: ids, kind, placeholder
        counts and the anonymised scenario. `mark` records the export (each goes out once)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, eval_id, kind, replaced, scenario FROM pisa_scenario_candidates "
                    "WHERE exported_at IS NULL ORDER BY made_at"
                )
            ).fetchall()
            if mark and rows:
                await conn.execute(
                    "UPDATE pisa_scenario_candidates SET exported_at = %s WHERE id = ANY(%s)",
                    (self._clock(), [r[0] for r in rows]),
                )
        return [
            {"id": str(i), "eval_id": str(e), "kind": k, "replaced": r, "scenario": s}
            for i, e, k, r, s in rows
        ]
