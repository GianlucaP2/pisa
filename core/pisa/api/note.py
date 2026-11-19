"""Their note of standing instructions, for "Su di te". They read it with its versions,
replaces it (a version like any other) and restores an earlier one. The phone's writes are their own: no
outside text reaches these endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter

from pisa.api import note_schemas as s
from pisa.api.deps import DeviceDep, running
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1/profile/note", tags=["profile"], responses=ERROR_RESPONSES)


NoteDep = Annotated[Any, running("note", "not_found", "Pisa's note is not running.", "Try again in a minute")]


@router.get("", response_model=s.Note)
async def get_note(_: DeviceDep, service: NoteDep) -> dict[str, Any]:
    """The note now, its cap, the lines kept only as wishes, and its versions newest first."""
    return await service.view()


@router.put("", response_model=s.Note)
async def put_note(body: s.NotePut, device: DeviceDep, service: NoteDep) -> dict[str, Any]:
    """Their own note, replacing Pisa's: a new version (the same text again writes none)."""
    return await service.put(body.text, device.id)


@router.post("/restore", response_model=s.Note)
async def restore_note(body: s.NoteRestore, device: DeviceDep, service: NoteDep) -> dict[str, Any]:
    """An earlier version written again as the newest; the versions in between stay."""
    return await service.restore(body.version, device.id)
