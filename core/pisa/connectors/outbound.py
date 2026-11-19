"""Pisa's one fetcher for addresses it was given (a page to read, a paper's open copy): no SSRF, no DNS
rebinding.

Each hop resolves the host once, refuses it unless every address it resolves to is public (no loopback,
private, link-local, reserved or multicast address: nothing in the tailnet or on the host), and connects to
the address it checked: the connection is pinned to that IP (`PinnedTransport`), while the Host header, the
TLS SNI and the certificate check stay on the hostname. Redirects are followed by hand, each hop checked the
same way. Each caller sets its own policy: the schemes and ports it allows, how many redirects.

Never logged: a URL.
"""

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx

Resolver = Callable[[str], Awaitable[list[str]]]
# the transport a checked hop goes through, given the IP it was pinned to (tests inject a fake one)
TransportFor = Callable[[str], httpx.AsyncBaseTransport]
Headers = Callable[[str], Awaitable[dict[str, str]]]
REDIRECTS = (301, 302, 303, 307, 308)


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # RFC 6052: the IPv4 address is the last 32 bits
# never public: IPv4-compatible, site-local, and the local-use NAT64 prefix (RFC 8215; its IPv4 address sits
# where the network's prefix length puts it, which Pisa cannot know)
_NEVER = (
    ipaddress.IPv6Network("::/96"),
    ipaddress.IPv6Network("fec0::/10"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def _public_one(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if any(ip in net for net in _NEVER):
            return False
        if ip in _NAT64:  # a NAT64 address reaches the IPv4 address inside it
            return _public_one(ipaddress.IPv4Address(ip.packed[12:16]))
    return ip.is_global and not ip.is_multicast


def public(addresses: list[str]) -> bool:
    """Every address the host resolves to is a public one (one private answer is enough to refuse)."""
    if not addresses:
        return False
    for a in addresses:
        ip = _ip(a)
        if ip is None or not _public_one(ip):
            return False
    return True


def pick(addresses: list[str]) -> str:
    """The address to connect to: the first IPv4 one (Pisa's server goes out over IPv4), else the first."""
    ips = [ip for a in addresses if (ip := _ip(a)) is not None]
    chosen = next((ip for ip in ips if ip.version == 4), ips[0])
    return str(chosen)


class Refused(ValueError):
    """A hop Pisa may not fetch: `not_public` (scheme, port, credentials in the URL, or an address that is not
    public) or `too_many_redirects`."""


@dataclass(frozen=True)
class Policy:
    schemes: frozenset[str]
    ports: frozenset[int] | None  # None: any port
    redirects: int
    url_max: int | None = None  # the longest URL a hop may have


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connects to the pinned IP whatever host the connection is for: the host is never resolved again."""

    def __init__(self, ip: str) -> None:
        self._ip = ip
        self._backend: httpcore.AsyncNetworkBackend = httpcore.AnyIOBackend()  # pyright: ignore[reportAttributeAccessIssue]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore's interface)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_tcp(
            self._ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("A pinned connection opens no Unix socket.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _httpx_error(exc: Exception) -> httpx.TransportError:
    """httpcore's exception as httpx's of the same name (what the callers catch)."""
    kind = getattr(httpx, type(exc).__name__, httpx.TransportError)
    if not (isinstance(kind, type) and issubclass(kind, httpx.TransportError)):
        kind = httpx.TransportError
    return kind(str(exc))


class _Body(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for part in self._stream:
                yield part
        except httpcore.TimeoutException as exc:
            raise _httpx_error(exc) from exc
        except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise _httpx_error(exc) from exc

    async def aclose(self) -> None:
        await self._stream.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    """An HTTP(S) transport whose connections all go to `ip`. The request's URL keeps its hostname, so the
    Host header, the TLS SNI and the certificate's hostname check are the hostname's."""

    def __init__(self, ip: str, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(), network_backend=_PinnedBackend(ip)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        core = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            answer = await self._pool.handle_async_request(core)
        except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise _httpx_error(exc) from exc
        except httpcore.UnsupportedProtocol as exc:
            raise httpx.UnsupportedProtocol(str(exc)) from exc
        return httpx.Response(
            status_code=answer.status,
            headers=answer.headers,
            stream=_Body(answer.stream),
            extensions=answer.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class Outbound:
    """GETs under a policy, each hop resolved once, checked, and pinned to the address checked."""

    def __init__(
        self,
        policy: Policy,
        *,
        resolver: Resolver = system_resolver,
        transport: TransportFor | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._policy = policy
        self._resolve = resolver
        self._transport = transport  # None: a PinnedTransport per hop, closed after it
        self._timeout = timeout

    async def address(self, url: httpx.URL) -> str:
        """The public address this hop connects to; `Refused` (not_public) otherwise."""
        if url.scheme not in self._policy.schemes or not url.host or url.userinfo:
            raise Refused("not_public")
        if self._policy.url_max is not None and len(str(url)) > self._policy.url_max:
            raise Refused("not_public")
        port = url.port or (443 if url.scheme == "https" else 80)
        if self._policy.ports is not None and port not in self._policy.ports:
            raise Refused("not_public")
        try:
            addresses = await self._resolve(url.host)
        except OSError:
            raise Refused("not_public") from None
        if not public(addresses):
            raise Refused("not_public")
        return pick(addresses)

    async def allowed(self, url: str) -> bool:
        try:
            await self.address(httpx.URL(url))
        except (Refused, httpx.InvalidURL):
            return False
        return True

    @asynccontextmanager
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: Headers | None = None,
    ) -> AsyncIterator[tuple[str, httpx.Response]]:
        """GET `url` (with `params` on the first hop only; `headers` made for each hop's URL), redirects
        followed hop by hop. Yields the final URL and the response, its body not read yet. `Refused` for a hop
        Pisa may not fetch, or past the policy's redirects."""
        target = httpx.URL(url, params=params or None)
        for _ in range(self._policy.redirects + 1):
            ip = await self.address(target)
            transport = self._transport(ip) if self._transport is not None else PinnedTransport(ip)
            client = httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=self._timeout)
            try:
                sent = await headers(str(target)) if headers is not None else {}
                response = await client.send(client.build_request("GET", target, headers=sent), stream=True)
                try:
                    location = response.headers.get("location")
                    if response.status_code in REDIRECTS and location:
                        target = target.join(location)
                        continue
                    yield str(target), response
                    return
                finally:
                    await response.aclose()
            finally:
                if self._transport is None:
                    await transport.aclose()
        raise Refused("too_many_redirects")
