"""`pisa doctor --live`: with natural mode set, open a real session with the configured talker and model (a
few seconds, no audio: it spends a little, so it is opt-in), close it, and say OK or the exact refusal: its
kind and code, never the key. Why a natural talk closes at once, without the phone."""

import asyncio
from collections.abc import Mapping
from typing import Any

from pisa.domain import guide, model_catalog
from pisa.ports.talker import Talker, TalkerConfig, TalkerError
from pisa.settings import Settings

OPEN_WITHIN_S = 15.0


def default_talkers() -> dict[str, Talker]:
    from pisa.connectors.talker_gemini import GeminiLive
    from pisa.connectors.talker_xai import GrokVoice

    return {"gemini": GeminiLive(), "xai": GrokVoice()}


async def talker_check(
    conn: Any, settings: Settings, talkers: Mapping[str, Talker] | None = None
) -> tuple[str, dict[str, object]]:
    """(the doctor's text key, its values): `talker_off` (economy), `talker_no_key`, `talker_ok` or
    `talker_failed` (the refusal's kind, code and the provider's words, the key cut out)."""
    from pisa.infra.keys import KeyStore
    from pisa.services import settings as settings_service

    stored = await settings_service.load(conn, "en-US")
    conversation = stored["conversation"]
    if conversation.get("mode") != "natural":
        return "talker_off", {}
    talker_id = str(conversation.get("talker"))
    talker = (talkers or default_talkers())[talker_id]
    model = model_catalog.default_model("talker", talker_id)
    values: dict[str, object] = {"talker": talker.name, "model": model}
    key = await KeyStore(settings.secrets_key).get(conn, talker.key_provider)
    if not key:
        return "talker_no_key", values | {"provider": talker.key_provider}
    voice = (conversation.get("voices") or {}).get(talker_id) or talker.default_voice
    cfg = TalkerConfig(key=key, voice=voice, lang="en-US", instructions="Pisa's doctor: a connection check.")
    try:
        session = await asyncio.wait_for(talker.open(cfg), OPEN_WITHIN_S)
    except TalkerError as err:
        return "talker_failed", values | {"kind": err.message, "code": err.status or "-",
                                          "reason": _safe(err.reason or "-", key)}  # fmt: skip
    except Exception as exc:  # a timeout, or a connector's unexpected error: its class and its message
        return "talker_failed", values | {"kind": exc.__class__.__name__, "code": "-",
                                          "reason": _safe(str(exc) or "-", key)}  # fmt: skip
    await session.close()
    return "talker_ok", values


def _safe(text: str, key: str) -> str:
    """The provider's exact words, with the key (and anything key-like) masked (`failures.redact`)."""
    from pisa.failures import redact

    return redact(text, key)[:400]


async def live_lines(
    settings: Settings, lang: str, talkers: Mapping[str, Talker] | None = None
) -> tuple[bool, str]:
    """(fine, the line to print) for `pisa doctor --live`."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
        key, values = await talker_check(conn, settings, talkers)
    return key in ("talker_ok", "talker_off"), guide.doctor_text(key, lang, **values)
