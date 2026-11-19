"""The day plan: GET the latest for a day (today's is made when there is none), POST re-plans.
Options and adjustments: offer variants of the day, apply one, list and undo
adjustments."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from pisa.api import schemas
from pisa.api.deps import DateQuery, DeviceDep, RuntimeDep, parse_day
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError
from pisa.services import wrist_flags

router = APIRouter(prefix="/v1", tags=["plan"], responses=ERROR_RESPONSES)


@router.get(
    "/plan", response_model=schemas.Plan, response_model_by_alias=True, response_model_exclude_unset=True
)
async def get_plan(
    device: DeviceDep,
    runtime: RuntimeDep,
    date_: DateQuery = None,
) -> dict[str, Any]:
    plan = await runtime.planner.get(parse_day(date_), requested_by=device.id)
    return await _overlay(runtime, plan)  # type: ignore[return-value]


@router.post(
    "/plan",
    status_code=201,
    response_model=schemas.Plan,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
)
async def replan(device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    plan = await runtime.planner.plan(reason="asked", requested_by=device.id)
    return await _overlay(runtime, plan)  # type: ignore[return-value]


async def _overlay(runtime: Any, plan: Any) -> Any:
    """What really happened, and what the wrist may name of each item."""
    out = await runtime.follow.overlay(plan)
    async with runtime.pool.connection() as conn:
        return await wrist_flags.mark_plan(conn, out)


@router.post(
    "/plan/options", response_model=schemas.PlanOptions, response_model_by_alias=True,
    response_model_exclude_unset=True,
)  # fmt: skip
async def plan_options(body: schemas.PlanOptionsRequest, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    target = body.target.model_dump(exclude_none=True) if body.target else None
    return await runtime.plan_options.options(target, body.goal)


@router.post(
    "/plan/insert", response_model=schemas.Plan | schemas.PlanOptions, response_model_by_alias=True,
    response_model_exclude_unset=True,
)  # fmt: skip
async def plan_insert(body: schemas.PlanInsert, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Place the stop where the user said; the new plan when it fits, else options (the shape:
    apply one with POST /v1/plan/options/{options_id}/apply)."""
    result = await runtime.plan_options.insert(
        task_id=body.task_id, place_id=body.place_id, when=body.when, relative_to=body.relative_to,
        duration_min=body.duration_min, source="app",
    )  # fmt: skip
    if "options_id" in result:
        return result
    return await _overlay(runtime, result)  # type: ignore[return-value]


@router.get(
    "/plan/options/{options_id}", response_model=schemas.PlanOptions, response_model_by_alias=True,
    response_model_exclude_unset=True,
)  # fmt: skip
async def get_options(options_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Pisa's options by id while they last (15 minutes; gone once one is applied: 404
    options_expired): the phone reads the options a `plan` object names (`options_id`)."""
    return await runtime.plan_options.stored(options_id)


@router.post(
    "/plan/options/{options_id}/apply", response_model=schemas.Plan, response_model_by_alias=True,
    response_model_exclude_unset=True,
)  # fmt: skip
async def apply_option(
    options_id: str, body: schemas.ApplyOption, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    return await _overlay(
        runtime,  # type: ignore[return-value]
        await runtime.plan_options.apply(options_id, body.option_id, "app"),
    )


@router.get(
    "/plan/adjustments", response_model=schemas.AdjustmentsResponse, response_model_exclude_unset=True
)
async def adjustments(
    _: DeviceDep,
    runtime: RuntimeDep,
    date_: Annotated[str | None, Query(alias="date", description="YYYY-MM-DD; default today")] = None,
) -> dict[str, Any]:
    return {"adjustments": await runtime.plan_options.adjustments(parse_day(date_))}


@router.post(
    "/plan/adjustments", status_code=201, response_model=schemas.Plan, response_model_by_alias=True,
    response_model_exclude_unset=True,
)  # fmt: skip
async def add_adjustment(body: schemas.ManualAdjustment, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Move or skip an item of today's plan from the app (source app). No options: no plan_changed."""
    if (body.task_id is None) == (body.event_id is None):
        raise PisaError("validation_failed", "Give exactly one of task_id and event_id.", "Fix the request")
    target = {"task_id": body.task_id} if body.task_id else {"event_id": str(body.event_id)}
    plan = await runtime.plan_options.manual(target, body.action, body.start)
    return await _overlay(runtime, plan)  # type: ignore[return-value]


@router.delete("/plan/adjustments/{adjustment_id}", status_code=204)
async def undo_adjustment(adjustment_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Undo an adjustment, with the other changes of the same option (`option_ref`), and re-plan."""
    await runtime.plan_options.undo(adjustment_id)
    return Response(status_code=204)
