"""A fake model for intake: answers triage and extraction with scripted structured output, and records what
it was sent (prompts, instructions, tools) so tests can check what reached the model."""

import json
from collections.abc import Callable
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

Answer = dict[str, Any] | Callable[[int], dict[str, Any]]  # a fixed answer, or one per attempt (0, 1)


class IntakeModel:
    def __init__(self) -> None:
        # subject → category, or the whole verdict {"category", "automated", "asks_owner_to_act"}
        self.triage: dict[str, str | dict[str, Any]] = {}
        self.extract: dict[str, Answer] = {}  # subject → {"tasks": [...]}
        self.seen: list[dict[str, Any]] = []

    def __call__(self, spec, model_id, api_key, ollama_base_url) -> FunctionModel:  # type: ignore[no-untyped-def]
        return FunctionModel(self._answer, model_name=f"fake-intake-{model_id}")

    def _answer(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = next(
            str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        )
        # The email's text is datamarked: every space a ˆ.
        subject = next(line[9:] for line in prompt.splitlines() if line.startswith("Subject: "))
        subject = subject.replace("ˆ", " ")
        retries = [
            p
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, RetryPromptPart)
        ]
        tool = info.output_tools[0]
        kind = "triage" if "category" in json.dumps(tool.parameters_json_schema) else "extract"
        self.seen.append(
            {
                "kind": kind,
                "subject": subject,
                "prompt": prompt,
                "instructions": info.instructions or "",
                "function_tools": [t.name for t in info.function_tools],
                "retry_errors": [str(r.content) for r in retries],
            }
        )
        if kind == "triage":
            verdict = self.triage.get(subject, "info")
            args: dict[str, Any] = {
                "automated": False,
                "asks_owner_to_act": False,
                "already_done": False,
                "reason": "Scripted.",
            }
            args |= {"category": verdict} if isinstance(verdict, str) else verdict
        else:
            answer = self.extract.get(subject, {"tasks": []})
            args = answer(len(retries)) if callable(answer) else answer
        return ModelResponse(parts=[ToolCallPart(tool.name, json.dumps(args))])

    def calls(self, kind: str) -> list[str]:
        return [s["subject"] for s in self.seen if s["kind"] == kind]


def task(title: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": title,
        "due_at": None,
        "due_kind": "none",
        "duration_min": None,
        "place_text": None,
        "priority": 2,
        "confidence": 0.9,
    }
    return base | fields
