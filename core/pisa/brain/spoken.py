"""Pisa's turn is a loop. The model calls tools (several at once when they are independent),
reads every result and calls again until the task is done; then it speaks. At most MAX_ROUNDS model calls a
turn with tools, then one with none to say where it got to; past that (or if it fails), code says it from
the round's results. the style is for the final words only.

`finish(say=…)` keeps a one-round action fast: the model may give its final sentence in the same round as its
last writes. The conversation's model is wrapped (`Loop`): when every other result of that round is done (a
write `done`, an object on the user's screen), the next "model call" streams that sentence without calling
the provider; a proposal, an error, a read or anything else goes back to the model, which answers from it.
Nothing is billed for a call that never happened (the stream reports no usage).
"""

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from pisa.domain import results
from pisa.domain import talk as form
from pisa.domain.tools import named
from pisa.logging import get_logger

_log = get_logger("pisa.spoken")

# the presentation tools (the registry's `shows`: present_tools' tools, show_places, the research answer,
# Pisa's drawing) and the acknowledgments that leave nothing to say: the object is (or will be) in front
# of the user.
PRESENTATION = named(lambda t: t.shows)
TERMINAL = frozenset({"shown", "queued", "deferred"})
# RAIL: model calls in one turn before Pisa stops and says where it got to (a guard, not a style rule).
MAX_ROUNDS = 8
FINISH = "finish"
STOPPED = {
    "it-IT": "Mi fermo qui, a metà: {done} modifiche riuscite, {waiting} in attesa del tuo sì. Continuo?",
    "en-US": "I'll stop here, halfway: {done} changes went through, {waiting} wait for your yes. Go on?",
}


@dataclass
class Round:
    """The turn's loop: its rounds, the last round's `finish` sentence and shows, every result's status."""

    lang: str = "en-US"
    say: str | None = None  # the model's finish(say=…) of the round being run
    shows: list[tuple[str, str]] = field(default_factory=list)  # this round's shows: (tool, status)
    rounds: int = 0  # the provider's model calls (not the ones code answered: finish, the stop)
    finished: bool = False  # the reply was the model's own finish sentence, said by code
    statuses: dict[str, str] = field(default_factory=dict)  # each write result's status, by its receipt
    research: bool = False  # a research tool ran: its loop guard bounds the turn, not MAX_ROUNDS
    # the provider's last response, as streamed. A response that writes text and calls tools ends
    # the stream run, and its tools then run with a context that lacks it (finish read "no write holds their
    # words" and handed them back: the model answered again, "Hai detto che…")
    response: ModelResponse | None = None
    # the research loop guard said stop and the model searched again: the next
    # call may use no tool, so the model answers in its own words (never the stock NO_ANSWER)
    answer_now: Callable[[], bool] = field(default=lambda: False)

    def unseen(self, returns: list[ToolReturnPart]) -> bool:
        """This round showed something that isn't in front of the user (unavailable, unsupported, held,
        unconfirmed, or refused: a presentation result without an acknowledgment): the model words it."""
        shown = sum(status in TERMINAL for _, status in self.shows)
        asked = sum(p.tool_name in PRESENTATION for p in returns)
        return any(status not in TERMINAL for _, status in self.shows) or asked > shown

    def ready(self, messages: list[ModelMessage]) -> str | None:
        """The finish sentence, when the last round gave one and every other result of it is done; else
        None (the model reads the results)."""
        last = messages[-1] if messages else None
        say, self.say = self.say, None
        if not isinstance(last, ModelRequest):
            self.shows = []
            return None
        returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        for part in returns:
            if part.tool_name in results.WRITE_TOOLS and (status := results.status_of(_text(part))):
                # one entry per proposal or change (a proposal re-made is the same one)
                self.statuses[results.receipt_of(_text(part)) or part.tool_call_id] = status
        unseen = self.unseen(returns)
        self.shows = []
        if say is None or not say.strip() or unseen:
            return None
        if any(isinstance(p, RetryPromptPart) for p in last.parts):
            return None  # a call the model got wrong: it reads the retry prompt
        others = [p for p in returns if p.tool_name != FINISH and p.tool_name not in PRESENTATION]
        if any(p.tool_name not in results.WRITE_TOOLS or results.status_of(_text(p)) != results.DONE
               for p in others):  # fmt: skip
            return None
        self.finished = True
        # a model can write its words as text AND give them to finish in the same
        # response: the text was already said, so code says nothing more (one space: a blank reply would
        # be retried). Finish's paraphrase of that text too:
        # only a sentence of finish's that the text didn't say is said. Never the same reply twice.
        written = _written(messages)
        if not written.strip():
            return say
        # a sentence of finish's is said unless the text already said it (a long lead, "Ok, ci
        # penso io subito.", never hides "Ho spostato la palestra alle 17")
        new = [s for s in form.split_sentences(say) if not form.repeats(s, [written])]
        return " ".join(s.strip() for s in new) or " "

    def stopped(self) -> str:
        """Where Pisa got to, after MAX_ROUNDS (code's count of the turn's results)."""
        table = STOPPED.get(self.lang, STOPPED["en-US"])
        done = [r for r, s in self.statuses.items() if s == results.DONE and " done:" in r]
        waiting = {r.split(": ", 1)[-1] for r, s in self.statuses.items() if s == results.NEEDS_YES}
        return table.format(done=len(done), waiting=len(waiting))


def _written(messages: list[ModelMessage]) -> str:
    """The text the response that called finish wrote beside it (already streamed to the user)."""
    response = messages[-2] if len(messages) >= 2 else None
    if not isinstance(response, ModelResponse):
        return ""
    return "".join(p.content for p in response.parts if isinstance(p, TextPart))


def spoke_and_finished(messages: list[ModelMessage]) -> bool:
    """The last model response wrote its reply as text and called finish beside it: the model
    meant that text as its reply (a later round may add to it, never say it again)."""
    response = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
    if response is None:
        return False
    text = "".join(p.content for p in response.parts if isinstance(p, TextPart))
    return bool(text.strip()) and any(
        isinstance(p, ToolCallPart) and p.tool_name == FINISH for p in response.parts
    )


def _text(part: ToolReturnPart) -> str:
    return part.content if isinstance(part.content, str) else str(part.content)


# `loop` below: the turn's loop, handed to the tools when the agent is built for the turn (`finish` and the
# shows write to it); None outside a turn.


def presented(loop: Round | None, tool: str, status: str) -> None:
    """A presentation tool's acknowledgment: an object in front of the user counts as done for `finish`."""
    if loop is not None:
        loop.shows.append((tool, status))


def with_streamed(
    loop: Round | None, messages: list[ModelMessage], call_id: str | None
) -> list[ModelMessage]:
    """The run's messages with the streamed response that made this call, when the run left it
    out (text and tool calls in one response: its tools run before the run records it)."""
    response = loop.response if loop is not None else None
    if response is None or call_id is None or any(r is response for r in messages):
        return messages
    if not any(isinstance(p, ToolCallPart) and p.tool_call_id == call_id for p in response.parts):
        return messages
    return [*messages, response]


def finish_said(loop: Round | None, say: str) -> None:
    if loop is not None:
        loop.say = say


@dataclass
class _Spoken(StreamedResponse):
    _text: str = ""
    _model_name: str = "finish"
    _timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        for event in self._parts_manager.handle_text_delta(vendor_part_id="content", content=self._text):
            yield event

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str | None:
        return None

    @property
    def provider_url(self) -> str | None:
        return None

    @property
    def timestamp(self) -> datetime:
        return self._timestamp


class Loop(WrapperModel):
    def __init__(self, wrapped: Model, loop: Round) -> None:
        super().__init__(wrapped)
        self.loop = loop

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        text = self.loop.ready(messages)
        if text is None and self.loop.rounds == MAX_ROUNDS and not self.loop.research:
            # writes made one a round can leave no call to say so ("Mi fermo qui, a metà" after all was
            # done). Past the cap, one last call with no tools: the model says where it got to, from the
            # results it read. The tools stay listed (the prefix every earlier round, and its
            # thinking, was made with) and the call may use none of them.
            self.loop.rounds += 1
            last = ModelSettings(**{**(model_settings or {}), "tool_choice": "none"})
            async with AsyncExitStack() as stack:
                try:
                    stream = await stack.enter_async_context(
                        self.wrapped.request_stream(messages, last, model_request_parameters, run_context)
                    )
                except Exception as exc:  # the provider refused it: code says where Pisa got to
                    _log.warning("round_cap_call_failed", cause=exc.__class__.__name__)
                    stream = _Spoken(
                        model_request_parameters=model_request_parameters, _text=self.loop.stopped()
                    )
                yield stream
            return
        if text is None and self.loop.rounds > MAX_ROUNDS and not self.loop.research:
            text = self.loop.stopped()
        if text is None and self.loop.answer_now():
            # the tools stay listed (the prefix this turn's thinking was made with), as on the
            # round cap's last call; this call may use none of them
            model_settings = ModelSettings(**{**(model_settings or {}), "tool_choice": "none"})
        if text is None:
            self.loop.rounds += 1
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                self.loop.response = None
                yield stream
                self.loop.response = stream.get()
            return
        yield _Spoken(model_request_parameters=model_request_parameters, _text=text)
