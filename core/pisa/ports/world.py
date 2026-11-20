"""What Pisa's own outgoing requests and the cart tools need.

- `Signer`: Pisa's identity (pisa/services/identity.py) as the connectors see it: the headers of one request,
  signed when Pisa may sign it (RAIL: `NotSignable` raised otherwise, and nothing is sent).
- `WorldData`: the merchants, their catalogs and carts, and the ladder, as the brain's tools see them.
"""

from typing import Any, Protocol


class Signer(Protocol):
    agent_url: str | None

    async def headers(self, method: str, url: str, purpose: Any = "read") -> dict[str, str]:
        """Pisa's User-Agent and, when Pisa has a published key, its Web Bot Auth signature for this
        request."""
        ...


class WorldData(Protocol):
    async def merchant(self, named: str) -> dict[str, Any] | None:
        """The allowlisted merchant their words name: {key, name, host, country, ucp: bool}, or None."""
        ...

    async def find_products(self, merchant: str, query: str) -> dict[str, Any]:
        """{merchant, route: protocol|deep_link, products: [{id, title, price, price_text}], search_url}."""
        ...

    async def build_cart(self, merchant: str, items: list[tuple[str, int]], lang: str) -> dict[str, Any]:
        """The merchant's cart for these products ({route, lines, total_text, continue_url, cart_id}), or the
        deep-link route when the merchant has no UCP cart. Nothing is bought."""
        ...

    async def ladder(self) -> dict[str, Any]:
        """The routes per category and country (`GET /v1/world/ladder`)."""
        ...

    async def first_rung(self, category: str, country: str | None, available: set[str]) -> str | None:
        """The best rung for this case today."""
        ...

    async def order(self, category: str, country: str | None, rungs: list[str]) -> list[str]:
        """These rungs in the ladder's order for the category and country (live ones first)."""
        ...
