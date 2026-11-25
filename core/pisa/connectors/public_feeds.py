"""The public sources' HTTP reads (GET only, no key, no cookie, Pisa's own User-Agent as the NWS
asks). A body is capped (each source's own `cap`) while it streams; redirects are never followed (the URLs are
the sources' final ones, `domain.public_sources`). Every failure is logged with its reason, the
NWS's by its status only, and raised as PublicDataError."""

from urllib.parse import urlsplit

import httpx

from pisa.domain.public_sources import FEED_MAX, USER_AGENT
from pisa.failures import external_failure
from pisa.ports.public_data import PublicDataError

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
BIG_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class PublicFeeds:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def get(
        self, url: str, *, params: dict[str, str] | None = None, big: bool = False, cap: int = FEED_MAX
    ) -> bytes:
        host = urlsplit(url).hostname or "?"
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        try:
            async with self._http.stream(
                "GET", url, params=params, headers=headers, timeout=BIG_TIMEOUT if big else TIMEOUT
            ) as response:
                if response.status_code != 200:
                    # the NWS's error body repeats the point asked (their place): its status only
                    body = None if host == "api.weather.gov" else (await response.aread())[:400]
                    external_failure("public_data", host, response.status_code, body)
                    raise PublicDataError(f"{host} answered HTTP {response.status_code}.")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > cap:
                        external_failure("public_data", host, "too_large", f"over {cap} bytes")
                        raise PublicDataError(f"{host} sent more than Pisa keeps.")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            external_failure("public_data", host, type(exc).__name__, exc)
            raise PublicDataError(f"{host} is unreachable ({exc.__class__.__name__}).") from None
        return b"".join(chunks)
