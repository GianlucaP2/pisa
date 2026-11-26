"""A fake OpenAI-compatible model server (an in-process ASGI app) with switchable quirks.

Each preset mimics one server as it behaves with the flags its docs describe: vLLM with and without a tool
parser, Ollama (tools, no `tool_choice`), a server with JSON mode only, llama.cpp's server with and without
`--jinja`. The answers are canned: the server never thinks, it only shapes its replies the way the real one
does, so the probe and the model profile can be tested against each shape.

The quirks measured on llama.cpp b12679 and Ollama 0.40.18 (LOCAL-1 L0, `scripts/local-models/findings.md`)
are here too: SSE streaming (tool-call deltas in pieces or whole, the usage chunk, `[DONE]`, `:` keepalives),
`tool_choice` "none" ignored (Ollama calls the tool) or misread (llama.cpp writes `<tool_call>` as text) and
"required" ignored, thinking as `reasoning_content`, `reasoning` or `<think>` in the content (and thinking
that spends the whole output limit), llama.cpp's 200 for an unknown model and its context overflow error,
Ollama's errors and its silent cut of a long prompt, each server's id formats, llama.cpp's /props and
Ollama's /api/ps and /api/show. `replay` serves recorded streams (`tests/data/compat_captures`) as they came.
"""

import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

TEST_KEY = "sk-fake-server-key-0000"  # an obviously fake key
CAPTURES = Path(__file__).with_name("data") / "compat_captures"


def capture(name: str) -> list[dict[str, Any]]:
    """One recorded check (`<framework>-<model>/<check>`): its exchanges, as `matrix.py --captures` wrote."""
    return json.loads((CAPTURES / f"{name}.json").read_text())


@dataclass
class Quirks:
    models: tuple[str, ...] = ("test-model",)
    tools: bool = True  # tool calling with tool_choice "auto"
    tool_choice: bool = True  # tool_choice "required" or a named tool is honoured
    tool_choice_rejected: bool = False  # tool_choice other than "auto" is refused (HTTP 400), not ignored
    tools_as_text: bool = False  # the call is written in the content (vLLM without a tool parser)
    json_schema: bool = True
    json_object: bool = True
    vision: bool = False
    audio: bool = False
    embeddings: bool = False
    llamacpp: bool = False  # llama.cpp's server: its model list, its error shapes, /props
    ollama: bool = False  # Ollama: its error shapes, /api/ps and /api/show, its ids
    api_key: str | None = None  # a key the server requires
    # tool_choice "none": honoured, the call made anyway (Ollama), or written as text (llama.cpp with Jinja)
    none_choice: Literal["honour", "call", "text"] = "honour"
    # where a thinking model's thoughts come: a field of the message, or `<think>` in the content
    reasoning: Literal["reasoning_content", "reasoning", "think"] | None = None
    think_tokens: int = 0  # the thinking's length: an output limit below it is spent before any answer
    lead_blank: bool = False  # the answer starts with "\n\n" (llama.cpp --reasoning-format none)
    ctx: int | None = None  # the context: a longer prompt is refused (llama.cpp) or cut silently (Ollama)
    unknown_model_ok: bool = False  # any model name is answered (llama.cpp serves its one model)
    keepalive: bool = False  # `:` comment lines in a stream before its first chunk (llama.cpp reading)


PRESETS: dict[str, Quirks] = {
    # vLLM with --enable-auto-tool-choice --tool-call-parser <family>
    "vllm": Quirks(),
    # vLLM without --enable-auto-tool-choice: "auto" tool choice is refused
    "vllm-no-tools": Quirks(tools=False, tool_choice=False),
    # a server that takes tools but leaves the model's call as text (no tool parser)
    "tools-as-text": Quirks(tools_as_text=True, tool_choice=False),
    # Ollama: tools, but tool_choice ignored
    "ollama": Quirks(tool_choice=False),
    # JSON mode only: no tools, no json_schema
    "json-object-only": Quirks(tools=False, tool_choice=False, json_schema=False),
    # llama.cpp's server with --jinja (its default since b6000-odd): "required" ignored, "none" misread
    "llamacpp-jinja": Quirks(
        llamacpp=True, tool_choice=False, none_choice="text", unknown_model_ok=True, keepalive=True,
        ctx=32768, models=("qwen2.5-7b-instruct-q4_k_m.gguf",),
    ),
    # llama.cpp's server without --jinja: tools are refused
    "llamacpp": Quirks(
        llamacpp=True, tools=False, tool_choice=False, unknown_model_ok=True, ctx=32768,
        models=("model.gguf",),
    ),
    # a thinking model on llama.cpp: `reasoning_content`, with --reasoning-format none: `<think>` in the text
    "llamacpp-thinking": Quirks(
        llamacpp=True, tool_choice=False, none_choice="text", unknown_model_ok=True, ctx=32768,
        reasoning="reasoning_content", think_tokens=300, models=("thinker.gguf",),
    ),
    "llamacpp-reasoning-none": Quirks(
        llamacpp=True, tool_choice=False, none_choice="text", unknown_model_ok=True, ctx=32768,
        reasoning="think", lead_blank=True, models=("thinker.gguf",),
    ),
    # Ollama as measured: "none" ignored, its own errors, a 4096-token context cut silently
    "ollama-real": Quirks(
        tool_choice=False, none_choice="call", ollama=True, ctx=4096, models=("small:1.5b", "embedder"),
    ),
    "ollama-thinking": Quirks(
        tool_choice=False, none_choice="call", ollama=True, ctx=4096, reasoning="reasoning",
        think_tokens=300, models=("thinker:0.6b",),
    ),
}  # fmt: skip


def sample(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> Any:
    """A value that fits a simple JSON schema (what a server that enforces it would return)."""
    defs = defs if defs is not None else schema.get("$defs", {})
    assert defs is not None
    if "$ref" in schema:
        return sample(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return sample(schema[key][0], defs)
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: sample(v, defs) for k, v in props.items() if k in schema.get("required", props)}
    if kind == "integer":
        return 5
    if kind == "number":
        return 1.5
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "null":
        return None
    if "enum" in schema:
        return schema["enum"][0]
    return "five"


@dataclass
class FakeServer:
    quirks: Quirks = field(default_factory=Quirks)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # A test may answer a chat request itself: the message to return, or None for the canned one.
    reply: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    # Recorded streams to serve, in order, to the stream requests (each a capture's `sse` list)
    replay: list[list[Any]] = field(default_factory=list)

    @classmethod
    def preset(cls, name: str, **changes: Any) -> "FakeServer":
        return cls(replace(PRESETS[name], **changes))

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/models", self.models, methods=["GET"]),
                Route("/v1/chat/completions", self.chat, methods=["POST"]),
                Route("/v1/embeddings", self.embed, methods=["POST"]),
                Route("/v1/audio/transcriptions", self.transcribe, methods=["POST"]),
                Route("/props", self.props, methods=["GET"]),
                Route("/api/ps", self.ps, methods=["GET"]),
                Route("/api/show", self.show, methods=["POST"]),
            ]
        )

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app())

    def handler(self) -> Callable[[httpx.Request], Any]:
        """For the test client's network handler: requests to this server's host go to the app."""
        transport = self.transport()

        async def handle(request: httpx.Request) -> httpx.Response:
            return await transport.handle_async_request(request)

        return handle

    # --- helpers ---

    def _error(self, status: int, message: str, kind: str = "server_error") -> JSONResponse:
        if self.quirks.llamacpp:
            return JSONResponse(
                {"error": {"code": status, "message": message, "type": kind}}, status_code=status
            )
        if self.quirks.ollama:
            kind = "not_found_error" if status == 404 else "invalid_request_error"
            return JSONResponse(
                {"error": {"message": message, "type": kind, "param": None, "code": None}}, status_code=status
            )
        return JSONResponse(
            {"object": "error", "message": message, "type": "BadRequestError", "code": status},
            status_code=status,
        )

    def _refused(self, request: Request) -> JSONResponse | None:
        if self.quirks.api_key and request.headers.get("authorization") != f"Bearer {self.quirks.api_key}":
            return self._error(401, "Unauthorized")
        return None

    # --- routes ---

    async def models(self, request: Request) -> JSONResponse:
        if refused := self._refused(request):
            return refused
        if self.quirks.llamacpp:
            data = [
                {"id": m, "object": "model", "owned_by": "llamacpp", "meta": {"n_ctx_train": 32768}}
                for m in self.quirks.models
            ]
            return JSONResponse(
                {
                    "object": "list",
                    "data": data,
                    "models": [{"name": m, "model": m} for m in self.quirks.models],
                }
            )
        data = [
            {"id": m, "object": "model", "owned_by": "vllm", "max_model_len": 8192}
            for m in self.quirks.models
        ]
        return JSONResponse({"object": "list", "data": data})

    async def chat(self, request: Request) -> Response:
        if refused := self._refused(request):
            return refused
        body = await request.json()
        self.requests.append(("chat", body))
        q = self.quirks
        model = body.get("model")
        if model not in q.models and not q.unknown_model_ok:
            if q.ollama:
                return self._error(404, f"model '{model}' not found")
            return self._error(404, f"The model `{model}` does not exist.")
        messages = body.get("messages", [])
        prompt_tokens = _tokens(messages, body.get("tools"))
        if q.ctx is not None and prompt_tokens > q.ctx and q.llamacpp:
            return JSONResponse(
                {"error": {"code": 400, "type": "exceed_context_size_error",
                           "n_prompt_tokens": prompt_tokens, "n_ctx": q.ctx,
                           "message": f"request ({prompt_tokens} tokens) exceeds the available "
                           f"context size ({q.ctx} tokens), try increasing it"}},
                status_code=400,
            )  # fmt: skip
        if q.ctx is not None and prompt_tokens > q.ctx:  # Ollama: cut, and answered as if read
            prompt_tokens = q.ctx // 2 + 2
        has_image = any(
            isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
            for m in messages
        )
        if has_image and not q.vision:
            return self._error(400, "This model does not support image input.")
        tools = body.get("tools")
        choice = body.get("tool_choice", "auto")
        if tools:
            if not q.tools:
                if q.llamacpp:
                    return self._error(500, "tools param requires --jinja flag")
                return self._error(
                    400,
                    '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set',
                )
            if choice not in ("auto", "none") and q.tool_choice_rejected:
                return self._error(400, "tool_choice is not supported")
        response_format = body.get("response_format") or {}
        kind = response_format.get("type")
        if kind == "json_schema" and not q.json_schema:
            return self._error(400, "response_format json_schema is not supported")
        if kind == "json_object" and not q.json_object:
            return self._error(400, "response_format json_object is not supported")

        if body.get("stream") and self.replay:
            return self._replayed(self.replay.pop(0))
        message: dict[str, Any] | None = self.reply(body) if self.reply else None
        if message is None:
            message = self._canned(messages, tools, choice, response_format, has_image)
        message, finish = self._thought(message, body)
        if finish is None:
            finish = "tool_calls" if message.get("tool_calls") else "stop"
        if q.ollama and message.get("tool_calls"):
            message = {**message, "content": ""}  # Ollama: an empty text beside its calls
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 7, "total_tokens": prompt_tokens + 7}
        if body.get("stream"):
            return self._streamed(model, message, finish, usage, body)
        return JSONResponse(
            {
                "id": self._response_id(),
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}
                ],
                "usage": usage,
            }
        )

    # --- thinking, ids, streams ---

    def _thinks(self, body: dict[str, Any]) -> bool:
        """A thinking model thinks unless the request turned it off the way its server reads it."""
        if self.quirks.reasoning is None:
            return False
        kwargs = body.get("chat_template_kwargs") or {}
        if self.quirks.llamacpp and kwargs.get("enable_thinking") is False:
            return False
        return not (
            self.quirks.ollama and (body.get("reasoning_effort") == "none" or body.get("think") is False)
        )

    def _thought(self, message: dict[str, Any], body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """The message with its thinking where the server puts it; ("length") when the output limit was spent
        thinking."""
        q = self.quirks
        if not self._thinks(body):
            return message, None
        thoughts = "Let me think about this. " * max(1, q.think_tokens // 6)
        if q.think_tokens and int(body.get("max_tokens") or 10**6) < q.think_tokens:
            cut = thoughts[: int(body["max_tokens"]) * 4]
            if q.reasoning == "think":
                return {"content": f"<think>\n{cut}"}, "length"
            assert q.reasoning is not None
            return {"content": "" if q.ollama else None, q.reasoning: cut}, "length"
        if q.reasoning == "think":
            lead = "\n\n" if q.lead_blank else ""
            text = message.get("content") or ""
            return {**message, "content": f"<think>\n{thoughts}\n</think>{lead}{text}"}, None
        assert q.reasoning is not None
        return {**message, q.reasoning: thoughts}, None

    def _response_id(self) -> str:
        if self.quirks.llamacpp:
            return "chatcmpl-" + "A1b2C3d4" * 4  # 32 characters
        if self.quirks.ollama:
            return "chatcmpl-385"
        return "chatcmpl-fake"

    def _call_id(self, n: int) -> str:
        if self.quirks.llamacpp:
            return f"Y7HzVnHnmKqokLCt7hQY3NpPWCaixIR{n}"  # 32 characters
        if self.quirks.ollama:
            return f"call_5fg7ulw{n}"
        return f"call_{n + 1}"

    def _streamed(
        self, model: str, message: dict[str, Any], finish: str, usage: dict[str, Any], body: dict[str, Any]
    ) -> StreamingResponse:
        """The answer as SSE: text and thinking in small pieces; calls in pieces (llama.cpp, vLLM) or whole
        (Ollama); the finish chunk; the usage chunk when asked; `[DONE]`."""
        q = self.quirks
        rid = self._response_id()

        def chunk(delta: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
            return {"id": rid, "object": "chat.completion.chunk", "created": 0, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}  # fmt: skip

        events: list[Any] = [chunk({"role": "assistant", "content": "" if q.ollama else None})]
        for name in ("reasoning_content", "reasoning"):
            for piece in _pieces(message.get(name) or ""):
                events.append(chunk({name: piece, **({"content": ""} if q.ollama else {})}))
        for piece in _text_pieces(message.get("content") or ""):
            events.append(chunk({"content": piece}))
        for n, call in enumerate(message.get("tool_calls") or []):
            fn = call["function"]
            if q.ollama:
                args = json.dumps(json.loads(fn["arguments"]), separators=(",", ":"))
                call = {"id": self._call_id(n), "index": n, "type": "function",
                        "function": {"name": fn["name"], "arguments": args}}  # fmt: skip
                events.append(chunk({"content": "", "tool_calls": [call]}))
                continue
            pieces = _pieces(fn["arguments"], 4) or [""]
            events.append(chunk({"tool_calls": [{"index": n, "id": self._call_id(n), "type": "function",
                                 "function": {"name": fn["name"], "arguments": pieces[0]}}]}))  # fmt: skip
            for piece in pieces[1:]:
                events.append(chunk({"tool_calls": [{"index": n, "function": {"arguments": piece}}]}))
        events.append(chunk({}, finish))
        if (body.get("stream_options") or {}).get("include_usage"):
            events.append({"id": rid, "object": "chat.completion.chunk", "created": 0, "model": model,
                           "choices": [], "usage": usage})  # fmt: skip
        events.append("[DONE]")
        return self._sse(events)

    def _replayed(self, events: list[Any]) -> StreamingResponse:
        return self._sse(events)

    def _sse(self, events: list[Any]) -> StreamingResponse:
        keepalive = self.quirks.keepalive

        async def lines() -> AsyncIterator[bytes]:
            if keepalive:
                yield b": keepalive\n\n"
            for event in events:
                data = event if isinstance(event, str) else json.dumps(event)
                yield f"data: {data}\n\n".encode()

        return StreamingResponse(lines(), media_type="text/event-stream")

    def _canned(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        choice: Any,
        response_format: dict[str, Any],
        has_image: bool,
    ) -> dict[str, Any]:
        q = self.quirks
        last: Any = (messages[-1].get("content") if messages else "") or ""
        words = (
            last
            if isinstance(last, str)
            else " ".join(p.get("text", "") for p in last if isinstance(p, dict))
        )
        if tools:
            forced = choice not in ("auto", "none") and q.tool_choice
            named = next((t for t in tools if t["function"]["name"] in words), None)
            if isinstance(choice, dict) and q.tool_choice:
                named = next(t for t in tools if t["function"]["name"] == choice["function"]["name"])
            target = named or (tools[0] if forced else None)
            if target is not None and choice != "none":
                name = target["function"]["name"]
                args = (
                    {"a": 2, "b": 3} if name == "pisa_probe_add" else sample(target["function"]["parameters"])
                )
                if q.tools_as_text and not forced:
                    text = json.dumps({"name": name, "arguments": args})
                    return {"content": f"<tool_call>{text}</tool_call>"}
                return {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": self._call_id(0),
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            if named is not None and choice == "none" and q.none_choice != "honour":
                name = named["function"]["name"]
                args = sample(named["function"]["parameters"])
                if q.none_choice == "text":  # llama.cpp: the template's call, as text
                    text = json.dumps({"name": name, "arguments": args})
                    return {"content": f"<tool_call>\n{text}\n</tool_call>"}
                return {  # Ollama: the call, "none" or not
                    "content": None,
                    "tool_calls": [
                        {"id": self._call_id(0), "type": "function",
                         "function": {"name": name, "arguments": json.dumps(args)}}
                    ],
                }  # fmt: skip
        kind = response_format.get("type")
        if kind == "json_schema":
            return {"content": json.dumps(sample(response_format["json_schema"]["schema"]))}
        if kind == "json_object":
            return {"content": json.dumps({"sky": "blue"})}
        if has_image:
            return {"content": "Red."}
        if "2 plus 3" in words:
            return {"content": "2 plus 3 is 5."}
        if "JSON" in words:
            return {"content": '```json\n{"sky": "blue"}\n```'}  # a model left to itself fences its JSON
        return {"content": "Hello!"}

    async def props(self, request: Request) -> Response:
        if not self.quirks.llamacpp:
            return Response(status_code=404)
        self.requests.append(("props", {}))
        return JSONResponse({"default_generation_settings": {"n_ctx": self.quirks.ctx or 4096},
                             "total_slots": 1, "model_path": self.quirks.models[0]})  # fmt: skip

    async def ps(self, request: Request) -> Response:
        if not self.quirks.ollama:
            return Response(status_code=404)
        self.requests.append(("ps", {}))
        loaded = [
            {"name": m, "model": m, "context_length": self.quirks.ctx or 4096} for m in self.quirks.models
        ]
        return JSONResponse({"models": loaded})

    async def show(self, request: Request) -> Response:
        if not self.quirks.ollama:
            return Response(status_code=404)
        self.requests.append(("show", await request.json()))
        return JSONResponse({"parameters": "temperature 0.7", "model_info": {"general.architecture": "x"}})

    async def embed(self, request: Request) -> JSONResponse:
        if refused := self._refused(request):
            return refused
        body = await request.json()
        self.requests.append(("embeddings", body))
        if not self.quirks.embeddings:
            if self.quirks.ollama:
                return self._error(
                    501, "This server does not support embeddings. Start it with `--embeddings`"
                )
            if self.quirks.llamacpp:
                return self._error(
                    501, "This server does not support embeddings. Start it with `--embeddings`"
                )
            return self._error(404, "Not Found")
        inputs = body.get("input")
        texts = inputs if isinstance(inputs, list) else [inputs]
        vector = [0.01 * i for i in range(16)]
        data: list[dict[str, Any]] = []
        for n, text in enumerate(texts):
            values = [v + 0.001 * (len(str(text)) % 7) for v in vector]
            if body.get("encoding_format") == "base64":  # the OpenAI SDK's default
                import base64
                import struct

                encoded: Any = base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode()
            else:
                encoded = values
            data.append({"object": "embedding", "index": n, "embedding": encoded})
        return JSONResponse(
            {
                "object": "list",
                "data": data,
                "model": body.get("model"),
                "usage": {"prompt_tokens": 2 * len(texts), "total_tokens": 2 * len(texts)},
            }
        )

    async def transcribe(self, request: Request) -> JSONResponse:
        if refused := self._refused(request):
            return refused
        raw = await request.body()  # multipart: the model field and the file, read without a form parser
        found = re.search(rb'name="model"\r\n\r\n([^\r]*)', raw)
        self.requests.append(("transcriptions", {"model": found.group(1).decode() if found else None}))
        if b'filename="' not in raw:
            return self._error(400, "No audio file")
        if not self.quirks.audio:
            return self._error(404, "Not Found")
        return JSONResponse({"text": ""})


def _tokens(messages: list[dict[str, Any]], tools: Any) -> int:
    """A prompt's tokens as a server would count them, roughly: four characters each, images aside."""

    def chars(value: Any) -> int:
        if isinstance(value, str):
            return 0 if value.startswith("data:") else len(value)
        if isinstance(value, dict):
            return sum(chars(v) for v in value.values())
        if isinstance(value, list):
            return sum(chars(v) for v in value)
        return 0

    return 10 + (chars(messages) + chars(tools or [])) // 4


def _pieces(text: str, size: int = 8) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _text_pieces(text: str) -> list[str]:
    """Streamed text as a server sends it: small pieces, a thinking tag always whole (one token)."""
    out: list[str] = []
    for part in re.split(r"(</?think>)", text):
        out.extend([part] if part in ("<think>", "</think>") else _pieces(part, 6))
    return out
