"""The agency eval v1 on fake models: the harness's own numbers, no key, no cost.

`python -m tests.agency_eval` prints them; `test_body_line` asserts them. Two fakes bracket the metrics:
- `labelled` makes each case's first right move (with a purpose on every act): every metric must read perfect;
- `eager` acts at once on everything, without a purpose: right moves fall, over-asking stays 0, and every
  rail case counts as a breach attempted;
- `showy` puts a note on the phone for everything: over-showing rises, the red-team shows are
  attempted and held (0 shown), glasses-only shows come back unavailable and are said in words.
The real model's numbers come from the maintenance job `agency-eval` (pisa_tools/agency_eval.py).
"""

import asyncio
import json
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pisa_tools import agency_eval as ev

_FIRST = {"act": ("move_event", "answer_check_in"), "propose": ("compose_message",),
          "show": ("show_places",), "lookup": ("get_agenda",)}  # fmt: skip


def _case(messages: list[ModelMessage]) -> ev.Case:
    """The case being asked: the user's words, the turns before them, and Pisa's body and day in that
    request's instructions."""
    at, asked = next((i, m) for i, m in enumerate(messages) if isinstance(m, ModelRequest)
                     and any(isinstance(p, UserPromptPart) for p in m.parts) and m.instructions)  # fmt: skip
    words = next(p.content for p in asked.parts if isinstance(p, UserPromptPart))
    lines = asked.instructions or ""
    return next(c for c in ev.CASES if c.words == words and c.body in lines and len(c.before) == at
                and (c.day or ev.DAY) in lines and (bool(c.day) or ev.DAY_INVITE not in lines))  # fmt: skip


# A show's arguments, enough to read as the call (no tool runs in the eval).
_SHOW_ARGS: dict[str, dict[str, Any]] = {
    "compare_places": {"place_ids": ["p1", "p2"], "criteria": ["distanza"]},
    "show_setting": {"section": "places_modes", "highlight": "walk_max_min"},
    "show_note": {"title": "Dentista", "lines": ["Via Immaginaria 1"]},
    "ask_choice": {"question": "Dove?",
                   "options": [{"id": "o1", "label": "Riva"}, {"id": "o2", "label": "Ansa"}]},
    "show_item": {"ref": "draft:1"},
    "show_places": {"title": "x", "request": "y", "places": []},
}  # fmt: skip


def _show(case: ev.Case, purpose: str) -> ModelResponse:
    """Pisa says one line and shows, in the focus its body calls for."""
    args: dict[str, Any] = {"purpose": purpose} | _SHOW_ARGS.get(case.tool, {})
    if case.focus:
        args["focus"] = sorted(case.focus)[-1]  # take, or quiet / offer for a phone away
    return ModelResponse(parts=[TextPart(content="Te lo metto sul telefono."), ToolCallPart(case.tool, args)])


def _respond(move: str, purpose: str | None, never: frozenset[str] = frozenset()) -> ModelResponse:
    if move == "ask":
        return ModelResponse(parts=[TextPart(content="A che ora?")])
    if move == "speak":
        return ModelResponse(parts=[TextPart(content="Io andrei a piedi: sono dieci minuti.")])
    args: dict[str, Any] = {"purpose": purpose} if purpose else {}
    tool = next(t for t in _FIRST[move] if t not in never)
    return ModelResponse(parts=[ToolCallPart(tool, args)])


def labelled(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    case = _case(messages)
    order = ("act", "propose", "ask", "show", "speak", "lookup")
    move = next(m for m in order if m in case.moves)
    response = (
        _show(case, "così lo vedi") if move == "show" else _respond(move, "così sei in tempo", case.never)
    )
    return _found_first(response, info)


def _found_first(response: ModelResponse, info: AgentInfo) -> ModelResponse:
    """Batch E: a tool not offered yet is found with search_tools first (by its name), then called."""
    offered = {t.name for t in info.function_tools}
    missing = [
        p.tool_name for p in response.parts if isinstance(p, ToolCallPart) and p.tool_name not in offered
    ]
    if missing and ev.SEARCH in offered:
        return ModelResponse(parts=[ToolCallPart(ev.SEARCH, {"queries": missing})])
    return response


def eager(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _respond("act", None)


def showy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Puts a note on the phone for everything, in front, with one line: what outside text asks for too."""
    if any(isinstance(p, ToolReturnPart) and p.tool_name == "show_note" for p in messages[-1].parts):
        return ModelResponse(parts=[TextPart(content="Ecco.")])  # a show that wasn't seen: it says so
    note = {"title": "Nota", "lines": ["una riga"], "focus": "take", "purpose": "così lo leggi"}
    return ModelResponse(parts=[TextPart(content="Te lo scrivo."), ToolCallPart("show_note", note)])


async def numbers(fake: Any) -> dict[str, Any]:
    tools = await asyncio.to_thread(ev.tool_definitions)
    deferred = await asyncio.to_thread(ev.deferred_definitions, tools)
    model = FunctionModel(fake)

    async def ask(case: ev.Case, sent: list[ModelMessage], offered: list[Any]) -> tuple[ModelResponse, str]:
        return await ev.first_move(model, sent, offered), "fake"

    out = await ev.evaluate(ask, ev.CASES, tools, deferred)
    return {k: v for k, v in out.items() if k != "results"} | {"tools": len(tools), "deferred": len(deferred)}


if __name__ == "__main__":
    for name, fake in (("labelled", labelled), ("eager", eager), ("showy", showy)):
        print(name, json.dumps(asyncio.run(numbers(fake))))
