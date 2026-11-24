"""REST models (server side): Pisa's memory of their days for the Memory screen. The contract:
protocol/README.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pisa.api.schemas import DateTime
from pisa.api.schemas import IsoDay as Day


class MemoryPlace(BaseModel):
    id: str
    name: str


class MemoryPerson(BaseModel):
    id: str
    name: str
    by_owner: bool = Field(description='they set this link themselves ("correggi" or "no, era con …")')


class MemoryEpisode(BaseModel):
    id: str
    kind: Literal["stay", "talk", "event", "trip_leg"]
    day: Day
    started_at: DateTime
    ended_at: DateTime
    home: bool
    place: MemoryPlace | None = None
    people: list[MemoryPerson]
    digest: str | None = Field(default=None, description="≤ 2 lines; null until the night writes it")
    corrected_by_owner: bool


class MemoryDay(BaseModel):
    day: Day
    episodes: list[MemoryEpisode]


class MemoryDays(BaseModel):
    days: list[MemoryDay]


class MemorySource(BaseModel):
    kind: Literal[
        "place", "task", "event", "message_range", "decision", "file", "claim", "thing", "trip", "email"
    ]
    id: str
    role: Literal["with", "at", "about", "made", "said", "near"]
    source: Literal["code", "owner"]
    sender_class: Literal["contact", "own", "other"] | None = Field(
        default=None, description="emails only: who sent it, as a class (never a word of the mail)"
    )
    title: str | None = Field(default=None, description="events only: their own title (never an invite's)")


class MemoryEpisodeDetail(MemoryEpisode):
    sources: list[MemorySource]


class MemoryCorrect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    people: list[str] = Field(max_length=10, description="the ids of their people they were with")


class MemoryForgotten(BaseModel):
    forgotten: Literal[True]


class MemoryNight(BaseModel):
    night: Day | None
    made: int
    digested: int
    episodes: list[MemoryEpisode]
