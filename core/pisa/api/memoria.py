"""Settings → Memoria: their standing instructions (every turn reads them as their preferences) and their
notes. Device-authenticated like the profile's and the v0.1 note's edits (no signature: their own text, which
changes no approval, no tool and no rail). A note's write first checks the AI consent when the `memory.embed`
slot has a model (the note goes to it), as Pisa Learn's writes do. No conversation tool reaches these
routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Response

from pisa.api import memoria_schemas as s
from pisa.api.deps import DeviceDep, LangDep, RuntimeDep, require_ai_consent, running
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1/memoria", tags=["memoria"], responses=ERROR_RESPONSES)

MemoriaDep = Annotated[
    Any, running("memoria", "not_found", "Memoria is not running.", "Try again in a minute")
]


@router.get("", response_model=s.Memoria)
async def get_memoria(_: DeviceDep, service: MemoriaDep) -> dict[str, Any]:
    """Their standing instructions (oldest first), their notes (newest first) and the caps."""
    return await service.view()


@router.post("/instructions", response_model=s.MemoriaInstruction, status_code=201)
async def add_instruction(
    body: s.MemoriaInstructionIn, device: DeviceDep, service: MemoriaDep
) -> dict[str, Any]:
    """A standing instruction of their own. Over the caps (20, 280 characters each, 2,000 in all): 400
    `validation_failed`, with the cap in its message."""
    return await service.add(body.text, device.id)


@router.patch("/instructions/{instruction_id}", response_model=s.MemoriaInstruction)
async def edit_instruction(
    instruction_id: int, body: s.MemoriaInstructionIn, _: DeviceDep, service: MemoriaDep
) -> dict[str, Any]:
    """Their new wording of one (the caps hold)."""
    return await service.edit(instruction_id, body.text)


@router.delete("/instructions/{instruction_id}", status_code=204)
async def delete_instruction(instruction_id: int, _: DeviceDep, service: MemoriaDep) -> Response:
    """Gone from the next turn on."""
    await service.remove(instruction_id)
    return Response(status_code=204)


@router.post("/notes", response_model=s.MemoriaNote, status_code=201)
async def add_note(
    body: s.MemoriaNoteIn, device: DeviceDep, runtime: RuntimeDep, lang: LangDep, service: MemoriaDep
) -> dict[str, Any]:
    """A note of theirs. 409 `ai_consent_required` when it would be embedded without their AI consent."""
    if await service.embeds():  # their words decide health consent too (E1 MEM-1)
        await require_ai_consent(runtime, device, lang, " ".join(filter(None, (body.title, body.text))))
    return await service.save(body.text, body.title, source="app", device=device.id)


@router.patch("/notes/{note_id}", response_model=s.MemoriaNote)
async def edit_note(
    note_id: int, body: s.MemoriaNotePatch, device: DeviceDep, runtime: RuntimeDep, lang: LangDep,
    service: MemoriaDep,
) -> dict[str, Any]:  # fmt: skip
    """Its new text or title (a `title` sent as null clears it)."""
    if await service.embeds():  # their new words decide health consent too (E1 MEM-1)
        words = " ".join(filter(None, (body.title, body.text))) or None
        await require_ai_consent(runtime, device, lang, words)
    clear = "title" in body.model_fields_set and body.title is None
    return await service.edit_note(note_id, body.text, body.title, clear)


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(note_id: int, _: DeviceDep, service: MemoriaDep) -> Response:
    """Gone, and find_note no longer finds it."""
    await service.remove_note(note_id)
    return Response(status_code=204)
