# ruff: noqa: E501
"""Pisa's own web reads, signed (Web Bot Auth) and honest about what Pisa is.

`read(url)` GETs one public page as Pisa, through `connectors.outbound` (each hop resolved once, checked and
pinned to the address checked: no SSRF into the tailnet or the host, no DNS rebinding): https on port 443
only; redirects followed by hand, at most 3, each checked again; HTML, text or JSON only; at most READ_MAX bytes; the text is the page's
words (scripts and styles gone). A page that refuses Pisa (a bot challenge, a 403/429) gives None, and the caller
falls back to its provider. The text is outside text: the caller hands it to the model as data.

Never logged: a URL, a page.
"""

import httpx

from pisa.connectors.door import guard
from pisa.connectors.outbound import (
    Outbound,
    Policy,
    Refused,
    Resolver,
    TransportFor,
    public,
    system_resolver,
)
from pisa.domain import visible
from pisa.logging import get_logger
from pisa.ports.world import Signer

READ_MAX = 1_500_000
REDIRECTS_MAX = 3
TIMEOUT_S = 10.0
TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/json")
# https on port 443 only, at most 3 redirects, a URL of at most 2000 characters (`connectors.outbound`)
POLICY = Policy(frozenset({"https"}), frozenset({443}), REDIRECTS_MAX, url_max=2000)
_log = get_logger("pisa.web_reader")

__all__ = ["Resolver", "SignedReader", "page_text", "public", "system_resolver"]


def page_text(body: str, content_type: str) -> tuple[str, int]:
    """The page's visible text, one line, and the hidden runs left out (CSS-hidden
    elements, white on white, tiny fonts, zero-width characters), a count only."""
    if "html" not in content_type:
        text, hidden = visible.plain(body)
        return " ".join(text.split()), hidden
    seen = visible.html(body)
    if seen.unparsed is not None:
        _log.warning("page_text_unparsed", cause=seen.unparsed)
    return " ".join(seen.text.split()), seen.hidden


class SignedReader:
    def __init__(
        self, signer: Signer, *, resolver: Resolver = system_resolver, transport: TransportFor | None = None
    ) -> None:
        self._signer = signer
        self._outbound = Outbound(POLICY, resolver=resolver, transport=transport, timeout=TIMEOUT_S)

    async def allowed(self, url: str) -> bool:
        return await self._outbound.allowed(url)

    async def read(self, url: str) -> tuple[str, str] | None:
        """(the page's final URL, its text), or None when Pisa can't or mayn't read it directly. A link holding their
        values is refused (`PisaError` validation_failed, detail personal_refused)."""
        found = await self.read_counted(url)
        return (found[0], found[1]) if found is not None else None

    async def read_counted(self, url: str) -> tuple[str, str, int] | None:
        """`read`, with the hidden runs left out of the text ."""
        guard("a page read", url, provider="signed")

        async def headers(current: str) -> dict[str, str]:  # each hop signed for its own URL
            signed = await self._signer.headers("GET", current, "read")
            return signed | {"Accept": "text/html,text/plain;q=0.9,application/json;q=0.8"}

        try:
            async with self._outbound.get(url.strip(), headers=headers) as (current, response):
                kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if response.status_code != 200 or kind not in TEXT_TYPES:
                    _log.info("web_read_refused", status=response.status_code)
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > READ_MAX:
                        return None
                text, hidden = page_text(body.decode(response.encoding or "utf-8", errors="replace"), kind)
                return (current, text, hidden) if text else None
        except (Refused, httpx.InvalidURL):
            return None
        except httpx.HTTPError:
            _log.info("web_read_failed")
            return None
