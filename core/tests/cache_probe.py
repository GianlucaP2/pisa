"""The prompt cache, measured without a provider.

`CacheProbe` wraps the tests' scripted model (a FunctionModel) and shows Pisa's agent an Anthropic model: the
agent prepares every request exactly as for `anthropic:claude-haiku-5-5` (the replay's model), the probe
renders that request as Anthropic's own wire body (Pydantic AI's AnthropicModel against a transport that
answers locally) and plays it against a simulated prompt cache, then the scripted model answers. The round's
usage carries the simulated `cache_read_tokens` / `cache_write_tokens`, so Pisa's own measures (the turn's
parts, the replay's summary) report them as they would on the server.

The simulated cache follows Anthropic's published rules:
- the cache key is the prefix in the order tools → system → messages; a tool with `defer_loading` is not
  part of it (its schema is withheld until a `tool_reference` reveals it);
- a request writes the prefix at each `cache_control` breakpoint (and at the automatic one, top-level
  `cache_control`, on the last block);
- a request reads the longest prefix written before that ends at a block boundary at most 20 blocks before
  one of its breakpoints;
- tokens are estimated as the block's JSON characters / 4 (a ratio, so the estimate cancels out in the
  share).

`Binding` plays Anthropic's thinking-block binding too. Each probed response carries a thinking
block whose signature names the request that produced it (its tools, system and messages); a later request
replaying that block under a different prefix is answered as Anthropic answers it (HTTP 400, "The block is
bound to a different conversation"), so Pydantic AI's own recovery runs and warns
(`AnthropicStaleThinkingBlockWarning`), as on the server.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import httpx2 as httpx
from pydantic_ai.messages import ModelMessage, ModelResponse, ThinkingPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

LOOKBACK = 20
WIRE_MODEL = "claude-haiku-5-5"


@dataclass
class Round:
    tools: list[str]  # the cache-keyed tools, in wire order
    deferred: list[str]
    system: str
    total: int
    read: int
    written: int
    body: dict[str, Any] = field(default_factory=dict, repr=False)
    keyed: list[str] = field(default_factory=list, repr=False)  # the cache-keyed tools' exact bytes
    model: int = 0  # the probed model that made it (Pisa builds its brain's model once a turn)


@dataclass
class Binding:
    """Anthropic's thinking-block binding: each block is bound to the prefix (tools, system and the messages
    before it) of the request that produced it."""

    bound: dict[str, str] = field(default_factory=dict)  # signature -> the prefix's hash
    rejected: int = 0  # requests answered 400: a replayed block under another prefix
    causes: dict[str, int] = field(default_factory=dict)  # what changed first: tools, system, a message
    _bodies: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def sign(self, body: dict[str, Any]) -> str:
        """The signature of the thinking block answering this request (accepted)."""
        signature = f"sig-{len(self.bound)}"
        self.bound[signature] = _prefix(body, len(body.get("messages") or []))
        self._bodies[signature] = body
        return signature

    def cause(self, signature: str, body: dict[str, Any], upto: int) -> str:
        """What differs first between the block's own request and this one's prefix."""
        was = _uncached(self._bodies[signature])
        now = _uncached(body)
        if was.get("tools") != now.get("tools"):
            return "tools"
        if was.get("system") != now.get("system"):
            return "system"
        for i, (a, b) in enumerate(
            zip(was.get("messages") or [], (now.get("messages") or [])[:upto], strict=False)
        ):
            if a != b:
                kinds = sorted(
                    {c.get("type", "?") for c in (b["content"] if isinstance(b["content"], list) else [])}
                )
                return f"message ({b['role']}, {'/'.join(kinds)}), {upto - i} before the block"
        return "message count"

    def stale(self, body: dict[str, Any]) -> list[str]:
        """The replayed blocks whose prefix changed since they were made (none when the request asks the
        drop itself)."""
        thinking = body.get("thinking") or {}
        if (thinking.get("block_binding") or {}).get("prefix_mismatch_behavior") == "drop_block":
            return []
        out = []
        for i, message in enumerate(body.get("messages") or []):
            content = message.get("content")
            for block in content if message.get("role") == "assistant" and isinstance(content, list) else []:
                signature = block.get("signature") if block.get("type") == "thinking" else None
                if signature in self.bound and self.bound[signature] != _prefix(body, i):
                    out.append(signature)
                    why = self.cause(signature, body, i)
                    self.causes[why] = self.causes.get(why, 0) + 1
        return out


def _prefix(body: dict[str, Any], upto: int) -> str:
    """The bytes a thinking block is bound to: tools, system, and the messages before it (cache_control is
    the request's caching, not its content)."""
    keyed = {"tools": body.get("tools") or [], "system": body.get("system") or "",
             "messages": (body.get("messages") or [])[:upto]}  # fmt: skip
    return hashlib.sha256(json.dumps(_uncached(keyed), sort_keys=True).encode()).hexdigest()


def _uncached(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _uncached(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_uncached(v) for v in value]
    return value


STALE = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "messages: thinking block signature mismatch. The block is bound to a different "
        "conversation prefix.",
    },
}


@dataclass
class Cache:
    written: set[str] = field(default_factory=set)
    rounds: list[Round] = field(default_factory=list)
    binding: Binding = field(default_factory=Binding)
    models: int = 0  # the probed models built so far

    def play(self, body: dict[str, Any], model: int = 0) -> Round:
        blocks, breaks = _blocks(body)
        sizes = [len(b) // 4 for b in blocks]
        keys: list[str] = []
        h = hashlib.sha256()
        for b in blocks:
            h.update(b.encode())
            keys.append(h.hexdigest())
        read_at = -1
        for bp in breaks:
            for i in range(bp, max(-1, bp - LOOKBACK - 1), -1):
                if keys[i] in self.written:
                    read_at = max(read_at, i)
                    break
        read = sum(sizes[: read_at + 1])
        written = 0
        for bp in breaks:
            if keys[bp] not in self.written:
                written = max(written, sum(sizes[: bp + 1]) - read)
                self.written.add(keys[bp])
        tools = [t.get("name", "") for t in body.get("tools") or [] if t.get("defer_loading") is not True]
        deferred = [t.get("name", "") for t in body.get("tools") or [] if t.get("defer_loading") is True]
        system = body.get("system")
        text = system if isinstance(system, str) else "".join(s.get("text", "") for s in system or [])
        keyed = [b for b in blocks if b.startswith("tool:")]
        out = Round(tools, deferred, text, sum(sizes), read, written, body, keyed, model)
        self.rounds.append(out)
        return out


def _strip(block: Any) -> tuple[str, bool]:
    if isinstance(block, dict) and "cache_control" in block:
        return json.dumps({k: v for k, v in block.items() if k != "cache_control"}, sort_keys=True), True
    return json.dumps(block, sort_keys=True), False


def _blocks(body: dict[str, Any]) -> tuple[list[str], list[int]]:
    blocks: list[str] = []
    breaks: list[int] = []
    for tool in body.get("tools") or []:
        if tool.get("defer_loading") is True:
            continue  # withheld: outside the cache key
        text, bp = _strip(tool)
        blocks.append("tool:" + text)
        if bp:
            breaks.append(len(blocks) - 1)
    system = body.get("system")
    for part in [{"type": "text", "text": system}] if isinstance(system, str) else system or []:
        text, bp = _strip(part)
        blocks.append("system:" + text)
        if bp:
            breaks.append(len(blocks) - 1)
    for message in body.get("messages") or []:
        content = message.get("content")
        for part in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
            text, bp = _strip(part)
            blocks.append(f"{message.get('role')}:" + text)
            if bp:
                breaks.append(len(blocks) - 1)
    if body.get("cache_control") and blocks:
        breaks.append(len(blocks) - 1)
    return blocks, sorted(set(breaks))


_REPLY = {
    "id": "msg_probe",
    "type": "message",
    "role": "assistant",
    "model": WIRE_MODEL,
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def wire_model(seen: list[dict[str, Any]], binding: Binding | None = None) -> Model:
    """Pydantic AI's AnthropicModel for the replay's model, its requests answered locally (bodies kept); with
    `binding`, a replayed thinking block under another prefix is refused as Anthropic refuses it."""
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if binding is not None and binding.stale(body):
            binding.rejected += 1
            return httpx.Response(400, json=STALE)
        seen.append(body)
        return httpx.Response(200, json=_REPLY)

    client = AsyncAnthropic(
        api_key="sk-probe", http_client=httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    return AnthropicModel(WIRE_MODEL, provider=AnthropicProvider(anthropic_client=client))


class CacheProbe(WrapperModel):
    """The scripted model's answers, Anthropic's request preparation and a simulated prompt cache."""

    def __init__(self, scripted: Model, cache: Cache) -> None:
        super().__init__(scripted)
        self._seen: list[dict[str, Any]] = []
        self.wire = wire_model(self._seen, cache.binding)
        self.cache = cache
        cache.models += 1
        self.number = cache.models

    @property
    def profile(self) -> Any:  # type: ignore[override]
        return self.wire.profile

    @property
    def system(self) -> str:
        return self.wire.system

    @property
    def reveals_by_reference(self) -> bool:  # what Pisa's agent asks (`tool_search.by_reference`)
        return self.wire.tool_addition_mode == "by_reference"

    @property
    def model_name(self) -> str:
        return self.wire.model_name

    def customize_request_parameters(self, model_request_parameters: ModelRequestParameters) -> Any:
        return self.wire.customize_request_parameters(model_request_parameters)

    def prepare_request(self, model_settings: ModelSettings | None, model_request_parameters: Any) -> Any:
        return self.wire.prepare_request(model_settings, model_request_parameters)

    def prepare_messages(self, messages: list[ModelMessage], model_request_parameters: Any = None) -> Any:
        return self.wire.prepare_messages(messages, model_request_parameters)

    async def _probe(self, messages: list[ModelMessage], settings: Any, params: Any) -> tuple[Round, str]:
        await self.wire.request(messages, settings, params)
        body = self._seen.pop()
        return self.cache.play(body, self.number), self.cache.binding.sign(body)

    @staticmethod
    def _answer(response: ModelResponse, probed: tuple[Round, str]) -> ModelResponse:
        """The scripted answer as Anthropic gives it: its usage from the simulated cache, and a thinking block
        bound to the request (replayed in the turn's later requests)."""
        round_, signature = probed
        usage = replace(
            response.usage,
            input_tokens=round_.total,
            cache_read_tokens=round_.read,
            cache_write_tokens=round_.written,
        )
        thought = ThinkingPart(content="…", signature=signature, provider_name="anthropic")
        return replace(response, usage=usage, parts=[thought, *response.parts])

    async def request(
        self, messages: list[ModelMessage], model_settings: Any, model_request_parameters: Any
    ) -> ModelResponse:
        probed = await self._probe(messages, model_settings, model_request_parameters)
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        return self._answer(response, probed)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: Any,
        model_request_parameters: Any,
        run_context: Any = None,
    ) -> AsyncIterator[StreamedResponse]:
        probed = await self._probe(messages, model_settings, model_request_parameters)
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            patch = self._answer
            base = type(stream)

            class Probed(base):  # type: ignore[valid-type, misc]
                def get(self) -> ModelResponse:
                    return patch(base.get(self), probed)

            stream.__class__ = Probed
            yield stream


def probing(fake_models: Any, cache: Cache) -> Any:
    """The tests' model factory, every model wrapped in the probe."""

    def build(spec: Any, model_id: str, api_key: Any, ollama: Any) -> Any:
        return CacheProbe(fake_models(spec, model_id, api_key, ollama), cache)

    return build
