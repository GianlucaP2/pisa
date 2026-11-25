"""`pisa.domain.voice3`: speaking about the night.

- **The night-shift story** ("cosa hai fatto stanotte?"), by code from last night's records (`pisa_nights` and
  its children): every count is its rows' ids, the proudest idea is one whose evidence passes its check
  now, the wrong one is Pisa's own (a killed idea it believed, a confident miss, a turn it wouldn't redo), the
  next question is its shelf's top one (their words or Pisa's template; else only what it is about).
- **"Dammi tutto"**: the full drawer (ideas, dead ones with their evidence, open questions, scan items) built
  from the records as a sheet; pull has no limit, the push caps stay. `stanotte(query)` finds the rest.
- **The sparring partner**, **la scommessa**, **il quarto d'ora**: their rows here (0086), the decision
  journal (0080 `pisa_desk_decisions`) scoring their p and Pisa's p; the bets and the spars resolved by the
  night's `v3_resolve` step, a bet only from a public page Pisa links.
- **Gate**: the night's surface reaches them only with the stanotte gate open (`StanotteService._open`: code
  opened it once and their switch is on); otherwise these paths are the model's and the tool says it is off.
- **Outside text is data**: no delta, page claim, window title, invite or contact name is
  ever spoken or returned by the tool; an idea's words only when nothing outside seeded it.
- No turn, intake, profile, grounding or style path reads what this file returns into a prompt: the story
  and the drawer go to them by code; `stanotte` is the one read tool, behind `search_tools`. Logs carry ids
  and counts only.
"""

import json
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, LiteralString

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import curious as curious_rules
from pisa.domain import stanotte as night_rules
from pisa.domain import voice1 as voice1_rules
from pisa.domain import voice3 as rules
from pisa.domain.owner import Owner
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger
from pisa.services import home as home_store
from pisa.services import zone
from pisa.services.rituals import ritual_on
from pisa.services.zone import located_phone

_log = get_logger("pisa.voice3")
Material = tuple[str | None, str | None, str | None, list[dict[str, str]]]
Observe = Callable[
    [float, float, date], Awaitable[tuple[float, str]]
]  # (mm of rain that day, the public URL)
LAST_NIGHT = timedelta(hours=20)  # "stanotte": a night that started this long ago at most
IDLE = timedelta(hours=3)  # "idle for hours": no message with this device for this long
SHOWN = ("ready", "shown", "kept")
_EVIDENCE = (
    "SELECT id, rung, claim, check_kind, passed, simulated, url, final_url, number, run "
    "FROM pisa_idea_evidence "
)


@dataclass
class Reply:
    """What Pisa says by code, the path it opens for their next words, and the sheet it shows."""

    said: str
    opens: tuple[str, str] | None = None  # (path, ref): story/idea, p/decision, quarto/id, quarto_turn/idea
    asks: bool = False  # it ends with Pisa's own question (their p)
    sheet: dict[str, Any] | None = None
    bet_id: uuid.UUID | None = None


async def settle_by_idea(
    conn: AsyncConnection, link: Literal["pisa_spars", "pisa_consigli"], night: date, now: datetime
) -> int:
    """The open decisions a spar (`pisa_spars`) or il consiglio (`pisa_consigli`) made about an idea, settled
    by the idea's fate in Pisa's records: kept, true; killed or past its day, false; else still open. Returns
    how many were settled."""
    done = 0
    for decision, state, resolves_by in await (
        await conn.execute(
            f"SELECT d.id, i.state, d.resolves_by FROM {link} l JOIN pisa_desk_decisions d "  # noqa: S608
            "ON d.id = l.decision_id JOIN pisa_ideas i ON i.id = l.idea_id WHERE d.outcome IS NULL"
        )
    ).fetchall():
        outcome = True if state == "kept" else False if state == "killed" or resolves_by < night else None
        if outcome is None:
            continue
        await conn.execute(
            "UPDATE pisa_desk_decisions SET outcome = %s, resolved_at = %s WHERE id = %s",
            (outcome, now, decision),
        )
        done += 1
    return done


def _ev(row: Any) -> night_rules.Evidence:
    eid, rung, claim, kind, passed, simulated, url, final_url, number, run = row
    run = run if isinstance(run, dict) or run is None else json.loads(run)
    return night_rules.Evidence(
        str(eid), rung, claim, kind, passed, bool(simulated), url, final_url,
        float(number) if number is not None else None, run,
    )  # fmt: skip


class NightVoice:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        stanotte: Any = None,  # StanotteService: the gate (its `_open`)
        weather: Any = None,  # WeatherService: Pisa's p for rain (the forecast)
        observe: Observe | None = None,  # Open-Meteo's day, after it: a bet's public resolver
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._stanotte = stanotte
        self._weather = weather
        self._observe = observe
        self._opened: dict[uuid.UUID, tuple[Any, str, str]] = {}  # device → (Pisa's turn, path, ref)
        self._talks: dict[uuid.UUID, tuple[uuid.UUID, datetime]] = {}  # device → its quarto (id, fixed end)

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {"v3_resolve": self.resolve}

    # --- the paths Pisa's own last thing opened ------------------------------------------------------------

    def opened(self, device_id: uuid.UUID, turn: Any) -> tuple[str, str] | None:
        """The path Pisa's last thing to this device opened, only while that thing is still Pisa's last
        turn."""
        got = self._opened.get(device_id)
        return (got[1], got[2]) if got is not None and turn is not None and got[0] is turn else None

    def mark(self, device_id: uuid.UUID, turn: Any, opens: tuple[str, str] | None) -> None:
        if opens is None:
            self._opened.pop(device_id, None)
        else:
            self._opened[device_id] = (turn, *opens)

    async def idle(self, device_id: uuid.UUID) -> bool:
        """The talk with this device was idle (no reply of Pisa's in the last IDLE): a bare "cosa
        hai fatto stanotte?" then opens a talk, never answers someone else's."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT max(m.created_at) FROM messages m "
                    "JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE c.device_id = %s",
                    (device_id,),
                )
            ).fetchone()
        return row is None or row[0] is None or row[0] <= self._clock() - IDLE

    async def gate(self, conn: AsyncConnection) -> bool:
        return self._stanotte is not None and bool(await self._stanotte.open(conn))

    async def heard(
        self,
        device_id: uuid.UUID,
        kind: rules.Heard,
        text: str,
        lang: str,
        before: Any,
        *,
        after_reading: bool = False,
        story_open: bool = False,
    ) -> Reply | None:
        """Their closed phrase, answered by code, or None (the model's): each only on its path. The story's
        path: Pisa's own last thing was the knock's reading or today's briefing, or the talk was idle
        (`story_open`, the conversation's)."""
        path = self.opened(device_id, before)
        if kind == "story":
            return await self.story(lang) if story_open else None
        if kind == "spar":
            if path is None or path[0] not in ("story", "quarto_turn") or not path[1]:
                return None
            return await self.spar(path[1], lang)
        if kind == "p":
            if path is None or path[0] != "p":
                return None
            p = rules.p_of(text)
            return await self.his_p(path[1], p, lang) if p is not None else None
        if kind == "bet":
            return await self.bet(text, lang)
        if kind == "quarto_start":
            return await self.quarto_start(device_id, lang) if after_reading else None
        return await self.quarto(device_id, kind, lang)

    # --- the night-shift story --------------------------------------------------------------------------

    async def _last_night(self, conn: AsyncConnection) -> tuple[date, datetime] | None:
        row = await (
            await conn.execute(
                "SELECT night, started_at FROM pisa_nights WHERE started_at >= %s AND status <> 'running' "
                "ORDER BY night DESC LIMIT 1",
                (self._clock() - LAST_NIGHT,),
            )
        ).fetchone()
        return (row[0], row[1]) if row else None

    async def night(self, conn: AsyncConnection, lang: str) -> rules.Night | None:
        """Last night's records (None: no night ran since yesterday evening)."""
        found = await self._last_night(conn)
        if found is None:
            return None
        night, since = found

        async def ids(sql: LiteralString, *args: Any) -> list[str]:
            return [str(r[0]) for r in await (await conn.execute(sql, args)).fetchall()]

        out = rules.Night(night)
        out.problems = await ids(
            "SELECT id FROM pisa_idea_problems WHERE prepared_on = %s ORDER BY id", night
        )
        out.incubated = await ids("SELECT id FROM pisa_ideas WHERE night = %s ORDER BY id", night)
        out.validated = await ids(
            "SELECT id FROM pisa_ideas WHERE night = %s AND kills_at IS NOT NULL ORDER BY id", night
        )
        out.ready = await ids(
            "SELECT id FROM pisa_ideas WHERE night = %s AND state = ANY(%s) ORDER BY id", night, list(SHOWN)
        )
        out.killed = await ids(
            "SELECT idea_id FROM pisa_idea_cemetery WHERE killed_at >= %s ORDER BY idea_id", since
        )
        out.searches = await ids(
            "SELECT id FROM pisa_night_searches WHERE night = %s AND sent ORDER BY id", night
        )
        out.answered = await ids(
            "SELECT id FROM pisa_unknown_changes WHERE night = %s AND change = 'answered' ORDER BY id", night
        )
        out.proud = await self._proud(conn, night, lang)
        out.wrong = await self._wrong(conn, night, since)
        out.next = await self._next(conn)
        return out

    async def _idea(self, conn: AsyncConnection, row: Any, lang: str) -> tuple[night_rules.Idea, str] | None:
        """An idea as the sheet checks it, and the words Pisa may say for it (its own, or what it is)."""
        iid, statement, mechanism, market, kind, _seeds, problem = row
        evidence = tuple(
            _ev(r) for r in await (
                await conn.execute(_EVIDENCE + "WHERE idea_id = %s ORDER BY checked_at, id", (iid,))
            ).fetchall()
        )  # fmt: skip
        idea = night_rules.Idea(str(iid), statement, mechanism, market, kind, "", evidence)
        if not idea.shown():  # an idea whose evidence fails a check is never said
            return None
        outside = await self._outside(conn, iid)
        words = (
            f"«{night_rules.cut(statement, 120)}»"
            if rules.speakable(statement, problem or "", outside_seeds=outside)
            else rules.idea_words(kind, market, lang)
        )
        return idea, words

    async def _outside(self, conn: AsyncConnection, iid: Any) -> bool:
        """An idea is outside text when it or any ancestor (lineage, or its seeds' parents) was
        seeded by a world delta (a night search's page). Fails closed: an unknown idea is outside."""
        row = await (
            await conn.execute(
                "WITH RECURSIVE anc(id) AS (SELECT %s::uuid UNION "
                "SELECT x.parent FROM anc JOIN ("
                "  SELECT l.parent, l.child FROM pisa_idea_lineage l"
                "  UNION SELECT p.value::uuid, i.id FROM pisa_ideas i,"
                "  jsonb_array_elements_text(CASE WHEN jsonb_typeof(i.seeds->'parents') = 'array'"
                "  THEN i.seeds->'parents' ELSE '[]'::jsonb END) p(value)"
                "  WHERE p.value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
                ") x ON x.child = anc.id) "
                "SELECT count(i.id), bool_or(i.seeds ?| array['delta', 'deltas', 'delta_ids']) "
                "FROM anc LEFT JOIN pisa_ideas i ON i.id = anc.id",
                (iid,),
            )
        ).fetchone()
        return row is None or not row[0] or bool(row[1])

    async def ideas(
        self, conn: AsyncConnection, lang: str, night: date | None = None
    ) -> list[tuple[night_rules.Idea, str]]:
        rows = await (
            await conn.execute(
                "SELECT i.id, i.statement, i.mechanism, i.market, i.kind, i.seeds, p.statement "
                "FROM pisa_ideas i LEFT JOIN pisa_idea_problems p ON p.id = i.problem_id WHERE i.state = "
                "ANY(%s) AND (%s::date IS NULL OR i.night = %s) "
                "ORDER BY i.night DESC, i.p_keep DESC NULLS LAST, i.id",
                (list(SHOWN), night, night),
            )
        ).fetchall()
        out = []
        for row in rows:
            if (got := await self._idea(conn, row, lang)) is not None:
                out.append(got)
        return out

    async def _proud(self, conn: AsyncConnection, night: date, lang: str) -> rules.Proud | None:
        ideas = await self.ideas(conn, lang, night)
        if not ideas:
            return None
        idea, words = ideas[0]
        checked = sum(night_rules.line_ok(e) for e in idea.evidence if not e.simulated)
        return rules.Proud(idea.id, words, checked)

    async def _wrong(self, conn: AsyncConnection, night: date, since: datetime) -> rules.Wrong | None:
        killed = await (
            await conn.execute(
                "SELECT i.id, i.p_keep FROM pisa_idea_cemetery c JOIN pisa_ideas i ON i.id = c.idea_id "
                "WHERE c.killed_at >= %s AND i.p_keep IS NOT NULL ORDER BY i.p_keep DESC, i.id LIMIT 1",
                (since,),
            )
        ).fetchone()
        if killed is not None and float(killed[1]) >= curious_rules.CONFIDENT:
            return rules.Wrong("idea", "ideas", (str(killed[0]),), p=float(killed[1]))
        for pid, kind, p, outcome, day in await (
            await conn.execute(
                "SELECT id, kind, p, outcome, for_day FROM pisa_night_predictions WHERE scored_at >= %s "
                "AND outcome IS NOT NULL ORDER BY abs(p - 0.5) DESC, id",
                (since,),
            )
        ).fetchall():
            if (said := curious_rules.miss_question(kind, float(p), bool(outcome), day)) is not None:
                return rules.Wrong("miss", "misses", (str(pid),), p=float(p), miss=said)
        replays = await (
            await conn.execute(
                "SELECT message_id FROM pisa_night_replays WHERE night = %s AND NOT "
                "passed ORDER BY message_id",
                (night,),
            )
        ).fetchall()
        if replays:
            return rules.Wrong("replay", "replays", tuple(str(r[0]) for r in replays))
        return None

    async def _next(self, conn: AsyncConnection) -> rules.Next | None:
        row = await (
            await conn.execute(
                "SELECT id, source, question, about, hedonic_negative, neutral_title FROM pisa_unknowns "
                "WHERE state = 'open' AND on_shelf ORDER BY coalesce(interest, 0) DESC, opened_at, id LIMIT 1"
            )
        ).fetchone()
        if row is None:
            return None
        uid, source, question, about, negative, neutral = row
        return rules.Next(str(uid), _spoken_question(source, question, negative, neutral), str(about))

    async def story(self, lang: str) -> Reply | None:
        """ "Cosa hai fatto stanotte?": the story, or None (gate shut, no night: the model's)."""
        async with self._pool.connection() as conn:
            if not await self.gate(conn):
                return None
            night = await self.night(conn, lang)
        if night is None:
            return None
        claims = rules.story(night, lang)
        _log.info("voice3_story", claims=len(claims), proud=night.proud is not None)
        return Reply(rules.told(claims, lang), opens=("story", night.proud.id if night.proud else ""))

    # --- "Dammi tutto": the drawer -------------------------------------------------------------------------

    async def drawer(self, lang: str) -> rules.Drawer | None:
        """Everything the night holds that passes its checks (None: the gate is shut)."""
        async with self._pool.connection() as conn:
            if not await self.gate(conn):
                return None
            d = rules.Drawer()
            for idea, words in await self.ideas(conn, lang):
                grade = night_rules.cut(_grade(idea, lang), 80)
                d.ideas.append(
                    rules.Item(f"{words} — {grade}", "ideas", idea.id,
                               f"{night_rules.cut(idea.statement, 110)} — {grade}")
                )  # fmt: skip
            for item in await self.dead(conn, lang):
                d.dead.append(item)
            for uid, source, question, about, negative, neutral in await (
                await conn.execute(
                    "SELECT id, source, question, about, hedonic_negative, neutral_title FROM pisa_unknowns "
                    "WHERE state IN ('open', 'parked') ORDER BY on_shelf DESC, coalesce(interest, 0) DESC, "
                    "opened_at, id"
                )
            ).fetchall():
                said = _spoken_question(source, question, negative, neutral)
                text = said or (
                    f"Una domanda su {rules.about_words(about, lang)}." if lang.startswith("it")
                    else f"A question about {rules.about_words(about, lang)}."
                )  # fmt: skip
                screen = neutral if negative else question
                ok = bool(screen) and night_rules.clean(str(screen), about_him=True)
                d.questions.append(rules.Item(text, "shelf", str(uid), str(screen) if ok else None))
            for hid, item, question in await (
                await conn.execute(
                    "SELECT h.id, h.item, h.question FROM pisa_horizon h LEFT JOIN pisa_unknowns u "
                    "ON u.id = h.unknown_id WHERE u.id IS NULL OR u.state = 'open' "
                    "ORDER BY h.created_at DESC, h.id"
                )
            ).fetchall():
                there = await self._still_there(conn, str(item))
                q = night_rules.Question(str(hid), str(question), there)
                if there:  # the sheet's own rule: shown only while its dated item is still theirs
                    shown = str(question) if night_rules.question_shown(q) else None
                    d.scan.append(rules.Item(_scan_words(str(item), lang), "horizon", str(hid), shown))
        return d

    async def dead(self, conn: AsyncConnection, lang: str) -> list[rules.Item]:
        """Every dead idea whose killing line still passes its check, with that line's link host."""
        out: list[rules.Item] = []
        for iid, statement, kills, criterion, eid, market, kind, _seeds, problem in await (
            await conn.execute(
                "SELECT c.idea_id, i.statement, i.kill_criteria, c.criterion, c.evidence_id, i.market, "
                "i.kind, i.seeds, p.statement FROM pisa_idea_cemetery c JOIN pisa_ideas i ON i.id = "
                "c.idea_id LEFT JOIN pisa_idea_problems p ON p.id = i.problem_id ORDER BY "
                "c.killed_at DESC, c.idea_id"
            )
        ).fetchall():
            if eid is None:
                continue
            row = await (await conn.execute(_EVIDENCE + "WHERE id = %s", (eid,))).fetchone()
            evidence = _ev(row) if row else None
            kills = kills if isinstance(kills, list) else json.loads(kills or "[]")
            metric, op, value = "margin", "<", None
            if criterion is not None and 0 <= criterion < len(kills):
                k = kills[criterion]
                metric, op, value = str(k["metric"]), str(k["op"]), float(k["value"])
            outside = await self._outside(conn, iid)
            words = (
                statement if rules.speakable(statement, problem or "", outside_seeds=outside)
                else rules.idea_words(kind, market, lang)
            )  # fmt: skip
            killed = night_rules.Killed(str(iid), statement, metric, op, value, evidence)
            if not killed.shown() or evidence is None:  # its killing line passes its check again, now
                continue
            host = night_rules.host(evidence.final_url or evidence.url or "") if evidence.url else ""
            said = night_rules.killed_line(
                night_rules.Killed(str(iid), words, metric, op, value, evidence), lang
            )
            line = night_rules.killed_line(killed, lang) + (f" ({host})" if host else "")
            out.append(rules.Item(said, "cemetery", str(iid), line))
        return out

    async def _still_there(self, conn: AsyncConnection, item: str) -> bool:
        """A scan item's dated event or task is still in their records (the sheet's own rule)."""
        kind, _, ref = item.partition(":")
        if kind == "event":
            cal, _, eid = ref.partition(":")
            row = await (
                await conn.execute(
                    "SELECT 1 FROM calendar_events WHERE calendar_id = %s AND id = %s "
                    "AND status <> 'cancelled' AND starts_at >= %s",
                    (cal, eid, self._clock()),
                )
            ).fetchone()
            return row is not None
        if kind == "task":
            try:
                tid = uuid.UUID(ref)
            except ValueError:
                return False
            row = await (
                await conn.execute(
                    "SELECT 1 FROM tasks WHERE id = %s AND status = 'open' AND deleted_at IS NULL", (tid,)
                )
            ).fetchone()
            return row is not None
        return False

    # --- `stanotte(query)`: the one read tool ---------------------------------------------------------

    async def find(self, query: str, lang: str) -> dict[str, Any] | None:
        """Pisa's own night records matching `query`, as data with ids (None: the gate is shut). No outside
        text: no delta, page claim, quote, link title or window title; an idea's words only when it is
        speakable, a question only their own or Pisa's template."""
        story = await self.story(lang)
        d = await self.drawer(lang)
        if d is None:
            return None
        found = [
            {"id": i.id, "kind": i.table, "text": i.text}
            for i in d.items()
            if rules.matches(query, i.text, i.table)
        ]
        _log.info("voice3_find", found=len(found))
        return {"night": story.said if story is not None else "", "records": found}

    # --- the sparring partner --------------------------------------------------------------------------

    async def spar(self, idea_id: str, lang: str) -> Reply | None:
        today, now = self._owner.day(self._clock()), self._clock()
        try:
            iid = uuid.UUID(idea_id)
        except ValueError:
            return None
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT i.id, i.statement, i.mechanism, i.market, i.kind, i.seeds, p.statement, "
                    "i.kill_criteria, i.convergence, i.p_keep FROM pisa_ideas i "
                    "LEFT JOIN pisa_idea_problems p ON p.id = i.problem_id "
                    "WHERE i.id = %s AND i.state = ANY(%s)",
                    (iid, list(SHOWN)),
                )
            ).fetchone()
            if row is None:
                return None
            got = await self._idea(conn, row[:7], lang)
            if got is None:
                return None
            idea, words = got
            spar_id = uuid.uuid4()
            mine = await (
                await conn.execute(
                    "INSERT INTO pisa_spars (id, idea_id, day, created_at) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (idea_id, day) DO NOTHING RETURNING id",
                    (spar_id, iid, today, now),
                )
            ).fetchone()
            if mine is None:  # once per idea a day, decided by the row, never a read before it
                said = (
                    "Su questa idea abbiamo già fatto a pugni oggi: domani, se vuoi."
                    if lang.startswith("it")
                    else "We already fought over this idea today: tomorrow, if you like."
                )
                return Reply(said)
            kills = row[7] if isinstance(row[7], list) else json.loads(row[7] or "[]")
            kill = (str(kills[0]["metric"]), str(kills[0]["op"]), float(kills[0]["value"])) if kills else None
            sibling = await (
                await conn.execute(
                    "SELECT i.kind, i.market, c.idea_id FROM pisa_idea_cemetery c "
                    "JOIN pisa_ideas i ON i.id = c.idea_id WHERE i.kind = %s AND c.killed_at >= %s "
                    "AND i.id <> %s ORDER BY c.killed_at DESC LIMIT 1",
                    (idea.kind, now - timedelta(days=7), iid),
                )
            ).fetchone()
            facts = rules.SparFacts(
                idea.id, words, kill, lab=any(e.rung == "V3" for e in idea.evidence),
                simulated=sum(e.simulated for e in idea.evidence),
                sibling=(str(sibling[0]), str(sibling[1])) if sibling else None,
                convergence=int(row[8] or 1),
            )  # fmt: skip
            points = [{"kind": "idea", "table": "pisa_ideas", "id": idea.id}]
            if sibling is not None:
                points.append({"kind": "sibling", "table": "pisa_idea_cemetery", "id": str(sibling[2])})
            decision = uuid.uuid4()
            pisa_p = rules.journal_p(float(row[9]) if row[9] is not None else 0.5)
            await conn.execute(
                "INSERT INTO pisa_desk_decisions (id, dossier_id, domain, question, pisa_p, resolves_by, "
                "created_at) VALUES (%s, NULL, 'sparring', %s, %s, %s, %s)",
                (decision, f"spar:{idea.id}", pisa_p, today + rules.SPAR_RESOLVES, now),
            )
            await conn.execute(
                "UPDATE pisa_spars SET decision_id = %s, points = %s WHERE id = %s",
                (decision, json.dumps(points), spar_id),
            )
        _log.info("voice3_spar", idea_id=idea.id, decision_id=str(decision))
        return Reply(rules.spar_words(facts, lang), opens=("p", f"decision:{decision}"), asks=True)

    async def his_p(self, ref: str, p: float, lang: str) -> Reply | None:
        """Their p to Pisa's question (a spar's, a bet's): kept in the journal, once."""
        try:
            decision = uuid.UUID(ref.removeprefix("decision:"))
        except ValueError:
            return None
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE pisa_desk_decisions SET his_p = %s WHERE id = %s AND his_p IS NULL "
                    "AND outcome IS NULL RETURNING pisa_p",
                    (p, decision),
                )
            ).fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE pisa_bets SET his_p = %s WHERE decision_id = %s AND his_p IS NULL", (p, decision)
            )
        him, j = round(p * 100), round(float(row[0]) * 100)
        _log.info("voice3_his_p", decision_id=str(decision))
        said = (
            f"Segnato nel diario delle decisioni: tu {him}%, io {j}%. Vediamo chi ci prende."
            if lang.startswith("it")
            else f"In the decision journal: you {him}%, me {j}%. We'll see who's right."
        )
        return Reply(said)

    # --- la scommessa --------------------------------------------------------------------------------

    async def bet(self, text: str, lang: str) -> Reply | None:
        today, now = self._owner.day(self._clock()), self._clock()
        async with self._pool.connection() as conn:
            names = [str(r[0]) for r in await (await conn.execute("SELECT name FROM people")).fetchall()]
        b = rules.parse_bet(text, today, lang, names=names)
        if b is None:
            return None
        if b.refused is not None:
            _log.info("voice3_bet_refused", resolver=b.resolver)
            return Reply(b.refused)
        assert b.by is not None and b.resolver is not None
        place: dict[str, float] | None = None
        if b.resolver == "rain":
            async with self._pool.connection() as conn:
                home = await home_store.read(conn)
            if home is None or self._weather is None:
                said = ("Non so dove controllarla: dimmi prima dov'è casa." if lang.startswith("it")
                        else "I don't know where to check it: tell me where home is first.")  # fmt: skip
                return Reply(said)
            try:
                forecast = await self._weather.forecast(home.coord)
                day = next(d for d in forecast.days if d.date == b.by)
            # the forecast is Pisa's cache or its provider: a bet without p is no bet
            except Exception as exc:
                _log.info("voice3_bet_no_forecast", cause=exc.__class__.__name__)
                said = (
                    "Adesso non riesco a leggere le previsioni: riprova tra poco." if lang.startswith("it")
                    else "I can't read the forecast right now: try again shortly."
                )  # fmt: skip
                return Reply(said)
            rain = rules.rain_p(int(day.precip_prob))
            pisa_p = rain if b.yes else rules.journal_p(1 - rain)
            # about a kilometre is enough for the rain; their exact home is never kept with the bet
            place = {"lat": round(float(home.coord[0]), 2), "lon": round(float(home.coord[1]), 2)}
        else:
            async with self._pool.connection() as conn:
                out = await self._window(conn, b.subject, today)
            if out is not None:
                where = night_rules.host(out[1])
                said = (
                    f"È già uscito: lo vedo su {where}. Non è una scommessa." if lang.startswith("it")
                    else f"It's already out: I see it on {where}."
                )  # fmt: skip
                return Reply(said)
            pisa_p = 0.5
        bet_id, decision = uuid.uuid4(), uuid.uuid4()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO pisa_desk_decisions (id, dossier_id, domain, question, pisa_p, his_p, "
                "resolves_by, created_at) VALUES (%s, NULL, 'bet', %s, %s, %s, %s, %s)",
                (decision, f"bet:{bet_id}", pisa_p, b.his_p, b.by, now),
            )
            await conn.execute(
                "INSERT INTO pisa_bets (id, claim, resolver, by_day, yes, subject, place, his_p, pisa_p, "
                "decision_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (bet_id, b.claim, b.resolver, b.by, b.yes, list(b.subject),
                 json.dumps(place) if place else None, b.his_p, pisa_p, decision, now),
            )  # fmt: skip
        _log.info("voice3_bet", bet_id=str(bet_id), resolver=b.resolver)
        asks = b.his_p is None
        return Reply(
            rules.bet_words(b, pisa_p, lang, asks=asks),
            opens=("p", f"decision:{decision}") if asks else None,
            asks=asks,
            bet_id=bet_id,
        )

    async def link_message(self, device_id: uuid.UUID, bet_id: uuid.UUID) -> None:
        """The bet is their sentence: it goes with their message (the newest of their on this device)."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_bets SET message_id = (SELECT max(m.id) FROM messages m JOIN conversations c "
                "ON c.id = m.conversation_id WHERE c.device_id = %s AND m.role = 'user') WHERE id = %s",
                (device_id, bet_id),
            )

    async def _window(
        self, conn: AsyncConnection, subject: tuple[str, ...] | list[str], by: date
    ) -> tuple[str, str] | None:
        """A call in the window calendar matching the bet's name, read on its official page by
        `by`: (its id, its official URL)."""
        if not await (
            await conn.execute("SELECT to_regclass('public.pisa_desk_windows') IS NOT NULL")
        ).fetchone():
            return None
        for wid, title, url in await (
            await conn.execute(
                "SELECT id, title, url FROM pisa_desk_windows WHERE passed AND first_seen <= %s "
                "ORDER BY first_seen, id",
                (by,),
            )
        ).fetchall():
            if rules.window_matches(list(subject), str(title)) and str(url).startswith("https://"):
                return str(wid), str(url)
        return None

    # --- the night's step: bets and spars resolved --------------------------------------------------

    async def resolve(self, night: date) -> int:
        """Bets past their day, from their public source only (a link, or left unresolved); spars by the
        idea's fate in Pisa's records. The journal's own step (d4) then scores them."""
        now, done = self._clock(), 0
        async with self._pool.connection() as conn:
            bets = await (
                await conn.execute(
                    "SELECT id, resolver, by_day, yes, subject, place, decision_id FROM pisa_bets "
                    "WHERE state = 'open' AND by_day < %s ORDER BY by_day, id",
                    (night,),
                )
            ).fetchall()
        for bid, resolver, by, yes, subject, place, decision in bets:
            outcome: bool | None = None
            url: str | None = None
            if resolver == "rain" and self._observe is not None and place:
                where = place if isinstance(place, dict) else json.loads(place)
                try:
                    mm, url = await self._observe(float(where["lat"]), float(where["lon"]), by)
                    outcome = (mm >= rules.BET_RAIN_MM) == bool(yes)
                except Exception as exc:  # the provider: tried again next night
                    _log.info("voice3_resolve_skipped", bet_id=str(bid), cause=exc.__class__.__name__)
                    continue
            elif resolver == "window":
                async with self._pool.connection() as conn:  # read the morning after counts
                    got = await self._window(conn, list(subject or []), by + timedelta(days=1))
                if got is not None:
                    outcome, url = True, got[1]
                elif night <= by + rules.BET_WINDOW_GRACE:
                    continue  # a skipped scan: looked at again the next nights
            async with self._pool.connection() as conn, conn.transaction():
                if outcome is None or url is None:
                    if resolver == "window":  # RAIL: no public page proves it: never scored
                        await conn.execute(
                            "UPDATE pisa_bets SET state = 'unresolved', resolved_at = %s WHERE id = %s",
                            (now, bid),
                        )
                    continue
                await conn.execute(
                    "UPDATE pisa_bets SET state = %s, resolved_url = %s, resolved_at = %s WHERE id = %s",
                    ("won" if outcome else "lost", url, now, bid),
                )
                if decision is not None:
                    await conn.execute(
                        "UPDATE pisa_desk_decisions SET outcome = %s, resolved_at = %s WHERE id = %s "
                        "AND outcome IS NULL",
                        (outcome, now, decision),
                    )
            done += 1
        async with self._pool.connection() as conn, conn.transaction():
            done += await settle_by_idea(conn, "pisa_spars", night, now)
        async with self._pool.connection() as conn:  # a talk nobody closed ends at its fixed end
            await self._close_stale(conn, now)
        _log.info("voice3_resolved", bets=len(bets), resolved=done)
        return done

    async def _close_stale(self, conn: AsyncConnection, now: datetime) -> None:
        await conn.execute(
            "UPDATE pisa_quarti SET state = 'ended', ended_at = ends_at "
            "WHERE state = 'talking' AND ends_at <= %s",
            (now,),
        )

    # --- il quarto d'ora -----------------------------------------------------------------------------

    async def quarto_part(
        self, conn: AsyncConnection, now: datetime, lang: str, *, speaking: bool = False
    ) -> voice1_rules.Part | None:
        """The knock's part (the one way it is offered): their `quarto` on, the phone on foot now, fewer
        than two this week, and the night has something checked to bring. Spoken: the offer is kept."""
        if not await ritual_on(conn, "quarto") or not await self.gate(conn):
            return None
        day = now.astimezone(self._owner.zone).date()
        week = rules.week_of(day)
        if speaking:  # the count and the offer's row in one lock (two readings never make two)
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa_quarti'))")
        row = await (
            await conn.execute("SELECT count(*) FROM pisa_quarti WHERE week = %s", (week,))
        ).fetchone()
        offered_today = await (
            await conn.execute(
                "SELECT id FROM pisa_quarti WHERE offered_at >= %s ORDER BY offered_at DESC LIMIT 1",
                (local_day_bounds(day, self._owner.tz)[0],),
            )
        ).fetchone()
        if not rules.quarto_room(int(row[0]) if row else 0) and offered_today is None:
            return None
        if not await self._on_foot(conn, now) or not (await self._material(conn, lang))[3]:
            return None
        if speaking and offered_today is None:
            await conn.execute(
                "INSERT INTO pisa_quarti (id, week, offered_at) VALUES (%s, %s, %s)",
                (uuid.uuid4(), week, now),
            )
            _log.info("voice3_quarto_offered")
        return voice1_rules.Part("quarto", rules.quarto_offer(lang), "pisa_quarti", week.isoformat())

    async def _on_foot(self, conn: AsyncConnection, now: datetime) -> bool:
        device = await located_phone(conn)
        if device is None:
            return False
        fixes = await zone.fixes(conn, device, now - timedelta(minutes=10), now)
        return rules.on_foot(fixes, now)

    async def _material(self, conn: AsyncConnection, lang: str) -> Material:
        """The validated idea, the fascination (the topic they are learning, their words), the open question;
        their records."""
        ideas = await self.ideas(conn, lang)
        idea = ideas[0] if ideas else None
        topic = await (
            await conn.execute(
                "SELECT id, name FROM pisa_topics WHERE state = 'active' ORDER BY updated_at DESC LIMIT 1"
            )
        ).fetchone()
        nxt = await self._next(conn)
        refs = [{"table": "pisa_ideas", "id": idea[0].id}] if idea else []
        refs += [{"table": "pisa_topics", "id": str(topic[0])}] if topic else []
        refs += [{"table": "pisa_unknowns", "id": nxt.id}] if nxt and nxt.question else []
        fascination = (
            night_rules.cut(str(topic[1]), 80) if topic and night_rules.clean(str(topic[1])) else None
        )
        if not refs:
            return None, None, None, []
        return idea[1] if idea else None, fascination, nxt.question if nxt else None, refs

    async def quarto_start(self, device_id: uuid.UUID, lang: str) -> Reply | None:
        """Their "quarto d'ora" right after the knock's reading that offered it: the fixed end is set now, and
        the talk is this device's (its words are answered only on it, only until the end)."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await self._close_stale(conn, now)
            row = await (
                await conn.execute(
                    "UPDATE pisa_quarti SET state = 'talking', started_at = %s, ends_at = %s WHERE id = "
                    "(SELECT id FROM pisa_quarti WHERE state = 'offered' AND offered_at >= %s "
                    "ORDER BY offered_at DESC LIMIT 1) RETURNING id, ends_at",
                    (now, now + timedelta(minutes=rules.QUARTO_MINUTES), now - rules.QUARTO_START_WITHIN),
                )
            ).fetchone()
        if row is None:
            return None
        self._talks[device_id] = (row[0], row[1])
        ends = row[1].astimezone(self._owner.zone)
        _log.info("voice3_quarto_started", quarto_id=str(row[0]))
        return Reply(
            rules.quarto_start(f"{ends.hour}:{ends.minute:02d}", lang), opens=("quarto", str(row[0]))
        )

    async def quarto(self, device_id: uuid.UUID, kind: rules.Heard, lang: str) -> Reply | None:
        """ "Tocca a te" and "basta" inside this device's talk; at or past its fixed end, its first word
        closes it (once); no talk of this device's: None (voice1's "basta", the model's "e tu?")."""
        talk = self._talks.get(device_id)
        if talk is None:
            return None
        qid, ends = talk
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT ends_at FROM pisa_quarti WHERE id = %s AND state = 'talking' FOR UPDATE", (qid,)
                )
            ).fetchone()
            if row is None:
                self._talks.pop(device_id, None)
                return None
            if now >= ends or kind == "quarto_end":
                await conn.execute(
                    "UPDATE pisa_quarti SET state = 'ended', ended_at = %s WHERE id = %s",
                    (min(now, ends), qid),
                )
                self._talks.pop(device_id, None)
                _log.info("voice3_quarto_ended", quarto_id=str(qid), timed_out=now >= ends)
                return Reply(rules.quarto_end(lang, timed_out=now >= ends))
            idea, fascination, question, refs = await self._material(conn, lang)
            await conn.execute("UPDATE pisa_quarti SET refs = %s WHERE id = %s", (json.dumps(refs), qid))
        left = max(1, math.ceil((ends - now).total_seconds() / 60))
        idea_id = next((r["id"] for r in refs if r["table"] == "pisa_ideas"), "")
        return Reply(
            rules.quarto_turn(idea, fascination, question, left, lang), opens=("quarto_turn", idea_id)
        )


def _spoken_question(source: str, question: str, negative: bool, neutral: str | None) -> str | None:
    """A shelf question Pisa may say: their own words, or Pisa's template for a confident miss; an unpleasant
    one by its neutral title. Anything a model wrote from outside pages (a topic's, a horizon's) is said only
    as what it is about."""
    if negative:
        return neutral or None
    if source in ("his_question", "miss") and night_rules.clean(question):
        return question
    return None


def _grade(idea: night_rules.Idea, lang: str) -> str:
    facts: dict[str, float] = {}
    if (m := idea.market_line()) is not None and m.number is not None:
        facts["buyers"] = m.number
    facts |= idea.economics()
    facts["competitors"] = float(len(idea.competitors()))
    return night_rules.grade_words(lang, [e.rung for e in idea.evidence if not e.simulated], facts)


def _scan_words(item: str, lang: str) -> str:
    """A scan item by its kind (its question is a model's words about one of their items: never shown)."""
    kind = item.partition(":")[0]
    if lang.startswith("it"):
        return {
            "event": "Una domanda su un tuo impegno in agenda",
            "task": "Una domanda su una tua attività",
        }.get(kind, "Una domanda su una tua cosa in calendario")
    return {"event": "A question about an event of yours", "task": "A question about a task of yours"}.get(
        kind, "A question about something of yours"
    )
