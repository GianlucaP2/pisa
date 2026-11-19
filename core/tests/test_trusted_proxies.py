"""PISA_TRUSTED_PROXIES: X-Forwarded-For is believed only from a proxy the operator named; a client can't pick
the address its pairing guesses are counted under."""

import ipaddress
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from pisa.api.pairing import _client_address
from pisa.domain.proxies import client_address, trusted

LOOPBACK = (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("::1/128"))
PROXIES = (ipaddress.ip_network("10.0.0.0/8"),)


def test_no_trusted_proxy_the_peer_is_the_client_whatever_the_header_says() -> None:
    assert client_address("203.0.113.9", "198.51.100.1", ()) == "203.0.113.9"
    assert client_address("127.0.0.1", "198.51.100.1", ()) == "127.0.0.1"  # loopback isn't trusted by default


def test_a_trusted_proxy_s_last_hop_is_the_client() -> None:
    assert client_address("10.1.2.3", "198.51.100.1", PROXIES) == "198.51.100.1"
    assert client_address("10.1.2.3", None, PROXIES) == "10.1.2.3"


def test_a_spoofed_hop_left_of_the_proxy_s_own_is_never_believed() -> None:
    # the client wrote "192.0.2.66"; the proxy appended the client's real address after it
    assert client_address("10.1.2.3", "192.0.2.66, 198.51.100.1", PROXIES) == "198.51.100.1"


def test_a_chain_of_trusted_proxies_is_walked_from_the_right() -> None:
    assert client_address("10.0.0.1", "198.51.100.1, 10.0.0.2, 10.0.0.3", PROXIES) == "198.51.100.1"
    assert client_address("10.0.0.1", "10.0.0.2", PROXIES) == "10.0.0.1"  # only proxies: the peer


def test_a_hop_that_is_not_an_address_stops_the_walk() -> None:
    assert client_address("10.0.0.1", "198.51.100.1, unknown", PROXIES) == "10.0.0.1"


def test_addresses_with_ports_and_v6() -> None:
    assert trusted("[::1]:443", LOOPBACK) and trusted("127.0.0.1:80", LOOPBACK)
    assert not trusted("testclient", LOOPBACK)
    assert client_address("::1", "[2001:db8::7]:5000", LOOPBACK) == "2001:db8::7"


def pairing_source(
    request: Request, proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
) -> str:
    """The address the pairing routes count a request under, with these trusted proxies."""
    runtime = SimpleNamespace(settings=SimpleNamespace(proxy_networks=proxies))
    return _client_address(request, runtime)


def _request(peer: str, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/pair",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 50000),
    }
    return Request(scope)


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "198.51.100.77"},
        {"Tailscale-User-Login": "someone@example.com"},
        {"X-Forwarded-For": "198.51.100.77", "Tailscale-User-Login": "someone@example.com"},
    ],
)
def test_pairing_counts_a_spoofing_client_under_its_own_address(headers: dict[str, str]) -> None:
    assert pairing_source(_request("127.0.0.1", headers), ()) == "127.0.0.1"
    assert pairing_source(_request("203.0.113.5", headers), ()) == "203.0.113.5"
    assert pairing_source(_request("203.0.113.5", headers), LOOPBACK) == "203.0.113.5"


def test_pairing_behind_a_trusted_proxy_counts_the_forwarded_client() -> None:
    assert pairing_source(_request("127.0.0.1", {"X-Forwarded-For": "100.64.0.9"}), LOOPBACK) == "100.64.0.9"
    # an identity header is no address: the forwarded client is counted, and only from the trusted proxy
    login = {"Tailscale-User-Login": "someone@example.com", "X-Forwarded-For": "100.64.0.9"}
    assert pairing_source(_request("127.0.0.1", login), LOOPBACK) == "100.64.0.9"
    assert pairing_source(_request("203.0.113.5", login), LOOPBACK) == "203.0.113.5"
    assert pairing_source(_request("127.0.0.1", {}), LOOPBACK) == "127.0.0.1"


def test_the_setting_refuses_what_is_not_a_network() -> None:
    from pydantic import ValidationError

    from pisa.settings import Settings

    assert Settings(trusted_proxies=" 10.0.0.0/8 , ::1 ").proxy_networks == (  # type: ignore[call-arg]
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    )
    assert Settings().proxy_networks == ()  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match=r"PISA_TRUSTED_PROXIES: 'proxy\.local' is not an IP address"):
        Settings(trusted_proxies="10.0.0.0/8,proxy.local")  # type: ignore[call-arg]
