"""Briefings: the latest one, or a fresh one composed now and returned (never delivered)."""

from typing import Any

from fastapi import APIRouter

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["briefings"], responses=ERROR_RESPONSES)


@router.get("/briefings/latest", response_model=schemas.Briefing)
async def latest_briefing(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return (await runtime.briefings.latest()).json()


@router.post("/briefings", status_code=201, response_model=schemas.Briefing)
async def ask_briefing(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Composed now and stored with trigger `ask`; returned, not delivered. Never counts against the repeat
    rule."""
    return (await runtime.briefings.ask()).json()
