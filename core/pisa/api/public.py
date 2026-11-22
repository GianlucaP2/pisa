"""The public data Pisa reads, for Settings ("Dati pubblici"): each source, its licence and the
attribution it asks for, its status (ok, failing, unused, a reviewed table) and when Pisa last read it.
Read-only; the reads themselves happen in a talk, in a view or a skill, and in the briefing."""

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["public"], responses=ERROR_RESPONSES)


class PublicSource(BaseModel):
    key: str
    name: str
    licence: str = Field(description="CC BY-SA 4.0, CC BY 4.0, IODL 2.0, MTA terms, US public domain, or a "
                         "reviewed table Pisa encoded from the source's page")  # fmt: skip
    attribution: str = Field(description="The credit line the licence asks for")
    url: str
    official: bool
    status: Literal["ok", "failing", "unused", "reviewed"]
    fetched_at: str | None = Field(description="When Pisa last read it (ISO 8601), null if never")


class PublicSourcesResponse(BaseModel):
    sources: list[PublicSource]


@router.get("/public/sources", response_model=PublicSourcesResponse)
async def public_sources(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return {"sources": await runtime.public.sources()}
