"""Approvals: the user's yes or no to Pisa's actions outside Pisa."""

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, from_notification
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import RESUME_REFUSAL_STATUS, PisaError
from pisa.logging import get_logger
from pisa.services import approval_runs
from pisa.services.agui import JSON_WAIT_S, new_run_id
from pisa.services.background import Background

router = APIRouter(prefix="/v1", tags=["approvals"], responses=ERROR_RESPONSES)
_log = get_logger("pisa.approvals.api")
_background = Background()


def _finished(task: "asyncio.Future[Any]") -> None:
    if not task.cancelled() and task.exception() is not None:
        _log.warning("approval_resume_late_failed", cause=task.exception().__class__.__name__)


@router.get("/approvals", response_model=schemas.ApprovalsResponse)
async def list_approvals(
    _: DeviceDep,
    runtime: RuntimeDep,
    status: Annotated[Literal["pending", "all"], Query(description="all: the last 30 days")] = "pending",
) -> dict[str, Any]:
    return {"approvals": await runtime.approvals.list(status)}


@router.get("/approvals/{approval_id}", response_model=schemas.ApprovalDetail)
async def get_approval(approval_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """One approval, with `preview` (an email's whole text) for the app to show before the yes, and (contract
    2) `payload` and `recipients` exactly as hashed, so the phone recomputes the interrupt's digest from what
    it shows and offers Approve only when it matches."""
    return await runtime.approvals.detail(approval_id)


@router.get("/drafts/{draft_id}", response_model=schemas.DraftItem)
async def get_draft(draft_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """One of Pisa's drafts by id, for a `draft:<id>` object Pisa showed: the recipients and the whole
    text. 404 when unknown (kept 90 days)."""
    return await runtime.actions.draft_view(draft_id)


@router.post(
    "/approvals/{approval_id}/resume",
    response_model=schemas.CollectedRun,
    responses={
        202: {"model": schemas.RunAccepted, "description": "Still running after 8 s"},
        410: {"model": schemas.ErrorResponse},
    },
)
async def resume(
    approval_id: str,
    body: approval_runs.ApprovalResumeIn,
    device: DeviceDep,
    runtime: RuntimeDep,
    request: Request,
) -> dict[str, Any]:
    """Contract 2: decide an approval outside the socket (a notification's action, the watch's reject), with
    the same `ApprovalResume` payload the socket's resume carries (CONTRACT-2 §6.1). When nothing runs, the
    error envelope: 404 not_found, 409 approval_decided or approval_changed, 410 approval_expired, 401
    biometric_required. When the decision is taken, 200 with the resuming run collected (`events`:
    RUN_STARTED, `pisa.action_done` when it ran, RUN_FINISHED; RUN_ERROR `action_failed` when it ran and
    failed). 202 with `runId` when the action still runs after 8 s: its outcome is in the Approvals list.
    `X-Pisa-Origin: notification` names a notification action; `watch` the watch (it only rejects)."""
    by = "notification" if from_notification(request) else None
    run_id = new_run_id()
    deciding = asyncio.ensure_future(
        approval_runs.decide(runtime, approval_id, body.model_dump(exclude_none=True), device.id, by)
    )
    done, _ = await asyncio.wait({deciding}, timeout=JSON_WAIT_S)
    if not done:  # it goes on to its end (kept alive, its failure logged)
        _background.keep(deciding).add_done_callback(_finished)
        return JSONResponse({"runId": run_id}, status_code=202)  # type: ignore[return-value]
    try:
        approval = deciding.result()
    except PisaError as err:
        status = RESUME_REFUSAL_STATUS.get(err.code)
        if status is None:
            raise
        raise PisaError(err.code, err.message, err.action, status=status) from None
    thread = approval_runs.thread_of(str(approval["id"]))
    events = approval_runs.collected(
        str(device.id), thread, run_id, lambda w: approval_runs.write_outcome(w, approval)
    )
    return {"events": events}


@router.post("/approvals/{approval_id}/undo", response_model=schemas.Approval)
async def undo(approval_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Calendar kinds, before `undo_until`: the event is as it was."""
    return await runtime.approvals.undo(approval_id)
