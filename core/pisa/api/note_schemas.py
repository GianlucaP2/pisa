"""REST models: their note of standing instructions, for "Su di te"."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pisa.api.schemas import DateTime
from pisa.domain import note as rules


class NoteVersion(BaseModel):
    version: int = Field(ge=1)
    text: str = Field(description="the whole note as it was at this version")
    change: str = Field(description="Pisa's one line of what changed, said to them (empty for the app's own)")
    source: Literal["turn", "app", "restore", "held"] = Field(
        description="turn: a conversation turn; app: their PUT; restore: an earlier version written again; "
        "held: "
        "their yes to a rewrite Pisa asked about (a turn that read outside text)"
    )
    restored_from: int | None = Field(default=None, description="the version a restore copied")
    at: DateTime


class Note(BaseModel):
    text: str = Field(description='the note now ("" before their first telling)')
    version: int = Field(ge=0, description="0 before their first telling")
    updated_at: DateTime | None = None
    cap: int = Field(description="its most characters")
    wishes: list[str] = Field(
        description="its lines that ask Pisa to act without their yes: kept as their wishes, with no effect "
        "on "
        "approvals"
    )
    versions: list[NoteVersion] = Field(description=f"newest first, at most {rules.VERSIONS_SHOWN}")
    versions_total: int


class NotePut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=rules.CAP, description="the whole new note (empty clears it)")


class NoteRestore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, description="the version to write again as the newest")
