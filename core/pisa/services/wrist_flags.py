"""What the wrist may name of the plan.
Every plan item carries `private` (a calendar event private or confidential, a task they marked private) and
`sensitive` (a sensitive place, or health): the watch shows "Impegno" and never speaks it unless both
are false."""

import uuid
from typing import Any

from psycopg import AsyncConnection

from pisa.domain import wrist
from pisa.domain.health import health_place
from pisa.domain.learning import sensitive_place

PRIVATE_VISIBILITY = frozenset({"private", "confidential"})


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def mark_plan(conn: AsyncConnection, plan: dict[str, Any]) -> dict[str, Any]:
    """Each item of the plan (in place) with `private` and `sensitive`; a few small reads, never a model."""
    items = [i for i in plan.get("items") or [] if isinstance(i, dict)]
    if not items:
        return plan
    event_ids = sorted({str(e) for i in items for e in [i.get("event_id"), *(i.get("event_ids") or [])] if e})
    task_ids = [t for t in {_uuid(i.get("task_id")) for i in items} if t is not None]
    place_ids = [p for p in {_uuid(i.get(k)) for i in items for k in ("place_id", "to_place_id")} if p]
    events: dict[str, tuple[str | None, str | None]] = {}
    if event_ids:
        for event_id, visibility, location in await (
            await conn.execute(
                "SELECT id, visibility, location FROM calendar_events WHERE id = ANY(%s)", (event_ids,)
            )
        ).fetchall():
            events[str(event_id)] = (visibility, location)
    private_tasks: set[str] = set()
    if task_ids:
        rows = await (
            await conn.execute("SELECT id::text FROM tasks WHERE id = ANY(%s) AND private", (task_ids,))
        ).fetchall()
        private_tasks = {r[0] for r in rows}
    places: dict[str, tuple[str | None, str | None]] = {}
    if place_ids:
        for place_id, name, category in await (
            await conn.execute("SELECT id::text, name, category FROM places WHERE id = ANY(%s)", (place_ids,))
        ).fetchall():
            places[place_id] = (name, category)
    for item in items:
        ids = [str(e) for e in [item.get("event_id"), *(item.get("event_ids") or [])] if e]
        private = any((events.get(e, (None, None))[0] or "") in PRIVATE_VISIBILITY for e in ids) or (
            str(item.get("task_id") or "") in private_tasks
        )
        sensitive = any(_sensitive(*places[p]) for k in ("place_id", "to_place_id")
                        if (p := str(item.get(k) or "")) in places)  # fmt: skip
        sensitive = sensitive or any(
            _sensitive(location, None) for e in ids if (location := events.get(e, (None, None))[1])
        )
        # health by the item's own title ("Dentista", "Ritirare le analisi"), as the notices' rail
        sensitive = sensitive or bool(item.get("title") and _sensitive(str(item["title"]), None))
        item["private"], item["sensitive"] = private, sensitive
    return plan


def _sensitive(name: str | None, category: str | None) -> bool:
    return (
        wrist.withheld(here=(name, category), place=None, path=None)
        or health_place(name, category)
        or (sensitive_place(name, category))
    )
