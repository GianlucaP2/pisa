"""Open-Meteo for the tests: its HTTP API (forecast and city search) and the same forecast in memory.

The forecast starts on the clock's day (Europe/Rome) and covers 8 days. Rain is likely (80%) from 13:00 to
15:59 on the first day; the other hours are partly cloudy (10%). The first day: light rain, 9 to 13 °C."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from pisa.connectors.open_meteo import OpenMeteo

TZ = ZoneInfo("Europe/Rome")
RAINY = range(13, 16)
CITIES = {"amsterdam": (52.389, 4.869, "Amsterdam"), "roma": (41.826, 12.432, "Roma")}


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=TZ)


def forecast_json(first: date) -> dict[str, Any]:
    """With timeformat=unixtime, as Pisa asks: the days are local midnights, the hours instants (a DST day has
    23 or 25 of them)."""
    days = [first + timedelta(days=i) for i in range(8)]
    start, end = _midnight(days[0]).astimezone(UTC), _midnight(days[-1] + timedelta(days=1)).astimezone(UTC)
    hours = [
        (start + timedelta(hours=n)).astimezone(TZ) for n in range(int((end - start).total_seconds() // 3600))
    ]
    rainy = [t.date() == first and t.hour in RAINY for t in hours]
    return {
        "daily": {
            "time": [int(_midnight(d).timestamp()) for d in days],
            "weather_code": [61] + [2] * 7,
            "temperature_2m_min": [9.2] + [11.0] * 7,
            "temperature_2m_max": [12.6] + [16.0] * 7,
            "precipitation_sum": [4.2] + [0.0] * 7,
            "precipitation_probability_max": [80] + [10] * 7,
            "wind_speed_10m_max": [25.0] + [12.0] * 7,
        },
        "hourly": {
            "time": [int(t.timestamp()) for t in hours],
            "temperature_2m": [11.0 if r else 12.0 for r in rainy],
            "precipitation_probability": [80 if r else 10 for r in rainy],
            "weather_code": [53 if r else 2 for r in rainy],
        },
    }


class FakeMeteoAPI:
    """Open-Meteo over HTTP (an httpx handler). `requests` counts the forecast calls."""

    def __init__(self, first: date) -> None:
        self.first = first
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.open-meteo.com":
            assert request.url.params["timezone"] == "Europe/Rome"
            assert request.url.params["timeformat"] == "unixtime"
            return httpx.Response(200, content=json.dumps(forecast_json(self.first)))
        if request.url.host == "geocoding-api.open-meteo.com":
            found = CITIES.get(request.url.params["name"].casefold())
            results = [{"name": found[2], "latitude": found[0], "longitude": found[1]}] if found else []
            return httpx.Response(200, json={"results": results} if results else {})
        raise httpx.ConnectError("network disabled in tests", request=request)


def fake_meteo(first: date) -> OpenMeteo:
    """The real adapter on top of the fake API (no network)."""
    return OpenMeteo(httpx.AsyncClient(transport=httpx.MockTransport(FakeMeteoAPI(first))))
