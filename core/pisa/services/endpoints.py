"""The server's addresses and TLS pins (CONTRACT-2 §3.4): what the pairing QR carries, and what
`GET /v1/server/endpoints` and the `pisa.endpoints` event tell a paired app after a change.

The pins come from PISA_TLS_CERT (the certificate the app pins, or its public key) and PISA_TLS_NEXT_KEY (the
next key, announced before a rotation so the app already trusts it when the server switches): each a PEM
file's path, or the PEM itself inline. The addresses come from PISA_ADDRESSES. Files are read on every call: a
rotation needs no restart of the API, and the next connect of each app is told.

Without PISA_TLS_CERT, PISA_TLS_PIN_FROM (`host:port`, docker compose's caddy mode: `caddy:443`) pins the leaf
certificate the TLS proxy presents there for the first https address's host, read over a handshake that must
pass WebPKI verification (the system's roots and the host name): a certificate that fails it is never pinned.
A pin read is kept for 10 minutes (a failure for 30 seconds), so a connect does not wait on a handshake each
time.

The pin model (stated the same in docs-site/self-hosting.md "The pin"): the pin
authenticates pairing. The QR pins, per compose mode: tailscale, the current leaf `tailscale serve` presents,
exported by the sidecar; caddy, the leaf Caddy presents, observed and WebPKI-verified; local, Pisa's own key.
After pairing the app relies on WebPKI in tailscale and caddy modes (a later pin reaches it in
`pisa.endpoints`), and on the pin in local mode, where no authority vouches for the key. The command-line
client still pins on every connection (a planned change). A server identity apart from TLS (D6b) is a contract
2.x change.

On a developer's machine, PISA_INSECURE_LOOPBACK_PAIRING pairs without TLS: the pin is the fixed
`INSECURE_LOOPBACK_PIN`, every address is http on loopback, and the API answers this machine only
(`pisa.settings` refuses the setting otherwise, `pisa.api.loopback` refuses other peers).
"""

import socket
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pisa.domain import pairing as rules
from pisa.domain.errors import PisaError
from pisa.logging import get_logger
from pisa.settings import Settings

_log = get_logger("pisa.endpoints")

# docker compose's tailscale mode: the certificate `tailscale serve` presents, exported by the sidecar.
TAILSCALE_CERT_DIR = "/etc/pisa/ts-cert/"
OBSERVED_TTL, FAILED_TTL = 600.0, 30.0
_observed: dict[tuple[str, str], tuple[float, str | PisaError]] = {}


def _pin(value: str, setting: str) -> str:
    inline = rules.tls_pem(value)
    where = "inline" if inline is not None else value
    try:
        return rules.spki_pin(inline if inline is not None else Path(value).read_text())
    except FileNotFoundError:
        if not value.startswith(TAILSCALE_CERT_DIR):
            raise PisaError(
                "validation_failed",
                f"{setting} ({where}) names a file that does not exist.",
                f"Point {setting} at the PEM file of the server's TLS certificate, or put the PEM in it",
            ) from None
        raise PisaError(
            "validation_failed",
            "Tailscale hasn't fetched its certificate yet, so there is no pin for the pairing QR.",
            "Check that HTTPS certificates are enabled for the tailnet (the DNS page of the Tailscale admin "
            "console) and wait a minute (`docker compose logs tailscale`)",
        ) from None
    except (OSError, ValueError) as exc:
        raise PisaError(
            "validation_failed",
            f"{setting} ({where}) is not a readable PEM certificate or public key "
            f"({exc.__class__.__name__}).",
            f"Point {setting} at the PEM file of the server's TLS certificate, or put the PEM itself in it",
        ) from None


def webpki_context() -> ssl.SSLContext:
    """The trust a PISA_TLS_PIN_FROM handshake must pass: the system's WebPKI roots, the host name checked."""
    return ssl.create_default_context()


def _target(value: str) -> tuple[str, int]:
    host, sep, port = value.strip().rpartition(":")
    if not sep:
        return value.strip(), 443
    if not host or not port.isdigit():
        raise PisaError(
            "validation_failed",
            f"PISA_TLS_PIN_FROM ({value}) is not host:port.",
            "Set PISA_TLS_PIN_FROM to where the TLS proxy listens, e.g. caddy:443",
        )
    return host.strip("[]"), int(port)


def observed_pin(settings: Settings) -> str:
    """The pin of the leaf certificate the proxy at PISA_TLS_PIN_FROM presents for the first https address's
    host, after WebPKI verification; never a certificate that fails it."""
    names = [h for a in settings.address_list if (p := urlsplit(a)).scheme == "https" and (h := p.hostname)]
    if not names:
        raise PisaError(
            "validation_failed",
            "PISA_TLS_PIN_FROM needs the domain clients use: PISA_ADDRESSES has no https:// address.",
            "Set PISA_ADDRESSES=https://<your domain>",
        )
    key = (settings.tls_pin_from.strip(), names[0])
    now = time.monotonic()
    if (kept := _observed.get(key)) and now < kept[0]:
        if isinstance(kept[1], PisaError):
            raise kept[1]
        return kept[1]
    try:
        pin = _handshake(*_target(key[0]), key[1])
    except PisaError as err:
        _observed[key] = (now + FAILED_TTL, err)
        raise
    _observed[key] = (now + OBSERVED_TTL, pin)
    return pin


def _handshake(host: str, port: int, name: str) -> str:
    try:
        with (
            socket.create_connection((host, port), timeout=5) as raw,
            webpki_context().wrap_socket(raw, server_hostname=name) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
    except ssl.SSLCertVerificationError as exc:
        _log.warning("tls_pin_unverified", target=f"{host}:{port}", reason=exc.verify_message)
        raise PisaError(
            "validation_failed",
            f"The certificate {host}:{port} presents for {name} fails WebPKI verification "
            f"({exc.verify_message}), so it is not pinned.",
            "Check that the proxy has a valid certificate for the domain (docker compose logs caddy: the "
            "first one can take a minute) and that PISA_ADDRESSES names that domain",
        ) from None
    except (OSError, ssl.SSLError) as exc:
        raise PisaError(
            "validation_failed",
            f"Pisa can't read the TLS certificate at {host}:{port} for {name} ({exc.__class__.__name__}).",
            "Check that the proxy runs (docker compose ps) and has its certificate for the domain",
        ) from None
    if not der:
        raise PisaError("validation_failed", f"{host}:{port} presented no certificate.", "Check the proxy")
    return rules.spki_pin_der(der)


def pins(settings: Settings) -> list[str]:
    """The current pin, then the next one when a rotation is announced; empty without PISA_TLS_CERT or
    PISA_TLS_PIN_FROM. In insecure loopback mode, the fixed loopback pin alone."""
    if settings.insecure_loopback_pairing:
        return [rules.INSECURE_LOOPBACK_PIN]
    if not settings.tls_cert:
        return [observed_pin(settings)] if settings.tls_pin_from.strip() else []
    out = [_pin(settings.tls_cert, "PISA_TLS_CERT")]
    if settings.tls_next_key:
        nxt = _pin(settings.tls_next_key, "PISA_TLS_NEXT_KEY")
        if nxt not in out:
            out.append(nxt)
    return out


def not_configured() -> PisaError:
    return PisaError(
        "not_found",
        "This server has no addresses or TLS pin configured, so it has no endpoints to tell and no QR to "
        "show.",
        "Set PISA_ADDRESSES and PISA_TLS_CERT (or PISA_TLS_PIN_FROM) on the server (see .env.example) and "
        "restart it",
    )


def current(settings: Settings) -> dict[str, Any]:
    """`$defs/s_pisa_endpoints`."""
    addresses, found = settings.address_list, pins(settings)
    if not addresses or not found:
        raise not_configured()
    try:
        return rules.endpoints(addresses, found)
    except rules.PairingInputError as err:
        raise PisaError(
            "validation_failed",
            f"PISA_ADDRESSES has an address Pisa cannot use: {str(err).rstrip('.')}.",
            "Fix PISA_ADDRESSES",
        ) from None


def revision(settings: Settings) -> str | None:
    """The endpoints' revision, or None when they are not configured (or not readable)."""
    try:
        return current(settings)["revision"]
    except PisaError:
        return None
