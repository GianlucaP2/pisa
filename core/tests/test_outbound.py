"""One outbound fetcher (D-139): each hop resolved once, refused unless every address is public, and connected
to the address it checked, so a name that answers a public address to the check and a private one to the
connection (DNS rebinding) never reaches the private one."""

import asyncio
from typing import Any

import httpx
import pytest

from pisa.connectors.outbound import Outbound, PinnedTransport, Policy, Refused, public

ANY = Policy(frozenset({"http", "https"}), None, 3)
WEB = Policy(frozenset({"https"}), frozenset({443}), 3)
PUBLIC = "93.184.215.14"


def _fetch(
    policy: Policy, answers: dict[str, list[list[str]]], handler: Any
) -> tuple[Outbound, dict[str, Any]]:
    """An Outbound whose resolver answers each host's lists in turn, and whose transport records the IP each
    hop was pinned to."""
    seen: dict[str, Any] = {"resolved": [], "pinned": []}

    async def resolve(host: str) -> list[str]:
        seen["resolved"].append(host)
        turns = answers[host]
        return turns.pop(0) if len(turns) > 1 else turns[0]

    def transport(ip: str) -> httpx.AsyncBaseTransport:
        seen["pinned"].append(ip)
        return httpx.MockTransport(handler)

    return Outbound(policy, resolver=resolve, transport=transport), seen


async def _get(fetch: Outbound, url: str) -> tuple[str, int]:
    async with fetch.get(url) as (final, response):
        return final, response.status_code


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok")


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.7"], ["127.0.0.1"], ["::1"], ["169.254.169.254"], ["100.64.0.1"],
        [PUBLIC, "192.168.1.2"], [],
        # E1 U1: NAT64 addresses carrying a private IPv4 one, IPv4-compatible and site-local IPv6
        ["64:ff9b::7f00:1"], ["64:ff9b::a9fe:a9fe"], ["64:ff9b:1:a00:0:7::"], ["::7f00:1"], ["fec0::1"],
    ],
)  # fmt: skip
def test_a_name_that_resolves_to_a_private_address_is_refused(addresses: list[str]) -> None:
    fetch, seen = _fetch(ANY, {"inside.example": [addresses]}, _ok)
    with pytest.raises(Refused, match="not_public"):
        asyncio.run(_get(fetch, "http://inside.example/admin"))
    assert seen["pinned"] == []  # nothing was connected to


def test_a_nat64_address_of_a_public_ipv4_one_is_public() -> None:
    assert public(["64:ff9b::808:808"])
    assert not public(["64:ff9b:1:808:8:800::"])  # the local-use NAT64 prefix: refused whatever it carries


def test_a_rebinding_name_is_resolved_once_and_connected_at_the_address_checked() -> None:
    """The check sees a public address; a second resolution would answer loopback. There is none."""
    fetch, seen = _fetch(ANY, {"rebind.example": [[PUBLIC], ["127.0.0.1"]]}, _ok)
    assert asyncio.run(_get(fetch, "http://rebind.example/")) == ("http://rebind.example/", 200)
    assert seen["resolved"] == ["rebind.example"] and seen["pinned"] == [PUBLIC]


def test_each_redirect_hop_is_checked_and_pinned_the_same_way() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "jump.example":
            return httpx.Response(302, headers={"location": "https://inside.example/admin"})
        return _ok(request)

    fetch, seen = _fetch(ANY, {"jump.example": [[PUBLIC]], "inside.example": [["10.1.2.3"]]}, handler)
    with pytest.raises(Refused, match="not_public"):
        asyncio.run(_get(fetch, "https://jump.example/"))
    assert seen["pinned"] == [PUBLIC]  # the private hop was never connected to


def test_the_caller_s_scheme_and_port_policy_holds() -> None:
    fetch, seen = _fetch(WEB, {"a.example": [[PUBLIC]]}, _ok)
    for url in ("http://a.example/", "https://a.example:8443/", "https://someone@a.example/"):
        assert asyncio.run(fetch.allowed(url)) is False
    assert asyncio.run(fetch.allowed("https://a.example/")) is True
    assert seen["pinned"] == []


def test_too_many_redirects_are_refused() -> None:
    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    fetch, _ = _fetch(ANY, {"a.example": [[PUBLIC]]}, loop)
    with pytest.raises(Refused, match="too_many_redirects"):
        asyncio.run(_get(fetch, "http://a.example/"))


def test_a_pinned_connection_never_resolves_the_name_and_keeps_its_host_header() -> None:
    """A name that can't resolve (`.invalid`, RFC 2606) is reached at the pinned address; the server sees the
    hostname in the Host header."""

    async def go() -> tuple[int, str, bytes]:
        got: list[bytes] = []

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            got.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\npinned")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            transport = PinnedTransport("127.0.0.1")
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.get(f"http://pinned.invalid:{port}/page")
            return response.status_code, response.text, got[0]
        finally:
            server.close()
            await server.wait_closed()

    status, text, request = asyncio.run(go())
    assert (status, text) == (200, "pinned")
    assert b"host: pinned.invalid:" in request.lower() and request.startswith(b"GET /page ")


def test_a_pinned_connection_that_fails_is_an_httpx_error() -> None:
    async def go() -> None:
        async with httpx.AsyncClient(transport=PinnedTransport("127.0.0.1")) as client:
            await client.get("http://pinned.invalid:9/")  # the discard port: nothing listens

    with pytest.raises(httpx.ConnectError):
        asyncio.run(go())
