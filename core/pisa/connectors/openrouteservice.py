"""OpenRouteService: travel times on OpenStreetMap's roads, with the user's own free API key (sealed in
`place_providers` as provider `openrouteservice`, like the Google and Tavily keys).

- `/v2/matrix/{profile}` (POST): one call for an origin and up to `MAX_DESTINATIONS` destinations; the key
  goes in the Authorization header, never in the URL. Profiles: walk → foot-walking, bike → cycling-regular,
  drive → driving-car. ORS has no public transport and no arrival or departure time: transit, and any
  arrive-by, is a request it can't do (`can_travel` False, so the provider choice says `unsupported`).
- Travel needs the real points (rounded to 5 decimals, ~1 m): that is what a route is between.
- No key: there is no adapter (the provider is "not configured"; `not_configured` is the error when one is
  asked anyway), never a failure. A 429 makes this adapter refuse calls until Retry-After (else 60 s).
"""

import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from pisa import __version__
from pisa.domain.errors import PisaError
from pisa.domain.places import Mode
from pisa.failures import external_failure
from pisa.ports.places import Coord, Meter, PlaceProviderError, TravelEstimate, Usage

ORS_URL = "https://api.openrouteservice.org"
PROFILE: dict[str, str] = {"walk": "foot-walking", "bike": "cycling-regular", "drive": "driving-car"}
MAX_DESTINATIONS = 25
TIMEOUT = httpx.Timeout(10.0, connect=4.0)
BACKOFF_S = 60.0
BODY_MAX = 512 * 1024
SKU = "ors.matrix"


def not_configured() -> PisaError:
    return PisaError(
        "place_provider_not_configured",
        "OpenRouteService has no key yet, so it gives no travel times.",
        "Add your free OpenRouteService key in Settings → Places",
        detail="provider_not_configured",
    )


class OpenRouteService:
    id = "openrouteservice"

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        meter: Meter,
        *,
        base_url: str = ORS_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._key = api_key.strip()
        self._meter = meter
        self._base = base_url.rstrip("/")
        self._clock = clock
        self._backoff_until = -math.inf

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def test(self) -> str:
        """The cheapest real call: a two-point walking matrix."""
        await self.travel((52.37, 4.89), [(52.371, 4.891)], "walk")
        return "OpenRouteService accepted the key: a travel-time matrix worked."

    def can_travel(self, mode: Mode, *, arrive_by: bool) -> bool:
        return self.configured and mode in PROFILE and not arrive_by

    def usage_for_travel(self, mode: Mode, destinations: int) -> Usage:
        return Usage(SKU, -(-destinations // MAX_DESTINATIONS))

    async def travel(
        self,
        origin: Coord,
        destinations: list[Coord],
        mode: Mode,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> list[TravelEstimate | None]:
        """One estimate per destination (None where ORS finds no route). `depart_at` is ignored (ORS has no
        timetable or traffic); `arrive_by` and transit are refused before any call."""
        if not self.configured:
            raise not_configured()
        if mode not in PROFILE or arrive_by is not None:
            raise PisaError(
                "validation_failed",
                "OpenRouteService has no public transport and no arrive-by times.",
                "Ask another provider for this",
                detail="unsupported",
            )
        out: list[TravelEstimate | None] = []
        for i in range(0, len(destinations), MAX_DESTINATIONS):
            chunk = destinations[i : i + MAX_DESTINATIONS]
            body = {
                "locations": [_lonlat(origin), *(_lonlat(d) for d in chunk)],
                "sources": [0],
                "destinations": list(range(1, len(chunk) + 1)),
                "metrics": ["duration", "distance"],
            }
            data = await self._call(f"/v2/matrix/{PROFILE[mode]}", body)
            out.extend(_estimates(data, len(chunk)))
        return out

    async def _call(self, path: str, body: dict[str, Any]) -> Any:
        try:
            return await self._call_once(path, body)
        except PlaceProviderError as err:  # status and kind only, never ORS's words (nor the key)
            external_failure("openrouteservice", f"POST {path}", err.status, err)
            raise

    async def _call_once(self, path: str, body: dict[str, Any]) -> Any:
        if self._clock() < self._backoff_until:
            raise PlaceProviderError(
                "OpenRouteService is backing off after a 429 (the key's quota).", 429, quota=True
            )
        headers = {
            "Authorization": self._key,
            "User-Agent": f"PISA/{__version__}",
            "Accept": "application/json",
        }
        try:
            response = await self._http.post(
                self._base + path, json=body, headers=headers, timeout=TIMEOUT, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            await self._meter(Usage(SKU), False)
            raise PlaceProviderError(f"OpenRouteService is unreachable ({exc.__class__.__name__}).") from None
        status = response.status_code
        await self._meter(Usage(SKU), status < 300)
        if status == 429:
            retry = response.headers.get("Retry-After", "")
            self._backoff_until = self._clock() + (float(retry) if retry.isdigit() else BACKOFF_S)
            raise PlaceProviderError(
                "OpenRouteService refused the call: the key's quota is used up (HTTP 429).", 429, quota=True
            )
        if status in (401, 403):
            raise PlaceProviderError(f"OpenRouteService rejected the key (HTTP {status}).", status)
        if status >= 300:  # a redirect is never followed: it could lead to another host
            raise PlaceProviderError(f"OpenRouteService refused the call (HTTP {status}).", status)
        if len(response.content) > BODY_MAX:
            raise PlaceProviderError("OpenRouteService sent an answer too large to read.")
        try:
            return response.json()
        except ValueError:
            raise PlaceProviderError("OpenRouteService returned a response that is not JSON.") from None


def _lonlat(c: Coord) -> list[float]:
    return [round(c[1], 5), round(c[0], 5)]  # ORS: [lon, lat]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _estimates(data: Any, count: int) -> list[TravelEstimate | None]:
    """The matrix's one row (the origin's), one estimate per destination; a null is no route."""
    out: list[TravelEstimate | None] = [None] * count
    if not isinstance(data, dict):
        return out
    durations, distances = data.get("durations"), data.get("distances")
    row = durations[0] if isinstance(durations, list) and durations and isinstance(durations[0], list) else []
    far = distances[0] if isinstance(distances, list) and distances and isinstance(distances[0], list) else []
    for i in range(min(count, len(row))):
        seconds = _number(row[i])
        if seconds is None:
            continue
        metres = _number(far[i]) if i < len(far) else None
        out[i] = TravelEstimate(  # minutes rounded up: never late
            math.ceil(seconds / 60), None, round(metres) if metres is not None else None
        )
    return out
