"""Batch E: what a turn sends the model, before (every tool at once) and after (about 20 tools, the
rest found by search_tools), on Pisa's own agent with a fake model. No key, no cost.

`python -m tests.turn_size` prints the numbers; `test_changes_feed` asserts them. Per turn: the model requests
it takes, the input tokens of each (4 characters a token, as the gateway estimates them: the tools' schemas,
the instructions, the messages), the stable prefix (the tools and the system prompt: what the provider's cache
keeps from one turn to the next) and whether that prefix is the same in every turn. The first sentence's wait
is modelled as the latency report models a provider: 0.45 s + 0.12 s per 1,000 input tokens to the first
token, the same again for each round before it.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pisa.brain.conversation import TurnContext, build_agent, system_prompt
from pisa.brain.research_turn import ResearchTurn
from tests.owner import owner_at

CHARS_PER_TOKEN = 4
FIRST_TOKEN_S, PER_KTOKEN_S = 0.45, 0.12
# A turn: the user's words and the deferred tool the model needs (None: an answer from the loaded ones).
TURNS: list[tuple[str, str | None]] = [
    ("che ore sono?", None),
    ("cosa ho oggi?", None),
    ("sposta la palestra a domani", None),
    ("che tempo fa domani?", None),
    ("trovami un bar qui vicino", "map_search"),
    ("metti il pacco in alta priorità", "set_task_priority"),
]


def _chars(info: AgentInfo, messages: list[ModelMessage]) -> tuple[int, int]:
    """(the stable prefix: the tools' schemas and the system prompt; everything the request carries)."""
    tools = sum(len(json.dumps(t.parameters_json_schema)) + len(t.description or "") + len(t.name)
                for t in info.function_tools)  # fmt: skip
    prefix = tools + len(system_prompt(lang="it-IT"))
    rest = sum(len(str(getattr(p, "content", "") or "")) for m in messages for p in m.parts)
    dynamic = len(info.instructions or "") - len(system_prompt(lang="it-IT"))
    return prefix, prefix + max(dynamic, 0) + rest


async def one_turn(words: str, needs: str | None, *, defer: bool) -> dict[str, Any]:
    requests: list[tuple[int, int, str]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prefix, total = _chars(info, messages)
        names = sorted(t.name for t in info.function_tools)
        requests.append((prefix, total, ",".join(names)))
        offered = set(names)
        if needs is not None and needs not in offered and len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": [needs]})])
        return ModelResponse(parts=[TextPart(content="Ok.")])  # the answer (the tool's own call left out)

    stand_in = MagicMock(timezone="Europe/Rome")

    async def noop(_: Any) -> bool:
        return True

    agent = build_agent(
        FunctionModel(model), TurnContext(owner=owner_at(), clock=lambda: datetime.now(UTC), data=stand_in,
        places=stand_in, actions=stand_in, profile=stand_in, compose=noop, call=noop,
        research=ResearchTurn(lang="it-IT", prompt_chars=0), decided=stand_in, defer=defer,)
    )  # fmt: skip
    await agent.run(words)
    tokens = [total // CHARS_PER_TOKEN for _, total, _ in requests]
    first_sentence = sum(FIRST_TOKEN_S + PER_KTOKEN_S * t / 1000 for t in tokens)
    return {
        "requests": len(requests),
        "input_tokens": sum(tokens),
        "prefix_tokens": requests[0][0] // CHARS_PER_TOKEN,
        "first_sentence_s": round(first_sentence, 2),
        "prefix_key": requests[0][2],
    }


async def numbers() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for defer in (False, True):
        rows = {words: await one_turn(words, needs, defer=defer) for words, needs in TURNS}
        keys = {r.pop("prefix_key") for r in rows.values()}
        out["after" if defer else "before"] = {
            "turns": rows,
            "prefix_same_every_turn": len(keys) == 1,  # the provider's cache keeps it from turn to turn
            "input_tokens_mean": round(sum(r["input_tokens"] for r in rows.values()) / len(rows)),
        }
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(numbers()), indent=1))
