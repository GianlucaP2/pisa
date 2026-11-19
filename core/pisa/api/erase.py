"""Their deletion by family and age. GET the preview (how many rows, per table, and the rows that go
with them), then POST the delete with a signature (`device` challenge, op `erase`, target
`<family>:<YYYY-MM-DD>`) by a Face ID key or the device's own key (`device-<its id>`), always, even before
Pisa holds a Face ID key. The delete runs in the background:
POST answers 202 with the job at once, GET /v1/data/erase/{erase_id} its state and counts (no second
signature). The POST answers the job `queued` at once and counts inside it; the same family and day
while one is queued or running answers that job (the same `erase_id`); GET /v1/data/erase lists their latest
jobs, so a phone that lost the 202 finds its own. No conversation tool reaches these routes."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import erase as rules

router = APIRouter(prefix="/v1", tags=["erase"], responses=ERROR_RESPONSES)


@router.get("/data/erase/preview", response_model=schemas.ErasePreview)
async def preview(
    family: Annotated[schemas.EraseFamily, Query(description="What to delete")],
    before: Annotated[date, Query(description="Rows older than this day (their zone) go")],
    _: DeviceDep,
    runtime: RuntimeDep,
) -> dict[str, Any]:
    """How many rows the delete would remove now, per table (counts only)."""
    return await runtime.erase.preview(family, before)


@router.post("/data/erase", response_model=schemas.EraseJob, status_code=202)
async def erase(
    body: schemas.EraseIn, device: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> dict[str, Any]:
    """Delete the family's rows older than `before` (in the background, batched). Needs a signature (a
    Face ID key's, or this device's own key `device-<its id>`): 403 `biometric_required` otherwise; a day
    after today: 400 `validation_failed`. The same family and day while a job is queued or running: that
    job, not a second."""
    await runtime.autopilot.keys.guard(
        "erase",
        rules.target(body.family, body.before),
        signed.nonce,
        signed.signature,
        signed.key_id,
        device_id=signed.device_id,
        always=True,
    )
    return await runtime.erase.start(body.family, body.before, device_id=device.id)


@router.get("/data/erase", response_model=schemas.EraseJobs)
async def jobs(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Their latest deletions, newest first: states and counts only."""
    return {"jobs": await runtime.erase.jobs()}


@router.get("/data/erase/{erase_id}", response_model=schemas.EraseJob)
async def job(erase_id: uuid.UUID, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """A deletion's state (`queued`, `running`, `done`, `failed`) and what went so far, per table."""
    return await runtime.erase.job(erase_id)
