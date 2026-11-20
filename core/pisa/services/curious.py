"""The curiosity engine, as steps of the frame's night.

**Shadow only**: nothing here reaches them (no notification, message, candidate, task or job)
and nothing here enters any prompt: no turn, intake, profile, grounding or style path reads `pisa_unknown*`,
`pisa_topic*`, `pisa_horizon` or `pisa_curious_sources` (only this module names them). The shelf's changes and
the topic's explainer wait for the surface. **No quizzing them**: the Topic Loop's tests are
Pisa's own, answered by Pisa.

The steps (`night.STEPS`, after the idea engine's; the frame's stop, budget `night` and search gate hold over
each):
- **c1_map**, the ignorance map: their "Pisa, tienimelo aperto: …" questions; **the miss becomes a question**
  (each prediction that missed with confidence opens "perché mi sono sbagliato?", about Pisa itself,
  answered by their own data and never searched); the topic they name ("Pisa, voglio capire X": five
  pre-questions from five perspectives, one topic at a time); the horizon scan (≤ 2 a week, each a question,
  never an action).
- **c2_score**, the interestingness score: `value x progress x tractability x novelty x (1 - noisy)`. A cheap
  model scores novelty and the cognitive part; code the rest, value weighted by their liked acts (never
  opens). The noisy-TV filter parks a question whose reads keep teaching nothing; a debated one with no
  convergence is parked as open for the experts; an unanswerable one is parked, not chased.
- **c3_read**, three gaits over ≤ 10 searches (hunter 7, busybody 2, dancer 1), like a fact checker: results
  read by index, a claim kept only when its quote is on the page Pisa fetched (else dashed), a lateral check
  per new source (cached 90 days), a forced opposing view on a debated claim, a foraging stop after two reads
  that add nothing.
- **c4_topic**, the Topic Loop: the map (principles with two cases, numbers on their quotes, dashed unknowns);
  Pisa's self-test, written from the sources and answered next night from the map alone (its score is the
  learning progress); the two-minute explainer with one open question for them (stored, never sent); a monthly
  refresh once progress and their liking are flat.
- **c5_shelf**, the Ovsiankina shelf: ≤ 20 questions held for days (their first), each with what is known,
  what is blocking and when Pisa looks again; a state change (answered, parked, a deadline near) is logged for
  the sheet, never sent.

**Caps**: ≤ €0.05 of the night's €0.25 (summed from the ledger by trace id, the next call assumed
as dear as the night's dearest), ≤ 10 of the night's 40 searches (takes ≤ 30); every search
through the frame's `search` and its filter. Logs carry ids and counts only.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain import curious as brain
from pisa.domain import curious as rules
from pisa.domain import taint
from pisa.domain.ideas import cosine, generic, link, local_vector, on_page, proper_nouns, same_site
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import foreign
from pisa.services.night_step import NightStep, Read, no_read

if TYPE_CHECKING:
    from pisa.services.night import NightService

_log = get_logger("pisa.curious")
KIND_PREFIX = "c3_"  # every search of the curiosity's is a `c3_*` kind of the frame's rows
SCORE_MAX = 40  # open rows scored a night (the newest and their own first)
LOOK_BACK = 7  # their words of the last week; the predictions' misses of the last week
NEAR = 0.8  # a question this close to one they were told or Pisa answered is no novelty to them


class CuriousService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        night: "NightService",
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        think: brain.Think | None = None,
        read: Read = no_read,
    ) -> None:
        self._pool = pool
        self._night = night
        self._owner = owner
        self._clock = clock
        self._think = think
        # the frame's stop, budget, call, read and search
        self.step = NightStep(pool, name="curious", trace="curious", owner=owner, night=night, read=read)
        # tonight's queries, in memory only (never stored: the frame's rows keep counts, never a query)
        self._queries: dict[uuid.UUID, tuple[str, str]] = {}

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {
            "c1_map": self.step.guarded(self.gather),
            "c2_score": self.step.guarded(self.score),
            "c3_read": self.step.guarded(self.read),
            "c4_topic": self.step.guarded(self.topic),
            "c5_shelf": self.step.guarded(self.shelf),
        }

    # --- the gates every call and search goes through ---

    async def _ask[T](self, night: date, task: str, instructions: str, prompt: str, out: type[T]) -> T | None:
        """RAIL (b, c): one model call through the frame (its stop, its failure policy), never past the
        curiosity's €0.05 (checked before the call, the next one assumed as dear as tonight's dearest)."""
        return await self.step.ask(
            self._think, task, instructions, prompt, out,
            trace=f"curious-{night.isoformat()}-{task}",
            cap=(self.step.tonight(night), rules.NIGHT_CAP_EUR, rules.EST_CALL_EUR),
        )  # fmt: skip

    async def searches_left(self, night: date) -> int:
        """The curiosity's share left tonight, counted from the frame's own rows (never in memory)."""
        return rules.NIGHT_SEARCHES - await self.step.sent(night, KIND_PREFIX)

    async def _his_names(self) -> list[taint.Personal]:
        """The proper nouns of their own words on the map (their questions, the topic they named, the
        horizon's questions, and the messages they came from: a client or a town Pisa never stored as a
        contact), handed to the frame's filter as names."""
        async with self._pool.connection() as conn:
            texts = [t for (t,) in await (
                await conn.execute(
                    "SELECT question FROM pisa_unknowns UNION ALL SELECT name FROM pisa_topics "
                    "UNION ALL SELECT question FROM pisa_horizon UNION ALL SELECT m.content FROM messages m "
                    "WHERE m.id IN (SELECT message_id FROM pisa_unknowns UNION SELECT "
                    "message_id FROM pisa_topics)"
                )
            ).fetchall()]  # fmt: skip
        return [taint.Personal(n) for n in sorted({n for t in texts for n in proper_nouns(t or "")})]

    async def _search(self, night: date, query: str, kind: str) -> list[dict[str, str]] | None:
        """RAIL: only through the frame's `search` (its filter, its 40, its stop); the curiosity's
        own share is 10. Every query is model-written (or page-shaped, the opposing view): it loses every
        capitalised word that is not generic, and their own words' proper nouns go to the filter as names.
        None when refused."""
        if await self.searches_left(night) <= 0:
            return None
        return await self.step.search(generic(query), kind=kind, extra=await self._his_names(), link=link)

    async def _page(self, url: str) -> str | None:
        """The cited page, fetched by Pisa: its text, or None when the link doesn't resolve."""
        if not link(url):
            return None
        got = await self.step.read(url)
        if not got or not same_site(url, got[0]):  # a redirect to another site is no evidence
            return None
        return got[1]

    async def _open(
        self,
        conn: AsyncConnection,
        source: str,
        ref: str,
        question: str,
        *,
        about: str,
        answer_by: str = "web",
        explicit: bool = False,
        topic_id: uuid.UUID | None = None,
        deadline: date | None = None,
        why: str | None = None,
        message_id: int | None = None,
    ) -> uuid.UUID | None:
        now = self._clock()
        row = await (
            await conn.execute(
                "INSERT INTO pisa_unknowns (id, source, ref, question, about, answer_by, explicit, topic_id, "
                "deadline, why, message_id, opened_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s) ON CONFLICT (source, ref) DO NOTHING RETURNING id",
                (uuid.uuid4(), source, ref, question[:400], about, answer_by, explicit, topic_id, deadline,
                 why, message_id, now, now),
            )
        ).fetchone()  # fmt: skip
        return row[0] if row else None

    # --- c1: the ignorance map ---

    async def gather(self, night: date) -> int:
        """The rows of the ignorance map opened tonight: their questions, the misses, a topic, the horizon."""
        now = self._clock()
        opened = 0
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, content, created_at FROM messages WHERE role = 'user' AND created_at >= %s "
                    "AND created_at < %s ORDER BY id",
                    (now - timedelta(days=LOOK_BACK), now),
                )
            ).fetchall()
            for message_id, content, at in rows:
                asked = rules.his_question(content or "")
                if asked is not None:  # they hand Pisa a question to hold
                    opened += (
                        await self._open(
                            conn,
                            "his_question",
                            str(message_id),
                            asked,
                            about="life",
                            explicit=True,
                            message_id=message_id,
                        )
                        is not None
                    )
                named = rules.topic(content or "")
                if named is not None:
                    await conn.execute(
                        "INSERT INTO pisa_topics (id, message_id, name, opened_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING",
                        (uuid.uuid4(), message_id, named[:200], at, now),
                    )
            # the miss becomes a question: about Pisa itself, answered by their own data
            for pid, kind, day, p, outcome in await self._night.scored(night - timedelta(days=LOOK_BACK)):
                asked = rules.miss_question(kind, p, outcome, day)
                if asked is not None:
                    opened += (
                        await self._open(
                            conn, "miss", str(pid), asked, about="self", answer_by="his_data",
                            why=f"prediction:{kind}",
                        )
                        is not None
                    )  # fmt: skip
            # a monthly topic whose refresh is due comes back
            await conn.execute(
                "UPDATE pisa_topics SET state = 'active', updated_at = %s WHERE state = 'monthly' "
                "AND refresh_on <= %s",
                (now, night),
            )
        opened += await self._start_topic(night)
        opened += await self._horizon(night)
        return opened

    async def _start_topic(self, night: date) -> int:
        """One topic at a time: the oldest they named becomes active, with five pre-questions (STORM)."""
        async with self._pool.connection() as conn:
            if await (await conn.execute("SELECT 1 FROM pisa_topics WHERE state = 'active'")).fetchone():
                return 0
            row = await (
                await conn.execute(
                    "SELECT id, name, message_id FROM pisa_topics WHERE state = 'queued' "
                    "ORDER BY opened_at, id"
                )
            ).fetchone()
        if row is None:
            return 0
        tid, name, mid = row
        made = await self._ask(
            night,
            "prequestions",
            brain.PREQUESTIONS,
            brain.block("owner-profile", name, "the topic they named, their own words"),
            brain.PreQuestions,
        )
        if made is None:
            return 0
        opened = 0
        async with self._pool.connection() as conn:
            for i, q in enumerate(made.items[: rules.TOPIC_QUESTIONS]):
                opened += (
                    await self._open(
                        conn, "topic", f"{tid}:{i}", q.question, about="world", answer_by=q.answer_by,
                        explicit=True, topic_id=tid, why=f"topic:{q.perspective[:40]}", message_id=mid,
                    )
                    is not None
                )  # fmt: skip
            await conn.execute(
                "UPDATE pisa_topics SET state = 'active', updated_at = %s WHERE id = %s", (self._clock(), tid)
            )
        return opened

    async def _his_items(self, conn: AsyncConnection, night: date) -> list[tuple[str, str, date]]:
        """Their dated items of the next 60 days: (ref, title, day), events and tasks. RAIL:
        only their own: an event or a task whose title a stranger controls (`foreign.FOREIGN_EVENT`, an
        invite, Gmail's, a shared calendar's, Pisa's after outside text; an email's task) is never listed.
        These go to the model as Pisa's own day-context, and the gaps it finds may become a search's query."""
        start = self._night.day(night)[0]
        end = start + timedelta(days=rules.HORIZON_DAYS)
        items: list[tuple[str, str, date]] = []
        for cal, eid, title, at in await (
            await conn.execute(
                # constant SQL fragments, no input
                f"SELECT e.calendar_id, e.id, e.title, e.starts_at FROM {foreign.EVENTS} "  # noqa: S608
                f"WHERE e.status <> 'cancelled' AND NOT coalesce({foreign.FOREIGN_EVENT}, false) "
                "AND e.starts_at >= %s AND e.starts_at < %s ORDER BY e.starts_at, e.id LIMIT 15",
                (start, end),
            )
        ).fetchall():
            items.append((f"event:{cal}:{eid}", str(title), at.date()))
        for tid, title, at in await (
            await conn.execute(
                f"SELECT t.id, t.title, t.due_at FROM tasks t WHERE t.status = 'open' "  # noqa: S608
                f"AND NOT {foreign.FOREIGN_TASK} AND t.due_at >= %s AND t.due_at < %s "
                "ORDER BY t.due_at, t.id LIMIT 10",
                (start, end),
            )
        ).fetchall():
            items.append((f"task:{tid}", str(title), at.date()))
        return items

    async def _horizon(self, night: date) -> int:
        """RAIL (the best proactive model is right two times in three): at most two a week,
        each a question, never an action; a candidate already in their data is no gap."""
        async with self._pool.connection() as conn:
            week = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_horizon WHERE night > %s", (night - timedelta(days=7),)
                )
            ).fetchone()
            room = rules.HORIZON_WEEK - int(week[0] if week else 0)
            if room <= 0:
                return 0
            items = await self._his_items(conn, night)
            if not items:
                return 0
            known = [
                str(t)
                for (t,) in await (
                    await conn.execute("SELECT title FROM tasks WHERE status = 'open'")
                ).fetchall()
            ]
            known += [
                str(q) for (q,) in await (await conn.execute("SELECT question FROM pisa_unknowns")).fetchall()
            ]
        listed = "\n".join(f"[{i}] {d.isoformat()} {t[:120]}" for i, (_, t, d) in enumerate(items))
        made = await self._ask(
            night,
            "horizon",
            brain.HORIZON,
            brain.block("day-context", listed, "their dated items"),
            brain.Horizon,
        )
        if made is None:
            return 0
        opened = 0
        his = known + [t for _, t, _ in items]
        async with self._pool.connection() as conn:
            for need in made.items:
                if opened >= room or not 0 <= need.item < len(items):
                    continue
                question = " ".join(need.question.split())
                if not question.endswith("?") or rules.already_there(question, his):
                    continue  # a question, never an action; and only a real gap
                ref, _, day = items[need.item]
                hid = uuid.uuid4()
                uid = await self._open(
                    conn, "horizon", str(hid), question, about="life", answer_by=need.answer_by,
                    deadline=day, why=ref.split(":")[0],
                )  # fmt: skip
                if uid is None:
                    continue
                await conn.execute(
                    "INSERT INTO pisa_horizon (id, night, item, question, unknown_id, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (hid, night, ref, question, uid, self._clock()),
                )
                his.append(question)
                opened += 1
        return opened

    # --- c2: the interestingness score ---

    async def score(self, night: date) -> int:
        """Each open row scored: `value x progress x tractability x novelty x (1 - noisy)`; the untractable,
        the noisy and the endlessly debated are parked, not chased."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, source, question, deadline, explicit, reads, adds, debated, topic_id, "
                    "answer_by "
                    "FROM pisa_unknowns WHERE state = 'open' ORDER BY explicit DESC, opened_at DESC, id "
                    "LIMIT %s",
                    (SCORE_MAX,),
                )
            ).fetchall()
            if not rows:
                return 0
            told = [
                str(q)
                for (q,) in await (
                    await conn.execute(
                        "SELECT question FROM pisa_unknowns WHERE state = 'answered' OR shown_at IS NOT NULL"
                    )
                ).fetchall()
            ]
            likes = {
                str(s): (int(liked), int(shown))
                for s, liked, shown in await (
                    await conn.execute(
                        "SELECT source, count(*) FILTER (WHERE liked), count(*) FROM pisa_unknowns "
                        "WHERE shown_at IS NOT NULL GROUP BY source"
                    )
                ).fetchall()
            }
            topics = {
                tid: list(scores or [])
                for tid, scores in await (await conn.execute("SELECT id, scores FROM pisa_topics")).fetchall()
            }
        listed = "\n".join(f"[{i}] {r[2][:300]}" for i, r in enumerate(rows))
        made = await self._ask(
            night,
            "score",
            brain.SCORE,
            brain.block("understanding", listed, "Pisa's open questions"),
            brain.ScoredAll,
        )
        by_index = {s.index: s for s in (made.items if made else [])}
        told_v = [local_vector(t) for t in told]
        async with self._pool.connection() as conn:
            for i, (
                uid,
                source,
                question,
                deadline,
                explicit,
                reads,
                adds,
                debated,
                tid,
                answer_by,
            ) in enumerate(rows):
                s = by_index.get(i)
                novelty = s.novelty if s else 0.5
                v = local_vector(question)
                if any(cosine(v, t) >= NEAR for t in told_v):
                    # observer-relative novelty: they were told, or Pisa answered it
                    novelty = min(novelty, 0.1)
                # RAIL: a question about them is answered by their data, never searched
                if source == "miss":
                    about, answer_by = "self", "his_data"
                elif s is not None:
                    about, answer_by = s.about, s.answer_by
                    # RAIL: their own question or a gap in their plans, about their life or Pisa's view of
                    # them, is never searched for: it waits for them (or their data)
                    if source in ("his_question", "horizon") and about in ("life", "self"):
                        answer_by = answer_by if answer_by not in rules.SEARCHED else "ask_him"
                else:
                    about = None
                weight = rules.like_weight(*likes.get(source, (0, 0)))  # their liked acts, never opens
                val = rules.value(
                    rules.instrumental(deadline, night), s.cognitive if s else 0.0, bool(explicit), weight
                )
                prog = rules.topic_progress(topics.get(tid, [])) if tid else rules.progress(reads, adds)
                tract = rules.TRACTABLE[answer_by]
                is_noisy = rules.noisy(reads, adds)
                score = rules.interest(val, prog, tract, novelty, is_noisy)
                park: str | None = None
                if is_noisy:
                    park = "noisy"
                elif rules.open_for_experts(bool(debated), reads, False):
                    park = "open_for_experts"
                elif answer_by == "none":
                    park = "untractable"
                if s is not None and answer_by in rules.SEARCHED:
                    self._queries[uid] = (s.query, s.around)
                await conn.execute(
                    "UPDATE pisa_unknowns SET novelty = %s, cognitive = %s, instrumental = %s, progress = "
                    "%s, tractability = %s, noisy = %s, interest = %s, answer_by = %s, "
                    "about = coalesce(%s, about), hedonic_negative = %s, neutral_title = %s, "
                    "state = CASE WHEN %s::text IS NULL THEN state ELSE 'parked' END, park_reason = %s, "
                    "blocking = CASE WHEN %s = 'open_for_experts' THEN %s ELSE blocking END, updated_at = %s "
                    "WHERE id = %s",
                    (
                        novelty, s.cognitive if s else 0.0, rules.instrumental(deadline, night), prog, tract,
                        is_noisy, score, answer_by, about, bool(s and s.hedonic_negative),
                        (s.neutral_title or None) if s and s.hedonic_negative else None,
                        park, park, park, rules.OPEN_FOR_EXPERTS, self._clock(), uid,
                    ),
                )  # fmt: skip
                if park is not None:
                    await self._change(conn, uid, night, "parked")
        return len(rows)

    async def _change(self, conn: AsyncConnection, uid: uuid.UUID, night: date, change: str) -> None:
        await conn.execute(
            "INSERT INTO pisa_unknown_changes (unknown_id, night, change) VALUES (%s, %s, %s) ON CONFLICT DO "
            "NOTHING",
            (uid, night, change),
        )

    # --- c3: three gaits over ≤ 10 searches ---

    async def read(self, night: date) -> int:
        """The night's reads in three gaits on the top-scored rows a web source can answer."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, question FROM pisa_unknowns WHERE state = 'open' AND NOT noisy "
                    "AND answer_by = ANY(%s) AND interest > 0 AND (next_look IS NULL OR next_look <= %s) "
                    "ORDER BY interest DESC, id LIMIT %s",
                    (list(rules.SEARCHED), night, rules.ANSWER_MAX),
                )
            ).fetchall()
        # their question is `Personal`: it reaches a search only through the frame's strip
        rows = [(uid, taint.Personal(q)) for uid, q in rows if uid in self._queries]
        if not rows:
            return 0
        done = 0
        dry: dict[uuid.UUID, int] = {}  # tonight's reads in a row that added nothing (the foraging stop)
        opposing: dict[uuid.UUID, str] = {}
        closed: set[uuid.UUID] = set()
        hunter_turn = 0
        leapt = False
        for gait in rules.gaits(rules.NIGHT_SEARCHES):
            if await self.searches_left(night) <= 0:
                break
            live = [(u, q) for u, q in rows if u not in closed and dry.get(u, 0) < rules.FORAGE_STOP]
            if not live:
                break
            if gait == "hunter":
                uid, question = live[hunter_turn % len(live)]
                hunter_turn += 1
                query, kind = self._queries[uid][0], "c3_hunter"
                if uid in opposing:  # a forced opposing view on a debated claim
                    query, kind, gait = opposing.pop(uid), "c3_opposing", "opposing"
            elif gait == "busybody":
                uid, question = live[0 if done % 2 == 0 else min(1, len(live) - 1)]
                query, kind = self._queries[uid][1], "c3_busybody"
            else:
                if leapt:
                    continue
                leapt = True
                listed = "\n".join("[" + str(i) + "] " + q[:300] for i, (_, q) in enumerate(live))
                leap = await self._ask(
                    night,
                    "leap",
                    brain.LEAP,
                    brain.block("understanding", listed, "Pisa's open questions"),
                    brain.Leap,
                )
                if leap is None or not 0 <= leap.index < len(live):
                    continue
                uid, question = live[leap.index]
                query, kind = leap.query, "c3_dancer"
            if not query:
                continue
            outcome = await self._one(night, uid, question, query, kind, gait)
            if outcome is None:
                break  # the night refused (its 40, the stop): no further read
            done += 1
            adds, answered, opposite = outcome
            dry[uid] = 0 if adds else dry.get(uid, 0) + 1
            if opposite:
                opposing[uid] = opposite
            if answered or await self._park_noisy(night, uid):
                closed.add(uid)
        return done

    async def _one(
        self, night: date, uid: uuid.UUID, question: taint.Personal, query: str, kind: str, gait: str
    ) -> tuple[bool, bool, str] | None:
        """One read: the search, the model's reading by index, the quote checked on the page Pisa fetched, and
        a lateral check before a new source answers anything. None when the search was refused."""
        found = await self._search(night, query, kind)
        if found is None:
            return None
        got = None
        if found:
            got = await self._ask(
                night,
                "read",
                brain.READ,
                brain.block("understanding", question, "Pisa's question") + "\n" + brain.results_block(found),
                brain.Read,
            )
        url = page = None
        if got is not None and got.index is not None and 0 <= got.index < len(found):
            url = found[got.index]["url"]
            page = await self._page(url)
        supported = bool(got and page is not None and rules.quoted(got.quote, page, got.claim))
        async with self._pool.connection() as conn:
            before = [
                str(c)
                for (c,) in await (
                    await conn.execute(
                        "SELECT claim FROM pisa_unknown_reads WHERE unknown_id = %s AND supported", (uid,)
                    )
                ).fetchall()
            ]
        claim = " ".join((got.claim if got else "").split())[:300]
        fresh = bool(claim) and all(cosine(local_vector(claim), local_vector(b)) < NEAR for b in before)
        adds = bool(got and got.adds and supported and fresh)  # RAIL: only a checked, new claim adds
        debated = bool(got and got.debated)
        answers = bool(got and got.answers and supported)
        if answers and url is not None:
            answers = await self._lateral(night, url)
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pisa_unknown_reads (id, unknown_id, night, gait, url, claim, quote, supported, "
                "adds, "
                "answers, debated, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (uuid.uuid4(), uid, night, gait, url if supported else None, claim or None,
                 (got.quote[:300] if got and supported else None), supported, adds, answers, debated,
                 self._clock()),
            )  # fmt: skip
            await conn.execute(
                "UPDATE pisa_unknowns SET reads = reads + 1, adds = adds + %s, debated = debated OR %s, "
                "updated_at = %s WHERE id = %s",
                (int(adds), debated, self._clock(), uid),
            )
            if answers:
                await conn.execute(
                    "UPDATE pisa_unknowns SET state = 'answered', answer = %s, answer_url = %s, updated_at = "
                    "%s WHERE id = %s",
                    (claim, url, self._clock(), uid),
                )
                await self._change(conn, uid, night, "answered")
        opposite = got.opposing_query if got and debated and gait != "opposing" else ""
        return adds, answers, opposite

    async def _lateral(self, night: date, url: str) -> bool:
        """RAIL (lateral reading): before a new source answers anything, what others say about it;
        cached for 90 days. No search left: it answers nothing tonight."""
        host = rules.host(url)
        if host is None:
            return False
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT reliable FROM pisa_curious_sources WHERE host = %s AND checked_on > %s",
                    (host, night - timedelta(days=rules.LATERAL_DAYS)),
                )
            ).fetchone()
        if row is not None:
            return bool(row[0])
        words = " ".join(w for w in host.replace("-", ".").split(".") if w not in ("www", "com", "it", "org"))
        found = await self._search(night, f"{words} affidabilità fonte", "c3_lateral")
        if not found:
            return False
        said = await self._ask(night, "lateral", brain.LATERAL, brain.results_block(found), brain.Lateral)
        if said is None:
            return False
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pisa_curious_sources (host, reliable, checked_on) VALUES (%s, %s, %s) "
                "ON CONFLICT (host) DO UPDATE SET reliable = excluded.reliable, checked_on = "
                "excluded.checked_on",
                (host, said.reliable, night),
            )
        return said.reliable

    async def _park_noisy(self, night: date, uid: uuid.UUID) -> bool:
        """RAIL (the noisy-TV filter): reads that keep changing and teach nothing park the question at once,
        so it spends no further search tonight."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT reads, adds FROM pisa_unknowns WHERE id = %s", (uid,))
            ).fetchone()
            if row is None or not rules.noisy(int(row[0]), int(row[1])):
                return False
            await conn.execute(
                "UPDATE pisa_unknowns SET noisy = true, interest = 0, state = 'parked', park_reason = "
                "'noisy', updated_at = %s WHERE id = %s",
                (self._clock(), uid),
            )
            await self._change(conn, uid, night, "parked")
        return True

    # --- c4: the Topic Loop ---

    async def topic(self, night: date) -> int:
        """The active topic: last night's self-test answered from the map alone and scored by code; the map
        rebuilt from the supported reads; a new self-test; the explainer; a monthly refresh once flat."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT id, name, graph, scores, liked FROM pisa_topics WHERE state = 'active'"
                )
            ).fetchone()
            if row is None:
                return 0
            tid, _, graph, scores, liked = row
            scores = list(scores or [])
            tests = await (
                await conn.execute(
                    "SELECT id, question, expected FROM pisa_topic_tests WHERE topic_id = %s AND answered_on "
                    "IS NULL "
                    "AND written_on < %s ORDER BY id",
                    (tid, night),
                )
            ).fetchall()
            reads = await (
                await conn.execute(
                    "SELECT r.claim, r.quote, r.url FROM pisa_unknown_reads r JOIN pisa_unknowns u ON u.id = "
                    "r.unknown_id "
                    "WHERE u.topic_id = %s AND r.supported ORDER BY r.at, r.id",
                    (tid,),
                )
            ).fetchall()
            dashed = [
                str(q)
                for (q,) in await (
                    await conn.execute(
                        "SELECT question FROM pisa_unknowns WHERE topic_id = %s AND state <> 'answered' "
                        "ORDER BY ref",
                        (tid,),
                    )
                ).fetchall()
            ]
        answered = False
        if tests and graph:  # 4. Pisa tests itself, from the map alone (its sources are not in the prompt)
            asked = "\n".join(f"[{i}] {q}" for i, (_, q, _) in enumerate(tests))
            got = await self._ask(
                night,
                "test_answer",
                brain.TEST_ANSWER,
                brain.block("understanding", _graph_text(graph), "Pisa's own map of the topic")
                + "\n"
                + brain.block("understanding", asked, "Pisa's own questions"),
                brain.TestAnswers,
            )
            if got is not None:
                results: list[bool] = []
                async with self._pool.connection() as conn:
                    for i, (test_id, _, expected) in enumerate(tests):
                        answer = got.answers[i] if i < len(got.answers) else ""
                        ok = rules.correct(expected, answer)
                        results.append(ok)
                        await conn.execute(
                            "UPDATE pisa_topic_tests SET answered_on = %s, got = %s, "
                            "correct = %s WHERE id = %s",
                            (night, answer[:300], ok, test_id),
                        )
                scores.append(rules.score(results))
                answered = True
        if not reads:
            await self._save_topic(tid, scores=scores)
            return 0
        claims = "\n".join(f"[{i}] {c} — «{q}» ({u})" for i, (c, q, u) in enumerate(reads))
        source = brain.block("web", claims, "supported claims Pisa read, outside text")
        made = await self._ask(night, "map", brain.MAP, source, brain.Graph)  # 3. the map
        if made is not None:
            graph = {
                "principles": [p.model_dump() for p in made.principles],
                # RAIL: a number stays on the map only when it is in its claim's quote (on the fetched page)
                "numbers": [
                    {"claim": n.claim, "value": n.value, "url": reads[n.read][2]}
                    for n in made.numbers
                    if 0 <= n.read < len(reads) and on_page(n.value, str(reads[n.read][1] or ""), n.claim)
                ],
                "unknowns": dashed + [u for u in made.unknowns if u not in dashed],  # drawn dashed
            }
        pending = bool(tests) and not answered  # last night's test not yet answered: no new one
        if graph and not pending:  # a fresh self-test for the next night
            written = await self._ask(night, "test_write", brain.TEST_WRITE, source, brain.TestWritten)
            async with self._pool.connection() as conn:
                await conn.execute(
                    "DELETE FROM pisa_topic_tests WHERE topic_id = %s AND answered_on IS NULL", (tid,)
                )
                kept = 0
                for t in written.items if written else []:
                    if kept >= rules.TEST_MAX or not 0 <= t.read < len(reads):
                        continue
                    c, q, _ = reads[t.read]
                    if not rules.correct(t.expected, f"{c} {q}"):  # its answer is in its source, by code
                        continue
                    await conn.execute(
                        "INSERT INTO pisa_topic_tests (id, topic_id, written_on, question, expected) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (uuid.uuid4(), tid, night, t.question, t.expected),
                    )
                    kept += 1
        explainer = None
        if graph and graph.get("principles"):  # 5. the two-minute explainer, for the sheet (never sent)
            said = await self._ask(
                night,
                "explain",
                brain.EXPLAIN,
                brain.block("understanding", _graph_text(graph), "Pisa's own map of the topic"),
                brain.Explainer,
            )
            numbers = graph.get("numbers") or []
            if said is not None and said.open_question.strip().endswith("?"):
                explainer = {
                    "principle": said.principle,
                    "cases": said.cases,
                    "numbers": [numbers[i] for i in said.numbers if 0 <= i < len(numbers)],
                    "disagreement": said.disagreement,
                    "open_question": said.open_question.strip(),  # one open question for them, at the end
                }
        await self._save_topic(tid, scores=scores, graph=graph, explainer=explainer)
        if rules.flat(scores, liked):  # 7. boredom: a monthly refresh
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pisa_topics SET state = 'monthly', refresh_on = %s WHERE id = %s",
                    (night + timedelta(days=rules.REFRESH_DAYS), tid),
                )
        return 1

    async def _save_topic(
        self, tid: uuid.UUID, *, scores: list[float], graph: Any = None, explainer: Any = None
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_topics SET scores = %s, graph = coalesce(%s, graph), "
                "explainer = coalesce(%s, explainer), updated_at = %s WHERE id = %s",
                (
                    Jsonb(scores),
                    Jsonb(graph) if graph else None,
                    Jsonb(explainer) if explainer else None,
                    self._clock(),
                    tid,
                ),
            )

    # --- c5: the Ovsiankina shelf ---

    async def shelf(self, night: date) -> int:
        """≤ 20 questions held for days (their own first, then by score), each with what is known, what is
        blocking and when Pisa looks again; a deadline near is a state change, logged for the sheet."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, interest, answer_by, deadline FROM pisa_unknowns WHERE state = 'open' "
                    "ORDER BY (source = 'his_question') DESC, coalesce(interest, 0) DESC, opened_at, id"
                )
            ).fetchall()
            keep = rows[: rules.SHELF_MAX]
            await conn.execute("UPDATE pisa_unknowns SET on_shelf = false WHERE on_shelf")
            for uid, score, answer_by, deadline in keep:
                known = [
                    str(c)
                    for (c,) in await (
                        await conn.execute(
                            "SELECT claim FROM pisa_unknown_reads WHERE unknown_id = %s AND supported "
                            "ORDER BY at DESC LIMIT 3",
                            (uid,),
                        )
                    ).fetchall()
                ]
                blocking = _blocking(answer_by)
                await conn.execute(
                    "UPDATE pisa_unknowns SET on_shelf = true, next_look = %s, known = %s, "
                    "blocking = coalesce(%s, blocking) WHERE id = %s",
                    (rules.next_look(night, float(score or 0)), "; ".join(known) or None, blocking, uid),
                )
                if deadline is not None and night <= deadline <= night + timedelta(days=3):
                    seen = await (
                        await conn.execute(
                            "SELECT 1 FROM pisa_unknown_changes WHERE unknown_id = %s "
                            "AND change = 'deadline'",
                            (uid,),
                        )
                    ).fetchone()
                    if seen is None:
                        await self._change(conn, uid, night, "deadline")
        return len(keep)


def _blocking(answer_by: str) -> str | None:
    if answer_by == "his_data":
        return "si risponde con i suoi dati, non con il web"
    if answer_by == "ask_him":
        return "serve chiederlo a lui (non di notte)"
    return None


def _graph_text(graph: dict[str, Any]) -> str:
    lines = [f"- {p['principle']} (casi: {'; '.join(p['cases'])})" for p in graph.get("principles") or []]
    lines += [f"- {n['claim']}: {n['value']:g}" for n in graph.get("numbers") or []]
    lines += [f"- ? {u}" for u in graph.get("unknowns") or []]
    return "\n".join(lines) or "-"
