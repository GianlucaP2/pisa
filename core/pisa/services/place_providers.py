"""Place providers: keys, status and tests, usage, the monthly cap, and the choice of
provider for each request.

**Choice**: for a job (routes, search, details, web) Pisa walks the user's order (settings `places`) and uses
the first provider that is configured, can do the request (a transport mode, a filter) and is under the cap.
On an error it tries the next. Every provider skipped, and why, is logged and returned to the caller, which
names it in the answer's `sources`.

**Cap**: every HTTP call is recorded in `place_calls` by SKU. Before a call that would cost money (beyond the
free monthly allowance) and cross `monthly_cap_eur` this month (local), the provider is skipped, and the
user gets one `notify` a day naming the cap.

**The night's budget** (b): the night's web searches (`night_search`) are `place_calls` with
budget `night`: never through `choose`, never counted by the user's cap and never their cap notice; they have
their own monthly cap (the free tier counts for them first) and the hard limit counts them.

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
from decimal import Decimal
from typing import Any, Literal

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.apple_maps import AppleMaps, MapsKey
from pisa.connectors.google_maps import GoogleMaps
from pisa.connectors.tavily import Tavily
from pisa.db.settings import get_setting, set_setting
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.place_prices import PRICES, extra_cost_usd, month_cost_usd
from pisa.infra import hard_limit, locks
from pisa.infra.fx import usd_to_eur
from pisa.infra.hard_limit import HardLimits
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.ports.places import Meter, PlaceProviderError, Usage
from pisa.services.notifications import enqueue
from pisa.services.push import apple_setting, check_p8, normalize_p8

Job = Literal["routes", "search", "details", "web"]
ProviderId = Literal["apple", "google", "tavily"]
MAPS_SECRET = "maps"  # noqa: S105 (the row name in server_secrets, not a secret)
CAP_NOTIFIED_KEY = "places_cap_notified_on"
RESERVATION_STALE = timedelta(minutes=5)


@dataclass(frozen=True)
class PlaceProvider:
    """What a places provider does and how its key is kept: the services ask this, never the provider's id."""

    name: str
    jobs: tuple[Job, ...]
    # Its adapter from (http, key, meter), for a provider whose key is an API key in `place_providers`; None:
    # its key is an Apple Maps signing key (the .p8 text and its key id) in `server_secrets`.
    adapter: Callable[[httpx.AsyncClient, str, Meter], Any] | None = None
    paid_test: bool = False  # its test call can spend a paid credit: the cap is checked first
    id_column: Literal["apple_id", "google_place_id"] | None = None  # the `places` column of its place ids
    cache_24h: bool = False  # its content may be kept 24 hours at most (Google's terms)

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
}
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


def _unknown(provider: str) -> PisaError:
    return PisaError(
        "not_found", f"There is no place provider {provider[:40]!r}.", "Use apple, google or tavily"
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
    ) -> None:
        self._pool = pool
        self.disabled = disabled  # App Store review mode: no provider is ever called, as if none were set up
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

    async def _reserve(self, provider: str, usage: Usage, cap_eur: float) -> tuple[bool, int | None]:
        """(over the cap, the reservation's row): a priced call holds its units in place_calls while it runs,
        under one lock, so two calls racing at the cap can't both pass."""
        price = PRICES.get(usage.sku)
        if price is None or price.free_per_month is None:  # never billed: nothing to hold
            return False, None
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.PLACES_CAP,))
            await conn.execute(  # a process that stopped mid-call left its reservation behind
                "DELETE FROM place_calls WHERE reserved AND at < %s", (now - RESERVATION_STALE,)
            )
            if await self._over_cap_in(conn, usage, cap_eur):
                return True, None
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
        return False, row[0] if row else None

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
        adapter = await self.adapter("tavily", budget="night")
        if adapter is None:
            return None
        usage = adapter.usage_for_search()
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.PLACES_CAP,))
            if await self._night_spent_eur(conn, usage) > cap_eur:
                _log.info("night_places_cap_reached")
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
        review mode (PISA_REVIEW_MODE)."""
        build = PROVIDERS[provider].adapter
        if self.disabled:
            return None
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
            cached = self._adapters.get(provider)
            if budget == "owner" and cached is not None and cached[0] == row[1]:
                return cached[1]
            secret = self._keys.unseal(bytes(row[0]))
            built = build(self._http, secret, self._meter_for(provider, budget))
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
        empty result is the answer. `only`: that provider alone (the model asked for it), if the
        user's order for the job has it."""
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
        order: list[str] = settings["places"][job]
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
        capped = False
        empty: Choice[T] | None = None  # the last provider that answered but found nothing
        for provider in order:
            try:
                adapter = await self.adapter(provider)
            except PisaError as err:  # e.g. its key no longer decrypts: that provider only is out
                skipped.append(f"{provider}: {err.message}")
                causes.append(err.message)
                continue
            if adapter is None:
                skipped.append(f"{provider}: not configured")
                continue
            if not can(adapter):
                skipped.append(f"{provider}: can't do this request")
                continue
            over, reservation = await self._reserve(provider, usage(adapter), cap)
            if over:
                skipped.append(f"{provider}: monthly cap reached")
                capped = True
                await self._cap_reached(cap, lang)
                continue
            try:
                value = await do(adapter)
            except PlaceProviderError as err:
                skipped.append(f"{provider}: {err.message}")
                causes.append(err.message)
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
        _log.warning("place_job_failed", job=job, skipped=_providers(skipped))
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
        raise PisaError(
            "place_provider_not_configured",
            f"No provider for {JOB_PHRASE[job]} is ready ({'; '.join(skipped)}).",
            "Add a key in Settings → Places",
        )

    # --- provider keys, status and usage --------------------------------------------------------------------

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
        if provider not in PROVIDERS:
            raise _unknown(provider)
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
