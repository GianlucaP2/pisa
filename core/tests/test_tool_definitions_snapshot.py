"""The model's tool input, frozen: for each kind of turn, every tool definition the conversation model is
offered on the turn's first request (name, description, JSON schema, deferral, in order), every tool
registered (the deferred ones included, with their deferral), and the tool search's own description. A change
to how the tools are declared or registered must leave these bytes as they are.

The snapshot (`snapshots/tool_definitions.json`) holds, per turn kind, each offered tool's name and the
sha256 of its whole canonical definition. Regenerate it only for an intended change to the model's input:
`PISA_REGEN_TOOL_SNAPSHOT=1 uv run pytest tests/test_tool_definitions_snapshot.py`.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tests.owner import owner_at

SNAPSHOT = Path(__file__).parent / "snapshots" / "tool_definitions.json"
NOW = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)

# kind -> (their words, voice, defer, drawing box on, drawing turn's origin, a kept drawing, kept findings)
KINDS: dict[str, tuple[str, bool, bool, bool, str, bool, bool]] = {
    "text": ("che ore sono?", False, True, False, "words", False, False),
    "voice": ("che ore sono?", True, True, False, "words", False, False),
    "text_box_on": ("che ore sono?", False, True, True, "words", False, False),
    "voice_box_on": ("che ore sono?", True, True, True, "words", False, False),
    "drawing_tap": ("", False, True, True, "drawing", True, False),
    "learn_course": ("cosa dicono le fonti del corso?", False, True, False, "words", False, False),
    "learn_course_box_on": ("cosa dicono le fonti del corso?", False, True, True, "words", False, False),
    # the turn's offers: a view named, a redraw of the kept drawing, saving it, a telling for their note,
    # the last research's findings
    "offers": (
        "mostrami meteo e ridisegnalo, salvalo; la prossima volta dimmelo prima",
        False,
        True,
        True,
        "words",
        True,
        True,
    ),
    "every_tool": ("cosa dicono le fonti del corso?", False, False, True, "words", False, False),
}


def _canonical(tool: Any) -> str:
    return json.dumps(dataclasses.asdict(tool), sort_keys=True, default=str, ensure_ascii=False)


def definitions(kind: str, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The tool definitions offered on a turn of this kind, as (name, sha256) in order (stand-in services)."""
    from pisa.brain import tool_search
    from pisa.brain.conversation import TurnContext, build_agent
    from pisa.brain.drawing_tools import DrawingTurn
    from pisa.brain.profile_tools import ProfileTurn
    from pisa.brain.research_turn import ResearchTurn

    words, voice, defer, box, origin, kept, findings = KINDS[kind]
    monkeypatch.setenv("PISA_DRAWING_BOX", "on" if box else "off")
    seen: list[AgentInfo] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart(content="ok")])

    s = MagicMock(timezone="Europe/Rome")
    s.skill_names = AsyncMock(return_value=[])
    s.paired = AsyncMock(return_value=False)
    s.names = AsyncMock(return_value=["meteo"])

    async def noop(_: Any) -> bool:
        return True

    async def kept_note(_: str) -> None:
        return None

    research = ResearchTurn(lang="it-IT", prompt_chars=0)
    if findings:
        research.earlier, research.earlier_at = {"shown": [], "pages": []}, "14:05"
    drawing = DrawingTurn(
        lang="it-IT", words=words, origin=origin, kept={"basis": "knowledge"} if kept else None
    )
    agent = build_agent(
        FunctionModel(capture),
        TurnContext(owner=owner_at(clock=lambda: NOW), clock=lambda: NOW, data=s, places=s,
        actions=s, profile=s, compose=noop, call=noop, research=research, decided=s, defer=defer, voice=voice,
        views=s, trips=s, people=s, rituals=(s, s), autopilot=s, handoffs=s, money=s, public=(s, s), world=s,
        explore=s, team=kept_note, sources=s, loops=s, owner_note=s, learn=s, night=s, memory=s,
        drawing=drawing, profile_turn=ProfileTurn(words=words, lang="it-IT"),)
    )  # fmt: skip
    asyncio.run(agent.run(words or "x"))
    info = seen[0]
    out = [[t.name, hashlib.sha256(_canonical(t).encode()).hexdigest()] for t in info.function_tools]
    out.append(["<tool_search.DESCRIPTION>", hashlib.sha256(tool_search.DESCRIPTION.encode()).hexdigest()])
    # every tool registered, the deferred ones included (sent with defer_loading where the provider searches)
    for toolset in agent.toolsets:
        for name, tool in getattr(toolset, "tools", {}).items():
            canonical = _canonical(tool.tool_def) + f"|defer={tool.defer_loading}"
            out.append([f"registered:{name}", hashlib.sha256(canonical.encode()).hexdigest()])
    return out


# the night's own listing (`services.night.tool_definitions`): N8's yardstick, offered and every tool
NIGHT_KINDS: dict[str, bool] = {"night": True, "night_every_tool": False}


def night_definitions(defer: bool, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The night agent's tool definitions, as (name, sha256) in order."""
    from pisa.services.night import tool_definitions

    monkeypatch.setenv("PISA_DRAWING_BOX", "off")
    return [
        [t.name, hashlib.sha256(_canonical(t).encode()).hexdigest()] for t in tool_definitions(defer=defer)
    ]


def test_every_turn_kind_offers_the_same_tool_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {kind: definitions(kind, monkeypatch) for kind in KINDS}
    now |= {kind: night_definitions(defer, monkeypatch) for kind, defer in NIGHT_KINDS.items()}
    if os.environ.get("PISA_REGEN_TOOL_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(now, indent=1) + "\n", encoding="utf-8")
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(now) == set(frozen)
    for kind in [*KINDS, *NIGHT_KINDS]:
        assert [n for n, _ in now[kind]] == [n for n, _ in frozen[kind]], f"{kind}: the tools or their order"
        changed = [n for (n, h), (_, f) in zip(now[kind], frozen[kind], strict=True) if h != f]
        assert not changed, f"{kind}: these definitions changed: {changed}"
