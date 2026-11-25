"""F6 (Google off): the model searched for Gmail three to five times, then a working filler and the refusal
twice. search_tools now answers a search for a feature switched off on this install once and plainly (off
here, say so, don't search again), and a second search in the turn is told to stop. Only results change."""

import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pisa.brain import tool_search
from pisa.brain.conversation import TurnContext, build_agent
from pisa.domain import connectors, experimental
from tests.owner import owner_at

NONE: frozenset[str] = frozenset()


def _force_off(monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
    """Google and places are built, so on by default: these tests force them off (a test-only registry
    override), as an install with them experimental would have."""
    for feature_id in ids:
        demoted = dataclasses.replace(experimental.BY_ID[feature_id], experimental=True)
        monkeypatch.setitem(experimental.BY_ID, feature_id, demoted)


def test_rules_on_a_default_install_only_an_experimental_feature_is_named() -> None:
    assert tool_search.searched_off(["read my latest email"], NONE, found=False) is None  # Google is on
    assert tool_search.searched_off(["garmin sleep"], NONE, found=False) == "Garmin"
    assert tool_search.searched_off(["garmin sleep"], frozenset({"garmin"}), found=False) is None


def test_rules_a_search_for_an_off_feature_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_off(monkeypatch, "google", "places")
    assert tool_search.searched_off(["read my latest email"], NONE, found=False) == (
        "Google (Gmail and Google Calendar)"
    )
    assert tool_search.searched_off(["gmail calendar"], NONE, found=True) is not None  # a strong word
    assert tool_search.searched_off(["calendar"], NONE, found=True) is None  # Pisa's own calendar, on
    assert tool_search.searched_off(["set_task_priority"], NONE, found=False) is None  # nothing off
    assert tool_search.searched_off(["gmail"], experimental.ALL, found=False) is None  # on: searched as ever
    assert tool_search.searched_off(["map search restaurants"], NONE, found=False) == (
        "searching places, maps and the web"
    )


def test_rules_the_off_answer_is_plain_once_then_says_stop() -> None:
    first = tool_search.off_message("Google (Gmail and Google Calendar)", again=False)
    again = tool_search.off_message("Google (Gmail and Google Calendar)", again=True)
    assert "isn't available on PISA" in first and "don't search for it again" in first
    assert "Stop searching" in again and len(again) < len(first)
    off_tools = set(connectors.GOOGLE_TOOLS) | set().union(*experimental.TOOLS.values())
    assert not any(name in first or name in again for name in off_tools)  # no tool name to call


def _run(searches: list[list[str]], enabled: frozenset[str]) -> list[Any]:
    returns: list[Any] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns[:] = [
            p.content for m in messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart) and p.tool_name == "search_tools"
        ]  # fmt: skip
        if len(returns) < len(searches):
            return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": searches[len(returns)]})])
        return ModelResponse(parts=[TextPart("Google non è attivo su questo server.")])

    agent = build_agent(
        FunctionModel(model),
        TurnContext(owner=owner_at(), clock=lambda: datetime.now(UTC), enabled=enabled),
    )
    asyncio.run(agent.run("leggi la mia ultima email"))
    return returns


def test_a_search_for_gmail_with_google_off_is_answered_once_then_told_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_off(monkeypatch, "google")
    first, second, third = _run([["read_my_email"], ["gmail inbox"], ["email"]], NONE)
    # weak matches (a "read", a "my") never hide that the feature asked for is off
    assert "isn't available on PISA" in first["message"]
    assert "Stop searching" in second["message"] and "Stop searching" in third["message"]


def test_a_search_for_an_on_feature_is_unchanged() -> None:
    (only,) = _run([["set_task_priority"]], NONE)
    assert tool_search.OFF_MARK not in str(only)
