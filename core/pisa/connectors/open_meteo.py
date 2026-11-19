"""Open-Meteo: the forecast and city names, free and without a key (attribution: Open-Meteo.com,
CC BY 4.0). One forecast call gives today and the next 7 days, daily and hourly."""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from pisa.domain.weather import DAYS_AHEAD, Day, Forecast, Hour
from pisa.failures import external_failure
from pisa.ports.weather import WeatherError

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT = httpx.Timeout(8.0, connect=4.0)


class OpenMeteo:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _get(self, url: str, params: dict[str, Any], what: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, params=params, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            external_failure("open_meteo", what, type(exc).__name__, exc)
            raise WeatherError(f"Open-Meteo is unreachable ({exc.__class__.__name__}).") from None
        if response.status_code != 200:
            external_failure("open_meteo", what, response.status_code, response.text)
            raise WeatherError(f"Open-Meteo's {what} failed (HTTP {response.status_code}).")
        return response.json()

    async def forecast(self, lat: float, lon: float, timezone: str) -> Forecast:
        body = await self._get(
            FORECAST_URL,
            {
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "daily": "weather_code,temperature_2m_min,temperature_2m_max,precipitation_sum,"
                "precipitation_probability_max,wind_speed_10m_max",
                "timezone": timezone,  # the days are the user's local days
                "timeformat": "unixtime",  # instants: the hour repeated when DST ends stays two
                "forecast_days": DAYS_AHEAD + 1,
            },
            "forecast",
        )
        tz = ZoneInfo(timezone)
        try:
            d, h = body["daily"], body["hourly"]
            days = [
                Day(datetime.fromtimestamp(t, tz).date(), d["temperature_2m_min"][i],
                    d["temperature_2m_max"][i], d["precipitation_sum"][i] or 0.0,
                    d["precipitation_probability_max"][i] or 0, d["wind_speed_10m_max"][i] or 0.0,
                    d["weather_code"][i] or 0)
                for i, t in enumerate(d["time"])
                if d["temperature_2m_min"][i] is not None
            ]  # fmt: skip
            hours = [
                Hour(datetime.fromtimestamp(t, tz), h["temperature_2m"][i],
                     h["precipitation_probability"][i] or 0, h["weather_code"][i] or 0)
                for i, t in enumerate(h["time"])
                if h["temperature_2m"][i] is not None
            ]  # fmt: skip
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise WeatherError(
                f"Open-Meteo's forecast was not understood ({exc.__class__.__name__})."
            ) from None
        return Forecast(days, hours)

    async def observed_rain(self, lat: float, lon: float, day: date, timezone: str) -> tuple[float, str]:
        """A past day's precipitation (mm) at a point, and the public URL it was
        read from (the same request, linkable). Raises WeatherError."""
        params = {
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "daily": "precipitation_sum",
            "start_date": day.isoformat(), "end_date": day.isoformat(), "timezone": timezone,
        }  # fmt: skip
        body = await self._get(FORECAST_URL, params, "past day")
        try:
            mm = float(body["daily"]["precipitation_sum"][0] or 0.0)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise WeatherError(
                f"Open-Meteo's past day was not understood ({exc.__class__.__name__})."
            ) from None
        return mm, str(httpx.URL(FORECAST_URL, params=params))

    async def geocode(self, name: str, lang: str) -> tuple[float, float, str] | None:
        body = await self._get(
            GEOCODE_URL, {"name": name, "count": 1, "language": lang[:2], "format": "json"}, "city search"
        )
        results = body.get("results") or []
        if not results:
            return None
        first = results[0]
        return float(first["latitude"]), float(first["longitude"]), str(first.get("name") or name)
