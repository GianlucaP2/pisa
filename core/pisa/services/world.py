# ruff: noqa: E501
"""The merchants Pisa may talk to, their UCP catalogs and carts, and the ladder.

- `merchant(named)`: only the allowlist (`app_settings.world.merchants`, else the seed); a merchant off it is
  never contacted.
- `discovery(m)`: its `/.well-known/ucp`, read at most once every DISCOVERY_DAYS (kept in `ucp_merchants`: the
  endpoint, the version, the capability names; the profile itself is outside text and isn't kept).
- `find_products` / `build_cart`: the protocol rung when the merchant has a UCP endpoint, else the deep-link rung
  (its own search page). Prices as the merchant states them; nothing bought (the user pays on its page).
- `ladder` / `first_rung`: the research's table, `app_settings.world.ladder` over it.

Never logged: a query, a product, a price, a URL.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from pisa.connectors.ucp import UcpClient, UcpError
from pisa.db.settings import get_setting
from pisa.domain import world as rules
from pisa.logging import get_logger

KEY = "world"
_log = get_logger("pisa.world")


def _route(r: rules.Route) -> dict[str, str]:
    return {"rung": r.rung, "state": r.state, "via": r.via, "note": r.note, "source": r.source}


class WorldService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        ucp: UcpClient | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._ucp = ucp
        self._clock = clock

    async def _setting(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            value = await get_setting(conn, KEY)
        return value if isinstance(value, dict) else {}

    async def _merchant(self, named: str) -> rules.Merchant | None:
        return rules.merchant(rules.merchants(await self._setting()), named)

    async def discovery(self, m: rules.Merchant) -> tuple[str, list[str]] | None:
        """(the endpoint, the capabilities) of a merchant's UCP shopping service, or None (none, or it failed)."""
        now = self._clock()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT has_ucp, endpoint, capabilities FROM ucp_merchants WHERE host = %s AND checked_at > %s "
                    "AND (error IS NULL OR checked_at > %s)",  # a failure is tried again in 1 h
                    (m.host, now - timedelta(days=rules.DISCOVERY_DAYS), now - rules.DISCOVERY_ERROR_FOR),
                )
            ).fetchone()
        if row is not None:
            return (row[1], list(row[2])) if row[0] else None
        if self._ucp is None:
            return None
        found, error = None, None
        try:
            found = rules.profile(await self._ucp.discover(m.host), m.host)
        except UcpError as err:
            error = str(err)[:80]
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO ucp_merchants (host, checked_at, has_ucp, version, endpoint, capabilities, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (host) DO UPDATE SET checked_at = "
                "EXCLUDED.checked_at, has_ucp = EXCLUDED.has_ucp, version = EXCLUDED.version, endpoint = "
                "EXCLUDED.endpoint, capabilities = EXCLUDED.capabilities, error = EXCLUDED.error",
                (m.host, now, found is not None, found[0] if found else None, found[1] if found else None,
                 found[2] if found else [], error),
            )  # fmt: skip
        _log.info("ucp_discovered", host=m.host, has_ucp=found is not None)
        return (found[1], found[2]) if found else None

    async def merchant(self, named: str) -> dict[str, Any] | None:
        m = await self._merchant(named)
        if m is None:
            return None
        return {"key": m.key, "name": m.name, "host": m.host, "country": m.country,
                "ucp": await self.discovery(m) is not None}  # fmt: skip

    async def _rung(self, m: rules.Merchant, ucp: bool) -> str | None:
        table = rules.ladder(await self._setting())
        return rules.first_rung(
            table, "retail", m.country, {"protocol", "deep_link"} if ucp else {"deep_link"}
        )

    async def find_products(self, merchant: str, query: str) -> dict[str, Any]:
        m = await self._merchant(merchant)
        if m is None:
            return {"merchant": None}
        query = " ".join(query.split())[:120]
        found = await self.discovery(m)
        rung = await self._rung(m, found is not None)
        out: dict[str, Any] = {"merchant": m.name, "key": m.key, "host": m.host, "route": rung,
                               "products": [], "search_url": rules.search_page(m, query)}  # fmt: skip
        if rung == "protocol" and found is not None and self._ucp is not None:
            try:
                listed = rules.products(await self._ucp.search(found[0], query, rules.PRODUCTS_MAX))
            except UcpError:
                listed = None
            if listed is None:
                out["route"] = "deep_link"
            else:
                out["products"] = [{"id": p.id, "title": p.title, "price": p.price} for p in listed]
        _log.info("ucp_searched", host=m.host, route=out["route"], found=len(out["products"]))
        return out

    async def build_cart(self, merchant: str, items: list[tuple[str, int]], lang: str) -> dict[str, Any]:
        m = await self._merchant(merchant)
        if m is None:
            return {"merchant": None}
        found = await self.discovery(m)
        out: dict[str, Any] = {"merchant": m.name, "key": m.key, "host": m.host, "route": "deep_link"}
        if found is None or self._ucp is None or await self._rung(m, True) != "protocol":
            return out
        clean = [(pid[:120], qty) for pid, qty in items if pid.strip() and 1 <= qty <= rules.QUANTITY_MAX]
        if not clean:
            return out
        try:
            got = rules.cart(await self._ucp.cart(found[0], clean[: rules.ITEMS_MAX], "USD"), m.host)
        except UcpError:
            got = None
        if got is None or got.continue_url is None or not got.lines:
            _log.info("ucp_cart_unusable", host=m.host)
            return out | {"failed": True}
        _log.info("ucp_cart_built", host=m.host, lines=len(got.lines))
        return out | {
            "route": "protocol",
            "cart_id": got.id,
            "continue_url": got.continue_url,
            "lines": [
                {
                    "id": ln.product_id,
                    "title": ln.title,
                    "quantity": ln.quantity,
                    "price_text": rules.money(ln.price, lang),
                    "total_text": rules.money(ln.total, lang) if ln.total else None,
                }
                for ln in got.lines
            ],
            "total_text": rules.money(got.total, lang) if got.total else None,
        }

    async def ladder(self) -> dict[str, Any]:
        table = rules.ladder(await self._setting())
        return {
            "categories": [
                {"category": c, "countries": {k: [_route(r) for r in v] for k, v in table.get(c, {}).items()}}
                for c in rules.CATEGORIES
            ],
            "carried": sorted(rules.CARRIED),
        }

    async def first_rung(self, category: str, country: str | None, available: set[str]) -> str | None:
        return rules.first_rung(rules.ladder(await self._setting()), category, country, available)

    async def order(self, category: str, country: str | None, rungs: list[str]) -> list[str]:
        return rules.order(rules.ladder(await self._setting()), category, country, rungs)
