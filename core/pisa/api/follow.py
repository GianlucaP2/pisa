"""What really happened: check-ins, their answers, and the day so far."""

from typing import Any, Literal

from fastapi import APIRouter

from pisa.api import schemas
from pisa.api.deps import DateQuery, DeviceDep, RuntimeDep, parse_day
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["plan"], responses=ERROR_RESPONSES)


@router.get("/check-ins", response_model=schemas.CheckInsResponse, response_model_exclude_unset=True)
async def check_ins(
    _: DeviceDep, runtime: RuntimeDep, status: Literal["open", "all"] = "open", date_: DateQuery = None
) -> dict[str, Any]:
    return {"check_ins": await runtime.follow.check_ins(status, parse_day(date_))}


@router.post(
    "/check-ins/{check_in_id}/answer", response_model=schemas.CheckInAnswered, response_model_by_alias=True,
    response_model_exclude_unset=True,
)  # fmt: skip
async def answer(
    check_in_id: str, body: schemas.CheckInAnswer, device: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    # one about the plan, or one of Pisa's offers riding a check-in ("Conosciamoci": the first talk, here)
    return await runtime.follow.answer(check_in_id, body.choice, "app", device.id)


@router.get("/day", response_model=schemas.Day, response_model_exclude_unset=True)
async def day(_: DeviceDep, runtime: RuntimeDep, date_: DateQuery = None) -> dict[str, Any]:
    return await runtime.follow.day(parse_day(date_))
