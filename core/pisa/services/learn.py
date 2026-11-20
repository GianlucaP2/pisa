"""Pisa Learn's sources. `POST /v1/learn/sources` records each item (an arXiv id, a DOI, a URL, a title,
or their uploaded PDF or Markdown) and answers at once; the ingestion runs in the background, one source at a
time per source (an advisory lock), idempotent: the same item in the same course is one source, and a source
whose parsed text did not change is not indexed again.

Resolution:
- an arXiv id: arXiv's HTML (LaTeXML) or else its LaTeX e-print, plus the PDF's text layer for page numbers;
- a DOI: Unpaywall, then OpenAlex, to the open copy; nothing open: "needs their copy";
- a title: Semantic Scholar and OpenAlex; one exact title match is taken, anything else is returned to them as
  candidates, never picked silently;
- their Markdown is a syllabus hint (`citable=false`), never a source.
Imported text is data, never instructions: it is stored and indexed, and reaches a model only in
document blocks. Logs carry ids and counts only.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.pdf_text import pdf_pages
from pisa.domain import learn as rules
from pisa.domain import learn_parse as parse
from pisa.domain import locale
from pisa.domain.errors import PisaError
from pisa.logging import get_logger
from pisa.ports.learn import Candidate, Embed, LearnFetch, Meta

_log = get_logger("pisa.learn")
EMBED_BATCH = 256  # texts per embeddings call (the provider's own limit is far above)
CANDIDATES_SHOWN = 5
Upload = tuple[str, str, bytes]  # (file name, media type, bytes)
_TS = locale.TS_CONFIGS  # each language's text search configuration


class Unresolved(Exception):
    """A source Pisa can't read yet: `needs_copy` or `candidates`, with what they need to know."""

    def __init__(self, state: str, error: str, candidates: list[dict[str, Any]] | None = None) -> None:
        super().__init__(state)
        self.state, self.error, self.candidates = state, error, candidates


def short_name(authors: Sequence[str], year: int | None, title: str | None) -> str:
    """How a spoken citation names a source: "Zou et al. 2023", else the title's first words."""
    if authors:
        last = authors[0].split(",")[0].split()[-1] if authors[0].strip() else ""
        if last:
            who = (
                last
                if len(authors) == 1
                else (f"{last} e {authors[1].split()[-1]}" if len(authors) == 2 else f"{last} et al.")
            )
            return f"{who} {year}" if year else who
    words = (title or "").split()
    return " ".join(words[:5]) + ("…" if len(words) > 5 else "") if words else "?"


class LearnService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        fetch: LearnFetch,
        *,
        embed: Embed | None = None,
        seal: Callable[[bytes], bytes] | None = None,
        unseal: Callable[[bytes], bytes] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """`seal`/`unseal`: their uploaded files are kept encrypted at rest (their books and papers,
        private); none (tests): stored as given."""
        self._seal = seal or (lambda b: b)
        self._unseal = unseal or (lambda b: b)
        self._pool = pool
        self._fetch = fetch
        self._embed = embed
        self._clock = clock
        self._running: set[asyncio.Task[Any]] = set()
        # one ingestion at a time in this process: arXiv answers one request every 3 s anyway, and each run
        # holds a connection for its lock (many at once starved the pool and left sources `queued`)
        self._one = asyncio.Semaphore(1)

    # --- what they ask
    # ---------------------------------------------------------------------------------------

    async def add(
        self, items: Sequence[str], *, course: str | None = None, upload: Upload | None = None
    ) -> list[dict[str, Any]]:
        """Each item recorded `queued` (or the source it already is) and its ingestion started."""
        rows: list[tuple[str, str, str, dict[str, Any]]] = []
        for raw in items:
            if not raw.strip():
                continue
            item = rules.parse_item(raw)
            rows.append(
                (
                    item.kind,
                    item.key,
                    item.ref,
                    {"arxiv_id": item.arxiv_id, "doi": item.doi, "url": item.url, "citable": True},
                )
            )
        if upload is not None:
            name, media, data = upload
            kind = _upload_kind(name, media)
            digest = hashlib.sha256(data).hexdigest()
            rows.append(
                (kind, f"file:{digest}", name, {"upload": data, "citable": kind != "markdown", "title": name})
            )
        if not rows:
            raise PisaError(
                "validation_failed", "No source was given.", "Send arXiv ids, DOIs, URLs, titles or a file"
            )
        out: list[dict[str, Any]] = []
        now = self._clock()
        async with self._pool.connection() as conn:
            for kind, key, ref, extra in rows:
                row = await (
                    await conn.execute(
                        "INSERT INTO learn_sources (id, course, key, kind, ref, arxiv_id, doi, url, title, "
                        "citable, "
                        "upload, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (coalesce(course, ''), key) DO UPDATE SET "
                        "state = CASE WHEN learn_sources.state = 'failed' THEN 'queued' ELSE "
                        "learn_sources.state END, "
                        "error = CASE WHEN learn_sources.state = 'failed' THEN NULL ELSE "
                        "learn_sources.error END "
                        "RETURNING id, state",
                        (
                            uuid.uuid4(),
                            course,
                            key,
                            kind,
                            ref,
                            extra.get("arxiv_id"),
                            extra.get("doi"),
                            extra.get("url"),
                            extra.get("title"),
                            extra["citable"],
                            self._seal(extra["upload"]) if extra.get("upload") is not None else None,
                            now,
                            now,
                        ),
                    )
                ).fetchone()
                assert row is not None
                if row[1] == "queued":
                    self._spawn(self.ingest(row[0]))
                out.append(await _source(conn, row[0]))
        _log.info("learn_sources_added", count=len(out), course_set=course is not None)
        return out

    async def sources(self, course: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            ids = await (
                await conn.execute(
                    "SELECT id FROM learn_sources WHERE %s::text IS NULL OR course = %s ORDER BY created_at",
                    (course, course),
                )
            ).fetchall()
            return [await _source(conn, r[0]) for r in ids]

    async def delete(self, source_id: uuid.UUID) -> int:
        """Their delete: the source, its segments and chunks (cascade)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("DELETE FROM learn_sources WHERE id = %s", (source_id,))
        if not cur.rowcount:
            raise PisaError(
                "not_found", "There is no such source.", "List your sources with GET /v1/learn/sources"
            )
        _log.info("learn_source_deleted", source_id=str(source_id))
        return cur.rowcount

    # --- the background job ---------------------------------------------------------------------------------

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(work)
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def resume(self) -> None:
        """At start: a source a restart interrupted is ingested again (idempotent)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id FROM learn_sources WHERE state IN ('queued', 'fetching', 'indexing')"
                )
            ).fetchall()
        for (source_id,) in rows:
            self._spawn(self.ingest(source_id))

    async def settled(self) -> None:
        while self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def close(self) -> None:
        for task in self._running:
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def ingest(self, source_id: uuid.UUID) -> str:
        """Resolve, fetch, parse and index one source; its final state. One run at a time per source (across
        processes: an advisory lock) and one source at a time in this process. Whatever breaks leaves the
        source `failed` with its cause, never `queued` forever."""
        lock = int.from_bytes(hashlib.sha256(f"learn:{source_id}".encode()).digest()[:8], "big", signed=True)
        async with self._one:
            try:
                async with self._pool.connection() as guard:
                    got = await (await guard.execute("SELECT pg_try_advisory_lock(%s)", (lock,))).fetchone()
                    if not got or not got[0]:
                        return "running"
                    try:
                        return await self._ingest(source_id)
                    finally:
                        await guard.execute("SELECT pg_advisory_unlock(%s)", (lock,))
            except asyncio.CancelledError:
                raise  # a shutdown: left for the resume
            except Exception as err:
                _log.warning("learn_ingest_crashed", source_id=str(source_id), cause=type(err).__name__)
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE learn_sources SET state = 'failed', error = %s, updated_at = %s "
                        "WHERE id = %s",
                        (_cause(err), self._clock(), source_id),
                    )
                return "failed"

    async def _ingest(self, source_id: uuid.UUID) -> str:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT kind, ref, arxiv_id, doi, url, upload, title, content_hash, state "
                    "FROM learn_sources WHERE id = %s",
                    (source_id,),
                )
            ).fetchone()
            if row is None:
                return "gone"
            await _state(conn, source_id, "fetching", self._clock())
        kind, ref, arxiv_id, doi, url, sealed, title, old_hash, _ = row
        upload = self._unseal(bytes(sealed)) if sealed is not None else None
        try:
            meta, parsed, origin, parser = await self._resolve(kind, ref, arxiv_id, doi, url, upload, title)
        except Unresolved as stop:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE learn_sources SET state = %s, error = %s, candidates = %s, updated_at = %s "
                    "WHERE id = %s",
                    (
                        stop.state,
                        stop.error,
                        Jsonb(stop.candidates) if stop.candidates is not None else None,
                        self._clock(),
                        source_id,
                    ),
                )
            _log.info("learn_source_unresolved", source_id=str(source_id), state=stop.state)
            return stop.state
        except Exception as err:  # the cause, named, never the text
            cause = _cause(err)
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE learn_sources SET state = 'failed', error = %s, updated_at = %s WHERE id = %s",
                    (cause, self._clock(), source_id),
                )
            _log.warning("learn_source_failed", source_id=str(source_id), cause=type(err).__name__)
            return "failed"
        return await self._index(source_id, meta, parsed, origin, parser, old_hash)

    async def _resolve(
        self,
        kind: str,
        ref: str,
        arxiv_id: str | None,
        doi: str | None,
        url: str | None,
        upload: bytes | None,
        title: str | None,
    ) -> tuple[Meta, parse.Parsed, str, str]:
        if kind == "arxiv" and arxiv_id:
            return await self._arxiv(arxiv_id)
        if kind == "doi" and doi:
            return await self._doi(doi)
        if kind == "url" and url:
            got = await self._fetch.page(url)
            if got is None:
                raise Unresolved("failed", f"not_found: {url} answered 404. Check the link and add it again.")
            return await self._open_copy(Meta(open_url=url), got, "web")
        if kind == "title":
            return await self._title(ref)
        if kind == "pdf" and upload is not None:
            outcome, pages = await pdf_pages(upload)
            if outcome != "read":
                raise Unresolved(
                    "failed", f"pdf_{outcome}: the PDF could not be read ({outcome}). Send another copy."
                )
            return Meta(title=title), parse.pdf_pages(pages, title), "upload", "pdf_text"
        if kind == "markdown" and upload is not None:
            parsed = parse.markdown(upload.decode("utf-8", "replace"))
            return Meta(title=parsed.title or title), parsed, "upload", "markdown"
        raise Unresolved("failed", "validation_failed: nothing to read for this source. Add it again.")

    async def _arxiv(self, arxiv_id: str, doi: str | None = None) -> tuple[Meta, parse.Parsed, str, str]:
        meta = await self._fetch.arxiv_meta(arxiv_id)
        meta.arxiv_id, meta.doi = arxiv_id, meta.doi or doi
        html = await self._fetch.arxiv_html(arxiv_id)
        parsed, origin, parser = None, "", ""
        if html:
            parsed, origin, parser = parse.arxiv_html(html), "arxiv_html", "latexml_html"
        else:
            tex = await self._fetch.arxiv_latex(arxiv_id)
            if tex is not None:
                parsed, origin, parser = parse.latex(*tex), "arxiv_latex", "latex"
        pdf = await self._fetch.arxiv_pdf(arxiv_id)
        pages: list[str] = []
        if pdf:
            _, pages = await pdf_pages(pdf)
        if parsed is None or not parsed.segments:
            if not pages:
                raise Unresolved("failed", f"not_found: arXiv has no readable text for {arxiv_id}.")
            parsed, origin, parser = parse.pdf_pages(pages, meta.title), "arxiv_pdf", "pdf_text"
        else:
            parsed = parse.with_pages(parsed, pages)
        meta.title = meta.title or parsed.title
        return meta, parsed, origin, parser

    async def _doi(self, doi: str) -> tuple[Meta, parse.Parsed, str, str]:
        if a := rules.parse_item(doi).arxiv_id:
            return await self._arxiv(a, doi)
        meta = await self._fetch.unpaywall(doi)
        alex = await self._fetch.openalex_doi(doi)
        if meta is None or not (meta.open_pdf or meta.open_url):
            meta = alex if alex is not None else meta
        if meta is None:
            raise Unresolved(
                "needs_copy", f"needs_copy: no open copy of {doi} was found. Upload your copy (PDF)."
            )
        if alex is not None and alex.arxiv_id:
            return await self._arxiv(alex.arxiv_id, doi)
        for link in (meta.open_pdf, meta.open_url):
            if not link:
                continue
            got = await self._fetch.page(link)
            if got is not None:
                meta.doi = doi
                return await self._open_copy(meta, got, "open_access")
        raise Unresolved(
            "needs_copy", f"needs_copy: no open copy of {doi} was found. Upload your copy (PDF)."
        )

    async def _open_copy(
        self, meta: Meta, got: tuple[str, bytes], origin: str
    ) -> tuple[Meta, parse.Parsed, str, str]:
        media, body = got
        if media == "application/pdf" or body[:4] == b"%PDF":
            outcome, pages = await pdf_pages(body)
            if outcome != "read":
                raise Unresolved(
                    "needs_copy", f"pdf_{outcome}: the open PDF could not be read. Upload your copy."
                )
            return meta, parse.pdf_pages(pages, meta.title), origin, "pdf_text"
        parsed = parse.web_page(body.decode("utf-8", "replace"))
        if not parsed.segments:
            raise Unresolved(
                "needs_copy", "needs_copy: the page has no readable text. Upload your copy (PDF)."
            )
        meta.title = meta.title or parsed.title
        return meta, parsed, origin, "html"

    async def _title(self, title: str) -> tuple[Meta, parse.Parsed, str, str]:
        found: list[Candidate] = []
        for search in (self._fetch.s2_title, self._fetch.openalex_title):
            try:
                found += await search(title)
            except Exception as err:  # one index down: the other still answers
                _log.warning("learn_title_search_failed", cause=type(err).__name__)
        unique: dict[str, Candidate] = {}
        for c in found:
            unique.setdefault(_paper_key(c), c)
        cands = list(unique.values())
        exact = rules.exact_title_matches(title, [c.title for c in cands])
        if len(exact) == 1:
            pick = cands[exact[0]]
            if pick.arxiv_id:
                return await self._arxiv(pick.arxiv_id, pick.doi)
            if pick.doi:
                return await self._doi(pick.doi)
        shown = [
            {
                "title": c.title,
                "year": c.year,
                "authors": list(c.authors[:3]),
                "venue": c.venue,
                "item": f"arXiv:{c.arxiv_id}" if c.arxiv_id else c.doi,
            }
            for c in cands[:CANDIDATES_SHOWN]
            if c.arxiv_id or c.doi
        ]
        if not shown:
            raise Unresolved(
                "needs_copy", "not_found: no paper with this title was found. Send its DOI or a PDF."
            )
        raise Unresolved(
            "candidates",
            "ambiguous: more than one paper could be this title. Choose one (add its item).",
            shown,
        )

    async def _index(
        self,
        source_id: uuid.UUID,
        meta: Meta,
        parsed: parse.Parsed,
        origin: str,
        parser: str,
        old: str | None,
    ) -> str:
        segs = parsed.segments
        if not segs:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE learn_sources SET state = 'failed', error = %s, updated_at = %s WHERE id = %s",
                    ("empty: no text was found in this source. Send another copy.", self._clock(), source_id),
                )
            return "failed"
        digest = hashlib.sha256(
            "\n".join(f"{s.kind}|{s.label}|{s.page_from}|{s.text}" for s in segs).encode()
        ).hexdigest()
        title = meta.title or parsed.title or "?"
        lang = rules.question_lang(" ".join(s.text for s in segs[:20]))
        plans = rules.chunk(segs)
        async with self._pool.connection() as conn:
            await _state(conn, source_id, "indexing", self._clock())
            current = await (
                await conn.execute("SELECT embed_model FROM learn_sources WHERE id = %s", (source_id,))
            ).fetchone()
        # unchanged text, and its vectors made already (or no embedder to make them): nothing to redo
        if old == digest and current is not None and (current[0] is not None or self._embed is None):
            async with self._pool.connection() as conn:
                await _state(conn, source_id, "ready", self._clock())
            _log.info("learn_source_unchanged", source_id=str(source_id))
            return "ready"
        prefixes = [
            (
                rules.prefix(title, segs[p.seg_from].section_path, segs[p.seg_from].label, "it"),
                rules.prefix(title, segs[p.seg_from].section_path, segs[p.seg_from].label, "en"),
            )
            for p in plans
        ]
        vectors, model = await self._vectors(
            [f"{en}\n{p.text}" for p, (_, en) in zip(plans, prefixes, strict=True)]
        )
        quality = rules.parse_quality(segs, pages_expected=parsed.has_pages)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM learn_segments WHERE source_id = %s", (source_id,))
            await conn.execute("DELETE FROM learn_chunks WHERE source_id = %s", (source_id,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO learn_segments (source_id, ord, kind, section_path, page_from, page_to, "
                    "label, "
                    "text, is_description, cites) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            source_id,
                            i,
                            s.kind,
                            list(s.section_path),
                            s.page_from,
                            s.page_to,
                            s.label,
                            s.text,
                            s.is_description,
                            list(s.cites),
                        )
                        for i, s in enumerate(segs)
                    ],
                )
                await cur.executemany(
                    "INSERT INTO learn_chunks (source_id, seg_from, seg_to, kind, text, prefix_it, "
                    "prefix_en, "
                    "is_description, ts_config, embedding, embed_model) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::regconfig,%s::vector,%s)",
                    [
                        (
                            source_id,
                            p.seg_from,
                            p.seg_to,
                            p.kind,
                            p.text,
                            it,
                            en,
                            p.is_description,
                            _TS[lang],
                            _vec(vectors[i]) if vectors else None,
                            model if vectors else None,
                        )
                        for i, (p, (it, en)) in enumerate(zip(plans, prefixes, strict=True))
                    ],
                )
            await conn.execute(
                "UPDATE learn_sources SET state = 'ready', error = NULL, candidates = NULL, title = %s, "
                "authors = %s, year = %s, licence = %s, origin = %s, parser = %s, parse_quality = %s, lang "
                "= %s, "
                "arxiv_id = coalesce(arxiv_id, %s), doi = coalesce(doi, %s), content_hash = %s, embed_model "
                "= %s, "
                "segments = %s, chunks = %s, indexed_at = %s, updated_at = %s WHERE id = %s",
                (
                    title,
                    meta.authors,
                    meta.year,
                    meta.licence,
                    origin,
                    parser,
                    quality,
                    lang,
                    meta.arxiv_id,
                    meta.doi,
                    digest,
                    model if vectors else None,
                    len(segs),
                    len(plans),
                    self._clock(),
                    self._clock(),
                    source_id,
                ),
            )
        _log.info(
            "learn_source_ready",
            source_id=str(source_id),
            segments=len(segs),
            chunks=len(plans),
            vectors=bool(vectors),
            quality=quality,
        )
        return "ready"

    async def _vectors(self, texts: list[str]) -> tuple[list[list[float]], str | None]:
        if self._embed is None:
            return [], None
        out: list[list[float]] = []
        model = None
        try:
            for i in range(0, len(texts), EMBED_BATCH):
                made = await self._embed(texts[i : i + EMBED_BATCH], "document")
                if made is None:
                    return [], None
                model, vecs = made
                out += vecs
        except Exception as err:  # lexical only, said in every answer's metadata
            _log.warning("learn_embed_failed", cause=type(err).__name__)
            return [], None
        return out, model


def _upload_kind(name: str, media: str) -> Literal["pdf", "markdown"]:
    lower = name.lower()
    if media == "application/pdf" or lower.endswith(".pdf"):
        return "pdf"
    if media in ("text/markdown", "text/x-markdown", "text/plain") or lower.endswith(
        (".md", ".markdown", ".txt")
    ):
        return "markdown"
    raise PisaError(
        "validation_failed",
        f"Pisa Learn reads PDF and Markdown files, not {media or name}.",
        "Send the paper as a PDF or your notes as Markdown",
    )


def _paper_key(c: Candidate) -> str:
    return (
        f"arxiv:{c.arxiv_id}" if c.arxiv_id else f"doi:{(c.doi or '').lower()}" if c.doi else f"t:{c.title}"
    )


def _vec(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def _cause(err: Exception) -> str:
    name = type(err).__name__
    if str(err) == "too_big":
        return "too_big: the download is over 128 MB. Upload a smaller copy."
    if "HTTPStatusError" in name:
        code = getattr(getattr(err, "response", None), "status_code", "?")
        return (
            f"fetch_failed: the source's server answered HTTP {code}. Pisa tries again when you add it again."
        )
    if "Timeout" in name or "Connect" in name:
        return f"fetch_failed: the source's server did not answer ({name}). Add it again later."
    return f"internal: ingestion failed ({name}). Add it again; if it fails again, tell the team."


async def _state(conn: AsyncConnection, source_id: uuid.UUID, state: str, now: datetime) -> None:
    await conn.execute(
        "UPDATE learn_sources SET state = %s, updated_at = %s WHERE id = %s", (state, now, source_id)
    )


async def _source(conn: AsyncConnection, source_id: uuid.UUID) -> dict[str, Any]:
    row = await (
        await conn.execute(
            "SELECT id, course, kind, ref, arxiv_id, doi, url, title, authors, year, licence, origin, "
            "parser, "
            "parse_quality, citable, state, error, candidates, embed_model, segments, chunks, created_at, "
            "indexed_at FROM learn_sources WHERE id = %s",
            (source_id,),
        )
    ).fetchone()
    assert row is not None
    keys = (
        "id",
        "course",
        "kind",
        "ref",
        "arxiv_id",
        "doi",
        "url",
        "title",
        "authors",
        "year",
        "licence",
        "origin",
        "parser",
        "parse_quality",
        "citable",
        "state",
        "error",
        "candidates",
        "embed_model",
        "segments",
        "chunks",
        "created_at",
        "indexed_at",
    )
    out: dict[str, Any] = dict(zip(keys, row, strict=True))
    out["lexical_only"] = out.pop("embed_model") is None
    return out


class LearnTurnData:
    """What the brain's course tools call (`ports.learn.LearnData`): the answer and their sources' list."""

    def __init__(self, sources: LearnService, asker: Any) -> None:
        self._sources = sources
        self._asker = asker

    async def ask(self, question: str, course: str | None = None) -> dict[str, Any]:
        return await self._asker.ask(question, course)

    async def sources(self, course: str | None = None) -> list[dict[str, Any]]:
        return await self._sources.sources(course)
