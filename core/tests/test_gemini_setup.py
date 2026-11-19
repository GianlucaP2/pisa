"""Every Gemini Live session was refused (1007 "Unknown name proactivity at 'setup'") and fell back to
economy. Pisa's setup carries only fields Google's API accepts (maintenance → gemini-live-check opened it)."""

from pisa.connectors import talker_gemini
from pisa.ports.talker import TalkerConfig


def test_the_setup_has_no_field_google_refuses() -> None:
    cfg = TalkerConfig(key="k", voice="Aoede", lang="it-IT", instructions="Sei Pisa.")
    setup = talker_gemini.connect_config(cfg)
    assert setup.proactivity is None
    assert setup.speech_config is not None and setup.speech_config.language_code == "it-IT"
    assert setup.tools and setup.input_audio_transcription is not None


def test_a_refused_session_keeps_the_provider_s_reason_for_the_log() -> None:
    from pisa.ports.talker import TalkerError

    err = TalkerError(
        "Gemini Live refused the session (HTTP 1007).", 1007, "Unknown name \"x\" at 'setup'" * 30
    )
    assert err.reason is not None and err.reason.startswith("Unknown name") and len(err.reason) == 400
    assert TalkerError("unreachable").reason is None
