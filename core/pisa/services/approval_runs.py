"""Contract 2's approval runs (CONTRACT-2 §6.1), shared by `/v2/live`, `POST /v2/runs` and
`POST /v1/approvals/{id}/resume`.

- Each approval has its own thread, `pisa-approval:<approvalId>`. Its first run raises one interrupt
  (`int-<approvalId>`) and ends `RUN_FINISHED` with outcome `interrupt`; the conversation never waits for it.
- The run that resumes it carries one `ResumeEntry`: `resolved` with an `ApprovalResume` payload, or
  `cancelled` (the app let the interrupt go: nothing is decided, the approval stays in the Approvals list
  until it expires). The decision is checked and run by `ApprovalsService.resume`; the run reports
  `pisa.action_done`, or `RUN_ERROR` with the refusal's code (`approval_changed`, `approval_expired`,
  `approval_decided`, `biometric_required`), or `action_failed` with the cause when the action ran and failed.
  A reject ends the run with success and nothing else.
- Decided anywhere else (a spoken yes, the watch, a notification, another device, expiry), the server starts a
  short run of its own on the thread with `pisa.approval.state`: the app drops the interrupt it holds.
"""

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pisa.domain import approval_sign
from pisa.domain.errors import PisaError
from pisa.ports.push import OutgoingApproval
from pisa.services.agui import LiveSession, RunWriter
from pisa.services.runtime import Runtime


class ApprovalResumeIn(BaseModel):
    """`$defs/ApprovalResume`: a decision on a contract 2 approval interrupt (CONTRACT-2 §6.1)."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    auth: Literal["tap", "voice", "biometric"]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = Field(
        description="`pisa-approval-digest/2` of what the phone showed (the interrupt's digest)"
    )
    nonce: Annotated[str, Field(min_length=1, max_length=200)] | None = Field(
        default=None, description="biometric: the challenge's nonce (POST /v1/phone/challenge)"
    )
    signature: Annotated[str, Field(max_length=200)] | None = Field(
        default=None, description="biometric: the signature of the `pisa-approval/2` text, DER, base64"
    )
    keyId: Annotated[str, Field(min_length=1, max_length=80)] | None = Field(
        default=None, description="biometric: the signing key"
    )


THREAD_PREFIX = "pisa-approval:"
# RUN_ERROR.code on an approval's thread: the closed `ApprovalErrorCode` set (CONTRACT-2 §6.1)
ERROR_CODES = frozenset(
    {"approval_changed", "approval_expired", "approval_decided", "biometric_required", "action_failed"}
)


def thread_of(approval_id: str) -> str:
    return f"{THREAD_PREFIX}{approval_id.lower()}"


def approval_of_thread(thread: str) -> str | None:
    return thread.removeprefix(THREAD_PREFIX) if thread.startswith(THREAD_PREFIX) else None


def interrupt_of(approval: OutgoingApproval) -> dict[str, Any] | None:
    """The approval's AG-UI interrupt (`$defs/ApprovalInterrupt`); None without its digest and server id."""
    if approval.digest is None or approval.server is None:
        return None
    approval_id = approval.id.lower()
    return approval_sign.interrupt_for(
        interrupt_id=approval_sign.interrupt_id_for(approval_id),
        approval_id=approval_id,
        kind=approval.kind,
        requires_biometric=approval.requires_biometric,
        digest_hex=approval.digest,
        recipients_count=approval.recipients_count,
        undo_minutes=approval.undo_minutes,
        expires_at=approval.expires_at,
        server=approval.server,
        lang=approval.lang,
    )


def state_value(state: dict[str, Any]) -> dict[str, Any]:
    """`$defs/s_pisa_approval_state` from `ApprovalsService`'s (approval_id, state, by)."""
    approval_id = str(state["approval_id"]).lower()
    value = {
        "approvalId": approval_id,
        "interruptId": approval_sign.interrupt_id_for(approval_id),
        "state": state["state"],
    }
    if state.get("by"):
        value["by"] = state["by"]
    return value


def resume_entry(approval_id: str, resume: Any) -> dict[str, Any]:
    """The one `ResumeEntry` that answers this approval's interrupt; 400 validation_failed otherwise."""
    expected = approval_sign.interrupt_id_for(approval_id.lower())
    entries = resume if isinstance(resume, list) else []
    if len(entries) != 1 or not isinstance(entries[0], dict) or entries[0].get("interruptId") != expected:
        raise PisaError(
            "validation_failed",
            f"A run on this approval's thread resumes exactly its interrupt, {expected}.",
            "Resume the interrupt the approval's run raised",
        )
    entry = entries[0]
    if entry.get("status") not in ("resolved", "cancelled"):
        raise PisaError("validation_failed", "A resume's status is resolved or cancelled.", "Fix the request")
    return entry


def payload_of(raw: Any) -> dict[str, Any]:
    """An `ApprovalResume`, validated; 400 validation_failed otherwise."""
    try:
        return ApprovalResumeIn.model_validate(raw).model_dump(exclude_none=True)
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "payload"
        raise PisaError(
            "validation_failed",
            f"The resume payload's '{where}' is invalid: {first['msg']}.",
            "Fix the request",
        ) from None


async def decide(
    runtime: Runtime, approval_id: str, payload: dict[str, Any], device_id: uuid.UUID, by: str | None = None
) -> dict[str, Any]:
    """The decision, checked and run (`ApprovalsService.resume`); raises the refusal when nothing ran."""
    return await runtime.approvals.resume(
        approval_id,
        payload,
        device_id=device_id,
        interrupt_id=approval_sign.interrupt_id_for(approval_id.lower()),
        verify=runtime.autopilot.keys.verify_approval,
        by=by,
    )


def action_done_words(summary: str | None, result: str | None) -> str:
    """An approved action's outcome as Pisa's words, for an app that did not pick `pisa.approval`."""
    return ": ".join(w for w in ((summary or "").strip()[:500], (result or "").strip()[:500]) if w) or "Done."


def write_outcome(writer: RunWriter, approval: dict[str, Any], lang: str | None = None) -> None:
    """The decided approval, as the end of its resuming run. An app that did not pick `pisa.approval` gets
    its outcome as a text message in `lang` instead of `pisa.action_done`."""
    if approval["status"] == "done":
        value: dict[str, Any] = {
            "approvalId": str(approval["id"]).lower(),
            "kind": approval["kind"],
            "summary": (approval["summary"] or "-")[:500],
            "result": (approval["result"] or "-")[:500],
        }
        if approval.get("undo_until"):
            value["undoUntil"] = approval["undo_until"]
        if not writer.custom("pisa.action_done", value) and lang is not None:
            writer.text([action_done_words(approval["summary"], approval["result"])], lang=lang)
    elif approval["status"] == "failed":
        writer.error((approval["result"] or "It wasn't done.")[:500], "action_failed")
        return
    writer.finish()


def collected(
    device_id: str, thread: str, run_id: str, write: Callable[[RunWriter], object]
) -> list[dict[str, Any]]:
    """One run written whole, off any socket: its AG-UI events (the JSON mode of the HTTP binding)."""
    scratch = LiveSession(device_id)
    writer = scratch.start_run(thread, run_id)
    write(writer)
    if not writer.closed:
        writer.finish()
    return [frame["event"] for frame in scratch.kept() if "event" in frame]
