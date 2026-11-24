"""The people the user loves, in the app (list, their corrections, a note's delete), and K23b's
care circle (opt-in; stored only: nothing is collected from anyone)."""

from typing import Any

from fastapi import APIRouter, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["people"], responses=ERROR_RESPONSES)


@router.get("/people", response_model=schemas.People)
async def list_people(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The people Pisa knows from their mail, their calendar and their words, by name; never invented. Gift
    ideas keep their source: `email` is the person's own words, kept as data (never acted on)."""
    return await runtime.people.listed()


@router.patch("/people/{person_id}", response_model=schemas.Person)
async def patch_person(
    person_id: str, body: schemas.PersonPatch, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """Their corrections: they win over every sync. Null clears relation, tz, city or birthday; `hidden` takes
    the person out of the list and out of everything Pisa says."""
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    return await runtime.people.patch(person_id, changes)


@router.delete("/people/{person_id}/notes/{note_id}", status_code=204, response_class=Response)
async def delete_person_note(person_id: str, note_id: int, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Delete one note or gift idea (its `id`)."""
    await runtime.people.delete_note(person_id, note_id)
    return Response(status_code=204)


@router.get("/care", response_model=schemas.Care)
async def get_care(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The care circle as they entered it (opt-in). Nothing is collected or shared in this slice."""
    return await runtime.people.care()


@router.put("/care", response_model=schemas.Care)
async def put_care(body: schemas.CarePut, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Replace the care circle (at most 20). `since` stays for a member whose `shares` didn't change."""
    return await runtime.people.set_care([(str(m.person_id), m.shares) for m in body.members])


@router.put("/people/orbits", response_model=schemas.Orbits)
async def put_orbits(body: schemas.Orbits, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Their switch for the Sunday line about someone they haven't heard from (off by their words too)."""
    return await runtime.people.set_orbits(body.orbits)


@router.delete("/people/{person_id}", status_code=204, response_class=Response)
async def delete_person(person_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Forget someone: everything Pisa knew about them. Only their address stays, hidden, so the sync never
    brings them back."""
    await runtime.people.delete(person_id)
    return Response(status_code=204)


@router.post("/people/{person_id}/merge", response_model=schemas.Person)
async def merge_person(
    person_id: str, body: schemas.PersonMerge, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """Two rows that are one person: this one's addresses, notes and dates go into `into` (whose own
    birthday and care entry stay); this one is gone."""
    return await runtime.people.merge(person_id, str(body.into))


@router.post("/people/{person_id}/notes/{note_id}/keep", response_model=schemas.Person)
async def keep_person_note(person_id: str, note_id: int, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Keep an email's gift idea as their own: it becomes `from: owner` and outlives the email (90 days)."""
    return await runtime.people.keep_note(person_id, note_id)
