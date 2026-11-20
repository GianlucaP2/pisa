"""The data behind the conversation agent's place tools (PlaceData port)."""

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from psycopg_pool import AsyncConnectionPool
from rapidfuzz import fuzz

from pisa.domain import quarantine, visible
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.places import Mode, open_state
from pisa.domain.tasks import title_key
from pisa.ports.places import Coord, Usage
from pisa.services.places import PlacesService, parse_coord
from pisa.services.reader import ReaderService, note_hidden
from pisa.services.weather import WeatherService

WEB_RESULTS = 6
MAP_RESULTS = 10  # a pick compares many; Pisa's search allows 10
SNIPPET_MAX = 400  # a result's snippet, for the model
PAGE_MAX = 12_000  # a page kept and read (a guide's whole list)
WALK_MAX = 12  # places in one batched walk-times call
# web_search's language → Tavily's country boost (pages from that country rank higher).
COUNTRY_BY_LANGUAGE = {"it": "italy", "nl": "netherlands", "fr": "france", "de": "germany", "es": "spain",
                       "pt": "portugal"}  # fmt: skip
_COORD = re.compile(r"-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?")
HERE_WORDS = frozenset({"here", "qui", "qua", "where i am", "dove sono", "from here", "da qui"})
TITLE_MATCH = 85  # how close a title must be to an event's or task's (partial ratio, normalised)


class PlaceAssistantData:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        places: PlacesService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        weather: WeatherService | None = None,
        reader: ReaderService | None = None,
    ) -> None:
        self._pool = pool
        self._places = places
        self._weather = weather
        self._owner = owner
        self._clock = clock
        self.reader = reader  # the tool-less reader

    async def where(self) -> dict[str, Any] | None:
        latest = await self._places.latest_location()
        if latest is None:
            return None
        (lat, lon), at = latest
        now = self._clock()
        async with self._pool.connection() as conn:
            accuracy = await (
                await conn.execute(
                    "SELECT accuracy_m FROM device_locations ORDER BY measured_at DESC LIMIT 1"
                )
            ).fetchone()
            nxt = await (
                await conn.execute(
                    "SELECT e.title, e.starts_at, coalesce(p.name, e.location), e.place_id::text "
                    "FROM calendar_events e LEFT JOIN places p ON p.id = e.place_id "
                    "WHERE NOT e.all_day AND e.starts_at > %s AND e.starts_at < %s "
                    "AND (e.place_id IS NOT NULL OR e.location IS NOT NULL) ORDER BY e.starts_at LIMIT 1",
                    (now, now.astimezone(self._owner.zone).replace(hour=23, minute=59)),
                )
            ).fetchone()
        address = None
        lang = await self._owner.lang(self._pool)
        try:
            choice = await self._places.providers.choose(
                "search",
                lambda a: a.reverse_geocode((lat, lon), lang=lang),
                can=lambda a: hasattr(a, "reverse_geocode"),
                usage=lambda a: Usage("apple.geocode"),
            )
            address = choice.value.address if choice.value else None
        except PisaError:
            pass  # the coordinates alone still answer
        return {
            "lat": lat,
            "lon": lon,
            "age_min": int((now - at).total_seconds() // 60),
            "accuracy_m": accuracy[0] if accuracy else None,
            "address": address,
            "next": {
                "title": nxt[0],
                "when": nxt[1].astimezone(self._owner.zone).isoformat(),
                "place": nxt[2],
                "place_id": nxt[3],
            }
            if nxt
            else None,
        }

    async def map_search(
        self,
        query: str,
        *,
        near: str | None,
        radius_m: int | None,
        open_now: bool,
        provider: str | None,
    ) -> list[dict[str, Any]]:
        """Places with what the map knows about them (rating, price, open now, attributes): map_search."""
        center: Any = None
        if near:
            center = "here" if near.strip().lower() in HERE_WORDS else (await self.resolve(near))[0]
        return await self._places.search(
            query, near=center, open_now=open_now, limit=MAP_RESULTS, rich=True, radius_m=radius_m,
            provider=provider,
        )  # fmt: skip

    async def find(self, place: str) -> str:
        text = place.strip()
        try:
            place_id = uuid.UUID(text)
        except ValueError:
            found = await self._places.search(text, near=None, limit=1) if len(text) >= 2 else []
            if not found:
                raise PisaError(
                    "not_found", f"No place matches {text[:60]!r}.", "Say the name differently"
                ) from None
            return found[0]["id"]
        await self._places.name(place_id)  # not_found when it doesn't exist
        return str(place_id)

    async def card(self, place_id: str, *, reviews: bool = True, travel: bool = True) -> dict[str, Any]:
        return await self._places.card(PlacesService.parse_id(place_id), reviews=reviews, travel=travel)

    async def open_at(self, place_id: str, at: datetime) -> dict[str, Any]:
        """{name, known, open, next_change} at `at`, from the place's hours (cached for a day)."""
        pid = PlacesService.parse_id(place_id)
        info, _ = await self._places.details(pid, reviews=False)
        name = await self._places.name(pid)
        periods = (info.current + info.weekly) if info else []
        if not periods:
            return {"name": name, "known": False, "open": None, "next_change": None}
        is_open, change = open_state(
            periods, at, self._owner.tz, frozenset(info.closed_dates if info else [])
        )
        return {"name": name, "known": True, "open": is_open, "next_change": change}

    async def web_search(
        self,
        query: str,
        *,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        recent_days: int | None,
        language: str | None,
    ) -> list[dict[str, str]]:
        """The web's results (title, url, snippet): web_search. The model reads and weighs them."""
        country = COUNTRY_BY_LANGUAGE.get((language or "").lower()[:2])
        found = await self._places.providers.choose(
            "web",
            lambda a: a.search(
                query,
                max_results=WEB_RESULTS,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                recent_days=recent_days,
                country=country,
            ),
            usage=lambda a: a.usage_for_search(),
        )
        # titles and snippets stay text, their hidden characters out and counted
        out, hidden = [], 0
        for r in found.value:
            (title, a), (snippet, b) = visible.plain(r.title), visible.plain(r.content)
            hidden += a + b
            out.append({"title": " ".join(title.split())[:200], "url": r.url,
                        "snippet": " ".join(snippet.split())[:SNIPPET_MAX]})  # fmt: skip
        if hidden:
            async with self._pool.connection() as conn:
                await note_hidden(conn, "page", hidden, self._owner.day(self._clock()))
        return out

    async def save_menu(self, place_id: str, url: str, highlights: list[str]) -> None:
        await self._places.save_menu(place_id, url, highlights)

    async def read_page(self, url: str, question: str | None = None) -> dict[str, Any] | None:
        """One page's text (trimmed) and its reader fields (`fields`: what the model
        sees), or None when it couldn't be read: read_page. `question`: their request, for the reader."""
        page = await self._page(url)
        if page is None:
            return None
        text, runs = visible.plain(page["text"])
        hidden = page.get("hidden", 0) + runs
        if hidden:  # counted (a number, never the text)
            async with self._pool.connection() as conn:
                day = self._owner.day(self._clock())
                await note_hidden(conn, "page", hidden, day)
        if self.reader is None:
            fields = quarantine.read("web-page", text, source=page["url"], title=page["url"], hidden=hidden)
        else:
            fields = await self.reader.page(
                page["url"], text, hidden, await self._owner.lang(self._pool), question
            )
        return {"url": page["url"], "text": text, "fields": fields}

    async def _page(self, url: str) -> dict[str, Any] | None:
        url = url.strip()
        if not (url.startswith(("https://", "http://")) and len(url) <= 2000 and " " not in url):
            raise PisaError(
                "validation_failed",
                "The address to read must be a full http(s) url.",
                "Use a url from web_search",
            )
        if self._places.counted_reader is not None:  # Pisa reads it itself, signed
            read = await self._places.counted_reader(url)
            if read is not None:
                return {"url": read[0], "text": " ".join(read[1].split())[:PAGE_MAX], "hidden": read[2]}
        found = await self._places.providers.choose(
            "web", lambda a: a.extract([url]), usage=lambda a: a.usage_for_extract(1)
        )
        if not found.value:
            return None
        page = found.value[0]
        return {"url": page.url, "text": " ".join(page.text.split())[:PAGE_MAX]}

    async def weather(self, where: str | None, when: str | None) -> dict[str, Any]:
        if self._weather is None:
            raise PisaError("internal", "The weather isn't set up in this process.", "Try again later")
        return await self._weather.for_tool(where, when, self.resolve)

    async def resolve(self, text: str) -> tuple[Coord, str, str | None]:
        """(position, name, place id) of "here", "lat,lon", today's event or open task by title (its place),
        or a place name or address (a search near the user)."""
        text = text.strip()
        if text.casefold() in HERE_WORDS:
            return await self._places.here(), "here", None
        if _COORD.fullmatch(text):
            return parse_coord(text.replace(" ", ""), "place"), text, None
        key = title_key(text)
        now = self._clock()
        day_start = now.astimezone(self._owner.zone).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT e.title, p.lat, p.lon, p.id::text, p.name FROM calendar_events e "
                    "JOIN places p ON p.id = e.place_id WHERE NOT e.all_day AND p.lat IS NOT NULL "
                    "AND e.starts_at >= %s AND e.starts_at < %s "
                    "UNION ALL SELECT t.title, p.lat, p.lon, p.id::text, p.name FROM tasks t "
                    "JOIN places p ON p.id = t.place_id WHERE t.status = 'open' AND p.lat IS NOT NULL",
                    (day_start, day_start + timedelta(days=1)),
                )
            ).fetchall()
        scored = [
            (fuzz.partial_ratio(key, title_key(title)), lat, lon, pid, name)
            for title, lat, lon, pid, name in rows
        ]
        best = max(scored, default=None, key=lambda r: r[0])
        if best is not None and best[0] >= TITLE_MATCH and key:
            return (best[1], best[2]), best[4], best[3]
        place_id = await self.find(text)
        return (
            await self._places.coords(uuid.UUID(place_id)),
            await self._places.name(uuid.UUID(place_id)),
            place_id,
        )

    async def _mode(self, mode: str | None) -> Mode:
        if mode is None:
            async with self._pool.connection() as conn:
                mode = (await self._owner.settings(conn))["places"]["default_mode"]
        return cast(Mode, mode)

    async def travel_many(self, place_ids: list[str], mode: str | None) -> dict[str, Any]:
        """From here to many places in one provider call (a distance column for a
        whole table costs one call, not one a place)."""
        travel_mode = await self._mode(mode)
        ids = list(dict.fromkeys(p.strip() for p in place_ids if p.strip()))[:WALK_MAX]
        found: list[tuple[str, Any]] = []
        for place_id in ids:
            try:
                found.append((place_id, await self._places.coords(PlacesService.parse_id(place_id))))
            except PisaError:
                continue  # a place with no coordinates: no time, never a guess
        if not found:
            return {"mode": travel_mode, "provider": "", "times": []}
        estimates, provider, _ = await self._places.travel_between(
            await self._places.here(), [c for _, c in found], travel_mode
        )
        times = [
            {"place_id": pid, "minutes": e.minutes if e else None, "distance_m": e.distance_m if e else None}
            for (pid, _), e in zip(found, estimates, strict=False)
        ]
        return {"mode": travel_mode, "provider": provider, "times": times}

    async def detour(self, origin: str, via: str, destination: str, mode: str | None) -> dict[str, Any]:
        """The extra minutes of a stop on the way: (origin → via → destination) - (origin → destination)."""
        travel_mode = await self._mode(mode)
        start, start_name, _ = await self.resolve(origin)
        stop, stop_name, stop_id = await self.resolve(via)
        end, end_name, _ = await self.resolve(destination)
        firsts, provider, _ = await self._places.travel_between(start, [stop, end], travel_mode)
        seconds, _, _ = await self._places.travel_between(stop, [end], travel_mode)
        legs = [firsts[0], seconds[0], firsts[1]]
        if any(leg is None for leg in legs):
            raise PisaError(
                "place_provider_error",
                f"{provider} found no {travel_mode} route for one of the legs.",
                "Try another mode",
            )
        to_stop, onward, direct = (leg.minutes for leg in legs)  # type: ignore[union-attr]
        return {
            "extra_min": to_stop + onward - direct, "to_stop_min": to_stop, "onward_min": onward,
            "direct_min": direct, "mode": travel_mode, "provider": provider, "from": start_name,
            "via": stop_name, "via_place_id": stop_id, "to": end_name,
        }  # fmt: skip

    async def travel(
        self,
        destination: str,
        mode: str | None,
        depart_at: datetime | None,
        arrive_by: datetime | None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        if origin is not None and origin.strip().casefold() not in HERE_WORDS:
            start, start_name, _ = await self.resolve(origin)
            end, end_name, end_id = await self.resolve(destination)
            travel_mode = await self._mode(mode)
            estimates, provider, _ = await self._places.travel_between(
                start, [end], travel_mode, depart_at=depart_at, arrive_by=arrive_by
            )
            if estimates[0] is None:
                raise PisaError(
                    "place_provider_error",
                    f"{provider} found no {travel_mode} route there.",
                    "Try another mode",
                )
            e = estimates[0]
            return {
                "minutes": e.minutes, "traffic_minutes": e.traffic_minutes, "distance_m": e.distance_m,
                "mode": travel_mode, "provider": provider, "place_id": end_id, "name": end_name,
                "from": start_name,
            }  # fmt: skip
        text = destination.strip()
        if _COORD.fullmatch(text):
            coord = parse_coord(text.replace(" ", ""), "destination")
            if mode is None:
                async with self._pool.connection() as conn:
                    mode = (await self._owner.settings(conn))["places"]["default_mode"]
            estimates, provider, _ = await self._places.travel_between(
                await self._places.here(), [coord], cast(Mode, mode), depart_at=depart_at, arrive_by=arrive_by
            )
            if estimates[0] is None:
                raise PisaError(
                    "place_provider_error", f"{provider} found no {mode} route there.", "Try another mode"
                )
            e = estimates[0]
            return {
                "minutes": e.minutes, "traffic_minutes": e.traffic_minutes, "distance_m": e.distance_m,
                "mode": mode, "provider": provider, "place_id": None, "name": text,
            }  # fmt: skip
        place_id = await self.find(text)
        result = await self._places.travel(
            uuid.UUID(place_id),
            mode=cast(Mode | None, mode),
            origin="here",
            depart_at=depart_at,
            arrive_by=arrive_by,
        )
        card_name = await self._places.name(uuid.UUID(place_id))
        return {**result, "place_id": place_id, "name": card_name}
