"""Who a request comes from, behind reverse proxies the operator trusts (PISA_TRUSTED_PROXIES).

A client can write any X-Forwarded-For it likes: only the hops a trusted proxy appended are believed. The
header is read from the right (the hop nearest Pisa first); every address that is itself a trusted proxy is
skipped, and the first one that is not is the client. With no trusted proxy, the TCP peer is the client and
the header is ignored. Pure: no I/O."""

import ipaddress
from collections.abc import Sequence

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = text.strip()
    if text.startswith("[") and "]" in text:  # [v6]:port
        text = text[1 : text.index("]")]
    elif text.count(":") == 1:  # v4:port
        text = text.split(":", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def limit_key(address: str) -> str:
    """What a per-address limit counts by: an IPv4 address alone (its /32), an IPv6 address by its /64 (one
    host is usually given a whole /64, so a /128 limits nothing); an IPv4-mapped IPv6 address as the IPv4 one.
    Anything that is not an address (a Tailscale login, a test client's name) is its own key."""
    parsed = _address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.IPv6Network((parsed, 64), strict=False))
    return str(parsed) if parsed is not None else address


def trusted(peer: str, proxies: Sequence[Network]) -> bool:
    """The peer is one of the operator's proxies (a peer that is not an IP address never is)."""
    address = _address(peer)
    return address is not None and any(address in network for network in proxies)


def client_address(peer: str, forwarded_for: str | None, proxies: Sequence[Network]) -> str:
    """The client's address: the TCP peer, unless the peer is a trusted proxy; then the nearest hop in
    X-Forwarded-For that is not a trusted proxy (the peer when the header names none)."""
    if not trusted(peer, proxies):
        return peer
    for hop in reversed([h.strip() for h in (forwarded_for or "").split(",") if h.strip()]):
        address = _address(hop)
        if address is None:
            return peer  # a hop that isn't an address: nothing further left can be believed
        if not any(address in network for network in proxies):
            return str(address)
    return peer
