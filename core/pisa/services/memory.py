"""Pisa learns them from what they say.

**The night's step `m1_extract`** (`night.STEPS`, inside the one night: its 03:30 in their zone, its "ferma
tutto", its ledger budget `night`): every finished conversation (no message for 30 minutes) of the last three
local days not read yet, oldest first, at most 40 a night, is read **once** (a long talk is one call):
- only their own messages (never Pisa's replies, never a quoted or outside text), in a data block;
- one call to the conversation slot's model, no thinking (`pisa.brain.memory`), whose output is claims
  only: a kind, a subject, a value and their quote;
- code keeps a claim only when `pisa.domain.memory.why_not` allows it (their words, word for word; a subject
  and a value; never sensitive, never other people) and the profile's own rails allow it
  (`ProfileService.learned`: paused, off limits, forgotten, said wrong). New ones are `candidate` /
  `inferred`; one they already have is reinforced, never duplicated.

**Money**: each call goes on the ledger (its trace `memory-<night>-<n>`). Before a
call, the night's spend so far plus one more call (the night's dearest, at least `EST_CALL_EUR`) must stay
within €0.05, else the night stops (`capped`) and the rest wait for the next night; a call that cost more than
€0.005 (its words are capped at `WORDS_MAX` characters so it can't) stops the night too. The night's row in
`profile_extract_nights` says what it read, kept, reinforced, left waiting and spent: the morning's count.

Logs and rows carry ids, counts and euros only, never their words.
"""

import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.brain.memory import Estimate, Extract
from pisa.domain import memory as rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services.night_step import NightStep
from pisa.services.profile import ProfileService

_log = get_logger("pisa.memory")
TRACE = "memory"  # the calls' trace prefix: `memory-<night>-<n>`
FINISHED = timedelta(minutes=30)  # a conversation with no message this long is finished (the talk's gap)
LOOK_BACK_DAYS = 3  # the day's conversations, and any a missed night left


class MemoryService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        profile: ProfileService,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        extract: Extract | None = None,
        estimate: Estimate | None = None,
    ) -> None:
        self._pool = pool
        self.step = NightStep(pool, name="memory", trace=TRACE)  # the frame's stop and spend
        self._profile = profile
        self._owner = owner
        self._clock = clock
        self._extract = extract
        self._estimate = estimate

    def steps(self) -> dict[str, Callable[[date], Any]]:
        return {"m1_extract": self.extract}

    async def _finished(self, conn: AsyncConnection, night: date) -> list[uuid.UUID]:
        start = datetime.combine(night - timedelta(days=LOOK_BACK_DAYS), time(0), tzinfo=self._owner.zone)
        rows = await (
            await conn.execute(
                "SELECT c.id FROM conversations c WHERE c.last_message_at >= %s AND c.last_message_at < %s "
                "AND NOT EXISTS (SELECT 1 FROM profile_extractions x WHERE x.conversation_id = c.id) "
                "ORDER BY c.last_message_at",
                (start, self._clock() - FINISHED),
            )
        ).fetchall()
        return [r[0] for r in rows]

    async def _his_words(self, conn: AsyncConnection, conversation_id: uuid.UUID) -> str:
        rows = await (
            await conn.execute(
                "SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' AND NOT quoted "
                "ORDER BY id",
                (conversation_id,),
            )
        ).fetchall()
        return "\n".join(" ".join(str(r[0]).split()) for r in rows if str(r[0]).strip())[: rules.WORDS_MAX]

    async def _done(
        self,
        conversation_id: uuid.UUID,
        night: date,
        status: str,
        counts: tuple[int, int, int],
        cost: Decimal,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO profile_extractions (conversation_id, night, status, proposed, stored, "
                "reinforced, "
                "cost_eur, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (conversation_id) "
                "DO NOTHING",
                (conversation_id, night, status, *counts, cost, self._clock()),
            )

    async def extract(self, night: date) -> int:
        """The night's pass (see the module). Returns the new candidate claims."""
        async with self._pool.connection() as conn:
            if await self._profile.paused_until(conn) is not None:
                _log.info("memory_paused")  # learning paused: nothing read, nothing waits on it
                return 0
            lang = await self._owner.lang(conn)
            waiting = await self._finished(conn, night)
            people = [str(n) for (n,) in await (await conn.execute("SELECT name FROM people")).fetchall()]
        read = stored = reinforced = calls = 0
        capped = False
        for conversation_id in waiting[: rules.CONVERSATIONS_MAX]:
            async with self._pool.connection() as conn:
                words = await self._his_words(conn, conversation_id)
            if await self.step.stopped():
                break
            if not words or self._extract is None:
                if not words:
                    await self._done(conversation_id, night, "empty", (0, 0, 0), Decimal(0))
                continue
            spent, dearest = await self.step.spent(self.step.tonight(night))
            one = max(rules.EST_CALL_EUR, dearest)
            est = await self._estimate(words, lang) if self._estimate is not None else None
            if est is not None:  # RAIL: the slot's model as set now, before the call runs
                if 2 * est > rules.CONVERSATION_EUR:  # one output retry included
                    _log.warning("memory_call_too_dear", estimate=str(est))
                    capped = True
                    break
                one = max(one, est)
            if spent + one > rules.NIGHT_EUR:
                capped = True  # past the night's €0.05: the rest wait for the next night
                break
            calls += 1
            trace = f"{TRACE}-{night.isoformat()}-{calls}"
            status = "done"
            try:
                proposals = await self._extract(words, lang, trace=trace)
            except PisaError as err:
                if err.code == "budget_exceeded":
                    capped = True  # the night's own budget (the frame's €0.25) or the hard limit said no
                    break
                # no model, a key, a provider down or throttled: the pass stops and this
                # conversation and the rest wait for the next night (never burned as read)
                _log.info("memory_model_unavailable", error=err.code)
                break
            except Exception as exc:  # a model's bad answer ends this conversation only
                _log.info("memory_extract_failed", error=exc.__class__.__name__)
                proposals, status = [], "failed"
            kept = [p for p in (q.clean() for q in proposals) if rules.why_not(p, words, people) is None]
            kept = kept[: rules.PROPOSALS_MAX]
            new, again = await self._profile.learned(kept, ref=f"conv:{conversation_id}", at=self._clock())
            cost = await self.step.cost(trace)
            await self._done(conversation_id, night, status, (len(proposals), new, again), cost)
            read += 1
            stored += new
            reinforced += again
            if cost > rules.CONVERSATION_EUR:  # RAIL: a call dearer than €0.005 stops the night
                _log.warning("memory_call_over", cost=str(cost))
                capped = True
                break
        spent, _ = await self.step.spent(self.step.tonight(night))
        async with self._pool.connection() as conn:
            done_now = await (
                await conn.execute("SELECT count(*) FROM profile_extractions WHERE night = %s", (night,))
            ).fetchone()
            still = await self._finished(conn, night)
            left = len(still)
            await conn.execute(
                "INSERT INTO profile_extract_nights (night, conversations, stored, reinforced, "
                "waiting, spent_eur, "
                "capped, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (night) DO UPDATE SET "
                "conversations = EXCLUDED.conversations, stored = profile_extract_nights.stored + "
                "EXCLUDED.stored, "
                "reinforced = profile_extract_nights.reinforced + EXCLUDED.reinforced, waiting = "
                "EXCLUDED.waiting, "
                "spent_eur = EXCLUDED.spent_eur, capped = profile_extract_nights.capped OR EXCLUDED.capped, "
                "at = EXCLUDED.at",
                (night, int(done_now[0]) if done_now else read, stored, reinforced, left, spent, capped,
                 self._clock()),
            )  # fmt: skip
        _log.info(
            "memory_extracted", read=read, stored=stored, reinforced=reinforced, waiting=left, capped=capped
        )
        return stored

    async def night(self, night: date) -> dict[str, Any] | None:
        """The night's count (the morning's): what it read, kept, reinforced, left waiting, spent, and
        whether it stopped at its €0.05."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT conversations, stored, reinforced, waiting, spent_eur, capped FROM "
                    "profile_extract_nights WHERE night = %s",
                    (night,),
                )
            ).fetchone()
        if row is None:
            return None
        conversations, stored, reinforced, waiting, spent, capped = row
        return {
            "conversations": conversations,
            "stored": stored,
            "reinforced": reinforced,
            "waiting": waiting,
            "spent_eur": str(spent),
            "capped": capped,
        }
