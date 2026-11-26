"""The reflex's output limit (conversation.reflex): a reasoning model spent its 120-token limit thinking
(the gateway's "it used its whole output limit", pydantic-ai's "Model token limit (120) exceeded before any
response was generated") and the slot rested 5 minutes each time, so turns fell back to the rules. Now its
settings leave room for reasoning and ask for the least of it, its output is cut rather than refused, and a
rest is warned about once an hour per slot and counted in readyz (Pisa has no operator alert channel).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from pisa.brain import reflex as brain_reflex
from pisa.brain.call import FAST_REASONING_TOKENS, fast_settings
from pisa.brain.models import rests
from pisa.domain.errors import PisaError
from pisa.domain.reflex import RulesRead


class _Profiled:
    def __init__(self, **profile: Any) -> None:
        self.profile = profile


@pytest.mark.parametrize(
    ("profile", "thinking"),
    [
        ({"thinking_always_enabled": True, "thinking_enabled_by_default": True}, "minimal"),  # gpt-5-nano
        ({"supports_thinking": True}, False),  # Gemini 2.5 Flash: thinks by default, off when asked
        ({"supports_thinking": False}, None),  # gpt-4.1-nano
    ],
)
def test_the_reflex_leaves_room_for_reasoning_and_asks_for_the_least(
    profile: dict[str, Any], thinking: Any
) -> None:
    settings = fast_settings(_Profiled(**profile), brain_reflex.READ_TOKENS)  # type: ignore[arg-type]
    assert settings.get("max_tokens") == brain_reflex.READ_TOKENS + FAST_REASONING_TOKENS
    assert settings.get("thinking") == thinking


def test_the_reflex_s_longest_valid_output_fits_its_limit() -> None:
    """At 2 characters a token (a cautious rate for JSON with Italian words), its longest valid output fits
    the answer's own tokens: 120 and 90 didn't."""
    longest = brain_reflex.ReflexRead(
        intent="question",
        goal="è" * 120,
        reads=["agenda", "tasks", "travel"],
        day="2026-11-22",
        place="à" * 80,
    )
    assert len(longest.model_dump_json()) / 2 <= brain_reflex.READ_TOKENS
    answer = brain_reflex.ReflexAnswer(answer="ò" * 240)
    assert len(answer.model_dump_json()) / 2 <= brain_reflex.ANSWER_TOKENS


def test_a_reflex_output_a_little_off_is_cut_not_refused() -> None:
    """With no retry, a goal one word too long failed the read and rested the slot for 5 minutes."""
    read = brain_reflex.ReflexRead.model_validate(
        {
            "intent": "ask",
            "goal": "x" * 150,
            "reads": ["agenda", "weather", "tasks", "plan", "waiting"],
            "place": "y" * 100,
        }
    )
    assert (read.intent, len(read.goal), read.reads, len(read.place or "")) == (
        "unclear", 120, ["agenda", "tasks", "plan"], 80,
    )  # fmt: skip
    assert len(brain_reflex.ReflexAnswer.model_validate({"answer": "z" * 300}).answer or "") == 240


class _FailingGateway:
    """The slot's model, failing as the owner's did: the gateway's "used its whole output limit"."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, slot: str, call: Any, **_: Any) -> Any:
        self.calls += 1
        raise PisaError(
            "provider_error",
            "OpenAI returned a response Pisa could not use.",
            "Try again",
            detail="token_limit",
        )


def test_a_resting_slot_warns_once_and_is_counted(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import reflex as reflex_service

    rests.reset()
    gateway = _FailingGateway()
    service = reflex_service.ReflexService(
        gateway,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        owner=None,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(service, "_now_line", lambda: "Sunday 2026-11-22 12:00")
    read, by = asyncio.run(service._read("che cosa ho domani mattina", "it-IT"))
    assert isinstance(read, RulesRead) and by == "rules" and gateway.calls == 1
    asyncio.run(service._read("che cosa ho domani mattina", "it-IT"))
    assert gateway.calls == 1  # resting: the rules read the words, no call
    service._rest_until = 0.0  # the 5 minutes are over; it fails again within the hour
    asyncio.run(service._read("che cosa ho domani", "it-IT"))
    assert gateway.calls == 2 and rests.counts() == {"conversation.reflex": 2}
    out = capsys.readouterr().out
    warnings = [line for line in out.splitlines() if "model_slot_resting" in line]
    assert len(warnings) == 1  # once an hour
    assert "conversation.reflex" in warnings[0] and "token_limit" in warnings[0]
    assert "domani" not in out  # never the user's words
    rests.reset()


def test_rests_warn_again_after_an_hour() -> None:
    rests.reset()
    now = [0.0]
    assert rests.rested("attend.decide", "openai:gpt-5-nano", "token_limit", 300, clock=lambda: now[0])
    now[0] = 3599.0
    assert not rests.rested("attend.decide", "openai:gpt-5-nano", "token_limit", 300, clock=lambda: now[0])
    now[0] = 3601.0
    assert rests.rested("attend.decide", "openai:gpt-5-nano", "token_limit", 300, clock=lambda: now[0])
    assert rests.counts() == {"attend.decide": 3}
    rests.reset()


def test_readyz_shows_the_rests(test_database_url: str) -> None:
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.settings import Settings

    rests.reset()
    rests.rested("conversation.reflex", None, "token_limit", 300)
    ready = TestClient(create_app(Settings(timezone="Europe/Rome", database_url=test_database_url))).get(
        "/readyz"
    )
    assert ready.json()["model_rests"] == {"conversation.reflex": 1}
    rests.reset()
