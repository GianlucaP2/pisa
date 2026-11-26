"""The iPhone's speech recogniser: its contextual strings, the names Pisa knows, refreshed daily."""

from typing import Any

from fastapi import APIRouter

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.brain.understand import zipf

router = APIRouter(prefix="/v1", tags=["speech"], responses=ERROR_RESPONSES)


@router.get("/speech/hints", response_model=schemas.SpeechHints)
async def speech_hints(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The 100 best names for the recogniser's contextual strings: today's plan and home first, then this
    week's events, venues and people, places visited in 30 days, contacts, names Pisa fixed lately; never one
    common word. Fetch it daily and when the app starts."""
    return await runtime.lexicon.hints(zipf)
