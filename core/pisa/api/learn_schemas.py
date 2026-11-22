"""The REST shapes of Pisa Learn (the phone lists their sources, deletes one, asks in a course's
context)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class LearnFile(BaseModel):
    name: str = Field(description="The file's name (paper.pdf, plan.md)", max_length=300)
    media_type: str = Field(description="application/pdf, text/markdown or text/plain", max_length=100)
    # base64 of at most the PDF ceiling (128 MB): a larger body is refused before it is decoded
    data: str = Field(description="The file, base64", max_length=128 * 1024 * 1024 * 4 // 3 + 4)


class LearnSourcesAdd(BaseModel):
    items: list[str] = Field(
        default_factory=list,
        description="arXiv ids, DOIs, URLs or titles, one each; the same item in the same course is "
        "one source",
        max_length=200,
    )
    course: str | None = Field(
        None, description="The course they belong to (a name); none: no course", max_length=200
    )
    file: LearnFile | None = Field(
        None,
        description="An uploaded PDF (a source) or Markdown (a study plan: a syllabus hint, never cited)",
    )


class LearnCandidate(BaseModel):
    title: str
    year: int | None = None
    authors: list[str] = Field(default_factory=list)
    venue: str | None = None
    item: str | None = Field(None, description="Send it back in `items` to take this paper")


class LearnSource(BaseModel):
    id: uuid.UUID
    course: str | None
    kind: Literal["arxiv", "doi", "url", "title", "pdf", "markdown"]
    ref: str = Field(description="What they gave")
    arxiv_id: str | None
    doi: str | None
    url: str | None
    title: str | None
    authors: list[str]
    year: int | None
    licence: str | None = Field(description="As the origin states it")
    origin: str | None
    parser: str | None
    parse_quality: float | None = Field(description="0..1: pages, sections and structure found")
    citable: bool = Field(description="false: an AI-written study plan, a syllabus hint, never cited")
    state: Literal["queued", "fetching", "indexing", "ready", "needs_copy", "candidates", "failed"]
    error: str | None = Field(description="Why it isn't ready, and what to do")
    candidates: list[LearnCandidate] | None = Field(description="An ambiguous title: the papers it could be")
    lexical_only: bool = Field(description="No embeddings (no `memory.embed` model): searched by words only")
    segments: int
    chunks: int
    created_at: datetime
    indexed_at: datetime | None


class LearnSources(BaseModel):
    sources: list[LearnSource]


class LearnDeleted(BaseModel):
    deleted: int


class LearnAsk(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    course: str | None = Field(None, description="Search this course's sources only", max_length=200)


class LearnCitation(BaseModel):
    source_id: uuid.UUID
    source: str = Field(description="The source's short name (Zou et al. 2023)")
    section: str | None
    page: int | None
    label: str | None = Field(description="Table 3, Figure 7, Eq. (2)")
    kind: Literal["paragraph", "table", "caption", "equation", "appendix"]
    ord: int = Field(description="The cited segment, in the source's reading order")
    quote: str | None = Field(description="The cited text, as the provider gave it")


class LearnSentence(BaseModel):
    text: str
    tier: Literal[1, 2, 3] = Field(description="1 the sources say; 2 my inference; 3 none of my sources")
    verdict: Literal["supported", "partial", "unsupported", "contradicted"] | None
    citations: list[LearnCitation]


class LearnAnswer(BaseModel):
    answer_id: uuid.UUID
    answer: str = Field(
        description="What Pisa says, each tier spoken as such, each sentence with its citation"
    )
    lang: Literal["it", "en"]
    rationale: bool = Field(description="A question about a design choice")
    abstained: bool = Field(description="No source explains it (tier 3 only)")
    lexical_only: bool
    latency_ms: int
    sentences: list[LearnSentence]
    passages: list[int]
