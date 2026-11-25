"""One typed model call on a slot, and the settings every short call shares.

`call` runs one agent with a typed output on a slot's model through the gateway: one ledger row per call,
with `trace` as its trace id. The caller writes the instructions and the prompt (outside text only as a data
block); the language name comes from `pisa.domain.lexicon.languages`, as before.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from pisa.brain.models.compat import output_spec
from pisa.brain.models.gateway import CallContext

# A reasoning model counts its thinking against max_tokens: with only the words' tokens (60 words → 240) it
# can spend them all thinking and answer nothing. Paid only when used.
REASONING_TOKENS = 2048
# These calls are short (a briefing, an email's triage, its tasks): a model that reasons by default thinks
# briefly. Only for those: on a model whose thinking is off unless asked, "low" would
# turn it on, with its own token budget.
SHORT_THINKING = "low"


def short_settings(model: Model, max_tokens: int, **more: Any) -> ModelSettings:
    """max_tokens, and a low reasoning effort only for a model that reasons when not told otherwise."""
    profile = model.profile
    reasons = bool(profile.get("thinking_enabled_by_default") or profile.get("thinking_always_enabled"))
    settings = ModelSettings(max_tokens=max_tokens, **more)
    if reasons:
        settings["thinking"] = SHORT_THINKING
    return settings


# A fast call (the reflex, aimed at 300 ms) never thinks where the model lets it: thinking=False. A model that
# always reasons (OpenAI's gpt-5 and o-series, grok-3-mini) thinks the least it can ("minimal"), and one whose
# "off" is still a low level (Gemini 3) keeps some: either way the reasoning counts against max_tokens, so the
# limit is the answer's tokens plus this room (a Gemini 2.5 Flash thinking by default, or a gpt-5-nano at
# "low", spent a 120-token limit thinking and answered nothing). The room is a ceiling, paid only when used.
FAST_REASONING_TOKENS = 1024


def fast_settings(model: Model, max_tokens: int) -> ModelSettings:
    """max_tokens for the answer plus room for any reasoning, and the least thinking the model allows."""
    profile = model.profile
    settings = ModelSettings(max_tokens=max_tokens + FAST_REASONING_TOKENS)
    if profile.get("thinking_always_enabled"):
        settings["thinking"] = "minimal"
    elif profile.get("supports_thinking") or profile.get("thinking_enabled_by_default"):
        settings["thinking"] = False
    return settings


async def call[T](
    gateway: Any,
    slot: str,
    instructions: str,
    prompt: str,
    out: type[T],
    *,
    max_tokens: int,
    name: str,
    trace: str,
    thinking: bool = True,
) -> T:
    """One typed call on `slot`'s model through the gateway: one ledger row with `trace` as its trace id, the
    output checked against `out` (one retry). `max_tokens` is the answer's own: `thinking=False` is the
    rulings' "no thinking", as little as the model allows, with room for what it can't turn off
    (`fast_settings`); otherwise a reasoning model thinks briefly (`short_settings`) within REASONING_TOKENS
    more."""
    limit = max_tokens + REASONING_TOKENS if thinking else max_tokens

    async def run(ctx: CallContext) -> T:
        agent: Agent[None, T] = Agent(
            ctx.model,
            output_type=output_spec(ctx.model, out),
            instructions=instructions,
            model_settings=short_settings(ctx.model, limit)
            if thinking
            else fast_settings(ctx.model, max_tokens),
            retries={"output": 1},
            name=name,
        )
        result = await agent.run(prompt)
        ctx.usage.update(result.usage)
        return result.output

    return await gateway.run(
        slot, run, prompt_chars=len(instructions) + len(prompt), max_output_tokens=limit, trace_id=trace
    )


class Think(Protocol):
    """An engine's typed call by task name; tests pass a fake."""

    async def __call__[T](
        self, task: str, instructions: str, prompt: str, out: type[T], *, trace: str
    ) -> T: ...


def thinker(gateway: Any, slot: str, max_tokens: Mapping[str, int], agent: str) -> Think:
    """The production Think of an engine: each task's call on `slot` with that task's token cap, as agent
    `pisa-{agent}-{task}`. The night's budget (or the day's caps) is the caller's."""

    async def think[T](task: str, instructions: str, prompt: str, out: type[T], *, trace: str) -> T:
        name = f"pisa-{agent}-{task}"
        return await call(
            gateway, slot, instructions, prompt, out, max_tokens=max_tokens[task], name=name, trace=trace
        )

    return think  # type: ignore[return-value]
