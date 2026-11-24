"""Briefings: the latest one, or a fresh one composed now and returned (never delivered)."""

from typing import Any

from fastapi import APIRouter

from pisa.api import schemas
from pisa.api.deps import DeviceDep, LangDep, RuntimeDep, require_ai_consent
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["briefings"], responses=ERROR_RESPONSES)


@router.get("/briefings/latest", response_model=schemas.Briefing)
async def latest_briefing(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The latest briefing. Today's is the riepilogo as a view of the current plan: `text` is the morning's
    summary line and what is left today, derived on every read (no model call), so a task closed or added
    shows at once; `morning` is the text as composed; `view` its parts. The app reads it again on
    `pisa.app.changed` (contract 2.2)."""
    return await runtime.briefings.latest_view()


@router.post("/briefings", status_code=201, response_model=schemas.Briefing)
async def ask_briefing(device: DeviceDep, runtime: RuntimeDep, lang: LangDep) -> dict[str, Any]:
    """Composed now and stored with trigger `ask`; returned, not delivered. Never counts against the repeat
    rule."""
    # a model writes it: 409 `ai_consent_required` until the user's AI consent covers it (FL-1)
    await require_ai_consent(runtime, device, lang)
    return (await runtime.briefings.ask()).json()
