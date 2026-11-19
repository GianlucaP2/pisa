"""ECB exchange rate with fallbacks, and the catalog merge + capability rules."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from pisa.brain.models.catalog import (
    CatalogRow,
    builtin_rows,
    conservative_capabilities,
    merge_catalog,
    openrouter_capabilities,
    refresh_catalog,
)
from pisa.brain.models.providers import PROVIDERS_BY_ID, ListedModel, parse_model_list
from pisa.infra.fx import DEFAULT_USD_TO_EUR, parse_ecb_daily, refresh_usd_to_eur, usd_to_eur
from pisa.infra.keys import KeyStore
from tests.conftest import FAKE_KEY, dsn
from tests.gateway_env import run

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube><Cube time="2026-10-19">
    <Cube currency="JPY" rate="171.20"/><Cube currency="USD" rate="1.1625"/>
  </Cube></Cube>
</gesmes:Envelope>"""


def test_parse_ecb() -> None:
    day, rate = parse_ecb_daily(ECB_XML)
    assert day.isoformat() == "2026-10-19"
    assert rate == Decimal("0.860215")  # 1 / 1.1625


def _refresh(db: psycopg.Connection, handler) -> Decimal:  # type: ignore[no-untyped-def]
    async def go() -> Decimal:
        async with (
            await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        ):
            await refresh_usd_to_eur(conn, http)
            return await usd_to_eur(conn)

    return run(go)


def test_ecb_fallbacks(db: psycopg.Connection) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert _refresh(db, down) == DEFAULT_USD_TO_EUR == Decimal("0.86")  # nothing stored yet
    assert _refresh(db, lambda r: httpx.Response(200, text=ECB_XML)) == Decimal("0.860215")
    assert _refresh(db, lambda r: httpx.Response(503)) == Decimal("0.860215")  # last stored value
    assert _refresh(db, lambda r: httpx.Response(200, text="<not-xml")) == Decimal("0.860215")


def test_capability_rules() -> None:
    assert conservative_capabilities("text-embedding-4") == ["embeddings"]
    assert conservative_capabilities("brand-new-chat-model") == []
    item = {
        "supported_parameters": ["tools", "response_format", "structured_outputs"],
        "architecture": {"input_modalities": ["text", "image", "audio"], "output_modalities": ["text"]},
    }
    assert openrouter_capabilities(item) == ["tools", "structured_output", "vision", "audio_in"]
    json_mode_only = {"supported_parameters": ["response_format"], "architecture": {}}
    assert openrouter_capabilities(json_mode_only) == []


def test_parse_provider_model_lists() -> None:
    mistral = parse_model_list(
        PROVIDERS_BY_ID["mistral"],
        {
            "data": [
                {
                    "id": "mistral-small",
                    "max_context_length": 128000,
                    "capabilities": {"function_calling": True, "vision": True},
                }
            ]
        },
    )
    assert mistral == [ListedModel("mistral-small", None, 128000, ("tools", "structured_output", "vision"))]
    google = parse_model_list(
        PROVIDERS_BY_ID["google"],
        {"models": [{"name": "models/text-embedding-005", "supportedGenerationMethods": ["embedContent"]}]},
    )
    assert google[0].model_id == "text-embedding-005" and google[0].capabilities == ("embeddings",)
    anthropic = parse_model_list(
        PROVIDERS_BY_ID["anthropic"],
        {
            "data": [
                {
                    "id": "claude-x",
                    "display_name": "Claude X",
                    "capabilities": {
                        "image_input": {"supported": True},
                        "structured_outputs": {"supported": False},
                    },
                }
            ]
        },
    )
    assert anthropic[0].capabilities == ("tools", "vision") and anthropic[0].name == "Claude X"


def test_sources() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    builtin = builtin_rows(now)
    assert builtin["openai:gpt-6-luna"].input_usd_per_mtok == Decimal("0.1")  # base tier
    assert not any(r.startswith("google:claude") for r in builtin)  # Vertex partner models are not Gemini API
    openrouter = [
        {
            "id": "openai/gpt-6-luna",
            "name": "OpenAI: GPT-6 Luna",
            "context_length": 400000,
            "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
            "supported_parameters": ["tools", "structured_outputs"],
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        },
        {"id": "acme/free-model", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": []},
    ]
    listed = {
        "openai": [
            ListedModel("gpt-6-luna"),
            ListedModel("ft:gpt-6-luna:custom"),
            ListedModel("text-embedding-9"),
        ]
    }
    rows = merge_catalog(builtin, openrouter, listed)
    luna = rows["openai:gpt-6-luna"]
    assert luna.capabilities == ["tools", "structured_output", "vision"]  # from OpenRouter's entry
    assert luna.price_source == "genai-prices"
    assert (
        rows["openrouter:acme/free-model"].price_known
        and rows["openrouter:acme/free-model"].capabilities == []
    )
    custom = rows["openai:ft:gpt-6-luna:custom"]
    assert custom.capabilities == [] and not custom.price_known  # unknown: conservative
    assert rows["openai:text-embedding-9"].capabilities == ["embeddings"]


@pytest.mark.parametrize("ollama", [None, "http://127.0.0.1:11434"])
def test_refresh_catalog_offline_and_with_sources(
    db: psycopg.Connection, secrets_key: str, ollama: str | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                200,
                json={"data": [{"id": "x/y", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]},
            )
        if request.url.host == "api.openai.com":
            assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
            return httpx.Response(200, json={"data": [{"id": "gpt-6-luna"}, {"id": "gpt-new"}]})
        if request.url.host == "127.0.0.1" and request.url.path == "/v1/models":
            # Ollama is an OpenAI-compatible server: its models come from /v1/models, unprobed until probed
            return httpx.Response(200, json={"data": [{"id": "llama3.3:latest"}, {"id": "qwen3:8b"}]})
        raise httpx.ConnectError("offline", request=request)

    async def go() -> list[str]:
        keys = KeyStore(SecretStr(secrets_key))
        async with (
            await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        ):
            await keys.set(conn, "openai", FAKE_KEY)
            await conn.execute(
                "INSERT INTO model_capabilities (provider, model_id, probe, probed_at) "
                "VALUES ('ollama', 'qwen3:8b', %s, now())",
                (Jsonb({"tools": True, "vision": False}),),
            )
            report = await refresh_catalog(conn, http, keys, ollama_base_url=ollama)
            return report.errors

    assert run(go) == []
    refs = {r[0] for r in db.execute("SELECT ref FROM model_catalog").fetchall()}
    assert {"openai:gpt-6-luna", "openai:gpt-new", "openrouter:x/y", "anthropic:claude-haiku-5-5"} <= refs
    ollama_rows = {
        r[0]: tuple(r[1:])
        for r in db.execute(
            "SELECT model_id, capabilities, input_usd_per_mtok, output_usd_per_mtok, price_source "
            "FROM model_catalog WHERE provider = 'ollama'"
        ).fetchall()
    }
    # never probed: nothing assumed; probed: what the probe found; no price either way
    assert ollama_rows == (
        {
            "llama3.3:latest": ([], Decimal(0), Decimal(0), "local"),
            "qwen3:8b": (["tools", "structured_output"], Decimal(0), Decimal(0), "local"),
        }
        if ollama
        else {}
    )
    assert db.execute("SELECT count(*) FROM app_settings WHERE key = 'catalog_refreshed_at'").fetchone() == (
        1,
    )


def test_catalog_row_price_known() -> None:
    assert not CatalogRow("a:b", "a", "b", "b").price_known


def test_refresh_keeps_stored_capabilities_when_openrouter_is_down(
    db: psycopg.Connection, secrets_key: str
) -> None:
    openrouter_up = True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai" and openrouter_up:
            item = {
                "id": "openai/gpt-6-luna",
                "supported_parameters": ["tools", "response_format", "structured_outputs"],
                "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
            }
            return httpx.Response(200, json={"data": [item]})
        raise httpx.ConnectError("offline", request=request)

    async def go() -> list[str]:
        keys = KeyStore(SecretStr(secrets_key))
        async with (
            await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        ):
            return (await refresh_catalog(conn, http, keys, ollama_base_url=None)).errors

    def caps() -> list[str]:
        row = db.execute("SELECT capabilities FROM model_catalog WHERE ref = 'openai:gpt-6-luna'").fetchone()
        assert row is not None
        return row[0]

    assert run(go) == []
    good = caps()
    assert "tools" in good
    openrouter_up = False
    errors = run(go)
    assert any("OpenRouter" in e for e in errors)
    assert caps() == good  # not wiped to the conservative []


def test_unreadable_key_does_not_abort_refresh(db: psycopg.Connection, secrets_key: str) -> None:
    from cryptography.fernet import Fernet

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def go() -> list[str]:
        async with (
            await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        ):
            await KeyStore(SecretStr(secrets_key)).set(conn, "openai", FAKE_KEY)
            rotated = KeyStore(SecretStr(Fernet.generate_key().decode()))  # PISA_SECRETS_KEY changed
            return (await refresh_catalog(conn, http, rotated, ollama_base_url=None)).errors

    errors = run(go)
    assert errors  # reported, not raised
    assert db.execute("SELECT count(*) FROM model_catalog").fetchone()[0] > 0  # pyright: ignore[reportOptionalSubscript]
