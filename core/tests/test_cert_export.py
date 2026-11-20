# SPDX-License-Identifier: AGPL-3.0-only
"""deploy/tailscale/cert-export.sh, the tailscale sidecar's entrypoint, with a mock `tailscale` CLI and a mock
containerboot: it exports the serve certificate and the node's first tailnet IPv4 address to the shared
volume (atomically, 0644), retries every "minute" until the node has an address, and rewrites a stale one.
Only 100.64.0.x addresses."""

import os
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "tailscale" / "cert-export.sh"

# answers `status --json`, `cert --cert-file F --key-file K DOMAIN` and `ip -4` (from $MOCK_IP_FILE: none
# until the test writes it, as before the node's first login)
MOCK_TAILSCALE = """#!/bin/sh
shift  # --socket=...
case "$1" in
  status) echo '{"Self": {}, "CertDomains": ["pisa.tail1234.ts.net"]}' ;;
  cert) echo "CERT for $6" > "$3"; echo KEY > "$5" ;;
  ip) [ -s "$MOCK_IP_FILE" ] || exit 1; cat "$MOCK_IP_FILE" ;;
  *) exit 2 ;;
esac
"""


@pytest.fixture
def sidecar(tmp_path: Path) -> dict[str, Path]:
    bin_dir, out = tmp_path / "bin", tmp_path / "ts-cert"
    bin_dir.mkdir()
    out.mkdir()
    (bin_dir / "tailscale").write_text(MOCK_TAILSCALE)
    (bin_dir / "containerboot").write_text("#!/bin/sh\nexec sleep 30\n")
    for name in ("tailscale", "containerboot"):
        (bin_dir / name).chmod(0o755)
    return {"bin": bin_dir, "out": out, "ip": tmp_path / "mock-ip"}


@contextmanager
def running(paths: dict[str, Path]) -> Iterator[subprocess.Popen[bytes]]:
    env = {
        "PATH": f"{paths['bin']}:{os.environ['PATH']}",
        "TS_SOCKET": "/nonexistent/tailscaled.sock",
        "MOCK_IP_FILE": str(paths["ip"]),
        "CERT_EXPORT_DIR": str(paths["out"]),
        "CERT_EXPORT_BOOT": str(paths["bin"] / "containerboot"),
        "CERT_EXPORT_TICK": "0.1",
    }
    proc = subprocess.Popen(["/bin/sh", str(SCRIPT)], env=env, start_new_session=True)  # noqa: S603
    try:
        yield proc
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def until(check: object, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(check) and check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def test_the_sidecar_writes_its_first_tailnet_ipv4_once_it_has_one(sidecar: dict[str, Path]) -> None:
    ip_file = sidecar["out"] / "ip"
    with running(sidecar):
        until(lambda: read(sidecar["out"] / "cert.pem") == "CERT for pisa.tail1234.ts.net\n")
        time.sleep(0.5)  # a few "minutes" with no address yet: no file
        assert not ip_file.exists()
        sidecar["ip"].write_text("100.64.0.7\n100.64.0.8\n")  # logged in: the first address only
        until(lambda: read(ip_file) == "100.64.0.7\n")
    assert stat.S_IMODE(ip_file.stat().st_mode) == 0o644
    assert sorted(p.name for p in sidecar["out"].iterdir()) == ["cert.pem", "ip"]  # no temporary file left


def test_the_sidecar_rewrites_a_changed_address(sidecar: dict[str, Path]) -> None:
    ip_file = sidecar["out"] / "ip"
    ip_file.write_text("100.64.0.7\n")
    sidecar["ip"].write_text("100.64.0.9\n")
    with running(sidecar):
        until(lambda: read(ip_file) == "100.64.0.9\n")
    assert stat.S_IMODE(ip_file.stat().st_mode) == 0o644


def test_the_sidecar_writes_no_address_that_is_not_one(sidecar: dict[str, Path]) -> None:
    sidecar["ip"].write_text("fd7a:115c:a1e0::7\n")
    with running(sidecar):
        until(lambda: (sidecar["out"] / "cert.pem").exists())
        time.sleep(0.5)
    assert not (sidecar["out"] / "ip").exists()


def test_the_sidecar_ends_with_containerboot(sidecar: dict[str, Path]) -> None:
    (sidecar["bin"] / "containerboot").write_text("#!/bin/sh\nsleep 0.5\nexit 3\n")
    sidecar["ip"].write_text("100.64.0.7\n")
    with running(sidecar) as proc:
        assert proc.wait(timeout=10) == 3
    assert read(sidecar["out"] / "ip") == "100.64.0.7\n"
