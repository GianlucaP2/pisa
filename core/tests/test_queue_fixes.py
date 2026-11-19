"""Queued fixes: stale catalog rows, pagination, tolerant parsing, "can't verify", caps
order, database-down sentences, CLI behaviour, JSON logs for uvicorn, OpenAPI 400s."""

import json
import logging
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from pisa.brain.models.catalog import refresh_catalog
from pisa.brain.models.providers import PROVIDERS_BY_ID, list_models, parse_model_list
from pisa.infra.keys import KeyStore
from tests.conftest import FAKE_KEY, PROTOCOL_DIR, dsn, pair
from tests.gateway_env import run


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def test_stale_catalog_rows_go_after_three_missed_refreshes(db: psycopg.Connection, secrets_key: str) -> None:
    listing = {"openai": ["gpt-6-luna", "gpt-retired"], "openrouter_up": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            if not listing["openrouter_up"]:
                return httpx.Response(503)
            return httpx.Response(
                200, json={"data": [{"id": "x/y", "pricing": {"prompt": "0", "completion": "0"}}]}
            )
        if request.url.host == "api.openai.com":
            return httpx.Response(200, json={"data": [{"id": m} for m in listing["openai"]]})  # type: ignore[union-attr]
        raise httpx.ConnectError("offline", request=request)

    async def refresh(day: int) -> None:
        keys = KeyStore(SecretStr(secrets_key))
        async with (
            await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        ):
            if not (await keys.statuses(conn)):
                await keys.set(conn, "openai", FAKE_KEY)
            await refresh_catalog(
                conn, http, keys, ollama_base_url=None, now=datetime(2026, 10, day, tzinfo=UTC)
            )

    def refs() -> set[str]:
        return {r[0] for r in db.execute("SELECT ref FROM model_catalog").fetchall()}

    run(lambda: refresh(1))
    assert {"openai:gpt-retired", "openrouter:x/y"} <= refs()
    listing["openai"] = ["gpt-6-luna"]
    listing["openrouter_up"] = False
    for day in (2, 3, 4):  # OpenRouter is down: its rows don't count as missed
        run(lambda day=day: refresh(day))
    assert "openai:gpt-retired" not in refs()
    assert "openrouter:x/y" in refs() and "openai:gpt-6-luna" in refs()


def test_paginated_model_lists() -> None:
    pages = {
        "anthropic": [
            {"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"},
            {"data": [{"id": "claude-b"}], "has_more": False, "last_id": "claude-b"},
        ],
        "google": [
            {"models": [{"name": "models/gemini-a"}], "nextPageToken": "t2"},
            {"models": [{"name": "models/gemini-b"}]},
        ],
    }
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider = "anthropic" if request.url.host == "api.anthropic.com" else "google"
        params = dict(request.url.params)
        seen.append(params)
        second = "after_id" in params or "pageToken" in params
        return httpx.Response(200, json=pages[provider][1 if second else 0])

    async def go(provider: str) -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return [m.model_id for m in await list_models(PROVIDERS_BY_ID[provider], FAKE_KEY, http)]

    assert run(lambda: go("anthropic")) == ["claude-a", "claude-b"]
    assert run(lambda: go("google")) == ["gemini-a", "gemini-b"]
    assert {"limit": "1000", "after_id": "claude-a"} in seen and {
        "pageSize": "1000",
        "pageToken": "t2",
    } in seen


def test_model_list_parsing_skips_malformed_items() -> None:
    body = {"data": ["oops", None, 3, {"id": 7}, {"id": "gpt-ok"}, {"name": "no-id"}]}
    assert [m.model_id for m in parse_model_list(PROVIDERS_BY_ID["openai"], body)] == ["gpt-ok"]
    assert parse_model_list(PROVIDERS_BY_ID["google"], {"models": "nope"}) == []
    assert parse_model_list(PROVIDERS_BY_ID["openai"], ["not", "a", "dict"]) == []


def test_zai_key_test_says_cant_verify(client, auth) -> None:  # type: ignore[no-untyped-def]
    assert client.put("/v1/ai/providers/zhipu/key", headers=auth, json={"key": FAKE_KEY}).status_code == 204
    tested = client.post("/v1/ai/providers/zhipu/test", headers=auth).json()
    assert tested["ok"] is None and tested["models_found"] == 0
    assert "can't verify" in tested["message"] and tested["message"].endswith(".")
    zhipu = next(
        p for p in client.get("/v1/ai/providers", headers=auth).json()["providers"] if p["id"] == "zhipu"
    )
    assert zhipu["last_test"]["ok"] is None


def test_daily_cap_must_not_exceed_monthly(client, auth) -> None:  # type: ignore[no-untyped-def]
    response = client.put("/v1/ai/caps", headers=auth, json={"daily_eur": 30, "monthly_eur": 20})
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "validation_failed"
    assert body["message"] == "The daily cap (€30.00) can't be higher than the monthly cap (€20.00)."


def test_database_down_is_one_sentence_over_rest(client, auth, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from psycopg_pool import PoolTimeout

    async def unreachable(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PoolTimeout("couldn't get a connection after 30.00 sec")

    monkeypatch.setattr("pisa.services.ai.AiService.spend", unreachable)
    response = client.get("/v1/ai/spend", headers=auth)
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal",
        "message": "The database is unreachable.",
        "action": "Try again shortly",
    }


def test_cli_says_one_sentence_when_the_database_is_down(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from pisa.cli import main
    from pisa.settings import get_settings

    monkeypatch.setenv("PISA_DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level: None)
    get_settings.cache_clear()
    try:
        assert main(["devices", "list"]) == 1
    finally:
        get_settings.cache_clear()
    err = capsys.readouterr().err
    assert err.startswith("error: The database is unreachable (OperationalError).") and "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_secrets_set_refuses_before_prompting_without_secrets_key(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from pisa.cli import main
    from pisa.settings import get_settings

    monkeypatch.delenv("PISA_SECRETS_KEY", raising=False)
    monkeypatch.setenv("PISA_SECRETS_KEY", "")
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level: None)
    monkeypatch.setattr("pisa.cli._read_secret", lambda provider: pytest.fail("must not prompt"))
    get_settings.cache_clear()
    try:
        assert main(["secrets", "set", "openai"]) == 2
    finally:
        get_settings.cache_clear()
    assert "PISA_SECRETS_KEY is not set" in capsys.readouterr().err


def test_uvicorn_logs_are_json(capsys) -> None:  # type: ignore[no-untyped-def]
    import structlog

    from pisa.logging import configure_logging
    from tests.conftest import UVICORN_LOGGERS, uvicorn_logging_kept

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    with uvicorn_logging_kept():
        try:
            # as `pisa serve` starts (uvicorn.run with log_config=None): uvicorn's loggers have no level of
            # their own and are enabled, whatever an earlier test's uvicorn.Config left on this worker
            for name in UVICORN_LOGGERS:
                logging.getLogger(name).setLevel(logging.NOTSET)
                logging.getLogger(name).disabled = False
            configure_logging("info")
            logging.getLogger("uvicorn.access").info(
                '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/health", "1.1", 200
            )
            line = capsys.readouterr().out.strip().splitlines()[-1]
            event = json.loads(line)
            assert event["logger"] == "uvicorn.access" and event["level"] == "info"
            assert event["event"] == '127.0.0.1:1 - "GET /health HTTP/1.1" 200'
            callback = "/v1/some/path?code=4/secret-code&state=secret-state"
            logging.getLogger("uvicorn.access").info(
                '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", callback, "1.1", 302
            )
            line = capsys.readouterr().out.strip().splitlines()[-1]
            assert json.loads(line)["event"] == '127.0.0.1:1 - "GET /v1/some/path HTTP/1.1" 302'
            assert "secret" not in line
        finally:
            root.handlers[:], level = saved
            root.setLevel(level)
            structlog.reset_defaults()


def test_openapi_declares_400_not_422() -> None:
    text = (PROTOCOL_DIR / "openapi.json").read_text()
    spec = json.loads(text)
    put_caps = spec["paths"]["/v1/ai/caps"]["put"]["responses"]
    assert "422" not in put_caps and "400" in put_caps
    assert "HTTPValidationError" not in text


def test_security_audit_outbound_urls_never_reach_the_logs() -> None:
    """Httpx INFO lines carry every outbound URL with its query (coordinates, search
    words); after configure_logging only its warnings remain."""
    import logging

    from pisa.logging import configure_logging

    configure_logging("info")
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)
