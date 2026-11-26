"""Tasks and the agenda."""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, LangDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError
from pisa.services import changes as changes_feed
from pisa.services import email_rules
from pisa.services.intake import email_view, quiet_inbox
from pisa.services.tasks import AGENDA_DEFAULT_DAYS, AGENDA_MAX_DAYS, task_json

router = APIRouter(prefix="/v1", tags=["tasks"], responses=ERROR_RESPONSES)


@router.get("/tasks", response_model=schemas.TasksResponse)
async def list_tasks(
    _: DeviceDep,
    runtime: RuntimeDep,
    lang: LangDep,
    status: Annotated[str, Query(description="open, done, dismissed or all")] = "open",
    priority: Annotated[
        Literal["low"] | None,
        Query(description='"low" is the Low priority list, with the emails that aren\'t tasks'),
    ] = None,
) -> dict[str, Any]:
    """Every task (each with its priority and why); open ones are the pool: by deadline (its
    source: deadline_source), then priority. `priority=low` the "Low priority" list: the low tasks,
    and the emails that became no task as its email part (the quiet inbox, newest first)."""
    tasks = await runtime.tasks.list(status, low=True if priority == "low" else None)
    out: dict[str, Any] = {"tasks": [task_json(t, runtime.tasks.tz, lang) for t in tasks]}
    if priority == "low":
        async with runtime.pool.connection() as conn:
            emails = await quiet_inbox(conn, lang)
        tz = ZoneInfo(runtime.tasks.tz)
        out["emails"] = [e | {"sent_at": e["sent_at"].astimezone(tz).isoformat()} for e in emails]
    return out


@router.post("/tasks", response_model=schemas.Task, status_code=201)
async def create_task(
    body: schemas.TaskCreate, _: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> dict[str, Any]:
    task = await runtime.tasks.create(source_kind="manual", **body.model_dump())
    return task_json(task, runtime.tasks.tz, lang)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Permanently. Dismissing (PATCH status) is the reversible way."""
    await runtime.tasks.delete(task_id)
    return Response(status_code=204)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Undo a "never from this sender / this kind" rule by its id (`rule_id` of the dismiss): the
    rule goes, as when the user forgets it in About you, and within 24 h of making it the tasks the rule
    itself dismissed reopen. A rule already undone: 204 again. 404 when Pisa never had it."""
    async with runtime.pool.connection() as conn:
        if not await email_rules.is_rule(conn, rule_id):
            if await email_rules.was_forgotten(conn, rule_id):
                return Response(status_code=204)
            raise PisaError("not_found", f"There is no rule {rule_id[:40]!r}.", "Refresh the rules")
    await runtime.profile.forget(rule_id)  # as in About you: the rule's undo reopens what it dismissed
    return Response(status_code=204)


@router.post("/tasks/bulk", response_model=schemas.TasksBulkDone)
async def tasks_bulk(body: schemas.TasksBulk, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Today's edit mode: done, dismiss or delete the selected open tasks at once (delete: restorable
    for 24 h, as by voice). One Undo for the whole set; the same as Pisa's `tasks_bulk`."""
    out = await runtime.tasks_bulk.apply([str(i) for i in body.ids], body.action, origin="app")
    return out | {"undo_until": out["undo_until"].astimezone(ZoneInfo(runtime.tasks.tz)).isoformat()}


@router.post("/tasks/bulk/{bulk_id}/undo", response_model=schemas.TasksBulkUndone)
async def tasks_bulk_undo(bulk_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The set back as it was, within 24 h, once; 404 `not_found` after that."""
    return await runtime.tasks_bulk.undo(bulk_id)


@router.post("/tasks/{task_id}/dismiss", response_model=schemas.TaskDismissed)
async def dismiss_task(
    task_id: str, body: schemas.TaskDismiss, _: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> dict[str, Any]:
    """Dismiss it (reversible: PATCH status open). From an email, `never` also makes the user's rule
    "never from this sender" or "never this kind", kept in About you, and dismisses the open email tasks it
    covers."""
    out = await runtime.intake.dismiss_task(runtime.tasks, task_id, body.never, lang)
    return out | {"task": task_json(out["task"], runtime.tasks.tz, lang)}


@router.get("/inbox", response_model=schemas.InboxResponse)
async def inbox(_: DeviceDep, runtime: RuntimeDep, lang: LangDep) -> dict[str, Any]:
    """The quiet "From your email" list: recent emails that became no task, newest first (50 at most),
    each with why. Spam and promotions aren't listed."""
    tz = ZoneInfo(runtime.tasks.tz)
    emails = await runtime.intake.inbox(lang)
    return {"emails": [e | {"sent_at": e["sent_at"].astimezone(tz).isoformat()} for e in emails]}


@router.get("/changes", response_model=schemas.ChangesResponse)
async def changes(
    _: DeviceDep,
    runtime: RuntimeDep,
    lang: LangDep,
    since: Annotated[datetime | None, Query(description="RFC 3339; default 7 days ago")] = None,
) -> dict[str, Any]:
    """Pisa's own changes, newest first (50 at most): what Pisa dismissed, closed, moved or changed, each
    with its reason and its Undo while it lasts. Read-only; the Undo is each item's own REST call."""
    async with runtime.pool.connection() as conn:
        found = await changes_feed.changes(
            conn, since=since, now=runtime.tasks.now(), timezone=runtime.tasks.tz, lang=lang
        )
    return {"changes": found}


@router.get("/emails/{email_id}", response_model=schemas.EmailItem)
async def email(email_id: str, _: DeviceDep, runtime: RuntimeDep, lang: LangDep) -> dict[str, Any]:
    """One email by id, for an `email:<id>` object Pisa showed: who, when, the subject, Pisa's one line on
    it, and the task it became or why it is none. Never its body; 404 for spam, promotions or an unknown
    id."""
    async with runtime.pool.connection() as conn:
        found = await email_view(conn, email_id, lang)
    if found is None:
        raise PisaError("not_found", "There is no email with that id.", "Check the id")
    return found | {"sent_at": found["sent_at"].astimezone(ZoneInfo(runtime.tasks.tz)).isoformat()}


@router.post("/tasks/{task_id}/undo-done", response_model=schemas.Task)
async def undo_done(task_id: str, _: DeviceDep, runtime: RuntimeDep, lang: LangDep) -> dict[str, Any]:
    """Pisa closed the task because the user was there (or said so); within 24 h the user can undo it
    and the task is open again. 404 when Pisa didn't close it, 409 `undo_expired` after 24 h."""
    await runtime.done.undo_task(task_id)
    return task_json(await runtime.tasks.get(task_id), runtime.tasks.tz, lang)


@router.patch("/tasks/{task_id}", response_model=schemas.Task)
async def update_task(
    task_id: str, body: schemas.TaskPatch, _: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> dict[str, Any]:
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    if "repeat" in changes and changes["repeat"] is not None:
        changes["repeat"] = changes["repeat"].model_dump()
    if changes.get("title", "") is None:
        raise PisaError("validation_failed", "A task title can't be empty.", "Give the task a title")
    task = await runtime.tasks.update(task_id, changes)
    return task_json(task, runtime.tasks.tz, lang)


@router.get("/agenda", response_model=schemas.AgendaResponse)
async def agenda(
    _: DeviceDep,
    runtime: RuntimeDep,
    start: Annotated[
        date | None, Query(alias="from", description="YYYY-MM-DD; default today in the user's time zone")
    ] = None,
    days: Annotated[int, Query(ge=1, le=AGENDA_MAX_DAYS)] = AGENDA_DEFAULT_DAYS,
) -> dict[str, Any]:
    return await runtime.tasks.agenda(start, days)
