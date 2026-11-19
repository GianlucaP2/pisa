"""The conversation's history per model call, about 12k tokens at most.

- The last `KEEP_EXCHANGES` exchanges go verbatim; older ones become Pisa's summary of them (`digest`, made
  again from the turns for each call), given as one data block before them.
- Inside a turn every model request carries all the turn's tool results so far (a research turn read pages and
  reviews: most of the 25-40k input tokens per call). Past `BUDGET_TOKENS`, the results of the earlier rounds
  are cut to their first lines (`trim`), the largest first: outside results, then Pisa's own lists; the latest
  round stays whole. A tool result is never dropped: its call and its return stay paired, so the model still
  knows what it did.

- Tool calls: Pisa's replies keep their turn's tool calls and results (`kept`, `restored`). The last
  `KEEP_FULL_TURNS` turns give them back whole (a long outside result cut short); older ones keep each
  result's one-line receipt, framed as data: Pisa always knows what it did and what is only a proposal.
- Thinking: a thinking block is bound to the request that made it. An earlier turn's come back without their
  thinking (each turn's history is rebuilt), and so do a turn's responses after a result cut short.

Tokens are estimated (4 characters a token), as the gateway estimates them.
"""

import re
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
)

from pisa.brain.prefix import unthought
from pisa.brain.untrusted import OWN_KINDS, data_block
from pisa.domain import results
from pisa.domain.ai import CHARS_PER_TOKEN

BUDGET_TOKENS = 12_000  # FACT: the user's target for the history of one call
# the voice path's restored history
VOICE_BUDGET_TOKENS = 6_000
KEEP_EXCHANGES = 3  # the last user/Pisa exchanges kept word for word
DIGEST_CHARS = 1_600  # Pisa's summary of the older ones (oldest dropped first)
TRIMMED_CHARS = 600  # what an earlier round's tool result keeps once over budget
LATEST_CHARS = 2_500  # what a result of the latest round keeps, when even that round is over budget
KEEP_FULL_TURNS = 2  # the last turns whose tool results go back whole; older ones keep their receipts
WHOLE_CHARS = 2_000  # a kept result longer than this (a page, reviews) is cut short in later turns
_OPENED = re.compile(r"<([a-z-]+-[0-9a-f]{16})>")  # a data block's opening marker (untrusted.data_block)


def tokens(messages: list[ModelMessage]) -> int:
    return chars(messages) // CHARS_PER_TOKEN


def chars(messages: list[ModelMessage]) -> int:
    """The characters a request carrying these messages sends (contents and tool arguments)."""
    chars = 0
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                chars += len(content)
            elif content is not None:
                chars += len(str(content))
            args = getattr(part, "args", None)
            chars += len(str(args)) if args else 0
    return chars


def digest(older: list[tuple[str, str]]) -> str:
    """Pisa's summary of older exchanges: one line each, newest kept when it is too long. Deterministic (no
    model call, no cost): the user's words and Pisa's answer, each cut short."""
    lines: list[str] = []
    for role, content in older:
        text = " ".join(content.split())
        text = text if len(text) <= 140 else text[:139] + "…"
        lines.append(("the user: " if role == "user" else "Pisa: ") + text)
    out = "\n".join(lines)
    while len(out) > DIGEST_CHARS and "\n" in out:
        out = out.split("\n", 1)[1]
    return out[-DIGEST_CHARS:]


EARLIER = "Earlier in this conversation (Pisa's summary):"


def summary_message(summary: str) -> ModelRequest:
    block = data_block("history", summary, what="Pisa's summary of the earlier part of this conversation")
    return ModelRequest(
        parts=[UserPromptPart(content=f"Earlier in this conversation (Pisa's summary):\n{block.render()}")]
    )


def trim(messages: list[ModelMessage]) -> list[ModelMessage]:
    """A history processor: under BUDGET_TOKENS, the messages as they are; over it, tool results cut short:
    this turn's earlier rounds first, then the earlier turns' restored results, and the latest
    round last, which keeps more of each result (LATEST_CHARS): no call goes over the budget because one
    round read many pages."""
    if tokens(messages) <= BUDGET_TOKENS:
        return messages
    start = max(
        (i for i, m in enumerate(messages) if isinstance(m, ModelRequest)
         and any(isinstance(p, UserPromptPart) for p in m.parts)),
        default=0,
    )  # fmt: skip
    rounds = [i for i in range(start + 1, len(messages)) if _has_returns(messages[i])]
    before = [i for i in range(start) if _has_returns(messages[i])]
    out = list(messages)
    # the largest first (one huge page is what keeps a call over budget). Outside
    # results first, then, still over, Pisa's own lists too (largest first)
    for group, keep in ((rounds[:-1], TRIMMED_CHARS), (before, TRIMMED_CHARS), (rounds[-1:], LATEST_CHARS)):
        for own in (False, True):
            cuts = sorted(
                ((len(_text(p)), i, j) for i in group for j, p in enumerate(out[i].parts)
                 if _cuttable(p, own=own, keep=keep)),
                reverse=True,
            )  # fmt: skip
            for _, i, j in cuts:
                out[i] = _cut(out[i], j, keep)
                if tokens(out) <= BUDGET_TOKENS:
                    return _unbound_after(out, messages)
    return _unbound_after(out, messages)


def _unbound_after(out: list[ModelMessage], before: list[ModelMessage]) -> list[ModelMessage]:
    """The responses after the first result cut short lose their thinking: it was bound to the
    whole result (Anthropic refuses a replayed block whose earlier messages changed)."""
    first = next((i for i, (a, b) in enumerate(zip(out, before, strict=True)) if a is not b), len(out))
    return [*out[:first], *(unthought(m) for m in out[first:])]


def _cuttable(part: object, *, own: bool = False, keep: int = TRIMMED_CHARS) -> bool:
    """A long result: outside ones (pages, reviews, email), or, with `own`, only Pisa's own lists (the plan,
    the tasks: the model acts on every line, so they go last)."""
    text = _text(part)
    kinds = {tag.rsplit("-", 1)[0] for tag in _OPENED.findall(text)}
    mine = bool(kinds) and kinds <= OWN_KINDS
    return len(text) > keep and mine == own


def _text(part: object) -> str:
    """A tool result's text ('' for any other part)."""
    return part.content if isinstance(part, ToolReturnPart) and isinstance(part.content, str) else ""


def _has_returns(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in message.parts)


def _cut(message: ModelMessage, index: int, keep: int = TRIMMED_CHARS) -> ModelRequest:
    assert isinstance(message, ModelRequest)
    parts = []
    for n, part in enumerate(message.parts):
        if n == index and isinstance(part, ToolReturnPart) and isinstance(part.content, str):
            kept = part.content[:keep]
            # RAIL: a data block cut short is closed again, so nothing after it reads as instructions.
            # (the rule line names each marker once: count, don't look for it)
            tags = dict.fromkeys(_OPENED.findall(kept))
            closing = "".join(f"\n</{t}>" * (kept.count(f"<{t}>") - kept.count(f"</{t}>")) for t in tags)
            short = kept + "\n(trimmed: Pisa read the rest earlier in this turn)" + closing
            part = ToolReturnPart(tool_name=part.tool_name, content=short, tool_call_id=part.tool_call_id)
        parts.append(part)
    return ModelRequest(parts=parts, instructions=message.instructions)


def kept(messages: list[ModelMessage], *, said: bool = False) -> list[Any] | None:
    """The turn's model messages to keep with Pisa's reply: those after the user's words and before
    the final answer, as JSON; None when the turn called no tool. Instructions are left out: each turn has its
    own. The user's words are found (the last message with a prompt), never counted: Pydantic AI
    merges a history ending on a request with the new prompt. `said`: the finish sentence was said aloud.
    """
    start = max(
        (i for i, m in enumerate(messages) if isinstance(m, ModelRequest)
         and any(isinstance(p, UserPromptPart) for p in m.parts)),
        default=-1,
    )  # fmt: skip
    between = list(messages[start + 1 :])
    if between and isinstance(between[-1], ModelResponse):
        between.pop()  # the reply itself: kept as Pisa's words (what was said, after the rails)
    if not between:
        return None
    clean = [replace(m, instructions=None) if isinstance(m, ModelRequest) else m for m in between]
    if said:
        clean = [_finish_said(m) if isinstance(m, ModelRequest) else m for m in clean]
    return ModelMessagesTypeAdapter.dump_python(clean, mode="json")


def restored(raw: list[Any], *, whole: bool) -> list[ModelMessage]:
    """A turn's kept messages for the next model call: whole for the last turns (a long result cut short),
    else each result as its one-line receipt (framed as data, titles may come from an invite)."""
    try:
        messages = ModelMessagesTypeAdapter.validate_python(raw)
    except ValueError:
        return []
    out: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            request = message
            for n, part in enumerate(message.parts):
                if not isinstance(part, ToolReturnPart):
                    continue
                if not whole:
                    request = _receipt(request, n)
                elif len(_text(part)) > WHOLE_CHARS:
                    request = _cut(request, n)
            message = request
        out.append(unthought(message))  # bound to that turn's own requests, never this one's
    return out


def _finish_said(message: ModelRequest) -> ModelRequest:
    parts = [
        ToolReturnPart(tool_name=p.tool_name, content="Said aloud.", tool_call_id=p.tool_call_id)
        if isinstance(p, ToolReturnPart) and p.tool_name == "finish" else p
        for p in message.parts
    ]  # fmt: skip
    return ModelRequest(parts=parts, instructions=message.instructions)


def outside_kinds(messages: list[ModelMessage]) -> list[str]:
    """The outside text (email, pages) the history gives back whole: read this turn too."""
    kinds: list[str] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolReturnPart):  # a block's tag is named twice (its rule, its opening)
                tags = dict.fromkeys(_OPENED.findall(_text(part)))
                kinds += [kind for tag in tags if (kind := tag.rsplit("-", 1)[0]) not in OWN_KINDS]
    return kinds


def _receipt(message: ModelRequest, index: int) -> ModelRequest:
    parts = list(message.parts)
    part = parts[index]
    assert isinstance(part, ToolReturnPart)
    own = results.receipt_of(_text(part)) if part.tool_name in results.WRITE_TOOLS else None
    # A tool without a receipt may have written (a draft, a task note): never an invitation to do it again.
    # Pisa's ledger lists what it changed.
    said = (
        own or f"{part.tool_name}: called earlier (result gone; never repeat a change: the ledger has them)"
    )
    block = data_block("action", said, what="the receipt of what Pisa's tool did, from Pisa's own record")
    parts[index] = ToolReturnPart(
        tool_name=part.tool_name, content=block.render(), tool_call_id=part.tool_call_id
    )
    return ModelRequest(parts=parts, instructions=message.instructions)
