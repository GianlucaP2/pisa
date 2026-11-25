"""The phone's own maps as Pisa's first places provider (contract 2.3, `pisa.maps`, the feature `maps.phone`).

**The delegate** (`PhoneMaps`): one per live session whose welcome granted `pisa.maps`. A request goes in the
turn's run as `pisa.maps.search` or `pisa.maps.eta` with a fresh `requestId` (a UUID), and waits for the
phone's `pisa.maps.result` for that id at most `WAIT_S` (4 s). The answer is matched by its `requestId` in
this session's own table, which only this device's socket reads (`DeviceSession.maps_result`): an answer for
an id this session never asked, one already answered or past its wait, or one from another device is dropped
(logged by kind, never by content). Not ok (`denied`, `unavailable`, `no_results`, `timeout`,
`unsupported`), no answer in time, or a socket that is gone: the provider choice goes on to the next provider
(Google with the user's key, then OpenStreetMap; `services.place_providers.choose`).

**The turn's delegate** (`current`): a ContextVar the conversation sets for the turn it answers
(`ConversationService.respond`), so the places service asks the phone only within a turn on an app that
granted it; a turn over HTTP (`POST /v2/runs`) or the night's work has none, and the phone is skipped.

**The adapter** (`PhoneMapsAdapter`): the places port over the delegate (search and travel times), what
`choose` calls like any provider's. Never billed (no SKU of it is priced). The phone's answer is untrusted:
checked whole against the schema (`domain.phone_maps.parse_result`), at most `limit` places kept, its text
reaching the model only inside the map results' data block, like any provider's. Nothing it says (a name, an
address, a coordinate, the query) is ever logged.
"""

import asyncio
import math
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from pisa.domain import phone_maps as rules
from pisa.domain.places import Mode
from pisa.logging import get_logger
from pisa.ports.places import Coord, PlaceProviderError, ProviderPlace, TravelEstimate, Usage

PROVIDER = "phone"  # its id in a choice (`Choice.provider`, the skipped list) and on its places
SEARCH_SKU = "phone.search"  # metered nowhere, priced never: the phone's own maps
ETA_SKU = "phone.eta"
DEPART_SOON = timedelta(
    minutes=10
)  # an ETA is "leaving now": a later departure is asked of the next provider
_log = get_logger("pisa.places.phone")

Send = Callable[[str, dict[str, Any]], bool]
"""(event name, value) → whether it went: `pisa.maps.search` or `pisa.maps.eta` in the turn's run."""

current: ContextVar["PhoneMaps | None"] = ContextVar("phone_maps", default=None)


@contextmanager
def using(delegate: "PhoneMaps | None") -> Iterator[None]:
    """The turn's delegate while the block runs (None: no phone for this turn)."""
    token = current.set(delegate)
    try:
        yield
    finally:
        current.reset(token)


class PhoneMaps:
    """One live session's requests to its phone's maps, each waiting for its own answer."""

    def __init__(self, device_id: str, send: Send, *, wait_s: float = rules.WAIT_S) -> None:
        self.device_id = device_id.lower()
        self._send = send
        self._wait_s = wait_s
        self._pending: dict[
            str, tuple[str, asyncio.Future[rules.MapsResult]]
        ] = {}  # requestId → (asked, answer)

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def ask(self, name: str, value: dict[str, Any]) -> rules.MapsResult | None:
        """Send one request and wait for its answer: the answer, or None (not sent, or none within WAIT_S)."""
        request_id = str(value["requestId"]).lower()
        answer: asyncio.Future[rules.MapsResult] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (name, answer)
        try:
            if not self._send(name, value):
                _log.info("phone_maps_unsent", device_id=self.device_id, asked=name)
                return None
            try:
                return await asyncio.wait_for(asyncio.shield(answer), self._wait_s)
            except TimeoutError:
                _log.info("phone_maps_timeout", device_id=self.device_id, asked=name, wait_s=self._wait_s)
                return None
        finally:
            self._pending.pop(request_id, None)

    def answer(self, result: rules.MapsResult, device_id: str) -> bool:
        """The phone's `pisa.maps.result` (already checked): True when it answered a request of this session's
        still waiting. One for another device, an unknown or expired id, or a second answer is dropped."""
        if device_id.lower() != self.device_id:
            _log.warning("phone_maps_result_other_device", device_id=device_id)
            return False
        waiting = self._pending.get(result.request_id)
        if waiting is None or waiting[1].done():
            _log.info("phone_maps_result_unknown", device_id=self.device_id)
            return False
        asked, answer = waiting
        if result.ok and (result.eta is not None) != (
            asked == rules.ETA
        ):  # places for an ETA, or the reverse
            _log.info("phone_maps_result_mismatched", device_id=self.device_id, asked=asked)
            result = rules.MapsResult(result.request_id, False, reason="unsupported")
        _log.info(
            "phone_maps_result", device_id=self.device_id, asked=asked, ok=result.ok, reason=result.reason,
            places=len(result.places),
        )  # fmt: skip
        answer.set_result(result)
        return True

    async def search(self, query: str, near: Coord | None, limit: int) -> list[rules.MapsPlace] | str:
        """Places for a query near a point (None: wherever the phone's maps search), at most `limit`; or why
        not (a `reason`, "timeout" when no answer came)."""
        value = rules.search_value(str(uuid.uuid4()), query, near, limit)
        if value is None:
            return "unsupported"
        got = await self.ask(rules.SEARCH, value)
        if got is None:
            return "timeout"
        if not got.ok:
            return got.reason or "unavailable"
        return list(got.places[: value["limit"]])

    async def eta(self, origin: Coord, destination: Coord, mode: str) -> rules.MapsEta | str:
        """A travel time between two points by `mode`; or why not."""
        got = await self.ask(rules.ETA, rules.eta_value(str(uuid.uuid4()), origin, destination, mode))
        if got is None:
            return "timeout"
        if not got.ok or got.eta is None:
            return got.reason or "unavailable"
        if got.eta.mode != mode:  # an answer for another mode isn't this one's time
            return "unsupported"
        return got.eta


def provider_place(found: rules.MapsPlace) -> ProviderPlace:
    """The phone's place as a provider's: its id stays the phone's (never stored: `places` matches it by name
    and position), `other` is no category."""
    return ProviderPlace(
        provider=PROVIDER,
        provider_id=found.id,
        name=found.name,
        lat=found.lat,
        lon=found.lon,
        category=found.kind if found.kind not in (None, "other") else None,
        address=found.address,
        phone=found.phone,
        website=found.url,
        open_now=found.open_now,
    )


def failed(reason: str) -> PlaceProviderError:
    """Why the phone gave nothing: a reason code only (the next provider is asked)."""
    return PlaceProviderError(f"the phone's maps: {reason}")


class PhoneMapsAdapter:
    """The places port over the phone's maps: search and travel times (walk, transit, drive, bike; leaving
    now). No key, no bill."""

    id = PROVIDER
    can_filter_open_now = True  # the phone says openNow: only the places it says are open are kept
    can_search_rich = False

    def __init__(self, maps: PhoneMaps, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._maps = maps
        self._clock = clock

    def usage_for_search(self, rich: bool = False) -> Usage:
        return Usage(SEARCH_SKU, 0)

    async def search(
        self, query: str, *, near: Coord | None, open_now: bool, limit: int, lang: str, rich: bool = False
    ) -> list[ProviderPlace]:
        """`no_results` is an empty answer (the next provider is asked); any other reason fails this one."""
        got = await self._maps.search(query, near, max(1, min(limit, rules.PLACES_MAX)))
        if got == "no_results":
            return []
        if isinstance(got, str):
            raise failed(got)
        places = [provider_place(p) for p in got]
        return [p for p in places if p.open_now is True] if open_now else places

    def can_travel(self, mode: Mode, *, arrive_by: bool) -> bool:
        return mode in rules.MODES and not arrive_by

    def usage_for_travel(self, mode: Mode, destinations: int) -> Usage:
        return Usage(ETA_SKU, 0)

    async def travel(
        self,
        origin: Coord,
        destinations: list[Coord],
        mode: Mode,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> list[TravelEstimate | None]:
        """One `pisa.maps.eta` per destination, together (at most ETA_MAX); leaving now only."""
        if (
            arrive_by is not None
            or not 1 <= len(destinations) <= rules.ETA_MAX
            or (depart_at is not None and depart_at - self._clock() > DEPART_SOON)
        ):
            raise failed("unsupported")
        got = await asyncio.gather(*(self._maps.eta(origin, d, mode) for d in destinations))
        if all(isinstance(g, str) for g in got):
            raise failed(str(got[0]))
        return [
            None if isinstance(g, str) else TravelEstimate(math.ceil(g.seconds / 60), None, g.distance_m)
            for g in got
        ]  # minutes rounded up: never late
