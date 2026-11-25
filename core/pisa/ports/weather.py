"""Port for the weather (Open-Meteo, no key). The adapter lives in pisa.connectors."""

from typing import Protocol

from pisa.domain.weather import Forecast


class WeatherError(Exception):
    """The provider failed: the message names the cause."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class WeatherProvider(Protocol):
    async def forecast(self, lat: float, lon: float, timezone: str) -> Forecast:
        """Today and the next 7 days, daily and hourly, in `timezone`. Raises WeatherError."""
        ...

    async def geocode(self, name: str, lang: str) -> tuple[float, float, str] | None:
        """A city by name: (lat, lon, its name), or None when there is none. Raises WeatherError."""
        ...
