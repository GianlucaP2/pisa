"""Insecure loopback mode (PISA_INSECURE_LOOPBACK_PAIRING, a developer's machine): pairing and everything
after it travel over plain http, so the API answers this machine only.

`LoopbackOnly` lets a request in (HTTP or the WebSocket) only from a loopback peer, or, when Pisa runs in a
container and the owner set PISA_INSECURE_LOOPBACK_GATEWAY, from the container's default gateway: the host
side of the compose network, which is where the host's own loopback connections arrive from under rootful
Docker with the port published on 127.0.0.1. (Under Docker Desktop, rootless Docker or Podman, or the
userland proxy, other machines can arrive from that address too: hence the explicit setting.) A request that
came through a proxy, a tunnel or a CDN (any of the headers in `_PROXIED`) is refused too: a front means the
server is meant to be reached from elsewhere, which this mode is not for. A raw TCP tunnel adds no header and
defeats the mode: it is for one machine only.

Anything else gets 403 `auth_failed` (the socket's handshake is refused), and the API logs a warning at start.
"""

import ipaddress
import socket
import struct
from pathlib import Path

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from pisa.logging import get_logger
from pisa.settings import Settings, in_container

_log = get_logger("pisa.loopback")
# the headers a proxy, a tunnel or a CDN in front adds; any of them refuses the request. A raw TCP
# tunnel (`ssh -R`, socat, an editor's port forwarding) adds none: the mode is for one machine only
_PROXIED = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-real-ip",
        b"via",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"cf-connecting-ip",
        b"true-client-ip",
        b"x-client-ip",
        b"fastly-client-ip",
    }
)
ROUTE_TABLE = Path("/proc/net/route")


def default_gateway(table: Path = ROUTE_TABLE) -> str | None:
    """The IPv4 default gateway from the kernel's route table (Linux), or None."""
    try:
        lines = table.read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            try:
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
            except (ValueError, struct.error):
                return None
    return None


def local_peers(container: bool, gateway: str | None, trust_gateway: bool = False) -> frozenset[str]:
    """The non-loopback peers that are this machine: in a container, its default gateway, and only when the
    owner said so (PISA_INSECURE_LOOPBACK_GATEWAY: rootful Docker on Linux with the port published on
    127.0.0.1 only; under Docker Desktop, rootless Docker or Podman, or the userland proxy, other machines can
    arrive from the gateway too)."""
    return frozenset({gateway}) if container and gateway and trust_gateway else frozenset()


def is_local(peer: str | None, extra: frozenset[str]) -> bool:
    if not peer:
        return False
    try:
        if ipaddress.ip_address(peer).is_loopback:
            return True
    except ValueError:
        return False
    return peer in extra


class LoopbackOnly:
    """The ASGI guard of insecure loopback mode (see the module's docstring)."""

    def __init__(self, app: ASGIApp, extra: frozenset[str]) -> None:
        self.app = app
        self.extra = extra

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        peer = client[0] if client else None
        proxied = any(name.lower() in _PROXIED for name, _ in scope.get("headers", []))
        if is_local(peer, self.extra) and not proxied:
            await self.app(scope, receive, send)
            return
        _log.warning("insecure_loopback_refused", proxied=proxied)
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008, "reason": "loopback only"})
            return
        response = JSONResponse(
            {
                "error": {
                    "code": "auth_failed",
                    "message": "This server is in insecure loopback mode "
                    "(PISA_INSECURE_LOOPBACK_PAIRING): it answers only this machine, directly.",
                    "action": "Connect from the server's own machine, or set up TLS (PISA_TLS_CERT) and turn "
                    "the mode off",
                }
            },
            status_code=403,
        )
        await response(scope, receive, send)


def allowed_peers(settings: Settings) -> frozenset[str]:
    """The non-loopback peers `LoopbackOnly` lets in on this machine, with the mode's warning in the log."""
    container = in_container()
    extra = local_peers(container, default_gateway(), settings.insecure_loopback_gateway)
    _log.warning(
        "insecure_loopback_pairing",
        why="PISA_INSECURE_LOOPBACK_PAIRING is on: pairing and every request travel as plain http; only this "
        "machine is answered. For development only.",
        container=container,
        gateway=next(iter(extra), None),
        peers=["loopback", *sorted(extra)],
        api_host=settings.api_host,
    )
    return extra
