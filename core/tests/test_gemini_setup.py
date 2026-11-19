"""Every Gemini Live session was refused (1007 "Unknown name proactivity at 'setup'") and fell back to
economy. Pisa's setup carries only fields Google's API accepts (maintenance → gemini-live-check opened it)."""

from typing import Any

import pytest

from pisa.connectors import talker_gemini
from pisa.ports.talker import TalkerConfig


def test_the_setup_has_no_field_google_refuses() -> None:
    cfg = TalkerConfig(key="k", voice="Aoede", lang="it-IT", instructions="Sei Pisa.")
    setup = talker_gemini.connect_config(cfg)
    assert setup.proactivity is None
    # native audio models choose the language themselves and refuse a language code (Google's Live guide)
    assert setup.speech_config is not None and setup.speech_config.language_code is None
    assert setup.tools and setup.input_audio_transcription is not None


@pytest.mark.parametrize(
    ("route", "client_activity", "resume", "affect"),
    [(None, False, None, False), ("speaker", False, "h-1", False), (None, True, None, False),
     ("airpods", False, None, True)],
)  # fmt: skip
def test_the_exact_setup_goes_through_the_pinned_sdk_s_own_types_and_converter(
    route: str | None, client_activity: bool, resume: str | None, affect: bool
) -> None:
    """The setup message as `client.aio.live.connect` builds it for the Gemini API (an API key, v1beta), with
    the pinned SDK's pydantic types (unknown fields refused) and its own converter, offline: a field the SDK
    rejects fails here, not on the owner's phone. What Google's server refuses beyond the SDK (fields it
    refuses for a model) is pinned by the assertions on the message."""
    import asyncio
    import json

    from google import genai
    from google.genai import _common, types
    from google.genai import _live_converters as converters
    from google.genai import _transformers as transformers
    from google.genai.live import _t_live_connect_config

    from pisa.domain import model_catalog

    cfg = TalkerConfig(key="k", voice="Aoede", lang="it-IT", instructions="Sei Pisa.", route=route,
                       client_activity=client_activity, resume=resume, affect=affect)  # fmt: skip
    config = talker_gemini.connect_config(cfg)
    types.LiveConnectConfig.model_validate(config.model_dump(exclude_none=True))  # extra fields are refused
    api = genai.Client(api_key="not-a-key")._api_client
    assert api._http_options.api_version == "v1beta"  # Google's Live examples for gemini-3.8-live use v1beta

    async def setup() -> dict[str, Any]:
        model = transformers.t_model(api, model_catalog.default_model("talker", "gemini"))
        parameters = await _t_live_connect_config(api, config)
        built = converters._LiveConnectParameters_to_mldev(
            api_client=api,
            from_object=types.LiveConnectParameters(model=model, config=parameters).model_dump(
                exclude_none=True
            ),
        )
        return _common.convert_to_dict(built)["setup"]

    message = asyncio.run(setup())
    text = json.dumps(message)
    assert message["generationConfig"]["responseModalities"] == ["AUDIO"]  # native audio: AUDIO only
    assert message["generationConfig"]["speechConfig"] == {"voice_config": {"prebuilt_voice_config": {
        "voice_name": "Aoede"}}}  # fmt: skip
    # refused by gemini-3.8-live (Google's model comparison): no language code, no thinking level, no
    # proactivity off, no affective dialog
    # (affective dialog only when `cfg.affect`, which nothing sets: no setting turns it on)
    affective = () if affect else ("affective", "Affective")
    for refused in ('language_code"', 'languageCode"', "thinking", "proactiv", *affective):
        assert refused not in text, refused
    assert message["inputAudioTranscription"] == {
        "language_codes": ["it-IT"]
    }  # `languageCodes[]` is in v1beta
    assert message["systemInstruction"] == {"parts": [{"text": "Sei Pisa."}], "role": "user"}
    assert message["tools"][0]["functionDeclarations"][0]["behavior"] == "NON_BLOCKING"
    assert ("handle" in message["sessionResumption"]) == (resume is not None)


def test_a_refused_session_keeps_the_provider_s_reason_for_the_log() -> None:
    from pisa.ports.talker import TalkerError

    err = TalkerError(
        "Gemini Live refused the session (HTTP 1007).", 1007, "Unknown name \"x\" at 'setup'" * 30
    )
    assert err.reason is not None and err.reason.startswith("Unknown name") and len(err.reason) == 400
    assert TalkerError("unreachable").reason is None
