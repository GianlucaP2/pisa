"""Ports for places, travel times and web research. Each provider adapter implements some of them.

Adapters raise `PlaceProviderError` with the real cause; the service tries the next provider in the user's
order. They never see or decide which provider comes next, the cap or the cache.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from pisa.domain.places import Mode, Period

Coord = tuple[float, float]  # (lat, lon)
# a provider id Pisa made for an address with no id of its own (not a provider's id)
GEOCODE_ID_PREFIX = "geo:"


class PlaceProviderError(Exception):
    """A provider call failed. `status` is the HTTP status when there was one; `quota` marks a 429."""

    def __init__(self, message: str, status: int | None = None, *, quota: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.quota = quota


@dataclass(frozen=True)
class Usage:
    """What a call bills: `units` of `sku` (see pisa.domain.place_prices)."""

    sku: str
    units: int = 1


Meter = Callable[[Usage, bool], Awaitable[None]]
"""(what a call bills, whether it succeeded): adapters report every HTTP call they make, the token exchange
included."""


@dataclass(frozen=True)
class TravelEstimate:
    minutes: int  # without traffic
    traffic_minutes: int | None  # with live or predicted traffic (driving only), else None
    distance_m: int | None

    @property
    def best_minutes(self) -> int:
        return self.traffic_minutes if self.traffic_minutes is not None else self.minutes


@dataclass(frozen=True)
class Review:
    author: str | None
    rating: float | None
    text: str
    published_at: str | None
    source: str
    relative_time: str | None = None  # the provider's own words ("2 settimane fa")
    author_uri: str | None = None  # the author's profile: shown as a link with the name (Google's policy)


@dataclass(frozen=True)
class Photo:
    """A provider photo: its name (to fetch a short-lived URL later) and who took it (shown with it)."""

    name: str
    author: str | None
    author_uri: str | None
    width: int | None = None
    height: int | None = None


@dataclass
class ProviderPlace:
    """One provider's view of a place. Everything after `lon` is optional."""

    provider: str
    provider_id: str
    name: str
    lat: float | None
    lon: float | None
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    weekly: list[Period] = field(default_factory=list)  # the regular week
    current: list[Period] = field(default_factory=list)  # dated periods for the coming days, when known
    closed_dates: list[date] = field(default_factory=list)  # special days with no opening
    hours_note: str | None = None
    open_now: bool | None = None
    rating: float | None = None
    rating_count: int | None = None
    price_level: int | None = None
    summary: str | None = None
    reviews: list[Review] = field(default_factory=list)
    # What the provider says the place offers (Google: outdoor seating, serves coffee…), when it says.
    attributes: dict[str, bool] = field(default_factory=dict)
    photos: list[Photo] = field(default_factory=list)
    # Google's AI summaries, each shown with its disclosure ("Summarized with Gemini") and its report link:
    review_summary: str | None = None  # what the reviews say (Google's reviewSummary)
    review_summary_disclosure: str | None = None
    review_summary_flag_uri: str | None = None
    reviews_uri: str | None = None  # the reviews on Google Maps
    description: str | None = None  # a description of the place (generativeSummary), not of its reviews
    description_disclosure: str | None = None
    description_flag_uri: str | None = None
    # the credit the data's licence asks to be shown with it ("© OpenStreetMap contributors", ODbL), else None
    attribution: str | None = None


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    content: str


@dataclass(frozen=True)
class WebPage:
    url: str
    text: str


class TravelTimes(Protocol):
    id: str

    def can_travel(self, mode: Mode, *, arrive_by: bool) -> bool: ...

    def usage_for_travel(self, mode: Mode, destinations: int) -> Usage: ...

    async def travel(
        self,
        origin: Coord,
        destinations: list[Coord],
        mode: Mode,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> list[TravelEstimate | None]:
        """One estimate per destination (None when there is no route)."""
        ...


class PlaceDetails(Protocol):
    id: str

    def usage_for_details(self, reviews: bool) -> Usage: ...

    async def details(self, provider_id: str, *, reviews: bool, lang: str) -> ProviderPlace: ...

    def usage_for_photo(self) -> Usage: ...

    async def photo_uri(self, name: str, *, max_width: int) -> str:
        """A short-lived URL of the photo (the key never leaves the server)."""
        ...

    async def find_id(self, query: str, *, near: Coord | None, lang: str) -> tuple[str, Coord | None] | None:
        """The provider's id and position for a place known by name and position (the caller checks the
        position is the same spot before linking the id)."""
        ...
