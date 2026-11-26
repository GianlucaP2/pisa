"""The weather: Open-Meteo's forecast for here, a place or a point, cached 30 minutes per
~1 km cell; the `get_weather` tool's answers, the briefing's line and the rain hours the planner uses."""

import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import weather as rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.weather import Day, Forecast, Hour
from pisa.logging import get_logger
from pisa.ports.places import Coord
from pisa.ports.weather import WeatherError, WeatherProvider
from pisa.services import home as home_store
from pisa.services.places import PlacesService, parse_coord

_log = get_logger("pisa.weather")


def _to_json(forecast: Forecast) -> dict[str, Any]:
    return {
        "days": [dataclasses.asdict(d) | {"date": d.date.isoformat()} for d in forecast.days],
        "hours": [dataclasses.asdict(h) | {"time": h.time.isoformat()} for h in forecast.hours],
    }


def _from_json(body: dict[str, Any]) -> Forecast:
    return Forecast(
        [Day(**(d | {"date": date.fromisoformat(d["date"])})) for d in body["days"]],
        [Hour(**(h | {"time": datetime.fromisoformat(h["time"])})) for h in body["hours"]],
    )


class WeatherService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        provider: WeatherProvider,
        places: PlacesService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._provider = provider
        self._places = places
        self._owner = owner
        self._clock = clock

    async def forecast(self, coord: Coord) -> Forecast:
        """Today and 7 days ahead at `coord`: from the cache when its cell was fetched in the last 30
        minutes. Never one fetched after now.
        """
        key = rules.cell(*coord)
        now = self._clock()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT forecast FROM weather_cache WHERE cell = %s AND fetched_at > %s "
                    "AND fetched_at <= %s",
                    (key, now - timedelta(minutes=rules.CACHE_MIN), now),
                )
            ).fetchone()
        if row is not None:
            return _from_json(row[0])
        try:
            fresh = await self._provider.forecast(coord[0], coord[1], self._owner.tz)
        except WeatherError as err:
            raise PisaError("provider_error", err.message, "Try again in a minute") from None
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO weather_cache (cell, forecast, fetched_at) VALUES (%s, %s, %s) ON CONFLICT "
                "(cell) "
                "DO UPDATE SET forecast = EXCLUDED.forecast, fetched_at = EXCLUDED.fetched_at",
                (key, Jsonb(_to_json(fresh)), now),
            )
            await conn.execute(
                "DELETE FROM weather_cache WHERE fetched_at < %s OR fetched_at > %s",
                (now - timedelta(days=1), now),
            )
        return fresh

    # --- GET /v1/weather ----------------------------------------------------------------------------------

    async def report(self, near: str, day: date | None) -> dict[str, Any]:
        """near: here, place:<uuid> or <lat>,<lon>. location_unknown when "here" has no location."""
        today = self._owner.day(self._clock())
        day = day or today
        if not today <= day <= today + timedelta(days=rules.DAYS_AHEAD):
            raise PisaError(
                "validation_failed",
                f"The forecast covers today and the next {rules.DAYS_AHEAD} days, not {day.isoformat()}.",
                "Ask for a date within a week",
            )
        coord, label = await self._where(near)
        return self._view(await self.forecast(coord), day, label or "", await self._owner.lang(self._pool))

    async def _where(self, near: str) -> tuple[Coord, str | None]:
        if near == "here":
            try:
                coord = await self._places.here()  # location_unknown without one
            except PisaError as err:
                # no location from the phone: the user's home city:
                # only while they are in their home zone (away, home's weather isn't "here")
                away = self._owner.away
                if err.code != "location_unknown" or away or (home := await self._home()) is None:
                    raise
                return home
            return coord, await self._places.city_at(coord) or "here"
        if near.startswith("place:"):
            try:
                place_id = uuid.UUID(near.removeprefix("place:"))
            except ValueError:
                raise PisaError(
                    "validation_failed", f"{near[:60]!r} is not place:<uuid>.", "Fix the request"
                ) from None
            return await self._places.coords(place_id), await self._places.name(place_id)
        coord = parse_coord(near, "near")
        return coord, await self._places.city_at(coord) or f"{coord[0]:.3f}, {coord[1]:.3f}"

    async def _home(self) -> tuple[Coord, str] | None:
        async with self._pool.connection() as conn:
            home = await home_store.read(conn)
        if home is None:
            return None
        city = await self._places.city_at(home.coord)
        return home.coord, city or home.name or "home"

    def _view(self, forecast: Forecast, day: date, label: str, lang: str) -> dict[str, Any]:
        tz = self._owner.zone
        found = next((d for d in forecast.days if d.date == day), None)
        if found is None:
            raise PisaError(
                "provider_error", f"Open-Meteo has no forecast for {day.isoformat()}.", "Try again later"
            )
        now = self._clock()
        if day == self._owner.day(self._clock()):  # the next 24 hours
            start = now.astimezone(tz).replace(minute=0, second=0, microsecond=0)
            hours = [h for h in forecast.hours if start <= h.time < start + timedelta(hours=24)]
        else:
            hours = [h for h in forecast.hours if h.time.astimezone(tz).date() == day]
        return {
            "label": label,
            "date": day.isoformat(),
            "day": {
                "min_c": round(found.min_c, 1), "max_c": round(found.max_c, 1),
                "precip_mm": round(found.precip_mm, 1), "precip_prob": found.precip_prob,
                "wind_kmh": round(found.wind_kmh, 1), "summary": rules.summary(found.code, lang),
            },
            "hours": [
                {"time": h.time.astimezone(tz).isoformat(), "temp_c": round(h.temp_c, 1),
                 "precip_prob": h.precip_prob, "summary": rules.summary(h.code, lang)}
                for h in hours
            ],
        }  # fmt: skip

    # --- the tool, the briefing, the planner ---------------------------------------------------------------

    async def for_tool(self, where: str | None, when: str | None, resolve: Any) -> dict[str, Any]:
        """`get_weather`: where is here, a place, an event or task title (resolved by `resolve`), or a
        city."""
        day = rules.parse_when(when, self._owner.day(self._clock()))
        text = (where or "here").strip()
        if text.casefold() in ("here", "qui", "qua", ""):
            coord, label = await self._where("here")
        else:
            coord, label = await self._place_or_city(text, resolve)
        return self._view(await self.forecast(coord), day, label or text, await self._owner.lang(self._pool))

    async def _place_or_city(self, text: str, resolve: Any) -> tuple[Coord, str]:
        if resolve is not None:
            try:
                coord, name, _ = await resolve(text)
                return coord, name
            except PisaError as err:  # not a place Pisa knows: a city, then
                _log.info("weather_place_not_resolved", code=err.code)
        try:
            found = await self._provider.geocode(text, await self._owner.lang(self._pool))
        except WeatherError as err:
            raise PisaError("provider_error", err.message, "Try again in a minute") from None
        if found is None:
            raise PisaError("not_found", f"No place or city called {text[:60]!r}.", "Say it differently")
        return (found[0], found[1]), found[2]

    async def briefing_line(self, lang: str) -> str | None:
        """Today's weather where the user is (the latest location, however old: weather isn't precise)."""
        latest = await self._places.latest_location()
        if latest is None:
            return None
        forecast = await self.forecast(latest[0])
        day = next((d for d in forecast.days if d.date == self._owner.day(self._clock())), None)
        return rules.briefing_line(day, lang) if day else None

    async def rainy_hours(self, coord: Coord, day: date) -> list[datetime]:
        """The hours of `day` with a precipitation probability of 60% or more (the planner's rain rule)."""
        tz = self._owner.zone
        forecast = await self.forecast(coord)
        return [
            t
            for t in rules.rainy_hours(forecast)
            if t.astimezone(tz).date() in (day, day + timedelta(days=1))
        ]
