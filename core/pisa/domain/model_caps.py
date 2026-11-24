"""What a model on an OpenAI-compatible server can do: the probe's vocabulary, overrides and output modes.

A self-hosted server (vLLM, SGLang, llama.cpp, Ollama, LM Studio, a LiteLLM proxy) says little about its
models, so Pisa probes each one with tiny real requests. The user can override any result by hand. The
effective value of a capability is the probe's result with the user's overrides applied; a model never
probed has every capability false. Nothing is assumed.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args

from pisa.domain.ai import SLOTS, Capability

ServerCapability = Literal[
    "tools", "json_schema", "json_object", "tool_choice", "vision", "audio", "embeddings"
]
SERVER_CAPABILITIES: tuple[ServerCapability, ...] = get_args(ServerCapability)

OutputMode = Literal["native", "tool", "prompted"]
"""How a use gets structured output: the server's `json_schema` response format, a tool call the answer goes
into, or a JSON schema in the prompt (with the `json_object` response format when the server has it)."""

# The id of the server PISA_OLLAMA_BASE_URL describes.
OLLAMA_ID = "ollama"

# A server's id is the provider part of its model references ("<id>:<model_id>"), so it can't hold ':'.
SERVER_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# The features (ids as in /v1/server's `features`) whose slot needs tool calling.
SLOT_FEATURES: dict[str, str] = {"chat.conversation": "conversation"}


def features_needing_tools() -> list[str]:
    return sorted({SLOT_FEATURES[s.id] for s in SLOTS if "tools" in s.needs and s.id in SLOT_FEATURES})


@dataclass(frozen=True)
class ModelCaps:
    """One model's capabilities on one server: the probe's results, when, and the user's overrides."""

    probe: dict[str, bool] | None = None  # None: never probed
    probed_at: datetime | None = None
    overrides: dict[str, bool] = field(default_factory=dict)

    def has(self, cap: ServerCapability) -> bool:
        if cap in self.overrides:
            return self.overrides[cap]
        return bool(self.probe and self.probe.get(cap, False))

    def effective(self) -> dict[ServerCapability, bool]:
        return {c: self.has(c) for c in SERVER_CAPABILITIES}

    def capabilities(self) -> list[Capability]:
        """The slot vocabulary (`SlotSpec.needs`) these capabilities satisfy."""
        found: list[Capability] = []
        if self.has("tools"):
            found.append("tools")
        if self.has("json_schema") or self.has("json_object") or self.has("tools"):
            found.append("structured_output")
        if self.has("vision"):
            found.append("vision")
        if self.has("embeddings"):
            found.append("embeddings")
        if self.has("audio"):
            found.append("audio_in")
        return found

    def output_mode(self) -> OutputMode:
        """The structured-output mode for this model, in order of reliability: a schema the server enforces,
        then a tool call, then a schema in the prompt (validated, and retried once by the use)."""
        if self.has("json_schema"):
            return "native"
        if self.has("tools"):
            return "tool"
        return "prompted"


def clean_overrides(value: Any) -> dict[str, bool]:
    """Overrides as stored: only known capabilities with boolean values."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in SERVER_CAPABILITIES and isinstance(v, bool)}


# Which server capabilities satisfy a slot need, in words for an error message.
NEED_WORDS: dict[str, str] = {
    "tools": "tool calling",
    "structured_output": "structured output (a JSON schema, JSON mode or tool calling)",
    "vision": "image input",
    "embeddings": "embeddings",
    "audio_in": "audio input (speech to text)",
    "audio_out": "audio output (text to speech)",
}

# What the user can do on the server for each need, for the error's action.
NEED_FIXES: dict[str, str] = {
    "tools": "Start the server with tool calling on (vLLM: --enable-auto-tool-choice --tool-call-parser "
    "<family>; llama.cpp: --jinja) and probe the model again, or choose another model in Settings → AI "
    "models",
    "structured_output": "Start the server with JSON schema output on and probe the model again, or choose "
    "another model in Settings → AI models",
}
