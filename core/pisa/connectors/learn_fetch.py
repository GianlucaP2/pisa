"""Where Pisa Learn's sources come from, over HTTP. arXiv (its HTML, the LaTeX e-print, the PDF and the
abstract page's metadata), Unpaywall and OpenAlex for a DOI's open copy, Semantic Scholar and OpenAlex for a
title. Rate limits are kept: arXiv one request every 3 s, one at a time (its terms, across all of Pisa). No
shadow
libraries: only these hosts and the open copies they point to.

What comes back is outside text: returned as bytes or plain fields, parsed by `domain.learn_parse`,
never shown to a model outside document blocks.
"""

import asyncio
import gzip
import io
import re
import tarfile
import time
from html import unescape
from typing import Any

import httpx

from pisa.connectors.outbound import Outbound, Policy, Resolver, TransportFor, system_resolver
from pisa.connectors.pdf_text import PAPER_MAX_BYTES
from pisa.domain.learn import ARXIV_EVERY_S
from pisa.failures import external_failure
from pisa.logging import get_logger
from pisa.ports.learn import Candidate, Meta

_log = get_logger("pisa.learn.fetch")
USER_AGENT = "Pisa-Learn/1 (a personal research assistant; one user)"
MAX_DOWNLOAD = PAPER_MAX_BYTES  # a download is read as a paper: the same ceiling
TIMEOUT_S = 60.0
REDIRECTS_MAX = 5
# http or https, any port, at most 5 redirects, each hop checked and pinned (`connectors.outbound`)
POLICY = Policy(frozenset({"http", "https"}), None, REDIRECTS_MAX)
_ARXIV_HOSTS = ("arxiv.org", "export.arxiv.org")


class _ArxivGate:
    """One arXiv request at a time, at least 3 s apart (shared by every fetcher in the process)."""

    def __init__(self, every: float) -> None:
        self.every = every
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def wait(self) -> None:
        delay = self.last + self.every - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.last = time.monotonic()


ARXIV_GATE = _ArxivGate(ARXIV_EVERY_S)


class LearnFetcher:
    def __init__(
        self,
        *,
        contact_email: str | None = None,
        gate: _ArxivGate = ARXIV_GATE,
        resolver: Resolver | None = None,
        transport: TransportFor | None = None,
    ) -> None:
        self._email = contact_email
        self._gate = gate
        self._resolve = resolver
        self._transport = transport

    async def _get(self, url: str, **params: Any) -> httpx.Response | None:
        """One GET, arXiv's through the gate; a body over MAX_DOWNLOAD is refused. None on 404."""
        host = httpx.URL(url).host
        if host in _ARXIV_HOSTS:
            async with self._gate.lock:
                await self._gate.wait()
                return await self._send(url, params)
        return await self._send(url, params)

    async def _send(self, url: str, params: dict[str, Any]) -> httpx.Response | None:
        """Each hop checked and pinned: public http(s) hosts only (no SSRF, no DNS rebinding)."""
        fetch = Outbound(
            POLICY,
            resolver=self._resolve or system_resolver,  # looked up now: tests replace the module's
            transport=self._transport,
            timeout=TIMEOUT_S,
        )

        async def headers(_: str) -> dict[str, str]:
            return {"User-Agent": USER_AGENT}

        async with fetch.get(url, params=params, headers=headers) as (final, response):
            return await self._read(final, response)

    async def _read(self, url: str, response: httpx.Response) -> httpx.Response | None:
        """The response with its body read (at most MAX_DOWNLOAD); None on 404."""
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            external_failure("learn", f"GET {httpx.URL(url).host}", response.status_code, "", None)
            response.raise_for_status()
        size = 0
        chunks: list[bytes] = []
        async for part in response.aiter_bytes():
            size += len(part)
            if size > MAX_DOWNLOAD:
                raise ValueError("too_big")
            chunks.append(part)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=b"".join(chunks),
            request=response.request,
        )

    # --- arXiv ---------------------------------------------------------------------------------------------

    async def arxiv_meta(self, arxiv_id: str) -> Meta:
        """The abstract page's citation metadata and its licence link."""
        r = await self._get(f"https://arxiv.org/abs/{arxiv_id}")
        if r is None:
            return Meta(arxiv_id=arxiv_id)
        html = r.text
        meta = {
            m.group(1): unescape(m.group(2))
            for m in re.finditer(r'<meta name="citation_(title|date|doi)" content="([^"]*)"', html)
        }
        authors = [unescape(a) for a in re.findall(r'<meta name="citation_author" content="([^"]*)"', html)]
        licence = None
        if m := re.search(r'<div class="abs-license">.*?<a[^>]*href="([^"]+)"', html, re.S):
            licence = _licence_name(m.group(1))
        year = int(meta["date"][:4]) if meta.get("date", "")[:4].isdigit() else None
        return Meta(
            meta.get("title"),
            authors,
            year,
            licence or "arXiv non-exclusive distribution",
            meta.get("doi"),
            arxiv_id,
        )

    async def arxiv_html(self, arxiv_id: str) -> str | None:
        r = await self._get(f"https://arxiv.org/html/{arxiv_id}")
        if r is None or "ltx_document" not in r.text:
            return None
        return r.text

    async def arxiv_latex(self, arxiv_id: str) -> tuple[str, str] | None:
        """The e-print's LaTeX, `\\input`s inlined from the main file (the one with \\documentclass), and its
        .bbl. None when the e-print is a PDF only."""
        r = await self._get(f"https://arxiv.org/e-print/{arxiv_id}")
        if r is None:
            return None
        return latex_from_eprint(r.content)

    async def arxiv_pdf(self, arxiv_id: str) -> bytes | None:
        r = await self._get(f"https://arxiv.org/pdf/{arxiv_id}")
        return None if r is None else r.content

    # --- DOIs and titles -----------------------------------------------------------------------------------

    async def unpaywall(self, doi: str) -> Meta | None:
        """Unpaywall's best open location (its licence as given; "bronze" is readable, no reuse licence).
        Unpaywall needs a contact address: without one it is skipped and OpenAlex answers."""
        if not self._email:
            return None
        r = await self._get(f"https://api.unpaywall.org/v2/{doi}", email=self._email)
        if r is None:
            return None
        data = r.json()
        best = data.get("best_oa_location") or {}
        return Meta(
            data.get("title"),
            [a.get("family", "") for a in data.get("z_authors") or [] if a.get("family")],
            data.get("year"),
            best.get("license") or data.get("oa_status"),
            doi,
            None,
            best.get("url_for_pdf"),
            best.get("url_for_landing_page") or best.get("url"),
        )

    async def openalex_doi(self, doi: str) -> Meta | None:
        r = await self._get(f"https://api.openalex.org/works/doi:{doi}")
        return None if r is None else _openalex_meta(r.json())

    async def openalex_title(self, title: str) -> list[Candidate]:
        r = await self._get("https://api.openalex.org/works", search=title, per_page=5)
        if r is None:
            return []
        return [_openalex_candidate(w) for w in r.json().get("results") or []]

    async def s2_title(self, title: str) -> list[Candidate]:
        r = await self._get(
            "https://api.semanticscholar.org/graph/v1/paper/search/match",
            query=title,
            fields="title,year,authors,externalIds,venue",
        )
        if r is None:
            return []
        out = []
        for p in r.json().get("data") or []:
            ids = p.get("externalIds") or {}
            out.append(
                Candidate(
                    p.get("title") or "",
                    p.get("year"),
                    tuple(a.get("name", "") for a in p.get("authors") or []),
                    ids.get("DOI"),
                    ids.get("ArXiv"),
                    p.get("venue"),
                )
            )
        return out

    async def page(self, url: str) -> tuple[str, bytes] | None:
        """Any open URL: its content type and body."""
        r = await self._get(url)
        if r is None:
            return None
        return r.headers.get("content-type", "").split(";")[0].strip().lower(), r.content

    async def close(self) -> None:
        """Nothing to close: each fetch opens and closes its own pinned connection."""


def _licence_name(href: str) -> str:
    if m := re.search(r"creativecommons\.org/(?:licenses|publicdomain)/([a-z\-]+)/([0-9.]+)", href):
        kind = m.group(1).upper()
        return "CC0 1.0" if kind == "ZERO" else f"CC {kind} {m.group(2)}"
    if "nonexclusive-distrib" in href:
        return "arXiv non-exclusive distribution"
    return href


def _openalex_meta(w: dict[str, Any]) -> Meta:
    oa = w.get("open_access") or {}
    best = w.get("best_oa_location") or {}
    ids = w.get("ids") or {}
    arxiv = None
    for loc in w.get("locations") or []:
        if m := re.search(
            r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", (loc or {}).get("landing_page_url") or ""
        ):
            arxiv = m.group(1)
    return Meta(
        w.get("title") or w.get("display_name"),
        [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []],
        w.get("publication_year"),
        best.get("license"),
        (ids.get("doi") or "").removeprefix("https://doi.org/") or None,
        arxiv,
        best.get("pdf_url"),
        oa.get("oa_url") or best.get("landing_page_url"),
    )


def _openalex_candidate(w: dict[str, Any]) -> Candidate:
    meta = _openalex_meta(w)
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
    return Candidate(meta.title or "", meta.year, tuple(meta.authors[:3]), meta.doi, meta.arxiv_id, venue)


def latex_from_eprint(data: bytes) -> tuple[str, str] | None:
    """arXiv's e-print: a gzipped tar of the sources, or one gzipped .tex. The main file is the one with
    \\documentclass; its \\input/\\include are inlined (one level deep is the common case; up to 3)."""
    try:
        raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    except OSError:
        return None
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.endswith((".tex", ".bbl")) and member.size < 8_000_000:
                    f = tar.extractfile(member)
                    if f is not None:
                        files[member.name.removeprefix("./")] = f.read().decode("utf-8", "replace")
    except tarfile.TarError:
        if raw[:4] == b"%PDF":
            return None
        files = {"main.tex": raw.decode("utf-8", "replace")}
    main = next((n for n, t in files.items() if n.endswith(".tex") and "\\documentclass" in t), None)
    if main is None:
        return None
    text = files[main]
    for _ in range(3):
        text = re.sub(
            r"\\(?:input|include)\{([^}]+)\}",
            lambda m: files.get(m.group(1) if m.group(1).endswith(".tex") else f"{m.group(1)}.tex", ""),
            text,
        )
    bbl = next((t for n, t in files.items() if n.endswith(".bbl")), "")
    return text, bbl
