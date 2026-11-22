"""Pisa's own views, kept and run. A view's reads are Pisa's own read-only tools from an allowlist
(`domain.views.READS`), run now with the turn's budget and rails as any read; its blocks are filled with the
values as text and drawn through the rails (`sheet_blocks`: images and links only from what this run read).
Saved only on the user's own words (the brain's tool checks them); at most VIEWS_MAX."""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter, ValidationError
from rapidfuzz import fuzz

from pisa.brain.present_tools import sheet_basis, sheet_blocks
from pisa.domain import drawing as drawing_rules
from pisa.domain import owner_loop as owner_rules
from pisa.domain import quarantine
from pisa.domain import views as rules
from pisa.domain import wishes as wish_rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.sheet import SheetBlock, SheetData
from pisa.logging import get_logger
from pisa.ports.assistant import AssistantData, PlaceData
from pisa.ports.public_data import PublicData

_log = get_logger("pisa.views")
_BLOCK = TypeAdapter(SheetBlock)
# a view is found by its name as whole words in the request ("mostrami il meteo" finds "meteo"), or
# by a close spelling of the whole request (ratio, never partial: "metro" isn't "meteo"); two within
# FIND_CLOSE of each other: Pisa asks which
FIND_MIN = 90
FIND_CLOSE = 5


class ViewReads:
    """The allowlisted reads, each returning JSON-able data (a placeholder's path walks it)."""

    def __init__(
        self, data: AssistantData | None, places: PlaceData | None, *, owner: Owner,
        clock: Callable[[], datetime],
        public: PublicData | None = None,
    ) -> None:  # fmt: skip
        self._data, self._places, self._owner, self._clock = data, places, owner, clock
        self.public = public

    @property
    def owner(self) -> Owner:
        """The user's context (the views' sheets default to its language)."""
        return self._owner

    def _day(self, value: Any) -> date:
        today = self._owner.day(self._clock())
        text = str(value or "today").strip().casefold()
        if text in ("today", "oggi"):
            return today
        if text in ("tomorrow", "domani"):
            return today + timedelta(days=1)
        if text.startswith("+") and text[1:].isdigit():
            return today + timedelta(days=int(text[1:]))
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return today

    async def run(self, tool: str, args: dict[str, Any]) -> Any:
        if tool not in rules.READS:  # RAIL: never anything but the allowlist
            raise PisaError("validation_failed", f"{tool} is no read a view may run.", "")
        now = self._owner.local(self._clock())
        if tool == "get_current_time":
            return {"date": now.date().isoformat(), "time": f"{now:%H:%M}", "weekday": f"{now:%A}"}
        data, places = self._data, self._places
        if tool == "get_agenda" and data is not None:
            return await data.agenda_day(self._day(args.get("day")))
        if tool == "get_tasks" and data is not None:
            return {"tasks": [
                {"title": t.title, "priority": t.priority,
                 "due": t.due_at.astimezone(self._owner.zone).isoformat() if t.due_at else None}
                for t in await data.tasks("open")
            ][:rules.EACH_MAX]}  # fmt: skip
        if tool == "get_plan" and data is not None:
            return await data.plan()
        if tool == "get_weather" and places is not None:
            when = self._day(args.get("when")).isoformat()
            return await places.weather(str(args.get("where") or "") or None, when)
        if tool == "map_search" and places is not None:
            found = await places.map_search(
                str(args.get("query") or ""),
                near=args.get("near"),
                radius_m=None,
                open_now=False,
                provider=None,
            )
            return {"places": found[: rules.EACH_MAX]}
        if tool == "web_search" and places is not None:
            results = await places.web_search(
                str(args.get("query") or ""), include_domains=None, exclude_domains=None, recent_days=None,
                language=None,
            )  # fmt: skip
            return {"results": results[: rules.EACH_MAX]}
        if tool == "read_page" and places is not None:
            page = await places.read_page(str(args.get("url") or ""))
            if page is not None and isinstance(page.get("fields"), quarantine.Fields):  # as text
                page = page | {"fields": quarantine.render(page["fields"])}
            return page
        if tool in rules.PUBLIC_READS and self.public is not None:
            return await self.public.read(tool, args)
        if tool == "search_my_email" and data is not None:
            days = max(rules.DAYS_MIN, min(rules.DAYS_MAX, int(args.get("days") or 30)))
            hits = await data.search_email(str(args.get("query") or ""), days, 10)
            return {"emails": [asdict(h) for h in hits]}
        return None


_PLACE_URLS = ("url", "maps_url", "map_url", "photo_url")


def _pages(reads: list[dict[str, Any]], results: list[Any]) -> list[dict[str, str]]:
    """The pages this run read (the rail for images and links). A read_page result's own address
    and a map_search place's map and photo addresses; never any https string in a result (a search result,
    an email or a page's text names any host: "https://evil.example/phish")."""
    out: list[str] = []
    for read, result in zip(reads, results, strict=True):
        if not isinstance(result, dict):
            continue
        if read["tool"] == "read_page":
            out.append(str(result.get("url") or ""))
        elif read["tool"] == "map_search":
            for place in result.get("places") or []:
                if isinstance(place, dict):
                    out += [str(place.get(k) or "") for k in _PLACE_URLS]
                    photos = place.get("photos")
                    out += [str(p.get("url") or "") for p in photos or [] if isinstance(p, dict)]
    return [{"url": u} for u in out if u.startswith("https://")]


class ViewService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        reads: ViewReads,
        *,
        clock: Callable[[], datetime],
        builtin: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        self._pool, self._reads, self._clock = pool, reads, clock
        # the user loop: Pisa's built-in view "report" (never saved, never listed, never in the prompt)
        self.builtin = builtin

    async def names(self) -> list[str]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT name FROM pisa_views ORDER BY updated_at DESC")
            ).fetchall()
        return [str(r[0]) for r in rows]

    async def listed(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT id::text, name, description, provenance, created_at, updated_at, shown_at, "
                "jsonb_array_length(recipe->'reads') AS reads, "
                "jsonb_array_length(recipe->'blocks') AS blocks "
                "FROM pisa_views ORDER BY updated_at DESC"
            )
            return list(await cur.fetchall())

    async def delete(self, view_id: str) -> None:
        try:
            key = uuid.UUID(view_id)
        except ValueError:
            raise PisaError("not_found", "There is no view with that id.", "Refresh Pisa's views") from None
        async with self._pool.connection() as conn:
            gone = await (
                await conn.execute("DELETE FROM pisa_views WHERE id = %s RETURNING id", (key,))
            ).fetchone()
        if gone is None:
            raise PisaError("not_found", "There is no view with that id.", "Refresh Pisa's views")
        _log.info("view_deleted", view_id=view_id)

    async def save(
        self, name: str, description: str, recipe: dict[str, Any], *, hosts: set[str], said: str,
        turn_id: str | None, device_id: str | None,
    ) -> str:  # fmt: skip
        name = " ".join(name.split())[: rules.NAME_MAX]
        key = rules.name_key(name)
        if len(key) < rules.NAME_MIN:  # "a" would be found in every request
            least = rules.NAME_MIN
            return f"Not saved: a view needs a name of {least} letters or more (the user's word for it)."
        checked = rules.validate(recipe, hosts, said)
        if isinstance(checked, str):
            return f"Not saved: {checked}."
        if (wrong := await self.unresolved(checked)) is not None:
            return wrong
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa_views'))")
            count = await (
                await conn.execute("SELECT count(*) FROM pisa_views WHERE name_key <> %s", (key,))
            ).fetchone()
            if count is not None and count[0] >= rules.VIEWS_MAX:
                return f"Not saved: Pisa keeps {rules.VIEWS_MAX} views at most. Ask the user which to delete."
            old = await (
                await conn.execute("SELECT name FROM pisa_views WHERE name_key = %s", (key,))
            ).fetchone()
            if old is not None and not rules.asks_to_replace(said):
                # never replace a view silently: Pisa asks first
                return (
                    f'Not saved: Pisa already has a view called "{old[0]}". Ask the user in one line whether '
                    'to replace it; save again only when they say so ("sostituiscila", "replace it").'
                )
            await conn.execute(
                "INSERT INTO pisa_views (id, name, name_key, description, recipe, provenance, turn_id, "
                "device_id, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 'said', %s, %s, %s, %s) "
                "ON CONFLICT (name_key) DO UPDATE SET name = EXCLUDED.name, "
                "description = EXCLUDED.description, "
                "recipe = EXCLUDED.recipe, turn_id = EXCLUDED.turn_id, device_id = EXCLUDED.device_id, "
                "updated_at = EXCLUDED.updated_at",
                (uuid.uuid4(), name, key, " ".join(description.split())[: rules.DESCRIPTION_MAX],
                 Jsonb(checked.as_json()), _uuid(turn_id), _uuid(device_id), now, now),
            )  # fmt: skip
        _log.info("view_saved", reads=len(checked.reads), blocks=len(checked.blocks))
        return (
            f'Saved the view "{name}": "mostrami {name}" shows it again with fresh data. Say so in a few '
            "words."
        )

    async def unresolved(self, recipe: rules.Recipe, again: str = "Save it again") -> str | None:
        """The recipe's reads run now; a placeholder that finds nothing in
        what a read answered is refused, with that read's fields, so the model saves it again with them in
        the same turn (a placeholder like {{0.days.0.min}} would save a view that never shows). Reads only."""
        return self._wrong(recipe, await self._read_all(recipe, quiet=True), again)

    async def trial(
        self, title: str, stored: dict[str, Any], again: str
    ) -> tuple[dict[str, Any] | None, list[str], str | None]:
        """A wish's trial, its reads run once: (the sheet, what was dropped, the placeholders'
        refusal with the reads' fields, as `unresolved` gives it, or None)."""
        recipe = rules.Recipe(list(stored.get("reads") or []), list(stored.get("blocks") or []))
        results = await self._read_all(recipe)
        if (wrong := self._wrong(recipe, results, again)) is not None:
            return None, [], wrong
        sheet, dropped = self._sheet(title, recipe, results, self._reads.owner.tag)
        return sheet, dropped, None

    async def _read_all(self, recipe: rules.Recipe, *, quiet: bool = False) -> list[Any]:
        results: list[Any] = []
        for read in recipe.reads:
            try:
                value = await self._reads.run(read["tool"], read.get("args") or {})
            except Exception as err:  # a read failing now proves nothing (and leaves its blocks out)
                if not quiet:
                    _log.warning("view_read_failed", tool=read["tool"], error=type(err).__name__)
                value = None
            results.append(json.loads(json.dumps(value, default=str)) if value is not None else None)
        return results

    @staticmethod
    def _wrong(recipe: rules.Recipe, results: list[Any], again: str) -> str | None:
        wrong = rules.unresolved(recipe, results)
        if not wrong:
            return None
        sources: set[int] = set()
        for w in wrong:
            head = w.strip("{} ").split(".")[0].strip()
            if head.isdigit():
                sources.add(int(head))
            else:  # "{{get_weather.max}}" names its read by the tool: that read's fields
                named = {n for n, r in enumerate(recipe.reads) if r["tool"] == head}
                sources |= named or set(range(len(recipe.reads)))
        listed = "; ".join(
            f"read {n} ({recipe.reads[n]['tool']}): " + ", ".join(rules.fields(results[n], str(n)))
            for n in sorted(sources)
            if n < len(results) and results[n] is not None
        )
        return (
            f"Not saved: {', '.join(wrong[:6])} name no single value in what the reads answer now (a "
            "placeholder is {{n.path}}, n the read's number from 0). Their "
            f"fields: {listed}. {again} now with these paths (the same name), then say it in a few words."
        )

    async def find(self, name: str) -> list[dict[str, Any]]:
        """The view the user's words name: one, none, or the two closest when they score alike (Pisa asks)."""
        wanted = rules.name_key(name)
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT id::text, name, name_key, recipe FROM pisa_views")
            rows = list(await cur.fetchall())
        scored = sorted(((_score(wanted, r["name_key"]), r) for r in rows), key=lambda pair: -pair[0])
        found = [(score, r) for score, r in scored if score >= FIND_MIN]
        if len(found) > 1 and found[0][0] - found[1][0] <= FIND_CLOSE:
            return [found[0][1], found[1][1]]
        return [found[0][1]] if found else []

    async def run(self, name: str) -> tuple[str, dict[str, Any] | None, list[str]]:
        views = await self.find(name)
        if len(views) > 1:
            first, second = views[0]["name"], views[1]["name"]
            return f'Two views match {name[:40]!r}: "{first}" and "{second}". Ask the user which.', None, []
        view = views[0] if views else None
        if (
            view is None
            and self.builtin is not None
            and owner_rules.VIEW_NAME in rules.name_key(name).split()
        ):
            data = await self.builtin()  # the week's talk report, numbers only
            if data is None:
                return "No report yet: Pisa makes the first on Sunday.", None, []
            return data["title"], data, []
        if view is None:
            names = await self.names()
            known = f" Pisa's views: {', '.join(names[:10])}." if names else " Pisa has no saved views."
            return f"No view called {name[:40]!r}.{known}", None, []
        # it passed the save's rails; the reads are checked again as they run
        if view["recipe"].get("drawing") is not None:
            data, dropped = await self.drawing_of(view["recipe"])
        else:
            data, dropped = await self.sheet_of(view["name"], view["recipe"])
        if data is None:
            return view["name"], None, dropped
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_views SET shown_at = %s WHERE id = %s", (self._clock(), view["id"])
            )
        return view["name"], data, dropped

    async def drawing_of(self, stored: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """A saved drawing run now: its reads again, PISA_DATA filled from them, the
        code as it was saved and checked again (never rewritten). ({"drawing": …} or None, what was left
        out). Reads only."""
        drawing = dict(stored["drawing"])
        recipe = rules.Recipe(list(stored.get("reads") or []), [], drawing)
        # a drawing never reads outside text (one saved before the rule shows nothing)
        if any(isinstance(r, dict) and r.get("tool") in wish_rules.OUTSIDE_READS for r in recipe.reads):
            return None, ["it reads outside text (mail, the web, the map, public feeds): Pisa never draws it"]
        results = await self._read_all(recipe)
        data, missing = rules.fill_data(drawing.get("data") or {}, results)
        dropped = [f"{missing} value(s) with nothing today"] if missing else []
        if found := drawing_rules.problems(str(drawing.get("html") or "")):
            _log.warning("view_drawing_refused", problems=len(found))
            return None, ["its code no longer passes the drawing check"]
        if len(json.dumps(data, default=str).encode()) > drawing_rules.DATA_MAX:
            return None, ["today's data is too large to draw"]
        # a saved drawing with no reads is what Pisa knows ("Da quello che so"), every time it is shown
        basis = None if recipe.reads else "knowledge"
        return {"drawing": {**{k: drawing.get(k) for k in ("title", "summary", "kind", "html")},
                            "data": data, "basis": basis}}, dropped  # fmt: skip

    async def sheet_of(self, title: str, stored: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """A recipe run now into a sheet (the trials and skills): its reads, filled, every block
        through `show_sheet`'s rails and the protocol model; (None, why) when nothing is left to show. Reads
        only."""
        recipe = rules.Recipe(list(stored.get("reads") or []), list(stored.get("blocks") or []))
        return self._sheet(title, recipe, await self._read_all(recipe), self._reads.owner.tag)

    @staticmethod
    def _sheet(
        title: str, recipe: rules.Recipe, results: list[Any], lang: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        filled, missing = rules.fill(recipe, results)
        # a number the sheet shows traces to this run's reads, or it is left out
        texts = [json.dumps(r, ensure_ascii=False) for r in results if r is not None]
        built, dropped = sheet_blocks(
            filled,
            _pages(recipe.reads, results),
            results=texts,
            lang=lang,  # Pisa's language
        )
        kept: list[dict[str, Any]] = []
        for block in built:
            try:
                _BLOCK.validate_python(block)
                kept.append(block)
            except ValidationError:
                dropped.append(f"a {block.get('type')} that didn't fit its template")
        if missing:
            dropped.append(f"{missing} block(s) with no value today")
        if not kept:
            return None, dropped
        data: dict[str, Any] = {"title": title[:120], "blocks": kept}
        if (basis := sheet_basis(kept, texts)) is not None:
            data["basis"] = basis  # a saved diagram of what Pisa knows still says so
        SheetData.model_validate(data)
        return data, dropped


def _score(wanted: str, key: str) -> float:
    """The view's name as whole words in the request is a match, a longer name a closer one ("il
    meteo di roma" is "meteo roma" before "meteo"); else the whole request's spelling."""
    if len(key) >= rules.NAME_MIN and f" {key} " in f" {wanted} ":
        return 100.0 + 10 * len(key.split())
    return fuzz.ratio(wanted, key)


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None
