# SPDX-License-Identifier: AGPL-3.0-only
"""Voice networking for self-hosted installs: the UDP range and the advertised ICE host (settings and the
candidates a real aiortc answer carries), the fallback when the media never connects (the talk ends
`fallback`, voice unavailable, the phone goes on over /v2/live), the Tailscale relay, and what `pisa init`
writes. Only loopback and documentation addresses (192.0.2.0/24)."""

# ruff: noqa: S104, S311

import asyncio
import random
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pisa.cli_init import read_env
from pisa.services.talk import peer_gone
from pisa.services.talk_media import MediaPeer, MediaUnreachable, VoiceNet, resolve_host, voice_net
from pisa.settings import Settings, udp_ports
from pisa.voice_relay import Relay, held
from tests.test_cli_init import SYNTHETIC_TS_KEY
from tests.test_cli_init import run as init_run


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, timezone="Europe/London", **values)  # type: ignore[arg-type, call-arg]


# --- the settings -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "ports"),
    [("", None), ("  ", None), ("40000-40019", range(40000, 40020)), ("40000", range(40000, 40001)),
     (" 50000-50001 ", range(50000, 50002))],
)  # fmt: skip
def test_the_udp_range_is_a_port_or_an_inclusive_range(spec: str, ports: range | None) -> None:
    assert udp_ports(spec) == ports
    assert settings(voice_udp_ports=spec).voice_ports == ports


@pytest.mark.parametrize("spec", ["40019-40000", "80-90", "40000-70000", "forty", "40000-", "1-2-3", "-1"])
def test_a_udp_range_pisa_cannot_use_is_refused_at_boot(spec: str) -> None:
    with pytest.raises(ValidationError, match="PISA_VOICE_UDP_PORTS"):
        settings(voice_udp_ports=spec)


def test_the_voice_net_comes_from_the_settings() -> None:
    assert voice_net(settings()) == VoiceNet(None, "", 15.0)
    got = voice_net(
        settings(voice_udp_ports="40000-40019", ice_host=" 100.64.0.7 ", voice_connect_timeout_s=5)
    )
    assert got == VoiceNet(range(40000, 40020), "100.64.0.7", 5.0)
    with pytest.raises(ValidationError):
        settings(voice_connect_timeout_s=0)


def test_the_ice_host_file_is_a_setting() -> None:
    assert settings().ice_host_file == ""
    assert settings(ice_host_file="/etc/pisa/ts-cert/ip").ice_host_file == "/etc/pisa/ts-cert/ip"


def test_the_ice_host_file_is_read_at_each_talk_when_the_ice_host_is_unset(tmp_path: Path) -> None:
    path = tmp_path / "ip"
    path.write_text("100.64.0.7\n")
    configured = settings(voice_udp_ports="40000-40019", ice_host_file=str(path))
    assert voice_net(configured) == VoiceNet(range(40000, 40020), "100.64.0.7", 15.0)
    path.write_text("100.64.0.8\n")  # the sidecar rewrote it: the next talk offers the new address
    assert voice_net(configured).host == "100.64.0.8"


def test_the_ice_host_setting_wins_over_the_file(tmp_path: Path) -> None:
    path = tmp_path / "ip"
    path.write_text("100.64.0.7\n")
    assert voice_net(settings(ice_host="100.64.0.9", ice_host_file=str(path))).host == "100.64.0.9"
    assert voice_net(settings(ice_host="  ", ice_host_file=str(path))).host == "100.64.0.7"


@pytest.mark.parametrize(
    "content", [None, "", "\n", "not an address", "100.64.0", "100.64.0.300", "fd7a:115c:a1e0::7",
                "100.64.0.7 100.64.0.8", "100.64.0.7" + "0" * 100, "\xff\xfe"],
)  # fmt: skip
def test_a_missing_or_invalid_ice_host_file_means_no_advertised_host(
    tmp_path: Path, content: str | None
) -> None:
    from structlog.testing import capture_logs

    path = tmp_path / "ip"
    if content is not None:
        path.write_bytes(content.encode("latin-1"))
    with capture_logs() as logs:
        assert voice_net(settings(ice_host_file=str(path))) == VoiceNet(None, "", 15.0)
    assert [log["log_level"] for log in logs] == ["debug"]
    assert all(str(tmp_path) not in str(log) for log in logs)  # the reason only
    assert voice_net(settings(ice_host_file=str(tmp_path))).host == ""  # a directory: no crash either


def test_the_advertised_host_is_an_ip_or_a_name_resolved_now() -> None:
    assert asyncio.run(resolve_host("192.0.2.10")) == "192.0.2.10"
    assert asyncio.run(resolve_host("localhost")) in ("127.0.0.1", "::1")
    assert asyncio.run(resolve_host("unresolvable.invalid")) is None


# --- the candidates of a real answer ------------------------------------------------------------------------


def free_range(size: int) -> range:
    """A range of UDP ports free on 127.0.0.1 now (tests run in parallel)."""
    while True:
        start = random.randrange(42000, 60000 - size)
        socks = []
        try:
            for port in range(start, start + size):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks.append(sock)
                sock.bind(("0.0.0.0", port))
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
        return range(start, start + size)


def candidates(sdp: str) -> list[tuple[str, int, str]]:
    found = re.findall(r"a=candidate:\S+ \d+ udp \d+ (\S+) (\d+) typ (\w+)", sdp, flags=re.IGNORECASE)
    return [(host, int(port), kind) for host, port, kind in found]


def nowhere(sdp: str) -> str:
    """The phone's offer with its candidates at a documentation address: Pisa's checks can't reach it."""
    return re.sub(r"(a=candidate:\S+ \d+ udp \d+ )\S+", r"\g<1>192.0.2.20", sdp, flags=re.IGNORECASE)


async def phone_talks_to(
    net: VoiceNet, wait_s: float = 10.0, offer: Callable[[str], str] = lambda sdp: sdp
) -> tuple[MediaPeer, str, str, list[str]]:
    """A phone's peer (aiortc, on this machine) offers to Pisa's; what Pisa answered, the data channel's
    state after `wait_s` at most, and whatever ended Pisa's peer."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    from pisa.api.talk_channel import talk_dialects

    server = MediaPeer([], 16_000, talk_dialects("device-1", "thread-1", "en-US"), net)
    closed: list[str] = []
    server.on_closed = lambda: closed.append(peer_gone(server)[0])
    phone = RTCPeerConnection()
    phone.addTransceiver("audio", direction="sendrecv")
    channel = phone.createDataChannel("pisa", protocol="pisa.v2")
    try:
        await phone.setLocalDescription(await phone.createOffer())
        answer = await server.answer(offer(phone.localDescription.sdp))
        await phone.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        for _ in range(int(wait_s / 0.05)):
            if channel.readyState == "open" or closed:
                break
            await asyncio.sleep(0.05)
        return server, answer, channel.readyState, list(closed)  # before the close below
    finally:
        await phone.close()
        await server.close()


def test_the_answer_offers_the_advertised_host_on_the_range_and_the_phone_connects() -> None:
    ports = free_range(4)
    _, answer, state, closed = asyncio.run(phone_talks_to(VoiceNet(ports, "127.0.0.1")))
    found = candidates(answer)
    assert found and all(
        host == "127.0.0.1" and port in ports and kind == "host" for host, port, kind in found
    )
    assert state == "open" and closed == []


def test_the_range_alone_keeps_each_interface_s_address() -> None:
    ports = free_range(8)
    _, answer, state, _ = asyncio.run(phone_talks_to(VoiceNet(ports, "")))
    found = candidates(answer)
    assert found and all(port in ports for _, port, _ in found)
    assert state == "open"


def test_unset_it_is_aiortc_s_own_choice() -> None:
    _, answer, state, _ = asyncio.run(phone_talks_to(VoiceNet()))
    assert candidates(answer) and state == "open"


# --- the fallback -------------------------------------------------------------------------------------------


def test_media_that_never_connects_ends_the_talk_as_fallback() -> None:
    """The advertised host doesn't reach Pisa, nor Pisa the phone (documentation addresses): within the
    connect timeout the peer is unreachable and closes, and its talk ends `fallback` (voice unavailable, the
    app goes on over /v2/live), not `error`."""
    ports = free_range(2)
    server, answer, state, closed = asyncio.run(
        phone_talks_to(VoiceNet(ports, "192.0.2.10", connect_timeout_s=0.5), wait_s=5.0, offer=nowhere)
    )
    assert {host for host, _, _ in candidates(answer)} == {"192.0.2.10"}
    assert state != "open"
    assert server.unreachable and closed[:1] == ["fallback"]
    reason, by, detail = peer_gone(server)
    assert (reason, by) == ("fallback", "peer") and "text" in detail


def test_a_range_with_no_free_port_answers_nothing() -> None:
    """Every port taken: no candidate, so no answer (the API says talk_economy: the app talks in economy)."""
    ports = free_range(1)
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("0.0.0.0", ports[0]))
    try:
        with pytest.raises(MediaUnreachable):
            asyncio.run(phone_talks_to(VoiceNet(ports, "127.0.0.1")))
    finally:
        taken.close()


class _Link:
    def __init__(self, unreachable: bool | None) -> None:
        if unreachable is not None:
            self.unreachable = unreachable


@pytest.mark.parametrize(("unreachable", "reason"), [(True, "fallback"), (False, "error"), (None, "error")])
def test_a_peer_lost_after_it_connected_is_still_an_error(unreachable: bool | None, reason: str) -> None:
    assert peer_gone(_Link(unreachable))[0] == reason  # type: ignore[arg-type]


def test_no_free_port_is_talk_economy_on_the_api(client, db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The talk's open, with every media port taken, refuses with the typed economy error (409, the app's
    documented path to economy, where text works), and the talker it opened is closed."""
    from aiortc import RTCPeerConnection

    from tests.conftest import pair, set_key, talk_naturally
    from tests.test_live_talk import FakeTalker

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    talk_naturally(db)  # with Grok Voice, their consent covering it
    set_key(db, secrets_key, provider="xai")
    runtime = client.app.state.runtime
    runtime.talk._talkers = {"xai": FakeTalker()}
    ports = free_range(1)
    monkeypatch.setattr(
        runtime,
        "settings",
        runtime.settings.model_copy(update={"voice_udp_ports": str(ports[0]), "ice_host": "127.0.0.1"}),
    )

    async def offer() -> str:
        pc = RTCPeerConnection()
        pc.addTransceiver("audio", direction="sendrecv")
        pc.createDataChannel("pisa")
        await pc.setLocalDescription(await pc.createOffer())
        sdp = pc.localDescription.sdp
        await pc.close()
        return sdp

    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("0.0.0.0", ports[0]))
    try:
        body = {"sdp_offer": asyncio.run(offer()), "trigger": "press", "lang": "en-US"}
        refused = client.post("/v1/talk/sessions", headers=auth, json=body)
    finally:
        taken.close()
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "talk_economy"
    assert runtime.talk.runs == {}


# --- the Tailscale relay ------------------------------------------------------------------------------------


def test_the_relay_carries_each_flow_to_the_api_and_back() -> None:
    """The relay on 127.0.0.2 (the sidecar's loopback, in the compose file) forwards each port to the same
    port on 127.0.0.1 (the API) and the API's replies back to the right client."""
    ports = free_range(2)

    class Echo(asyncio.DatagramProtocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self.transport: Any = transport

        def datagram_received(self, data: bytes, addr: Any) -> None:
            self.transport.sendto(b"api:" + data, addr)

    class Client(asyncio.DatagramProtocol):
        def __init__(self) -> None:
            self.got: asyncio.Queue[tuple[bytes, Any]] = asyncio.Queue()

        def datagram_received(self, data: bytes, addr: Any) -> None:
            self.got.put_nowait((data, addr))

    async def go() -> list[tuple[bytes, Any]]:
        loop = asyncio.get_running_loop()
        apis = [(await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", p)))[0] for p in ports]
        relay = Relay(ports, "127.0.0.1", listen="127.0.0.2", idle_s=0.2)
        await relay.start()
        assert held(ports[0], "127.0.0.2")
        clients = [await loop.create_datagram_endpoint(Client, local_addr=("127.0.0.1", 0)) for _ in range(2)]
        try:
            got = []
            for i, (transport, client) in enumerate(clients):
                transport.sendto(f"phone{i}".encode(), ("127.0.0.2", ports[i]))
                got.append(await asyncio.wait_for(client.got.get(), 3))
            clients[0][0].sendto(b"again", ("127.0.0.2", ports[0]))
            got.append(await asyncio.wait_for(clients[0][1].got.get(), 3))
            await asyncio.sleep(0.3)
            for port in relay.bound:
                port.sweep(loop.time() + 1e9)  # idle flows go
            assert all(not port.flows for port in relay.bound)
            return got
        finally:
            relay.close()
            for transport, _ in clients:
                transport.close()
            for transport in apis:
                transport.close()

    from structlog.testing import capture_logs

    with capture_logs() as logs:
        got = asyncio.run(go())
    flows = [e for e in logs if e["event"] == "voice_relay_flow"]
    # each new flow at INFO, counts and the port only (never the phone's address)
    assert [(e["log_level"], e["port"]) for e in flows] == [("info", ports[0]), ("info", ports[1])]
    assert all(set(e) == {"event", "log_level", "port", "flows", "total"} for e in flows)
    assert [data for data, _ in got] == [b"api:phone0", b"api:phone1", b"api:again"]
    assert [addr[1] for _, addr in got] == [ports[0], ports[1], ports[0]]  # from the port the phone sent to
    assert not held(ports[0], "127.0.0.2")


class _Echo(asyncio.DatagramProtocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport: Any = transport

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.transport.sendto(b"api:" + data, addr)


class _Client(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.got: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.got.put_nowait(data)


async def _answered(clients: list[Any], port: int) -> list[bool]:
    """Each client sends one packet to the relay's `port`: whether the API's answer comes back."""
    for transport, _ in clients:
        transport.sendto(b"hello", ("127.0.0.2", port))
    await asyncio.sleep(0.3)
    return [not client.got.empty() for _, client in clients]


def test_the_relay_caps_its_flows_per_port_and_in_total() -> None:
    """Many source ports open no more flows (sockets) than the caps; packets beyond them are
    dropped and logged at debug with counts only; flows already open carry on, and the sweep frees room."""
    import structlog

    ports = free_range(2)

    async def go() -> None:
        loop = asyncio.get_running_loop()
        apis = [(await loop.create_datagram_endpoint(_Echo, local_addr=("127.0.0.1", p)))[0] for p in ports]
        relay = Relay(ports, "127.0.0.1", listen="127.0.0.2", idle_s=60, max_per_port=3, max_flows=4)
        await relay.start()
        clients = [
            await loop.create_datagram_endpoint(_Client, local_addr=("127.0.0.1", 0)) for _ in range(8)
        ]
        try:
            with structlog.testing.capture_logs() as logs:
                assert await _answered(clients[:5], ports[0]) == [True] * 3 + [False] * 2  # 3 a port
                assert await _answered(clients[5:8], ports[1]) == [True, False, False]  # 4 in all
            assert [len(p.flows) for p in relay.bound] == [3, 1]
            dropped = [e for e in logs if e["event"] == "voice_relay_flow_dropped"]
            assert len(dropped) == 4
            assert all(e["log_level"] == "debug" for e in dropped)
            assert all(set(e) == {"event", "log_level", "flows", "total"} for e in dropped)  # no address
            assert dropped[0]["flows"] == 3 and dropped[-1]["total"] == 4
            assert await _answered(clients[:1], ports[0]) == [True]  # an open flow still carries
            for port in relay.bound:
                port.sweep(loop.time() + 1e9)
            assert relay.count == 0
            assert await _answered(clients[3:4], ports[0]) == [True]  # room again
        finally:
            relay.close()
            for transport, _ in clients:
                transport.close()
            for transport in apis:
                transport.close()

    asyncio.run(go())


def test_the_relay_opens_at_most_eight_flows_from_many_source_ports() -> None:
    """The review's PoC: 300 source ports against one port of the range open 8 flows, not 300."""
    from pisa import voice_relay

    assert (voice_relay.MAX_FLOWS_PER_PORT, voice_relay.MAX_FLOWS) == (8, 256)
    ports = free_range(1)

    async def go() -> int:
        loop = asyncio.get_running_loop()
        api, _ = await loop.create_datagram_endpoint(_Echo, local_addr=("127.0.0.1", ports[0]))
        relay = Relay(ports, "127.0.0.1", listen="127.0.0.2")
        await relay.start()
        senders = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(300)]
        try:
            for sock in senders:
                sock.sendto(b"x", ("127.0.0.2", ports[0]))
            await asyncio.sleep(0.5)
            return relay.count
        finally:
            for sock in senders:
                sock.close()
            relay.close()
            api.close()

    assert asyncio.run(go()) == 8


# --- what `pisa init` writes --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "bind", "host", "hint"),
    [("tailscale", "127.0.0.1", "", None), ("caddy", "0.0.0.0", "pisa.example.com", "open UDP"),
     ("local", "127.0.0.1", "", "LAN address")],
)  # fmt: skip
def test_init_writes_each_mode_s_voice(
    tmp_path: Path, mode: str, bind: str, host: str, hint: str | None
) -> None:
    """tailscale's address is left empty and nothing asks for it: the sidecar writes it
    (PISA_ICE_HOST_FILE)."""
    env = tmp_path / ".env"
    code, printed = init_run(
        env, "--yes", "--mode", mode, "--timezone", "Europe/London", "--domain", "pisa.example.com",
        "--email", "ops@example.com", environ={"TS_AUTHKEY": SYNTHETIC_TS_KEY},
    )  # fmt: skip
    assert code == 0, printed
    values = read_env(env)
    assert values["PISA_VOICE_UDP_PORTS"] == "40000-40019"
    assert (values["PISA_VOICE_UDP_BIND"], values["PISA_ICE_HOST"]) == (bind, host)
    if hint is None:
        assert "PISA_ICE_HOST" not in printed and "tailscale ip" not in printed
    else:
        assert hint in printed


def test_init_keeps_the_voice_values_already_set(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert init_run(env, "--yes", "--mode", "local", "--timezone", "Europe/London")[0] == 0
    text = env.read_text().replace("PISA_ICE_HOST=", "PISA_ICE_HOST=192.0.2.5")
    env.write_text(text.replace("PISA_VOICE_UDP_PORTS=40000-40019", "PISA_VOICE_UDP_PORTS=41000-41004"))
    code, printed = init_run(env, "--yes", "--force", "--mode", "local", "--timezone", "Europe/London")
    assert code == 0, printed
    values = read_env(env)
    assert (values["PISA_ICE_HOST"], values["PISA_VOICE_UDP_PORTS"]) == ("192.0.2.5", "41000-41004")
    assert "PISA_ICE_HOST in" not in printed


def test_init_drops_another_mode_s_voice_values(tmp_path: Path) -> None:
    """caddy's 0.0.0.0 and its domain must not follow into another mode; local's IPv4 --lan-host is the ICE
    host."""
    env = tmp_path / ".env"
    caddy = ("--mode", "caddy", "--domain", "pisa.example.com", "--email", "ops@example.com")
    assert init_run(env, "--yes", "--timezone", "Europe/London", *caddy)[0] == 0
    assert init_run(env, "--yes", "--force", "--mode", "tailscale", "--tailnet", "tail1234.ts.net")[0] == 0
    values = read_env(env)
    assert (values["PISA_VOICE_UDP_BIND"], values["PISA_ICE_HOST"]) == ("127.0.0.1", "")
    assert init_run(env, "--yes", "--force", "--mode", "local", "--lan-host", "pisa.local,192.0.2.7")[0] == 0
    values = read_env(env)
    assert (values["PISA_VOICE_UDP_BIND"], values["PISA_ICE_HOST"]) == ("127.0.0.1", "192.0.2.7")
