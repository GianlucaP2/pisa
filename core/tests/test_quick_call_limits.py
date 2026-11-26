"""Every quick model call leaves room for reasoning, as the reflex does (984699ef9): a reasoning model spent a
small output limit thinking and answered nothing ("Model token limit exceeded before any response was
generated"). A call that must not think gets `fast_settings` (its answer's tokens plus FAST_REASONING_TOKENS,
thinking off where the model allows it, "minimal" where it always reasons); a call that may think briefly gets
`short_settings` with REASONING_TOKENS more. Intake, the briefing and the reader already add REASONING_TOKENS.
"""

import asyncio
from typing import Any

import pytest

from pisa.brain import attend, call, episodes, learn, place_research, understand
from pisa.brain.call import FAST_REASONING_TOKENS, REASONING_TOKENS, fast_settings, short_settings


class _Profiled:
    def __init__(self, **profile: Any) -> None:
        self.profile = profile


PROFILES = [
    {"thinking_always_enabled": True, "thinking_enabled_by_default": True},  # gpt-5-nano
    {"supports_thinking": True},  # Gemini 2.5 Flash: thinks by default, off when asked
    {"thinking_enabled_by_default": True},  # a model that reasons unless told
    {"supports_thinking": False},  # gpt-4.1-nano
]


class _Stop(Exception):
    pass


def _captured(monkeypatch: pytest.MonkeyPatch, module: Any) -> dict[str, Any]:
    """The settings the module's next Agent is built with (the call stops there)."""
    seen: dict[str, Any] = {}

    def agent(model: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(module, "Agent", agent)
    return seen


def _run(coro: Any) -> None:
    with pytest.raises(_Stop):
        asyncio.run(coro)


@pytest.mark.parametrize("profile", PROFILES)
def test_each_quick_call_leaves_room_for_reasoning(
    monkeypatch: pytest.MonkeyPatch, profile: dict[str, Any]
) -> None:
    model: Any = _Profiled(**profile)
    fast: list[tuple[str, dict[str, Any], int]] = []  # (call, its settings, the answer's own tokens)
    brief: list[tuple[str, dict[str, Any], int]] = []

    seen = _captured(monkeypatch, understand)
    _run(understand.confirm(model, "-", None, lambda usage: None))  # type: ignore[arg-type]
    fast.append(("understand.confirm", dict(seen), understand.CONFIRM_TOKENS))

    for name, run, tokens in (
        ("learn.translate", lambda: learn.translate(model, "-", "en"), learn.TRANSLATE_MAX_TOKENS),
        ("learn.rerank", lambda: learn.rerank(model, "-", ["a"], 1), learn.RERANK_MAX_TOKENS),
        ("learn.judge", lambda: learn.judge(model, "-", ["a"]), learn.JUDGE_MAX_TOKENS),
    ):
        seen = _captured(monkeypatch, learn)
        _run(run())
        fast.append((name, dict(seen), tokens))

    monkeypatch.setattr(attend, "prompt", lambda candidates, moment: ("-", {}))
    seen = _captured(monkeypatch, attend)
    _run(attend.decide(model, [], None, "en-US"))  # type: ignore[arg-type]
    brief.append(("attend.decide", dict(seen), attend.MAX_OUTPUT_TOKENS - REASONING_TOKENS))

    monkeypatch.setattr(place_research, "prompt", lambda *a: ("-", "-"))
    seen = _captured(monkeypatch, place_research)
    _run(place_research.research(model, None, None, [], [], "en-US"))  # type: ignore[arg-type]
    settings = dict(seen)["model_settings"]
    assert settings["max_tokens"] == 1024 + REASONING_TOKENS  # as the reader: the answer and the room

    for name, kw, tokens in fast:
        assert kw["model_settings"] == fast_settings(model, tokens), name
        assert kw["model_settings"]["max_tokens"] == tokens + FAST_REASONING_TOKENS, name
        assert kw["model_settings"].get("thinking") in (None, False, "minimal"), name
    for name, kw, tokens in brief:
        assert kw["model_settings"] == short_settings(model, tokens + REASONING_TOKENS), name


class _Gateway:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.max_output_tokens: list[int] = []

    async def run(self, slot: str, run: Any, *, max_output_tokens: int, **_: Any) -> Any:
        self.max_output_tokens.append(max_output_tokens)

        class Ctx:
            model = self.model
            usage = None

        return await run(Ctx())


@pytest.mark.parametrize("profile", PROFILES)
def test_the_engines_calls_leave_room_for_reasoning(
    monkeypatch: pytest.MonkeyPatch, profile: dict[str, Any]
) -> None:
    """`call()`: the night's engines (desk, ideas, curious, voice6, episodes) think briefly within
    REASONING_TOKENS more; memory and portrait (thinking=False) get fast_settings. The gateway's estimate
    counts the brief thinking's room, as intake's does; a fast call's estimate is its answer, as the
    reflex's."""
    model: Any = _Profiled(**profile)
    for thinking, expected, estimate in (
        (True, short_settings(model, 300 + REASONING_TOKENS), 300 + REASONING_TOKENS),
        (False, fast_settings(model, 300), 300),
    ):
        gateway = _Gateway(model)
        seen = _captured(monkeypatch, call)
        _run(call.call(gateway, "s", "-", "-", str, max_tokens=300, name="n", trace="t", thinking=thinking))
        assert seen["model_settings"] == expected and gateway.max_output_tokens == [estimate]


def test_the_longest_valid_answers_fit_their_own_tokens() -> None:
    """At 2 characters a token (cautious for JSON and Italian words), each quick call's longest valid answer
    fits the answer's own tokens: the reasoning room is beside it, never needed for the answer."""
    assert len("different") / 2 <= understand.CONFIRM_TOKENS
    assert len("contradicted") / 2 <= learn.JUDGE_MAX_TOKENS
    assert len(", ".join(str(n) for n in range(40, 60))) / 2 <= learn.RERANK_MAX_TOKENS  # 20 passage numbers
    longest = episodes.Written(digest="è" * 240).model_dump_json()
    assert len(longest) / 2 <= episodes.MAX_TOKENS
