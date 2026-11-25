"""The v0.1 note of their standing instructions, an archive since v0.1.2 (deprecated: `/v1/memoria` holds
their standing instructions now, one a row; migration 0020 copied the note's lines there, and no turn reads
the note). They read it with its versions, replace it and restore an earlier one, as before: versions of the
archive only. The phone's writes are their own: no outside text reaches these endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter

from pisa.api import note_schemas as s
from pisa.api.deps import DeviceDep, running
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1/profile/note", tags=["profile"], responses=ERROR_RESPONSES)


NoteDep = Annotated[Any, running("note", "not_found", "Pisa's note is not running.", "Try again in a minute")]


@router.get("", response_model=s.Note, deprecated=True)
async def get_note(_: DeviceDep, service: NoteDep) -> dict[str, Any]:
    """Deprecated (see /v1/memoria): the archived note, its cap, the lines kept only as wishes, and its
    versions newest first."""
    return await service.view()


@router.put("", response_model=s.Note, deprecated=True)
async def put_note(body: s.NotePut, device: DeviceDep, service: NoteDep) -> dict[str, Any]:
    """Deprecated (see /v1/memoria): a new version of the archived note, read by no turn (the same text again
    writes none)."""
    return await service.put(body.text, device.id)


@router.post("/restore", response_model=s.Note, deprecated=True)
async def restore_note(body: s.NoteRestore, device: DeviceDep, service: NoteDep) -> dict[str, Any]:
    """Deprecated (see /v1/memoria): an earlier version of the archived note written again as the newest."""
    return await service.restore(body.version, device.id)
