"""The provider's prefix (the tools, then the system prompt) is the same bytes on every request
of a turn.

Pydantic AI renders every instruction, the dynamic ones too (the turn's context, the clock, the hints, the
research guide, a follow-up run's line), into the provider's top-level system prompt on every request. So the
system prompt changed inside a turn whenever a line joined or a named line's data block got a new tag. On a
model that binds its thinking to the prefix (Claude 5.5: Anthropic refuses a replayed thinking block whose
tools, system or earlier messages changed) the turn's earlier reasoning was refused and retried without it
(`AnthropicStaleThinkingBlockWarning`), and the prompt cache missed what followed.

`StablePrefix` freezes the system prompt at the turn's first request: the fixed instructions and every line
the turn starts with (the turn's context, the hints, Pisa's views…), as first said, and the same bytes on
every later request of the turn (a follow-up run of it too). Only a line that joins mid-turn (the research
guide after the result that started it, the cut line on the last run) goes into the turn's messages, as a
system entry in the request where it joined, and stays there. Across turns the system prompt changes, which
binds nothing: an earlier turn comes back without its thinking (`history.restored`).

 moved the turn's context out of the system prompt too, after their words; the real model followed it
less (more questions, more rounds, Pisa in the third person), so it is back where the model reads
it.

A scripted model (Pydantic AI's FunctionModel and TestModel: no provider, no prefix) gets the instructions as
the agent made them: the tests read the turn's context where Pydantic AI hands it to them.
"""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from pisa.brain.models.providers import PROVIDERS

SCRIPTED = frozenset({"function", "test"})  # Pydantic AI's own scripted models: no provider prefix
INLINE_SYSTEMS = frozenset(p.id for p in PROVIDERS if p.inline_system)  # Pydantic AI system names


class StablePrefix(WrapperModel):
    """The system prompt frozen at the turn's first request; a line that joins later goes into the turn's
    messages where it joined (one instance per turn's agent: a follow-up run of the same turn finds the same
    system prompt and its lines in the same places)."""

    def __init__(self, wrapped: Any) -> None:
        super().__init__(wrapped)
        self._turn: tuple[int, str] | None = None  # the request with their words: a new one is a new turn
        self._system: list[InstructionPart] = []  # the turn's system prompt, as its first request said it
        self._said: set[str] = set()  # its dynamic lines (or their names)
        self._joined: dict[str, tuple[str, int]] = {}  # a line joined mid-turn -> it, and its request

    def moved(
        self, messages: list[ModelMessage], params: ModelRequestParameters
    ) -> tuple[list[ModelMessage], ModelRequestParameters]:
        """The request with the turn's system prompt and the lines that joined mid-turn in its messages."""
        parts = params.instruction_parts
        if not parts or self.system in SCRIPTED or not messages or not self.inline():
            return messages, params
        turn = _turn(messages)
        if turn != self._turn:  # the turn's first request: its lines, as said now, are its system prompt
            self._turn, self._joined = turn, {}
            self._system = [p for p in parts if not p.dynamic or p.content.strip()]
            self._said = {_key(p) for p in self._system if p.dynamic}
        out = list(messages)
        for part in parts:
            if part.dynamic and part.content.strip() and _key(part) not in self._said:
                self._joined.setdefault(_key(part), (part.content.strip(), len(out) - 1))
        at: dict[int, list[str]] = {}
        for line, index in self._joined.values():
            if index < len(out) and isinstance(out[index], ModelRequest):
                at.setdefault(index, []).append(line)
        for index, joined in at.items():
            request = out[index]
            assert isinstance(request, ModelRequest)
            part = SystemPromptPart(content="\n\n".join(joined))
            out[index] = dataclasses.replace(request, parts=[*request.parts, part])
        return out, dataclasses.replace(params, instruction_parts=list(self._system))

    def inline(self) -> bool:
        """The provider takes a system entry inside the messages. One
        without it would read the turn's lines (outside text in data blocks among them) as the user's own
        user text; it keeps them in the system prompt (no thinking bound to the prefix there:
        only the cache is lost). A FallbackModel (its models differ; its profile raises) does too."""
        # only a provider whose spec says so (Anthropic: the prefix binds the thinking); an
        # OpenAI model (its profile also takes inline system entries) keeps its system prompt first
        if self.system not in INLINE_SYSTEMS:
            return False
        try:
            return bool(self.profile.get("supports_inline_system_prompts", False))
        except NotImplementedError:
            return False

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        messages, model_request_parameters = self.moved(messages, model_request_parameters)
        return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncIterator[StreamedResponse]:
        messages, model_request_parameters = self.moved(messages, model_request_parameters)
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream


def _key(part: InstructionPart) -> str:
    """A named line (its data block's tag is new on every call) by its name; any other by its words."""
    return part.name or part.content.strip()


def _turn(messages: list[ModelMessage]) -> tuple[int, str]:
    """The turn: the newest request carrying their words (a follow-up run of the turn sends none of its
    own)."""
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if isinstance(message, ModelRequest):
            words = [p for p in message.parts if isinstance(p, UserPromptPart)]
            if words:
                return i, str(words[-1].content)
    return -1, ""


def unthought(message: ModelMessage) -> ModelMessage:
    """A response without its thinking: a block bound to a prefix that no longer exists (an earlier turn's,
    rebuilt for this one; a round whose earlier results were cut short) would only be refused."""
    if not isinstance(message, ModelResponse) or not any(isinstance(p, ThinkingPart) for p in message.parts):
        return message
    return dataclasses.replace(message, parts=[p for p in message.parts if not isinstance(p, ThinkingPart)])
