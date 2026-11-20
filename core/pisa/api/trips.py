"""The user's trips from their confirmation emails, and the residency counter. A trip is
built from email (data) and changed only here, by the user. The counter is a count, never advice."""

from typing import Any, Literal

from fastapi import APIRouter, Query

from pisa.api import schemas
from pisa.api.deps import NOT_A_DAY, DeviceDep, RuntimeDep, parse_day
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["trips"], responses=ERROR_RESPONSES)


@router.get("/trips", response_model=schemas.Trips)
async def list_trips(
    _: DeviceDep, runtime: RuntimeDep, scope: Literal["upcoming", "all"] = "upcoming"
) -> dict[str, Any]:
    """Their trips, soonest first (at most 50): each leg in its own zone, a booking code's last 4 only.
    `upcoming`: not cancelled and not back yet."""
    return {"trips": await runtime.trips.listed(scope)}


@router.patch("/trips/{trip_id}", response_model=schemas.Trip)
async def patch_trip(
    trip_id: str, body: schemas.TripPatch, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """The user renames a trip, cancels it (or plans it again), or changes its checklist (their own items are
    offered again on their next trip abroad)."""
    return await runtime.trips.patch(
        trip_id,
        title=body.title,
        status=body.status,
        checklist=[i.model_dump() for i in body.checklist] if body.checklist is not None else None,
        confirmed=body.confirmed,
        apply_change=body.apply_change,
    )


@router.get("/residency", response_model=schemas.Residency)
async def residency(
    _: DeviceDep, runtime: RuntimeDep, year: int | None = Query(default=None, ge=2000, le=2100)
) -> dict[str, Any]:
    """The year's days by country and each rule's count. "È un conteggio, non una consulenza"."""
    return await runtime.residency.report(year)


@router.post("/residency/days", response_model=schemas.ResidencyDayOut)
async def correct_day(body: schemas.ResidencyDay, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Their correction for a day: it wins over the phone, their trips and their calendar."""
    said = body.countries or ([body.country] if body.country else [])
    return await runtime.residency.correct(parse_day(body.day, NOT_A_DAY), list(said))
