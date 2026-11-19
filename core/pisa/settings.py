"""Configuration from environment variables, validated at startup (documented in .env.example).

A missing or invalid setting fails loudly at boot instead of breaking later.
"""

import ipaddress
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PISA_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # The user's home time zone, an IANA name ("America/New_York"). Required: Pisa has no built-in zone.
    # The phone's own zone overrides it while the user travels (pisa/domain/zone.py).
    timezone: str
    # The language Pisa speaks and writes by default: en or it. A turn in the other language is answered in
    # that language; the user's choice in Settings wins once made.
    language: Literal["en", "it"] = "en"
    # The user's country, ISO 3166-1 alpha-2 ("IT", "US"). Unset: no country-specific feature is on (public
    # transport feeds, taxi numbers, certified email, ...) and phone numbers need their + prefix.
    country: str | None = None
    # The reverse proxies whose X-Forwarded-For Pisa believes, a comma-separated list of CIDRs
    # ("127.0.0.1/32,10.0.0.0/8"). Empty: the client address is always the TCP peer's.
    trusted_proxies: str = ""

    # pisa_app role: read/write data, no DDL.
    database_url: str = Field(default="postgresql://pisa_app@localhost:5432/pisa")
    # pisa_migrate role: DDL only, used by `pisa migrate`.
    migrate_database_url: str | None = None
    # DBOS system database (`pisa_dbos`, owned by pisa_app), used by `pisa worker` only.
    dbos_database_url: str | None = None

    # Fernet key that encrypts provider API keys and OAuth tokens at rest.
    secrets_key: SecretStr | None = None

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # The API serves TLS itself with this certificate and key (PEM files' paths), as docker compose's `local`
    # mode does with the self-signed pair `pisa init` makes. Both empty: plain http (a proxy in front).
    api_tls_cert: str = ""
    api_tls_key: str = ""

    # APNs: the Apple team and the app's bundle id (the push topic). Not secret. The .p8 key itself is
    # stored encrypted on the server with `pisa secrets set-apns`. Unset: pushes (and the Apple Maps key,
    # which signs with the same team id) fail with a message naming PISA_APNS_TEAM_ID / PISA_APNS_TOPIC.
    apns_team_id: str | None = None
    apns_topic: str | None = None

    # The push relay (contract 2, "relay" push mode): its https origin, e.g. https://relay.example.net. Empty:
    # the relay is off, and a device registered for it gets no pushes (only its live connection).
    push_relay_url: str = ""
    # How the app reaches this server, for the pairing QR and pisa.endpoints: a comma-separated list of
    # https:// URLs (LAN, IPv6, IPv4, a host name) and relay://<host>/<handle> blind relays, at most 6.
    addresses: str = ""
    # The TLS certificate the app pins (a PEM file's path, or the PEM itself: the certificate, or its public
    # key), and the next key, made ahead of a rotation and announced in pisa.endpoints before it is used.
    # Unset: no QR pairing.
    tls_cert: str | None = None
    tls_next_key: str | None = None
    # Without tls_cert: where to read the certificate to pin, host:port of the TLS proxy (docker compose's
    # caddy mode: caddy:443). Pisa pins the leaf it presents for the first https address's host, only after
    # WebPKI verification (`pisa.services.endpoints`).
    tls_pin_from: str = ""
    # A developer's machine: pair over plain http on loopback, with no TLS certificate (a fixed pin that is no
    # key's). Refused unless every PISA_ADDRESSES entry is http on loopback, PISA_TLS_CERT is unset, and the
    # API listens on loopback (or in a container, where `pisa.api.loopback` lets in only this machine).
    insecure_loopback_pairing: bool = False
    # With it, in a container: also answer the container's default gateway, where the host's own loopback
    # connections arrive under rootful Docker on Linux with the port published on 127.0.0.1 only. Off: only
    # loopback inside the container (`docker compose exec`).
    insecure_loopback_gateway: bool = False

    # Ollama server root, e.g. http://127.0.0.1:11434. Unset = the ollama provider is disabled.
    ollama_base_url: str | None = None
    # the WebRTC ICE servers, a JSON list as browsers take it ([{"urls": [...], "username",
    # "credential"}]); empty: host candidates only (the phone must reach the server directly, e.g. over a
    # VPN).
    talk_ice_servers: str = "[]"
    # The talk's media on a fixed UDP port range ("40000-40019", or one port), so a firewall or a compose file
    # can publish exactly it; empty: any free port (aiortc's own choice). Each talk takes one port.
    voice_udp_ports: str = ""
    # The address the phone reaches those ports at, advertised as the talk's ICE host candidate (an IP, or a
    # name resolved at each talk): the tailnet address (tailscale), the public address or domain (caddy), the
    # LAN address (local). Set, the media binds every interface and is offered only there; empty: each
    # interface's own address.
    ice_host: str = ""
    # A talk whose media hasn't connected this long after Pisa's answer ends `fallback` (voice unavailable:
    # the phone carries on over /v2/live), so no talker minute is spent on a phone that can't be heard.
    voice_connect_timeout_s: float = Field(default=15.0, gt=0, le=120)
    # Pisa's own public https origin (https://host, nothing after it), where its agent card,
    # its key directory and its UCP profile are served to the web (nothing of the user's). Unset: Pisa signs
    # nothing and reads pages through its web provider.
    agent_url: str | None = None
    # the user-voice gate. `voice_gate` is the one switch back to today's path (off: no
    # talk is gated, whatever is enrolled); the models live in `voice_models_dir` (fetched at image build);
    # `voice_embedder` picks the speaker model (resnet34, the bench's pick; the only one shipped)
    voice_gate: bool = True
    voice_models_dir: str = "/app/models/voice"
    voice_embedder: Literal["resnet34"] = "resnet34"
    # Unpaywall asks every caller for a contact address (its terms). Unset: Pisa skips Unpaywall and finds
    # a DOI's open copy through OpenAlex alone.
    learn_contact_email: str | None = None
    # A JSON file laid over the shipped model catalog (per-model effort, the voice connectors' default
    # models; `pisa/domain/model_catalog.py`). Unset: the shipped catalog.
    model_catalog: str | None = None
    # The hard spending limit over every paid service together (AI, voice, maps, web), in euros, per local day
    # and per calendar month (`pisa/infra/hard_limit.py`). The caps the app sets can only be lower.
    hard_daily_eur: Decimal = Decimal("3")
    hard_monthly_eur: Decimal = Decimal("30")

    @field_validator("timezone", mode="after")
    @classmethod
    def _zone(cls, value: str) -> str:
        value = value.strip()
        if not value:  # an empty PISA_TIMEZONE= (as .env.example leaves it) is no zone
            raise ValueError("PISA_TIMEZONE is required: the user's home time zone, e.g. Europe/London")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(
                f"PISA_TIMEZONE {value!r} is not a time zone Pisa knows: use an IANA name such as "
                "Europe/London or America/New_York"
            ) from None
        return value

    @field_validator("country", mode="after")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        if not value or not value.strip():
            return None
        code = value.strip().upper()
        if len(code) != 2 or not code.isascii() or not code.isalpha():
            raise ValueError(
                f"PISA_COUNTRY {value!r} is not an ISO 3166-1 alpha-2 code (two letters, e.g. US)"
            )
        return code

    @field_validator("trusted_proxies", mode="after")
    @classmethod
    def _proxies(cls, value: str) -> str:
        for part in _parts(value):
            try:
                ipaddress.ip_network(part, strict=False)
            except ValueError:
                raise ValueError(
                    f"PISA_TRUSTED_PROXIES: {part!r} is not an IP address or CIDR (e.g. 10.0.0.0/8)"
                ) from None
        return value

    @field_validator("voice_udp_ports", mode="after")
    @classmethod
    def _udp_ports(cls, value: str) -> str:
        udp_ports(value)  # refused at boot, not at the first talk
        return value.strip()

    @property
    def voice_ports(self) -> range | None:
        return udp_ports(self.voice_udp_ports)

    @field_validator("push_relay_url", mode="after")
    @classmethod
    def _relay(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("https://", "http://")):
            raise ValueError(
                f"PISA_PUSH_RELAY_URL {value!r} is not an http(s) URL (e.g. https://relay.example.net)"
            )
        return value

    @property
    def address_list(self) -> list[str]:
        """PISA_ADDRESSES as a list."""
        return _parts(self.addresses)

    @property
    def proxy_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        """PISA_TRUSTED_PROXIES as networks (empty: no proxy is trusted)."""
        return tuple(ipaddress.ip_network(p, strict=False) for p in _parts(self.trusted_proxies))

    @field_validator("secrets_key", mode="after")
    @classmethod
    def _fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None  # an empty PISA_SECRETS_KEY= means "not set"
        from cryptography.fernet import Fernet

        try:
            Fernet(value.get_secret_value().encode())
        except ValueError:
            raise ValueError(
                "PISA_SECRETS_KEY is not a valid Fernet key (44 url-safe base64 characters)"
            ) from None
        return value

    @field_validator(
        "ollama_base_url",
        "dbos_database_url",
        "agent_url",
        "apns_team_id",
        "apns_topic",
        "tls_cert",
        "tls_next_key",
        mode="after",
    )
    @classmethod
    def _empty_is_unset(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def _prod_requirements(self) -> "Settings":
        if self.env == "prod" and self.secrets_key is None:
            raise ValueError("PISA_SECRETS_KEY is required when PISA_ENV=prod (see .env.example)")
        return self

    @model_validator(mode="after")
    def _insecure_loopback(self) -> "Settings":
        if self.insecure_loopback_pairing and (problem := loopback_problem(self)):
            raise ValueError(f"PISA_INSECURE_LOOPBACK_PAIRING is refused: {problem}")
        return self


def udp_ports(spec: str) -> range | None:
    """`PISA_VOICE_UDP_PORTS`: "40000-40019" (both ends included) or one port, unprivileged; "" is None (any
    port). ValueError for anything else."""
    spec = spec.strip()
    if not spec:
        return None
    low, sep, high = spec.partition("-")
    try:
        first, last = int(low), int(high if sep else low)
    except ValueError:
        raise ValueError(
            f"PISA_VOICE_UDP_PORTS must be a port or a range like 40000-40019, not {spec!r}"
        ) from None
    if not 1024 <= first <= last <= 65535:
        raise ValueError(f"PISA_VOICE_UDP_PORTS must be ascending ports in 1024-65535, not {spec!r}")
    return range(first, last + 1)


def in_container() -> bool:
    """Docker (`/.dockerenv`) or Podman (`/run/.containerenv`)."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def loopback_problem(settings: Settings) -> str | None:
    """Why insecure loopback pairing can't be on with these settings, or None."""
    from pisa.domain.pairing import is_loopback_url

    if settings.tls_cert or settings.tls_next_key or settings.tls_pin_from:
        return "PISA_TLS_CERT or PISA_TLS_PIN_FROM is set; with a certificate, pair over TLS (unset them)"
    addresses = settings.address_list
    if not addresses:
        return "set PISA_ADDRESSES to the loopback address clients use, e.g. http://127.0.0.1:8000"
    if wrong := [a for a in addresses if not is_loopback_url(a)]:
        return f"every PISA_ADDRESSES entry must be http:// on loopback (127.0.0.1, ::1), not {wrong[0]!r}"
    if not is_loopback_host(settings.api_host.strip("[]")) and not in_container():
        return (
            f"the API listens on {settings.api_host}, not on loopback: set PISA_API_HOST=127.0.0.1 "
            "(in a container, only this machine is let in)"
        )
    return None


def _parts(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # PISA_TIMEZONE comes from the environment
