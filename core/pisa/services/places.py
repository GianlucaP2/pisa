"""Places: one Pisa identity per place, the place card, travel times, web research, and the
resolution of event and task locations to places.

- **Identity**: `places` links the provider ids. A provider result is the same place as a row with its
  provider id, or with the same name less than 50 m away; otherwise it is a new place.
- **Provider terms**: Google content (hours, phone, reviews…) lives only in `place_cache`, 24 h; Google
  coordinates are kept 30 days (`coords_at`, the prune clears them; Apple's are preferred when known).
- **The card** merges the facts, live hours ("open until 15:00"), the travel time from the latest location,
  reviews, Pisa's research with its sources, and the events and tasks that go there. A missing piece never
  fails the card: it is null, and the skipped providers are named in `sources`.
- **Resolution** is a search, never a model guess: a location text becomes a place only when exactly one
  result lies within 50 km of the latest location (anywhere when none is known: the provider's best match).
"""

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, LiteralString

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain import place_research as brain
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.places import (
    CITY_LANGS,
    OSM_ATTRIBUTION,
    SAME_PLACE_M,
    Mode,
    Period,
    city_names_key,
    distance_m,
    map_links,
    matches_text,
    names_city,
    open_state,
    text_key,
)
from pisa.logging import get_logger
from pisa.ports.places import (
    GEOCODE_ID_PREFIX,
    Coord,
    Photo,
    PlaceProviderError,
    PlaceResults,
    ProviderPlace,
    Review,
    TravelEstimate,
    Usage,
)
from pisa.services import phone_maps, stays
from pisa.services.phone_maps import PROVIDER as PHONE
from pisa.services.place_providers import JOB_PHRASE, PlaceProviderService
from pisa.services.place_providers import PROVIDERS as PLACE_PROVIDERS

LOCATION_FRESH = timedelta(minutes=15)  # "here" means a location at most this old
NEAR_M = 5_000  # "near here": the close ones first
NEAR_ENOUGH = 3  # fewer than this within NEAR_M: widened once
NEAR_MAX_M = 25_000  # "near" is never farther than this, unless they named a distance
CACHE_TTL = timedelta(hours=24)  # provider content (terms)
GOOGLE_COORDS_TTL = timedelta(days=30)  # Google coordinates (terms)
INSIGHTS_TTL = timedelta(days=30)
RESOLVE_RADIUS_M = 50_000
RESOLVE_RETRY = timedelta(days=1)  # a text without one clear match is searched again after this
RESOLVE_PER_RUN = 20  # location texts resolved per sync round, so a first sync can't burst the quotas
RESEARCH_RESULTS = 6
RESEARCH_PAGES = 4
RELATED_PAST = timedelta(days=7)
LINK_MAX_M = 200  # a details provider's match farther than this from the place is another place
NO_MATCH_RETRY = timedelta(days=7)
RESOLUTION_TTL = timedelta(days=30)  # a resolved location text is searched again after this
GOOGLE_LABEL_TTL = timedelta(days=30)  # a Google-only place nothing refers to is kept this long (terms)
# every table of theirs that points at a place (ON DELETE SET NULL): the prune's NOT EXISTS names each; a
# test checks the list against the database (each has its place_id index, 0091). Episode_links
# (`place`, id) and profile_claims (`place:<id>`) point at one by text, and keep it too.
PLACE_REFERRERS = (
    "calendar_events", "tasks", "stays", "episodes", "place_notes", "plan_adjustments",
    "recommendation_outcomes", "pisa_promises", "done_by_presence",
)  # fmt: skip
REVIEWS_SHOWN = 5  # the detail sheet's excerpts
REVIEW_MAX = 300
GOOGLE_AI_DISCLOSURE = "Summarized with Gemini"  # when Google's answer carries no disclosure text
PHOTO_URL_LIFE = timedelta(minutes=50)
# the rich search already carries each place's photo names; Pisa keeps them a short while
# (Google's names are references, not ids to store) and serves the photos from them, so a card's first photo
# costs one Place Photo call and no details call. Details only when the search's names are gone.
PHOTO_REFS_LIFE = timedelta(hours=1)
PHOTO_MISS_LIFE = timedelta(minutes=10)  # a details call that gave no photos isn't tried again before this
PHOTO_WIDTHS = (400, 800, 1600)  # the widths Pisa asks Google for: the app's request snaps up to one of them
PHOTO_URLS_MAX = 500  # Google's photo URLs are short-lived: Pisa answers a little under that
NEARBY_MAX = 20  # GET /v1/places/search without q
NEARBY_DEG = 0.05  # ~5 km: the nearest known places are looked for this close


def nearby_box(lat: float, lon: float) -> tuple[tuple[float, float], list[tuple[float, float]]]:
    """The ±NEARBY_DEG box around a point: latitude clamped to the poles, longitude split in two where it
    crosses ±180°."""
    lats = (max(-90.0, lat - NEARBY_DEG), min(90.0, lat + NEARBY_DEG))
    lo, hi = lon - NEARBY_DEG, lon + NEARBY_DEG
    if lo < -180.0:
        return lats, [(-180.0, hi), (lo + 360.0, 180.0)]
    if hi > 180.0:
        return lats, [(lo, 180.0), (-180.0, hi - 360.0)]
    return lats, [(lo, hi)]


_log = get_logger("pisa.places")

From = Literal["here"] | Coord


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Fix the request and try again")


def parse_coord(text: str, what: str) -> Coord:
    try:
        lat_text, lon_text = text.split(",")
        lat, lon = float(lat_text), float(lon_text)
    except ValueError:
        raise _invalid(f"{what} must be here or <lat>,<lon>, e.g. 45.466,9.19.") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise _invalid(f"{what} must be here or <lat>,<lon> with lat -90..90 and lon -180..180.")
    return lat, lon


def city_of(address: str | None) -> str:
    """The locality in an address: "Via Roma 1, 20121 Milano MI, Italia" → "Milano" (the part before the
    country, without the postcode and the province code)."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if not parts:
        return ""
    part = parts[-2] if len(parts) >= 2 else parts[-1]
    return " ".join(w for w in part.split() if not w.isdigit() and not (len(w) == 2 and w.isupper()))


def _cache_json(place: ProviderPlace) -> dict[str, Any]:
    return json_safe(dataclasses.asdict(place))


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _from_cache(content: dict[str, Any]) -> ProviderPlace:
    def periods(items: list[dict[str, Any]]) -> list[Period]:
        return [
            Period(p["day"], p["open"], p["close"], date.fromisoformat(p["date"]) if p.get("date") else None)
            for p in items
        ]

    data = dict(content)
    data["weekly"] = periods(data.get("weekly") or [])
    data["current"] = periods(data.get("current") or [])
    data["closed_dates"] = [date.fromisoformat(d) for d in data.get("closed_dates") or []]
    data["reviews"] = [Review(**r) for r in data.get("reviews") or []]
    data["attributes"] = dict(data.get("attributes") or {})  # cached by an older version: none
    data["photos"] = [Photo(**p) for p in data.get("photos") or []]  # cached by an older version: none
    return ProviderPlace(**data)


def weekly_hours(periods: list[Any]) -> list[dict[str, Any]]:
    """The week's hours for the app. Google sends "open around the clock" as one period with no close:
    that is every day, 00:00-24:00."""
    if len(periods) == 1 and periods[0].close is None and periods[0].open == "00:00":
        return [{"day": day, "open": "00:00", "close": "24:00"} for day in range(1, 8)]
    return [{"day": p.day, "open": p.open, "close": p.close or "24:00"} for p in periods]


PAGE_READ_S = 12.0  # one of Pisa's own page reads, at most


class PlacesService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        providers: PlaceProviderService,
        gateway: ModelGateway,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reader: Callable[[str], Awaitable[tuple[str, str] | None]] | None = None,
        counted_reader: Callable[[str], Awaitable[tuple[str, str, int] | None]] | None = None,
    ) -> None:
        self._pool = pool
        self.providers = providers
        self._gateway = gateway
        self._owner = owner
        self._clock = clock
        self._cities: dict[tuple[float, float], frozenset[str]] = {}  # current_cities, per ~1 km cell
        self._photo_urls: dict[tuple[str, int], tuple[str, datetime]] = {}  # (photo, width) → (url, until)
        self._photo_refs: dict[uuid.UUID, tuple[list[Photo], datetime]] = {}  # from the search
        self._photo_misses: dict[uuid.UUID, datetime] = {}  # details gave no photos: until when not to ask
        # Pisa's own signed reads (pisa/connectors/web_reader.py), set when Pisa is published
        self.reader = reader
        # the same signed read, with the hidden runs it left out (a count)
        self.counted_reader = counted_reader

    # --- where the user is ------------------------------------------------------------------------------

    async def latest_location(self) -> tuple[Coord, datetime] | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT lat, lon, measured_at FROM device_locations ORDER BY measured_at DESC LIMIT 1"
                )
            ).fetchone()
        return ((row[0], row[1]), row[2]) if row else None

    async def here(self) -> Coord:
        """The latest device location, at most 15 minutes old; or, when older, an ongoing stay whose last fix
        is at most 2 hours old (sitting still for an hour isn't "unknown"); otherwise
        location_unknown."""
        latest = await self.latest_location()
        if latest is None or self._clock() - latest[1] > LOCATION_FRESH:
            async with self._pool.connection() as conn:
                staying = await stays.ongoing(conn, self._clock())
            if staying is not None:
                return staying
        if latest is None:
            raise PisaError(
                "location_unknown",
                "Pisa doesn't know where you are: no device has sent a location yet.",
                "Open the Pisa app with location access on",
            )
        coord, at = latest
        age = self._clock() - at
        if age > LOCATION_FRESH:
            minutes = int(age.total_seconds() // 60)
            raise PisaError(
                "location_unknown",
                f"Pisa doesn't know where you are now: the last location is {minutes} minutes old (the "
                f"limit is 15).",
                "Open the Pisa app with location access on",
            )
        return coord

    async def _here_or_none(self) -> Coord | None:
        try:
            return await self.here()
        except PisaError:
            return None

    async def _origin(self, origin: From) -> Coord:
        return await self.here() if origin == "here" else origin

    # --- identity ----------------------------------------------------------------------------------------

    async def upsert(self, conn: AsyncConnection, found: ProviderPlace) -> uuid.UUID:
        """The Pisa place for a provider result (see the module docstring), updated with what it brings. The
        phone's maps (contract 2.3) keep no id here (theirs is opaque, the phone's own): their place is the
        row with the same name less than 50 m away, else a new one."""
        info = PLACE_PROVIDERS.get(found.provider)
        id_column = info.id_column if info is not None else None
        # a provider that keeps place ids, or the phone
        assert id_column is not None or found.provider == PHONE
        now = self._clock()
        row = (
            await (
                await conn.execute(f"SELECT id FROM places WHERE {id_column} = %s", (found.provider_id,))  # noqa: S608
            ).fetchone()
            if id_column is not None
            else None
        )
        if row is None and found.lat is not None and found.lon is not None:
            margin = 0.001  # ~100 m of latitude: the candidates for "same name, < 50 m"
            candidates = await (
                await conn.execute(
                    "SELECT id, name, lat, lon FROM places WHERE lat BETWEEN %s AND %s AND lon "
                    "BETWEEN %s AND %s",
                    (found.lat - margin, found.lat + margin, found.lon - 2 * margin, found.lon + 2 * margin),
                )
            ).fetchall()
            for cid, name, lat, lon in candidates:
                if (
                    text_key(name) == text_key(found.name)
                    and distance_m((lat, lon), (found.lat, found.lon)) < SAME_PLACE_M
                ):
                    row = (cid,)
                    break
        coords = found.lat is not None and found.lon is not None
        if row is None and id_column is None:  # the phone's: no id to keep
            made = await (
                await conn.execute(
                    "INSERT INTO places (id, name, category, address, lat, lon, coords_source, coords_at, "
                    "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        uuid.uuid4(), found.name, found.category, found.address, found.lat, found.lon,
                        found.provider if coords else None, now if coords else None, now, now,
                    ),
                )
            ).fetchone()  # fmt: skip
            assert made is not None
            return made[0]
        if row is None:
            assert id_column is not None
            # Parallel searches (the fan-out) can meet the same new place at once: the provider id is
            # unique, so a second insert takes the first one's row instead of failing the turn.
            inserted = await (
                await conn.execute(
                    "INSERT INTO places (id, name, category, address, lat, lon, coords_source, "  # noqa: S608
                    f"coords_at, {id_column}, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"ON CONFLICT ({id_column}) DO NOTHING RETURNING id",
                    (
                        uuid.uuid4(), found.name, found.category, found.address, found.lat, found.lon,
                        found.provider if coords else None, now if coords else None, found.provider_id,
                        now, now,
                    ),
                )
            ).fetchone()  # fmt: skip
            if inserted is not None:
                return inserted[0]
            row = await (
                await conn.execute(f"SELECT id FROM places WHERE {id_column} = %s", (found.provider_id,))  # noqa: S608
            ).fetchone()
            assert row is not None  # the conflicting row's insert committed before ours went on
        place_id = row[0]
        keep_id: LiteralString = f"{id_column} = %s, " if id_column is not None else ""  # the phone's: never
        # Apple's coordinates win (kept without limit); Google's refresh their 30-day clock.
        await conn.execute(
            # The first provider's name, category and address stay: Pisa's label for the place doesn't flip.
            f"UPDATE places SET {keep_id}name = coalesce(name, %s), "  # noqa: S608
            "category = coalesce(category, %s), "
            "address = coalesce(address, %s), updated_at = %s, "
            "lat = CASE WHEN %s AND (coords_source IS DISTINCT FROM 'apple' OR %s = 'apple') THEN %s "
            "ELSE lat END, "
            "lon = CASE WHEN %s AND (coords_source IS DISTINCT FROM 'apple' OR %s = 'apple') THEN %s "
            "ELSE lon END, "
            "coords_at = CASE WHEN %s AND (coords_source IS DISTINCT FROM 'apple' OR %s = 'apple') THEN %s "
            "ELSE coords_at END, "
            "coords_source = CASE WHEN %s AND (coords_source IS DISTINCT FROM 'apple' OR %s = "
            "'apple') THEN %s "
            "ELSE coords_source END WHERE id = %s",
            (
                *([found.provider_id] if id_column is not None else []), found.name, found.category,
                found.address, now,
                coords, found.provider, found.lat,
                coords, found.provider, found.lon,
                coords, found.provider, now,
                coords, found.provider, found.provider,
                place_id,
            ),
        )  # fmt: skip
        return place_id

    async def _row(self, conn: AsyncConnection, place_id: uuid.UUID) -> dict[str, Any]:
        cur = await conn.execute(
            "SELECT id, name, category, address, lat, lon, apple_id, google_place_id, google_no_match_at, "
            "osm_id FROM places WHERE id = %s",
            (place_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise PisaError(
                "not_found", f"There is no place with id {place_id}.", "Search for the place again"
            )
        keys = (
            "id", "name", "category", "address", "lat", "lon", "apple_id", "google_place_id",
            "google_no_match_at", "osm_id",
        )  # fmt: skip
        return dict(zip(keys, row, strict=True))

    async def name(self, place_id: uuid.UUID) -> str:
        """The place's name; not_found when there is no such place."""
        async with self._pool.connection() as conn:
            return (await self._row(conn, place_id))["name"]

    async def label(self, place_id: uuid.UUID) -> tuple[str, str | None]:
        """The place's name and category; not_found when there is no such place."""
        async with self._pool.connection() as conn:
            row = await self._row(conn, place_id)
        return row["name"], row["category"]

    @staticmethod
    def parse_id(place_id: str) -> uuid.UUID:
        try:
            return uuid.UUID(place_id)
        except ValueError:
            raise PisaError(
                "not_found", f"There is no place with id {place_id[:40]!r}.", "Search for the place again"
            ) from None

    # --- search ------------------------------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        near: From | None,
        open_now: bool = False,
        limit: int = 5,
        rich: bool = False,
        radius_m: int | None = None,
        provider: str | None = None,
    ) -> PlaceResults:
        """Places for a query. `rich` (the conversation's map_search): the rating, price level, open now and
        the provider's attributes too, at the provider's richer price. `radius_m`: only places that close to
        `near` (or to the last known position). `provider`: that provider only. A filter the provider can't
        apply is dropped and named in the results' `not_applied`, never an error."""
        query = query.strip()
        if not 2 <= len(query) <= 200:
            raise _invalid("q must be 2 to 200 characters long.")
        if not 1 <= limit <= 10:
            raise _invalid("limit must be a whole number from 1 to 10.")
        # no provider can search places here: said as not set up, before any location is looked for (§29 w4:
        # "non so dove sei" for a search nothing could run)
        # (a turn with the phone's maps can always search: the phone answers first, with no key)
        if (
            provider is None
            and phone_maps.current.get() is None
            and JOB_PHRASE["search"] in await self.providers.unset()
        ):
            raise PisaError(
                "place_provider_not_configured",
                "No provider for place search is ready: none has a key on this server.",
                "Add a key in Settings → Places",
                detail="provider_not_configured",
            )
        if near is None:
            latest = await self.latest_location()
            center = latest[0] if latest else None  # a bias only: any age will do
        else:
            center = await self._origin(near)
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)

        def near_enough(found: ProviderPlace) -> bool:
            if radius_m is None or center is None or found.lat is None or found.lon is None:
                return True
            return distance_m(center, (found.lat, found.lon)) <= radius_m

        # "near here" or near a named place, with no distance of theirs: the close ones first (5 km),
        # widened once to 25 km when fewer than 3, never farther (§31 p1: restaurants 146-201 km away). A
        # city in their words with no `near` is a bias.
        capped = near is not None and radius_m is None and center is not None
        widened = False

        def within(found: list[ProviderPlace], meters: int) -> list[ProviderPlace]:
            if center is None:
                return found
            return [
                f for f in found
                if f.lat is not None and f.lon is not None and distance_m(center, (f.lat, f.lon)) <= meters
            ]  # fmt: skip

        # what each provider asked couldn't apply, by its id: the one that answers is said
        not_applied: dict[str, tuple[str, ...]] = {}

        async def ask(adapter: Any, bound_m: int | None) -> list[ProviderPlace]:
            # each filter and the richer call only where the provider has them (its stated capabilities)
            not_applied[adapter.id] = tuple(
                name for name, dropped in (
                    ("open_now", open_now and not adapter.can_filter_open_now),
                    ("rich", rich and not adapter.can_search_rich),
                    ("radius_m", radius_m is not None and center is None),  # no point to measure from
                ) if dropped
            )  # fmt: skip
            extra = {"within_m": bound_m} if bound_m is not None else {}
            found = await adapter.search(
                query,
                near=center,
                open_now=open_now and adapter.can_filter_open_now,
                limit=limit,
                lang=lang,
                rich=rich and adapter.can_search_rich,
                **extra,
            )
            return [f for f in found if near_enough(f)]

        async def search(adapter: Any) -> list[ProviderPlace]:
            nonlocal widened
            if not capped:
                return await ask(adapter, None)
            # a provider that can bound its search to a distance (OpenStreetMap's Photon) is asked within
            # 5 km, then once within 25 km (a second request, through its rate limiter); the others' results
            # are filtered the same way. 25 km is the last guard for every provider.
            bounds = getattr(adapter, "can_bound", False)
            found = await ask(adapter, NEAR_M if bounds else None)
            close = within(found, NEAR_M)
            if len(close) >= NEAR_ENOUGH:
                return close
            if bounds:
                found = await ask(adapter, NEAR_MAX_M)
            wide = within(found, NEAR_MAX_M)
            widened = len(wide) > len(close)
            return wide

        async def choose(can: Callable[[Any], bool]) -> Any:
            return await self.providers.choose(
                "search",
                search,
                can=can,
                usage=lambda a: a.usage_for_search(rich and a.can_search_rich),
                found=bool,  # one provider found nothing → ask the next ("a bakery for a coffee")
                only=provider,
            )

        # "open now" from a provider that can filter by it, first; with none able (§30 p1), the places all the
        # same, never "can't do this request": those their own listed hours say are closed now are left out
        # (OpenStreetMap's simple `opening_hours`), the others kept, open or with their hours unknown
        hours_unknown = False
        try:
            choice = await choose((lambda a: a.can_filter_open_now) if open_now else (lambda a: True))
        except PisaError as err:
            if not open_now or err.code != "place_provider_not_configured":
                raise
            choice = await choose(lambda a: not a.can_filter_open_now)
            hours_unknown = True
        out = PlaceResults()
        out.not_applied = not_applied.get(choice.provider, ())
        async with self._pool.connection() as conn:
            # One order for every search's inserts, so parallel searches that meet the same places in another
            # rank never wait on each other in a circle (a deadlock); the results keep the provider's rank.
            saved: dict[tuple[str, str], uuid.UUID] = {}
            for found in sorted(choice.value, key=lambda f: (f.provider, f.provider_id)):
                saved[(found.provider, found.provider_id)] = await self.upsert(conn, found)
            for found in choice.value:
                place_id = saved[(found.provider, found.provider_id)]
                if found.photos:
                    self._keep_photo_refs(place_id, found.photos)
                cached = await self._cached(conn, place_id)
                summary = self._summary(place_id, found, cached, center)
                if widened:
                    summary["widened_km"] = NEAR_MAX_M // 1000  # none close enough: the nearest, up to 25 km
                if hours_unknown and summary.get("open_now") is False:
                    out.closed_left_out += 1  # asked open now: its listed hours say closed
                    continue
                if hours_unknown and summary.get("open_now") is None:
                    summary["hours_unknown"] = True  # asked open now: this map can't say
                if rich:
                    summary |= {
                        "price_level": found.price_level if found.price_level is not None else (
                            cached.price_level if cached else None
                        ),
                        "attributes": found.attributes or (cached.attributes if cached else {}),
                        "photo_count": len(found.photos or (cached.photos if cached else [])),
                        "provider": choice.provider,
                    }  # fmt: skip
                out.append(summary)
        return out

    async def nearby(
        self, near: From, limit: int = NEARBY_MAX, open_now: bool = False
    ) -> list[dict[str, Any]]:
        """GET /v1/places/search without `q`: the places Pisa already knows nearest to `near`, by
        distance, in the search's item shape. No provider call: Pisa's own table, within ~5 km. With
        `open_now`, only the ones Pisa knows are open now (it was ignored)."""
        if not 1 <= limit <= NEARBY_MAX:
            raise _invalid(f"limit must be a whole number from 1 to {NEARBY_MAX}.")
        center = await self._origin(near)
        lat, lon = center
        (lat_lo, lat_hi), lons = nearby_box(lat, lon)
        where = " OR ".join("lon BETWEEN %s AND %s" for _ in lons)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    # where: constant text, one or two "lon BETWEEN %s AND %s"
                    "SELECT id, name, category, address, lat, lon FROM places WHERE lat BETWEEN %s AND %s "  # noqa: S608
                    f"AND ({where})",
                    (lat_lo, lat_hi, *[v for pair in lons for v in pair]),
                )
            ).fetchall()
            ranked = sorted(rows, key=lambda r: (distance_m(center, (r[4], r[5])), r[1]))
            out: list[dict[str, Any]] = []
            for pid, name, category, address, plat, plon in ranked:
                if len(out) >= limit:
                    break
                cached = await self._cached(conn, pid)
                is_open = (
                    open_state(
                        cached.current + cached.weekly,
                        self._clock(),
                        self._owner.tz,
                        frozenset(cached.closed_dates),
                    )[0]
                    if cached is not None
                    else None
                )
                if open_now and is_open is not True:
                    continue
                out.append({
                    "id": str(pid), "name": name,
                    "category": category or (cached.category if cached else None), "address": address,
                    "lat": plat, "lon": plon, "distance_m": distance_m(center, (plat, plon)),
                    "open_now": is_open, "rating": cached.rating if cached else None,
                    "rating_count": cached.rating_count if cached else None,
                })  # fmt: skip
        return out

    def _summary(
        self, place_id: uuid.UUID, found: ProviderPlace, cached: ProviderPlace | None, center: Coord | None
    ) -> dict[str, Any]:
        info = cached or found
        open_now = found.open_now
        if open_now is None and (found.weekly or found.current):  # the hours the search brought (OSM's)
            open_now = open_state(
                found.current + found.weekly, self._clock(), self._owner.tz, frozenset(found.closed_dates)
            )[0]
        if open_now is None and cached is not None:
            open_now = open_state(
                cached.current + cached.weekly, self._clock(), self._owner.tz, frozenset(cached.closed_dates)
            )[0]
        has_coords = found.lat is not None and found.lon is not None
        return {
            "id": str(place_id),
            "name": found.name,
            "category": found.category or info.category,
            "address": found.address or info.address,
            "lat": found.lat,
            "lon": found.lon,
            "distance_m": distance_m(center, (found.lat, found.lon)) if center and has_coords else None,  # type: ignore[arg-type]
            "open_now": open_now,
            "rating": info.rating,
            "rating_count": info.rating_count,
            # the credit the data's licence asks to be shown with it (OpenStreetMap's ODbL), when it asks one
            **({"attribution": found.attribution} if found.attribution else {}),
            # hours listed but not read (OSM's holidays, sunrise…): the raw text, never an open or closed
            **({"hours_note": found.hours_note} if open_now is None and found.hours_note else {}),
        }

    # --- details (cache, 24 h) ---------------------------------------------------------------------------

    async def _cached(
        self, conn: AsyncConnection, place_id: uuid.UUID, *, reviews: bool = False
    ) -> ProviderPlace | None:
        kinds = ["details_reviews"] if reviews else ["details_reviews", "details"]
        row = await (
            await conn.execute(
                "SELECT content FROM place_cache WHERE place_id = %s AND kind = ANY(%s) AND expires_at > %s "
                "ORDER BY fetched_at DESC LIMIT 1",
                (place_id, kinds, self._clock()),
            )
        ).fetchone()
        return _from_cache(row[0]) if row else None

    async def details(
        self, place_id: uuid.UUID, *, reviews: bool, skipped: list[str] | None = None
    ) -> tuple[ProviderPlace | None, str | None]:
        """Hours, phone, website, rating (and reviews): from the cache, else from the details provider. Never
        fails: (None, None) when no provider can give them (the reason goes to `skipped`)."""
        skipped = skipped if skipped is not None else []
        async with self._pool.connection() as conn:
            cached = await self._cached(conn, place_id, reviews=reviews)
            if cached is not None:
                return cached, cached.provider
            row = await self._row(conn, place_id)
            settings = await self._owner.settings(conn)
        lang = settings["language"]
        if not settings["places"]["details"]:
            skipped.append("details: switched off in Settings → Places")
            return None, None
        coord = (row["lat"], row["lon"]) if row["lat"] is not None else None

        async def fetch(adapter: Any) -> ProviderPlace:
            column = PLACE_PROVIDERS[adapter.id].id_column
            provider_id = row[column] if column is not None else None
            if provider_id is None:
                provider_id = await self._link(adapter, row, coord, lang)
                return await adapter.details(provider_id, reviews=reviews, lang=lang)
            try:
                return await adapter.details(provider_id, reviews=reviews, lang=lang)
            except PlaceProviderError as err:
                if err.status != 404:
                    raise
            # Google retired the stored id (places get merged or moved): forget it, look it up once more.
            async with self._pool.connection() as conn:
                await conn.execute("UPDATE places SET google_place_id = NULL WHERE id = %s", (place_id,))
            _log.info("place_id_obsolete", place_id=str(place_id), provider=adapter.id)
            provider_id = await self._link(adapter, {**row, "google_place_id": None}, coord, lang)
            return await adapter.details(provider_id, reviews=reviews, lang=lang)

        try:
            choice = await self.providers.choose(
                "details", fetch, usage=lambda a: a.usage_for_details(reviews)
            )
        except PisaError as err:
            skipped.append(f"details: {err.message}")
            return None, None
        skipped.extend(choice.skipped)
        found = choice.value
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            # Google's coordinates restart their 30-day clock, or fill a place that has none.
            if found.lat is not None and found.lon is not None:
                await conn.execute(
                    "UPDATE places SET lat = %s, lon = %s, coords_source = 'google', coords_at = %s "
                    "WHERE id = %s AND (coords_source = 'google' OR lat IS NULL)",
                    (found.lat, found.lon, now, place_id),
                )
            await conn.execute(
                "INSERT INTO place_cache (place_id, provider, kind, content, fetched_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (place_id, provider, kind) DO UPDATE SET "
                "content = EXCLUDED.content, fetched_at = EXCLUDED.fetched_at, expires_at = "
                "EXCLUDED.expires_at",
                (
                    place_id, found.provider, "details_reviews" if reviews else "details",
                    Jsonb(_cache_json(found)), now, now + CACHE_TTL,
                ),
            )  # fmt: skip
        return found, choice.provider

    # --- travel ------------------------------------------------------------------------------------------

    async def travel_between(
        self,
        origin: Coord,
        destinations: list[Coord],
        mode: Mode,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> tuple[list[TravelEstimate | None], str, list[str]]:
        choice = await self.providers.choose(
            "routes",
            lambda a: a.travel(origin, destinations, mode, depart_at=depart_at, arrive_by=arrive_by),
            can=lambda a: a.can_travel(mode, arrive_by=arrive_by is not None),
            usage=lambda a: a.usage_for_travel(mode, len(destinations)),
        )
        return choice.value, choice.provider, choice.skipped

    async def coords(self, place_id: uuid.UUID) -> Coord:
        """The place's coordinates; when Google's expired (30 days), found again by a search. A place not
        found again is searched again only after a day: the planner asks on every re-plan.
        """
        now = self._clock()
        async with self._pool.connection() as conn:
            row = await self._row(conn, place_id)
            retry = await (
                await conn.execute("SELECT coords_retry_at FROM places WHERE id = %s", (place_id,))
            ).fetchone()
        if row["lat"] is not None:
            return row["lat"], row["lon"]
        if retry is None or retry[0] is None or retry[0] <= now:
            query = row["name"] + (f", {row['address']}" if row["address"] else "")
            for found in await self.search(query, near=None, limit=3):
                if found["id"] == str(place_id) and found["lat"] is not None:
                    return found["lat"], found["lon"]
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE places SET coords_retry_at = %s WHERE id = %s", (now + RESOLVE_RETRY, place_id)
                )
        raise PisaError(
            "place_provider_error",
            f"Pisa has no coordinates for {row['name']} and couldn't find them again.",
            "Search for the place again",
        )

    async def travel(
        self,
        place_id: uuid.UUID,
        *,
        mode: Mode | None = None,
        origin: From = "here",
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> dict[str, Any]:
        if depart_at is not None and arrive_by is not None:
            raise _invalid("Give depart_at or arrive_by, not both.")
        if mode is None:
            async with self._pool.connection() as conn:
                mode = (await self._owner.settings(conn))["places"]["default_mode"]
        assert mode is not None
        start = await self._origin(origin)
        destination = await self.coords(place_id)
        estimates, provider, _ = await self.travel_between(
            start, [destination], mode, depart_at=depart_at, arrive_by=arrive_by
        )
        estimate = estimates[0]
        if estimate is None:
            raise PisaError(
                "place_provider_error",
                f"{provider} found no {mode} route to this place.",
                "Try another mode",
            )
        return {
            "minutes": estimate.minutes,
            "traffic_minutes": estimate.traffic_minutes,
            "distance_m": estimate.distance_m,
            "mode": mode,
            "provider": provider,
        }

    # --- the card ---------------------------------------------------------------------------

    async def _link(self, adapter: Any, row: dict[str, Any], coord: Coord | None, lang: str) -> str:
        """Find this place's id at the details provider and link it, only if it is the same spot.
        A mismatch is remembered for a week, so Pisa doesn't pay to look again."""
        now = self._clock()
        if coord is None:
            raise PlaceProviderError(f"{adapter.id}: this place has no position to check a match against.")
        checked = row.get("google_no_match_at")
        if checked is not None and now - checked < NO_MATCH_RETRY:
            raise PlaceProviderError(f"{adapter.id} has no place at this spot (checked {checked:%d/%m}).")
        query = row["name"] + (f", {row['address']}" if row["address"] else "")
        hit = await adapter.find_id(query, near=coord, lang=lang)
        far = hit is not None and hit[1] is not None and distance_m(hit[1], coord) > LINK_MAX_M
        if hit is None or hit[1] is None or far:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE places SET google_no_match_at = %s WHERE id = %s", (now, row["id"])
                )
            where = f"{distance_m(hit[1], coord)} m away" if far and hit and hit[1] else "not found here"
            raise PlaceProviderError(f"{adapter.id}'s closest match for this place is {where}.")
        provider_id = hit[0]
        async with self._pool.connection() as conn, conn.transaction():
            # The id belongs to this place: a row that held it (an older search result) gives it up.
            await conn.execute(
                "UPDATE places SET google_place_id = NULL WHERE google_place_id = %s AND id <> %s",
                (provider_id, row["id"]),
            )
            await conn.execute(
                "UPDATE places SET google_place_id = %s, google_no_match_at = NULL, updated_at = %s "
                "WHERE id = %s",
                (provider_id, now, row["id"]),
            )
        return provider_id

    async def card(self, place_id: uuid.UUID, *, reviews: bool = True, travel: bool = True) -> dict[str, Any]:
        """Contract I's card. `travel`: with the time from here (a route call); place_details leaves it to
        travel_time."""
        now = self._clock()
        skipped: list[str] = []
        async with self._pool.connection() as conn:
            row = await self._row(conn, place_id)
            settings = await self._owner.settings(conn)
        info, details_provider = await self.details(place_id, reviews=reviews, skipped=skipped)
        async with self._pool.connection() as conn:
            row = await self._row(conn, place_id)  # details may have refreshed it
            insight = await self._insight(conn, place_id)
            related = await self._related(conn, place_id, now)
        hours = None
        if info is not None and (info.weekly or info.current):
            is_open, change = open_state(
                info.current + info.weekly, now, self._owner.tz, frozenset(info.closed_dates)
            )
            hours = {
                "weekly": weekly_hours(info.weekly),
                "open_now": is_open,
                "next_change": change.isoformat() if change else None,
                "note": info.hours_note,
            }
        from_here = (
            await self._travel_from_here(row, settings["places"]["default_mode"], skipped) if travel else None
        )
        return {
            "id": str(place_id),
            "apple_id": None if (row["apple_id"] or "").startswith(GEOCODE_ID_PREFIX) else row["apple_id"],
            "name": row["name"],
            "category": row["category"] or (info.category if info else None),
            "address": row["address"] or (info.address if info else None),
            "lat": row["lat"],
            "lon": row["lon"],
            "phone": info.phone if info else None,
            "website": info.website if info else None,
            "hours": hours,
            "rating": info.rating if info else None,
            "rating_count": info.rating_count if info else None,
            "price_level": info.price_level if info else None,
            "summary": info.summary if info else None,
            "attributes": dict(info.attributes) if info else {},
            "photo_count": len(info.photos) if info else 0,
            "reviews": [dataclasses.asdict(r) for r in info.reviews] if info else [],
            "insights": insight,
            "travel": from_here,
            "links": map_links(row["name"], row["lat"], row["lon"], row["google_place_id"]),
            "related": related,
            # OpenStreetMap's credit (ODbL) when its data named the place
            "attribution": OSM_ATTRIBUTION if row["osm_id"] else None,
            "sources": {
                "search": "apple"
                if row["apple_id"]
                else "google"
                if row["google_place_id"]
                else "osm"
                if row["osm_id"]
                else None,
                "details": details_provider,
                "fetched_at": now.isoformat(),
                "skipped": skipped,
            },
        }

    async def _travel_from_here(
        self, row: dict[str, Any], mode: Mode, skipped: list[str]
    ) -> dict[str, Any] | None:
        here = await self._here_or_none()
        if here is None or row["lat"] is None:
            return None
        try:
            estimates, provider, more = await self.travel_between(here, [(row["lat"], row["lon"])], mode)
        except PisaError as err:
            skipped.append(f"routes: {err.message}")
            return None
        skipped.extend(more)
        estimate = estimates[0]
        if estimate is None:
            return None
        return {
            "from": "here",
            "mode": mode,
            "minutes": estimate.minutes,
            "traffic_minutes": estimate.traffic_minutes,
            "provider": provider,
            "at": self._clock().isoformat(),
        }

    # --- the detail sheet -------------------------------------------------------------------------

    async def detail_sheet(self, place_id: uuid.UUID) -> dict[str, Any]:
        """GET /v1/places/{id}/details: what the app's detail sheet shows. The reviews' summary is Google's
        when it has one, else Pisa's own research (with its sources); the menu only as Pisa read it."""
        info, _ = await self.details(place_id, reviews=True)
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            row = await self._row(conn, place_id)
            insight = await self._insight(conn, place_id)
            menu = await (
                await conn.execute("SELECT url, highlights FROM place_menus WHERE place_id = %s", (place_id,))
            ).fetchone()
        hours = None
        if info is not None and (info.weekly or info.current):
            is_open, change = open_state(
                info.current + info.weekly, self._clock(), self._owner.tz, frozenset(info.closed_dates)
            )
            hours = {
                "weekly": weekly_hours(info.weekly),
                "open_now": is_open,
                "next_change": change.astimezone(tz).isoformat() if change else None,
                "note": info.hours_note,
            }
        # Google's AI summaries go with their disclosure and report link (Google's attribution policy); its
        # generativeSummary describes the place, so it is never shown as what the reviews say.
        summary = None
        if info is not None and info.review_summary:
            summary = {
                "text": info.review_summary, "source": "google", "sources": [],
                "disclosure": info.review_summary_disclosure or GOOGLE_AI_DISCLOSURE,
                "flag_uri": info.review_summary_flag_uri, "uri": info.reviews_uri,
            }  # fmt: skip
        elif insight is not None:
            summary = {"text": insight["text"], "source": "pisa", "sources": insight["sources"]}
        description = None
        if info is not None and info.description:
            description = {
                "text": info.description,
                "disclosure": info.description_disclosure or GOOGLE_AI_DISCLOSURE,
                "flag_uri": info.description_flag_uri,
            }
        apple = row["apple_id"]
        return {
            "id": str(place_id),
            "name": row["name"],
            "hours": hours,
            "phone": info.phone if info else None,
            "website": info.website if info else None,
            "review_summary": summary,
            "description": description,
            "reviews": [
                {
                    "author": r.author,
                    "author_uri": r.author_uri,
                    "rating": r.rating,
                    "relative_time": r.relative_time,
                    "text": r.text[:REVIEW_MAX],
                }
                for r in (info.reviews if info else [])[:REVIEWS_SHOWN]
            ],
            "menu": {"url": menu[0], "highlights": list(menu[1]), "source": menu[0]} if menu else None,
            "apple_place_id": None if not apple or apple.startswith(GEOCODE_ID_PREFIX) else apple,
            "photos": [
                {"index": i, "attribution": {"name": p.author, "uri": p.author_uri}}
                for i, p in enumerate(info.photos if info else [])
            ],
        }

    def _keep_photo_refs(self, place_id: uuid.UUID, photos: list[Photo]) -> None:
        now = self._clock()
        self._photo_refs = {k: v for k, v in self._photo_refs.items() if v[1] > now}
        self._photo_refs[place_id] = (list(photos), now + PHOTO_REFS_LIFE)
        self._photo_misses.pop(place_id, None)

    async def _photos(self, place_id: uuid.UUID) -> list[Photo]:
        """The place's photo names: the search's, else the details cache, else a details call; a details
        call that gave none isn't repeated for 10 minutes (each photo request would pay it again)."""
        now = self._clock()
        refs = self._photo_refs.get(place_id)
        if refs is not None and refs[1] > now:
            return refs[0]
        async with self._pool.connection() as conn:
            cached = await self._cached(conn, place_id)
        if cached is not None and cached.photos:
            return cached.photos
        missed = self._photo_misses.get(place_id)
        if missed is not None and missed > now:
            return []
        info, _ = await self.details(place_id, reviews=True)
        if info is None or not info.photos:
            self._photo_misses = {k: v for k, v in self._photo_misses.items() if v > now}
            self._photo_misses[place_id] = now + PHOTO_MISS_LIFE
            return []
        return info.photos

    async def photo(self, place_id: uuid.UUID, index: int, width: int) -> dict[str, Any]:
        """A short-lived URL of the place's photo `index`: through Pisa's server, so the key stays here;
        no copy is kept. The same photo asked again within its life is not paid twice; widths snap to 400,
        800 or 1600 so near sizes share one URL."""
        photos = await self._photos(place_id)
        if not 0 <= index < len(photos):
            raise PisaError(
                "not_found",
                f"The place has {len(photos)} photos: there is no photo {index}.",
                "Refresh the place",
            )
        width = next((w for w in PHOTO_WIDTHS if w >= width), PHOTO_WIDTHS[-1])
        photo = photos[index]
        key = (photo.name, width)
        now = self._clock()
        cached = self._photo_urls.get(key)
        if cached is None or cached[1] <= now:
            self._photo_urls = {k: v for k, v in self._photo_urls.items() if v[1] > now}
            while len(self._photo_urls) >= PHOTO_URLS_MAX:  # the oldest first (insertion order)
                self._photo_urls.pop(next(iter(self._photo_urls)))
            choice = await self.providers.choose(
                "details",
                lambda a: a.photo_uri(photo.name, max_width=width),
                can=lambda a: hasattr(a, "photo_uri"),
                usage=lambda a: a.usage_for_photo(),
                only="google",
            )
            cached = (choice.value, now + PHOTO_URL_LIFE)
            self._photo_urls[key] = cached
        return {
            "url": cached[0],
            "expires_at": cached[1].astimezone(self._owner.zone).isoformat(),
            "attribution": {"name": photo.author, "uri": photo.author_uri},
        }

    async def save_menu(self, place_id: str, url: str, highlights: list[str]) -> None:
        """A menu Pisa read this turn (show_places): its page and the dishes it names."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO place_menus (place_id, url, highlights, read_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (place_id) DO UPDATE SET url = EXCLUDED.url, highlights = EXCLUDED.highlights, "
                "read_at = EXCLUDED.read_at",
                (self.parse_id(place_id), url, Jsonb(highlights), self._clock()),
            )

    async def _insight(self, conn: AsyncConnection, place_id: uuid.UUID) -> dict[str, Any] | None:
        row = await (
            await conn.execute(
                "SELECT summary, sources, created_at FROM place_insights WHERE place_id = %s AND "
                "expires_at > %s "
                "ORDER BY created_at DESC LIMIT 1",
                (place_id, self._clock()),
            )
        ).fetchone()
        if row is None:
            return None
        return {"text": row[0], "sources": row[1], "fetched_at": row[2].isoformat()}

    async def _related(
        self, conn: AsyncConnection, place_id: uuid.UUID, now: datetime
    ) -> list[dict[str, Any]]:
        """The why: events there (from a week ago on) and open tasks there."""
        tz = self._owner.zone
        events = await (
            await conn.execute(
                "SELECT DISTINCT ON (id) id, title, starts_at FROM calendar_events WHERE place_id = %s "
                "AND ends_at > %s ORDER BY id, starts_at",
                (place_id, now - RELATED_PAST),
            )
        ).fetchall()
        tasks = await (
            await conn.execute(
                "SELECT id::text, title, due_at FROM tasks WHERE place_id = %s AND status = 'open'",
                (place_id,),
            )
        ).fetchall()
        out = [
            {"kind": "event", "id": eid, "title": title, "when": start.astimezone(tz).isoformat()}
            for eid, title, start in sorted(events, key=lambda e: e[2])
        ]
        out += [
            {
                "kind": "task",
                "id": tid,
                "title": title,
                "when": due.astimezone(tz).isoformat() if due else None,
            }
            for tid, title, due in tasks
        ]
        return out

    # --- research ----------------------------------------------------------------------------------------

    async def pages(self, urls: list[str]) -> dict[str, str]:
        """{url: text} of these pages. Pisa reads each one itself first, signed (its own
        identity, when published); the ones it couldn't read (a refusal, a challenge) go to the provider."""
        out: dict[str, str] = {}
        if self.reader is not None:
            reader = self.reader

            async def one(url: str) -> tuple[str, str] | None:
                try:  # together, each cut off (a slow drip never holds the turn)
                    return await asyncio.wait_for(reader(url), PAGE_READ_S)
                except TimeoutError:
                    return None

            for url, read in zip(urls, await asyncio.gather(*(one(u) for u in urls)), strict=True):
                if read is not None:
                    out[url] = read[1]
        rest = [u for u in urls if u not in out]
        if rest:
            extracted = await self.providers.choose(
                "web", lambda a: a.extract(rest), usage=lambda a: a.usage_for_extract(len(rest))
            )
            out |= {p.url: p.text for p in extracted.value}
        return out

    async def research(self, place_id: uuid.UUID, question: str | None) -> dict[str, Any]:
        """Web search for the place → the top pages extracted → a summary with its sources (slot
        places.research) → stored 30 days. Returns the card with the fresh insights."""
        if question is not None and len(question) > 300:
            raise _invalid("question must be at most 300 characters long.")
        async with self._pool.connection() as conn:
            row = await self._row(conn, place_id)
            lang = await self._owner.lang(conn)
        city = city_of(row["address"])
        query = " ".join(x for x in (row["name"], city, question or "") if x)[:400]
        found = await self.providers.choose(
            "web",
            lambda a: a.search(query, max_results=RESEARCH_RESULTS),
            usage=lambda a: a.usage_for_search(),
        )
        results = found.value
        urls = [r.url for r in results[:RESEARCH_PAGES]]
        pages: dict[str, str] = {}
        if urls:
            pages = await self.pages(urls)
        sources = [
            brain.Source(r.title, r.url, pages.get(r.url) or r.content) for r in results[:RESEARCH_PAGES]
        ]
        async with self._pool.connection() as conn:  # only what is cached: research buys no Place Details
            info = await self._cached(conn, place_id)
            with_reviews = await self._cached(conn, place_id, reviews=True)
        reviews = [r.text for r in (with_reviews.reviews if with_reviews else [])]
        # Google content may be cached at most 24 h: a summary built from its reviews lives as long;
        # one from web pages alone, 30 days.
        insights_ttl = (
            CACHE_TTL
            if reviews and with_reviews and PLACE_PROVIDERS[with_reviews.provider].cache_24h
            else INSIGHTS_TTL
        )
        facts = brain.PlaceFacts(
            row["name"], row["address"], row["category"] or (info.category if info else None)
        )
        now = self._clock()
        if not sources:
            summary, fact_rows = (
                "Nessuna pagina web su questo posto."
                if lang == "it-IT"
                else "No web pages about this place.",
                [],
            )
        else:
            instructions, user = brain.prompt(facts, question, sources, reviews, lang)

            async def call(ctx: CallContext) -> brain.Research:
                output, usage = await brain.research(ctx.model, facts, question, sources, reviews, lang)
                ctx.usage.update(usage)
                return output

            result = await self._gateway.run(
                brain.SLOT,
                call,
                prompt_chars=len(instructions) + len(user),
                max_output_tokens=brain.MAX_OUTPUT_TOKENS,
            )
            summary, fact_rows = result.summary, [f.model_dump() for f in result.facts]
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO place_insights (id, place_id, question, summary, facts, sources, "
                "created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(), place_id, question, summary, Jsonb(fact_rows),
                    Jsonb([{"title": s.title, "url": s.url} for s in sources]), now, now + insights_ttl,
                ),
            )  # fmt: skip
        _log.info("place_researched", place_id=str(place_id), sources=len(sources), skipped=found.skipped)
        return await self.card(place_id, reviews=False)

    # --- resolution of event and task locations ----------------------------------------------------------

    async def resolve_text(self, text: str) -> uuid.UUID | None:
        """The place for a location text, only when one clear match exists. Memoised by normalised text."""
        key = text_key(text)
        if not key:
            return None
        now = self._clock()
        async with self._pool.connection() as conn:
            memo = await (
                await conn.execute(
                    "SELECT place_id, resolved_at FROM place_resolutions WHERE text_key = %s", (key,)
                )
            ).fetchone()
        age = now - memo[1] if memo is not None else None
        if (
            memo is not None
            and age is not None
            and age < (RESOLUTION_TTL if memo[0] is not None else RESOLVE_RETRY)
        ):
            return memo[0]  # a match is trusted 30 days, a no-match one day
        latest = await self.latest_location()
        reference = latest[0] if latest else None
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
        choice = await self.providers.choose(
            "search",
            lambda a: a.search(text, near=reference, open_now=False, limit=5, lang=lang),
            usage=lambda a: a.usage_for_search(),
        )
        distinct = _distinct_near(choice.value, reference)
        if len(distinct) != 1:  # a plain street address matches no single place: look the address up
            try:
                found = await self.providers.choose(
                    "search",
                    lambda a: a.geocode(text, near=reference, lang=lang),
                    can=lambda a: hasattr(a, "geocode"),
                    usage=lambda a: Usage("apple.geocode"),
                )
            except PisaError as err:
                _log.info("place_geocode_skipped", code=err.code)
            else:
                if len(addresses := _distinct_near(found.value, reference)) == 1:
                    distinct = addresses
        async with self._pool.connection() as conn, conn.transaction():
            place_id = await self.upsert(conn, distinct[0]) if len(distinct) == 1 else None
            await conn.execute(
                "INSERT INTO place_resolutions (text_key, place_id, resolved_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (text_key) DO UPDATE SET place_id = EXCLUDED.place_id, resolved_at = "
                "EXCLUDED.resolved_at, failed_at = NULL",
                (key, place_id, now),
            )
        _log.info("place_resolved" if place_id else "place_unresolved", matches=len(distinct))
        return place_id

    async def resolve_task_text(
        self, text: str, here: frozenset[str] | None
    ) -> tuple[uuid.UUID | None, str | None]:
        """A task's place: (the place, None), or (None, the city) when the text names another city than the
        one the user is in (`here`: that city's names, as text keys, in Italian and English), or (None,
        None). Stricter than an event's location: the found place must match the text, name or
        address (token set ≥ 60%), so a search near Amsterdam for a Rome parcel point doesn't settle on some
        Amsterdam shop. Memoised apart from event texts, with the city it was checked in: in another city it
        is checked again."""
        key = "task " + text_key(text)
        if key == "task ":
            return None, None
        now = self._clock()
        checked_in = city_names_key(here)
        async with self._pool.connection() as conn:
            memo = await (
                await conn.execute(
                    "SELECT place_id, resolved_at, elsewhere, here_city FROM place_resolutions "
                    "WHERE text_key = %s",
                    (key,),
                )
            ).fetchone()
        fresh = memo is not None and now - memo[1] < (RESOLUTION_TTL if memo[0] or memo[2] else RESOLVE_RETRY)
        if memo is not None and fresh and (memo[3] == checked_in or (memo[0] and not memo[2])):
            return memo[0], memo[2]  # a place found stays; "Elsewhere" holds only in the same city
        latest = await self.latest_location()
        reference = latest[0] if latest else None
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
        elsewhere = await self._elsewhere(text, here, lang) if here else None
        place_id = None
        if elsewhere is None:
            choice = await self.providers.choose(
                "search",
                lambda a: a.search(text, near=reference, open_now=False, limit=5, lang=lang),
                usage=lambda a: a.usage_for_search(),
            )
            found = [
                p for p in _distinct_near(choice.value, reference) if matches_text(text, p.name, p.address)
            ]
            async with self._pool.connection() as conn, conn.transaction():
                place_id = await self.upsert(conn, found[0]) if len(found) == 1 else None
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO place_resolutions (text_key, place_id, resolved_at, elsewhere, here_city) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (text_key) DO UPDATE SET "
                "place_id = EXCLUDED.place_id, resolved_at = EXCLUDED.resolved_at, "
                "elsewhere = EXCLUDED.elsewhere, here_city = EXCLUDED.here_city, failed_at = NULL",
                (key, place_id, now, elsewhere, checked_in),
            )
        _log.info("task_place_resolved" if place_id else "task_place_unresolved", elsewhere=bool(elsewhere))
        return place_id, elsewhere

    async def _elsewhere(self, text: str, here: frozenset[str], lang: str) -> str | None:
        """The city the text names, when it is not where the user is: the text is geocoded (in the user's
        language, then the other one), and the city of an address counts only when the text itself names it
        ("…, Roma" or "…, Rome"). Compared with both names of the user's city, so the language of the text
        doesn't matter."""
        for in_lang in (lang, *(other for other in CITY_LANGS if other != lang)):
            try:
                found = await self.providers.choose(
                    "search",
                    lambda a, in_lang=in_lang: a.geocode(text, near=None, lang=in_lang),
                    can=lambda a: hasattr(a, "geocode"),
                    usage=lambda a: Usage("apple.geocode"),
                )
            except PisaError as err:
                _log.info("task_place_geocode_skipped", code=err.code)
                return None
            for p in found.value:
                city = city_of(p.address)
                if names_city(text, city):
                    return None if text_key(city) in here else city
        return None

    async def current_cities(self) -> frozenset[str] | None:
        """The names of the user's city in Italian and English, as text keys ({"roma", "rome"}); None when
        unknown. Cached per ~1 km cell, for the process's life."""
        latest = await self.latest_location()
        if not latest:
            return None
        cell = (round(latest[0][0], 2), round(latest[0][1], 2))
        if cell not in self._cities:
            names = {await self.city_at(latest[0], in_lang) for in_lang in CITY_LANGS}
            keys = frozenset(text_key(n) for n in names if n)
            if not keys:
                return None
            self._cities[cell] = keys
        return self._cities[cell]

    async def city_at(self, coord: Coord, lang: str | None = None) -> str | None:
        """The city at a point (a reverse geocode, in `lang` or the user's language), or None when no
        provider can say."""
        if lang is None:
            async with self._pool.connection() as conn:
                lang = await self._owner.lang(conn)
        try:
            choice = await self.providers.choose(
                "search",
                lambda a: a.reverse_geocode(coord, lang=lang),
                can=lambda a: hasattr(a, "reverse_geocode"),
                usage=lambda a: Usage("apple.geocode"),
            )
        except PisaError as err:
            _log.info("current_city_unknown", code=err.code)
            return None
        return city_of(choice.value.address) or None if choice.value else None

    async def resolve_pending(self) -> int:
        """Events and open tasks whose location text changed (or was never tried) get a place, or none.
        At most RESOLVE_PER_RUN texts per run. A provider error parks that text for a day (the others go on);
        no provider configured, or the cap, stops the run. A removed location unlinks its place."""
        now = self._clock()
        async with self._pool.connection() as conn:
            await conn.execute(  # the location was removed
                "UPDATE calendar_events SET place_id = NULL, place_text = NULL "
                "WHERE (location IS NULL OR location = '') "
                "AND (place_id IS NOT NULL OR place_text IS NOT NULL)"
            )
            await conn.execute(
                "UPDATE tasks SET place_id = NULL, place_text_resolved = NULL, place_elsewhere = NULL "
                "WHERE (place_text IS NULL OR place_text = '') "
                "AND (place_id IS NOT NULL OR place_text_resolved IS NOT NULL OR place_elsewhere IS NOT NULL)"
            )
            parked = {
                r[0]
                for r in await (
                    await conn.execute(
                        "SELECT text_key FROM place_resolutions WHERE failed_at > %s", (now - RESOLVE_RETRY,)
                    )
                ).fetchall()
            }
            event_texts = [
                r[0]
                for r in await (
                    await conn.execute(
                        "SELECT DISTINCT location FROM calendar_events WHERE location IS NOT NULL "
                        "AND location <> '' "
                        "AND place_text IS DISTINCT FROM location AND ends_at > %s",
                        (self._clock() - RELATED_PAST,),
                    )
                ).fetchall()
            ]
            task_texts = [
                r[0]
                for r in await (
                    await conn.execute(
                        "SELECT DISTINCT place_text FROM tasks WHERE status = 'open' AND place_text "
                        "IS NOT NULL "
                        "AND place_text <> '' AND place_text_resolved IS DISTINCT FROM place_text"
                    )
                ).fetchall()
            ]
            # "Elsewhere" tasks, with the city they were checked in: checked again from another city.
            elsewhere_rows = await (
                await conn.execute(
                    "SELECT DISTINCT place_text, place_checked_in FROM tasks WHERE status = 'open' "
                    "AND place_elsewhere IS NOT NULL AND place_text_resolved = place_text"
                )
            ).fetchall()
        here: list[frozenset[str] | None] = []  # looked up once, and only when a task text needs it
        if elsewhere_rows:
            here.append(await self.current_cities())
            moved = [t for t, checked in elsewhere_rows if here[0] and checked != city_names_key(here[0])]
            task_texts += [t for t in moved if t not in task_texts]
        done = 0
        jobs = [("event", t) for t in dict.fromkeys(event_texts) if text_key(t) not in parked]
        jobs += [("task", t) for t in dict.fromkeys(task_texts) if "task " + text_key(t) not in parked]
        for kind, text in jobs[:RESOLVE_PER_RUN]:
            elsewhere = None
            try:
                if kind == "event":
                    place_id = await self.resolve_text(text)
                else:
                    if not here:
                        here.append(await self.current_cities())
                    place_id, elsewhere = await self.resolve_task_text(text, here[0])
            except PisaError as err:
                _log.info("place_resolution_postponed", cause=err.code)
                if err.code != "place_provider_error":
                    break  # not configured, or the cap: nothing else would work either
                key = text_key(text) if kind == "event" else "task " + text_key(text)
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO place_resolutions (text_key, place_id, resolved_at, failed_at) "
                        "VALUES (%s, NULL, %s, %s) ON CONFLICT (text_key) DO UPDATE SET failed_at = %s",
                        (key, now, now, now),
                    )
                continue
            async with self._pool.connection() as conn:
                if kind == "event":
                    await conn.execute(
                        "UPDATE calendar_events SET place_id = %s, place_text = location WHERE location = %s",
                        (place_id, text),
                    )
                else:
                    await conn.execute(
                        "UPDATE tasks SET place_id = %s, place_text_resolved = place_text, "
                        "place_elsewhere = %s, place_checked_in = %s WHERE place_text = %s",
                        (place_id, elsewhere, city_names_key(here[0] if here else None), text),
                    )
            done += 1
        return done

    # --- for the briefing and the tools ------------------------------------------------------------------

    async def place_brief(self, place_id: uuid.UUID) -> tuple[ProviderPlace | None, Coord | None]:
        """Cached-or-fetched details without reviews, and the coordinates (None when unknown)."""
        info, _ = await self.details(place_id, reviews=False)
        async with self._pool.connection() as conn:
            row = await self._row(conn, place_id)
        return info, (row["lat"], row["lon"]) if row["lat"] is not None else None


async def prune(conn: AsyncConnection, now: datetime) -> dict[str, int]:
    """Provider terms and Pisa's retention: expired cache rows, expired research, Google coordinates older
    than 30 days, Google-only places nothing refers to after 30 days, resolution memos after 30 days."""
    counts = {}
    cur = await conn.execute("DELETE FROM place_cache WHERE expires_at <= %s", (now,))
    counts["cache"] = cur.rowcount
    cur = await conn.execute("DELETE FROM place_insights WHERE expires_at <= %s", (now,))
    counts["insights"] = cur.rowcount
    cur = await conn.execute("DELETE FROM place_menus WHERE read_at <= %s", (now - INSIGHTS_TTL,))
    counts["menus"] = cur.rowcount
    cur = await conn.execute(
        "UPDATE places SET lat = NULL, lon = NULL, coords_source = NULL, coords_at = NULL "
        "WHERE coords_source = 'google' AND coords_at < %s",
        (now - GOOGLE_COORDS_TTL,),
    )
    counts["google_coords"] = cur.rowcount
    # Google allows only its place ids to be kept: a place known only from Google (its name, address and
    # category are Google's) that nothing of theirs refers to goes after 30 days.
    # a place any of their rows points at (a stay, an episode, a note, a plan change…) is their place: kept.
    cur = await conn.execute(
        "DELETE FROM places p WHERE p.apple_id IS NULL AND p.updated_at < %s AND NOT EXISTS ("
        "SELECT 1 FROM calendar_events r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM tasks r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM stays r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM episodes r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM place_notes r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM plan_adjustments r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM recommendation_outcomes r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM pisa_promises r WHERE r.place_id = p.id UNION ALL "
        "SELECT 1 FROM done_by_presence r WHERE r.place_id = p.id UNION ALL "
        # an episode's link and a claim about the place point at it by text, not by key
        "SELECT 1 FROM episode_links r WHERE r.target_kind = 'place' AND r.target_id = p.id::text UNION ALL "
        "SELECT 1 FROM profile_claims r WHERE r.subject_key = 'place:' || p.id::text)",
        (now - GOOGLE_LABEL_TTL,),
    )
    counts["google_places"] = cur.rowcount
    cur = await conn.execute(
        "DELETE FROM place_resolutions WHERE resolved_at < %s AND failed_at IS NULL", (now - RESOLUTION_TTL,)
    )
    counts["resolutions"] = cur.rowcount
    return counts


def _distinct_near(found: list[ProviderPlace], reference: Coord | None) -> list[ProviderPlace]:
    """The results within RESOLVE_RADIUS_M (any distance when no location is known), the same place listed
    twice (two entrances, a duplicate) once."""
    distinct: list[ProviderPlace] = []
    for p in found:
        if p.lat is None or p.lon is None:
            continue
        if reference is not None and distance_m(reference, (p.lat, p.lon)) > RESOLVE_RADIUS_M:
            continue
        if not any(distance_m((p.lat, p.lon), (q.lat, q.lon)) < SAME_PLACE_M for q in distinct):  # type: ignore[arg-type]
            distinct.append(p)
    return distinct
