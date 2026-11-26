"""(rituals) and (place notes): Settings → Rituals, the place notes the phone monitors, and the
rituals' sheets (opened from a ritual's push) with their buttons."""

from typing import Any

from fastapi import APIRouter, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError

router = APIRouter(prefix="/v1", tags=["rituals"], responses=ERROR_RESPONSES)


@router.get("/rituals", response_model=schemas.Rituals)
async def get_rituals(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Pisa's rituals: the Ghost Run (22:00), the morning's one thing, "Chiudiamo?" (21:30), Sunday's poster,
    something different (2 a week)."""
    return await runtime.rituals.get()


@router.put("/rituals", response_model=schemas.Rituals)
async def put_rituals(body: schemas.RitualsPatch, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Change any part; what is left out stays. 400 `validation_failed` names a wrong field."""
    return await runtime.rituals.put(body.model_dump(exclude_none=True))


@router.get("/places/notes", response_model=schemas.PlaceNotes)
async def list_place_notes(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The place notes (newest first), then the open tasks with a place (`task:<uuid>`). The phone registers
    the 20 nearest as region monitors."""
    return {"notes": await runtime.place_notes.listed()}


@router.post("/places/notes", response_model=schemas.PlaceNote, status_code=201)
async def create_place_note(
    body: schemas.PlaceNoteCreate, device: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """A note pinned to a place (the user's own words, from the app)."""
    return await runtime.place_notes.create(
        body.text, body.lat, body.lon, radius_m=body.radius_m, place_name=body.place_name, device_id=device.id
    )


@router.delete("/places/notes/{note_id}", status_code=204, response_class=Response)
async def delete_place_note(note_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Delete a note (404 for a task's entry: it goes with the task)."""
    await runtime.place_notes.delete(note_id)
    return Response(status_code=204)


@router.post("/places/enter", response_model=schemas.PlaceEntered)
async def enter_place(body: schemas.PlaceEnter, device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The phone entered these regions (as the WebSocket's `place_enter`): each note fires at most once in
    6 hours, as one notice to this device."""
    return await runtime.place_notes.enter(device.id, body.note_ids)


@router.get("/sheets/{sheet_id}", response_model=schemas.RitualSheet)
async def get_sheet(sheet_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """A ritual's sheet (the push's `sheet_id`), as the `present` object the app already renders; a people
    offer's sheet too (`ritual: people`: Pisa's line and the buttons with the draft)."""
    try:
        return await runtime.rituals.sheet(sheet_id)
    except PisaError as err:
        if err.code != "not_found":
            raise
        if (offer := await runtime.people.offers.sheet(sheet_id)) is not None:
            return offer
        # a REST turn's sheet (the complete answer of a turn asked from the watch)
        if runtime.turns is not None and (kept := await runtime.turns.sheet(sheet_id)) is not None:
            return kept
        raise


@router.post("/sheets/{sheet_id}/actions/{action_id}", response_model=schemas.SheetActionDone)
async def sheet_action(sheet_id: str, action_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """A button on a ritual's sheet, the user's own tap: the Ghost Run's yes or no, an Undo.
    404 `not_found` for an unknown button; 409 `approval_decided` for a question already answered or over."""
    return {"said": await runtime.rituals.act(sheet_id, action_id, "tap")}
