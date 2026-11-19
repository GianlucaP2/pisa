"""The AI disclosure from the feature slots as they are set, the user's recorded consent, and the check every
contract 2 run passes before any AI call (CONTRACT-2 §4.4; `domain.disclosure`)."""

import ipaddress
import uuid
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from psycopg import AsyncConnection

from pisa.brain.models.compat import get_server
from pisa.brain.models.providers import PROVIDERS_BY_ID
from pisa.brain.models.slots import all_slots
from pisa.domain import disclosure as rules
from pisa.domain.ai import ModelRef
from pisa.domain.errors import PisaError
from pisa.logging import get_logger

_log = get_logger("pisa.disclosure")
# Postgres NOTIFY channel: a slot or a model server changed, in any process (the API, `pisa models set`). The
# API process tells every connected device whose disclosure is older (`tell`). No payload.
CHANGED_CHANNEL = "pisa_ai_disclosure"


class Listeners(Protocol):
    def tell(
        self, name: str, value: dict[str, Any], only: set[uuid.UUID] | None = None
    ) -> list[uuid.UUID]: ...


async def changed(conn: AsyncConnection) -> None:
    """The slots or the model servers changed: the API process tells the devices (CHANGED_CHANNEL)."""
    await conn.execute("SELECT pg_notify(%s, '')", (CHANGED_CHANNEL,))


async def tell(conn: AsyncConnection, ollama_base_url: str | None, live: Listeners) -> None:
    """Every connected device told an older revision gets `pisa.ai.disclosure`, so the app asks for consent
    again (CONTRACT-2 §4.4). The others learn it when they connect; a run is refused until consent is given
    for the current revision (`check`)."""
    current_ = await current(conn, ollama_base_url)
    rows = await (
        await conn.execute(
            "SELECT id FROM devices WHERE revoked_at IS NULL AND ai_told IS NOT NULL AND ai_told <> %s",
            (current_["revision"],),
        )
    ).fetchall()
    if not rows:
        return
    for device_id in live.tell("pisa.ai.disclosure", current_, {r[0] for r in rows}):
        await told(conn, device_id, current_["revision"])


def _local(base_url: str) -> bool:
    """A model server on Pisa's own machine (loopback, or the compose network's own name)."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host in ("localhost", "ollama", "host.docker.internal"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def current(conn: AsyncConnection, ollama_base_url: str | None) -> dict[str, Any]:
    """`$defs/AIDisclosure` for the slots as they are now (a fallback model is a processor of its own)."""
    processors: list[dict[str, Any]] = []
    for slot in await all_slots(conn):
        for ref_text in (slot.model, slot.fallback):
            if not ref_text:
                continue
            try:
                ref = ModelRef.parse(ref_text)
            except PisaError:
                continue
            builtin = PROVIDERS_BY_ID.get(ref.provider)
            if builtin is not None:
                name, kind = builtin.name, "cloud"
            else:
                server = await get_server(conn, ref.provider, ollama_base_url)
                if server is None:
                    continue
                name, kind = server.name, "on_server" if _local(server.base_url) else "self_hosted"
            entry = rules.processor(slot.spec.id, ref.provider, name, kind, ref.model_id)
            if entry not in processors:
                processors.append(entry)
    return rules.disclosure(processors)


async def told(conn: AsyncConnection, device_id: uuid.UUID, revision: str) -> None:
    await conn.execute("UPDATE devices SET ai_told = %s WHERE id = %s", (revision, device_id))


async def record(
    conn: AsyncConnection, device_id: uuid.UUID, revision: str, health: bool, ollama_base_url: str | None,
    now: datetime,
) -> None:  # fmt: skip
    """`POST /v1/ai/consent`: only the current revision can be allowed (the user saw what is set now)."""
    disclosed = await current(conn, ollama_base_url)
    if revision != disclosed["revision"]:
        raise PisaError(
            "ai_consent_required",
            "The AI disclosure changed since the app read it: this consent is for a revision that is no "
            "longer current.",
            "Read GET /v1/ai/disclosure again and ask the user",
        )
    await conn.execute(
        "UPDATE devices SET ai_consent_revision = %s, ai_consent_health = %s, ai_consent_at = %s, ai_told = "
        "%s WHERE id = %s",
        (revision, health, now, revision, device_id),
    )
    _log.info("ai_consent_recorded", device_id=str(device_id), health=health)


async def recorded(conn: AsyncConnection, device_id: uuid.UUID) -> dict[str, Any] | None:
    row = await (
        await conn.execute(
            "SELECT ai_consent_revision, ai_consent_health FROM devices WHERE id = %s", (device_id,)
        )
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return {"aiRevision": row[0], "health": bool(row[1])}


async def check(
    conn: AsyncConnection, device_id: uuid.UUID, consent: dict[str, Any] | None, words: str | None,
    ollama_base_url: str | None,
) -> str | None:  # fmt: skip
    """Why this run may not use AI (`ai_consent_required`'s message), or None. The run's own consent
    (`forwardedProps.pisa.consent`) is used when it carries one, else the device's recorded consent."""
    if consent is None:
        consent = await recorded(conn, device_id)
    disclosed = await current(conn, ollama_base_url)
    why = rules.refused(consent, disclosed["revision"], rules.touches_health(words))
    if why is not None:
        _log.info("ai_consent_refused", device_id=str(device_id), has_consent=consent is not None)
    return why
