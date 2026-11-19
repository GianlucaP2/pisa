"""Pisa's voice: providers, speeds, prices and the fixed sentences. Pure rules, no I/O.

Prices (USD), used when the model catalog has none:
- OpenAI's default speech model (`domain/model_catalog.json`): $0.60 per 1M input (text) tokens and $12 per
  1M output (audio) tokens, about $0.015 a minute of speech. Source: OpenAI's pricing page
  (platform.openai.com/docs/pricing) and genai-prices, read 2026-10-22. The model's catalog row is used first.
- ElevenLabs, per 1,000 characters: Flash and Turbo models $0.05, the others (Multilingual, v3) $0.10. Source:
  elevenlabs.io/pricing/api, read 2026-10-22.
"""

import math
from decimal import Decimal
from typing import Any, Literal

from pisa.domain.ai import CHARS_PER_TOKEN
from pisa.domain.errors import PisaError

Provider = Literal["openai", "elevenlabs", "device"]
# How a speech provider bills: by the text and audio tokens (OpenAI), or by the characters (ElevenLabs).
PricedBy = Literal["tokens", "characters"]
PROVIDERS: tuple[Provider, ...] = ("openai", "elevenlabs", "device")
SPEED_MIN, SPEED_MAX = 0.8, 1.3
PREVIEW_MAX = 300  # a preview's own text, at most (`VoicePreview`)
SLOT = "voice.speak"

OPENAI_USD_PER_MTOK = (Decimal("0.60"), Decimal("12.00"))  # (input text, output audio); see the docstring
ELEVENLABS_USD_PER_KCHAR = {"fast": Decimal("0.05"), "other": Decimal("0.10")}
# For the estimate before a call (the ledger reserves it): speech runs at about 15 characters a second, and
# OpenAI bills about 1,250 audio tokens a minute ($0.015 / $12 per 1M).
CHARS_PER_SECOND = 15
AUDIO_TOKENS_PER_SECOND = Decimal(1250) / 60

PREVIEW_TEXT = {
    "it-IT": "Ciao, sono Pisa. Ti leggo i messaggi e ti dico quando è ora di partire.",
    "en-US": "Hi, I'm Pisa. I read you your messages and tell you when it's time to leave.",
}
# the chosen voice's account has no credits: the phone's voice for the rest of the day, said once
NO_CREDIT_NOTICE = {
    "it-IT": "La voce di {name} non ha più crediti: per oggi ti parlo con la voce dell'iPhone. Puoi "
    "ricaricare l'account o scegliere un'altra voce in Impostazioni → Voce.",
    "en-US": "{name}'s voice has no credits left: for today I'll speak with the iPhone's voice. You can top "
    "up the account or pick another voice in Settings → Voice.",
}
CAP_NOTICE = {
    "it-IT": "La voce di Pisa è in pausa fino a domani: il limite di spesa AI è "
    "raggiunto. Parla il telefono.",
    "en-US": "Pisa's voice is paused until tomorrow: the AI spend cap is reached. The phone speaks instead.",
}


def validate(voice: Any) -> None:
    """Settings D: `voice`. Every message names the field and what is allowed."""
    if voice["provider"] is not None and voice["provider"] not in PROVIDERS:
        raise _invalid(
            "voice.provider must be openai, elevenlabs or device (null: OpenAI when its key is set)."
        )
    if voice["model"] is not None and (
        not isinstance(voice["model"], str) or not 1 <= len(voice["model"]) <= 80
    ):
        raise _invalid("voice.model must be null (the provider's default) or a model id of 1-80 characters.")
    if voice["voice"] is not None and (
        not isinstance(voice["voice"], str) or not 1 <= len(voice["voice"]) <= 80
    ):
        raise _invalid(
            "voice.voice must be null (the provider's default) or the provider's voice id, 1-80 characters."
        )
    speed = voice["speed"]
    if isinstance(speed, bool) or not isinstance(speed, int | float) or not SPEED_MIN <= speed <= SPEED_MAX:
        raise _invalid(f"voice.speed must be a number from {SPEED_MIN} to {SPEED_MAX}.")


def normalize(current: Any, patch: Any, merged: Any) -> None:
    """The phone's own voice has no id: "" is null. A new provider without a voice or model in the same patch
    gets its defaults (null), so another provider's voice id is never sent to it. A stored
    null provider counts as OpenAI here: the voice chosen then was OpenAI's."""
    chosen = merged["voice"]
    if chosen["provider"] == "device" and chosen["voice"] == "":
        chosen["voice"] = None
    asked = patch.get("voice") if isinstance(patch, dict) else None
    if not isinstance(asked, dict) or "provider" not in asked:
        return
    if (asked["provider"] or "openai") != (current["voice"]["provider"] or "openai"):
        for key in ("voice", "model"):
            if key not in asked:
                chosen[key] = None


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Fix the setting and try again")


def elevenlabs_rate(model: str) -> Decimal:
    fast = "flash" in model or "turbo" in model
    return ELEVENLABS_USD_PER_KCHAR["fast" if fast else "other"]


def estimate_usd(
    priced_by: PricedBy,
    model: str,
    characters: int,
    openai_prices: tuple[Decimal, Decimal] = OPENAI_USD_PER_MTOK,
) -> Decimal:
    """Before the call, from the text's length."""
    if priced_by == "characters":
        return elevenlabs_rate(model) * characters / 1000
    seconds = Decimal(characters) / CHARS_PER_SECOND
    output = math.ceil(seconds * AUDIO_TOKENS_PER_SECOND)
    return cost_usd(
        priced_by, model, characters, math.ceil(characters / CHARS_PER_TOKEN), output, openai_prices
    )


def cost_usd(
    priced_by: PricedBy,
    model: str,
    characters: int,
    input_tokens: int | None,
    output_tokens: int | None,
    openai_prices: tuple[Decimal, Decimal] = OPENAI_USD_PER_MTOK,
) -> Decimal:
    """After the call: OpenAI by its token counts (estimated from the text when it gave none), ElevenLabs by
    the characters billed."""
    if priced_by == "characters":
        return elevenlabs_rate(model) * characters / 1000
    if input_tokens is None or output_tokens is None:
        return estimate_usd(priced_by, model, characters, openai_prices)
    return (openai_prices[0] * input_tokens + openai_prices[1] * output_tokens) / 1_000_000
