"""A real `pisa serve-api` for the live CI checks (`conformance_live.py`, `e2e_live.py`, `api_fuzz.py`,
`client_guide_live.py`), and devices paired with it exactly as the CLI's headless client pairs.

`LiveServer` migrates the database named by PISA_DATABASE_URL, starts the server on 127.0.0.1 (port
PISA_LIVE_PORT, else the caller's default) through `offline_serve.py`, so no socket leaves the machine, and
waits for `/health`; `pair` makes a device by QR (`pisa pair --no-wait --json`, the HMAC and SAS through
`pisa.client.Client`, `pisa pair --confirm <digits>`) and signs it a session token; `consent` records the AI
disclosure as it is now, consented to (no health).

The server's TLS pin (PISA_TLS_CERT, which QR pairing needs) and its secrets key are throwaway keys made here;
the server itself is reached over plain http on 127.0.0.1. With `insecure_loopback`, the server pairs without
any certificate instead, as a developer's machine does (PISA_INSECURE_LOOPBACK_PAIRING, an http loopback
address, the mode's fixed pin). Its own log goes to `server.log` in the work
directory; `dump_log` prints its errors and last lines when a caller fails.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from pisa.client import Client

OFFLINE_SERVE = Path(__file__).with_name("offline_serve.py")  # `pisa serve-api`, with no way off the machine


def run(*args: str, env: dict[str, str], quiet: bool = False) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(args), flush=True)
    done = subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)
    if not quiet or done.returncode != 0:
        print(done.stdout, end="", flush=True)
        print(done.stderr, end="", file=sys.stderr, flush=True)
    return done


@dataclass
class Paired:
    client: Client
    device_id: str
    token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class LiveServer:
    def __init__(self, default_port: int, prefix: str, *, insecure_loopback: bool = False) -> None:
        self.port = int(os.environ.get("PISA_LIVE_PORT", str(default_port)))
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws = f"ws://127.0.0.1:{self.port}"
        self.work = Path(tempfile.mkdtemp(prefix=prefix))
        pin = self.work / "tls-key.pem"  # the throwaway TLS key the QR pins (only its public half)
        pin.write_bytes(
            ec.generate_private_key(ec.SECP256R1())
            .public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        self.env = os.environ | {
            "PISA_TIMEZONE": os.environ.get("PISA_TIMEZONE", "Europe/Rome"),
            "PISA_MIGRATE_DATABASE_URL": os.environ.get(
                "PISA_MIGRATE_DATABASE_URL", os.environ["PISA_DATABASE_URL"]
            ),
            "PISA_API_HOST": "127.0.0.1",
            "PISA_API_PORT": str(self.port),
            "PISA_TLS_CERT": str(pin),
            "PISA_ADDRESSES": f"https://127.0.0.1:{self.port}",
            # a throwaway key, as every real install sets one (prod requires it): stored keys are encrypted
            "PISA_SECRETS_KEY": os.environ.get("PISA_SECRETS_KEY") or Fernet.generate_key().decode(),
        }
        if insecure_loopback:  # no certificate: plain http on loopback, this machine only
            self.env |= {
                "PISA_TLS_CERT": "",
                "PISA_INSECURE_LOOPBACK_PAIRING": "true",
                "PISA_ADDRESSES": self.base,
            }
        self.log = self.work / "server.log"
        self._server: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "LiveServer":
        if run("pisa", "migrate", env=self.env, quiet=True).returncode != 0:
            raise SystemExit("pisa migrate failed")
        with self.log.open("wb") as out:
            self._server = subprocess.Popen(
                [sys.executable, str(OFFLINE_SERVE)], env=self.env, stdout=out, stderr=subprocess.STDOUT
            )
        for _ in range(300):
            if self._server.poll() is not None:
                self.dump_log()
                raise SystemExit("the server exited before answering /health")
            try:
                if httpx.get(f"{self.base}/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.dump_log()
        raise SystemExit("the server never answered /health")

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.terminate()
            try:
                self._server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._server.kill()
        shutil.rmtree(self.work, ignore_errors=True)

    def dump_log(self, lines: int = 60) -> None:
        """The server's errors (each with its path and the exception's last line), then its last lines."""
        if not self.log.exists():
            return
        text = self.log.read_text(errors="replace").splitlines()
        errors: dict[str, None] = {}
        for line in text:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("level") in ("error", "critical"):
                cause = str(record.get("exception") or "").strip().splitlines()[-1:] or [""]
                errors[f"{record.get('path', '')} {record.get('event', '')}: {cause[0][:300]}"] = None
        print("--- the server's errors ---", *errors, sep="\n", file=sys.stderr, flush=True)
        print("--- the server's log (last lines) ---", *text[-lines:], sep="\n", file=sys.stderr, flush=True)

    async def pair(self, name: str) -> Paired:
        """A device paired by QR and confirmed on the server, with a fresh session token."""
        made = run("pisa", "pair", name, "--no-wait", "--json", env=self.env, quiet=True)
        if made.returncode != 0:
            raise SystemExit("pisa pair failed")
        uri = json.loads(made.stdout.strip().splitlines()[-1])["uri"]
        client = Client.new(self.base)
        async with client.http() as http:
            started = await client.pair(http, uri, name)
            confirmed = run("pisa", "pair", "--confirm", started["sas"], env=self.env, quiet=True)
            if (
                confirmed.returncode != 0
                or await client.wait_confirmed(http, started["pairingId"], 30) != "confirmed"
            ):
                raise SystemExit("the pairing was not confirmed")
            return Paired(client, started["deviceId"], await client.session(http))

    async def consent(self, device: Paired) -> None:
        """The AI disclosure as it is now, consented to (so a run reaches the pipeline)."""
        async with httpx.AsyncClient(base_url=self.base, timeout=30) as http:
            revision = (await http.get("/v1/ai/disclosure", headers=device.auth)).json()["revision"]
            answer = await http.post(
                "/v1/ai/consent", json={"revision": revision, "health": False}, headers=device.auth
            )
        if answer.status_code != 204:
            raise SystemExit(f"the consent was refused ({answer.status_code})")


def hello(device_id: str, **extra: Any) -> dict[str, Any]:
    """The app's hello: contract 2.0, AG-UI 1.0, Rings 0 and 1 in the core catalog, every extension."""
    return {
        "op": "hello",
        "contract": {"versions": ["2.0"]},
        "agui": {"protocolVersions": ["1.0"]},
        "a2ui": {"1.0": {"supportedCatalogIds": ["urn:pisa:a2ui:catalog:core:1"]}},
        "rings": [0, 1],
        "extensions": {
            "pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1, "pisa.voice": 1,
            "pisa.directions": 1, "pisa.call": 1, "pisa.compose": 1,
        },  # fmt: skip
        "device": {"deviceId": device_id, "appVersion": "1.0.0", "platform": "cli"},
        **extra,
    }
