"""Pisa's voice: the voices a provider offers, and a preview of the chosen one."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1/voice", tags=["voice"], responses=ERROR_RESPONSES)


@router.get("/voices", response_model=schemas.VoicesResponse)
async def voices(
    _: DeviceDep,
    runtime: RuntimeDep,
    provider: Annotated[str, Query(description="openai, elevenlabs or device (the phone lists its own: [])")],
) -> dict[str, Any]:
    return {"voices": await runtime.voice.voices(provider)}


@router.post(
    "/preview",
    response_class=Response,
    responses={200: {"content": {"audio/mpeg": {}}, "description": "The chosen voice saying the text (mp3)"}},
)
async def preview(body: schemas.VoicePreview, _: DeviceDep, runtime: RuntimeDep) -> Response:
    return Response(content=await runtime.voice.preview(body.text), media_type="audio/mpeg")
