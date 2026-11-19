"""The portrait's wire shapes."""

from typing import Literal

from pydantic import BaseModel, Field


class PortraitLine(BaseModel):
    text: str = Field(description="≤ 160 characters, in their Settings language")
    claim_ids: list[str] = Field(
        description="1-10 ids of the claims behind the line (GET /v1/profile's claims)"
    )


class PortraitTheme(BaseModel):
    id: str = Field(description="city, food, music, evenings, travel, moving, work, style or other")
    title: str
    lines: list[PortraitLine]


class Portrait(BaseModel):
    """Written by Pisa from their active claims only (never candidates, never sensitive ones). Empty `themes`
    (and `text` "") when Pisa knows too little."""

    text: str = Field(description="a short paragraph, ≤ 400 characters; empty when Pisa knows too little")
    themes: list[PortraitTheme]
    built_at: str | None = Field(
        description="when Pisa wrote it (ISO 8601, their zone); null before the first"
    )
    claims_used: int


class PortraitLineAnswer(BaseModel):
    """Their answer on one line: "Non è vero" (wrong) or "Correggi…" (correct, with their words)."""

    claim_ids: list[str] = Field(min_length=1, max_length=10, description="the line's claim_ids, as received")
    answer: Literal["wrong", "correct"]
    words: str | None = Field(
        default=None, max_length=200, description="their own words, for correct (1-200)"
    )
