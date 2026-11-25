"""Tavily web research: search (basic depth, 1 credit) and extract (basic depth, 1 credit per 5 pages
extracted; a failed page costs nothing). The key goes in the Authorization header. Usage is reported from the
response's `usage.credits` when Tavily sends it, else from those rules."""

from typing import Any

import httpx

from pisa.connectors.door import guard
from pisa.failures import external_failure
from pisa.ports.places import Meter, PlaceProviderError, Usage, WebPage, WebResult

BASE = "https://api.tavily.com"
PAGE_MAX_CHARS = 8_000  # what one page may bring to a model


class Tavily:
    id = "tavily"

    def __init__(self, http: httpx.AsyncClient, api_key: str, meter: Meter) -> None:
        self._http = http
        self._key = api_key
        self._meter = meter

    async def _post(self, path: str, body: dict[str, Any], estimate: int) -> dict[str, Any]:
        try:
            return await self._post_once(path, body, estimate)
        except PlaceProviderError as err:  # with Tavily's own reason, never the key
            external_failure("tavily", f"POST {path}", err.status, err, self._key)
            raise

    async def _post_once(self, path: str, body: dict[str, Any], estimate: int) -> dict[str, Any]:
        try:
            response = await self._http.post(
                BASE + path, json=body, headers={"Authorization": f"Bearer {self._key}"}, timeout=30
            )
        except httpx.HTTPError as exc:
            await self._meter(Usage("tavily.credit", 0), False)
            raise PlaceProviderError(f"Tavily is unreachable ({exc.__class__.__name__}).") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        credits = (data.get("usage") or {}).get("credits") if isinstance(data.get("usage"), dict) else None
        ok = response.status_code < 400
        await self._meter(
            Usage("tavily.credit", int(credits) if isinstance(credits, int) else (estimate if ok else 0)), ok
        )
        if response.status_code == 401:
            raise PlaceProviderError("Tavily rejected the key (HTTP 401).", 401)
        if response.status_code in (429, 432, 433):  # rate limit, plan limit, pay-as-you-go limit
            detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
            reason = (detail or {}).get("error") or ""
            raise PlaceProviderError(
                f"Tavily refused the call: a credit or rate limit is reached (HTTP {response.status_code}"
                + (f": {reason}" if reason else "") + ").",
                response.status_code,
                quota=True,
            )  # fmt: skip
        if not ok:
            detail = data.get("detail")
            reason = (detail.get("error") if isinstance(detail, dict) else detail) or ""
            raise PlaceProviderError(
                f"Tavily refused the call (HTTP {response.status_code}" + (f": {reason}" if reason else "")
                + ").", response.status_code,
            )  # fmt: skip
        return data

    async def test(self) -> str:
        await self.search("Eiffel Tower", max_results=1)
        return "Tavily accepted the key: one basic search worked (1 credit)."

    def usage_for_search(self) -> Usage:
        return Usage("tavily.credit", 1)

    def usage_for_extract(self, pages: int) -> Usage:
        return Usage("tavily.credit", -(-pages // 5))

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        recent_days: int | None = None,
        country: str | None = None,
    ) -> list[WebResult]:
        # RAIL: a query (or a site) holding their values never leaves Pisa
        guard("the web search", query, include_domains, exclude_domains, provider="tavily")
        body: dict[str, Any] = {
            "query": query, "search_depth": "basic", "max_results": max_results, "include_usage": True,
            "include_answer": False,  # the results themselves: the conversation model reads them
        }  # fmt: skip
        if include_domains:
            body["include_domains"] = include_domains
        if exclude_domains:
            body["exclude_domains"] = exclude_domains
        if recent_days:
            body["time_range"] = time_range(recent_days)
        if country:
            body["country"] = country
        data = await self._post("/search", body, 1)
        return [
            WebResult(str(r.get("title") or ""), str(r["url"]), str(r.get("content") or ""))
            for r in data.get("results") or []
            if isinstance(r, dict) and r.get("url")
        ]

    async def extract(self, urls: list[str]) -> list[WebPage]:
        guard("a page read", urls, provider="tavily")  # RAIL: a link can carry their values
        data = await self._post(
            "/extract",
            {"urls": urls, "extract_depth": "basic", "format": "text", "include_usage": True},
            -(-len(urls) // 5),
        )
        return [
            WebPage(str(r["url"]), str(r.get("raw_content") or "")[:PAGE_MAX_CHARS])
            for r in data.get("results") or []
            if isinstance(r, dict) and r.get("url") and r.get("raw_content")
        ]


def time_range(days: int) -> str:
    """Tavily's time_range covering the last `days` days: day, week, month or year."""
    for limit, name in ((1, "day"), (7, "week"), (31, "month")):
        if days <= limit:
            return name
    return "year"
