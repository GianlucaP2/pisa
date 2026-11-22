"""The portrait at the top of "Su di te", its
corrections, and the "Conosciamoci" offer. The only module that names `profile_portrait`.

- **The night's step `m2_portrait`** (after `m1_extract`): their active claims (never a candidate, never a
  sensitive one, never an email rule; `pisa.domain.portrait.usable`), the most confident 60. Unchanged since
  the last portrait (`claims_hash`, their language too): nothing. Fewer than 3: the empty portrait, no call.
  Else one call on the conversation slot, no thinking (`pisa.brain.portrait`), on the memory pass's own €0.05
  (its trace `memory-<night>-portrait`): over it, the old portrait stays and the night's count says
  `capped`.
- **Reading** (`get`): the stored portrait, without any line whose claims are no longer active and usable
  (a claim they forgot or corrected elsewhere leaves at once, by code).
- **Their corrections** (`correct`): "Non è vero" retires the line's claims by them; "Correggi" keeps their
  words on the line's first claim (their edit: confirmed, active) and retires the others. The stored portrait
  changes by code, no call; the next night writes it again from the claims as they are.
- **The offer** (`m3_offer`): when Pisa's own reply yesterday said it doesn't know what they like, one
  `notify` (info, a "Conosciamoci" choice, expects a reply), at most one a week, never while their first talk
  is going on or done, never while learning is paused. Quiet hours hold it.

Logs carry counts and ids only.
"""

from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain.portrait import Estimate, Write
from pisa.db.settings import get_setting, set_setting
from pisa.domain import memory as memory_rules
from pisa.domain import portrait as rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import follow
from pisa.services.memory import TRACE
from pisa.services.night_step import NightStep
from pisa.services.notifications import enqueue
from pisa.services.profile import ProfileService

_log = get_logger("pisa.portrait")
OFFERED_KEY = "profile_talk_offered"  # the day the "Conosciamoci" offer last went out
OFFER_EVERY = timedelta(days=7)
OFFER_TEXT = {
    "it-IT": "Ieri non sapevo cosa ti piace. Ti va di dirmelo? Bastano 10 minuti.",
    "en-US": "Yesterday I didn't know what you like. Want to tell me? It takes 10 minutes.",
}
OFFER_CHOICE = {"it-IT": "Conosciamoci", "en-US": "Let's get to know each other"}
OFFER_ID = "conosciamoci"  # the check-in's one choice: its answer starts the first talk
OFFER_PATH = "profile.talk_offer"  # its notice's path: what the check-in's answer starts (runtime)


def _bad(message: str) -> PisaError:
    return PisaError(
        "validation_failed", message, "Send the line's claim_ids and wrong, or correct with their words"
    )


class PortraitService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        profile: ProfileService,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        write: Write | None = None,
        estimate: Estimate | None = None,
    ) -> None:
        self._pool = pool
        self.step = NightStep(pool, name="portrait", trace=TRACE)  # the frame's spend
        self._profile = profile
        self._owner = owner
        self._clock = clock
        self._write = write
        self._estimate = estimate

    def steps(self) -> dict[str, Callable[[date], Any]]:
        return {"m2_portrait": self.build, "m3_offer": self.offer}

    async def _sources(self, conn: AsyncConnection, *, cap: bool = True) -> list[rules.Source]:
        cur = conn.cursor(row_factory=dict_row)
        rows = await (
            await cur.execute(
                "SELECT id::text AS id, kind, statement, state, sensitive, subject_key FROM profile_claims "
                "WHERE state = 'active' AND (expires_at IS NULL OR expires_at > %s) "
                "ORDER BY hard DESC, confidence DESC, updated_at DESC",
                (self._clock(),),
            )
        ).fetchall()
        kept = [rules.Source(r["id"], r["kind"], r["statement"]) for r in rows if rules.usable(r)]
        return kept[: rules.CLAIMS_READ] if cap else kept

    # --- reading --------------------------------------------------------------------------------------------

    async def _row(self, conn: AsyncConnection) -> dict[str, Any] | None:
        cur = conn.cursor(row_factory=dict_row)
        return await (
            await cur.execute(
                "SELECT text, themes, built_at, claims_used, claims_hash, lang, source_ids FROM "
                "profile_portrait WHERE id = 1"
            )
        ).fetchone()

    async def get(self) -> dict[str, Any]:
        """GET /v1/profile/portrait."""
        async with self._pool.connection() as conn:
            row = await self._row(conn)
            # every usable claim, not the night's top 60 (a line or the paragraph never leaves
            # because a newer claim outranked its own)
            live = {s.id for s in await self._sources(conn, cap=False)}
        if row is None:
            return {"text": "", "themes": [], "built_at": None, "claims_used": 0}
        themes = [
            dict(t, lines=[ln for ln in t["lines"] if set(ln["claim_ids"]) <= live])
            for t in row["themes"] or []
        ]
        themes = [t for t in themes if t["lines"]]
        too_little = len(live) < rules.MIN_CLAIMS or not themes
        # the paragraph has no claim_ids of its own: once a claim it was written from is gone
        # (forgotten, said wrong, corrected, retired) it is not shown until the night writes it again
        stale = not set(row["source_ids"] or []) <= live
        return {
            "text": "" if too_little or stale else row["text"],
            "themes": [] if too_little else themes,
            "built_at": row["built_at"].astimezone(self._owner.zone).isoformat(),
            "claims_used": int(row["claims_used"]),
        }

    # --- the night ------------------------------------------------------------------------------------------

    async def _store(
        self,
        conn: AsyncConnection,
        portrait: dict[str, Any],
        sources: Sequence[rules.Source],
        digest: str,
        lang: str,
    ) -> None:
        await conn.execute(
            "INSERT INTO profile_portrait (id, text, themes, built_at, claims_used, claims_hash, lang, "
            "source_ids) VALUES (1, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET text = "
            "EXCLUDED.text, themes = EXCLUDED.themes, built_at = EXCLUDED.built_at, claims_used = "
            "EXCLUDED.claims_used, claims_hash = EXCLUDED.claims_hash, lang = EXCLUDED.lang, source_ids = "
            "EXCLUDED.source_ids",
            (portrait["text"], Jsonb(portrait["themes"]), self._clock(), len(sources), digest, lang,
             Jsonb([x.id for x in sources])),
        )  # fmt: skip

    async def build(self, night: date) -> int:
        """The night's portrait (see the module). Returns 1 when a new one was written by the model."""
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            sources = await self._sources(conn)
            digest = rules.claims_hash(sources, lang)
            row = await self._row(conn)
            if row is not None and row["claims_hash"] == digest:
                return 0  # nothing changed since the last portrait
            if len(sources) < rules.MIN_CLAIMS:
                await self._store(conn, rules.empty(), sources, digest, lang)
                return 0  # Pisa knows too little: no call
        if self._write is None:
            return 0
        spent, dearest = await self.step.spent(self.step.tonight(night))
        items = [f"{s.kind}: {s.statement}" for s in sources]
        est = await self._estimate(items, lang) if self._estimate is not None else None
        if spent + max(memory_rules.EST_CALL_EUR, dearest, est or 0) > memory_rules.NIGHT_EUR:
            await self._capped(night)
            return 0
        try:
            raw = await self._write(
                items,
                lang,
                trace=f"{TRACE}-{night.isoformat()}-portrait",
            )
        except PisaError as err:
            if err.code == "budget_exceeded":
                await self._capped(night)
                return 0
            _log.info("portrait_failed", error=err.code)  # no model, no key: the old portrait stays
            return 0
        except Exception as exc:  # a model's bad answer: the old portrait stays, the next night tries again
            _log.info("portrait_failed", error=exc.__class__.__name__)
            return 0
        portrait = rules.checked(raw, sources, lang)
        async with self._pool.connection() as conn:
            await self._store(conn, portrait, sources, digest, lang)
        _log.info("portrait_built", themes=len(portrait["themes"]), claims=len(sources))
        return 1

    async def _capped(self, night: date) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO profile_extract_nights (night, capped, at) VALUES (%s, true, %s) ON "
                "CONFLICT (night) "
                "DO UPDATE SET capped = true",
                (night, self._clock()),
            )
        _log.info("portrait_capped")

    # --- their corrections
    # ------------------------------------------------------------------------------------

    async def correct(self, claim_ids: Sequence[str], answer: str, words: str | None) -> dict[str, Any]:
        """POST /v1/profile/portrait/lines."""
        ids = [str(c) for c in claim_ids]
        if not 1 <= len(ids) <= rules.CLAIM_IDS_MAX:
            raise _bad(f"claim_ids takes 1 to {rules.CLAIM_IDS_MAX} ids: the line's own.")
        if answer not in ("wrong", "correct"):
            raise _bad("answer is wrong or correct.")
        said = " ".join((words or "").split())
        if answer == "correct" and not 1 <= len(said) <= rules.WORDS_MAX:
            raise _bad(f"correct takes their words, 1 to {rules.WORDS_MAX} characters.")
        current = await self.get()
        at = rules.find_line(current["themes"], ids)
        if at is None:
            raise PisaError(
                "not_found", "No line of the portrait has these claim_ids.", "Refresh the portrait"
            )
        async with self._pool.connection() as conn:
            lang = "it-IT" if (await self._owner.lang(conn)).startswith("it") else "en-US"
        kept: str | None = None
        if answer == "wrong":
            await self._profile.said_wrong(
                ids, "Non è vero (il ritratto)" if lang == "it-IT" else "Not true (portrait)"
            )
        else:
            claim = await self._profile.edit(ids[0], statement=said, scope=None, hard=None)
            await self._profile.said_wrong(
                ids[1:], "Corretto (il ritratto)" if lang == "it-IT" else "Corrected"
            )
            kept = None if claim["sensitive"] else ids[0]  # their words now sensitive: off the portrait
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._row(conn)
            if row is not None:
                themes = row["themes"] or []
                stored = rules.find_line(themes, ids)
                if stored is not None:
                    themes = rules.corrected(themes, stored, said if kept else None, kept)
                    await conn.execute(  # the paragraph may say what they just corrected
                        "UPDATE profile_portrait SET themes = %s, text = '' WHERE id = 1", (Jsonb(themes),)
                    )
        _log.info("portrait_corrected", answer=answer, claims=len(ids))
        return await self.get()

    # --- the offer ------------------------------------------------------------------------------------------

    async def offer(self, night: date) -> int:
        """The "Conosciamoci" talk, offered once when Pisa said yesterday it doesn't know what the user likes.
        Returns 1 if sent."""
        now = self._clock()
        talk = await self._profile.talk()
        if talk.get("state") != "not_started":
            return 0
        start = datetime.combine(night - timedelta(days=1), time(0), tzinfo=self._owner.zone)
        end = datetime.combine(night, time(0), tzinfo=self._owner.zone)
        async with self._pool.connection() as conn, conn.transaction():
            if await self._profile.paused_until(conn) is not None:
                return 0
            last = await get_setting(conn, OFFERED_KEY)
            if last and night - date.fromisoformat(str(last)) < OFFER_EVERY:
                return 0
            replies = await (
                await conn.execute(
                    "SELECT content FROM messages WHERE role = 'assistant' AND created_at >= %s AND "
                    "created_at < %s",
                    (start, end),
                )
            ).fetchall()
            if not any(rules.said_unknown(str(r[0])) for r in replies):
                return 0
            lang = "it-IT" if (await self._owner.lang(conn)).startswith("it") else "en-US"
            # the chip rides a check-in: the app reads choices only from `checkIn`
            await enqueue(
                conn, kind="info", text=OFFER_TEXT[lang], lang=lang, now=now, timezone=self._owner.tz,
                check_in_id=await follow.open_offer(conn, OFFER_PATH),
                choices=[{"id": OFFER_ID, "label": OFFER_CHOICE[lang]}], expects_reply=True,
                path="profile.talk_offer",
            )  # fmt: skip
            await set_setting(conn, OFFERED_KEY, Jsonb(night.isoformat()))
        _log.info("portrait_talk_offered")
        return 1
