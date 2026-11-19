"""The night's frame: one night orchestrator, after the `learning.nightly`, in their zone, behind the stop.

**Shadow only**: nothing here reaches them (no notification, message, candidate or task) and
nothing here enters any prompt: no turn, intake, profile, grounding or style path reads `pisa_night*`. The
night's results wait for the sheet, a view or a private page.

The steps, in order (`night.STEPS`; the other engines add theirs after these, under the same stop and
budget):
- **Score** (`n9_score`): the predictions made for a day that has ended are scored against what happened
  (Brier).
- **Replay yesterday** (`n8_replay`) as a regression test, from the first night: each of yesterday's real
  turns (at most 30) is replayed against tonight's Pisa: a tool Pisa used for them then, which Pisa had last
  night and no longer has tonight, is a failed case. Model-free (€0): the turn's own record (their message,
  Pisa's moves in `pisa_decisions`) against the tools tonight's conversation agent is built with.
- **Predict tomorrow** (`n9_predict`): 5 to 10 predictions with a probability from their own base rates (they
  talk to Pisa; before 09:00; after 21:00; a busy day; a task done; each of tomorrow's events kept), never
  shown to them (the twin's fidelity score).

**The caps**: every model call the night makes goes on the ledger's `night` budget (€0.25 a night,
`ledger.night_budget`), never the user's; a step refused by the cap ends the night (`capped`), and their turns
are untouched. **Searches** go through `search` only: at most 40 a night, and the filter (`night.scrub`)
strips their names, their contacts', e-mails, addresses, amounts and dates first.

**The stop**: "Pisa, ferma tutto" active, the night does nothing at all: no step, no call, no row; a
stop said mid-night ends it before the next step and before any search. Logs carry counts only.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.door import guard
from pisa.domain import night as rules
from pisa.domain import taint
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.infra import ledger, locks
from pisa.logging import get_logger
from pisa.services.stop import stopped_now

_log = get_logger("pisa.night")
_KIND = re.compile(r"[a-z0-9_]{1,40}")
# the filter's proper nouns: a capitalised word (a name, a city, a company); the anonymiser reads it too
PROPER = re.compile(r"\b[A-ZÀ-Ý][^\W\d_]{2,}")
HISTORY_DAYS = 14  # their base rates for the predictions
EARLY, LATE = time(9, 0), time(21, 0)
BUSY = 5  # a busy day: five messages of theirs or more

Web = Callable[[taint.Stripped], Awaitable[list[Any] | None]]  # the night's door: only a `Stripped` goes out
Offered = Callable[[], Awaitable[set[str]]]
Step = Callable[[date], Awaitable[int]]


class Engine(Protocol):
    """A night engine: its steps by name (`STEPS` names their order)."""

    def steps(self) -> Mapping[str, Step]: ...


def tool_definitions(*, defer: bool = True) -> list[Any]:
    """The tools Pisa's conversation model is offered on a turn's first request, as the provider sees it, from
    Pisa's own agent (built with stand-in services: the definitions only; nothing runs). `defer` False: every
    tool, the deferred ones included (what search_tools finds them in)."""
    from datetime import UTC
    from unittest.mock import MagicMock

    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from pisa.brain.conversation import TurnContext, build_agent
    from pisa.brain.research_turn import ResearchTurn
    from pisa.domain.owner import Owner
    from pisa.services.settings import read

    seen: list[Any] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(info.function_tools)
        return ModelResponse(parts=[TextPart(content="ok")])

    stand_in = MagicMock(timezone="Europe/Rome")

    async def noop(_: Any) -> bool:
        return True

    # the night's tool groups (`brain.conversation.GROUPS`): its turn context has these families, each a
    # stand-in; the registry's order and tiers do the rest
    context = TurnContext(
        owner=Owner("Europe/Rome", language="it", country="IT", read=read),
        clock=lambda: datetime.now(UTC),
        data=stand_in,
        places=stand_in,
        actions=stand_in,
        profile=stand_in,
        compose=noop,
        call=noop,
        research=ResearchTurn(lang="it-IT", prompt_chars=0),
        decided=stand_in,
        defer=defer,
        sources=stand_in,  # the connectors' readers (offered while a connector feeding them is on)
        loops=stand_in,  # intentions and the waiting-for list (found by search_tools)
    )
    agent = build_agent(FunctionModel(capture), context)
    asyncio.run(agent.run("x"))  # a loop of its own: callers in a loop use a thread
    return seen


async def tonight_tools() -> set[str]:
    """Every tool tonight's conversation agent has (offered, or found by search_tools): the replay's
    yardstick."""
    return {t.name for t in await asyncio.to_thread(tool_definitions, defer=False)}


SEARCH_TOOLS = "search_tools"  # finding a deferred tool is no tool of their turn


def called(model_messages: Any) -> list[str]:
    """The tools a reply's model messages called (their `tool-call` parts)."""
    names: list[str] = []
    for message in model_messages if isinstance(model_messages, list) else []:
        for part in (message or {}).get("parts") or [] if isinstance(message, dict) else []:
            if isinstance(part, dict) and part.get("part_kind") == "tool-call" and part.get("tool_name"):
                names.append(str(part["tool_name"]))
    return names


def night_web(places: Any) -> Web:
    """The night's web search, five results, on the night's own place budget (never `choose`, never
    their cap or their cap notice; `PlaceProviderService.night_search`). None when refused."""

    async def web(query: str) -> list[Any] | None:
        # RAIL: the night's door takes only what the strip produced (`Stripped`)
        guard("the night's search", query, strict=True)
        return await places.providers.night_search(query, max_results=5, cap_eur=rules.NIGHT_PLACES_MONTH_EUR)

    return web


async def senders(conn: AsyncConnection) -> list[str]:
    """Everyone who ever wrote to them, names and addresses, most recent first: the whole list, never a subset
    (keeps mail, so a cap would drop new names from the night's filter and the anonymiser)."""
    rows = await (
        await conn.execute(
            "SELECT from_name, from_address FROM email_messages WHERE from_name IS NOT NULL "
            "OR from_address IS NOT NULL GROUP BY 1, 2 ORDER BY max(sent_at) DESC NULLS LAST"
        )
    ).fetchall()
    return [str(v) for sender, address in rows for v in (sender, address) if v]


class NightService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        web: Web | None = None,
        offered: Offered = tonight_tools,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._web = web
        self._offered = offered
        # the engines whose steps the night runs (`declare`); a name in STEPS with no step is skipped
        self.engines: tuple[Engine, ...] = ()

    def declare(self, *engines: "Engine") -> None:
        """The night's engines, declared once: their steps by name (`IdeasService.steps`), run in the order
        of `STEPS`, whatever the order they are declared in. Two engines never name the same step."""
        seen = set(self._own())
        for engine in engines:
            twice = seen & set(engine.steps())
            if twice:
                raise ValueError(f"night steps declared twice: {sorted(twice)}")
            seen |= set(engine.steps())
        self.engines = engines

    def steps(self) -> dict[str, Step]:
        """Every step tonight, by name: the frame's own and its engines'."""
        steps = self._own()
        for engine in self.engines:
            steps |= engine.steps()
        return steps

    def _own(self) -> dict[str, Step]:
        return {"n9_score": self.score, "n8_replay": self.replay, "n9_predict": self.predict}

    def night_of(self, at: datetime) -> date:
        return at.astimezone(self._owner.zone).date()

    def day(self, day: date) -> tuple[datetime, datetime]:
        """A local day's bounds in their zone, for the other engines' steps."""
        return self._day(day)

    async def scored(self, since: date) -> list[tuple[int, str, date, float, bool]]:
        """The predictions scored for a day since `since`: (id, kind, day, p, outcome). The curiosity
        engine reads its confident misses here (the miss becomes a question), without naming the frame's
        tables."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, kind, for_day, p, outcome FROM pisa_night_predictions WHERE outcome IS NOT "
                    "NULL AND for_day >= %s ORDER BY id",
                    (since,),
                )
            ).fetchall()
        return [(int(i), str(k), d, float(p), bool(o)) for i, k, d, p, o in rows]

    def _day(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time(0), tzinfo=self._owner.zone)
        return start, datetime.combine(day + timedelta(days=1), time(0), tzinfo=self._owner.zone)

    async def _stopped(self) -> bool:
        async with self._pool.connection() as conn:
            return await stopped_now(conn)

    # --- the night ------------------------------------------------------------------------------------------

    async def run(self) -> dict[str, int]:
        """The night, once per local date: its steps in order, counts only. Stopped: nothing at all."""
        if await self._stopped():  # RAIL: "ferma tutto" → no step, no call, no row
            _log.info("night_paused")
            return {"paused": 1}
        now = self._clock()
        hour = now.astimezone(self._owner.zone).hour
        if not rules.NIGHT_HOURS[0] <= hour < rules.NIGHT_HOURS[1]:  # their night, wherever they are
            _log.info("night_not_his_night", hour=hour)
            return {"not_night": 1}
        night = self.night_of(now)
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO pisa_nights (night, started_at) VALUES (%s, %s) ON CONFLICT (night) DO "
                    "NOTHING RETURNING night",
                    (night, now),
                )
            ).fetchone()
        if row is None:  # tonight already ran (a retried job): never twice
            _log.info("night_already_ran")
            return {"already": 1}
        counts: dict[str, int] = {}
        status = "done"
        steps = self.steps()
        try:
            with ledger.night_budget():  # RAIL: every call below goes on the night's own budget
                for name in rules.STEPS:
                    if await self._stopped():  # a stop said mid-night ends it here
                        status = "stopped"
                        break
                    if name not in steps:
                        continue
                    try:
                        counts[name] = await steps[name](night)
                    except PisaError as err:
                        if err.code != "budget_exceeded":
                            raise
                        status = "capped"  # the cap ends the night; their turns are untouched
                        break
        except BaseException:  # the worker's time limit cancels it, never marked `done`
            status = "failed"  # never left `running`: tomorrow's night runs, today's is not retried
            raise
        finally:
            async with self._pool.connection() as conn:
                spent = await ledger.spent_since(conn, now, "night")
                await conn.execute(
                    "UPDATE pisa_nights SET finished_at = %s, status = %s, counts = counts || %s, "
                    "spent_eur = %s WHERE night = %s",
                    (self._clock(), status, Jsonb(counts), spent, night),
                )
        _log.info("night_done", status=status, **counts)
        return counts | {status: 1}

    async def left(self) -> int:
        """The night's searches still unsent (of the 40, every engine together): takes only what
        the ideas and the curiosity left."""
        night = self.night_of(self._clock())
        async with self._pool.connection() as conn:
            sent = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_night_searches WHERE night = %s AND sent", (night,)
                )
            ).fetchone()
        return max(rules.NIGHT_SEARCHES - int(sent[0] if sent else 0), 0)

    # --- searches: the only way out to the web --------------------------------------------------------------

    async def names(self, conn: AsyncConnection) -> list[taint.Personal]:
        """What the filter strips: their names (the trips' setting, the connected accounts and their
        local parts, the names their own sent mail goes out under), the people in their life (contacts: names,
        e-mails, cities; everyone who wrote to them), their places' names and addresses, and the proper nouns
        of their profile (their city, their work, their relatives: capitalised words of every claim and its
        subject)."""
        from pisa.services.trips import trip_names

        found: list[str] = await trip_names(conn)
        for (account,) in await (
            await conn.execute("SELECT account FROM connector_accounts WHERE account IS NOT NULL")
        ).fetchall():
            local = str(account).split("@")[0]
            found += [w for w in local.replace("_", ".").replace("-", ".").replace("+", ".").split(".") if w]
            found.append(str(account))
        for name, emails, city in await (
            await conn.execute("SELECT name, emails, city FROM people")
        ).fetchall():
            found += [str(name), str(city or "")] + [str(e) for e in emails or ()]
        found += await senders(conn)  # everyone who wrote to them, all of them
        for name, address in await (await conn.execute("SELECT name, address FROM places")).fetchall():
            found += [str(name or ""), str(address or "")]
        for statement, subject in await (
            await conn.execute("SELECT statement, subject::text FROM profile_claims WHERE state <> 'retired'")
        ).fetchall():
            found += PROPER.findall(f"{statement} {subject}")
        return [taint.Personal(f) for f in found if f]

    async def search(self, query: str, *, kind: str, extra: Iterable[str] = ()) -> list[Any] | None:
        """RAIL (caps): one web search for a night step, or None when refused (the 41st, the stop, a
        query with nothing left once the personal is stripped, no provider). Only the filtered query leaves
        Pisa; the row keeps counts, never the query."""
        if not _KIND.fullmatch(kind):  # a step's name, never words of theirs
            raise ValueError("a night search's kind is a step's name: [a-z0-9_]{1,40}")
        now = self._clock()
        night = self.night_of(now)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.NIGHT_SEARCH,))
            running = await (
                await conn.execute(
                    "SELECT 1 FROM pisa_nights WHERE night = %s AND status = 'running'", (night,)
                )
            ).fetchone()
            if running is None or await stopped_now(conn):  # only inside a running night; stopped: no row
                _log.info("night_search_refused", reason="stopped" if running else "no_night")
                return None
            # `extra`: a step's own personal words (the proper nouns of their problem)
            clean = taint.strip(query, [*await self.names(conn), *extra])  # the only way to a `Stripped`
            stripped = clean.removed
            sent = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_night_searches WHERE night = %s AND sent", (night,)
                )
            ).fetchone()
            refused: str | None = None
            if (sent[0] if sent else 0) >= rules.NIGHT_SEARCHES:
                refused = "cap"
            elif not rules.sendable(clean):
                refused = "empty"
            elif self._web is None:
                refused = "no_provider"
            row = await (
                await conn.execute(
                    "INSERT INTO pisa_night_searches (night, at, kind, stripped, sent, refused) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (night, now, kind, stripped, refused is None, refused),
                )
            ).fetchone()
        if refused is not None or self._web is None:
            _log.info("night_search_refused", reason=refused, stripped=stripped)
            return None
        try:
            found = await self._web(clean)
        except Exception as exc:  # a provider's failure ends this search only
            _log.warning("night_search_failed", error=exc.__class__.__name__)
            found = None
        if found is None:  # the night's place budget, the hard limit or the provider said no: not sent
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pisa_night_searches SET sent = false, refused = 'budget' WHERE id = %s",
                    (row[0] if row else None,),
                )
        return found

    async def sent_tonight(self, night: date, prefix: str) -> int:
        """How many searches tonight's steps whose kind starts with `prefix` have sent (an engine's own
        share)."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_night_searches WHERE night = %s AND sent AND kind LIKE %s",
                    (night, prefix.replace("%", "") + "%"),
                )
            ).fetchone()
        return int(row[0]) if row else 0

    # --- yesterday as a regression test -----------------------------------------------------------------

    async def replay(self, night: date) -> int:
        """Yesterday's real turns (at most 30), each against tonight's tools; a tool Pisa had last night and
        has no more is a failed case. Returns the cases replayed."""
        start, end = self._day(night - timedelta(days=1))
        tonight = await self._offered()
        async with self._pool.connection() as conn:
            before = await (
                await conn.execute(
                    "SELECT tools FROM pisa_nights WHERE night < %s AND cardinality(tools) > 0 "
                    "ORDER BY night DESC LIMIT 1",
                    (night,),
                )
            ).fetchone()
            had = set(before[0]) if before else tonight  # night 1: tonight is the baseline
            # Pisa's reply is saved before its moves, under a later clock, and a read is no
            # move at all; so the exchange is their message up to their next one in the talk, and its tools
            # are the tool calls Pisa's reply carries (`model_messages`), the moves included
            turns = await (
                await conn.execute(
                    "SELECT m.id, ("
                    "  SELECT min(n.id) FROM messages n WHERE n.conversation_id = m.conversation_id "
                    "  AND n.role = 'user' AND n.id > m.id) "
                    "FROM messages m WHERE m.role = 'user' AND m.created_at >= %s AND m.created_at < %s "
                    "ORDER BY m.created_at, m.id LIMIT %s",
                    (start, end, rules.REPLAY_MAX),
                )
            ).fetchall()
            passed = 0
            for message_id, next_id in turns:
                replies = await (
                    await conn.execute(
                        "SELECT r.model_messages FROM messages r JOIN messages m ON m.id = %s "
                        "WHERE r.conversation_id = m.conversation_id AND r.role = 'assistant' "
                        "AND r.id > m.id "
                        "AND (%s::bigint IS NULL OR r.id < %s) ORDER BY r.id",
                        (message_id, next_id, next_id),
                    )
                ).fetchall()
                replied = bool(replies)
                tools = sorted({t for (mm,) in replies for t in called(mm)} - {SEARCH_TOOLS})
                missing = sorted(t for t in tools if t in had and t not in tonight)
                ok = replied and not missing
                passed += ok
                await conn.execute(
                    "INSERT INTO pisa_night_replays (night, message_id, tools, missing, replied, passed) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (night, message_id, tools, missing, replied, ok),
                )
            await conn.execute(
                "UPDATE pisa_nights SET tools = %s, counts = counts || %s WHERE night = %s",
                (sorted(tonight), Jsonb({"n8_passed": passed, "n8_failed": len(turns) - passed}), night),
            )
        return len(turns)

    # --- tomorrow's predictions, scored by Brier --------------------------------------------------------

    async def _his_days(self, conn: AsyncConnection, night: date) -> list[list[datetime]]:
        """Their messages' moments for each of the last 14 days (local), oldest first."""
        days: list[list[datetime]] = []
        for back in range(HISTORY_DAYS, 0, -1):
            start, end = self._day(night - timedelta(days=back))
            rows = await (
                await conn.execute(
                    "SELECT created_at FROM messages WHERE role = 'user' AND created_at >= %s "
                    "AND created_at < %s ORDER BY created_at",
                    (start, end),
                )
            ).fetchall()
            days.append([r[0].astimezone(self._owner.zone) for r in rows])
        return days

    async def _observed(self, conn: AsyncConnection, kind: str, ref: str | None, day: date) -> bool:
        start, end = self._day(day)
        if kind == "event_kept":
            calendar_id, event_id, epoch = json.loads(ref or "")  # ids may hold any character
            row = await (
                await conn.execute(
                    "SELECT 1 FROM calendar_events WHERE calendar_id = %s AND id = %s "
                    "AND status <> 'cancelled' "
                    "AND starts_at = to_timestamp(%s)",
                    (calendar_id, event_id, int(epoch)),
                )
            ).fetchone()
            return row is not None
        if kind == "task_done":
            row = await (
                await conn.execute(
                    "SELECT 1 FROM tasks WHERE status = 'done' AND updated_at >= %s "
                    "AND updated_at < %s LIMIT 1",
                    (start, end),
                )
            ).fetchone()
            return row is not None
        rows = await (
            await conn.execute(
                "SELECT created_at FROM messages WHERE role = 'user' AND created_at >= %s "
                "AND created_at < %s",
                (start, end),
            )
        ).fetchall()
        return _hit(kind, [r[0].astimezone(self._owner.zone) for r in rows])

    async def score(self, night: date) -> int:
        """The open predictions for a day that has ended, scored; tonight's Brier on `pisa_nights`."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, kind, ref, for_day, p FROM pisa_night_predictions WHERE outcome IS NULL "
                    "AND for_day < %s ORDER BY id",
                    (night,),
                )
            ).fetchall()
            pairs: list[tuple[float, bool]] = []
            for pid, kind, ref, day, p in rows:
                try:
                    happened = await self._observed(conn, kind, ref, day)
                except (ValueError, TypeError):  # one bad row is skipped (json's error is one)
                    _log.warning("night_prediction_unscored", id=pid)
                    continue
                pairs.append((float(p), happened))
                await conn.execute(
                    "UPDATE pisa_night_predictions SET outcome = %s, scored_at = %s WHERE id = %s",
                    (happened, self._clock(), pid),
                )
            await conn.execute(
                "UPDATE pisa_nights SET brier = %s WHERE night = %s", (rules.brier(pairs), night)
            )
        return len(pairs)

    async def predict(self, night: date) -> int:
        """5 to 10 predictions for the day this night opens, from their own last 14 days."""
        start, end = self._day(night)
        async with self._pool.connection() as conn:
            days = await self._his_days(conn, night)
            talked = [d for d in days if d]
            made: list[tuple[str, str | None, float]] = [
                ("talks", None, rules.rate(len(talked), len(days))),
                # over every day, as the outcome is (a day they didn't talk is no early day)
                ("early", None, rules.rate(sum(_hit("early", d) for d in days), len(days))),
                ("evening", None, rules.rate(sum(_hit("evening", d) for d in days), len(days))),
                ("busy", None, rules.rate(sum(_hit("busy", d) for d in days), len(days))),
            ]
            done = await (
                await conn.execute(
                    "SELECT count(DISTINCT (updated_at AT TIME ZONE %s)::date) FROM tasks "
                    "WHERE status = 'done' "
                    "AND updated_at >= %s AND updated_at < %s",
                    (str(self._owner.zone), start - timedelta(days=HISTORY_DAYS), start),
                )
            ).fetchone()
            made.append(("task_done", None, rules.rate(int(done[0]) if done else 0, HISTORY_DAYS)))
            past = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE status <> 'cancelled'), count(*) FROM calendar_events "
                    "WHERE starts_at >= %s AND starts_at < %s",
                    (start - timedelta(days=HISTORY_DAYS), start),
                )
            ).fetchone()
            kept_rate = rules.rate(int(past[0]), int(past[1])) if past else 0.5
            events = await (
                await conn.execute(
                    "SELECT calendar_id, id, starts_at FROM calendar_events WHERE status <> 'cancelled' "
                    "AND starts_at >= %s AND starts_at < %s ORDER BY starts_at, id LIMIT %s",
                    (start, end, rules.PREDICT_MAX - len(made)),
                )
            ).fetchall()
            made += [
                ("event_kept", json.dumps([cal, eid, int(at.timestamp())]), kept_rate)
                for cal, eid, at in events
            ]
            for kind, ref, p in made:
                await conn.execute(
                    "INSERT INTO pisa_night_predictions (night, for_day, kind, ref, p) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (night, night, kind, ref, min(max(p, 0.01), 0.99)),
                )
        return len(made)


def _hit(kind: str, moments: list[datetime]) -> bool:
    """Whether a day of their messages' moments (local) is a hit for one of the message kinds."""
    if kind == "talks":
        return bool(moments)
    if kind == "early":
        return any(m.time() < EARLY for m in moments)
    if kind == "evening":
        return any(m.time() >= LATE for m in moments)
    if kind == "busy":
        return len(moments) >= BUSY
    return False
