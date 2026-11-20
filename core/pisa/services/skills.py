"""The turn's skills (pisa.brain.skills), retrieved by embeddings in pgvector when the user picked an
embedding model for `memory.embed`, else by the free trigram index. Each skill's vector is made once (again
when its key text or the model changes); a turn embeds the user's words once (a paid call, in the ledger,
slot `memory.embed`). Any failure of the embedding path falls back to the trigram index: the turn never waits
on it. The previous turn's skills are kept per device, for a short follow-up."""

import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Literal

from psycopg_pool import AsyncConnectionPool

from pisa.brain.models.catalog import get_model
from pisa.brain.models.slots import get_slot
from pisa.brain.skills import SKILLS_DIR, Library, Skill, contexts, load
from pisa.domain.owner import Owner
from pisa.failures import external_failure
from pisa.infra import ledger
from pisa.infra.fx import usd_to_eur
from pisa.infra.hard_limit import HardLimits
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger

SLOT = "memory.embed"
InputType = Literal["query", "document"]
# (texts, what they are) → (the model's ref, one vector each), or None when no embedding model is picked
Embed = Callable[[Sequence[str], InputType], Awaitable[tuple[str, list[list[float]]] | None]]
_log = get_logger("pisa.skills")


def key_hash(skill: Skill) -> str:
    return hashlib.sha256(skill.key_text.encode()).hexdigest()


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


_LIBRARY: list[tuple[tuple[tuple[str, int, int], ...], Library]] = []


def _library() -> Library:
    """The reviewed skills' index, built once per set of skill files (their names, sizes and
    times): a runtime is built per test and, on the server, by every minute's job; the index is read-only."""
    stamp = tuple((p.name, (st := p.stat()).st_size, st.st_mtime_ns) for p in sorted(SKILLS_DIR.glob("*.md")))
    if not _LIBRARY or _LIBRARY[0][0] != stamp:
        _LIBRARY[:] = [(stamp, Library(load()))]
    return _LIBRARY[0][1]


class SkillsService:
    def __init__(self, pool: AsyncConnectionPool, embed: Embed | None = None) -> None:
        self._pool = pool
        self._embed = embed
        self.library = _library()
        self._previous: dict[uuid.UUID, list[str]] = {}

    async def find(
        self,
        words: str,
        device: uuid.UUID | None,
        hour: int,
        *,
        researched: bool = False,
        drawing: bool = False,
        blocks: bool = False,
    ) -> list[tuple[float, Skill]]:
        """The skills for this turn: by embeddings when there is a model, else by trigrams; plus the pins
        (the night, research) and, for a short follow-up, the previous turn's."""
        ranked = await self._ranked(words)
        found = self.library.search(
            words,
            context=contexts(hour, researched, drawing, blocks),
            previous=self._previous.get(device, []) if device is not None else [],
            ranked=ranked,
        )
        if device is not None:
            self._previous[device] = [s.name for _, s in found]
        return found

    async def _ranked(self, words: str) -> list[tuple[float, Skill]] | None:
        if self._embed is None or not words.strip():
            return None
        try:
            asked = await self._embed([words], "query")
            if asked is None:
                return None
            model_ref, (query,) = asked
            await self._ensure(model_ref)
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT name, 1 - (embedding <=> %s::vector) FROM skill_vectors WHERE model_ref = %s "
                        "ORDER BY embedding <=> %s::vector",
                        (_vector(query), model_ref, _vector(query)),
                    )
                ).fetchall()
        except Exception as err:  # the trigram index answers: a turn never waits on embeddings
            _log.warning("skills_embed_failed", error=type(err).__name__)
            return None
        by_name = {s.name: s for s in self.library.skills if not s.pin}
        return [(float(score), by_name[name]) for name, score in rows if name in by_name]

    async def _ensure(self, model_ref: str) -> None:
        """The skills' vectors for this model: made for the ones missing or changed, in one call."""
        assert self._embed is not None
        async with self._pool.connection() as conn:
            have = dict(
                await (
                    await conn.execute(
                        "SELECT name, key_hash FROM skill_vectors WHERE model_ref = %s", (model_ref,)
                    )
                ).fetchall()
            )
        stale = [s for s in self.library.skills if not s.pin and have.get(s.name) != key_hash(s)]
        if not stale:
            return
        made = await self._embed([s.key_text for s in stale], "document")
        if made is None:
            return
        _, vectors = made
        async with self._pool.connection() as conn:
            for skill, vec in zip(stale, vectors, strict=True):
                await conn.execute(
                    "INSERT INTO skill_vectors (name, key_hash, model_ref, embedding, updated_at) "
                    "VALUES (%s, %s, %s, %s::vector, now()) ON CONFLICT (name) DO UPDATE SET "
                    "key_hash = EXCLUDED.key_hash, model_ref = EXCLUDED.model_ref, "
                    "embedding = EXCLUDED.embedding, updated_at = now()",
                    (skill.name, key_hash(skill), model_ref, _vector(vec)),
                )


def gateway_embed(
    pool: AsyncConnectionPool,
    keys: KeyStore,
    clock: Callable[[], datetime],
    owner: Owner,
    hard_limits: HardLimits,
) -> Embed:
    """The production embedder: the `memory.embed` slot's model (OpenAI's embeddings API), its key from Pisa's
    store, each call reserved and settled in the ledger. None when no model is picked or it isn't OpenAI's."""

    async def embed(texts: Sequence[str], kind: InputType) -> tuple[str, list[list[float]]] | None:
        from pydantic_ai.embeddings import Embedder
        from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
        from pydantic_ai.providers.openai import OpenAIProvider

        async with pool.connection() as conn:
            slot = await get_slot(conn, SLOT)
            if not slot.model or not slot.model.startswith("openai:"):
                return None
            key = await keys.get(conn, "openai")
            model = await get_model(conn, slot.model)
            rate = await usd_to_eur(conn)
            per_mtok = Decimal(str(model.input_usd_per_mtok or 0)) if model is not None else Decimal(0)
            estimate = (per_mtok * Decimal(sum(len(t) for t in texts)) / 4 / 1_000_000 * rate).quantize(
                Decimal("0.000001")
            )
            call_id = await ledger.reserve(
                conn, slot=SLOT, model_ref=slot.model, estimate_eur=estimate, price_known=model is not None,
                trace_id=uuid.uuid4().hex, now=clock(), tz=owner.tz, hard=hard_limits,
            )  # fmt: skip
        if not key:
            return None
        started = time.monotonic()
        model_id = slot.model.split(":", 1)[1]
        try:
            result = await Embedder(
                OpenAIEmbeddingModel(model_id, provider=OpenAIProvider(api_key=key))
            ).embed(list(texts), input_type=kind)
        except Exception as err:
            external_failure("openai", f"embeddings {SLOT}", type(err).__name__, err, key)
            async with pool.connection() as conn:
                spent = int((time.monotonic() - started) * 1000)
                await ledger.fail(conn, call_id, error_code="provider_error", latency_ms=spent, now=clock())
            raise
        tokens = result.usage.input_tokens or 0
        async with pool.connection() as conn:
            cost = (per_mtok * Decimal(tokens) / 1_000_000 * rate).quantize(Decimal("0.000001"))
            spent = int((time.monotonic() - started) * 1000)
            await ledger.settle(
                conn, call_id, input_tokens=tokens, output_tokens=0, cost_eur=cost,
                price_known=model is not None, latency_ms=spent, now=clock(),
            )  # fmt: skip
        return slot.model, [list(v) for v in result.embeddings]

    return embed
