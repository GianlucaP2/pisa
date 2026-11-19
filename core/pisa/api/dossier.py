"""The morning's dossier on the phone, and their thumbs.

- `GET /v1/dossier` today's dossier: what Pisa said (at most three, `spoken`), then the rest; each item with
  the records read to make it (`sources`) and their thumb.
- `POST /v1/dossier/items/{id}/thumb` `{thumb: "up" | "down" | null}`. Device token, not signed: nothing
  outward, nothing deleted.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from pisa.api.deps import DeviceDep, running
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import dossier as dossier_rules
from pisa.services.dossier import DossierService

router = APIRouter(prefix="/v1/dossier", tags=["dossier"], responses=ERROR_RESPONSES)
Kind = dossier_rules.Kind
Thumb = Literal["up", "down"]


class SourceOut(BaseModel):
    table: str = Field(
        description="the record's table: obligations, intentions, email_drafts, calendar_events…"
    )
    id: str


class ItemOut(BaseModel):
    id: str
    kind: Kind
    text: str = Field(description="Pisa's own words for it, in their language (never a stranger's)")
    spoken: bool = Field(description="Pisa said it this morning (at most three a morning)")
    outside: bool = Field(description="its words rest on outside text: shown as data, never said")
    sources: list[SourceOut] = Field(description="the records read to make it")
    thumb: Thumb | None


class DossierOut(BaseModel):
    day: str
    said_at: str | None = Field(description="when Pisa read it aloud this morning; null: not yet")
    closing: str = Field(description='"È tutto": the spoken part ends with it')
    items: list[ItemOut] = Field(description="the spoken items first, then the rest, most pressing first")


class ThumbIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thumb: Thumb | None = Field(description="up, down, or null to take it back")


class ThumbOut(BaseModel):
    id: str
    thumb: Thumb | None


DossierDep = Annotated[
    DossierService, running("dossier", "internal", "The morning's dossier isn't running.", "Restart Pisa")
]


@router.get("", response_model=DossierOut)
async def dossier(_: DeviceDep, service: DossierDep) -> dict[str, Any]:
    """Today's dossier: what Pisa said, then the rest."""
    return await service.today()


@router.post("/items/{item_id}/thumb", response_model=ThumbOut)
async def thumb(item_id: uuid.UUID, body: ThumbIn, _: DeviceDep, service: DossierDep) -> dict[str, Any]:
    """Their thumb on one item."""
    return await service.thumb(item_id, body.thumb)
