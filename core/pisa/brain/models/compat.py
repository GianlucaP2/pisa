"""OpenAI-compatible servers: the user's own model servers (vLLM, SGLang, llama.cpp, Ollama, LM Studio, a
LiteLLM proxy), each one a provider with its own id.

A server has a base URL (the one that ends in `/v1`), an optional API key (stored sealed in `provider_keys`,
like every provider's key) and an optional CA bundle for a certificate a private authority signed. Its models
come from `GET {base}/models`. What each model can do is probed with tiny real requests (`probe_model`) and
can be overridden by hand (`pisa.domain.model_caps`). Calls cost nothing in the ledger: the server is the
user's.

PISA_OLLAMA_BASE_URL still works: it describes one such server, with the id `ollama`, kept in the settings
instead of the database.
"""

import base64
import functools
import json
import ssl
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic_ai.models import Model

from pisa.brain.models.providers import PROVIDERS_BY_ID, ProviderCallError, ProviderSpec, list_models
from pisa.db.settings import delete_setting
from pisa.domain.errors import PisaError
from pisa.domain.model_caps import (
    OLLAMA_ID,
    SERVER_CAPABILITIES,
    SERVER_ID,
    ModelCaps,
    ServerCapability,
    clean_overrides,
)
from pisa.failures import redact
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger

_log = get_logger("pisa.models.compat")

# Local servers can be slow, on a CPU above all: one probe request may take a while to load the model.
PROBE_TIMEOUT_S = 120.0
PROBE_MAX_TOKENS = 256
# Price source of every model on such a server: the ledger counts its tokens at no cost.
LOCAL_PRICE_SOURCE = "local"


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    base_url: str
    ca_bundle: str | None
    source: Literal["settings", "user"]  # settings: PISA_OLLAMA_BASE_URL, so it can't be edited from the app

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            self.id,
            self.name,
            self.base_url,
            "",
            needs_key=False,
            kind="openai_compatible",
            ca_bundle=self.ca_bundle,
        )


def settings_server(ollama_base_url: str | None) -> Server | None:
    """The server PISA_OLLAMA_BASE_URL names (its root or its `/v1` address)."""
    if not ollama_base_url:
        return None
    root = ollama_base_url.rstrip("/")
    base = root if root.endswith("/v1") else root + "/v1"
    return Server(OLLAMA_ID, "Ollama", base, None, "settings")


_SERVER_COLUMNS = "id, name, base_url, ca_bundle"


def _server(row: Any) -> Server:
    return Server(row[0], row[1], row[2], row[3], "user")


async def list_servers(conn: AsyncConnection, ollama_base_url: str | None) -> list[Server]:
    rows = await (
        await conn.execute(f"SELECT {_SERVER_COLUMNS} FROM model_servers ORDER BY created_at, id")  # noqa: S608
    ).fetchall()
    servers = [_server(r) for r in rows]
    from_settings = settings_server(ollama_base_url)
    if from_settings is not None and all(s.id != from_settings.id for s in servers):
        servers.append(from_settings)
    return servers


async def get_server(conn: AsyncConnection, server_id: str, ollama_base_url: str | None) -> Server | None:
    row = await (
        await conn.execute(
            f"SELECT {_SERVER_COLUMNS} FROM model_servers WHERE id = %s",  # noqa: S608
            (server_id,),
        )
    ).fetchone()
    if row is not None:
        return _server(row)
    from_settings = settings_server(ollama_base_url)
    return from_settings if from_settings is not None and from_settings.id == server_id else None


async def resolve_provider(
    conn: AsyncConnection, provider_id: str, ollama_base_url: str | None
) -> ProviderSpec | None:
    """A built-in provider, or one of the user's OpenAI-compatible servers; None when there is no such id."""
    spec = PROVIDERS_BY_ID.get(provider_id)
    if spec is not None:
        return spec
    server = await get_server(conn, provider_id, ollama_base_url)
    return server.spec() if server is not None else None


async def server_exists(conn: AsyncConnection, provider_id: str) -> bool:
    """Whether a model reference may name this provider (the settings' Ollama server counts)."""
    if provider_id == OLLAMA_ID:
        return True
    row = await (await conn.execute("SELECT 1 FROM model_servers WHERE id = %s", (provider_id,))).fetchone()
    return row is not None


def check_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise PisaError(
            "validation_failed",
            f"{url!r} is not a server address Pisa can use: it needs http:// or https://, a host, and no "
            "query.",
            "Give the address that ends in /v1, e.g. http://192.168.1.20:8000/v1",
        )
    return url


def check_ca_bundle(path: str | None) -> str | None:
    """The CA bundle must be a PEM file on Pisa's server that loads as certificate authorities."""
    if path is None or not path.strip():
        return None
    path = path.strip()
    if not Path(path).is_absolute() or not Path(path).is_file():
        raise PisaError(
            "validation_failed",
            f"The CA bundle {path!r} is not a file on Pisa's server (it needs an absolute path).",
            "Copy the PEM file of the authority that signed the server's certificate to Pisa's server, "
            "and give its absolute path",
        )
    try:
        ssl.create_default_context(cafile=path)
    except (ssl.SSLError, OSError, ValueError):
        raise PisaError(
            "validation_failed",
            f"The CA bundle {path!r} holds no certificate Pisa can read (it needs PEM certificates).",
            "Give the PEM file of the authority that signed the server's certificate",
        ) from None
    return path


def check_server_id(server_id: str) -> str:
    if not SERVER_ID.match(server_id) or server_id in PROVIDERS_BY_ID:
        raise PisaError(
            "validation_failed",
            f"{server_id!r} can't be a server's id: it needs 2 to 32 lowercase letters, digits or '-', "
            "starting with a letter, and no built-in provider's name.",
            "Choose another id, e.g. 'home-gpu'",
        )
    return server_id


async def create_server(
    conn: AsyncConnection,
    server_id: str,
    name: str,
    base_url: str,
    ca_bundle: str | None,
    ollama_base_url: str | None,
) -> Server:
    check_server_id(server_id)
    if await get_server(conn, server_id, ollama_base_url) is not None:
        raise PisaError(
            "validation_failed",
            f"There is already a provider with the id {server_id!r}.",
            "Choose another id, or change the existing server",
        )
    row = await (
        await conn.execute(
            "INSERT INTO model_servers (id, name, base_url, ca_bundle) VALUES (%s, %s, %s, %s) "
            "RETURNING id, name, base_url, ca_bundle",
            (server_id, name.strip() or server_id, check_base_url(base_url), check_ca_bundle(ca_bundle)),
        )
    ).fetchone()
    assert row is not None
    return _server(row)


def _editable(server: Server | None, server_id: str) -> Server:
    if server is None:
        raise PisaError(
            "not_found",
            f"There is no OpenAI-compatible server {server_id!r}.",
            "Add it first: POST /v1/ai/providers",
        )
    if server.source == "settings":
        raise PisaError(
            "validation_failed",
            f"The server {server_id!r} is set by PISA_OLLAMA_BASE_URL on Pisa's server, so it can't be "
            "changed from the app.",
            "Change PISA_OLLAMA_BASE_URL and restart Pisa, or add another server",
        )
    return server


async def update_server(
    conn: AsyncConnection,
    server_id: str,
    ollama_base_url: str | None,
    *,
    name: str | None = None,
    base_url: str | None = None,
    ca_bundle: str | None = None,
    clear_ca_bundle: bool = False,
) -> Server:
    server = _editable(await get_server(conn, server_id, ollama_base_url), server_id)
    new = Server(
        server.id,
        (name.strip() or server.id) if name is not None else server.name,
        check_base_url(base_url) if base_url is not None else server.base_url,
        None
        if clear_ca_bundle
        else (check_ca_bundle(ca_bundle) if ca_bundle is not None else server.ca_bundle),
        "user",
    )
    await conn.execute(
        "UPDATE model_servers SET name = %s, base_url = %s, ca_bundle = %s, updated_at = now() WHERE id = %s",
        (new.name, new.base_url, new.ca_bundle, new.id),
    )
    if new.base_url != server.base_url:
        # Another address may be another server: what was probed there no longer holds.
        await conn.execute(
            "UPDATE model_capabilities SET probe = NULL, probed_at = NULL WHERE provider = %s", (new.id,)
        )
        await conn.execute(
            "UPDATE model_catalog SET capabilities = %s WHERE provider = %s",
            ([], new.id),
        )
    return new


async def delete_server(conn: AsyncConnection, server_id: str, ollama_base_url: str | None) -> None:
    """The server, its key, its probes and overrides, and its models in the catalog. A slot that used one of
    its models keeps the reference and fails with a clear error until another model is chosen."""
    _editable(await get_server(conn, server_id, ollama_base_url), server_id)
    async with conn.transaction():
        await conn.execute("DELETE FROM model_servers WHERE id = %s", (server_id,))
        await conn.execute("DELETE FROM model_capabilities WHERE provider = %s", (server_id,))
        await conn.execute("DELETE FROM model_catalog WHERE provider = %s", (server_id,))
        await conn.execute("DELETE FROM provider_keys WHERE provider = %s", (server_id,))
        await delete_setting(conn, f"provider_test:{server_id}")


# --- capabilities -------------------------------------------------------------------------------------------


async def get_caps(conn: AsyncConnection, provider: str, model_id: str) -> ModelCaps:
    row = await (
        await conn.execute(
            "SELECT probe, probed_at, overrides FROM model_capabilities "
            "WHERE provider = %s AND model_id = %s",
            (provider, model_id),
        )
    ).fetchone()
    if row is None:
        return ModelCaps()
    probe = {k: bool(v) for k, v in row[0].items() if k in SERVER_CAPABILITIES} if row[0] else None
    return ModelCaps(probe, row[1], clean_overrides(row[2]))


async def stored_caps(conn: AsyncConnection, provider: str) -> dict[str, ModelCaps]:
    rows = await (
        await conn.execute(
            "SELECT model_id, probe, probed_at, overrides FROM model_capabilities WHERE provider = %s",
            (provider,),
        )
    ).fetchall()
    return {
        r[0]: ModelCaps(
            {k: bool(v) for k, v in r[1].items() if k in SERVER_CAPABILITIES} if r[1] else None,
            r[2],
            clean_overrides(r[3]),
        )
        for r in rows
    }


async def save_probe(
    conn: AsyncConnection, provider: str, model_id: str, probe: dict[ServerCapability, bool], at: datetime
) -> ModelCaps:
    await conn.execute(
        "INSERT INTO model_capabilities (provider, model_id, probe, probed_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (provider, model_id) DO UPDATE SET probe = EXCLUDED.probe, "
        "probed_at = EXCLUDED.probed_at",
        (provider, model_id, Jsonb(probe), at),
    )
    caps = await get_caps(conn, provider, model_id)
    await sync_catalog(conn, provider, model_id, caps, at)
    return caps


async def save_overrides(
    conn: AsyncConnection, provider: str, model_id: str, overrides: dict[str, bool], at: datetime
) -> ModelCaps:
    await conn.execute(
        "INSERT INTO model_capabilities (provider, model_id, overrides) VALUES (%s, %s, %s) "
        "ON CONFLICT (provider, model_id) DO UPDATE SET overrides = EXCLUDED.overrides",
        (provider, model_id, Jsonb(clean_overrides(overrides))),
    )
    caps = await get_caps(conn, provider, model_id)
    await sync_catalog(conn, provider, model_id, caps, at)
    return caps


async def sync_catalog(
    conn: AsyncConnection, provider: str, model_id: str, caps: ModelCaps, at: datetime
) -> None:
    """The model's catalog row carries its effective capabilities (the slots check them), at no price."""
    await conn.execute(
        "INSERT INTO model_catalog (ref, provider, model_id, name, capabilities, input_usd_per_mtok, "
        "output_usd_per_mtok, price_source, refreshed_at) VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s) "
        "ON CONFLICT (ref) DO UPDATE SET capabilities = EXCLUDED.capabilities",
        (
            f"{provider}:{model_id}",
            provider,
            model_id,
            model_id,
            list(caps.capabilities()),
            LOCAL_PRICE_SOURCE,
            at,
        ),
    )


# --- HTTP ---------------------------------------------------------------------------------------------------

_TLS_CLIENTS: dict[str, httpx.AsyncClient] = {}


def tls_context(ca_bundle: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_bundle)


def tls_client(ca_bundle: str) -> httpx.AsyncClient:
    """A client that trusts this CA bundle: one per bundle, kept for the process's life."""
    client = _TLS_CLIENTS.get(ca_bundle)
    if client is None or client.is_closed:
        client = _TLS_CLIENTS[ca_bundle] = httpx.AsyncClient(verify=tls_context(ca_bundle))
    return client


def http_for(spec: ProviderSpec, shared: httpx.AsyncClient) -> httpx.AsyncClient:
    """The client for a server's own API: Pisa's shared one, or one that trusts the server's CA bundle."""
    return tls_client(spec.ca_bundle) if spec.ca_bundle else shared


# --- the probe ----------------------------------------------------------------------------------------------

_PROBE_TOOL = "pisa_probe_add"
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": _PROBE_TOOL,
            "description": "Add two integers and return their sum.",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        },
    }
]
_SCHEMA_KEY = "pisa_probe_sum"


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A solid-colour PNG, made here so the probe carries no file."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + bytes(rgb) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def _wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """Silence as 16-bit mono PCM WAV."""
    frames = b"\x00\x00" * int(seconds * rate)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(frames))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(frames))
        + frames
    )


RED_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_png(32, 32, (220, 20, 20))).decode()


@dataclass(frozen=True)
class ProbeResult:
    caps: dict[ServerCapability, bool]
    notes: dict[str, str]  # why a capability failed, in Pisa's words (never the server's text)


class _Fail(Exception):
    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _message(body: Any) -> dict[str, Any]:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise _Fail("the answer has no message") from None
    if not isinstance(message, dict):
        raise _Fail("the answer has no message")
    return cast(dict[str, Any], message)


def _probe_call(message: dict[str, Any]) -> dict[str, Any]:
    """The probe tool's call and its arguments, or _Fail: a call written as text in the content (a server
    without a tool parser) is not a tool call."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls or not isinstance(calls[0], dict):
        raise _Fail("no tool call came back")
    function = calls[0].get("function")
    if not isinstance(function, dict) or function.get("name") != _PROBE_TOOL:
        raise _Fail("the tool call names another tool")
    arguments = function.get("arguments")
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        raise _Fail("the tool call's arguments are not JSON") from None
    if not isinstance(parsed, dict):
        raise _Fail("the tool call's arguments are not an object")
    return cast(dict[str, Any], parsed)


def _json_content(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, str):
        raise _Fail("the answer has no text")
    try:
        parsed = json.loads(content.strip())
    except ValueError:
        raise _Fail("the answer is not plain JSON") from None
    if not isinstance(parsed, dict):
        raise _Fail("the answer is not a JSON object")
    return cast(dict[str, Any], parsed)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class _Prober:
    def __init__(self, spec: ProviderSpec, key: str | None, model_id: str, http: httpx.AsyncClient) -> None:
        self.base = spec.base_url.rstrip("/")
        self.headers = spec.auth_headers(key)
        self.key = key
        self.model_id = model_id
        self.http = http

    async def post(self, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.post(
                self.base + path, headers=self.headers, timeout=PROBE_TIMEOUT_S, **kwargs
            )
        except httpx.TimeoutException:
            raise _Fail(f"no answer within {PROBE_TIMEOUT_S:g} s") from None
        except httpx.HTTPError as exc:
            raise _Fail(f"the request failed ({exc.__class__.__name__})") from None
        if response.status_code >= 400:
            raise _Fail(f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise _Fail("the answer is not JSON") from None

    async def chat(self, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        body = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": PROBE_MAX_TOKENS,
            "temperature": 0,
            "stream": False,
            **extra,
        }
        return _message(await self.post("/chat/completions", json=body))

    async def tools(self) -> None:
        message = await self.chat(
            [{"role": "user", "content": f"Use the {_PROBE_TOOL} tool to add 2 and 3."}], tools=_TOOLS
        )
        args = _probe_call(message)
        if not (_is_int(args.get("a")) and _is_int(args.get("b")) and {args["a"], args["b"]} == {2, 3}):
            raise _Fail("the tool call's arguments are wrong")

    async def tool_choice(self) -> None:
        # Nothing in the words asks for the tool: only a server that honours tool_choice calls it.
        message = await self.chat(
            [{"role": "user", "content": "Say hello."}], tools=_TOOLS, tool_choice="required"
        )
        _probe_call(message)

    async def json_schema(self) -> None:
        # The key is named nowhere in the words: only a server that enforces the schema produces it.
        schema = {
            "type": "object",
            "properties": {_SCHEMA_KEY: {"type": "integer"}},
            "required": [_SCHEMA_KEY],
            "additionalProperties": False,
        }
        message = await self.chat(
            [{"role": "user", "content": "What is 2 plus 3?"}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "pisa_probe", "strict": True, "schema": schema},
            },
        )
        if not _is_int(_json_content(message).get(_SCHEMA_KEY)):
            raise _Fail("the answer doesn't follow the schema")

    async def json_object(self) -> None:
        message = await self.chat(
            [{"role": "user", "content": "Describe the colour of the sky in JSON."}],
            response_format={"type": "json_object"},
        )
        _json_content(message)

    async def vision(self) -> None:
        message = await self.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What colour is this image? Answer with one word."},
                        {"type": "image_url", "image_url": {"url": RED_PNG_DATA_URL}},
                    ],
                }
            ]
        )
        content = message.get("content")
        if not isinstance(content, str) or "red" not in content.lower():
            raise _Fail("the answer doesn't name the image's colour")

    async def audio(self) -> None:
        body = await self.post(
            "/audio/transcriptions",
            data={"model": self.model_id, "response_format": "json"},
            files={"file": ("pisa-probe.wav", _wav(), "audio/wav")},
        )
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise _Fail("the answer has no transcript")

    async def embeddings(self) -> None:
        body = await self.post("/embeddings", json={"model": self.model_id, "input": "pisa probe"})
        try:
            vector = body["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            raise _Fail("the answer has no embedding") from None
        if (
            not isinstance(vector, list)
            or len(vector) < 8
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vector)
        ):
            raise _Fail("the embedding is not a list of numbers")


async def probe_model(
    spec: ProviderSpec, key: str | None, model_id: str, http: httpx.AsyncClient
) -> ProbeResult:
    """One tiny real request per capability, each with a strict pass test; one request at a time, as a local
    server serves them. `tool_choice` is tried only when tool calling works."""
    prober = _Prober(spec, key, model_id, http)
    caps: dict[ServerCapability, bool] = dict.fromkeys(SERVER_CAPABILITIES, False)
    notes: dict[str, str] = {}
    for cap in SERVER_CAPABILITIES:
        if cap == "tool_choice" and not caps["tools"]:
            notes[cap] = "tool calling failed"
            continue
        try:
            await getattr(prober, cap)()
        except _Fail as fail:
            notes[cap] = redact(fail.why, key)
            continue
        caps[cap] = True
    _log.info("model_probed", provider=spec.id, model=model_id, caps=caps, notes=notes)
    return ProbeResult(caps, notes)


@dataclass(frozen=True)
class FirstProbe:
    """What `probe_unprobed` found: the model's capabilities now, and why the server could not be reached
    when it could not (the model then stays unprobed)."""

    caps: ModelCaps
    unreachable: str | None = None


async def probe_unprobed(
    pool: AsyncConnectionPool,
    keys: KeyStore,
    spec: ProviderSpec,
    model_id: str,
    http: httpx.AsyncClient,
    now: Callable[[], datetime],
) -> FirstProbe:
    """Probe a model that was never probed, once: when a slot is pointed at it, or when a use first misses a
    capability on it. A per-model advisory lock makes two callers that miss at once run one probe: the second
    waits and finds the first one's result. A model probed meanwhile (`probed_at` set) is not probed again:
    its result is a real answer. The lock is the transaction's, so a cancelled probe leaves none behind."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"pisa.model_probe:{spec.id}:{model_id}",)
        )
        caps = await get_caps(conn, spec.id, model_id)
        if caps.probed_at is not None:
            return FirstProbe(caps)
        key = await keys.get(conn, spec.id)
        client = http_for(spec, http)
        try:
            await list_models(spec, key, client)  # reachable, and the key accepted, before seven requests
        except ProviderCallError as exc:
            _log.warning("model_probe_unreachable", provider=spec.id, model=model_id, cause=exc.message)
            return FirstProbe(caps, exc.message)
        result = await probe_model(spec, key, model_id, client)
        return FirstProbe(await save_probe(conn, spec.id, model_id, result.caps, now()))


# --- the model ----------------------------------------------------------------------------------------------


def compat_profile(caps: ModelCaps) -> Any:
    """The Pydantic AI profile from the effective capabilities, and nothing assumed beyond them."""
    from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer, OpenAIModelProfile

    return OpenAIModelProfile(
        json_schema_transformer=OpenAIJsonSchemaTransformer,
        supports_tools=caps.has("tools"),
        supports_json_schema_output=caps.has("json_schema"),
        supports_json_object_output=caps.has("json_object"),
        supports_forced_tool_choice=caps.has("tool_choice"),
        default_structured_output_mode=caps.output_mode(),
        # Servers differ in strict tool schemas; `max_tokens` is the field every one of them reads.
        openai_supports_strict_tool_definition=False,
        openai_chat_supports_max_completion_tokens=False,
    )


_SDK_TLS_CLIENTS: dict[str, Any] = {}


def _sdk_tls_client(ca_bundle: str) -> Any:
    """The OpenAI SDK's own HTTP client, trusting this CA bundle: one per bundle, for the process's life."""
    from openai import DefaultAsyncHttpxClient

    client = _SDK_TLS_CLIENTS.get(ca_bundle)
    if client is None or client.is_closed:
        client = _SDK_TLS_CLIENTS[ca_bundle] = DefaultAsyncHttpxClient(verify=tls_context(ca_bundle))
    return client


@functools.cache
def _compat_model_class() -> type[Any]:
    from pydantic_ai.models.openai import OpenAIChatModel

    class CompatChatModel(OpenAIChatModel):
        """An OpenAI chat model on the user's own server, carrying the capabilities it was built from."""

        caps: ModelCaps

    return CompatChatModel


def build_compat_model(
    spec: ProviderSpec,
    model_id: str,
    api_key: str | None,
    caps: ModelCaps,
    http_client: Any = None,
) -> Model:
    """`http_client` is for tests (a client on a fake transport); otherwise the SDK's own client, or one that
    trusts the server's CA bundle."""
    from openai import AsyncOpenAI
    from pydantic_ai.profiles import DEFAULT_PROFILE, merge_profile
    from pydantic_ai.providers.openai import OpenAIProvider

    if http_client is None and spec.ca_bundle:
        http_client = _sdk_tls_client(spec.ca_bundle)
    # The SDK wants a key even for a server without one; the placeholder is sent and never checked.
    client = AsyncOpenAI(base_url=spec.base_url, api_key=api_key or "no-key", http_client=http_client)
    provider = OpenAIProvider(openai_client=client)
    profile = compat_profile(caps)
    # The callable form replaces the provider's own guesses (OpenAI's, from the model's name) entirely.
    model = _compat_model_class()(
        model_id, provider=provider, profile=lambda _: merge_profile(DEFAULT_PROFILE, profile)
    )
    model.caps = caps
    return cast(Model, model)


def model_caps(model: Any) -> ModelCaps | None:
    """The capabilities a model on an OpenAI-compatible server was built with (through any wrapper)."""
    for _ in range(8):
        if model is None:
            return None
        caps = getattr(model, "caps", None)
        if isinstance(caps, ModelCaps):
            return caps
        model = getattr(model, "wrapped", None)
    return None


def output_spec[T](model: Model, out: type[T]) -> Any:
    """The output type a use gives its agent, with the output mode chosen for the model. A model on an
    OpenAI-compatible server gets the mode its capabilities allow (a schema the server enforces, a tool call,
    or a schema in the prompt); a built-in provider's model keeps Pydantic AI's choice for it."""
    caps = model_caps(model)
    if caps is None:
        return out
    from pydantic_ai import NativeOutput, PromptedOutput, ToolOutput

    match caps.output_mode():
        case "native":
            return NativeOutput(out)
        case "tool":
            return ToolOutput(out)
        case "prompted":
            return PromptedOutput(out)
