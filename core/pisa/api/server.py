"""Discovery, `GET /v1/server`: what this server is and offers, before pairing. No auth and nothing personal.

Contract 2's `$defs/ServerDiscovery` (CONTRACT-2 §4.1), built by `discovery`: the install's id (which
signed approvals bind, `server_identity`), the versions it speaks, its features (the `/v2/live` welcome's
list: `stop` and `approvals.signed` always; then each experimental feature turned on, as
`experimental.<id>`), its readiness, how it sees itself reached, its push modes (`relay` only when
PISA_PUSH_RELAY_URL is set and the experimental `push_relay` is on), its pairing methods (QR and the typed
token, when it has a TLS pin to show), and only the revision of its AI disclosure. No addresses, pins,
provider names or anything of the user's.
"""

import ipaddress
from typing import Any

from fastapi import APIRouter, Request

from pisa import __version__
from pisa.api import schemas
from pisa.api.deps import RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.db.errors import UNREACHABLE, database_unreachable
from pisa.db.migrate import latest_version
from pisa.domain import experimental
from pisa.domain import protocol_versions as versions
from pisa.logging import get_logger
from pisa.services import a2ui, disclosure, endpoints
from pisa.services.push import APNS_SECRET
from pisa.services.server_identity import server_id

_log = get_logger("pisa.server")
router = APIRouter(prefix="/v1", tags=["discovery"], responses=ERROR_RESPONSES)

CONTRACT_VERSIONS = list(versions.CONTRACTS)
AGUI_VERSIONS = [versions.AGUI]
A2UI_VERSIONS = [versions.A2UI]
MCP_APPS_VERSIONS = [versions.MCP_APPS]
# What this server implements completely, by the names of protocol/FEATURES.md (contract 2's ids below). The
# safety features `stop` and `approvals` are always listed, never paywalled or deprecated. (`coder` is not in
# this server.)
FEATURE_NAMES: tuple[str, ...] = (
    "stop",
    "approvals",
    "talk",
    "talk_natural",
    "talk_text",
    "app_actions",
    "briefing_view",
    "presentation",
    "drawings",
    "phone_actions",
    "today",
    "tasks",
    "planning",
    "follow",
    "briefing",
    "guardian",
    "autopilot",
    "trust",
    "vault",
    "people",
    "money",
    "trips",
    "places",
    "rituals",
    "connectors",
    "health",
    "profile",
    "voice",
    "voice_id",
    "ai_models",
    "views",
    "world",
    "data_erase",
    "watch",
    "companion",
)
# contract 2's id and version for a feature whose id differs from its name (`approvals.signed`: approvals as
# signed interrupts; `conversation` as in `features_needing_tools`); every other id is its name, version 1
V2_FEATURES: dict[str, tuple[str, int]] = {
    "approvals": ("approvals.signed", 2),
    "talk": ("conversation", 1),
    "talk_natural": ("voice.natural", 1),
    "talk_text": ("talk.text", 1),  # words typed into an open talk (`pisa.voice.talk.text`, contract 2.1)
    # contract 2.2 (`pisa.app`): Pisa opens the app's own screens and the app reports back (`pisa.app.open`,
    # `pisa.app.result`); the riepilogo is a view of the current plan, re-read on `pisa.app.changed`
    "app_actions": ("app.actions", 1),
    "briefing_view": ("briefing.view", 1),
}
# `$defs/FeatureList`: the `/v2/live` welcome's and `discovery`'s
FEATURES: list[dict[str, Any]] = [
    {"id": V2_FEATURES.get(name, (name, 1))[0], "version": V2_FEATURES.get(name, (name, 1))[1]}
    for name in FEATURE_NAMES
]
EXPERIMENTAL_PREFIX = "experimental."
# App Store review mode (PISA_REVIEW_MODE): a demo server, with synthetic data and nothing connected, so the
# app can say so ("Demo server")
REVIEW_MODE: dict[str, Any] = {"id": "review_mode", "version": 1}


def features(enabled: frozenset[str], *, review_mode: bool = False) -> list[dict[str, Any]]:
    """Discovery's features: `FEATURES`, then each experimental feature PISA_EXPERIMENTAL turned on, as
    `experimental.<id>` version 1 (contract 2's Feature id pattern allows it; nothing else changes), and
    `review_mode` on a server in App Store review mode (where no experimental feature is on)."""
    listed = [{"id": EXPERIMENTAL_PREFIX + f, "version": 1} for f in experimental.listed(enabled)]
    return [*FEATURES, *listed, *([REVIEW_MODE] if review_mode else [])]


def _network(host: str) -> str:
    """How the server is reached, from the host the request named: a tailnet name or address, a private or
    local one, or unknown (never claimed public: the app judges that from where it reached the server)."""
    host = host.strip("[]").lower()
    if host.endswith(".ts.net"):
        return "tailnet"
    if host == "localhost" or host.endswith((".local", ".lan", ".home.arpa")):
        return "lan"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "unknown"
    if address in ipaddress.ip_network("100.64.0.0/10") or address in ipaddress.ip_network(
        "fd7a:115c:a1e0::/48"
    ):
        return "tailnet"
    if address.is_private or address.is_loopback or address.is_link_local:
        return "lan"
    return "unknown"


async def discovery(runtime: Any, scheme: str, host: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    sid: str | None = None
    direct = False
    revision: str | None = None
    try:
        async with runtime.pool.connection() as conn:
            sid = await server_id(conn)
            revision = (await disclosure.current(conn, runtime.settings.ollama_base_url))["revision"]
            checks.append({"name": "database", "ok": True})
            applied = await (
                await conn.execute("SELECT coalesce(max(version), 0) FROM schema_migrations")
            ).fetchone()
            checks.append({"name": "migrations", "ok": bool(applied) and applied[0] == latest_version()})
            direct = runtime.push.configured() and (
                await (
                    await conn.execute("SELECT 1 FROM server_secrets WHERE name = %s", (APNS_SECRET,))
                ).fetchone()
                is not None
            )
    except UNREACHABLE as exc:
        _log.error("discovery_database_unreachable", cause=exc.__class__.__name__)
        checks = [{"name": "database", "ok": False}]
    relay = runtime.push.relay_configured()
    checks.append({"name": "push", "ok": direct or relay})
    doc: dict[str, Any] = {
        "product": "pisa",
        "serverId": sid,
        "serverVersion": __version__,
        "contract": {"versions": CONTRACT_VERSIONS},
        "agui": AGUI_VERSIONS,
        "a2ui": {"versions": A2UI_VERSIONS, "catalogIds": list(a2ui.CATALOG_IDS)},
        "mcpApps": MCP_APPS_VERSIONS,
        "features": features(runtime.enabled, review_mode=runtime.settings.review_mode),
        "readiness": {
            "ready": all(c["ok"] for c in checks if c["name"] in ("database", "migrations"))
            and sid is not None,
            "checks": checks,
        },
        "reach": {"scheme": "https" if scheme == "https" else "http", "network": _network(host)},
        "push": {
            "modes": [m for m, on in (("relay", relay), ("direct", direct), ("none", True)) if on],
            "relayConfigured": relay,
        },
    }
    if endpoints.revision(runtime.settings) is not None:  # a pin to show and addresses to put in the QR
        doc["pairing"] = {"methods": ["qr_v1", "typed_v1"]}
    if revision is not None:
        doc["ai"] = {"disclosureRevision": revision}
    return doc


@router.get(
    "/server",
    response_model=schemas.ServerDiscovery,
    response_model_exclude_none=True,
    openapi_extra={"security": []},
)
async def server(request: Request, runtime: RuntimeDep) -> dict[str, Any]:
    """What this server is and offers (contract 2's `ServerDiscovery`). No authentication, nothing personal.
    Called on connect, after pairing and when the app comes back to the foreground. 500 internal when the
    database can't say which install this is."""
    doc = await discovery(runtime, request.url.scheme, request.url.hostname or "")
    if doc["serverId"] is None:
        raise database_unreachable()
    return doc
