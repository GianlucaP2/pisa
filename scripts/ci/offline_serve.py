"""`pisa serve-api` with no way out of the machine, for the live CI checks (`live_server.py`).

The fuzzer stores made-up provider keys and connector credentials and then pokes the routes that use them;
the server would send those to the real providers. Here every socket the server opens to an address other than
loopback is refused (ENETUNREACH), and every name but localhost fails to resolve, so a provider call fails as
an unreachable upstream does and nothing leaves the machine. Postgres is reached by libpq, on 127.0.0.1.

Run as the server's command: `python offline_serve.py` (with the env `pisa serve-api` reads).
"""

import errno
import ipaddress
import socket
import sys
from typing import Any

from pisa.cli import main

LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_getaddrinfo = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _local(address: Any) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX
    try:
        return ipaddress.ip_address(str(address[0]).split("%")[0]).is_loopback
    except ValueError:
        return str(address[0]) in LOCAL_NAMES


def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    name = host.decode() if isinstance(host, bytes) else host
    if name is not None and not _local((name, 0)):
        raise socket.gaierror(socket.EAI_NONAME, f"offline: {name} is not resolved here")
    return _getaddrinfo(host, *args, **kwargs)


def connect(self: socket.socket, address: Any) -> None:
    if not _local(address):
        raise OSError(errno.ENETUNREACH, f"offline: no route to {address[0]}")
    return _connect(self, address)


def connect_ex(self: socket.socket, address: Any) -> int:
    return _connect_ex(self, address) if _local(address) else errno.ENETUNREACH


socket.getaddrinfo = getaddrinfo
socket.socket.connect = connect  # type: ignore[method-assign]
socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]

if __name__ == "__main__":
    sys.exit(main(["serve-api"]))
