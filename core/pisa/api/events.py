"""Calendar events as the plan sees them: each with its kind (fixed, window, soft), and the user's
override. Pisa's own data: Google Calendar is never edited."""

from typing import Annotated, Any

from fastapi import APIRouter, Query

from pisa.api import schemas
from pisa.api.deps import DateQuery, DeviceDep, RuntimeDep, parse_day
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["events"], responses=ERROR_RESPONSES)


@router.get("/events", response_model=schemas.EventsResponse)
async def list_events(
    _: DeviceDep,
    runtime: RuntimeDep,
    date_: DateQuery = None,
    days: Annotated[int, Query(ge=1, le=14)] = 1,
) -> dict[str, Any]:
    """The events of `days` days from `date`, each with its kind, who said so and a window's minimum stay."""
    return await runtime.planner.events(parse_day(date_), days)


@router.patch("/events/{event_id}", response_model=schemas.Event)
async def set_event_kind(
    event_id: str, body: schemas.EventPatch, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """The user's kind for the event (null: Pisa infers it again) and a window's minimum stay; the plan is
    made again with it."""
    return await runtime.planner.set_kind(event_id, body.kind, body.min_stay_min)


@router.post("/events/{event_id}/undo-attended", response_model=schemas.Event)
async def undo_attended(event_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Pisa marked the event attended because the user was there (or said so); within 24 h the user can
    undo it. 404 when Pisa didn't mark it, 409 `undo_expired` after 24 h."""
    await runtime.done.undo_event(event_id)
    return await runtime.planner.event(event_id)
