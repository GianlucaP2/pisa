"""`GET /v1/changes`: Pisa's own changes, newest first, each with its reason
and its Undo while it lasts. A read-only list the app decodes as its own (no new approval kind):

- **task_dismissed**: a task aged out (a week without an answer to "è ancora da fare?"), from
  `pisa_decisions`; restored with `PATCH /v1/tasks/{id}` `status: "open"` while it is still dismissed.
- **task_done / event_done**: closed because the user was there, said so, or a reply in the thread said it
  (`done_by_presence`); undone with the Undo within 24 h.
- **calendar / plan**: an action Pisa asked and the user approved, or Pisa's own proposal applied
  (`pisa_decisions` with its approval); undone with the approval's Undo while it lasts.
- **tasks_bulk**: tasks changed at once (`task_bulk`); undone once within 24 h.

The reason is Pisa's own words (the decision's purpose) or code's; never an email's text. Titles are the
user's items' (data the app shows as it shows Today).
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from pisa.domain import tasks_bulk as bulk_rules
from pisa.domain.reasons import purpose_said

KEPT = timedelta(days=7)  # how far back the list goes by default
LIMIT = 50
_DONE_WHY = {
    "it-IT": {"stay": "Chiuso: eri lì.", "voice": "Chiuso: l'hai detto tu.",
              "email": "Chiuso: una risposta nella conversazione dice che è fatto."},
    "en-US": {"stay": "Closed: you were there.", "voice": "Closed: you said so.",
              "email": "Closed: a reply in the thread says it's done."},
}  # fmt: skip
_BULK_WHY = {
    "it-IT": {"done": "{n} attività segnate fatte insieme.", "dismiss": "{n} attività tolte insieme.",
              "delete": "{n} attività eliminate insieme."},
    "en-US": {"done": "{n} tasks marked done together.", "dismiss": "{n} tasks dismissed together.",
              "delete": "{n} tasks deleted together."},
}  # fmt: skip
_APPROVAL_KIND = {"calendar_create": "calendar", "calendar_move": "calendar", "calendar_cancel": "calendar"}


def _iso(moment: datetime | None, tz: ZoneInfo) -> str | None:
    return moment.astimezone(tz).isoformat() if moment else None


async def changes(
    conn: AsyncConnection, *, since: datetime | None, now: datetime, timezone: str, lang: str
) -> list[dict[str, Any]]:
    tz = ZoneInfo(timezone)
    since = since or now - KEPT
    words = "it-IT" if lang == "it-IT" else "en-US"
    out: list[dict[str, Any]] = []

    aged = await (
        await conn.execute(
            "SELECT d.id, d.at, d.purpose, t.id::text, t.title, t.status FROM pisa_decisions d "
            "JOIN tasks t ON t.id::text = d.ref WHERE d.move = 'act' AND d.tool = 'aging' AND d.at >= %s",
            (since,),
        )
    ).fetchall()
    for decision, at, why, task_id, title, status in aged:
        undo = {"method": "PATCH", "path": f"/v1/tasks/{task_id}", "body": {"status": "open"}, "until": None}
        out.append({
            "id": f"decision:{decision}", "kind": "task_dismissed", "at": at, "title": title,
            "why": purpose_said(why, "task_dismissed", lang) or "", "by": "pisa", "task_id": task_id,
            "undo": undo if status == "dismissed" else None,
        })  # fmt: skip

    done = await (
        await conn.execute(
            "SELECT id, task_id::text, event_id, title, source, done_at, undo_until, undone_at "
            "FROM done_by_presence WHERE done_at >= %s",
            (since,),
        )
    ).fetchall()
    for row_id, task_id, event_id, title, source, at, until, undone in done:
        open_ = undone is None and until > now
        path = f"/v1/tasks/{task_id}/undo-done" if task_id else f"/v1/events/{event_id}/undo-attended"
        out.append({
            "id": f"done:{row_id}", "kind": "task_done" if task_id else "event_done", "at": at,
            "title": title, "why": _DONE_WHY[words].get(source, ""),
            "by": "owner" if source == "voice" else "pisa", "task_id": task_id, "event_id": event_id,
            "undo": {"method": "POST", "path": path, "body": None, "until": until} if open_ else None,
        })  # fmt: skip

    acts = await (
        await conn.execute(
            "SELECT d.id, d.at, d.tool, d.purpose, d.provenance, a.id::text, a.kind, a.summary, a.status, "
            "a.undo_until, a.undone_at FROM pisa_decisions d JOIN approvals a ON a.id::text = d.ref "
            "WHERE d.move = 'act' AND d.at >= %s AND a.status = 'done'",
            (since,),
        )
    ).fetchall()
    for decision, at, tool, why, provenance, approval, kind, summary, _, until, undone in acts:
        open_ = undone is None and until is not None and until > now
        undo = {"method": "POST", "path": f"/v1/approvals/{approval}/undo", "body": None, "until": until}
        line_kind = "plan" if tool == "apply_option" else _APPROVAL_KIND.get(kind, "calendar")
        out.append({
            "id": f"decision:{decision}",
            "kind": line_kind,
            "at": at, "title": summary, "why": purpose_said(why, line_kind, lang) or "",
            "by": "owner" if provenance == "owner_words" else "pisa", "approval_id": approval,
            "undo": undo if open_ else None,
        })  # fmt: skip

    sets = await (
        await conn.execute(
            "SELECT id, action, jsonb_array_length(items), created_at, undone_at FROM task_bulk "
            "WHERE created_at >= %s",
            (since,),
        )
    ).fetchall()
    for bulk_id, action, count, at, undone in sets:
        until = at + bulk_rules.UNDO_KEPT
        open_ = undone is None and until > now
        out.append({
            "id": f"bulk:{bulk_id}", "kind": "tasks_bulk", "at": at, "title": None,
            "why": _BULK_WHY[words][action].format(n=count), "by": "owner",
            "undo": {"method": "POST", "path": f"/v1/tasks/bulk/{bulk_id}/undo", "body": None, "until": until}
            if open_ else None,
        })  # fmt: skip

    out.sort(key=lambda c: c["at"], reverse=True)
    for change in out[:LIMIT]:
        change["at"] = _iso(change["at"], tz)
        if change["undo"] is not None:
            change["undo"]["until"] = _iso(change["undo"]["until"], tz)
    return out[:LIMIT]
