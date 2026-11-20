"""A UCP client (Universal Commerce Protocol, spec 2026-08-25) for US retail, as a
platform with a discoverable profile (the spec's permissionless onboarding): Pisa reads a merchant's
`/.well-known/ucp`, searches its catalog and builds a cart (a checkout session) whose `continue_url` the user
opens and pays on the merchant's page. Every request carries `UCP-Agent: profile="…"` and Pisa's Web Bot Auth
signature (RFC 9421, which UCP accepts without a prior relationship).

RAIL: this client has no call that completes a checkout, pays, or places an order; the signer refuses to sign
one anyway (`may_sign`). What a merchant returns is outside text, parsed strictly in pisa/domain/world.py.

Never logged: a query, a product, a price, a URL.
"""

import json
from typing import Any

import httpx

from pisa.domain import identity
from pisa.logging import get_logger
from pisa.ports.world import Signer

TIMEOUT_S = 8.0
BODY_MAX = 512_000
# The catalog and checkout paths under the shopping service's REST endpoint (the spec's REST binding).
CATALOG_SEARCH = "/catalog/search"
CHECKOUT_SESSIONS = "/checkout-sessions"
_log = get_logger("pisa.ucp")


class UcpError(Exception):
    """The merchant didn't answer usefully (a status, a timeout, a body that isn't JSON)."""


class UcpClient:
    def __init__(self, http: httpx.AsyncClient, signer: Signer) -> None:
        self._http = http
        self._signer = signer

    async def _headers(self, method: str, url: str, purpose: identity.Purpose) -> dict[str, str]:
        out = await self._signer.headers(method, url, purpose)
        out["Accept"] = "application/json"
        if self._signer.agent_url:
            out["UCP-Agent"] = f'profile="{self._signer.agent_url}{identity.UCP_PROFILE_PATH}"'
        return out

    async def _json(self, method: str, url: str, purpose: identity.Purpose, body: Any = None) -> Any:
        try:  # a request Pisa never signs is no request, and the caller falls back (no crash)
            headers = await self._headers(method, url, purpose)
        except identity.NotSignable:
            raise UcpError("not signable") from None
        content = json.dumps(body).encode() if body is not None else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with self._http.stream(
                method, url, headers=headers, content=content, timeout=TIMEOUT_S, follow_redirects=False
            ) as response:
                if response.status_code not in (200, 201):
                    _log.info("ucp_status", status=response.status_code)
                    raise UcpError(f"HTTP {response.status_code}")
                data = bytearray()
                async for chunk in response.aiter_bytes():  # capped while it streams
                    data += chunk
                    if len(data) > BODY_MAX:
                        raise UcpError("body too large")
        except httpx.HTTPError as err:
            raise UcpError(type(err).__name__) from None
        try:
            return json.loads(bytes(data))
        except ValueError:
            raise UcpError("not JSON") from None

    async def discover(self, host: str) -> Any | None:
        """The merchant's profile, or None when it publishes none (404)."""
        try:
            return await self._json("GET", f"https://{host}/.well-known/ucp", "read")
        except UcpError as err:
            if str(err) == "HTTP 404":
                return None
            raise

    async def search(self, endpoint: str, query: str, limit: int) -> Any:
        return await self._json(
            "POST", endpoint + CATALOG_SEARCH, "ucp_catalog", {"query": query, "pagination": {"limit": limit}}
        )

    async def cart(self, endpoint: str, items: list[tuple[str, int]], currency: str) -> Any:
        """A checkout session with these items: the merchant's cart, to hand to the user (never completed)."""
        body = {
            "line_items": [{"item": {"id": pid}, "quantity": qty} for pid, qty in items],
            "currency": currency,
        }
        return await self._json("POST", endpoint + CHECKOUT_SESSIONS, "ucp_cart", body)
