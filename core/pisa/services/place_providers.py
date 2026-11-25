"""Place providers: keys, status and tests, usage, the monthly cap, and the choice of
provider for each request.

**Choice**: for a job (routes, search, details, web) Pisa walks the order and uses the first provider that is
configured, can do the request (a transport mode, a filter) and is under the cap. On an error it tries the
next. Every provider skipped, and why, is logged and returned to the caller, which names it in the answer's
`sources`. The order (`order_for`), for a place search and a travel time:

1. **the phone's own maps** (`phone`, contract 2.3's `pisa.maps`): only in a turn on an app that granted it
   (`services.phone_maps.current`); no key. Its no or its silence (4 s) is never the answer's failure: the
   next provider is asked;
2. **the user's order** (settings `places`: Apple Maps and Google Maps, each with its key);
3. **the free fallback**: OpenStreetMap for a search (`osm`, no key; PISA_OSM_ENABLED), OpenRouteService for a
   travel time (`openrouteservice`, the user's own key; no transit). A fallback that isn't set up is passed
   over silently;
4. none of them ready: `place_provider_not_configured` with the detail `provider_not_configured` (the answer
   says plainly that places aren't set up).

**Cap**: every HTTP call is recorded in `place_calls` by SKU. Before a call that would cost money (beyond the
free monthly allowance) and cross `monthly_cap_eur` this month (local), the provider is skipped, and the
user gets one `notify` a day naming the cap.

**The night's budget** (b): the night's web searches (`night_search`) are `place_calls` with
budget `night`: never through `choose`, never counted by the user's cap and never their cap notice; they have
their own monthly cap (the free tier counts for them first) and the hard limit counts them.

**The paid OK** (`domain.connections`): a provider that bills beyond a free allowance (Google Maps, Tavily) is
never called, its test included, until the user acknowledged it and its own monthly cap (`paid_acks`, a signed
`paid_ack`). `adapter` is the one door every call goes through (`choose`, `night_search`, `test`): a paid
provider with a key and no OK raises `place_provider_not_configured` with the detail `paid_ack_required`,
which `choose` passes over like a provider not set up, and names plainly when nothing else could do the job.
Its cap counts every budget's calls to that provider this month (the real bill), checked with the reservation.

**Reservations**: a priced call reserves its units first, as a `place_calls` row with
`reserved = true`, under one advisory lock and after a cap check that counts the reservations still in
flight; so two paid calls racing at the cap can't both pass. The reservation goes when the call ends (the
call itself is then recorded), and one older than 5 minutes (a process that stopped mid-call) is cleared.

Accepted limit: the month is the user's (their time zone), while Google bills by Pacific
time, so the first and last hours of a month may count on the other side.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.apple_maps import AppleMaps, MapsKey
from pisa.connectors.google_maps import GoogleMaps
from pisa.connectors.openrouteservice import ORS_URL, OpenRouteService
from pisa.connectors.osm import OsmConfig, OsmPlaces
from pisa.connectors.tavily import Tavily
from pisa.db.settings import get_setting, set_setting
from pisa.domain import connections as ack_rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.place_prices import PRICES, extra_cost_usd, month_cost_usd
from pisa.infra import hard_limit, locks
from pisa.infra.fx import usd_to_eur
from pisa.infra.hard_limit import HardLimits
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.ports.places import PlaceProviderError, Usage
from pisa.services import phone_maps
from pisa.services.notifications import enqueue
from pisa.services.push import apple_setting, check_p8, normalize_p8

Job = Literal["routes", "search", "details", "web"]
ProviderId = Literal["apple", "google", "tavily", "osm", "openrouteservice"]
MAPS_SECRET = "maps"  # noqa: S105 (the row name in server_secrets, not a secret)
CAP_NOTIFIED_KEY = "places_cap_notified_on"
RESERVATION_STALE = timedelta(minutes=5)


@dataclass(frozen=True)
class PlaceProvider:
    """What a places provider does and how its key is kept: the services ask this, never the provider's id."""

    name: str
    jobs: tuple[Job, ...]
    # Its adapter from (http, key, meter, **the service's options for it), for a provider whose key is an API
    # key in `place_providers` (a keyless one gets "" for the key); None: its key is an Apple Maps signing key
    # (the .p8 text and its key id) in `server_secrets`.
    adapter: Callable[..., Any] | None = None
    paid_test: bool = False  # its test call can spend a paid credit: the cap is checked first
    # the `places` column of its place ids
    id_column: Literal["apple_id", "google_place_id", "osm_id"] | None = None
    cache_24h: bool = False  # its content may be kept 24 hours at most (Google's terms)
    keyless: bool = False  # no key at all (OpenStreetMap): configured when the operator has it on
    fallback: bool = (
        False  # never in the user's order: asked after it (`order_for`), passed over when not set up
    )

    @property
    def signing_key(self) -> bool:
        return self.adapter is None


PROVIDERS: dict[str, PlaceProvider] = {
    "apple": PlaceProvider("Apple Maps", ("routes", "search"), id_column="apple_id"),
    "google": PlaceProvider(
        "Google Maps",
        ("routes", "search", "details"),
        GoogleMaps,
        id_column="google_place_id",
        cache_24h=True,
    ),
    "tavily": PlaceProvider("Tavily", ("web",), Tavily, paid_test=True),
    # the free fallbacks, after the user's order: OpenStreetMap (no key) and OpenRouteService (the user's key)
    "osm": PlaceProvider(
        "OpenStreetMap",
        ("search",),
        lambda http, _key, meter, **o: OsmPlaces(http, meter, config=o.get("config")),
        id_column="osm_id",
        keyless=True,
        fallback=True,
    ),
    "openrouteservice": PlaceProvider(
        "OpenRouteService",
        ("routes",),
        lambda http, key, meter, **o: OpenRouteService(http, key, meter, base_url=o.get("base_url", ORS_URL)),
        fallback=True,
    ),
}
# the phone's own maps (contract 2.3): first for these jobs, in a turn on an app that granted `pisa.maps`
PHONE_JOBS: tuple[Job, ...] = ("search", "routes")
JOB_PHRASE = {
    "routes": "travel times",
    "search": "place search",
    "details": "place details",
    "web": "web research",
}
CAP_TEXT = {
    "it-IT": "Ho raggiunto il limite mensile di {cap} per mappe e ricerche web: fino a fine mese uso solo i "
    "servizi gratuiti.",
    "en-US": "The {cap} monthly cap for maps and web research is reached: until the end of the month Pisa "
    "uses only free services.",
}
_log = get_logger("pisa.places.providers")


def _job_reason(errors: list[PlaceProviderError | PisaError], skipped: list[str], *, capped: bool) -> str:
    """Why a place job found no provider, as a code (no provider's or user's text, for the log):
    `provider_not_configured` (no provider has a key), `quota`, `timeout`, `unreachable`,
    `provider_error:<status>`, `provider_error`, `key_error` (a stored key no longer opens), `cap_reached` or
    `unsupported` (no provider can do this request)."""
    if errors:
        err = errors[-1]
        if isinstance(err, PisaError):
            return "key_error"
        if err.quota:
            return "quota"
        if "Timeout" in err.message:  # "Google Maps is unreachable (ReadTimeout)."
            return "timeout"
        if err.status is not None:
            return f"provider_error:{err.status}"
        return "unreachable" if "unreachable" in err.message else "provider_error"
    if capped:
        return "cap_reached"
    # the phone's no or silence isn't a setup: what is left decides
    rest = [s for s in skipped if not s.startswith(f"{phone_maps.PROVIDER}:")]
    if rest and all(s.endswith(": not configured") for s in rest):
        return "provider_not_configured"
    return "unsupported"


def _providers(skipped: list[str]) -> list[str]:
    """The providers a job skipped, for a log line: names only, never a reason (a provider's reason may name
    the place that was asked about)."""
    return [s.split(":", 1)[0] for s in skipped]


@dataclass
class Choice[T]:
    """What a request got: the value, the provider that gave it, and the providers skipped on the way."""

    value: T
    provider: str
    skipped: list[str] = field(default_factory=list)


async def set_maps_key(conn: AsyncConnection, keys: KeyStore, key_id: str, pem: str) -> None:
    """Store the Apple Maps .p8 (Fernet-encrypted) next to its key id (not secret)."""
    key_id = key_id.strip()
    if not key_id.isalnum() or len(key_id) != 10:
        raise PisaError(
            "validation_failed", "An Apple Maps key id is 10 letters and digits, e.g. AB12CD34EF."
        )
    pem = normalize_p8(pem)
    try:
        check_p8(pem)
    except PisaError:
        raise PisaError(
            "validation_failed",
            "That is not an Apple Maps key: expected the text of the .p8 file (BEGIN PRIVATE KEY …).",
            "Paste the whole .p8 text, including the BEGIN and END lines",
        ) from None
    await conn.execute(
        "INSERT INTO server_secrets (name, ciphertext, meta, set_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (name) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, meta = EXCLUDED.meta, "
        "set_at = now()",
        (MAPS_SECRET, keys.seal(pem), Jsonb({"key_id": key_id})),
    )
    await conn.execute(
        "INSERT INTO place_providers (provider, set_at) VALUES ('apple', now()) ON CONFLICT "
        "(provider) DO UPDATE "
        "SET set_at = now(), last_test_ok = NULL, last_test_at = NULL, last_test_message = NULL"
    )
    _log.info("maps_key_set", key_id=key_id)


def _ack_needed(provider: str) -> PisaError:
    name = PROVIDERS[provider].name
    return PisaError(
        "place_provider_not_configured",
        f"{name} costs money and waits for your OK of its monthly cap in Settings → Connections: Pisa "
        "doesn't call it until then.",
        ack_rules.ACK_ACTION,
        detail=ack_rules.ACK_DETAIL,
        extra={"reason": ack_rules.ACK_REASON, "provider": provider},
    )


def _unknown(provider: str) -> PisaError:
    return PisaError(
        "not_found",
        f"There is no place provider {provider[:40]!r}.",
        "Use apple, google, tavily, osm or openrouteservice",
    )


class PlaceProviderService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        keys: KeyStore,
        http: httpx.AsyncClient,
        *,
        team_id: str | None,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        hard_limits: HardLimits = hard_limit.DEFAULT,
        disabled: bool = False,
        osm: OsmConfig | None = None,
        ors_url: str = ORS_URL,
    ) -> None:
        self._pool = pool
        self.disabled = disabled  # App Store review mode: no provider is ever called, as if none were set up
        # OpenStreetMap's backends (PISA_OSM_*); None: OSM is off (PISA_OSM_ENABLED=0, or not wired)
        self._osm = osm
        self._options: dict[str, dict[str, Any]] = {
            "osm": {"config": osm},
            "openrouteservice": {"base_url": ors_url},
        }
        self._keys = keys
        self._http = http
        self._team_id = team_id
        self._hard = hard_limits
        self._owner = owner
        self._clock = clock
        self._adapters: dict[str, tuple[Any, Any]] = {}  # provider → (version, adapter): rebuilt on a new key

    # --- metering ----------------------------------------------------------------------------------------

    async def meter(self, provider: str, usage: Usage, ok: bool, budget: str = "owner") -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO place_calls (provider, sku, units, at, ok, budget) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (provider, usage.sku, usage.units, self._clock(), ok, budget),
            )

    def _meter_for(self, provider: str, budget: str = "owner") -> Callable[[Usage, bool], Awaitable[None]]:
        async def meter(usage: Usage, ok: bool) -> None:
            await self.meter(provider, usage, ok, budget)

        return meter

    def _month_start(self) -> datetime:
        local = self._owner.local(self._clock())
        return datetime(local.year, local.month, 1, tzinfo=self._owner.zone)

    async def _units_this_month(self, conn: AsyncConnection, budget: str | None = None) -> dict[str, int]:
        """This month's units by SKU: every budget's (the real bill), or one budget's (the user's cap
        counts their calls only, so the night's searches never refuse their day)."""
        rows = await (
            await conn.execute(
                # In-flight reservations count too: the cap holds for calls still running.
                "SELECT sku, sum(units) FROM place_calls WHERE at >= %s "
                "AND (%s::text IS NULL OR budget = %s) "
                "GROUP BY sku",
                (self._month_start(), budget, budget),
            )
        ).fetchall()
        return {sku: int(units) for sku, units in rows}

    async def usage(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
            fx = await usd_to_eur(conn)
            units = await self._units_this_month(conn)
        items = []
        spent = Decimal(0)
        for sku, used in sorted(units.items()):
            price = PRICES.get(sku)
            if price is None:
                continue
            cost = month_cost_usd(sku, used) * fx
            spent += cost
            free = used if price.free_per_month is None else min(used, price.free_per_month)
            items.append(
                {
                    "provider": price.provider,
                    "sku": sku,
                    "calls": used,
                    "free_calls": free,
                    "free_allowance": price.free_per_month,  # None: no paid tier (always free)
                    "unit_eur": float(round(price.usd_per_1000 * fx / 1000, 6)),
                    "cost_eur": float(round(cost, 4)),
                }
            )
        local = self._owner.local(self._clock())
        return {
            "month": f"{local:%Y-%m}",
            "cap_eur": float(settings["places"]["monthly_cap_eur"]),
            "spent_eur": float(round(spent, 4)),
            "items": items,
        }

    async def _over_cap(self, usage: Usage, cap_eur: float) -> bool:
        """Would this call cost money and take the month past the cap?"""
        async with self._pool.connection() as conn:
            return await self._over_cap_in(conn, usage, cap_eur)

    async def _over_cap_in(self, conn: AsyncConnection, usage: Usage, cap_eur: float) -> bool:
        units = await self._units_this_month(conn, "owner")  # their cap, their calls (never the night's)
        fx = await usd_to_eur(conn)
        extra = extra_cost_usd(usage.sku, units.get(usage.sku, 0), usage.units) * fx
        if extra <= 0:
            return False
        spent = (
            sum((month_cost_usd(sku, used) for sku, used in units.items() if sku in PRICES), Decimal(0)) * fx
        )
        return spent + extra > Decimal(str(cap_eur))

    async def _reserve(
        self, provider: str, usage: Usage, cap_eur: float
    ) -> tuple[Literal["cap", "ack_cap"] | None, int | None]:
        """(the cap it would pass, the reservation's row): a priced call holds its units in place_calls while
        it runs, under one lock, so two calls racing at a cap can't both pass. `cap`: the user's cap for maps
        and the web; `ack_cap`: the provider's own cap, the one the user acknowledged (`paid_acks`)."""
        price = PRICES.get(usage.sku)
        if price is None or price.free_per_month is None:  # never billed: nothing to hold
            return None, None
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.PLACES_CAP,))
            await conn.execute(  # a process that stopped mid-call left its reservation behind
                "DELETE FROM place_calls WHERE reserved AND at < %s", (now - RESERVATION_STALE,)
            )
            if await self._over_cap_in(conn, usage, cap_eur):
                return "cap", None
            if await self._over_ack_cap(conn, provider, usage):
                return "ack_cap", None
            extra = extra_cost_usd(
                usage.sku, (await self._units_this_month(conn)).get(usage.sku, 0), usage.units
            )
            hard = await hard_limit.refusal(
                conn, extra_eur=extra * await usd_to_eur(conn), now=now, tz=self._owner.tz, hard=self._hard
            )
            if hard is not None:  # the ceiling over every paid service; the user's cap can't pass it
                raise hard
            row = await (
                await conn.execute(
                    "INSERT INTO place_calls (provider, sku, units, at, ok, reserved) "
                    "VALUES (%s, %s, %s, %s, false, true) RETURNING id",
                    (provider, usage.sku, usage.units, now),
                )
            ).fetchone()
        return None, row[0] if row else None

    # --- the paid OK: each paid provider's own cap ----------------------------------------------------------

    async def _ack_cap(self, conn: AsyncConnection, provider: str) -> int | None:
        """The monthly cap in cents the user acknowledged for this provider; None: not acknowledged."""
        row = await (
            await conn.execute("SELECT cap_cents FROM paid_acks WHERE provider = %s", (provider,))
        ).fetchone()
        return int(row[0]) if row else None

    async def _provider_spent_eur(
        self, conn: AsyncConnection, provider: str, extra: Usage | None = None
    ) -> Decimal:
        """This month's bill of one provider, every budget's calls (with `extra` more), in EUR."""
        units = await self._units_this_month(conn)
        if extra is not None:
            units[extra.sku] = units.get(extra.sku, 0) + extra.units
        usd = sum(
            (
                month_cost_usd(sku, n)
                for sku, n in units.items()
                if sku in PRICES and PRICES[sku].provider == provider
            ),
            Decimal(0),
        )
        return usd * await usd_to_eur(conn)

    async def _over_ack_cap(self, conn: AsyncConnection, provider: str, usage: Usage) -> bool:
        """Would this call take the provider past the cap the user acknowledged for it (or is there none)?"""
        if provider not in ack_rules.ACKED:
            return False
        cap = await self._ack_cap(conn, provider)
        if cap is None:  # the OK was withdrawn while the call was being chosen
            return True
        if (
            extra_cost_usd(usage.sku, (await self._units_this_month(conn)).get(usage.sku, 0), usage.units)
            <= 0
        ):
            return False  # inside the free allowance: costs nothing
        return await self._provider_spent_eur(conn, provider, usage) > Decimal(cap) / 100

    async def acks(self) -> dict[str, dict[str, Any]]:
        """Each acknowledged provider: its cap in cents, when, and this month's spend on it in cents."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT provider, cap_cents, acked_at FROM paid_acks")
            ).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for provider, cap, at in rows:
                out[provider] = {"cap_cents": int(cap), "at": at.isoformat()}
            for provider in ack_rules.ACKED:
                spent = await self._provider_spent_eur(conn, provider)
                out.setdefault(provider, {})["spent_cents"] = int(
                    (spent * 100).to_integral_value(ROUND_CEILING)
                )
        return out

    def hard_monthly_cents(self) -> int:
        return int(self._hard.monthly_eur * 100)

    async def acknowledge(self, provider: str, cap_cents: int, device_id: Any = None) -> dict[str, Any]:
        """The user's OK of a paid provider and its monthly cap (signed by the caller first)."""
        ack_rules.check_ack(provider, cap_cents, self.hard_monthly_cents())
        now = self._clock()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO paid_acks (provider, cap_cents, acked_at, device_id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (provider) DO UPDATE SET cap_cents = EXCLUDED.cap_cents, acked_at = "
                "EXCLUDED.acked_at, device_id = EXCLUDED.device_id",
                (provider, cap_cents, now, device_id),
            )
        _log.info("paid_ack_set", provider=provider, cap_cents=cap_cents)
        return {"cap_cents": cap_cents, "at": now.isoformat()}

    async def withdraw(self, provider: str) -> None:
        """The OK withdrawn: the provider is not called again until a new one."""
        if provider not in ack_rules.ACKED:
            raise _unknown(provider)
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM paid_acks WHERE provider = %s", (provider,))
        _log.info("paid_ack_withdrawn", provider=provider)

    # --- the night's own web budget -------------------------------------------------------------------------

    async def _night_spent_eur(self, conn: AsyncConnection, extra: Usage | None = None) -> Decimal:
        """What the night's calls add to this month's real bill (with `extra` more): the bill over every
        budget's units, less the bill their own units alone would make. The free tier goes to them first."""
        fx = await usd_to_eur(conn)
        every = await self._units_this_month(conn)
        if extra is not None:
            every[extra.sku] = every.get(extra.sku, 0) + extra.units
        his = await self._units_this_month(conn, "owner")

        def bill(units: dict[str, int]) -> Decimal:
            return sum((month_cost_usd(sku, n) for sku, n in units.items() if sku in PRICES), Decimal(0)) * fx

        return bill(every) - bill(his)

    async def night_search(self, query: str, *, max_results: int, cap_eur: Decimal) -> list[Any] | None:
        """RAIL (b): one web search on the night's own budget, never `choose`: the user's
        provider order, cap check and cap notice are their day's. Tavily only. None when Tavily isn't set up,
        when it would pass the night's month cap (`cap_eur`) or the hard limit: never a notice to them.
        Its calls are `place_calls` rows with budget `night`, which their cap does not count."""
        try:
            adapter = await self.adapter("tavily", budget="night")
        except PisaError as err:  # no paid OK (or a key that no longer opens): the night does without
            _log.info("night_search_refused", detail=err.detail or err.code)
            return None
        if adapter is None:
            return None
        usage = adapter.usage_for_search()
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.PLACES_CAP,))
            if await self._night_spent_eur(conn, usage) > cap_eur:
                _log.info("night_places_cap_reached")
                return None
            if await self._over_ack_cap(conn, "tavily", usage):  # Tavily's own cap, every budget's calls
                _log.info("night_paid_ack_cap_reached")
                return None
            extra = extra_cost_usd(
                usage.sku, (await self._units_this_month(conn)).get(usage.sku, 0), usage.units
            )
            if await hard_limit.refusal(
                conn, extra_eur=extra * await usd_to_eur(conn), now=now, tz=self._owner.tz, hard=self._hard
            ):
                _log.info("night_hard_limit_reached")
                return None
            row = await (
                await conn.execute(
                    "INSERT INTO place_calls (provider, sku, units, at, ok, reserved, budget) "
                    "VALUES ('tavily', %s, %s, %s, false, true, 'night') RETURNING id",
                    (usage.sku, usage.units, now),
                )
            ).fetchone()
        try:
            return list(await adapter.search(query, max_results=max_results))
        except PlaceProviderError as err:
            _log.warning("night_search_failed", status=err.status, quota=err.quota)
            return None
        finally:
            await asyncio.shield(self._release(row[0] if row else None))

    async def _release(self, reservation: int | None) -> None:
        if reservation is not None:
            async with self._pool.connection() as conn:
                await conn.execute("DELETE FROM place_calls WHERE id = %s AND reserved", (reservation,))

    async def _cap_reached(self, cap_eur: float, lang: str) -> None:
        """One notify a day naming the cap."""
        now = self._clock()
        today = now.astimezone(self._owner.zone).date().isoformat()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (CAP_NOTIFIED_KEY,))
            if await get_setting(conn, CAP_NOTIFIED_KEY) == today:
                return
            await set_setting(conn, CAP_NOTIFIED_KEY, Jsonb(today))
            cap = f"€{cap_eur:.2f}" if lang == "en-US" else f"{cap_eur:.2f} €".replace(".", ",")
            await enqueue(
                conn,
                kind="alert",
                text=CAP_TEXT[lang].format(cap=cap),
                lang=lang,
                now=now,
                timezone=self._owner.tz,
                path="places.cap",
            )
        _log.warning("places_cap_reached", cap_eur=cap_eur)

    # --- adapters ----------------------------------------------------------------------------------------

    async def adapter(self, provider: str, budget: str = "owner") -> Any:
        """The provider's adapter, or None when it is not configured. `budget` "night": a fresh one whose
        calls are metered on the night's budget, never cached beside theirs. None for every provider in
        review mode (PISA_REVIEW_MODE). `phone`: this turn's phone (`phone_maps.current`), None outside a turn
        on an app that granted `pisa.maps`; `osm`: None while OpenStreetMap is off."""
        if self.disabled:
            return None
        if provider == phone_maps.PROVIDER:
            delegate = phone_maps.current.get()
            return phone_maps.PhoneMapsAdapter(delegate, self._clock) if delegate is not None else None
        info = PROVIDERS[provider]
        build = info.adapter
        if info.keyless:
            if self._osm is None:
                return None
            cached = self._adapters.get(provider)
            if budget == "owner" and cached is not None:
                return cached[1]
            built = build(
                self._http, "", self._meter_for(provider, budget), **self._options.get(provider, {})
            )  # type: ignore[misc]
            if budget == "owner":
                self._adapters[provider] = (None, built)
            return built
        async with self._pool.connection() as conn:
            if build is None:  # the Apple Maps signing key
                row = await (
                    await conn.execute(
                        "SELECT ciphertext, meta, set_at FROM server_secrets WHERE name = %s", (MAPS_SECRET,)
                    )
                ).fetchone()
                if row is None:
                    return None
                cached = self._adapters.get(provider)
                if cached is not None and cached[0] == row[2]:
                    return cached[1]
                team_id = apple_setting(
                    self._team_id, "PISA_APNS_TEAM_ID", "Apple team id", "place_provider_not_configured"
                )
                key = MapsKey(str(row[1]["key_id"]), team_id, self._keys.unseal(bytes(row[0])))
                built: Any = AppleMaps(self._http, key, self._meter_for("apple"))
                self._adapters[provider] = (row[2], built)
                return built
            row = await (
                await conn.execute(
                    "SELECT ciphertext, set_at FROM place_providers WHERE provider = %s AND "
                    "ciphertext IS NOT NULL",
                    (provider,),
                )
            ).fetchone()
            if row is None:
                return None
            if provider in ack_rules.ACKED and await self._ack_cap(conn, provider) is None:
                # RAIL: a paid provider is never called without the user's OK of it and its cap
                raise _ack_needed(provider)
            cached = self._adapters.get(provider)
            if budget == "owner" and cached is not None and cached[0] == row[1]:
                return cached[1]
            secret = self._keys.unseal(bytes(row[0]))
            built = build(
                self._http, secret, self._meter_for(provider, budget), **self._options.get(provider, {})
            )
            if budget == "owner":
                self._adapters[provider] = (row[1], built)
            return built

    async def choose[T](
        self,
        job: Job,
        do: Callable[[Any], Awaitable[T]],
        *,
        can: Callable[[Any], bool] = lambda adapter: True,
        usage: Callable[[Any], Usage],
        found: Callable[[T], bool] = lambda value: True,
        only: str | None = None,
    ) -> Choice[T]:
        """Run `do` on the first suitable provider for `job` (see the module docstring). A result `found`
        rejects (a search with no results) goes on to the next provider; when none finds anything, the last
        empty result is the answer. `only`: that provider alone (the model asked for it), if the order for the
        job has it."""
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
        order = self.order_for(job, settings["places"][job])
        if only is not None:
            if only not in order:
                listed = ", ".join(order) or "none"
                raise PisaError(
                    "place_provider_not_configured",
                    f"{only} isn't one of the user's providers for {JOB_PHRASE[job]} ({listed}).",
                    "Use one of those, or leave the provider out",
                )
            order = [only]
        cap, lang = settings["places"]["monthly_cap_eur"], settings["language"]
        if not order:
            raise PisaError(
                "place_provider_not_configured",
                f"{JOB_PHRASE[job].capitalize()} is switched off (Settings → Places has no provider for it).",
                "Choose a provider in Settings → Places",
            )
        skipped: list[str] = []
        causes: list[str] = []
        errors: list[PlaceProviderError | PisaError] = []  # each cause's own error, for the log's reason code
        capped = False
        ack_capped: list[str] = []  # the providers past the cap the user acknowledged for each
        unacked: list[str] = []  # paid providers with a key that wait for the user's OK (never called)
        empty: Choice[T] | None = None  # the last provider that answered but found nothing
        for provider in order:
            try:
                adapter = await self.adapter(provider)
            except PisaError as err:  # e.g. its key no longer decrypts: that provider only is out
                if err.detail == ack_rules.ACK_DETAIL:  # a paid provider without the user's OK: not called
                    skipped.append(f"{provider}: waits for the paid OK")
                    unacked.append(PROVIDERS[provider].name)
                    continue
                skipped.append(f"{provider}: {err.message}")
                causes.append(err.message)
                errors.append(err)
                continue
            if adapter is None:
                if provider not in PROVIDERS or not PROVIDERS[provider].fallback:  # a fallback: passed over
                    skipped.append(f"{provider}: not configured")
                continue
            if not can(adapter):
                skipped.append(f"{provider}: can't do this request")
                continue
            over, reservation = await self._reserve(provider, usage(adapter), cap)
            if over == "ack_cap":
                skipped.append(f"{provider}: its own monthly cap reached")
                ack_capped.append(PROVIDERS[provider].name)
                continue
            if over:
                skipped.append(f"{provider}: monthly cap reached")
                capped = True
                await self._cap_reached(cap, lang)
                continue
            try:
                value = await do(adapter)
            except PlaceProviderError as err:
                if provider == phone_maps.PROVIDER:  # the phone's no or silence: the next one is asked
                    skipped.append(f"{provider}: {err.message.rsplit(': ', 1)[-1]}")
                    _log.info("place_provider_phone_passed", job=job)
                    continue
                skipped.append(f"{provider}: {err.message}")
                causes.append(err.message)
                errors.append(err)
                _log.warning(
                    "place_provider_failed", job=job, provider=provider, status=err.status, quota=err.quota
                )
                continue
            finally:
                await asyncio.shield(self._release(reservation))
            if not found(value):
                skipped.append(f"{provider}: found nothing")
                empty = Choice(value, provider, list(skipped))
                continue
            if skipped:
                _log.info("place_providers_skipped", job=job, used=provider, skipped=_providers(skipped))
            return Choice(value, provider, skipped)
        if empty is not None:
            _log.info("place_job_empty", job=job, skipped=_providers(skipped))
            return empty
        reason = _job_reason(errors, skipped, capped=capped or bool(ack_capped))
        if unacked and not (causes or capped or ack_capped):  # set up, waiting for the user's OK
            reason = ack_rules.ACK_DETAIL
        if reason in ("provider_not_configured", ack_rules.ACK_DETAIL):  # the owner's setup: told plainly
            _log.info("place_job_unconfigured", job=job, reason=reason)
        else:
            _log.warning("place_job_failed", job=job, reason=reason, skipped=_providers(skipped))
        if causes:
            cause = causes[-1].rstrip(".")
            raise PisaError(
                "place_provider_error", f"{JOB_PHRASE[job].capitalize()} failed ({cause}).", "Try again"
            )
        if capped:
            raise PisaError(
                "places_cap_reached",
                f"The €{cap:.2f} monthly cap for maps and web research is reached, and no free provider "
                f"can do this ({'; '.join(skipped)}).",
                "Raise the cap in Settings → Places, or wait for next month",
            )
        if ack_capped:
            raise PisaError(
                "places_cap_reached",
                f"The monthly cap you acknowledged for {' and '.join(ack_capped)} is reached, and no free "
                f"provider can do this ({'; '.join(skipped)}).",
                "Raise it in Settings → Connections, or wait for next month",
            )
        if unacked:  # set up, but waiting for the user's OK of its cost: told plainly, no failure
            raise PisaError(
                "place_provider_not_configured",
                ack_rules.ack_needed_message(unacked, JOB_PHRASE[job]),
                ack_rules.ACK_ACTION,
                detail=ack_rules.ACK_DETAIL,
                extra={"reason": ack_rules.ACK_REASON},
            )
        raise PisaError(
            "place_provider_not_configured",
            f"No provider for {JOB_PHRASE[job]} is ready ({'; '.join(skipped)}).",
            "Add a key in Settings → Places",
            detail=reason,  # provider_not_configured: no provider has a key (`failed_block` says it plainly)
        )

    def order_for(self, job: Job, users: list[str]) -> list[str]:
        """The providers asked for `job`, in order: the phone's maps (in a turn that has them, for a search or
        a travel time), the user's order, then each free fallback for the job (OpenStreetMap for a search
        while it is on, OpenRouteService for a travel time)."""
        first = (
            [phone_maps.PROVIDER]
            if job in PHONE_JOBS and phone_maps.current.get() is not None and not self.disabled
            else []
        )
        fallbacks = [
            p for p, info in PROVIDERS.items() if info.fallback and job in info.jobs and p not in users
            and (not info.keyless or self._osm is not None)
        ]  # fmt: skip
        return [*first, *users, *fallbacks]

    # --- provider keys, status and usage --------------------------------------------------------------------

    async def unset(self) -> list[str]:
        """What no provider here can do yet, as said to the user ("place search", "travel times", "web
        research"): no key set for any provider of that job, or review mode (none is ever called)."""
        configured = set() if self.disabled else {s["id"] for s in await self.statuses() if s["configured"]}
        jobs = ("search", "routes", "web")
        return [JOB_PHRASE[j] for j in jobs if not any(j in PROVIDERS[p].jobs for p in configured)]

    async def statuses(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = {
                r[0]: r[1:]
                for r in await (
                    await conn.execute(
                        "SELECT provider, ciphertext IS NOT NULL, last4, last_test_ok, last_test_at, "
                        "last_test_message FROM place_providers"
                    )
                ).fetchall()
            }
            maps = await (
                await conn.execute("SELECT meta FROM server_secrets WHERE name = %s", (MAPS_SECRET,))
            ).fetchone()
        out = []
        for provider, info in PROVIDERS.items():
            has_key, last4, ok, at, message = rows.get(provider, (False, None, None, None, None))
            if info.signing_key:
                has_key, last4 = maps is not None, str(maps[0]["key_id"])[-4:] if maps else None
            if info.keyless:  # no key: ready while the operator has it on
                has_key, last4 = self._osm is not None and not self.disabled, None
            out.append(
                {
                    "id": provider,
                    "name": info.name,
                    "configured": bool(has_key),
                    "key_last4": last4 if has_key else None,
                    "capabilities": list(info.jobs),
                    "last_test": {"ok": ok, "at": at.isoformat(), "message": message} if at else None,
                }
            )
        return out

    async def set_key(self, provider: str, key: str, key_id: str | None = None) -> None:
        if provider not in PROVIDERS:
            raise _unknown(provider)
        if self.disabled:
            raise PisaError(
                "connector_off",
                "This is a demo server (App Store review mode): it calls no maps or web provider.",
                "Nothing to set up here",
            )
        if PROVIDERS[provider].keyless:
            raise PisaError(
                "validation_failed",
                f"{PROVIDERS[provider].name} needs no key: it is free, and on unless the server's operator "
                "switched it off.",
                "Nothing to set up here",
            )
        if PROVIDERS[provider].signing_key:
            # The user pastes the .p8 text and its key id in the app (note; also `pisa secrets set-maps`).
            if not key_id:
                raise PisaError(
                    "validation_failed",
                    "The Apple Maps key needs its key id too (10 letters and digits, e.g. FGHIJ67890).",
                    "Fill in the key id next to the .p8 text",
                )
            async with self._pool.connection() as conn:
                await set_maps_key(conn, self._keys, key_id, key)
            return
        key = key.strip()
        if len(key) < 8 or any(c.isspace() for c in key):
            raise PisaError(
                "validation_failed",
                "That doesn't look like an API key: it is too short or contains spaces.",
                "Paste the whole key as shown by the provider",
            )
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO place_providers (provider, ciphertext, last4, set_at) VALUES (%s, %s, "
                "%s, now()) "
                "ON CONFLICT (provider) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, last4 = "
                "EXCLUDED.last4, "
                "set_at = now(), last_test_ok = NULL, last_test_at = NULL, last_test_message = NULL",
                (provider, self._keys.seal(key), key[-4:]),
            )
        _log.info("place_key_set", provider=provider)

    async def delete_key(self, provider: str) -> None:
        """The key goes. A paid provider's OK stays (it is of the provider and its cap, not of a key): the
        user withdraws it apart (`withdraw`)."""
        if provider not in PROVIDERS:
            raise _unknown(provider)
        if PROVIDERS[provider].keyless:
            raise PisaError(
                "validation_failed",
                f"{PROVIDERS[provider].name} has no key to delete: it is free, and on unless the server's "
                "operator switched it off.",
                "Nothing to delete here",
            )
        async with self._pool.connection() as conn:
            if PROVIDERS[provider].signing_key:
                await conn.execute("DELETE FROM server_secrets WHERE name = %s", (MAPS_SECRET,))
            await conn.execute("DELETE FROM place_providers WHERE provider = %s", (provider,))
        self._adapters.pop(provider, None)
        _log.info("place_key_deleted", provider=provider)

    async def test(self, provider: str) -> dict[str, Any]:
        """One real call of the provider's cheapest kind; the result is stored as last_test."""
        if provider not in PROVIDERS:
            raise _unknown(provider)
        adapter = await self.adapter(provider)
        if adapter is None and PROVIDERS[provider].keyless:
            raise PisaError(
                "place_provider_not_configured",
                f"{PROVIDERS[provider].name} is off on this server, so there is nothing to test.",
                "The server's operator turns it on (PISA_OSM_ENABLED)",
            )
        if adapter is None:
            raise PisaError(
                "place_provider_not_configured",
                f"{PROVIDERS[provider].name} has no key yet, so there is nothing to test.",
                f"Add the {PROVIDERS[provider].name} key in Settings → Places & search",
            )
        if PROVIDERS[provider].paid_test:  # Tavily's (Google's and Apple's are free)
            async with self._pool.connection() as conn:
                cap = float((await self._owner.settings(conn))["places"]["monthly_cap_eur"])
            if await self._over_cap(adapter.usage_for_search(), cap):
                raise PisaError(
                    "places_cap_reached",
                    "Testing Tavily would spend a paid credit past the monthly cap for maps and the web.",
                    "Raise the cap in Settings → Places & search, or test next month",
                )
        try:
            ok, message = True, await adapter.test()
        except PlaceProviderError as err:
            ok, message = False, err.message
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO place_providers (provider, last_test_ok, last_test_at, last_test_message) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (provider) DO UPDATE SET last_test_ok = "
                "EXCLUDED.last_test_ok, "
                "last_test_at = EXCLUDED.last_test_at, last_test_message = EXCLUDED.last_test_message",
                (provider, ok, self._clock(), message),
            )
        _log.info("place_key_tested", provider=provider, ok=ok)
        return {"ok": ok, "message": message}
