# ruff: noqa: E501
"""A map with a few invented places (their numbers and sites are synthetic) and a phone that shows
sheets, for the handoff tests and the replay (which has no places provider)."""

from typing import Any

from pisa.domain.errors import PisaError

PLACES: dict[str, dict[str, Any]] = {
    "glicine": {
        "id": "00000000-0000-4000-8000-00000000a001", "name": "Glicine", "address": "Via Fittizia Phi Seconda 21, Roma",
        "lat": 41.829, "lon": 12.424, "phone": "+44 20 7946 0718", "website": "https://www.glicine.example/prenota",
    },
    "trattoria senza sito": {
        "id": "00000000-0000-4000-8000-00000000a002", "name": "Trattoria Senza Sito",
        "address": "Via Fittizia Beta 1, Roma", "lat": 41.836, "lon": 12.418, "phone": None, "website": None,
    },
    "aeroporto": {
        "id": "00000000-0000-4000-8000-00000000a003", "name": "Aeroporto di Fiumicino",
        "address": "Fiumicino", "lat": 41.8, "lon": 12.239, "phone": None, "website": None,
    },
    "studio dentistico bianchi": {
        "id": "00000000-0000-4000-8000-00000000a004", "name": "Studio Dentistico Bianchi",
        "address": "Via Fittizia Theta 10, Roma", "lat": 41.817, "lon": 12.424, "phone": "+44 20 7946 0432", "website": None,
    },
}  # fmt: skip
HERE = {"lat": 41.825, "lon": 12.429, "age_min": 2, "accuracy_m": 15, "address": "Piazza della Prova, Roma"}


class FakePlaces:
    """The PlaceData calls the handoff tools make: find, card, where."""

    def __init__(self, here: dict[str, Any] | None = HERE) -> None:
        self.here = here
        self.searched: list[tuple[str, str | None]] = []

    async def where(self) -> dict[str, Any] | None:
        return self.here

    async def find(self, place: str) -> str:
        low = place.casefold().strip()
        for key, p in PLACES.items():
            if low == p["id"] or low in key or key in low or low in p["name"].casefold():
                return str(p["id"])
        # A map finds "il dentista" as a dentist's practice (the words' stems), as any
        # provider does; the exact-name match above stays first
        stems = {w[:6] for w in low.replace("'", " ").split() if len(w) >= 5}
        for key, p in PLACES.items():
            if stems and stems <= {w[:6] for w in key.split() if len(w) >= 5}:
                return str(p["id"])
        raise PisaError("not_found", f"No place matches {place[:60]!r}.", "")

    async def map_search(
        self, query: str, *, near: str | None, radius_m: int | None, open_now: bool, provider: str | None
    ) -> list[dict[str, Any]]:
        self.searched.append((query, near))
        try:
            return [{"id": await self.find(query)}]
        except PisaError:
            return []

    async def card(self, place_id: str, *, reviews: bool = True, travel: bool = True) -> dict[str, Any]:
        return dict(next(p for p in PLACES.values() if p["id"] == place_id))

    def __getattr__(self, name: str) -> Any:
        async def missing(*args: Any, **kwargs: Any) -> Any:
            raise PisaError("not_found", f"the replay's map has no {name}", "")

        return missing


class SheetPhone:
    """A phone on the socket that shows sheets: it records each `present` and answers `shown`."""

    kinds = frozenset({"sheet"})

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def present(self, message: dict[str, Any]) -> tuple[str, str] | None:
        self.sent.append(message)
        return ("shown", "phone")

    async def focus(self, present_id: str, object_id: str, highlight: str | None) -> tuple[str, str] | None:
        return ("shown", "phone")

    async def close(self, object_id: str) -> bool:
        return True

    async def card(self, kind: str, title: str, lines: list[str]) -> bool:
        return True

    def handoffs(self) -> list[dict[str, Any]]:
        return [b for m in self.sent for b in m["object"]["data"]["blocks"] if b["type"] == "handoff"]


# --- A US store that speaks UCP (invented ids and prices) ----------------------------

UCP_ENDPOINT = "https://api.target.com/ucp/v1"
TARGET_PROFILE = {
    "ucp": {
        "version": "2026-08-25",
        "services": {"dev.ucp.shopping": {"rest": {"endpoint": UCP_ENDPOINT}}},
        "capabilities": [{"name": "dev.ucp.shopping.catalog"}, {"name": "dev.ucp.shopping.checkout"}],
    }
}
TARGET_PRODUCTS = {
    "currency": "USD",
    "products": [
        {"id": "tcin-1001", "title": "Philips LED A19 60W Soft White, 4-pack", "price": {"amount": 1299, "currency": "USD"}},
        {"id": "tcin-1002", "title": "GE Basic LED 8-pack", "price": 899.5},  # a float: no stated price
    ],
}  # fmt: skip
CONTINUE_URL = "https://www.target.com/checkout/cs_7f3a?token=ab12"


def target_cart(body: dict[str, Any], continue_url: str | None = CONTINUE_URL) -> dict[str, Any]:
    lines = [
        {"id": f"li{i}", "item": {"id": li["item"]["id"], "title": "Philips LED A19 60W Soft White, 4-pack",
                                   "price": 1299},
         "quantity": li["quantity"], "totals": [{"type": "subtotal", "amount": 1299 * li["quantity"]}]}
        for i, li in enumerate(body["line_items"])
    ]  # fmt: skip
    sub = sum(x["totals"][0]["amount"] for x in lines)
    out: dict[str, Any] = {
        "id": "cs_7f3a", "status": "ready_for_complete", "currency": "USD", "line_items": lines,
        "totals": [{"type": "subtotal", "amount": sub}, {"type": "tax", "amount": 208}, {"type": "total", "amount": sub + 208}],
    }  # fmt: skip
    if continue_url:
        out["continue_url"] = continue_url
    return out


class UcpMerchant:
    """An httpx handler: Target with a UCP endpoint (or `ucp=False`: none); it records every request."""

    def __init__(self, *, ucp: bool = True, continue_url: str | None = CONTINUE_URL) -> None:
        self.ucp = ucp
        self.continue_url = continue_url
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        import json

        import httpx

        self.requests.append(request)
        url = str(request.url)
        if url == "https://target.com/.well-known/ucp":
            return httpx.Response(200, json=TARGET_PROFILE) if self.ucp else httpx.Response(404)
        if url == UCP_ENDPOINT + "/catalog/search" and request.method == "POST":
            return httpx.Response(200, json=TARGET_PRODUCTS)
        if url == UCP_ENDPOINT + "/checkout-sessions" and request.method == "POST":
            return httpx.Response(201, json=target_cart(json.loads(request.content), self.continue_url))
        return httpx.Response(404)


class ReplaySigner:
    """Pisa's identity as published (its URL), without a key: the replay checks what Pisa asks, not the signature."""

    agent_url: str | None = "https://pisa.example.org"

    async def headers(self, method: str, url: str, purpose: Any = "read") -> dict[str, str]:
        from pisa.domain.identity import may_sign, user_agent

        if not may_sign(method, url, purpose):
            raise ValueError("Pisa never signs this")
        return {"User-Agent": user_agent(self.agent_url)}


def replay_world(pool: Any, merchant: UcpMerchant, clock: Any) -> Any:
    import httpx

    from pisa.connectors.ucp import UcpClient
    from pisa.services.world import WorldService

    http = httpx.AsyncClient(transport=httpx.MockTransport(merchant))
    return WorldService(pool, UcpClient(http, ReplaySigner()), clock=clock)
