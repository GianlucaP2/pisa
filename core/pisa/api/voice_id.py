"""Their voice. The consent (before any recording), the enrolment's script (the
server owns it), the routes enrolled and whether the gate is on, re-learning a route and deleting everything.
The enrolment itself runs over the talk's own path (`POST /v1/talk/sessions` with `purpose: "enrol"`)."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import voice_enrol
from pisa.domain.errors import PisaError
from pisa.logging import get_logger

router = APIRouter(prefix="/v1/voice", tags=["voice"], responses=ERROR_RESPONSES)
_log = get_logger("pisa.voice")
Route = Literal["speaker", "receiver", "airpods", "headphones", "car", "glasses"]


@router.post("/consent", response_model=schemas.VoiceConsentGiven, status_code=201)
async def consent(body: schemas.VoiceConsent, device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Their explicit consent (GDPR art. 9) to record their voice and keep it, with the text's version."""
    at = await runtime.voice_id.store.consent(body.version, device.id)
    _log.info("voice_consent", device_id=str(device.id))
    return {"consent_at": at}


@router.get("/enrolment/script", response_model=schemas.VoiceScript)
async def script(
    _: DeviceDep, lang: Annotated[Literal["it", "en"], Query(description="The phone's language")] = "it"
) -> dict[str, Any]:
    return voice_enrol.script(lang)


@router.get("/profile", response_model=schemas.VoiceProfile)
async def profile(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.voice_id.state()


@router.delete("/profile/{route}", response_model=schemas.VoiceDeleted)
async def relearn(
    route: Annotated[Route, Path(description="The route to learn again")],
    device: DeviceDep,
    runtime: RuntimeDep,
) -> dict[str, Any]:
    """Re-learn: the route's prints and recordings go; its next talk asks a mini-enrolment."""
    gone = await runtime.voice_id.store.delete_route(route)
    if not gone:
        raise PisaError("not_found", f"No voice is enrolled on the {route} route.", "Enrol it first")
    _log.info("voice_route_deleted", device_id=str(device.id), route=route, rows=gone)
    return {"deleted": gone}


@router.delete("/profile", response_model=schemas.VoiceDeleted)
async def delete_all(device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Everything of their voice at once: the prints, the recordings and the consent (the gate goes off)."""
    gone = await runtime.voice_id.store.delete_all()
    _log.info("voice_deleted", device_id=str(device.id), rows=gone)
    return {"deleted": gone}
