"""REST models: Memoria, their standing instructions and their notes (Settings → Memoria)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pisa.api.schemas import DateTime
from pisa.domain import memoria as rules


class MemoriaInstruction(BaseModel):
    id: int = Field(ge=1)
    text: str = Field(description="the instruction, one line, their words (or Pisa's after their yes)")
    source: Literal["turn", "held", "app", "note"] = Field(
        description="turn: their words in a conversation; held: Pisa's words after their yes; app: Settings; "
        "note: copied from the v0.1 note"
    )
    created_at: DateTime
    updated_at: DateTime


class MemoriaNote(BaseModel):
    id: int = Field(ge=1)
    title: str | None = None
    text: str
    source: Literal["turn", "app"] = Field(description='turn: a conversation ("annota…"); app: Settings')
    created_at: DateTime
    updated_at: DateTime


class MemoriaCaps(BaseModel):
    instructions: int = Field(description="the most active standing instructions")
    instruction_chars: int = Field(description="the most characters of one instruction")
    total_chars: int = Field(description="the most characters of all the instructions together")
    note_chars: int = Field(description="the most characters of one note")


class Memoria(BaseModel):
    instructions: list[MemoriaInstruction] = Field(
        description="oldest first, the order every turn reads them"
    )
    notes: list[MemoriaNote] = Field(description="newest first")
    caps: MemoriaCaps
    total_chars: int = Field(description="the instructions' characters now")


class MemoriaInstructionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=rules.MAX_CHARS, description="one line (spaces collapsed)")


class MemoriaNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=rules.NOTE_MAX_CHARS)
    title: str | None = Field(default=None, max_length=rules.TITLE_MAX_CHARS)


class MemoriaNotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, min_length=1, max_length=rules.NOTE_MAX_CHARS)
    title: str | None = Field(
        default=None, max_length=rules.TITLE_MAX_CHARS, description="null (sent) clears it; left out keeps it"
    )
