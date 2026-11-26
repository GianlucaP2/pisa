"""The AI disclosure from the feature slots as they are set, the user's recorded consent, and the check every
contract 2 run passes before any AI call (CONTRACT-2 §4.4; `domain.disclosure`)."""

import ipaddress
import uuid
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.brain.models.compat import get_server
from pisa.brain.models.providers import PROVIDERS_BY_ID
from pisa.brain.models.slots import all_slots
from pisa.connectors.speech import ElevenLabsSpeech, OpenAISpeech
from pisa.db.settings import get_setting
from pisa.domain import disclosure as rules
from pisa.domain import experimental, model_catalog
from pisa.domain import settings as settings_rules
from pisa.domain.ai import ModelRef
from pisa.domain.errors import PisaError
from pisa.logging import get_logger
from pisa.settings import Settings, get_settings

_log = get_logger("pisa.disclosure")
# each server voice's own voice when none is picked (None: the user must pick one, else the phone speaks)
SPEECH_DEFAULT_VOICE: dict[str, str | None] = {
    OpenAISpeech.id: OpenAISpeech.default_voice, ElevenLabsSpeech.id: ElevenLabsSpeech.default_voice,
}  # fmt: skip
# Postgres NOTIFY channel: a slot or a model server changed, in any process (the API, `pisa models set`). The
# API process tells every connected device whose disclosure is older (`tell`). No payload.
CHANGED_CHANNEL = "pisa_ai_disclosure"
SETTINGS_KEY = "settings"  # app_settings' key of the user's settings (`services.settings.KEY`)
# the server's voices are on (the `tts_server` feature; off in App Store review mode): set at start by a
# runtime (the API's, the worker's) and `pisa consent` (`set_speech`), as the voice service is built with them
# or without. None (nothing set it in this process): `current` reads it from this process's settings, so no
# process holds a default of its own.
_speech: dict[str, bool | None] = {"on": None}


def set_speech(on: bool | None) -> None:
    """Whether the server's voices can speak (`experimental.on("tts_server", ...)`): without them the phone
    speaks, and no voice is a processor. None: read from the process's settings when a disclosure is made."""
    _speech["on"] = on


def speech_on(settings: Settings | None = None) -> bool:
    """The server's voices are on: as a runtime set them (`set_speech`), else as the settings say (the
    process's own when none is given): the `tts_server` feature, off in review mode."""
    on = _speech["on"]
    if on is not None:
        return on
    settings = settings or get_settings()
    return experimental.on(
        "tts_server", experimental.enabled(settings.experimental, review_mode=settings.review_mode)
    )


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
    # a device whose consent covers what is set now (the same or less sent) isn't asked again
    ask = set()
    for (device_id,) in rows:
        if not rules.covers(await allowed(conn, await recorded(conn, device_id)), current_["processors"]):
            ask.add(device_id)
    for device_id in live.tell("pisa.ai.disclosure", current_, ask):
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
    if (talker := await _talker(conn)) is not None:
        processors.append(talker)
    if (voice := await _voice(conn)) is not None:
        processors.append(voice)
    return rules.disclosure(processors)


async def _talker(conn: AsyncConnection) -> dict[str, Any] | None:
    """Natural mode's talker as a processor (it hears the user's voice), or None in economy mode."""
    conversation = settings_rules.with_defaults(await get_setting(conn, SETTINGS_KEY), "en")["conversation"]
    if conversation.get("mode") != "natural":
        return None
    talker = str(conversation.get("talker"))
    provider = rules.TALKER_PROVIDERS.get(talker)
    if provider is None:
        return None
    spec = PROVIDERS_BY_ID.get(provider)
    model = model_catalog.default_model("talker", talker)  # the model the talker's connector opens
    return rules.processor(rules.TALKER_SLOT, provider, spec.name if spec else provider, "cloud", model)


async def _voice(conn: AsyncConnection) -> dict[str, Any] | None:
    """The server's voice as a processor (every sentence Pisa says goes to it), or None when the phone speaks:
    the Device voice, the feature off, no key for the chosen provider, or no voice to speak with (as
    `services.voice.VoiceService.config` decides; a provider resting for the day for lack of credits stays
    one). A null provider is OpenAI when its key is set (`services.settings`)."""
    if not speech_on():
        return None
    chosen = settings_rules.with_defaults(await get_setting(conn, SETTINGS_KEY), "en")["voice"]
    provider = chosen.get("provider") or "openai"
    if provider not in rules.VOICE_PROVIDERS:
        return None
    if not chosen.get("voice") and SPEECH_DEFAULT_VOICE.get(provider) is None:
        return None  # ElevenLabs with no voice picked: the phone speaks
    keyed = await conn.execute("SELECT 1 FROM provider_keys WHERE provider = %s", (provider,))
    if await keyed.fetchone() is None:
        return None
    spec = PROVIDERS_BY_ID.get(provider)
    model = str(chosen.get("model") or model_catalog.default_model("speech", provider))
    return rules.processor(rules.VOICE_SLOT, provider, spec.name if spec else provider, "cloud", model)


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
    # what this revision sends, so a later disclosure that sends the same or less stays allowed (`covers`)
    await conn.execute(
        "INSERT INTO ai_disclosures (revision, processors, recorded_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (revision) DO NOTHING",
        (revision, Jsonb(disclosed["processors"]), now),
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


async def allowed(conn: AsyncConnection, consent: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The processors behind the consent's revision, as recorded when it was given (None: not recorded)."""
    revision = (consent or {}).get("aiRevision")
    if not isinstance(revision, str):
        return None
    row = await (
        await conn.execute("SELECT processors FROM ai_disclosures WHERE revision = %s", (revision,))
    ).fetchone()
    return list(row[0]) if row is not None else None


async def check(
    conn: AsyncConnection, device_id: uuid.UUID, consent: dict[str, Any] | None, words: str | None,
    ollama_base_url: str | None, lang: str = "en", *, health: bool = False,
) -> str | None:  # fmt: skip
    """Why this run may not use AI (`ai_consent_required`'s message, in `lang`), or None. The run's own
    consent (`forwardedProps.pisa.consent`) is used when it carries one, else the device's recorded
    consent. `health`: the AI will hear whatever they say next, not only these words (a natural talk's
    cloud talker), so their separate health consent is needed whatever the words are."""
    if consent is None:
        consent = await recorded(conn, device_id)
    disclosed = await current(conn, ollama_base_url)
    consented = await allowed(conn, consent)
    why = rules.refused(
        consent, disclosed["revision"], health or rules.touches_health(words), lang,
        consented=consented, current=disclosed["processors"],
    )  # fmt: skip
    if why is not None:
        _log.info("ai_consent_refused", device_id=str(device_id), has_consent=consent is not None)
    return why


async def require(
    conn: AsyncConnection, device_id: uuid.UUID, words: str | None, ollama_base_url: str | None,
    lang: str = "en", *, health: bool = False,
) -> None:  # fmt: skip
    """`check` with the device's recorded consent, as a refusal: 409 `ai_consent_required` (its message and
    action in `lang`) before any AI call. The REST routes that reach a model or an embedding call it first,
    as a run, the live session and a talk do."""
    why = await check(conn, device_id, None, words, ollama_base_url, lang, health=health)
    if why is not None:
        raise PisaError(rules.CONSENT_REQUIRED, why, rules.refused_action(lang))
