"""What a model on an OpenAI-compatible server can do: the probe's vocabulary, overrides and output modes.

A self-hosted server (vLLM, SGLang, llama.cpp, Ollama, LM Studio, a LiteLLM proxy) says little about its
models, so Pisa probes each one with tiny real requests. The user can override any result by hand. The
effective value of a capability is the probe's result with the user's overrides applied; a model never
probed has every capability false. Nothing is assumed.
"""

import re
from collections.abc import Mapping
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

# An own server's read timeout (seconds): how long a request may go without a byte from it. A CPU reads a long
# prompt for minutes before its first token; llama.cpp sends a keepalive every 30 s while it reads, Ollama
# sends nothing, so the default is above 30 s with room, and the operator raises it for long prompts.
DEFAULT_READ_TIMEOUT_S = 60
MIN_READ_TIMEOUT_S = 10
MAX_READ_TIMEOUT_S = 900

# The slots that think by default on an own server unless the operator turns it off: all but the fast ones,
# whose answers are short and whose wait is the user's (thinking there spends seconds and the output limit).
THINKING_OFF_SLOTS = frozenset({"conversation.reflex", "intake.triage", "understand.confirm"})

# What the probe learns beside the capabilities (`ModelCaps.facts`): the context the server serves the model
# with, whether the model thinks, and which server it is ("llama.cpp", "ollama"; absent: another one).
FACT_KEYS = ("context_tokens", "thinking", "server")
SERVER_KINDS = ("llama.cpp", "ollama")


def thinking_on(slot: str, overrides: Mapping[str, bool]) -> bool:
    """Whether a slot's calls on an own server may think: the operator's choice, else the slot's default."""
    if slot in overrides:
        return bool(overrides[slot])
    return slot not in THINKING_OFF_SLOTS


def clean_facts(value: Any) -> dict[str, Any]:
    """The probe's facts as stored: only known keys with values of their type."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    ctx = value.get("context_tokens")
    if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
        out["context_tokens"] = ctx
    if isinstance(value.get("thinking"), bool):
        out["thinking"] = value["thinking"]
    if value.get("server") in SERVER_KINDS:
        out["server"] = value["server"]
    return out


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
    facts: dict[str, Any] = field(default_factory=dict)  # `clean_facts`: context, thinking, server kind
    # the last time the server read far fewer prompt tokens than Pisa sent (a silent cut), None: never seen
    truncated: dict[str, Any] | None = None

    @property
    def thinks(self) -> bool:
        return bool(self.facts.get("thinking"))

    @property
    def context_tokens(self) -> int | None:
        return self.facts.get("context_tokens")

    @property
    def server(self) -> str | None:
        return self.facts.get("server")

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
