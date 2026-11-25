"""Pisa Learn's evaluation (maintenance → learn-eval; the pytest core runs the same metrics on synthetic
documents). The set (`learn_eval_set.json`) holds only arXiv ids, questions and gold locations: the papers are
fetched from arXiv at run time and never committed (arXiv: personal and research use, no re-hosting).

Metrics:
- retrieval recall@8: a question with gold locations counts when one of the 8 passages holds one (same paper,
  and the gold's label, or its anchor words, or its section);
- unsupported-sentence rate: of the sentences the answer model wrote with a citation, the share the judge
  did not find supported or partly supported (they are dropped, never spoken);
- abstention rate: answers with no tier-1 or tier-2 sentence; on the questions whose answer is in no source
  (kind `absent`), the share Pisa correctly abstained on;
- latency: p50 and p95 of the whole answer, in ms.
Numbers only: never a question's answer in a log.
"""

import json
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

SET_PATH = Path(__file__).with_name("learn_eval_set.json")
COURSE = "learn-eval"
K = 8


def load_set(path: Path = SET_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


@dataclass(frozen=True)
class Seen:
    """One retrieved passage as the metric sees it."""

    arxiv_id: str | None
    title: str | None
    text: str
    labels: tuple[str, ...]
    sections: tuple[str, ...]


def holds(gold: dict[str, Any], seen: Seen) -> bool:
    """The passage is from the gold's paper and holds its label, its anchor words or its section."""
    paper = gold.get("arxiv") or gold.get("source")
    if paper and paper not in (seen.arxiv_id, seen.title):
        return False
    label = (gold.get("label") or "").lower()
    if label and any(lb.lower() == label for lb in seen.labels):
        return True
    anchor = _words(gold.get("anchor") or "")
    if anchor and anchor in _words(seen.text):
        return True
    section = (gold.get("section") or "").strip()
    number = section.split(" ", 1)[0] if section else ""
    return bool(number) and not label and not anchor and any(s.startswith(number) for s in seen.sections)


@dataclass
class Scores:
    questions: int = 0
    with_gold: int = 0
    recalled: int = 0
    cited_sentences: int = 0
    unsupported: int = 0
    abstained: int = 0
    absent: int = 0
    absent_abstained: int = 0
    rationale: int = 0
    rationale_recalled: int = 0
    latencies: list[int] = field(default_factory=list)
    errors: int = 0

    def add(
        self,
        q: dict[str, Any],
        seen: Sequence[Seen],
        answer: dict[str, Any] | None,
        claims: list[dict[str, Any]],
    ) -> None:
        self.questions += 1
        gold = q.get("gold") or []
        if answer is None:
            self.errors += 1
            return
        if gold:
            self.with_gold += 1
            hit = any(holds(g, s) for g in gold for s in seen[:K])
            self.recalled += hit
            if q.get("kind") == "rationale":
                self.rationale += 1
                self.rationale_recalled += hit
        cited = [c for c in claims if c.get("cites")]
        self.cited_sentences += len(cited)
        self.unsupported += sum(c.get("verdict") not in ("supported", "partial") for c in cited)
        self.abstained += bool(answer.get("abstained"))
        if q.get("kind") == "absent":
            self.absent += 1
            self.absent_abstained += bool(answer.get("abstained"))
        self.latencies.append(int(answer.get("latency_ms") or 0))

    def report(self) -> dict[str, Any]:
        lat = sorted(self.latencies)

        def pct(p: float) -> int | None:
            return lat[min(len(lat) - 1, round(p * (len(lat) - 1)))] if lat else None

        def share(a: int, b: int) -> float | None:
            return round(a / b, 3) if b else None

        return {
            "questions": self.questions,
            "errors": self.errors,
            "recall_at_8": share(self.recalled, self.with_gold),
            "rationale_recall_at_8": share(self.rationale_recalled, self.rationale),
            "unsupported_sentence_rate": share(self.unsupported, self.cited_sentences),
            "abstention_rate": share(self.abstained, self.questions - self.errors),
            "absent_abstained": share(self.absent_abstained, self.absent),
            "latency_ms_p50": int(statistics.median(lat)) if lat else None,
            "latency_ms_p95": pct(0.95),
        }


async def seen_passages(pool: AsyncConnectionPool, chunk_ids: Sequence[int]) -> list[Seen]:
    """The answer's passages (its log's chunk ids, in order) as the metric sees them."""
    if not chunk_ids:
        return []
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT c.id, s.arxiv_id, s.title, c.text, array_remove(array_agg(g.label), NULL), "
                "array_agg(array_to_string(g.section_path, ' / ')) FROM learn_chunks c "
                "JOIN learn_sources s ON s.id = c.source_id JOIN learn_segments g ON g.source_id = "
                "c.source_id "
                "AND g.ord BETWEEN c.seg_from AND c.seg_to WHERE c.id = ANY(%s) GROUP BY c.id, s.arxiv_id, "
                "s.title",
                (list(chunk_ids),),
            )
        ).fetchall()
    by_id = {r[0]: r for r in rows}
    out = []
    for cid in chunk_ids:
        if (r := by_id.get(cid)) is not None:
            sections = tuple(p.split(" / ")[-1] for p in r[5] if p)
            out.append(Seen(r[1], r[2], r[3], tuple(r[4]), sections))
    return out


async def run(
    pool: AsyncConnectionPool, asker: Any, questions: Sequence[dict[str, Any]], course: str | None
) -> dict[str, Any]:
    """Each question through Pisa's own answer path; the metrics over all."""
    scores = Scores()
    for q in questions:
        try:
            answer = await asker.ask(q["question"], course)
        except Exception:  # counted, never its words
            scores.add(q, [], None, [])
            continue
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT claims FROM learn_answer_log WHERE id = %s", (answer["answer_id"],)
                )
            ).fetchone()
        claims = row[0] if row else []
        scores.add(q, await seen_passages(pool, answer["passages"]), answer, claims)
    return scores.report()
