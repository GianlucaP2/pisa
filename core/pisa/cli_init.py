# SPDX-License-Identifier: AGPL-3.0-only
"""`pisa init`: the first-run wizard of the docker compose bundle, for `pisa.cli`.

It writes `deploy/.env` (mode 0600): the database passwords and the key that encrypts stored provider keys,
made here with `secrets`; the reach mode (the compose profile: `tailscale`, `caddy` or `local`) and what that
mode needs; the user's time zone and language. In local mode it also makes the server's own self-signed TLS
key and certificate in deploy/tls/ (`local_tls`), once: clients trust them by the pin the pairing QR carries.
It never prints a secret. An `.env` that is already configured
is left alone unless `--force`, and even then its secrets are kept unless `--rotate-secrets`.

It runs anywhere: on a developer's machine, or inside the API image through `docker compose run` (the install
scripts), so the host needs no Python. It reads no server settings (none exist yet).

Provider API keys are not in `.env`: the server keeps them encrypted in its database (`pisa setup`, or
`pisa secrets set`), so the last step it prints is `pisa setup`, once the stack runs.
"""

import argparse
import base64
import getpass
import ipaddress
import os
import re
import secrets
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODES = ("tailscale", "caddy", "local")
DEFAULT_MODE = "tailscale"
DEFAULT_ENV_FILE = "deploy/.env"
TAILSCALE_KEYS_PAGE = "https://console.tailscale.com/admin/settings/keys"
TAILSCALE_DNS_PAGE = "https://console.tailscale.com/admin/dns"

# Generated once, kept on every re-run unless --rotate-secrets.
PASSWORDS = ("POSTGRES_PASSWORD", "PISA_MIGRATE_PASSWORD", "PISA_APP_PASSWORD", "PISA_READONLY_PASSWORD")
SECRETS = (*PASSWORDS, "PISA_SECRETS_KEY")
# A stored password shorter than this, or a known placeholder (any case), is refused, also on --force: a
# hand-filled .env's `changeme` is not kept (E1-PACK PK-4). The generated ones are 48 hex characters.
MIN_PASSWORD = 16
PLACEHOLDERS = frozenset({
    "init", "changeme", "change-me", "change_me", "changeit", "password", "passw0rd", "postgres", "pisa",
    "secret", "admin", "root", "test", "dev", "example", "placeholder", "default", "none", "null",
    "pisa_app", "pisa_migrate", "pisa_readonly", "1234567890123456", "passwordpassword",
})  # fmt: skip
# Typed by the user, never printed either.
USER_SECRETS = ("TS_AUTHKEY",)
# What each mode needs in .env besides the common keys (the compose file's names for them).
MODE_KEYS: dict[str, tuple[str, ...]] = {
    "tailscale": ("TS_AUTHKEY", "TS_HOSTNAME"),
    "caddy": ("PISA_DOMAIN", "PISA_ACME_EMAIL"),
    "local": ("PISA_LAN_BIND", "PISA_LAN_PORT"),
}
LAN_BIND, LAN_PORT = "127.0.0.1", "8443"  # the compose file's defaults: this machine only
# The local mode's TLS pair, in deploy/tls/ on the host, mounted read-only at /etc/pisa/tls in the containers.
TLS_KEY_FILE, TLS_CERT_FILE = "key.pem", "cert.pem"
CONTAINER_CERT = "/etc/pisa/tls/cert.pem"
# tailscale: the certificate `tailscale serve` presents, exported by the sidecar (deploy/tailscale/).
TAILSCALE_CERT = "/etc/pisa/ts-cert/cert.pem"
# caddy: the leaf Caddy presents, read and WebPKI-verified at pair time (pisa.services.endpoints).
CADDY_PIN_FROM = "caddy:443"
# The image's own user (core/Dockerfile): who api-lan runs as unless the key belongs to someone else.
IMAGE_UID = 10001
# Ten years: the pin, not a certificate authority, is what clients trust, so expiry adds nothing but a
# re-pairing. Rotate by hand (--rotate-tls) when the key may have leaked.
TLS_DAYS = 3650
MAX_LAN_HOSTS = 4
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
LABEL_RE = re.compile(rf"^{_LABEL}$")
DOMAIN_RE = re.compile(rf"^(?=.{{1,253}}$)({_LABEL}\.)+[a-z]([a-z0-9-]{{0,61}}[a-z0-9])?$")
# What Let's Encrypt takes, and nothing that could end the Caddyfile's `email` line (spaces, braces, quotes).
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")


ANY_V4 = "0.0.0.0"  # noqa: S104 (a value written to .env: the compose file publishes on it)
VOICE_KEYS = ("PISA_VOICE_UDP_PORTS", "PISA_VOICE_UDP_BIND", "PISA_ICE_HOST")


class InitError(Exception):
    """A refusal with what to do, printed as `error: ...` (exit 2)."""


def new_password() -> str:
    """Hex: safe inside a database URL with no escaping."""
    return secrets.token_hex(24)


def new_fernet_key() -> str:
    """The format `cryptography.fernet.Fernet.generate_key` makes: 32 random bytes, url-safe base64."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines; comments and blank lines skipped, surrounding quotes removed."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def weak_password(value: str) -> bool:
    """Too short, a known placeholder (`init`, `changeme`, `password`, `<...>` as examples write one), or one
    character repeated."""
    word = value.strip().lower()
    return (
        len(value) < MIN_PASSWORD
        or word in PLACEHOLDERS
        or (word.startswith("<") and word.endswith(">"))
        or len(set(word)) <= 2
    )


def refuse_weak(values: dict[str, str], shown: str) -> None:
    """A weak password kept from the old file stops init: the user rotates it. Names only, never values."""
    weak = [key for key in PASSWORDS if values.get(key) and weak_password(values[key])]
    if weak:
        one = len(weak) == 1
        raise InitError(
            f"{', '.join(weak)} in {shown} {'is' if one else 'are'} too weak (shorter than {MIN_PASSWORD} "
            "characters, or a placeholder such as `changeme`), so nothing was changed. Rotate "
            f"{'it' if one else 'them'}: for a database not created yet, run again with --force "
            "--rotate-secrets; for one already running, give the role a new password in Postgres (ALTER ROLE "
            "... PASSWORD, one made with `openssl rand -hex 24`) and put the same one in the file"
        )


def is_configured(values: dict[str, str]) -> bool:
    """An .env `pisa init` (or a person) already filled: it has the database's password. An empty file, or
    the install script's placeholder, is not."""
    return bool(values.get("POSTGRES_PASSWORD"))


def lan_hosts(text: str) -> list[str]:
    """The comma-separated names or addresses the phone uses on the home network, checked."""
    hosts: list[str] = []
    for raw in text.split(","):
        host = raw.strip().strip("[]").lower().rstrip(".")
        if not host:
            continue
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            if not (LABEL_RE.match(host) or DOMAIN_RE.match(host)) or host.endswith(".ts.net"):
                raise InitError(
                    f"{host!r} is not a name or address on your network: use e.g. --lan-host pisa.local or "
                    "--lan-host 192.168.1.20"
                ) from None
        if host not in hosts:
            hosts.append(host)
    if not hosts:
        raise InitError("local mode needs this machine's name or address: pass --lan-host, e.g. pisa.local")
    if len(hosts) > MAX_LAN_HOSTS:
        raise InitError(f"at most {MAX_LAN_HOSTS} names or addresses, not {len(hosts)}")
    return hosts


def default_lan_host(environ: dict[str, str]) -> str:
    """This machine's mDNS name (`<hostname>.local`). The install scripts pass the host's name in
    PISA_HOST_NAME: inside a container, the hostname is the container's."""
    name = (environ.get("PISA_HOST_NAME") or socket.gethostname()).strip().lower().rstrip(".")
    name = name if "." in name else f"{name}.local"
    try:
        return lan_hosts(name)[0]
    except InitError:
        return "pisa.local"


def https_url(host: str, port: str) -> str:
    return f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"


def _subject_names(hosts: list[str]) -> list[Any]:
    from cryptography import x509

    names: list[Any] = []
    for host in [*hosts, "localhost", "127.0.0.1"]:
        try:
            name: Any = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            name = x509.DNSName(host)
        if name not in names:
            names.append(name)
    return names


_NO_USAGE = (
    "content_commitment", "key_encipherment", "data_encipherment", "key_agreement", "key_cert_sign",
    "crl_sign",
    "encipher_only", "decipher_only",
)  # fmt: skip


def make_certificate(key: Any, hosts: list[str]) -> bytes:
    """A self-signed P-256 server certificate for `hosts` (and this machine's loopback), PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0][:64])])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=TLS_DAYS))
        .add_extension(x509.SubjectAlternativeName(_subject_names(hosts)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, **dict.fromkeys(_NO_USAGE, False)), critical=True
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _covers(cert_pem: bytes, hosts: list[str]) -> bool:
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (ValueError, x509.ExtensionNotFound):
        return False
    return all(name in names for name in _subject_names(hosts))


def _owner(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_uid, st.st_gid


def _lan_user(key_path: Path, values: dict[str, str]) -> None:
    """Who api-lan runs as: the key's owner, so a 0600 key is readable by it and by no one else.

    On Linux the install script runs init as the host user (`--user`), so the key is that user's and api-lan
    runs as that uid (PISA_LAN_UID/GID). Run by root, the install script gives the deploy folder to the
    image's user first and runs init as that user (`--user 10001:10001`), so the pair is created as theirs and
    nothing is chowned: the container has no capabilities, CAP_CHOWN included. Run as root some other way,
    init chowns only a file that isn't the image's user's already, and stops with what to do if it can't. A
    filesystem that shows root as every file's owner to a non-root process (Docker Desktop's bind mounts on
    Windows) keeps no ownership: the image's user reads it there."""
    uid, gid = _owner(key_path)
    if uid == 0 and hasattr(os, "geteuid") and os.geteuid() == 0:
        for path in (key_path, key_path.with_name(TLS_CERT_FILE)):
            if _owner(path)[0] == IMAGE_UID:
                continue
            try:
                os.chown(path, IMAGE_UID, IMAGE_UID)
            except PermissionError:
                who = f"{IMAGE_UID}:{IMAGE_UID}"
                raise InitError(
                    f"can't give {path.name} to the image's user (uid {IMAGE_UID}): this container may not "
                    f"change owners. Give the folder to that user (chown -R {who} <the deploy folder>) and "
                    f"run init as it (--user {who}), as install.sh does when run as root"
                ) from None
        uid = IMAGE_UID
    if uid in (0, IMAGE_UID):
        values.pop("PISA_LAN_UID", None)
        values.pop("PISA_LAN_GID", None)
    else:
        values["PISA_LAN_UID"], values["PISA_LAN_GID"] = str(uid), str(gid)


def local_tls(
    env_dir: Path,
    values: dict[str, str],
    *,
    rotate: bool = False,
    say: Callable[[str], None] = print,
    shown: str = "",
) -> None:
    """The `local` mode's TLS: this machine's self-signed P-256 key and certificate in `env_dir/tls` (the key
    0600, the certificate 0644: the worker reads it for `pisa pair`), made once and kept on every re-run
    unless `rotate`. Sets PISA_TLS_CERT (the pin the QR carries) and who api-lan runs as. Never prints the
    key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from pisa.domain import pairing as rules

    hosts = [urlsplit(a).hostname or "" for a in values.get("PISA_ADDRESSES", "").split(",") if a.strip()]
    tls = env_dir / "tls"
    shown_dir = shown or str(tls)  # the folder as the user sees it (not /setup inside the install container)
    tls.mkdir(parents=True, exist_ok=True)
    try:
        tls.chmod(0o755)  # the worker (another uid) reads the certificate through it
    except OSError:
        pass
    key_path, cert_path = tls / TLS_KEY_FILE, tls / TLS_CERT_FILE
    key: Any = None
    if key_path.exists() and not rotate:
        try:
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError):
            raise InitError(
                f"{shown_dir}/{TLS_KEY_FILE} is not a readable private key; --rotate-tls makes a new one "
                "(every paired device must then pair again)"
            ) from None
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise InitError(f"{shown_dir}/{TLS_KEY_FILE} is not an EC key; --rotate-tls makes a new one")
    made_key = key is None
    if made_key:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        write_private(key_path, pem)
    cert_pem = cert_path.read_bytes() if cert_path.exists() and not made_key else b""
    if cert_pem:
        try:
            same = rules.spki_pin(cert_pem.decode()) == rules.spki_pin(
                key.public_key()
                .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
                .decode()
            )
        except ValueError:
            same = False
        if not same:
            cert_pem = b""  # not this key's: a new certificate for the same key keeps the pin
    if not cert_pem:
        cert_pem = make_certificate(key, hosts)
        write_private(cert_path, cert_pem, mode=0o644)
    elif not _covers(cert_pem, hosts):
        say(
            f"Note: the certificate in {shown_dir}/{TLS_CERT_FILE} does not name {', '.join(hosts)}. "
            "Clients check the pin, not the name, so pairing still works; --rotate-tls remakes it (every "
            "paired device must then pair again)."
        )
    _lan_user(key_path, values)
    values["PISA_TLS_CERT"] = CONTAINER_CERT
    pin = rules.spki_pin(cert_pem.decode())
    if made_key and rotate:
        say(
            f"Made a new TLS key in {shown_dir}. Every paired device must pair again (`pisa pair`): "
            "they trust the old key only."
        )
    elif made_key:
        say(f"Made this server's TLS key and self-signed certificate in {shown_dir} (the key: yours only).")
    else:
        say(
            f"Kept the TLS key in {shown_dir} (--rotate-tls makes a new one; paired devices then pair again)."
        )
    say(f"Its pin, which the pairing QR carries: {pin}")


class Prompt:
    """Questions on a terminal, or none (`--yes`, or no terminal): then the flag's value or the default."""

    def __init__(
        self, interactive: bool, *, ask: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass, out: TextIO | None = None,
    ) -> None:  # fmt: skip
        self.interactive = interactive
        self._ask, self._secret = ask, secret
        self.out = out or sys.stdout

    def say(self, line: str = "") -> None:
        print(line, file=self.out)

    def ask(self, question: str, given: str | None, default: str = "") -> str:
        if given is not None:
            return given.strip()
        if not self.interactive:
            return default
        answer = self._ask(f"{question} [{default}]: " if default else f"{question}: ").strip()
        return answer or default

    def secret(self, question: str, from_env: str | None, current: str) -> str:
        """A hidden answer; Enter keeps the current one. Without a terminal: the environment's value."""
        if from_env:
            return from_env.strip()
        if not self.interactive:
            return current
        kept = " (Enter keeps the one saved)" if current else ""
        return self._secret(f"{question}{kept}: ").strip() or current


def build(
    args: argparse.Namespace, old: dict[str, str], prompt: Prompt, environ: dict[str, str]
) -> dict[str, str]:
    """The new .env's values: the old file's, updated by the answers, with secrets made where missing."""
    values = dict(old)
    if not args.rotate_secrets:
        refuse_weak(values, args.env_file)
    for key in SECRETS:
        if args.rotate_secrets or not values.get(key):
            values[key] = new_fernet_key() if key == "PISA_SECRETS_KEY" else new_password()

    mode = prompt.ask(
        "How will your phone reach Pisa? tailscale (no domain, no open port), caddy (your own domain), "
        "or local (this network only)",
        args.mode,
        saved if (saved := old.get("COMPOSE_PROFILES", "")) in MODES else DEFAULT_MODE,
    ).lower()
    if mode not in MODES:
        raise InitError(f"the mode must be one of {', '.join(MODES)}, not {mode!r}")
    values["COMPOSE_PROFILES"] = mode
    if old.get("COMPOSE_PROFILES") and old["COMPOSE_PROFILES"] != mode:
        values.pop("PISA_ADDRESSES", None)  # the old mode's way in; this mode sets its own (or none)
    # the pin each mode sets for itself (a PISA_TLS_CERT of your own is kept, and wins)
    if values.get("PISA_TLS_CERT") in (CONTAINER_CERT, TAILSCALE_CERT):
        values.pop("PISA_TLS_CERT")
    if values.get("PISA_TLS_PIN_FROM") == CADDY_PIN_FROM:
        values.pop("PISA_TLS_PIN_FROM")

    zone = prompt.ask(
        "Your home time zone (an IANA name, e.g. Europe/London)", args.timezone, old.get("PISA_TIMEZONE", "")
    )
    if not zone:
        raise InitError("a time zone is needed: pass --timezone, e.g. --timezone Europe/London")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise InitError(f"{zone!r} is not a time zone name: use one such as Europe/London") from None
    values["PISA_TIMEZONE"] = zone
    language = prompt.ask(
        "The language Pisa speaks (en or it)", args.language, old.get("PISA_LANGUAGE") or "en"
    )
    if language not in ("en", "it"):
        raise InitError(f"the language must be en or it, not {language!r}")
    values["PISA_LANGUAGE"] = language
    values.setdefault("PISA_ENV", "prod")
    if args.image is not None:
        values["PISA_IMAGE"] = args.image.strip()
    values.setdefault("PISA_IMAGE", "")

    if mode == "tailscale":
        prompt.say(
            "Tailscale: Pisa joins your tailnet with an auth key. Make one on the Keys page of the Tailscale "
            f"admin console ({TAILSCALE_KEYS_PAGE}): reusable, not ephemeral. Your phone needs the Tailscale "
            "app, signed in to the same tailnet."
        )
        values["TS_AUTHKEY"] = prompt.secret(
            "Tailscale auth key (hidden; empty: add TS_AUTHKEY to the .env later)",
            environ.get("TS_AUTHKEY"),
            old.get("TS_AUTHKEY", ""),
        )
        name = prompt.ask(
            "This server's name on the tailnet", args.hostname, old.get("TS_HOSTNAME") or "pisa"
        ).lower()
        if not LABEL_RE.match(name):
            raise InitError(
                f"{name!r} is not a machine name: letters, digits and dashes, e.g. --hostname pisa"
            )
        values["TS_HOSTNAME"] = name
        saved = old.get("PISA_ADDRESSES", "").removeprefix(f"https://{old.get('TS_HOSTNAME', '')}.")
        tailnet = (
            prompt.ask(
                "Your tailnet's DNS name, from the DNS page of the Tailscale admin console "
                f"({TAILSCALE_DNS_PAGE}), e.g. tail1234.ts.net (empty: add PISA_ADDRESSES later)",
                args.tailnet,
                saved if saved.endswith(".ts.net") else "",
            )
            .lower()
            .strip(".")
        )
        if tailnet:
            if not (DOMAIN_RE.match(tailnet) and tailnet.endswith(".ts.net")):
                raise InitError(
                    f"{tailnet!r} is not a tailnet DNS name: it ends in .ts.net, e.g. "
                    "--tailnet tail1234.ts.net"
                )
            values["PISA_ADDRESSES"] = f"https://{name}.{tailnet}"
        values.setdefault("PISA_TLS_CERT", TAILSCALE_CERT)
    elif mode == "caddy":
        domain = prompt.ask(
            "Your domain, pointing at this machine (e.g. pisa.example.com)",
            args.domain,
            old.get("PISA_DOMAIN", ""),
        )
        domain = domain.removeprefix("https://").rstrip("/").lower()
        if not domain:
            raise InitError("caddy mode needs a domain name: pass --domain, e.g. --domain pisa.example.com")
        if not DOMAIN_RE.match(domain):
            raise InitError(f"{domain!r} is not a domain name: pass --domain, e.g. --domain pisa.example.com")
        email = prompt.ask(
            "An email for Let's Encrypt (required: certificate expiry notices)",
            args.email,
            old.get("PISA_ACME_EMAIL", ""),
        )
        if not email:
            raise InitError(
                "caddy mode needs an email for Let's Encrypt: pass --email, e.g. --email you@example.com"
            )
        if not EMAIL_RE.match(email):
            raise InitError("the ACME email does not look like an address: pass --email you@example.com")
        values["PISA_DOMAIN"], values["PISA_ACME_EMAIL"] = domain, email
        if values.get("PISA_ADDRESSES", "") in ("", f"https://{old.get('PISA_DOMAIN', '')}"):
            values["PISA_ADDRESSES"] = f"https://{domain}"
        if not values.get("PISA_TLS_CERT"):
            values["PISA_TLS_PIN_FROM"] = CADDY_PIN_FROM
    else:
        prompt.say(
            "Local: Pisa makes its own self-signed TLS certificate on this machine, and the app pins it when "
            "you pair. Only devices on your network reach it."
        )
        values["PISA_LAN_BIND"] = values.get("PISA_LAN_BIND") or LAN_BIND
        port = values["PISA_LAN_PORT"] = values.get("PISA_LAN_PORT") or LAN_PORT
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise InitError(f"PISA_LAN_PORT must be a port number, not {port!r}")
        saved = [] if old.get("COMPOSE_PROFILES") != "local" else old.get("PISA_ADDRESSES", "").split(",")
        saved_hosts = [h for a in saved if a.strip() and (h := urlsplit(a.strip()).hostname)]
        hosts = lan_hosts(
            prompt.ask(
                "The name or address your phone uses for this machine on your network (several: separate "
                "them "
                "with commas)",
                args.lan_host,
                ",".join(saved_hosts) or default_lan_host(environ),
            )
        )
        values["PISA_ADDRESSES"] = ",".join(https_url(h, port) for h in hosts)
    values.update(voice_values(mode, values, old))
    return values


def voice_values(mode: str, values: dict[str, str], old: dict[str, str]) -> dict[str, str]:
    """The talk's media (deploy/docker-compose.yml): its UDP range, where it is published, the address the
    phone reaches it at. Kept when the old file has them; tailscale's address (the node's tailnet IP) and
    local's LAN address are known only on the machine, so they start empty (`next_steps` says how)."""
    host = values.get("PISA_DOMAIN", "") if mode == "caddy" else ""
    if mode == "local" and (lan := values.get("PISA_LAN_BIND") or old.get("PISA_LAN_BIND", "")):
        host = "" if lan in (ANY_V4, "127.0.0.1", "::") else lan
    if mode == "local" and not host:  # an IPv4 address given with --lan-host is where the phone reaches it
        for address in values.get("PISA_ADDRESSES", "").split(","):
            name = urlsplit(address.strip()).hostname or ""
            if "." in name and name.replace(".", "").isdigit():
                host = name
                break
    # the old file's choices, unless they were another mode's (caddy's 0.0.0.0 must not follow into tailscale)
    kept = old if old.get("COMPOSE_PROFILES", mode) == mode else {}
    return {
        "PISA_VOICE_UDP_PORTS": old.get("PISA_VOICE_UDP_PORTS") or "40000-40019",
        "PISA_VOICE_UDP_BIND": kept.get("PISA_VOICE_UDP_BIND")
        or (ANY_V4 if mode == "caddy" else "127.0.0.1"),
        "PISA_ICE_HOST": kept.get("PISA_ICE_HOST") or host,
    }


def render(values: dict[str, str]) -> str:
    """The file: Pisa's keys in a fixed order with a word on each, then whatever else the old file had."""
    sections: list[tuple[str, tuple[str, ...]]] = [
        (
            "Written by `pisa init`. It holds passwords: never commit or share it.\n# Keep a copy of "
            "PISA_SECRETS_KEY apart from your backups: without it the stored provider keys can't be read.",
            (),
        ),
        ("The reach mode: the compose profile (tailscale, caddy or local).", ("COMPOSE_PROFILES",)),
        ("You.", ("PISA_TIMEZONE", "PISA_LANGUAGE")),
        ("The server.", ("PISA_ENV", "PISA_IMAGE", "PISA_SECRETS_KEY")),
        ("The database's roles (generated).", PASSWORDS),
        ("Tailscale mode.", MODE_KEYS["tailscale"]),
        ("Caddy mode.", MODE_KEYS["caddy"]),
        (
            "Local mode: where the API is published on this machine. 127.0.0.1 is this machine only; set\n# "
            "PISA_LAN_BIND=0.0.0.0 (or this machine's LAN address) to let your phone in over the network.",
            MODE_KEYS["local"],
        ),
        (
            "Local mode: who the API runs as: the owner of deploy/tls/key.pem, which only it reads.",
            ("PISA_LAN_UID", "PISA_LAN_GID"),
        ),
        (
            "Pairing: how clients reach this server (the QR's addresses), and the certificate they pin.",
            ("PISA_ADDRESSES", "PISA_TLS_CERT", "PISA_TLS_PIN_FROM"),
        ),
        ("Voice: the talk's UDP range and where the phone reaches it.", VOICE_KEYS),
    ]
    lines: list[str] = []
    done: set[str] = set()
    for comment, keys in sections:
        present = [k for k in keys if k in values]
        if keys and not present:
            continue
        lines.append(f"# {comment}")
        lines.extend(f"{k}={values[k]}" for k in present)
        lines.append("")
        done.update(present)
    rest = [k for k in values if k not in done]
    if rest:
        lines.append("# Other settings (see .env.example).")
        lines.extend(f"{k}={values[k]}" for k in rest)
        lines.append("")
    return "\n".join(lines)


def write_private(path: Path, text: str | bytes, mode: int = 0o600) -> None:
    """Atomically, readable by its owner only (0600, unless `mode`) from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode() if isinstance(text, str) else text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def next_steps(show_dir: str, mode: str, values: dict[str, str]) -> list[str]:
    compose = f"docker compose -f {show_dir}/docker-compose.yml --env-file {show_dir}/.env"
    lines = ["", "Next:", f"  {compose} up -d"]
    # the worker runs under every profile, so the CLI does too
    lines.append(f"  {compose} exec worker pisa setup    # your AI provider key, notifications")
    lines.append(f"  {compose} exec worker pisa pair     # the QR code your phone scans")
    if mode == "tailscale" and not values.get("TS_AUTHKEY"):
        lines.append(
            f"Before `up`: add TS_AUTHKEY=<your auth key> to {show_dir}/.env ({TAILSCALE_KEYS_PAGE})."
        )
    if mode == "local" and values.get("PISA_LAN_BIND", LAN_BIND) in ("127.0.0.1", "::1", "localhost"):
        lines.append(
            "The API answers this machine only: to let your phone in over your network, set "
            f"PISA_LAN_BIND=0.0.0.0 in {show_dir}/.env before `up`."
        )
    if mode == "tailscale" and not values.get("PISA_ADDRESSES"):
        lines.append(
            "Before `pisa pair`: add PISA_ADDRESSES=https://"
            f"{values.get('TS_HOSTNAME') or 'pisa'}.<your tailnet>"
            f".ts.net to {show_dir}/.env (the tailnet's name is on {TAILSCALE_DNS_PAGE})."
        )
    if mode == "caddy":
        lines.append("Before `up`: point the domain's DNS at this machine and open ports 80 and 443 to it.")
    ports = values.get("PISA_VOICE_UDP_PORTS", "40000-40019")
    if mode == "caddy":
        lines.append(f"For voice: open UDP {ports} to it too.")
    if not values.get("PISA_ICE_HOST"):
        where = (
            f"the node's tailnet IP (`{compose} exec tailscale tailscale ip -4`, after `up`)"
            if mode == "tailscale"
            else "this machine's LAN address (and PISA_LAN_BIND to it or 0.0.0.0)"
        )
        lines.append(f"For voice: set PISA_ICE_HOST in {show_dir}/.env to {where}, then `up -d` again.")
    return lines


def cmd_init(
    args: argparse.Namespace, *, ask: Callable[[str], str] = input,
    secret: Callable[[str], str] = getpass.getpass, environ: dict[str, str] | None = None,
    out: TextIO | None = None, err: TextIO | None = None, tty: bool | None = None,
) -> int:  # fmt: skip
    out, err = out or sys.stdout, err or sys.stderr
    path = Path(args.env_file)
    interactive = not args.yes and (sys.stdin.isatty() if tty is None else tty)
    prompt = Prompt(interactive, ask=ask, secret=secret, out=out)
    try:
        old = read_env(path)
        if is_configured(old) and not (args.force or args.rotate_tls):
            raise InitError(
                f"{path} is already configured; nothing was changed. Run again with --force to update it "
                "(its passwords and keys are kept; --rotate-secrets makes new ones, only for a database not "
                "yet created)"
            )
        values = build(args, old, prompt, os.environ.copy() if environ is None else environ)
        if values["COMPOSE_PROFILES"] == "local":
            shown = f"{args.show_dir or path.parent}/tls"
            local_tls(path.parent, values, rotate=args.rotate_tls, say=prompt.say, shown=shown)
        write_private(path, render(values))
    except InitError as exc:
        print(f"error: {exc}.", file=err)
        return 2
    except EOFError:
        print("error: no answer (end of input); run with --yes and flags for no questions.", file=err)
        return 2
    kept = [k for k in SECRETS if old.get(k) and old[k] == values[k]]
    made = [k for k in SECRETS if k not in kept]
    prompt.say(f"Wrote {path} (readable by you only).")
    if made:
        prompt.say(f"Generated: {', '.join(made)}.")
    if kept:
        prompt.say(f"Kept: {', '.join(kept)}.")
    if "PISA_SECRETS_KEY" in made:
        prompt.say(
            "Back up PISA_SECRETS_KEY somewhere safe: without it the stored provider keys can't be read."
        )
    for line in next_steps(args.show_dir or str(path.parent), values["COMPOSE_PROFILES"], values):
        prompt.say(line)
    return 0


def add_init_parser(sub: Any) -> None:
    init = sub.add_parser("init", help="first run: write deploy/.env (passwords, keys, the reach mode)")
    init.add_argument(
        "--env-file", default=DEFAULT_ENV_FILE, help=f"the file to write (default {DEFAULT_ENV_FILE})"
    )
    init.add_argument("--mode", choices=MODES, help="how the phone reaches Pisa (default tailscale)")
    init.add_argument("--timezone", help="your home time zone, e.g. Europe/London")
    init.add_argument("--language", choices=("en", "it"), help="the language Pisa speaks (default en)")
    init.add_argument("--domain", help="caddy mode: your domain, e.g. pisa.example.com")
    init.add_argument("--email", help="caddy mode: the email Let's Encrypt writes to")
    init.add_argument("--hostname", help="tailscale mode: this server's name on the tailnet (default pisa)")
    init.add_argument("--tailnet", help="tailscale mode: your tailnet's DNS name, e.g. tail1234.ts.net")
    init.add_argument(
        "--lan-host",
        help="local mode: this machine's name or address on your network (default <hostname>.local)",
    )
    init.add_argument(
        "--rotate-tls", action="store_true",
        help="local mode: make a new TLS key and certificate (every paired device must pair again)",
    )  # fmt: skip
    init.add_argument("--image", help="the image to run (PISA_IMAGE; empty: built from core/)")
    init.add_argument("--yes", action="store_true", help="ask nothing: flags, the saved values or defaults")
    init.add_argument("--force", action="store_true", help="update an .env that is already configured")
    init.add_argument(
        "--rotate-secrets", action="store_true", help="make new passwords and keys (fresh database only)"
    )
    init.add_argument("--show-dir", help="the folder to name in the printed commands (default the file's)")
    init.set_defaults(func=cmd_init, no_settings=True)
