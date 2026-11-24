"""REST models: Pisa's identity (public facts only) and the ladder. The contract:
protocol/README.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pisa.api.schemas import DateTime


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_url: str | None
    signing: bool
    kid: str | None
    created_at: DateTime | None
    published: list[str]
    card_url: str | None
    directory_url: str | None


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rung: Literal["protocol", "business_agent", "signed_browser", "call", "deep_link"]
    state: Literal["live", "watch", "none"]
    via: str
    note: str
    source: str


class LadderCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["restaurants", "travel", "retail", "utilities", "public_it"]
    countries: dict[str, list[Route]] = Field(description="ISO 3166-1 alpha-2 → its routes, best first")


class Ladder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categories: list[LadderCategory]
    carried: list[str]
