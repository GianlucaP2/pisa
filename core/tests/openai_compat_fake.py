"""A fake OpenAI-compatible model server (an in-process ASGI app) with switchable quirks.

Each preset mimics one server as it behaves with the flags its docs describe: vLLM with and without a tool
parser, Ollama (tools, no `tool_choice`), a server with JSON mode only, llama.cpp's server with and without
`--jinja`. The answers are canned: the server never thinks, it only shapes its replies the way the real one
does, so the probe and the model profile can be tested against each shape.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

TEST_KEY = "sk-fake-server-key-0000"  # an obviously fake key


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
    llamacpp: bool = False  # llama.cpp's server: its model list, its error shapes
    api_key: str | None = None  # a key the server requires


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
    # llama.cpp's server with --jinja
    "llamacpp-jinja": Quirks(llamacpp=True, models=("qwen2.5-7b-instruct-q4_k_m.gguf",)),
    # llama.cpp's server without --jinja: tools are refused
    "llamacpp": Quirks(llamacpp=True, tools=False, tool_choice=False, models=("model.gguf",)),
}


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

    def _error(self, status: int, message: str) -> JSONResponse:
        if self.quirks.llamacpp:
            return JSONResponse(
                {"error": {"code": status, "message": message, "type": "server_error"}}, status_code=status
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

    async def chat(self, request: Request) -> JSONResponse:
        if refused := self._refused(request):
            return refused
        body = await request.json()
        self.requests.append(("chat", body))
        q = self.quirks
        model = body.get("model")
        if model not in q.models:
            return self._error(404, f"The model `{model}` does not exist.")
        messages = body.get("messages", [])
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

        message: dict[str, Any] | None = self.reply(body) if self.reply else None
        if message is None:
            message = self._canned(messages, tools, choice, response_format, has_image)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return JSONResponse(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            }
        )

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
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
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

    async def embed(self, request: Request) -> JSONResponse:
        if refused := self._refused(request):
            return refused
        body = await request.json()
        self.requests.append(("embeddings", body))
        if not self.quirks.embeddings:
            if self.quirks.llamacpp:
                return self._error(
                    501, "This server does not support embeddings. Start it with `--embeddings`"
                )
            return self._error(404, "Not Found")
        return JSONResponse(
            {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.01 * i for i in range(16)]}],
                "model": body.get("model"),
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
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
