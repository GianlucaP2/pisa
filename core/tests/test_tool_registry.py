"""The tool registry (`pisa.domain.tools`) is the one declaration of each tool: what the brain attaches to it
and what the domain derives from it agree with it."""

import pytest

from pisa.brain import registry, replay
from pisa.domain import actions, approvals, tools


def test_every_held_tool_has_its_replay_and_nothing_else_does() -> None:
    held = tools.named(lambda t: t.held)
    assert set(replay.REPLAYS) == held


def test_every_grounding_rule_is_one_the_domain_knows_and_its_tool_may_be_held() -> None:
    for tool in tools.TOOLS:
        if tool.grounding is not None:
            assert tool.grounding in actions._GROUNDS, tool.name  # pyright: ignore[reportPrivateUsage]
            assert tool.held, tool.name


def test_each_approval_kind_is_one_tool_s() -> None:
    kinds = [t.approval for t in tools.TOOLS if t.approval is not None]
    assert sorted(kinds) == sorted(approvals.KINDS)
    assert approvals.UNDO_MINUTES == {
        "calendar_create": 1440, "calendar_move": 1440, "calendar_cancel": 1440, "email_send": None,
    }  # fmt: skip
    assert approvals.TOOL_OF["email_send"] == "send_draft"


def test_a_tool_the_registry_does_not_declare_is_never_offered() -> None:
    from pydantic_ai import Agent

    agent = Agent(None)

    async def pay_the_bill() -> str:
        return "paid"

    with pytest.raises(ValueError, match="not in the tool registry"):
        registry.offer(agent)(pay_the_bill)


def test_a_held_change_with_no_replay_raises() -> None:
    import asyncio
    from typing import Any, cast

    with pytest.raises(ValueError, match="no replay"):
        asyncio.run(replay.run_change(cast(Any, None), replay.Change("send_draft", {})))
