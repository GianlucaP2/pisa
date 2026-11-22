"""What Pisa Learn's services need from the outside (the sources' hosts, the models) and what the
brain's
course tools need from the services."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pisa.domain.learn import Cite, Verdict

# `memory.embed`'s embedder: (model ref, vectors), or None when no model is chosen
Embed = Callable[
    [Sequence[str], Literal["query", "document"]], Awaitable[tuple[str, list[list[float]]] | None]
]


@dataclass
class Meta:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    licence: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    open_pdf: str | None = None
    open_url: str | None = None


@dataclass(frozen=True)
class Candidate:
    title: str
    year: int | None
    authors: tuple[str, ...]
    doi: str | None
    arxiv_id: str | None
    venue: str | None = None


class LearnFetch(Protocol):
    async def arxiv_meta(self, arxiv_id: str) -> Meta: ...
    async def arxiv_html(self, arxiv_id: str) -> str | None: ...
    async def arxiv_latex(self, arxiv_id: str) -> tuple[str, str] | None: ...
    async def arxiv_pdf(self, arxiv_id: str) -> bytes | None: ...
    async def unpaywall(self, doi: str) -> Meta | None: ...
    async def openalex_doi(self, doi: str) -> Meta | None: ...
    async def openalex_title(self, title: str) -> list[Candidate]: ...
    async def s2_title(self, title: str) -> list[Candidate]: ...
    async def page(self, url: str) -> tuple[str, bytes] | None: ...


@dataclass(frozen=True)
class AnswerPassage:
    source: str  # the source's id
    title: str  # "Zou et al. 2023, §4.2, p. 7"
    blocks: tuple[str, ...]  # one per paragraph, table, caption or equation


class LearnModels(Protocol):
    async def translate(self, text: str, to: Literal["it", "en"]) -> str | None:
        """None: no model, or it failed (the lexical leg uses their words)."""
        ...

    async def rerank(self, question: str, passages: Sequence[str], keep: int) -> list[int] | None:
        """None: neither the cheap model nor the fallback answered (the fused order stands)."""
        ...

    async def answer(
        self, question: str, passages: Sequence[AnswerPassage], lang: Literal["it", "en"], rationale: bool
    ) -> list[tuple[str, list[Cite]]]: ...

    async def judge(self, sentence: str, cited: Sequence[str]) -> Verdict: ...


class LearnData(Protocol):
    """What the brain's course tools call."""

    async def ask(self, question: str, course: str | None = None) -> dict[str, Any]: ...

    async def sources(self, course: str | None = None) -> list[dict[str, Any]]: ...
