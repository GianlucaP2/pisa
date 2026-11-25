# SPDX-License-Identifier: AGPL-3.0-only
"""`pisa voice-relay`: the talk's UDP media from the tailnet to the API, in the compose `tailscale` profile.

The Tailscale sidecar runs in userspace networking (no /dev/net/tun, no NET_ADMIN). There, tailscaled's
netstack hands a UDP flow addressed to the node's own tailnet IP to 127.0.0.1:<the same port> in the sidecar's
own network namespace (tailscale wgengine/netstack `forwardUDP`), never to another container. This relay
shares that namespace (`network_mode: service:tailscale`), listens on 127.0.0.1 at each port of
`PISA_VOICE_UDP_PORTS`, and forwards each flow to the API at the same port (`PISA_VOICE_RELAY_TO`, the compose
service `api`), and its replies back. The API advertises the tailnet address at those ports (`PISA_ICE_HOST`);
ICE sees the relay as a peer-reflexive address and connects through it.

The API itself stays on its own network: sharing the sidecar's namespace would also hand its plain-HTTP port
to any tailnet peer, as if from 127.0.0.1. Packets are copied, never read or logged.

Each new client address costs a socket toward the API, so the flows are capped: `MAX_FLOWS_PER_PORT` on each
port of the range, `MAX_FLOWS` across the relay. A packet that would open a flow beyond either cap is dropped
(the flows already open carry on), with a debug log of the counts only. The cap is per port, not per source
address: tailscaled's netstack delivers every tailnet peer's flow from 127.0.0.1 (at the peer's source port),
so the relay never sees who sent it, and a port of the range is one talk.
"""

import asyncio
import os
import socket
import sys
import time
from collections.abc import Callable

from pisa.logging import get_logger
from pisa.settings import udp_ports

IDLE_S = 120.0  # a flow with no packet either way this long is dropped (as tailscaled's own forwarder does)
# A talk holds one port and uses one flow on it, plus a few for ICE checks from the phone's other candidates.
MAX_FLOWS_PER_PORT = 8
# Across the relay: the default range (20 ports) at one flow per talk plus its checks needs well under 100.
# 256 leaves room for that, a larger range and stale flows awaiting the sweep, and keeps the relay's
# descriptors (one per flow, one per port) far below a container's default soft limit of 1024 open files, so
# a flood of source ports can't exhaust them.
MAX_FLOWS = 256
_log = get_logger("pisa.voice_relay")


class _Back(asyncio.DatagramProtocol):
    """One flow's socket toward the API: what the API sends back goes to the phone's side."""

    def __init__(self, reply: Callable[[bytes], None]) -> None:
        self.reply = reply
        self.seen = time.monotonic()

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        self.seen = time.monotonic()
        self.reply(data)


class _Port(asyncio.DatagramProtocol):
    """One port of the range on 127.0.0.1: each client address is a flow of its own toward `target`."""

    def __init__(self, relay: "Relay", port: int) -> None:
        self.relay, self.port = relay, port
        self.transport: asyncio.DatagramTransport | None = None
        self.flows: dict[tuple[str | int, ...], tuple[asyncio.DatagramTransport, _Back]] = {}
        self._opening: dict[tuple[str | int, ...], list[bytes]] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        if (flow := self.flows.get(addr)) is not None:
            flow[1].seen = time.monotonic()
            flow[0].sendto(data)
        elif (waiting := self._opening.get(addr)) is not None:
            waiting.append(data)
        elif self.count >= self.relay.max_per_port or self.relay.count >= self.relay.max_flows:
            _log.debug("voice_relay_flow_dropped", flows=self.count, total=self.relay.count)
        else:
            self._opening[addr] = [data]
            asyncio.get_running_loop().create_task(self._open(addr))

    @property
    def count(self) -> int:
        """The flows open or opening on this port."""
        return len(self.flows) + len(self._opening)

    async def _open(self, addr: tuple[str | int, ...]) -> None:
        def reply(data: bytes) -> None:
            if self.transport is not None:
                self.transport.sendto(data, addr)

        try:
            transport, back = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Back(reply), remote_addr=(self.relay.target, self.port)
            )
        except OSError as err:  # the API not up yet, its name not resolving: the phone's retry opens it again
            _log.warning("voice_relay_flow_failed", port=self.port, error=type(err).__name__)
            self._opening.pop(addr, None)
            return
        self.flows[addr] = (transport, back)
        # a talk's media path in the logs: counts and the port only, never the phone's address
        _log.info("voice_relay_flow", port=self.port, flows=self.count, total=self.relay.count)
        for data in self._opening.pop(addr, []):
            transport.sendto(data)

    def sweep(self, now: float) -> None:
        for addr, (transport, back) in list(self.flows.items()):
            if now - back.seen >= self.relay.idle_s:
                transport.close()
                del self.flows[addr]

    def close(self) -> None:
        for transport, _ in self.flows.values():
            transport.close()
        self.flows.clear()
        if self.transport is not None:
            self.transport.close()


class Relay:
    """Every port of `ports` on `listen`, each relayed to `target` at the same port."""

    def __init__(
        self, ports: range, target: str, listen: str = "127.0.0.1", idle_s: float = IDLE_S,
        max_per_port: int = MAX_FLOWS_PER_PORT, max_flows: int = MAX_FLOWS,
    ) -> None:  # fmt: skip
        self.ports, self.target, self.listen, self.idle_s = ports, target, listen, idle_s
        self.max_per_port, self.max_flows = max_per_port, max_flows
        self.bound: list[_Port] = []

    @property
    def count(self) -> int:
        """The flows open or opening on every port."""
        return sum(port.count for port in self.bound)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for port in self.ports:
            _, protocol = await loop.create_datagram_endpoint(
                lambda port=port: _Port(self, port), local_addr=(self.listen, port)
            )
            self.bound.append(protocol)

    async def sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(min(self.idle_s, 30.0))
            now = time.monotonic()
            for port in self.bound:
                port.sweep(now)

    def close(self) -> None:
        for port in self.bound:
            port.close()


def held(port: int, listen: str = "127.0.0.1") -> bool:
    """The healthcheck: the range's first port is held (by the relay)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind((listen, port))
        except OSError:
            return True
    return False


def main(check: bool = False) -> int:
    ports = udp_ports(os.environ.get("PISA_VOICE_UDP_PORTS", ""))
    if ports is None:
        print("error: set PISA_VOICE_UDP_PORTS (e.g. 40000-40019)", file=sys.stderr)
        return 2
    if check:
        return 0 if held(ports[0]) else 1
    relay = Relay(ports, os.environ.get("PISA_VOICE_RELAY_TO", "api"))

    async def run() -> None:
        await relay.start()
        _log.info("voice_relay_started", ports=f"{ports.start}-{ports.stop - 1}", to=relay.target)
        try:
            await relay.sweep_forever()
        finally:
            relay.close()

    asyncio.run(run())
    return 0
