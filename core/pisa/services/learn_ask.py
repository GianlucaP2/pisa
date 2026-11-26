"""Answering a deep question from their sources.

Retrieve (`learn_search`) → answer with the provider's Citations API over one block per paragraph, table,
caption or equation → a cheap judge checks every sentence against the blocks it cites → each sentence is
spoken at its tier (`domain.learn.tier`), a failed one moved down or dropped, never spoken as tier 1 → the
answer, its claims, verdicts and latency in `learn_answer_log`. No passage found: tier 3 at once, no model
call.
A design-rationale question ("perché i layer 5 e 9 e non 7 e 8?") separates the stated reason, the empirical
evidence and the absence; absence is said plainly.
"""

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain import learn as brain
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.domain import learn as rules
from pisa.domain import locale
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.ports.learn import AnswerPassage, LearnModels
from pisa.services.learn_search import LearnSearch, Passage

_log = get_logger("pisa.learn.ask")


def _where(p: Passage, b: Any) -> rules.Where:
    return rules.Where(p.source, b.section, b.page, b.label, b.kind)


class LearnAsk:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        search: LearnSearch,
        models: LearnModels,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._search = search
        self._models = models
        self._clock = clock

    async def ask(self, question: str, course: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        question = question.strip()
        if not question:
            raise PisaError(
                "validation_failed", "The question is empty.", "Ask a question about your sources"
            )
        lang = locale.short(await self._owner.lang(self._pool))  # in the language they set in Settings
        rationale = rules.is_rationale(question)
        found = await self._search.search(question, course)
        said: list[rules.Sentence] = []
        places: dict[int, list[rules.Where]] = {}
        rows: dict[int, list[dict[str, Any]]] = {}
        if found.passages:
            said, places, rows = await self._answer(question, found.passages, lang, rationale)
        text = rules.compose(said, places, lang, rationale=rationale)
        latency = int((time.monotonic() - started) * 1000)
        answer_id = uuid.uuid4()
        claims = [
            {
                "text": s.text,
                "tier": s.tier,
                "verdict": s.verdict,
                "cites": [
                    {"source_id": str(found.passages[c.passage].source_id), "ord": o}
                    for c in s.cites
                    if c.passage < len(found.passages)
                    for o in _ords(found.passages[c.passage], c)
                ],
            }
            for s in said
        ]
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO learn_answer_log (id, course, question, lang, rationale, claims, passages, "
                "lexical_only, tiers, latency_ms, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    answer_id,
                    course,
                    question,
                    lang,
                    rationale,
                    Jsonb(claims),
                    Jsonb([p.chunk_id for p in found.passages]),
                    found.lexical_only,
                    rules.tier_counts(said),
                    latency,
                    self._clock(),
                ),
            )
        _log.info(
            "learn_answer",
            answer_id=str(answer_id),
            passages=len(found.passages),
            tiers=rules.tier_counts(said),
            latency_ms=latency,
            lexical_only=found.lexical_only,
        )
        return {
            "answer_id": answer_id,
            "answer": text,
            "lang": lang,
            "rationale": rationale,
            "abstained": rules.abstained(said),
            "lexical_only": found.lexical_only,
            "latency_ms": latency,
            "sentences": [
                {"text": s.text, "tier": s.tier, "verdict": s.verdict, "citations": rows.get(i, [])}
                for i, s in enumerate(said)
                if s.tier
            ],
            "passages": [p.chunk_id for p in found.passages],
        }

    async def _answer(
        self, question: str, passages: Sequence[Passage], lang: Literal["it", "en"], rationale: bool
    ) -> tuple[list[rules.Sentence], dict[int, list[rules.Where]], dict[int, list[dict[str, Any]]]]:
        shown = [
            AnswerPassage(str(p.source_id), p.title(lang), tuple(b.text for b in p.blocks)) for p in passages
        ]
        blocks = await self._models.answer(question, shown, lang, rationale)
        said = rules.sentences(blocks)
        places: dict[int, list[rules.Where]] = {}
        cited_texts: dict[int, list[str]] = {}
        descriptions: dict[int, bool] = {}
        rows: dict[int, list[dict[str, Any]]] = {}
        for i, s in enumerate(said):
            texts, wheres, desc, cites = [], [], [], []
            for c in s.cites:
                if c.passage >= len(passages):
                    continue
                p = passages[c.passage]
                for b in p.blocks[c.block_from : c.block_to + 1]:
                    texts.append(b.text)
                    wheres.append(_where(p, b))
                    desc.append(b.is_description)
                    cites.append(
                        {
                            "source_id": p.source_id,
                            "ord": b.ord,
                            "quote": c.cited_text or None,
                            **_cite(_where(p, b)),
                        }
                    )
            s.cites = [c for c in s.cites if c.passage < len(passages)]
            places[i], cited_texts[i], rows[i] = wheres, texts, cites
            descriptions[i] = bool(desc) and all(desc)
        verdicts = await asyncio.gather(
            *(
                self._models.judge(s.text, cited_texts[i]) if s.cites and cited_texts[i] else _none()
                for i, s in enumerate(said)
            )
        )
        for i, (s, v) in enumerate(zip(said, verdicts, strict=True)):
            s.verdict = v
            s.tier = rules.tier(s, v, only_descriptions=descriptions[i])
            if s.tier == 3 and not rules.plain_absence(s.text, question):
                s.tier = 0  # an unchecked claim riding on an absence phrase is never spoken
        return said, places, rows


async def _none() -> None:
    return None


def _ords(p: Passage, c: rules.Cite) -> list[int]:
    return [b.ord for b in p.blocks[c.block_from : c.block_to + 1]]


def _cite(w: rules.Where) -> dict[str, Any]:
    return {"source": w.source, "section": w.section, "page": w.page, "label": w.label, "kind": w.kind}


# --- the models, through Pisa's gateway
# ------------------------------------------------------------------------


class GatewayLearnModels:
    """The production models: the answer on `learn.answer` (an Anthropic model: the Citations API), the
    translation, the reranking and the judge on the cheapest model slot Pisa has, with `learn.answer` as
    their fallback. Every call metered and capped by the gateway."""

    def __init__(self, gateway: ModelGateway, pool: AsyncConnectionPool) -> None:
        self._gateway = gateway
        self._pool = pool

    async def cheapest_slot(self) -> str | None:
        """The slot whose chosen chat model is the cheapest Pisa has (by its catalog price); never the
        embeddings slot."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT s.id FROM model_slots s JOIN model_catalog m ON m.ref = s.model_ref "
                    "WHERE s.model_ref IS NOT NULL AND s.id <> 'memory.embed' AND m.input_usd_per_mtok IS "
                    "NOT NULL "
                    "AND NOT ('embeddings' = ANY(m.capabilities) AND NOT 'tools' = ANY(m.capabilities)) "
                    "ORDER BY m.input_usd_per_mtok + coalesce(m.output_usd_per_mtok, 0), s.id LIMIT 1"
                )
            ).fetchone()
        return row[0] if row else None

    async def _cheap(self, run: Callable[[Any], Any], prompt_chars: int, max_tokens: int) -> Any:
        """The call on the cheapest slot, then on `learn.answer`; None when neither answered."""
        slots = [s for s in (await self.cheapest_slot(), brain.ANSWER_SLOT) if s]
        for slot in dict.fromkeys(slots):

            async def call(ctx: CallContext) -> Any:
                out, usage = await run(ctx.model)
                ctx.usage.update(usage)
                return out

            try:
                return await self._gateway.run(
                    slot,
                    call,
                    prompt_chars=prompt_chars,
                    max_output_tokens=max_tokens,
                    ledger_slot=rules.CHECK_SLOT,  # billed and capped as Pisa Learn's
                )
            except PisaError as err:
                if err.code == "budget_exceeded":  # the cap says so: never "my sources don't say"
                    raise
                _log.warning("learn_model_failed", slot=slot, cause=err.code)
            except Exception as err:  # the next slot, or none: the caller's safe default
                _log.warning("learn_model_failed", slot=slot, cause=getattr(err, "code", type(err).__name__))
        return None

    async def translate(self, text: str, to: Literal["it", "en"]) -> str | None:
        return await self._cheap(
            lambda m: brain.translate(m, text, to), len(text) + 300, brain.TRANSLATE_MAX_TOKENS
        )

    async def rerank(self, question: str, passages: Sequence[str], keep: int) -> list[int] | None:
        chars = sum(min(len(p), 1500) for p in passages) + len(question) + 600
        return await self._cheap(
            lambda m: brain.rerank(m, question, passages, keep), chars, brain.RERANK_MAX_TOKENS
        )

    async def judge(self, sentence: str, cited: Sequence[str]) -> rules.Verdict:
        chars = sum(len(c) for c in cited) + len(sentence) + 500
        verdict = await self._cheap(lambda m: brain.judge(m, sentence, cited), chars, brain.JUDGE_MAX_TOKENS)
        return verdict or "unsupported"  # unchecked is never spoken as tier 1

    async def answer(
        self, question: str, passages: Sequence[AnswerPassage], lang: Literal["it", "en"], rationale: bool
    ) -> list[tuple[str, list[rules.Cite]]]:
        shown = [brain.Passage(p.source, p.title, p.blocks) for p in passages]

        async def call(ctx: CallContext) -> list[tuple[str, list[rules.Cite]]]:
            blocks, usage = await brain.cited_answer(ctx.model, question, shown, lang, rationale)
            ctx.usage.input_tokens = (
                usage["input_tokens"] + usage["cache_read_tokens"] + usage["cache_write_tokens"]
            )
            ctx.usage.output_tokens = usage["output_tokens"]
            ctx.usage.cache_read_tokens = usage["cache_read_tokens"]
            ctx.usage.cache_write_tokens = usage["cache_write_tokens"]
            return blocks

        chars = sum(len(b) for p in passages for b in p.blocks) + len(question) + 1500
        return await self._gateway.run(
            brain.ANSWER_SLOT, call, prompt_chars=chars, max_output_tokens=brain.ANSWER_MAX_TOKENS
        )
