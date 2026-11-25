"""Retrieval over their sources.

1. The lexical leg: Postgres full-text search over each chunk's `tsvector` (its text, its English and Italian
   prefixes), the question translated to the corpus's language, and the exact numbers it names ("layer 5",
   "Tabella 3") ranked first as phrases. (Postgres ranks with `ts_rank_cd`, cover density, not BM25 proper.)
2. The vector leg: the chunks' embeddings (`memory.embed`'s model), when one is set; else lexical only, which
   every answer's metadata says.
3. Reciprocal-rank fusion, then one hop along citations: a source a top passage cites, already ingested, gives
   its best chunk.
4. Reranked to about 8 passages by the cheapest model Pisa has (`learn.answer`'s model as the fallback);
   neither answering, the fused order stands. Appendices are indexed like any section.
Only citable sources (never their AI-written study plan) and only ready ones are searched.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, LiteralString, cast

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import learn as rules
from pisa.domain import locale
from pisa.logging import get_logger
from pisa.ports.learn import Embed, LearnModels

_log = get_logger("pisa.learn.search")
_TS = locale.TS_CONFIGS  # each language's text search configuration


@dataclass(frozen=True)
class Block:
    ord: int
    kind: rules.SegKind
    text: str
    section: str | None
    page: int | None
    label: str | None
    is_description: bool


@dataclass(frozen=True)
class Passage:
    chunk_id: int
    source_id: uuid.UUID
    source: str  # its short name ("Zou et al. 2023")
    kind: rules.SegKind
    text: str
    blocks: tuple[Block, ...]
    hop: bool = False  # came by one hop along a citation

    def title(self, lang: Literal["it", "en"]) -> str:
        first = self.blocks[0] if self.blocks else None
        where = rules.Where(
            self.source,
            first.section if first else None,
            first.page if first else None,
            first.label if first else None,
            self.kind,
        )
        return rules.cite_text(where, lang)


@dataclass
class Retrieved:
    passages: list[Passage] = field(default_factory=list)
    lexical_only: bool = True
    translated: str | None = None
    reranked: bool = False


def _section_number(path: list[str] | None) -> str | None:
    """The innermost section as printed ("4.2 Probing setup" → "4.2"; "Appendix B Details" → "B")."""
    if not path:
        return None
    last = path[-1].removeprefix("Appendix ").strip()
    head = last.split(" ", 1)[0]
    return head if any(ch.isdigit() for ch in head) or (len(head) <= 4 and head[:1].isupper()) else last[:40]


class LearnSearch:
    def __init__(self, pool: AsyncConnectionPool, models: LearnModels | None, embed: Embed | None) -> None:
        self._pool = pool
        self._models = models
        self._embed = embed

    async def search(
        self, question: str, course: str | None = None, *, top: int = rules.TOP_PASSAGES
    ) -> Retrieved:
        qlang = rules.question_lang(question)
        async with self._pool.connection() as conn:
            corpus = await _corpus_lang(conn, course)
            if corpus is None:
                return Retrieved()
        translated = None
        if qlang != corpus and self._models is not None:
            translated = await self._models.translate(question, corpus)
        terms = rules.exact_terms(question)
        async with self._pool.connection() as conn:
            lexical = await _lexical(conn, course, translated or question, question, corpus, qlang, terms)
        vector, lexical_only = await self._vector(question, course)
        fused = rules.rrf([lexical, vector])[: rules.CANDIDATES]
        async with self._pool.connection() as conn:
            hops = await _hop(conn, course, fused[:top], translated or question, corpus, terms)
            ids = fused + [h for h in hops if h not in fused]
            passages = await _passages(conn, ids)
        passages = [p if p.chunk_id not in hops else _as_hop(p) for p in passages]
        order = None
        if self._models is not None and len(passages) > 1:
            order = await self._models.rerank(question, [f"{p.title('en')}\n{p.text}" for p in passages], top)
        if order:
            chosen = [passages[i] for i in order][:top]
            # a hop is one hop: kept after the reranked list when the reranker left it out
            chosen += [p for p in passages if p.hop and p not in chosen]
        else:
            base = [p for p in passages if not p.hop][:top]
            chosen = base + [p for p in passages if p.hop]
        _log.info(
            "learn_search",
            candidates=len(fused),
            hops=len(hops),
            kept=len(chosen),
            lexical_only=lexical_only,
            reranked=bool(order),
        )
        return Retrieved(chosen, lexical_only, translated, bool(order))

    async def _vector(self, question: str, course: str | None) -> tuple[list[int], bool]:
        if self._embed is None:
            return [], True
        try:
            made = await self._embed([question], "query")
        except Exception as err:
            _log.warning("learn_query_embed_failed", cause=type(err).__name__)
            return [], True
        if made is None:
            return [], True
        model, (vec,) = made
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT c.id FROM learn_chunks c JOIN learn_sources s ON s.id = c.source_id "
                    "WHERE s.state = 'ready' AND s.citable AND (%s::text IS NULL OR s.course = %s) "
                    "AND c.embed_model = %s ORDER BY c.embedding <=> %s::vector LIMIT %s",
                    (course, course, model, "[" + ",".join(f"{x:.7g}" for x in vec) + "]", rules.CANDIDATES),
                )
            ).fetchall()
        return [r[0] for r in rows], not rows


def _as_hop(p: Passage) -> Passage:
    return Passage(p.chunk_id, p.source_id, p.source, p.kind, p.text, p.blocks, hop=True)


async def _corpus_lang(conn: AsyncConnection, course: str | None) -> Literal["it", "en"] | None:
    row = await (
        await conn.execute(
            "SELECT lang, count(*) FROM learn_sources WHERE state = 'ready' AND citable "
            "AND (%s::text IS NULL OR course = %s) GROUP BY lang ORDER BY count(*) DESC LIMIT 1",
            (course, course),
        )
    ).fetchone()
    return None if row is None else ("it" if row[0] == "it" else "en")


async def _or_query(conn: AsyncConnection, config: str, text: str) -> str:
    """Any of the text's words (OR), as Postgres normalises them in `config`."""
    row = await (
        await conn.execute("SELECT tsvector_to_array(to_tsvector(%s::regconfig, %s))", (config, text))
    ).fetchone()
    lexemes = row[0] if row and row[0] else []
    return " | ".join("'" + lx.replace("'", "''") + "'" for lx in lexemes)


async def _lexical(
    conn: AsyncConnection,
    course: str | None,
    text: str,
    original: str,
    corpus: str,
    qlang: str,
    terms: list[str],
) -> list[int]:
    cfg = _TS[corpus]
    query = await _or_query(conn, cfg, " ".join([text, *terms]))
    # an Italian question also matches the chunks' Italian prefixes in its own words
    extra = await _or_query(conn, _TS["it"], original) if qlang == "it" and corpus != "it" else ""
    if not query and not extra:
        return []
    # the exact numbers first: a passage holding more of them ranks above any count of the other words
    boost = " + ".join(["(c.tsv @@ phraseto_tsquery(%s::regconfig, %s))::int" for _ in terms]) or "0::int"
    params: list[Any] = []
    for t in terms:
        params += [cfg, t]
    text_sql = cast(
        LiteralString,
        "SELECT c.id FROM learn_chunks c JOIN learn_sources s ON s.id = c.source_id, "  # noqa: S608
        "(SELECT CASE WHEN %s = '' THEN NULL ELSE to_tsquery(%s::regconfig, %s) END AS q, "
        "CASE WHEN %s = '' THEN NULL ELSE to_tsquery(%s::regconfig, %s) END AS qi) q "
        "WHERE s.state = 'ready' AND s.citable AND (%s::text IS NULL OR s.course = %s) "
        "AND (c.tsv @@ q.q OR c.tsv @@ q.qi) "
        f"ORDER BY {boost} DESC, coalesce(ts_rank_cd(c.tsv, q.q), 0) + coalesce(ts_rank_cd(c.tsv, q.qi), 0) "
        "DESC, c.id LIMIT %s",
    )
    rows = await (
        await conn.execute(
            text_sql,
            (
                query,
                cfg,
                query or "x",
                extra,
                _TS["it"],
                extra or "x",
                course,
                course,
                *params,
                rules.CANDIDATES,
            ),
        )
    ).fetchall()
    return [r[0] for r in rows]


async def _hop(
    conn: AsyncConnection, course: str | None, top: list[int], text: str, corpus: str, terms: list[str]
) -> list[int]:
    """One hop along citations: the ingested sources the top passages' segments cite, each its best chunk for
    the question (else its first)."""
    if not top:
        return []
    cited = await (
        await conn.execute(
            "SELECT DISTINCT unnest(g.cites) FROM learn_chunks c JOIN learn_segments g ON g.source_id = "
            "c.source_id "
            "AND g.ord BETWEEN c.seg_from AND c.seg_to WHERE c.id = ANY(%s)",
            (top,),
        )
    ).fetchall()
    keys = [r[0] for r in cited]
    if not keys:
        return []
    arxiv = [k.removeprefix("arxiv:") for k in keys if k.startswith("arxiv:")]
    dois = [k.removeprefix("doi:") for k in keys if k.startswith("doi:")]
    sources = await (
        await conn.execute(
            "SELECT id FROM learn_sources WHERE state = 'ready' AND citable AND (%s::text IS NULL OR course "
            "= %s) "
            "AND (arxiv_id = ANY(%s) OR lower(doi) = ANY(%s)) AND id NOT IN "
            "(SELECT source_id FROM learn_chunks WHERE id = ANY(%s))",
            (course, course, arxiv, dois, top),
        )
    ).fetchall()
    query = await _or_query(conn, _TS[corpus], " ".join([text, *terms]))
    out: list[int] = []
    for (source_id,) in sources:
        row = await (
            await conn.execute(
                "SELECT c.id FROM learn_chunks c WHERE c.source_id = %s ORDER BY "
                "CASE WHEN %s = '' THEN 0 ELSE ts_rank_cd(c.tsv, to_tsquery(%s::regconfig, %s)) END DESC, "
                "c.seg_from LIMIT %s",
                (source_id, query, _TS[corpus], query or "x", rules.HOP_PER_SOURCE),
            )
        ).fetchall()
        out += [r[0] for r in row]
    return out


async def _passages(conn: AsyncConnection, ids: list[int]) -> list[Passage]:
    if not ids:
        return []
    rows = await (
        await conn.execute(
            "SELECT c.id, c.source_id, c.kind, c.text, c.seg_from, c.seg_to, s.title, s.authors, s.year "
            "FROM learn_chunks c JOIN learn_sources s ON s.id = c.source_id WHERE c.id = ANY(%s)",
            (ids,),
        )
    ).fetchall()
    by_id = {r[0]: r for r in rows}
    from pisa.services.learn import short_name

    out: list[Passage] = []
    for chunk_id in ids:
        r = by_id.get(chunk_id)
        if r is None:
            continue
        segs = await (
            await conn.execute(
                "SELECT ord, kind, text, section_path, page_from, label, is_description FROM learn_segments "
                "WHERE source_id = %s AND ord BETWEEN %s AND %s ORDER BY ord",
                (r[1], r[4], r[5]),
            )
        ).fetchall()
        blocks = tuple(Block(o, k, t, _section_number(p), pg, lb, d) for o, k, t, p, pg, lb, d in segs)
        out.append(Passage(chunk_id, r[1], short_name(r[7] or [], r[8], r[6]), r[2], r[3], blocks))
    return out
