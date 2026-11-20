"""Apple Maps Server API: travel times and place search.

- Auth: an ES256 JWT signed with the Maps key (header kid = key id; claims iss = team id, iat, exp,
  scope "server_api") is exchanged at GET /v1/token for an access token valid `expiresInSeconds` (30 min). The
  access token is cached and renewed a minute early; a 401 renews it once.
- /v1/etas: up to 10 destinations a call; transportType Automobile, Transit, Walking, Cycling; departureDate
  or arrivalDate in UTC. expectedTravelTimeSeconds includes traffic, staticTravelTimeSeconds doesn't.
- /v1/search: name, coordinate, address and poiCategory only (no hours, phone, website or reviews).
- /v1/reverseGeocode: the address at a point (where_am_i).
- 25,000 calls a day shared with MapKit JS; a 429 makes this adapter refuse calls until Retry-After (else
  60 s).
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt

from pisa.connectors.door import guard
from pisa.domain.places import Mode, category_key, distance_m
from pisa.failures import external_failure
from pisa.ports.places import (
    GEOCODE_ID_PREFIX,
    Coord,
    Meter,
    PlaceProviderError,
    ProviderPlace,
    TravelEstimate,
    Usage,
)

BASE = "https://maps-api.apple.com"
JWT_TTL_S = 30 * 60
RENEW_MARGIN_S = 60
BACKOFF_S = 60.0
MAX_DESTINATIONS = 10
TRANSPORT: dict[str, str] = {
    "drive": "Automobile",
    "transit": "Transit",
    "walk": "Walking",
    "bike": "Cycling",
}


@dataclass(frozen=True)
class MapsKey:
    key_id: str
    team_id: str
    private_key_pem: str = field(repr=False)  # the .p8 content (secret)


class AppleMaps:
    id = "apple"
    can_filter_open_now = False  # Apple's places have no opening hours
    can_search_rich = False  # nor ratings, prices or attributes: no richer search

    def __init__(
        self,
        http: httpx.AsyncClient,
        key: MapsKey,
        meter: Meter,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._key = key
        self._meter = meter
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self._backoff_until = 0.0

    # --- auth --------------------------------------------------------------------------------------------

    def maps_jwt(self) -> str:
        now = int(self._clock())
        return jwt.encode(
            {"iss": self._key.team_id, "iat": now, "exp": now + JWT_TTL_S, "scope": "server_api"},
            self._key.private_key_pem,
            algorithm="ES256",
            headers={"kid": self._key.key_id, "typ": "JWT"},
        )

    async def access_token(self, *, renew: bool = False) -> str:
        if not renew and self._token is not None and self._clock() < self._token_expires - RENEW_MARGIN_S:
            return self._token
        response = await self._send("/v1/token", None, f"Bearer {self.maps_jwt()}", "apple.token")
        body = _json(response)
        token, ttl = body.get("accessToken"), body.get("expiresInSeconds")
        if not isinstance(token, str) or not isinstance(ttl, int):
            raise PlaceProviderError("Apple Maps returned a token response without accessToken.")
        self._token, self._token_expires = token, self._clock() + ttl
        return token

    async def test(self) -> str:
        await self.access_token(renew=True)
        return "Apple Maps accepted the key: the token exchange worked."

    async def _get(self, path: str, params: dict[str, str], sku: str) -> dict[str, Any]:
        response = await self._send(path, params, f"Bearer {await self.access_token()}", sku)
        if response.status_code == 401:  # the access token expired early or was revoked: renew once
            renewed = await self.access_token(renew=True)
            response = await self._send(path, params, f"Bearer {renewed}", sku)
            if response.status_code == 401:  # a fresh token refused too: an error, never an empty answer
                raise PlaceProviderError(
                    f"Apple Maps refused a fresh access token (HTTP 401{_detail(response)}).", 401
                )
        return _json(response)

    async def _send(self, path: str, params: dict[str, str] | None, auth: str, sku: str) -> httpx.Response:
        try:
            return await self._send_once(path, params, auth, sku)
        except PlaceProviderError as err:  # with Apple's own reason
            external_failure("apple_maps", f"GET {path}", err.status, err)
            raise

    async def _send_once(
        self, path: str, params: dict[str, str] | None, auth: str, sku: str
    ) -> httpx.Response:
        if self._clock() < self._backoff_until:
            raise PlaceProviderError(
                "Apple Maps is backing off after a 429 (the daily quota of 25,000 calls).", 429, quota=True
            )
        try:
            response = await self._http.get(
                BASE + path, params=params, headers={"Authorization": auth}, timeout=10
            )
        except httpx.HTTPError as exc:
            await self._meter(Usage(sku), False)
            raise PlaceProviderError(f"Apple Maps is unreachable ({exc.__class__.__name__}).") from None
        ok = response.status_code < 400
        await self._meter(Usage(sku), ok)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "")
            self._backoff_until = self._clock() + (float(retry) if retry.isdigit() else BACKOFF_S)
            raise PlaceProviderError(
                "Apple Maps refused the call: the daily quota of 25,000 calls is used up (HTTP 429).",
                429,
                quota=True,
            )
        if response.status_code == 401 and path == "/v1/token":
            raise PlaceProviderError(
                f"Apple Maps rejected the Maps key (HTTP 401{_detail(response)}): "
                "check the key id and the .p8.",
                401,
            )
        if response.status_code >= 400 and response.status_code != 401:
            raise PlaceProviderError(
                f"Apple Maps refused the call (HTTP {response.status_code}{_detail(response)}).",
                response.status_code,
            )
        return response

    # --- travel times ------------------------------------------------------------------------------------

    def can_travel(self, mode: Mode, *, arrive_by: bool) -> bool:
        return mode in TRANSPORT

    def usage_for_travel(self, mode: Mode, destinations: int) -> Usage:
        return Usage("apple.etas", -(-destinations // MAX_DESTINATIONS))

    async def travel(
        self,
        origin: Coord,
        destinations: list[Coord],
        mode: Mode,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> list[TravelEstimate | None]:
        out: list[TravelEstimate | None] = []
        for i in range(0, len(destinations), MAX_DESTINATIONS):
            chunk = destinations[i : i + MAX_DESTINATIONS]
            params = {
                "origin": _ll(origin),
                "destinations": "|".join(_ll(d) for d in chunk),
                "transportType": TRANSPORT[mode],
            }
            if depart_at is not None:
                params["departureDate"] = _utc(depart_at)
            if arrive_by is not None:
                params["arrivalDate"] = _utc(arrive_by)
            etas = (await self._get("/v1/etas", params, "apple.etas")).get("etas") or []
            if len(etas) == len(chunk):  # one answer per destination, in the order asked
                out.extend(_estimate(eta, mode) for eta in etas)
            else:  # some destinations have no route: pair the answers by their echoed coordinate
                out.extend(_estimate(_by_coordinate(dest, etas), mode) for dest in chunk)
        return out

    # --- search -----------------------------------------------------------------------------------------

    def usage_for_search(self, rich: bool = False) -> Usage:  # `rich` is never asked (can_search_rich)
        return Usage("apple.search")

    async def search(
        self, query: str, *, near: Coord | None, open_now: bool, limit: int, lang: str, rich: bool = False
    ) -> list[ProviderPlace]:
        """`open_now` and `rich` are never asked of Apple (`can_filter_open_now`, `can_search_rich`)."""
        guard("a map search", query, provider="apple")  # RAIL: their values never leave
        params = {"q": query, "lang": lang}
        if near is not None:
            params["searchLocation"] = params["userLocation"] = _ll(near)
        body = await self._get("/v1/search", params, "apple.search")
        return [p for p in (_place(r) for r in body.get("results") or []) if p is not None][:limit]

    async def reverse_geocode(self, coord: Coord, *, lang: str) -> ProviderPlace | None:
        """The address at a point (where the user is)."""
        body = await self._get("/v1/reverseGeocode", {"loc": _ll(coord), "lang": lang}, "apple.geocode")
        results = body.get("results") or []
        return _place({"id": "geocode", **results[0]}) if results else None

    async def geocode(self, text: str, *, near: Coord | None, lang: str) -> list[ProviderPlace]:
        """Street addresses for a text ("Keizersgracht 1, Amsterdam"): the fallback when a place search
        finds no single match. A result without a street (a whole city or region) is too vague to go to."""
        guard("a map search", text, provider="apple")  # RAIL: never their values
        params = {"q": text, "lang": lang}
        if near is not None:
            params["searchLocation"] = _ll(near)
        body = await self._get("/v1/geocode", params, "apple.geocode")
        out: list[ProviderPlace] = []
        for result in body.get("results") or []:
            street = result.get("structuredAddress") or {}
            if not (street.get("thoroughfare") or street.get("fullThoroughfare")):
                continue
            lines = result.get("formattedAddressLines") or []
            coordinate = result.get("coordinate") or {}
            point = f"{coordinate.get('latitude')},{coordinate.get('longitude')}"
            own_id = result.get("id") or GEOCODE_ID_PREFIX + point
            name = result.get("name") or (lines[0] if lines else None)
            if (place := _place({**result, "id": own_id, "name": name})) is not None:
                out.append(place)
        return out


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise PlaceProviderError("Apple Maps returned a response that is not JSON.") from None
    if not isinstance(body, dict):
        raise PlaceProviderError("Apple Maps returned an unexpected response.")
    return body


def _detail(response: httpx.Response) -> str:
    try:
        message = response.json().get("error", {}).get("message") or response.json().get("message")
    except (ValueError, AttributeError):
        return ""
    return f": {message}" if isinstance(message, str) and message else ""


def _ll(c: Coord) -> str:
    return f"{c[0]:.6f},{c[1]:.6f}"


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _by_coordinate(dest: Coord, etas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The answer whose echoed destination is (practically) this destination."""
    for eta in etas:
        d = eta.get("destination") or {}
        lat, lon = d.get("latitude"), d.get("longitude")
        if (
            isinstance(lat, int | float)
            and isinstance(lon, int | float)
            and distance_m(dest, (lat, lon)) < 25
        ):
            return eta
    return None


def _estimate(best: dict[str, Any] | None, mode: Mode) -> TravelEstimate | None:
    if best is None:
        return None
    expected, static = best.get("expectedTravelTimeSeconds"), best.get("staticTravelTimeSeconds")
    if not isinstance(expected, int | float):
        return None
    base = static if isinstance(static, int | float) else expected
    traffic = math.ceil(expected / 60) if mode == "drive" else None  # minutes rounded up: never late
    distance = best.get("distanceMeters")
    return TravelEstimate(
        math.ceil(base / 60), traffic, int(distance) if isinstance(distance, int | float) else None
    )


def _place(result: dict[str, Any]) -> ProviderPlace | None:
    coordinate = result.get("coordinate") or {}
    if not result.get("id") or not result.get("name"):
        return None
    lines = result.get("formattedAddressLines") or []
    return ProviderPlace(
        provider="apple",
        provider_id=str(result["id"]),
        name=str(result["name"]),
        lat=coordinate.get("latitude"),
        lon=coordinate.get("longitude"),
        category=category_key(result.get("poiCategory")),
        address=", ".join(str(line) for line in lines) or None,
    )
