"""The nightly incubation and the Validation Ladder, as steps of the frame's night.

**Shadow only**: nothing here reaches them (no notification, message, candidate, task or job) and
nothing here enters any prompt: no turn, intake, profile, grounding or style path reads `pisa_idea*` (only
this module names them). The morning sheet is another engine's; this one stops at `ready`.

The steps (`night.STEPS`, after the predictions; the frame's stop, budget `night` and search gate hold over
each):
- **i1_prepare**: their problems from their own words ("Pisa, problema: …") and records (a goal they said or
  confirmed), their fragments ("Pisa, idea: …"); ≤ 5 problems prepared ("why it's hard" in two lines, three
  reframes); one search each for the world's deltas.
- **i3_incubate**: far association under the constraint deck and a far persona, seeded by their fragments
  and the deltas; dedup at cosine 0.8 (the convergence count) and distance; the fit gate; a second cycle on
  the survivors' titles and a one-word verdict (k = 2, fixed), killed ideas as parents too. Lineage edges
  from the first night.
- **i7_select**: a Swiss tournament filters to ≤ 8 with the 70/20/10 quota (it ranks nothing for them); the
  three critics write typed kill criteria **before any evidence**.
- **i9_validate**: V0 desk, V1 signals, V2 numbers in code, V4 a simulated panel (labelled), V5 conscience;
  each evidence line checked by code (the link was fetched; the number is on the fetched page). A kill
  criterion that fires sends the idea to the cemetery. ≤ €0.10 of calls an idea and per-rung caps; a rung
  that would need a seventh search makes the idea a `question`. V3 (the Night Lab) is a typed hook, off.
- **i10_choose**: ≤ 3 ideas whose every evidence line passed become `ready` (≤ 1 far); the stepping-stone map
  (markets x mechanisms) updated in shadow.

Logs carry ids and counts only.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain import ideas as brain
from pisa.domain import ideas as rules
from pisa.domain import profile as profile_rules
from pisa.domain import taint
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services.night_step import Capped, NightStep, Read, Stopped, no_read

if TYPE_CHECKING:
    from pisa.services.night import NightService

_log = get_logger("pisa.ideas")
LabHook = Callable[[rules.LabRequest], Awaitable[None]]
EST_CALL_EUR = Decimal("0.005")  # what one ladder call is assumed to cost before it runs (the cap's margin)
DELTA_DAYS = 14


class IdeasService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        night: "NightService",
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        think: brain.Think | None = None,
        embed: brain.Embed | None = None,
        read: Read = no_read,
        lab: LabHook | None = None,
    ) -> None:
        self._pool = pool
        self._night = night
        self._owner = owner
        self._clock = clock
        self._think = think
        self._embed = embed
        # the frame's stop, call, read and search
        self.step = NightStep(pool, name="ideas", trace="idea", owner=owner, night=night, read=read)
        self._lab = lab  # RAIL: never called while `rules.LAB_ON` is False

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {
            "i1_prepare": self.prepare,
            "i3_incubate": self.incubate,
            "i7_select": self.select,
            "i9_validate": self.validate,
            "i10_choose": self.choose,
        }

    # --- the gates every call and search goes through ---

    async def _ask[T](
        self, task: str, instructions: str, prompt: str, out: type[T], *, trace: str
    ) -> T | None:
        """One model call, through the frame (`NightStep.ask`: its stop, its failure policy)."""
        return await self.step.ask(self._think, task, instructions, prompt, out, trace=trace)

    async def _his_names(self) -> list[taint.Personal]:
        """The proper nouns of their own problems and fragments (a client, a town Pisa never stored as a
        contact), handed to the frame's filter as names."""
        async with self._pool.connection() as conn:
            texts = [t for (t,) in await (
                await conn.execute(
                    "SELECT statement FROM pisa_idea_problems WHERE state <> 'closed' "
                    "UNION ALL SELECT text FROM pisa_idea_fragments"
                )
            ).fetchall()]  # fmt: skip
        return [taint.Personal(n) for n in sorted({n for t in texts for n in rules.proper_nouns(t)})]

    async def _search(self, night: date, query: str, kind: str) -> list[dict[str, str]] | None:
        """RAIL: only through the frame's `search` (its filter, its 40, its stop); the ideas' own
        share of the night is 30, counted on the frame's own rows. The model's query first loses every
        capitalised word that is not generic, and their problems' proper nouns go to the filter as names.
        None when refused."""
        if await self.step.sent(night, "i") >= rules.NIGHT_SEARCHES:
            return None
        return await self.step.search(
            rules.generic(query), kind=kind, extra=await self._his_names(), link=rules.link
        )

    async def _vectors(self, texts: Sequence[str]) -> list[list[float]]:
        if self._embed is not None and texts:
            try:
                made = await self._embed(texts)
            except PisaError as err:  # the provider said no: the local vectors stand in
                if err.code == "budget_exceeded":
                    raise
                _log.warning("ideas_embed_failed", code=err.code)
                made = None
            except Exception:  # anything else is a bug or a broken provider: the local vectors, and its trace
                _log.exception("ideas_embed_failed")
                made = None
            if made is not None and len(made) == len(texts):
                return made
        return [rules.local_vector(t) for t in texts]

    # --- i1: problems and fragments from their own words and records; prepare ≤ 5 ---

    async def harvest(self, conn: AsyncConnection, now: datetime) -> tuple[int, int]:
        """Their problems and fragments of the last week, from their own messages; their goals from their
        profile (said or confirmed, never sensitive, never inferred). Returns (problems, fragments) added."""
        problems = fragments = 0
        rows = await (
            await conn.execute(
                "SELECT id, content, created_at FROM messages WHERE role = 'user' AND created_at >= %s "
                "AND created_at < %s ORDER BY id",
                (now - timedelta(days=rules.WEEK_DAYS), now),
            )
        ).fetchall()
        for message_id, content, at in rows:
            said = rules.problem(content or "")
            if said is not None and profile_rules.is_sensitive("goal", said):  # never their health etc.
                said = None
            if said is not None:
                made = await (
                    await conn.execute(
                        "INSERT INTO pisa_idea_problems (id, source, ref, message_id, statement, created_at, "
                        "touched_at) VALUES (%s, 'his_words', %s, %s, %s, %s, %s) "
                        "ON CONFLICT (source, ref) DO NOTHING RETURNING id",
                        (uuid.uuid4(), str(message_id), message_id, said, at, at),
                    )
                ).fetchone()
                problems += made is not None
            idea = rules.fragment(content or "")
            if idea is not None and profile_rules.is_sensitive("goal", idea):
                idea = None
            if idea is not None:
                made = await (
                    await conn.execute(
                        "INSERT INTO pisa_idea_fragments (id, message_id, text, said_at) VALUES (%s, "
                        "%s, %s, %s) "
                        "ON CONFLICT (message_id) DO NOTHING RETURNING id",
                        (uuid.uuid4(), message_id, idea, at),
                    )
                ).fetchone()
                fragments += made is not None
        for claim_id, statement, updated in await (
            await conn.execute(
                "SELECT id::text, statement, updated_at FROM profile_claims WHERE kind = 'goal' "
                "AND state = 'active' AND NOT sensitive AND provenance IN ('said', 'confirmed')"
            )
        ).fetchall():
            made = await (
                await conn.execute(
                    "INSERT INTO pisa_idea_problems (id, source, ref, claim_id, statement, created_at, "
                    "touched_at) "
                    "VALUES (%s, 'his_record', %s, %s::uuid, %s, %s, %s) ON CONFLICT (source, ref) "
                    "DO UPDATE SET "
                    "statement = excluded.statement, touched_at = greatest(pisa_idea_problems.touched_at, "
                    "excluded.touched_at) RETURNING (xmax = 0)",
                    (uuid.uuid4(), claim_id, claim_id, statement, now, updated),
                )
            ).fetchone()
            problems += bool(made and made[0])
        # a goal they retired, or one now marked sensitive, is closed: Pisa stops working on it
        await conn.execute(
            "UPDATE pisa_idea_problems p SET state = 'closed' WHERE p.source = 'his_record' AND p.state "
            "<> 'closed' "
            "AND NOT EXISTS (SELECT 1 FROM profile_claims c WHERE c.id::text = p.ref AND c.kind = 'goal' "
            "AND c.state = 'active' AND NOT c.sensitive AND c.provenance IN ('said', 'confirmed'))"
        )
        rest = now - timedelta(days=rules.REST_DAYS)
        await conn.execute(
            "UPDATE pisa_idea_problems SET state = CASE WHEN touched_at < %s THEN 'resting' ELSE 'open' END "
            "WHERE state IN ('open', 'resting')",
            (rest,),
        )
        return problems, fragments

    async def _link_fragments(self, conn: AsyncConnection) -> None:
        """Each new fragment to its nearest open problem."""
        loose = await (
            await conn.execute("SELECT id, text FROM pisa_idea_fragments WHERE problem_id IS NULL")
        ).fetchall()
        open_ = await (
            await conn.execute(
                "SELECT id, statement FROM pisa_idea_problems WHERE state = 'open' ORDER BY id"
            )
        ).fetchall()
        if not loose or not open_:
            return
        vecs = await self._vectors([t for _, t in loose] + [s for _, s in open_])
        frag_v, prob_v = vecs[: len(loose)], vecs[len(loose) :]
        for (fid, _), v in zip(loose, frag_v, strict=True):
            best = max(range(len(open_)), key=lambda i: rules.cosine(v, prob_v[i]))
            await conn.execute(
                "UPDATE pisa_idea_fragments SET problem_id = %s WHERE id = %s", (open_[best][0], fid)
            )

    async def prepare(self, night: date) -> int:
        """≤ 5 open problems, those with their new fragments and their latest words first; each one's
        "why it's
        hard", three reframes, and one search for the world's deltas."""
        now = self._clock()
        async with self._pool.connection() as conn:
            added, fragments = await self.harvest(conn, now)
            await self._link_fragments(conn)
            picked = await (
                await conn.execute(
                    "SELECT p.id, p.statement FROM pisa_idea_problems p WHERE p.state = 'open' "
                    "ORDER BY EXISTS (SELECT 1 FROM pisa_idea_fragments f WHERE f.problem_id = p.id "
                    "AND f.said_at >= %s) DESC, p.touched_at DESC, p.id LIMIT %s",
                    (now - timedelta(days=rules.WEEK_DAYS), rules.PROBLEMS_MAX),
                )
            ).fetchall()
        prepared = 0
        try:
            for pid, statement in picked:
                out = await self._ask(
                    "prepare", brain.PREPARE, block_owner(statement), brain.Prepared, trace=f"problem-{pid}"
                )
                if out is None:
                    continue
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE pisa_idea_problems SET hard = %s, reframes = %s, "
                        "prepared_on = %s WHERE id = %s",
                        (out.hard, Jsonb(out.reframes[: rules.REFRAMES]), night, pid),
                    )
                prepared += 1
                await self._deltas(night, out.delta_query, pid)
        except Stopped:
            pass
        _log.info("ideas_prepared", problems=prepared, added=added, fragments=fragments)
        return prepared

    async def _deltas(self, night: date, query: str, pid: uuid.UUID) -> None:
        results = await self._search(night, query, "i1_delta")
        if not results:
            return
        out = await self._ask(
            "deltas", brain.DELTAS, brain.results_block(results), brain.Deltas, trace=f"problem-{pid}"
        )
        if out is None:
            return
        async with self._pool.connection() as conn:
            for d in out.items:
                if 0 <= d.index < len(results):  # an index the results have; never a link the model wrote
                    await conn.execute(
                        "INSERT INTO pisa_idea_deltas (id, night, kind, title, url, seen_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING",
                        (uuid.uuid4(), night, d.kind, d.title, results[d.index]["url"], self._clock()),
                    )

    # --- i3: incubation with forced diversity, k = 2 ---

    async def incubate(self, night: date) -> int:
        """Incubation over tonight's prepared problems: cycle 1 from the reframes, cycle 2 from the survivors'
        titles (k = 2, fixed). Returns the ideas kept."""
        async with self._pool.connection() as conn:
            problems = await (
                await conn.execute(
                    "SELECT id, statement, reframes FROM pisa_idea_problems WHERE "
                    "prepared_on = %s ORDER BY id",
                    (night,),
                )
            ).fetchall()
            fragments = await (
                await conn.execute(
                    "SELECT id, problem_id, text FROM pisa_idea_fragments ORDER BY said_at DESC"
                )
            ).fetchall()
            deltas = await (
                await conn.execute(
                    "SELECT id, kind, title FROM pisa_idea_deltas WHERE night > %s ORDER BY seen_at "
                    "DESC LIMIT 20",
                    (night - timedelta(days=DELTA_DAYS),),
                )
            ).fetchall()
        kept = 0
        try:
            for cycle in range(1, rules.K + 1):
                for pid, statement, reframes in problems:
                    if cycle == 1:
                        made = await self._cycle_one(night, pid, list(reframes or []), fragments, deltas)
                    else:
                        made = await self._cycle_two(night, pid)
                    kept += await self._keep(night, pid, statement, made, fragments)
        except Stopped:
            pass
        await self._band(night)
        _log.info("ideas_incubated", kept=kept, k=rules.K)
        return kept

    async def _cycle_one(
        self, night: date, pid: uuid.UUID, reframes: list[str], fragments: list[Any], deltas: list[Any]
    ) -> list[tuple[brain.Candidate, dict[str, Any], list[str], int]]:
        made: list[tuple[brain.Candidate, dict[str, Any], list[str], int]] = []
        others = [f for f in fragments if f[1] != pid]  # their fragment from a *different* problem
        for r, reframe in enumerate(reframes[: rules.REFRAMES]):
            seed = f"{night}:{pid}:{r}"
            ((card_id, card),) = rules.draw(seed + ":card", rules.DECK)
            ((persona_id, persona),) = rules.draw(seed + ":persona", rules.PERSONAS)
            seeds: dict[str, Any] = {"reframe": r, "card": card_id, "persona": persona_id}
            levers = [*rules.LEVERS_ALWAYS, f"card:{card_id}", f"persona:{persona_id}"]
            material = [f"Reframed problem: {reframe}", f"Constraint card: {card}", f"Far persona: {persona}"]
            if others:
                fid, _, text = others[r % len(others)]
                seeds["fragment"] = str(fid)
                levers.append("fragment")
                material.append(block_owner(text, what="one of their own ideas, from another problem"))
            if deltas:
                did, kind, title = deltas[r % len(deltas)]
                seeds["delta"] = str(did)
                levers.append("delta")
                material.append(
                    brain.block("web", f"{kind}: {title}", "a new thing in the world, outside text")
                )
            out = await self._ask(
                "generate", brain.GENERATE, "\n\n".join(material), brain.Candidates, trace=f"problem-{pid}"
            )
            for c in (out.items if out else [])[: rules.PER_CALL]:
                made.append((c, seeds, levers, 1))
        return made

    async def _cycle_two(
        self, night: date, pid: uuid.UUID
    ) -> list[tuple[brain.Candidate, dict[str, Any], list[str], int]]:
        """The second cycle: the survivors' titles only, each with a one-word verdict, and up to 3 killed
        ideas as parents: never a critic's reasons."""
        async with self._pool.connection() as conn:
            parents = await (
                await conn.execute(
                    "(SELECT id, statement, 'keep' FROM pisa_ideas WHERE night = %s AND problem_id = %s "
                    "AND cycle = 1 ORDER BY created_at, id) UNION ALL "
                    "(SELECT i.id, i.statement, 'drop' FROM pisa_ideas i JOIN pisa_idea_cemetery c ON "
                    "c.idea_id = i.id "
                    "WHERE i.problem_id = %s ORDER BY c.killed_at DESC LIMIT %s)",
                    (night, pid, pid, rules.CEMETERY_PARENTS),
                )
            ).fetchall()
        if not parents:
            return []
        titles = "\n".join(f"[{i}] {s} - {v}" for i, (_, s, v) in enumerate(parents))
        out = await self._ask(
            "evolve",
            brain.EVOLVE,
            brain.idea_block(titles, "idea titles"),
            brain.Candidates,
            trace=f"problem-{pid}",
        )
        made: list[tuple[brain.Candidate, dict[str, Any], list[str], int]] = []
        for c in (out.items if out else [])[: rules.PER_CALL]:
            ids = [str(parents[i][0]) for i in c.parents if 0 <= i < len(parents)]
            made.append((c, {"parents": ids}, [*rules.LEVERS_ALWAYS, "evolve"], 2))
        return made

    async def _keep(
        self,
        night: date,
        pid: uuid.UUID,
        statement: str,
        made: list[tuple[brain.Candidate, dict[str, Any], list[str], int]],
        fragments: list[Any],
    ) -> int:
        """The fit gate, dedup at cosine 0.8 (against every idea Pisa already has, too), the
        distance; the survivors stored with their levers, convergence and lineage."""
        fit = [m for m in made if rules.fit(m[0].statement, m[0].mechanism)]
        if not fit:
            return 0
        async with self._pool.connection() as conn:
            known = [
                list(v)
                for (v,) in await (
                    await conn.execute("SELECT vec FROM pisa_ideas WHERE vec IS NOT NULL")
                ).fetchall()
            ]
        vecs = await self._vectors([f"{c.statement}. {c.mechanism}" for c, *_ in fit])
        mine = await self._vectors([statement, *[t for _, _, t in fragments]])
        clusters = rules.dedup(vecs, known)
        now = self._clock()
        kept = 0
        async with self._pool.connection() as conn:
            for cluster in clusters:
                i = cluster[0]
                c, seeds, levers, cycle = fit[i]
                iid = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO pisa_ideas (id, night, problem_id, statement, mechanism, market, kind, "
                    "cycle, seeds, levers, vec, distance, convergence, verdict, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'keep', %s, %s)",
                    (
                        iid,
                        night,
                        pid,
                        c.statement,
                        c.mechanism,
                        c.market,
                        c.kind,
                        cycle,
                        Jsonb(seeds | {"probability": c.probability}),
                        [*levers, "dedup"],
                        vecs[i],
                        rules.distance(vecs[i], known + mine),
                        len(cluster),
                        now,
                        now,
                    ),
                )
                parents = seeds.get("parents") or []
                for parent in parents:
                    await conn.execute(
                        "INSERT INTO pisa_idea_lineage (parent, child, kind, night) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            uuid.UUID(parent),
                            iid,
                            "variation" if len(parents) == 1 else "recombination",
                            night,
                        ),
                    )
                known.append(vecs[i])
                kept += 1
        return kept

    async def _band(self, night: date) -> None:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, distance FROM pisa_ideas WHERE night = %s AND distance IS "
                    "NOT NULL ORDER BY id",
                    (night,),
                )
            ).fetchall()
            for (iid, _), band in zip(rows, rules.bands([float(d) for _, d in rows]), strict=True):
                await conn.execute(
                    "UPDATE pisa_ideas SET band = %s, levers = array_append(array_remove(levers, 'quota'), "
                    "'quota') WHERE id = %s",
                    (band, iid),
                )

    # --- i7: the tournament filters to ≤ 8; the critics write kill criteria first ---

    async def select(self, night: date) -> int:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, statement, band FROM pisa_ideas WHERE night = %s AND state = 'incubating' "
                    "ORDER BY convergence DESC, distance DESC NULLS LAST, id LIMIT %s",
                    (night, rules.POOL_MAX),
                )
            ).fetchall()
        if not rows:
            return 0
        chosen = 0
        try:
            ranked = await self._tournament(night, rows)
            picked = rules.quota(ranked, {i: rows[i][2] for i in range(len(rows))}, rules.FILTER_MAX)
            for i in picked:
                chosen += await self._critique(rows[i][0], rows[i][1])
        except Stopped:
            pass
        _log.info("ideas_selected", pool=len(rows), validating=chosen)
        return chosen

    async def _tournament(self, night: date, rows: list[Any]) -> list[int]:
        """A Swiss tournament, pairwise, 5 rounds, A/B order alternated, statements length-capped. It
        filters; nothing it says reaches them."""
        scores = dict.fromkeys(range(len(rows)), 0)
        if len(rows) <= rules.FILTER_MAX:
            return list(range(len(rows)))
        met: set[frozenset[int]] = set()
        for round_ in range(rules.ROUNDS):
            for n, (a, b) in enumerate(rules.swiss_pairs(scores, met)):
                met.add(frozenset((a, b)))
                first, second = (a, b) if (n + round_) % 2 == 0 else (b, a)
                prompt = brain.idea_block(
                    f"A: {rows[first][1][:300]}\nB: {rows[second][1][:300]}", "two ideas"
                )
                out = await self._ask(
                    "judge", brain.JUDGE, prompt, brain.Judged, trace=f"night-{night}-judge"
                )
                if out is not None:
                    scores[first if out.better == "A" else second] += 1
        return sorted(scores, key=lambda i: (-scores[i], i))

    async def _critique(self, iid: uuid.UUID, statement: str) -> int:
        """Kill criteria written before any evidence (`kills_at`); no criterion, no validation."""
        out = await self._ask(
            "critique", brain.CRITIQUE, brain.idea_block(statement), brain.Critique, trace=f"idea-{iid}-kill"
        )
        if out is None:
            return 0
        kills = [k.model_dump() for k in [*out.skeptic, *out.analogist, *out.owner]]
        if not kills:
            return 0
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_ideas SET kill_criteria = %s, kills_at = %s, p_keep = %s, state = 'validating', "
                "updated_at = %s WHERE id = %s AND state = 'incubating'",
                (Jsonb(kills), self._clock(), min(max(out.p_keep, 0.01), 0.99), self._clock(), iid),
            )
        return 1

    # --- i9: the ladder, V0-V2, V4 (simulated), V5; V3 a hook ---

    async def _call[T](
        self, iid: uuid.UUID, rung: str, task: str, instructions: str, prompt: str, out: type[T]
    ) -> T | None:
        """RAIL: ≤ €0.10 of calls an idea, and each rung's own cap, checked in code
        before every call (the next call assumed to cost as much as the idea's dearest so far); over it, the
        idea waits with its evidence so far."""
        await self.step.within(f"idea-{iid}-%", rules.IDEA_CAP_EUR, EST_CALL_EUR)
        cap = rules.RUNG_CAP_EUR.get(rung)
        if cap is not None:
            on_rung, _ = await self.step.spent(f"idea-{iid}-{rung}")
            if on_rung >= cap:  # a rung's own cap (V2 €0.02, V4 €0.03, V5 €0.005): no call past it
                raise Capped
        return await self._ask(task, instructions, prompt, out, trace=f"idea-{iid}-{rung}")

    async def _evidence(
        self,
        iid: uuid.UUID,
        rung: str,
        claim: str,
        check: str,
        *,
        passed: bool | None,
        url: str | None = None,
        number: float | None = None,
        run: Any = None,
        simulated: bool = False,
        final: str | None = None,
    ) -> uuid.UUID:
        eid = uuid.uuid4()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pisa_idea_evidence (id, idea_id, rung, claim, url, final_url, number, run, "
                "check_kind, passed, simulated, checked_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    eid,
                    iid,
                    rung,
                    claim[:300],
                    url,
                    final,
                    number,
                    Jsonb(run) if run is not None else None,
                    check,
                    passed,
                    simulated,
                    self._clock(),
                ),
            )
        return eid

    async def _page(self, url: str) -> tuple[str, str] | None:
        """The cited page, fetched by Pisa (`read`): (its final URL, its text), or None when the link doesn't
        resolve or its redirects left the cited site (a parked domain is no evidence)."""
        if not rules.link(url):
            return None
        got = await self.step.read(url)
        if not got or not rules.same_site(url, got[0]):
            return None
        return got

    async def _rung_search(
        self, night: date, iid: uuid.UUID, queries: list[str], kind: str
    ) -> list[dict[str, str]] | None:
        """A rung's searches, in order, until one finds something: ≤ 6 (a seventh means "not provable
        cheaply"). [] when all six found nothing; None when the night refused (the cap, the stop)."""
        for q in queries[: rules.RUNG_SEARCHES]:
            found = await self._search(night, q, kind)
            async with self._pool.connection() as conn:
                await conn.execute("UPDATE pisa_ideas SET searches = searches + 1 WHERE id = %s", (iid,))
            if found is None:
                return None
            if found:
                return found
        return []

    async def validate(self, night: date) -> int:
        """The ladder for ≤ 8 validating ideas, oldest kill criteria first. Returns the ideas that went up a
        rung."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, statement, mechanism, kill_criteria FROM pisa_ideas WHERE state = "
                    "'validating' AND kills_at IS NOT NULL "
                    "AND ((rung IS NULL AND waiting IS NULL) OR waiting = 'evidence') "
                    ""  # 'cap' is for good
                    "ORDER BY waiting NULLS FIRST, kills_at, id LIMIT %s",
                    (rules.FILTER_MAX,),
                )
            ).fetchall()
        moved = 0
        for iid, statement, mechanism, kills in rows:
            try:
                moved += await self._ladder(night, iid, f"{statement}\nMechanism: {mechanism}", kills)
            except Capped:
                await self._wait(iid, "cap")
            except Stopped:
                break
        _log.info("ideas_validated", tried=len(rows), moved=moved)
        return moved

    async def _wait(self, iid: uuid.UUID, why: str, state: str = "validating") -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_ideas SET waiting = %s, state = %s, updated_at = %s WHERE id = %s",
                (why, state, self._clock(), iid),
            )

    async def _kill(
        self,
        iid: uuid.UUID,
        kills: list[rules.Kill],
        facts: dict[str, float],
        evidence: uuid.UUID | None,
        criterion: int | None,
    ) -> int:
        """A fired kill criterion (or numbers that don't work): the cemetery, with what would revive it."""
        metric = kills[criterion].metric if criterion is not None else "margin"
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_ideas SET state = 'killed', waiting = NULL, updated_at = %s WHERE id = %s",
                (self._clock(), iid),
            )
            await conn.execute(
                "INSERT INTO pisa_idea_cemetery (idea_id, killed_at, criterion, evidence_id, revival) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (idea_id) DO NOTHING",
                (iid, self._clock(), criterion, evidence, f"new_evidence:{metric}"),
            )
        _log.info("idea_killed", idea=str(iid), criterion=criterion, facts=len(facts))
        return 0

    async def _ladder(self, night: date, iid: uuid.UUID, idea: str, raw_kills: Any) -> int:
        kills = [rules.Kill(k["metric"], k["op"], float(k["value"]), k.get("said", "")) for k in raw_kills]
        facts: dict[str, float] = {}
        passed: list[str] = []
        last: uuid.UUID | None = None

        def fired() -> int | None:
            return rules.first_fired(kills, facts)

        plan = await self._call(iid, "V0", "plan", brain.PLAN, brain.idea_block(idea), brain.Plan)
        if plan is None:
            return 0
        async with self._pool.connection() as conn:  # a retried ladder starts from no evidence (after
            await conn.execute("DELETE FROM pisa_idea_evidence WHERE idea_id = %s", (iid,))  # its first call)
        if not plan.v0:  # no search, no V0: "it doesn't exist" is never said without looking
            await self._mark(iid, passed, "not_provable", state="question")
            return 0
        # V0 desk: does it exist? Each competitor's page fetched; a base rate's number found on its page
        found = await self._rung_search(night, iid, plan.v0, "i9_v0")
        if found is None:
            await self._wait(iid, "evidence")
            return 0
        ok = True
        same = 0
        if not found:  # looked and found nothing: its own V0 line, the searches as its run
            last = await self._evidence(
                iid, "V0", "nessun prodotto uguale trovato", "run", passed=True, run={"searches_found": 0}
            )
        else:
            read = await self._call(
                iid, "V0", "v0_read", brain.V0_READ,
                f"{brain.idea_block(idea)}\n\n{brain.results_block(found)}", brain.V0Read,
            )  # fmt: skip
            if read is None:  # results unread are no "it doesn't exist"
                await self._wait(iid, "evidence")
                return 0
            for c in read.competitors:
                if not 0 <= c.index < len(found):
                    continue
                url = found[c.index]["url"]
                got = await self._page(url)
                last = await self._evidence(
                    iid, "V0", f"esiste già: {c.name}", "link", passed=got is not None, url=url,
                    final=got[0] if got else None,
                )  # fmt: skip
                ok &= got is not None
                same += got is not None and c.same
            if not read.competitors:
                last = await self._evidence(
                    iid, "V0", "nessun prodotto uguale tra i risultati", "run", passed=True,
                    run={"searches_found": len(found)},
                )  # fmt: skip
            if read.base_rate and 0 <= read.base_rate.index < len(found):
                url = found[read.base_rate.index]["url"]
                got = await self._page(url)
                claim = read.base_rate.reference_class
                good = got is not None and rules.on_page(read.base_rate.value, got[1], claim)
                last = await self._evidence(
                    iid, "V0", f"tasso di base: {claim}", "number", passed=good, url=url,
                    number=read.base_rate.value, final=got[0] if got else None,
                )  # fmt: skip
                ok &= good
        # RAIL: a link that doesn't resolve, a number not on its page: never shown, stays validating
        if not ok:
            await self._wait(iid, "evidence")
            return 0
        facts["competitors"] = same
        passed.append("V0")
        if (k := fired()) is not None:
            return await self._kill(iid, kills, facts, last, k)
        # V1 signals: numbers on demand, each found on its fetched page
        found = await self._rung_search(night, iid, plan.v1, "i9_v1")
        if found is None:
            await self._mark(iid, passed, "evidence")
            return 1
        if not found:  # six searches and nothing: not provable cheaply, a question for them
            await self._mark(iid, passed, "not_provable", state="question")
            return 1
        read1 = await self._call(
            iid,
            "V1",
            "v1_read",
            brain.V1_READ,
            f"{brain.idea_block(idea)}\n\n{brain.results_block(found)}",
            brain.V1Read,
        )
        buyers: tuple[float, uuid.UUID] | None = None
        ok = True
        for f in read1.numbers if read1 else []:
            if not 0 <= f.index < len(found):
                continue
            url = found[f.index]["url"]
            got = await self._page(url)
            good = got is not None and rules.on_page(f.value, got[1], f.claim)
            last = await self._evidence(
                iid,
                "V1",
                f.claim,
                "number",
                passed=good,
                url=url,
                number=f.value,
                final=got[0] if got else None,
            )
            ok &= good
            if good and f.buyers and buyers is None:
                buyers = (f.value, last)
        if not ok:
            await self._mark(iid, passed, "evidence")
            return 1
        if buyers is None:
            await self._mark(iid, passed, "not_provable", state="question")
            return 1
        facts["buyers"] = buyers[0]
        passed.append("V1")
        if (k := fired()) is not None:
            return await self._kill(iid, kills, facts, last, k)
        # V2 numbers: computed in code, every fact input linked to its V1 line
        v2 = await self._call(iid, "V2", "v2", brain.V2, brain.idea_block(idea), brain.V2Inputs)
        if v2 is None:
            await self._mark(iid, passed, "evidence")
            return 1
        inputs = rules.Inputs(buyers[0], v2.price_eur_month, v2.uptake, v2.unit_cost_eur_month, v2.cac_eur)
        if not rules.sane(inputs):
            await self._mark(iid, passed, "evidence")
            return 1
        out = rules.economics(inputs)
        last = await self._evidence(
            iid,
            "V2",
            "conti in codice",
            "run",
            passed=True,
            run={
                "inputs": inputs.__dict__,
                "linked": {"buyers": str(buyers[1])},
                "assumed": ["price_eur_month", "uptake", "unit_cost_eur_month", "cac_eur"],
                "output": out,
            },
        )
        facts |= {k: out[k] for k in ("revenue_year_eur", "margin", "payback_months")}
        if out["margin"] <= 0:
            return await self._kill(iid, kills, facts, last, None)
        passed.append("V2")
        if (k := fired()) is not None:
            return await self._kill(iid, kills, facts, last, k)
        # V3, the Night Lab: a typed hook, off until the user sets the lab's budget
        request = rules.LabRequest(str(iid), idea[:300], tuple(kills))
        if rules.LAB_ON and self._lab is not None:  # pragma: no cover
            await self._lab(request)
        # V4 simulate: labelled simulated, never validation
        panel = await self._call(iid, "V4", "panel", brain.PANEL, brain.idea_block(idea), brain.Panel)
        if panel is not None:
            await self._evidence(
                iid,
                "V4",
                "pannello simulato (non è una prova)",
                "simulated",
                passed=None,
                simulated=True,
                run={"objections": panel.objections, "would_try_of_10": panel.would_try_of_10},
            )
        # V5 conscience: a fixed rung; Pisa advises, never vetoes
        con = await self._call(
            iid, "V5", "conscience", brain.CONSCIENCE, brain.idea_block(idea), brain.Conscience
        )
        if con is None or not con.who_loses.strip() or not con.opposite.strip():
            await self._mark(iid, passed, "evidence")
            return 1
        await self._evidence(
            iid,
            "V5",
            "chi ci perde",
            "conscience",
            passed=True,
            run={
                "who_loses": con.who_loses,
                "dark_patterns": con.dark_patterns,
                "opposite": con.opposite,
                "refuse_to_polish": con.refuse_to_polish,
            },
        )
        passed.append("V5")
        await self._mark(iid, passed, None)
        return 1

    async def _mark(
        self, iid: uuid.UUID, passed: list[str], waiting: str | None, *, state: str = "validating"
    ) -> None:
        rung = max(passed, key=lambda r: rules.GRADE[r]) if passed else None
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_ideas SET rung = %s, waiting = %s, state = %s, updated_at = %s WHERE id = %s",
                (rung, waiting, state, self._clock(), iid),
            )

    # --- i10: ready for the sheet (≤ 3, ≤ 1 far); the stepping-stone map ---

    async def checked(self, conn: AsyncConnection, iid: uuid.UUID) -> bool:
        """RAIL ("validated" means checked): every rung V0, V1, V2, V5 has a line, and every non-simulated
        evidence line passed its code check."""
        rows = await (
            await conn.execute(
                "SELECT rung, passed, simulated FROM pisa_idea_evidence WHERE idea_id = %s", (iid,)
            )
        ).fetchall()
        real = [(r, p) for r, p, s in rows if not s]
        return all(p is True for _, p in real) and {"V0", "V1", "V2", "V5"} <= {r for r, _ in real}

    async def choose(self, night: date) -> int:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, band FROM pisa_ideas WHERE state = 'validating' AND rung = 'V5' AND "
                    "waiting IS NULL "
                    "ORDER BY p_keep DESC NULLS LAST, id"
                )
            ).fetchall()
            ready: list[uuid.UUID] = []
            wild = 0
            for iid, band in rows:
                if len(ready) >= rules.MORNING_MAX:
                    break
                if band == "far" and wild:
                    continue
                if not await self.checked(conn, iid):
                    await conn.execute("UPDATE pisa_ideas SET waiting = 'evidence' WHERE id = %s", (iid,))
                    continue
                ready.append(iid)
                wild += band == "far"
            for iid in ready:
                await conn.execute(
                    "UPDATE pisa_ideas SET state = 'ready', updated_at = %s WHERE id = %s",
                    (self._clock(), iid),
                )
            await self._map(conn, night)
        _log.info("ideas_ready", ready=len(ready))
        return len(ready)

    async def _map(self, conn: AsyncConnection, night: date) -> None:
        """The stepping-stone map, in shadow: each market x mechanism cell keeps its best living idea."""
        rows = await (
            await conn.execute(
                "SELECT id, market, kind, rung, p_keep FROM pisa_ideas WHERE state NOT IN ('killed') "
                "AND rung IS NOT NULL"
            )
        ).fetchall()
        best: dict[tuple[str, str], tuple[int, float, uuid.UUID]] = {}
        for iid, market, kind, rung, p in rows:
            score = (rules.GRADE.get(rung, 0), float(p or 0), iid)
            if (market, kind) not in best or score[:2] > best[(market, kind)][:2]:
                best[(market, kind)] = score
        for (market, kind), (grade, _, iid) in best.items():
            await conn.execute(
                "INSERT INTO pisa_idea_map (market, kind, idea_id, grade, night) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (market, kind) DO UPDATE SET idea_id = excluded.idea_id, grade = "
                "excluded.grade, "
                "night = excluded.night",
                (market, kind, iid, grade, night),
            )

    # --- reading, for the sheet and the tests (never a turn's) ---

    async def grade(self, iid: uuid.UUID) -> str:
        """The evidence grade in words."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT rung, check_kind, number, run, passed FROM pisa_idea_evidence WHERE idea_id = %s",
                    (iid,),
                )
            ).fetchall()
        facts: dict[str, float] = {}
        rungs: set[str] = set()
        competitors = 0
        for rung, kind, number, run, ok in rows:
            if ok is False:
                continue
            rungs.add(rung)
            competitors += rung == "V0" and kind == "link"
            if rung == "V1" and number is not None and "buyers" not in facts:
                facts["buyers"] = float(number)
            if rung == "V2" and run:
                facts["revenue_year_eur"] = float(
                    (run if isinstance(run, dict) else json.loads(run))["output"]["revenue_year_eur"]
                )
        facts["competitors"] = competitors
        return rules.grade_words(sorted(rungs), facts)

    # --- reading, for the dossiers (never a turn's) ---

    async def his_names(self) -> list[taint.Personal]:
        """The proper nouns of their problems and fragments, for the searches (there
        too)."""
        return await self._his_names()

    async def for_desk(self, n: int, with_dossier: Collection[uuid.UUID] = ()) -> list[dict[str, Any]]:
        """The ideas ready for a morning (makes their dossiers; `with_dossier`: the ideas that have one, kept
        on a tie): each one's statement,
        mechanism, market, kind, its V0 competitors' names and links that passed, and its V1 buyers and V2
        revenue a year when they passed."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    # A tie on p_keep keeps an idea with a dossier: a night's new ideas never displace one by
                    # a random id. Then the older idea, then the id (deterministic).
                    "SELECT id, statement, mechanism, market, kind FROM pisa_ideas WHERE state = 'ready' "
                    "ORDER BY p_keep DESC NULLS LAST, id = ANY(%s) DESC, created_at, id LIMIT %s",
                    (list(with_dossier), n),
                )
            ).fetchall()
            out: list[dict[str, Any]] = []
            for iid, statement, mechanism, market, kind in rows:
                lines = await (
                    await conn.execute(
                        "SELECT rung, check_kind, claim, url, number, run FROM pisa_idea_evidence "
                        "WHERE idea_id = %s AND passed IS TRUE AND NOT simulated ORDER BY checked_at, id",
                        (iid,),
                    )
                ).fetchall()
                competitors = [
                    (str(c).removeprefix("esiste già: "), str(u))
                    for r, k, c, u, _, _ in lines
                    if r == "V0" and k == "link" and u
                ]
                buyers = next((float(x) for r, k, _, _, x, _ in lines if r == "V1" and x is not None), None)
                revenue = next(
                    (
                        float(
                            (run if isinstance(run, dict) else json.loads(run))["output"]["revenue_year_eur"]
                        )
                        for r, _, _, _, _, run in lines
                        if r == "V2" and run
                    ),
                    None,
                )
                out.append(
                    {
                        "id": iid,
                        "statement": statement,
                        "mechanism": mechanism,
                        "market": market,
                        "kind": kind,
                        "competitors": competitors,
                        "buyers": buyers,
                        "revenue_year_eur": revenue,
                    }
                )
        return out

    async def neighbours(self, iid: uuid.UUID, n: int) -> list[uuid.UUID]:
        """The `n` ideas nearest to one (by its vector), living or killed: a dossier's neighbours."""
        async with self._pool.connection() as conn:
            me = await (await conn.execute("SELECT vec FROM pisa_ideas WHERE id = %s", (iid,))).fetchone()
            if me is None or me[0] is None:
                return []
            others = await (
                await conn.execute(
                    "SELECT id, vec FROM pisa_ideas WHERE id <> %s AND vec IS NOT NULL ORDER BY id", (iid,)
                )
            ).fetchall()
        ranked = sorted(others, key=lambda r: (-rules.cosine(me[0], r[1]), str(r[0])))
        return [r[0] for r in ranked[:n]]


def block_owner(text: str, what: str = "the user's own words") -> str:
    """Their words, marked as their (never instructions either)."""
    return brain.block("owner-profile", text, what)
