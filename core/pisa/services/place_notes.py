"""Place memory. A note pinned to a place in the user's own words ("ricordami quando
sono da Esselunga di comprare il bagnoschiuma"), and the pool's tasks with a place, which fire the same way.

- The phone lists them (`GET /v1/places/notes`), registers the 20 nearest as region monitors (the iPhone's
  limit), and says `place_enter {note_ids}` when it enters one (the WebSocket, or `POST /v1/places/enter`).
- Pisa answers with the note: one notice (a push with Pisa closed), once per visit (6 h cool-down per note).
- A task's entry has the id `task:<uuid>` (it is the task's place; deleted with the task, never here).
Never logged: the notes' words and their coordinates.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from pisa.domain import rituals as rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.settings import is_quiet
from pisa.logging import get_logger
from pisa.services.notifications import enqueue

_log = get_logger("pisa.place_notes")
TASK_PREFIX = "task:"
# a note that carries an intention they closed ("fatto") or archived stays, silent: never monitored,
# never fired (nothing is deleted)


class PlaceNotesService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    async def create(
        self,
        text: str,
        lat: float,
        lon: float,
        *,
        radius_m: int | None = None,
        place_name: str | None = None,
        place_id: str | None = None,
        turn_id: str | None = None,
        device_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        text = " ".join(text.split())[:300]
        radius = rules.RADIUS_DEFAULT if radius_m is None else radius_m
        if not text:
            raise PisaError("validation_failed", "A place note needs its text.", "")
        if not rules.RADIUS_MIN <= radius <= rules.RADIUS_MAX:
            raise PisaError(
                "validation_failed", f"The radius must be {rules.RADIUS_MIN}-{rules.RADIUS_MAX} metres.", ""
            )
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise PisaError("validation_failed", "The coordinates are out of range.", "")
        note_id = uuid.uuid4()
        async with self._pool.connection() as conn, conn.transaction():
            count = await (await conn.execute("SELECT count(*) FROM place_notes")).fetchone()
            if count and count[0] >= rules.NOTES_MAX:
                raise PisaError(
                    "validation_failed",
                    f"{rules.NOTES_MAX} place notes are kept at most.",
                    "Delete one first",
                )
            await conn.execute(
                "INSERT INTO place_notes (id, text, lat, lon, radius_m, place_name, place_id, turn_id, "
                "device_id, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    note_id,
                    text,
                    lat,
                    lon,
                    radius,
                    (place_name or None) and place_name[:200],
                    _maybe_uuid(place_id),
                    _maybe_uuid(turn_id),
                    device_id,
                    self._clock(),
                ),
            )
        _log.info("place_note_created", note_id=str(note_id), radius_m=radius)
        return await self.get(str(note_id))

    async def known_place(self, name: str) -> tuple[tuple[float, float], str, str] | None:
        """A place Pisa already knows by this name (one they went to, or a task's or an event's): the one they
        stayed at most recently first. None when none matches (then the place tools search)."""
        text = " ".join(name.split())
        if len(text) < 2:
            return None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT p.lat, p.lon, p.name, p.id::text FROM places p LEFT JOIN stays s ON s.place_id = "
                    "p.id WHERE p.lat IS NOT NULL AND lower(p.name) LIKE lower(%s) || '%%' ESCAPE '\\' "
                    "GROUP BY p.id "
                    "ORDER BY max(s.arrived_at) DESC NULLS LAST, p.updated_at DESC LIMIT 1",
                    (text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),),
                )
            ).fetchone()
        return ((row[0], row[1]), row[2], row[3]) if row else None

    async def get(self, note_id: str) -> dict[str, Any]:
        for note in await self.listed(tasks=False):
            if note["id"] == note_id:
                return note
        raise PisaError("not_found", "No such place note.", "")

    async def listed(self, *, tasks: bool = True) -> list[dict[str, Any]]:
        """The notes (newest first), then the open tasks with a place (they fire the same way)."""
        async with self._pool.connection() as conn:
            notes = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT id::text, text, lat, lon, radius_m, place_name, created_at, last_fired_at "
                    "FROM place_notes n WHERE NOT EXISTS (SELECT 1 FROM intentions i WHERE i.place_note_id = "
                    "n.id AND i.state IN ('done', 'archived')) ORDER BY created_at DESC"
                )
            ).fetchall()
            rows = (
                await (
                    await conn.cursor(row_factory=dict_row).execute(
                        "SELECT t.id::text AS id, t.title, p.lat, p.lon, p.name, t.created_at, f.fired_at "
                        "FROM tasks t JOIN places p ON p.id = t.place_id LEFT JOIN place_task_fires f ON "
                        "f.task_id = t.id WHERE t.status = 'open' AND p.lat IS NOT NULL ORDER BY "
                        "t.created_at DESC LIMIT 100"
                    )
                ).fetchall()
                if tasks
                else []
            )
        out = [dict(n) for n in notes]
        out += [
            {
                "id": TASK_PREFIX + r["id"],
                "text": r["title"],
                "lat": r["lat"],
                "lon": r["lon"],
                "radius_m": rules.RADIUS_DEFAULT,
                "place_name": r["name"],
                "created_at": r["created_at"],
                "last_fired_at": r["fired_at"],
                "task_id": r["id"],
            }
            for r in rows
        ]
        return out

    async def delete(self, note_id: str) -> None:
        key = _maybe_uuid(note_id)
        if key is None:
            raise PisaError("not_found", "No such place note (a task's place goes with the task).", "")
        async with self._pool.connection() as conn:
            cur = await conn.execute("DELETE FROM place_notes WHERE id = %s", (key,))
        if not cur.rowcount:
            raise PisaError("not_found", "No such place note.", "")
        _log.info("place_note_deleted", note_id=note_id)

    async def forget(self, what: str) -> str:
        """By voice ("dimentica la nota dell'Esselunga"): the one note whose words or place match."""
        wanted = rules.content_words(what)
        matches = [
            n
            for n in await self.listed(tasks=False)
            if wanted & rules.content_words(f"{n['text']} {n['place_name'] or ''}") or n["id"] == what.strip()
        ]
        if not matches:
            return "No place note matches."
        if len(matches) > 1:
            names = "; ".join(f"{n['place_name'] or '?'}: {n['text']}" for n in matches[:4])
            return f"Several notes match ({len(matches)}: {names}): ask which one."
        await self.delete(matches[0]["id"])
        return f"Forgotten: the note at {matches[0]['place_name'] or 'that place'}. Say it in a few words."

    async def enter(self, device_id: uuid.UUID | None, note_ids: list[str]) -> dict[str, Any]:
        """The phone entered these regions: each note (or task) not fired in the last 6 h fires now, in one
        notice. Returns {fired: [ids], text} (text null when nothing fired)."""
        now = self._clock()
        fired: list[str] = []
        lines: list[str] = []
        async with self._pool.connection() as conn, conn.transaction():
            settings = await self._owner.settings(conn)
            lang = str(settings["language"])
            if is_quiet(now, settings, self._owner.tz):  # RAIL: nothing in quiet hours; the next visit fires
                _log.info("place_entered_quiet", asked=len(note_ids))
                return {"fired": [], "text": None}
            for raw in dict.fromkeys(note_ids[: rules.REGIONS]):
                if raw.startswith(TASK_PREFIX):
                    task_id = _maybe_uuid(raw.removeprefix(TASK_PREFIX))
                    # one statement decides and records the fire (two entries at once: one fires)
                    row = (
                        await (
                            await conn.execute(
                                "INSERT INTO place_task_fires (task_id, fired_at) SELECT t.id, %s FROM "
                                "tasks t "
                                "WHERE t.id = %s AND t.status = 'open' AND t.place_id IS NOT NULL "
                                "ON CONFLICT (task_id) DO UPDATE SET fired_at = EXCLUDED.fired_at "
                                "WHERE place_task_fires.fired_at <= %s "
                                "RETURNING (SELECT title FROM tasks WHERE id = place_task_fires.task_id)",
                                (now, task_id, now - rules.COOL_DOWN),
                            )
                        ).fetchone()
                        if task_id
                        else None
                    )
                    if row is None:
                        continue
                    lines.append(rules.task_line(lang, row[0]))
                else:
                    key = _maybe_uuid(raw)
                    row = (
                        await (
                            await conn.execute(
                                "SELECT text, place_name, last_fired_at FROM place_notes n WHERE id = %s "
                                "AND NOT EXISTS (SELECT 1 FROM intentions i WHERE i.place_note_id = n.id "
                                "AND i.state IN ('done', 'archived')) FOR UPDATE",
                                (key,),
                            )
                        ).fetchone()
                        if key
                        else None
                    )
                    if row is None or not rules.may_fire(row[2], now):
                        continue
                    await conn.execute("UPDATE place_notes SET last_fired_at = %s WHERE id = %s", (now, key))
                    lines.append(rules.note_line(lang, row[1], row[0]))
                fired.append(raw)
            text = " ".join(lines)[:1000] if lines else None
            if text:
                await enqueue(
                    conn,
                    kind="reminder",
                    text=text,
                    lang=lang,
                    now=now,
                    timezone=self._owner.tz,
                    device_id=device_id,
                    respect_quiet=False,  # checked above: a note held for later is no use once they have left
                    path="place.note",
                )
        _log.info("place_entered", asked=len(note_ids), fired=len(fired))
        return {"fired": fired, "text": text}


def _maybe_uuid(text: str | None) -> uuid.UUID | None:
    if not text:
        return None
    try:
        return uuid.UUID(str(text))
    except ValueError:
        return None
