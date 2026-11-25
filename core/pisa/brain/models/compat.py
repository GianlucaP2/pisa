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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic_ai.models import Model

from pisa.brain.models import own_wire
from pisa.brain.models.providers import PROVIDERS_BY_ID, ProviderCallError, ProviderSpec, list_models
from pisa.db.settings import delete_setting
from pisa.domain.ai import SLOTS_BY_ID
from pisa.domain.errors import PisaError
from pisa.domain.model_caps import (
    DEFAULT_READ_TIMEOUT_S,
    MAX_READ_TIMEOUT_S,
    MIN_READ_TIMEOUT_S,
    OLLAMA_ID,
    SERVER_CAPABILITIES,
    SERVER_ID,
    ModelCaps,
    ServerCapability,
    clean_facts,
    clean_overrides,
    thinking_on,
)
from pisa.failures import redact
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger

_log = get_logger("pisa.models.compat")

# Local servers can be slow, on a CPU above all: one probe request may take a while to load the model.
PROBE_TIMEOUT_S = 120.0
PROBE_MAX_TOKENS = 256
# A thinking model can spend the probe's 256 tokens thinking (finish_reason "length" before any answer): the
# request is sent once more with this much room, so a capability is not judged on a cut answer.
PROBE_THINKING_MAX_TOKENS = 2048
# Price source of every model on such a server: the ledger counts its tokens at no cost.
LOCAL_PRICE_SOURCE = "local"


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    base_url: str
    ca_bundle: str | None
    source: Literal["settings", "user"]  # settings: PISA_OLLAMA_BASE_URL, so it can't be edited from the app
    read_timeout_s: int = DEFAULT_READ_TIMEOUT_S
    thinking: tuple[tuple[str, bool], ...] = ()  # the operator's per-slot choices (`model_caps.thinking_on`)

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            self.id,
            self.name,
            self.base_url,
            "",
            needs_key=False,
            kind="openai_compatible",
            ca_bundle=self.ca_bundle,
            read_timeout_s=float(self.read_timeout_s),
            thinking=self.thinking,
        )


def settings_server(ollama_base_url: str | None) -> Server | None:
    """The server PISA_OLLAMA_BASE_URL names (its root or its `/v1` address)."""
    if not ollama_base_url:
        return None
    root = ollama_base_url.rstrip("/")
    base = root if root.endswith("/v1") else root + "/v1"
    return Server(OLLAMA_ID, "Ollama", base, None, "settings")


_SERVER_COLUMNS = "id, name, base_url, ca_bundle, read_timeout_s, thinking"


def _thinking(value: Any) -> tuple[tuple[str, bool], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(sorted((k, v) for k, v in value.items() if k in SLOTS_BY_ID and isinstance(v, bool)))


def _server(row: Any) -> Server:
    return Server(row[0], row[1], row[2], row[3], "user", int(row[4]), _thinking(row[5]))


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


def check_read_timeout(seconds: Any) -> int:
    if (
        not isinstance(seconds, int)
        or isinstance(seconds, bool)
        or not MIN_READ_TIMEOUT_S <= seconds <= MAX_READ_TIMEOUT_S
    ):
        raise PisaError(
            "validation_failed",
            f"{seconds!r} can't be a server's read timeout: it needs whole seconds from {MIN_READ_TIMEOUT_S} "
            f"to {MAX_READ_TIMEOUT_S}.",
            f"Give the seconds a request may wait for the server's next byte, e.g. {DEFAULT_READ_TIMEOUT_S}; "
            "a CPU reading a long prompt needs several minutes",
        )
    return seconds


def check_thinking(choices: Any) -> dict[str, bool]:
    """Per-slot thinking choices: slot ids that call a text model, each on or off."""
    if not isinstance(choices, dict):
        raise PisaError("validation_failed", "Thinking choices are slot ids, each true or false.", "")
    for slot, on in choices.items():
        if slot not in SLOTS_BY_ID or slot == "memory.embed" or not isinstance(on, bool):
            raise PisaError(
                "validation_failed",
                f"{slot!r}: {on!r} is not a thinking choice: it needs a slot id that calls a text model, and "
                "true or false.",
                "Use a slot id as `pisa models list` shows it, e.g. chat.conversation",
            )
    return dict(choices)


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
    *,
    read_timeout_s: int | None = None,
    thinking: dict[str, bool] | None = None,
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
            "INSERT INTO model_servers (id, name, base_url, ca_bundle, read_timeout_s, thinking) "  # noqa: S608
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_SERVER_COLUMNS}",
            (
                server_id,
                name.strip() or server_id,
                check_base_url(base_url),
                check_ca_bundle(ca_bundle),
                DEFAULT_READ_TIMEOUT_S if read_timeout_s is None else check_read_timeout(read_timeout_s),
                Jsonb(check_thinking(thinking or {})),
            ),
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
    read_timeout_s: int | None = None,
    thinking: dict[str, bool] | None = None,
) -> Server:
    """`thinking`: the slots given change, the others keep their choice."""
    server = _editable(await get_server(conn, server_id, ollama_base_url), server_id)
    new = Server(
        server.id,
        (name.strip() or server.id) if name is not None else server.name,
        check_base_url(base_url) if base_url is not None else server.base_url,
        None
        if clear_ca_bundle
        else (check_ca_bundle(ca_bundle) if ca_bundle is not None else server.ca_bundle),
        "user",
        check_read_timeout(read_timeout_s) if read_timeout_s is not None else server.read_timeout_s,
        _thinking({**dict(server.thinking), **check_thinking(thinking)})
        if thinking is not None
        else server.thinking,
    )
    await conn.execute(
        "UPDATE model_servers SET name = %s, base_url = %s, ca_bundle = %s, read_timeout_s = %s, "
        "thinking = %s, updated_at = now() WHERE id = %s",
        (new.name, new.base_url, new.ca_bundle, new.read_timeout_s, Jsonb(dict(new.thinking)), new.id),
    )
    if new.base_url != server.base_url:
        # Another address may be another server: what was probed there no longer holds.
        await conn.execute(
            "UPDATE model_capabilities SET probe = NULL, probed_at = NULL, facts = '{}', truncated = NULL "
            "WHERE provider = %s",
            (new.id,),
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


_CAPS_COLUMNS = "probe, probed_at, overrides, facts, truncated"


def _caps(row: Any) -> ModelCaps:
    probe = {k: bool(v) for k, v in row[0].items() if k in SERVER_CAPABILITIES} if row[0] else None
    truncated = row[4] if isinstance(row[4], dict) else None
    return ModelCaps(probe, row[1], clean_overrides(row[2]), clean_facts(row[3]), truncated)


async def get_caps(conn: AsyncConnection, provider: str, model_id: str) -> ModelCaps:
    row = await (
        await conn.execute(
            f"SELECT {_CAPS_COLUMNS} FROM model_capabilities WHERE provider = %s AND model_id = %s",  # noqa: S608
            (provider, model_id),
        )
    ).fetchone()
    return ModelCaps() if row is None else _caps(row)


async def stored_caps(conn: AsyncConnection, provider: str) -> dict[str, ModelCaps]:
    rows = await (
        await conn.execute(
            f"SELECT model_id, {_CAPS_COLUMNS} FROM model_capabilities WHERE provider = %s",  # noqa: S608
            (provider,),
        )
    ).fetchall()
    return {r[0]: _caps(r[1:]) for r in rows}


async def save_probe(
    conn: AsyncConnection,
    provider: str,
    model_id: str,
    probe: dict[ServerCapability, bool],
    at: datetime,
    facts: dict[str, Any] | None = None,
) -> ModelCaps:
    """A new probe replaces the last one's facts. A cut seen before stays until a probe finds a context that
    holds the prompt that was cut (the operator raised it); the user may be told about a new cut again."""
    found = clean_facts(facts or {})
    cut = (await get_caps(conn, provider, model_id)).truncated
    if cut is not None and found.get("context_tokens", 0) >= int(cut.get("sent_tokens") or 0):
        cut = None
    await conn.execute(
        "INSERT INTO model_capabilities (provider, model_id, probe, probed_at, facts, truncated) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (provider, model_id) DO UPDATE SET probe = EXCLUDED.probe, "
        "probed_at = EXCLUDED.probed_at, facts = EXCLUDED.facts, truncated = EXCLUDED.truncated",
        (provider, model_id, Jsonb(probe), at, Jsonb(found), Jsonb(cut) if cut is not None else None),
    )
    own_wire.forget_cut(f"{provider}:{model_id}")
    caps = await get_caps(conn, provider, model_id)
    await sync_catalog(conn, provider, model_id, caps, at)
    return caps


async def save_truncation(
    conn: AsyncConnection, provider: str, model_id: str, record: dict[str, Any]
) -> None:
    """The last silent cut of a prompt this model's server made: token counts and when, never the prompt."""
    await conn.execute(
        "INSERT INTO model_capabilities (provider, model_id, truncated) VALUES (%s, %s, %s) "
        "ON CONFLICT (provider, model_id) DO UPDATE SET truncated = EXCLUDED.truncated",
        (provider, model_id, Jsonb(record)),
    )


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
    # what the probe saw beside the capabilities (`model_caps.clean_facts`): the served context, thinking,
    # the server's kind
    facts: dict[str, Any] = field(default_factory=dict)


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


def _thought(message: dict[str, Any]) -> bool:
    """The answer carries thinking: a reasoning field (`reasoning_content`, `reasoning`) or `<think>` in
    its text."""
    content = message.get("content")
    return any(
        isinstance(message.get(f), str) and message[f] for f in ("reasoning_content", "reasoning")
    ) or (isinstance(content, str) and "<think>" in content)


def _cut_while_thinking(body: Any, message: dict[str, Any]) -> bool:
    """The output limit ran out before any answer: thinking, with no text after it and no call."""
    try:
        finish = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return False
    if finish != "length" or message.get("tool_calls"):
        return False
    content = message.get("content")
    text = content.split("</think>", 1)[1] if isinstance(content, str) and "</think>" in content else None
    unanswered = not content or (isinstance(content, str) and "<think>" in content and not text)
    return unanswered and (_thought(message) or not content)


def server_root(base_url: str) -> str:
    """The server's own address: its OpenAI API's `/v1` taken off (llama.cpp's /props, Ollama's /api)."""
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _num_ctx(parameters: Any) -> int | None:
    """`num_ctx` in Ollama's Modelfile parameters ("num_ctx 8192" on a line of its own)."""
    if not isinstance(parameters, str):
        return None
    for line in parameters.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == "num_ctx" and words[1].isdigit():
            return int(words[1])
    return None


class _Prober:
    def __init__(self, spec: ProviderSpec, key: str | None, model_id: str, http: httpx.AsyncClient) -> None:
        self.base = spec.base_url.rstrip("/")
        self.root = server_root(spec.base_url)
        self.headers = spec.auth_headers(key)
        self.key = key
        self.model_id = model_id
        self.http = http
        # the server's own read timeout when it is longer: one probe request may load the model first
        self.timeout = max(PROBE_TIMEOUT_S, spec.read_timeout_s or 0.0)
        self.thinking = False  # an answer carried thinking

    async def post(self, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.post(
                self.base + path, headers=self.headers, timeout=self.timeout, **kwargs
            )
        except httpx.TimeoutException:
            raise _Fail(f"no answer within {self.timeout:g} s") from None
        except httpx.HTTPError as exc:
            raise _Fail(f"the request failed ({exc.__class__.__name__})") from None
        if response.status_code >= 400:
            raise _Fail(f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise _Fail("the answer is not JSON") from None

    async def chat(self, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        """One chat request; once more with room to think when the answer was cut while thinking."""
        body = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": PROBE_MAX_TOKENS,
            "temperature": 0,
            "stream": False,
            **extra,
        }
        answer = await self.post("/chat/completions", json=body)
        message = _message(answer)
        self.thinking = self.thinking or _thought(message)
        if _cut_while_thinking(answer, message):
            answer = await self.post(
                "/chat/completions", json={**body, "max_tokens": PROBE_THINKING_MAX_TOKENS}
            )
            message = _message(answer)
            self.thinking = self.thinking or _thought(message)
        return message

    async def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        """A server's own endpoint, best effort: None when it has none or it fails."""
        try:
            response = await self.http.request(
                method, url, headers=self.headers, timeout=min(self.timeout, 20.0), **kwargs
            )
            return response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def facts(self) -> dict[str, Any]:
        """The server's kind and the context it serves the model with, where the server says: llama.cpp's
        /props (`n_ctx`), Ollama's /api/ps (the loaded model's `context_length`) or /api/show (a
        Modelfile's `num_ctx`). Ollama's default context (4096, or OLLAMA_CONTEXT_LENGTH) is not shown
        anywhere until the model is loaded."""
        found: dict[str, Any] = {"thinking": self.thinking}
        props = await self._json("GET", self.root + "/props")
        if isinstance(props, dict) and ("default_generation_settings" in props or "n_ctx" in props):
            found["server"] = "llama.cpp"
            settings = props.get("default_generation_settings")
            n_ctx = (settings.get("n_ctx") if isinstance(settings, dict) else None) or props.get("n_ctx")
            if _is_int(n_ctx):
                found["context_tokens"] = n_ctx
            return clean_facts(found)
        ps = await self._json("GET", self.root + "/api/ps")
        if isinstance(ps, dict) and isinstance(ps.get("models"), list):
            found["server"] = "ollama"
            for item in ps["models"]:
                if isinstance(item, dict) and self.model_id in (item.get("name"), item.get("model")):
                    if _is_int(item.get("context_length")):
                        found["context_tokens"] = item["context_length"]
            if "context_tokens" not in found:
                show = await self._json("POST", self.root + "/api/show", json={"model": self.model_id})
                if isinstance(show, dict) and (n_ctx := _num_ctx(show.get("parameters"))) is not None:
                    found["context_tokens"] = n_ctx
        return clean_facts(found)

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
    facts = await prober.facts()
    _log.info("model_probed", provider=spec.id, model=model_id, caps=caps, notes=notes, facts=facts)
    return ProbeResult(caps, notes, facts)


async def embeds(spec: ProviderSpec, key: str | None, model_id: str, http: httpx.AsyncClient) -> bool:
    """The probe's embeddings request alone: whether the model answers `/v1/embeddings` with a vector."""
    try:
        await _Prober(spec, key, model_id, http).embeddings()
    except _Fail:
        return False
    return True


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
            _log.warning("model_probe_unreachable", provider=spec.id, model=model_id, status=exc.status)
            return FirstProbe(caps, exc.message)
        result = await probe_model(spec, key, model_id, client)
        return FirstProbe(await save_probe(conn, spec.id, model_id, result.caps, now(), result.facts))


# --- the model ----------------------------------------------------------------------------------------------


def compat_profile(caps: ModelCaps) -> Any:
    """The Pydantic AI profile from the effective capabilities, and nothing assumed beyond them; plus one
    workaround every own server needs, whatever its model: a whitespace-only start of the streamed text (the
    `\\n\\n` a thinking model writes after its thinking, before a tool call) is not text, so it never ends a
    tool turn as its answer (Pydantic AI sets it for Qwen-family names; here it is set for every model)."""
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
        ignore_streamed_leading_whitespace=True,
    )


_SDK_TLS_CLIENTS: dict[str, Any] = {}


def _sdk_tls_client(ca_bundle: str) -> Any:
    """The OpenAI SDK's own HTTP client, trusting this CA bundle: one per bundle, for the process's life."""
    from openai import DefaultAsyncHttpxClient

    client = _SDK_TLS_CLIENTS.get(ca_bundle)
    if client is None or client.is_closed:
        client = _SDK_TLS_CLIENTS[ca_bundle] = DefaultAsyncHttpxClient(verify=tls_context(ca_bundle))
    return client


_THINK_END = "</think>"


class _Lead:
    """Leading blank lines are never text: at the start of the answer and right after `</think>`."""

    def __init__(self) -> None:
        self._strip = True

    def feed(self, text: str) -> str:
        out: list[str] = []
        while text:
            if self._strip:
                text = text.lstrip()
                if not text:
                    break
                self._strip = False
            at = text.find(_THINK_END)
            if at < 0:
                out.append(text)
                break
            out.append(text[: at + len(_THINK_END)])
            text = text[at + len(_THINK_END) :]
            self._strip = True
        return "".join(out)


@functools.cache
def _compat_model_class() -> type[Any]:
    from collections.abc import AsyncIterator, Iterable
    from dataclasses import replace

    from pydantic_ai.messages import ModelResponse, ModelResponseStreamEvent, TextPart, ToolCallPart
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse
    from pydantic_ai.profiles import DEFAULT_THINKING_TAGS

    @dataclass
    class CompatStreamedResponse(OpenAIStreamedResponse):
        """The stream with the own-server rules: no tool-call markup, no leading blank lines, no call on a
        round sent without tools; the prompt's cut checked once the usage came."""

        _owner: Any = field(default=None, init=False)
        _no_tools: bool = field(default=False, init=False)
        _markup: own_wire.Markup = field(default_factory=own_wire.Markup, init=False)
        _lead: _Lead = field(default_factory=_Lead, init=False)
        _refused: bool = field(default=False, init=False)

        def _map_text_delta(self, choice: Any) -> Iterable[ModelResponseStreamEvent]:
            content = choice.delta.content
            if content:
                choice.delta.content = self._lead.feed(self._markup.feed(content)) or None
            return super()._map_text_delta(choice)

        def _map_tool_call_delta(self, choice: Any) -> Iterable[ModelResponseStreamEvent]:
            if self._no_tools and choice.delta.tool_calls:
                if not self._refused:
                    self._refused = True
                    _log.warning("stray_tool_call_refused", model=self.model_name)
                return ()
            return super()._map_tool_call_delta(choice)

        async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
            async for event in super()._get_event_iterator():
                yield event
            if rest := self._lead.feed(self._markup.flush()):
                for event in self._parts_manager.handle_text_delta(
                    vendor_part_id=("content", self._content_generation),
                    content=rest,
                    thinking_tags=self._model_profile.get("thinking_tags", DEFAULT_THINKING_TAGS),
                    ignore_leading_whitespace=True,
                ):
                    yield event
            if self._owner is not None:
                self._owner.check_cut(self.usage.input_tokens)

    class CompatChatModel(OpenAIChatModel):
        """An OpenAI chat model on the user's own server, carrying the capabilities it was built from, its
        server's read timeout and whether this use may think. A round that may use no tool is sent without
        tools (both servers measured ignore or misread `tool_choice: "none"`); a call on it is refused."""

        caps: ModelCaps
        read_timeout_s: float = float(DEFAULT_READ_TIMEOUT_S)
        thinking_off: bool = False
        cut: dict[str, Any] | None = None  # the last silent cut of a prompt this model's calls saw
        server_id: str = ""
        _no_tools: bool = False
        _tool_chars: int = 0
        _sent_chars: int = 0

        @property
        def ref(self) -> str:
            return f"{self.server_id}:{self.model_name}"

        def prepare_request(self, model_settings: Any, model_request_parameters: Any) -> Any:
            settings, params = super().prepare_request(model_settings, model_request_parameters)
            own: dict[str, Any] = dict(settings or {})
            own["timeout"] = self.read_timeout_s  # the server's, whatever the use asked
            if self.thinking_off and self.caps.thinks:
                extra: dict[str, Any] = dict(own.get("extra_body") or {})
                own["extra_body"] = {**extra, **own_wire.thinking_off_body(self.caps.server)}
            return own, params

        def _get_tool_choice(self, model_settings: Any, model_request_parameters: Any) -> Any:
            tools, choice = super()._get_tool_choice(model_settings, model_request_parameters)
            self._no_tools = choice == "none"
            if self._no_tools:
                self._tool_chars = 0
                return [], None
            self._tool_chars = own_wire.wire_chars(tools)
            return tools, choice

        async def _map_messages(self, *args: Any, **kwargs: Any) -> Any:
            mapped = await super()._map_messages(*args, **kwargs)
            self._sent_chars = own_wire.wire_chars(mapped) + self._tool_chars
            return mapped

        def check_cut(self, prompt_tokens: int | None) -> None:
            cut = own_wire.truncated(self._sent_chars, prompt_tokens)
            if cut is None:
                return
            self.cut = own_wire.record_cut(self.ref, cut)
            _log.warning("model_prompt_truncated", model=self.ref, **cut)

        def _process_response(self, response: Any) -> ModelResponse:
            processed = super()._process_response(response)
            parts: list[Any] = []
            lead = _Lead()
            for part in processed.parts:
                if isinstance(part, TextPart):
                    text = lead.feed(own_wire.strip_markup(part.content))
                    if text:
                        parts.append(replace(part, content=text))
                elif isinstance(part, ToolCallPart) and self._no_tools:
                    _log.warning("stray_tool_call_refused", model=self.model_name)
                else:
                    parts.append(part)
            self.check_cut(processed.usage.input_tokens)
            return replace(processed, parts=parts)

        @property
        def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
            return CompatStreamedResponse

        async def _process_streamed_response(self, *args: Any, **kwargs: Any) -> OpenAIStreamedResponse:
            stream = await super()._process_streamed_response(*args, **kwargs)
            if isinstance(stream, CompatStreamedResponse):
                stream._owner, stream._no_tools = self, self._no_tools
            return stream

    return CompatChatModel


def build_compat_model(
    spec: ProviderSpec,
    model_id: str,
    api_key: str | None,
    caps: ModelCaps,
    http_client: Any = None,
) -> Model:
    """`http_client` is for tests (a client on a fake transport); otherwise the SDK's own client, or one that
    trusts the server's CA bundle. The SDK makes no retry of its own: a server that timed out or refused would
    otherwise get the same request three times, and a CPU still reading the first one is not helped by a
    second."""
    from pydantic_ai.profiles import DEFAULT_PROFILE, merge_profile
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(openai_client=_sdk_client(spec, api_key, http_client))
    profile = compat_profile(caps)
    # The callable form replaces the provider's own guesses (OpenAI's, from the model's name) entirely.
    model = _compat_model_class()(
        model_id, provider=provider, profile=lambda _: merge_profile(DEFAULT_PROFILE, profile)
    )
    model.caps = caps
    model.server_id = spec.id
    model.read_timeout_s = read_timeout(spec)
    return cast(Model, model)


def read_timeout(spec: ProviderSpec) -> float:
    return spec.read_timeout_s or float(DEFAULT_READ_TIMEOUT_S)


def _sdk_client(spec: ProviderSpec, api_key: str | None, http_client: Any) -> Any:
    from openai import AsyncOpenAI

    if http_client is None and spec.ca_bundle:
        http_client = _sdk_tls_client(spec.ca_bundle)
    # The SDK wants a key even for a server without one; the placeholder is sent and never checked.
    return AsyncOpenAI(
        base_url=spec.base_url,
        api_key=api_key or "no-key",
        http_client=http_client,
        max_retries=0,
        timeout=read_timeout(spec),
    )


def build_compat_embedder(
    spec: ProviderSpec, model_id: str, api_key: str | None, http_client: Any = None
) -> Any:
    """Pydantic AI's OpenAI embedding model on the server's `/v1/embeddings` (llama.cpp with --embeddings,
    Ollama, vLLM), with the same client rules as its chat models."""
    from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIEmbeddingModel(
        model_id, provider=OpenAIProvider(openai_client=_sdk_client(spec, api_key, http_client))
    )


def _own_model(model: Any) -> Any:
    """The own-server model under any wrapper, or None."""
    for _ in range(8):
        if model is None:
            return None
        if isinstance(getattr(model, "caps", None), ModelCaps) and hasattr(model, "thinking_off"):
            return model
        model = getattr(model, "wrapped", None)
    return None


def for_slot(model: Any, slot: str, spec: ProviderSpec) -> None:
    """The slot's thinking choice on this server (`model_caps.thinking_on`), for the calls the model makes."""
    own = _own_model(model)
    if own is not None:
        own.thinking_off = not thinking_on(slot, dict(spec.thinking))


def cut_of(model: Any) -> dict[str, Any] | None:
    """The silent cut the model's calls saw (token counts and when), or None."""
    own = _own_model(model)
    return own.cut if own is not None else None


def cut_notice_once(model: Any, lang: str) -> str | None:
    """The sentence that tells the user their model server cut what Pisa sent: once per model in this
    process (until the model is probed again), and only after a cut was seen."""
    own = _own_model(model)
    if own is None or own.cut is None or not own_wire.tell_once(own.ref):
        return None
    return own_wire.cut_notice(lang)


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
