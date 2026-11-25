"""REST contract B: providers and keys, slots, models with prices, spend and caps."""

import json
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
from structlog.testing import capture_logs

from tests.conftest import FAKE_KEY, pair, seed_catalog, set_key
from tests.support.fake_secrets import openai_key

WRONG = openai_key("wrong-key-123456")  # a deliberately wrong key (fake)


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def openai_models(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.openai.com" and request.url.path == "/v1/models":
        if request.headers.get("authorization") != f"Bearer {FAKE_KEY}":
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": "Incorrect API key provided: " + request.headers.get("authorization", "")
                    }
                },
            )
        return httpx.Response(
            200, json={"data": [{"id": "gpt-6-luna"}, {"id": "gpt-6"}, {"id": "text-embedding-4"}]}
        )
    raise httpx.ConnectError("offline", request=request)


def test_keys_are_write_only_encrypted_and_never_logged(  # type: ignore[no-untyped-def]
    client, auth, db, http_handler, capsys
) -> None:
    http_handler["handler"] = openai_models
    with capture_logs() as logs:
        put = client.put("/v1/ai/providers/openai/key", headers=auth, json={"key": FAKE_KEY})
        assert put.status_code == 204 and put.content == b""
        listed = client.get("/v1/ai/providers", headers=auth)
        tested = client.post("/v1/ai/providers/openai/test", headers=auth)
        wrong = client.put(
            "/v1/ai/providers/openai/key",
            headers=auth,
            json={"key": WRONG},
        )
        failed_test = client.post("/v1/ai/providers/openai/test", headers=auth)
        invalid = client.put(
            "/v1/ai/providers/openai/key", headers=auth, json={"key": FAKE_KEY, "extra": [1]}
        )
        bad_shape = client.put("/v1/ai/providers/openai/key", headers=auth, json={"key": 12345678901})

    openai = next(p for p in listed.json()["providers"] if p["id"] == "openai")
    assert openai == {
        "id": "openai",
        "name": "OpenAI",
        "type": "builtin",
        "key_set": True,
        "key_last4": FAKE_KEY[-4:],
        "last_test": None,
        "keys_url": "https://platform.openai.com/api-keys",
        "base_url": None,
        "ca_bundle": None,
        "editable": False,
        "read_timeout_s": None,  # an own server's only
        "thinking": None,
    }
    assert tested.json() == {"ok": True, "message": "3 models are available.", "models_found": 3}
    assert wrong.status_code == 204
    assert failed_test.json() == {
        "ok": False,
        "message": "OpenAI rejected the key (HTTP 401).",
        "models_found": 0,
    }
    assert invalid.status_code == 204 and bad_shape.status_code == 400

    stored = db.execute("SELECT ciphertext, last4 FROM provider_keys WHERE provider = 'openai'").fetchone()
    assert stored is not None and FAKE_KEY.encode() not in bytes(stored[0]) and stored[1] == FAKE_KEY[-4:]

    everything = json.dumps(logs, default=str) + capsys.readouterr().out + capsys.readouterr().err
    for response in (put, listed, tested, wrong, failed_test, invalid, bad_shape):
        everything += response.text
    assert FAKE_KEY not in everything
    assert WRONG not in everything
    assert any(e["event"] == "provider_key_set" for e in logs)

    # the suite's slots are on OpenAI: its key can't go while they are (tests/test_connections.py)
    in_use = client.delete("/v1/ai/providers/openai/key", headers=auth)
    assert in_use.status_code == 409 and in_use.json()["error"]["code"] == "provider_in_use"
    db.execute("UPDATE model_slots SET model_ref = NULL, fallback_ref = NULL")
    assert client.delete("/v1/ai/providers/openai/key", headers=auth).status_code == 204
    after = next(
        p for p in client.get("/v1/ai/providers", headers=auth).json()["providers"] if p["id"] == "openai"
    )
    assert (after["key_set"], after["key_last4"]) == (False, None)
    assert db.execute("SELECT count(*) FROM provider_keys").fetchone() == (0,)


def test_keys_refused_without_secrets_key(app_settings, db, fake_models, clock, auth) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    settings = app_settings.model_copy(update={"secrets_key": None})

    async def factory(s):  # type: ignore[no-untyped-def]
        return await Runtime.start(s, model_factory=fake_models, clock=clock)

    with TestClient(create_app(settings, runtime_factory=factory)) as c:
        response = c.put("/v1/ai/providers/openai/key", headers=auth, json={"key": FAKE_KEY})
    assert response.status_code == 500
    assert response.json()["error"]["message"].startswith("PISA_SECRETS_KEY is not set")
    assert ".env.example" in response.json()["error"]["action"]


def test_providers_cover_the_registry(client, auth) -> None:  # type: ignore[no-untyped-def]
    ids = [p["id"] for p in client.get("/v1/ai/providers", headers=auth).json()["providers"]]
    # an OpenAI-compatible server (Ollama's included) is listed after these once it is added
    assert ids == [
        "anthropic", "openai", "google", "mistral", "groq", "cohere", "xai", "openrouter",
        "deepseek", "moonshot", "zhipu", "minimax", "qwen", "elevenlabs",
    ]  # fmt: skip
    unknown = client.put("/v1/ai/providers/nope/key", headers=auth, json={"key": FAKE_KEY})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    missing = client.post("/v1/ai/providers/anthropic/test", headers=auth)
    assert missing.json()["error"]["code"] == "provider_key_missing"


def test_slots_list_and_compatibility(client, auth, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    for provider in ("openai", "anthropic", "openrouter"):  # a slot points only at a provider with a key
        set_key(db, secrets_key, provider)
    seed_catalog(
        db,
        [
            {
                "ref": "openai:gpt-6-luna",
                "capabilities": ["tools", "structured_output"],
                "input": Decimal("0.1"),
                "output": Decimal("0.5"),
            },
            {
                "ref": "anthropic:claude-haiku-5-5",
                "capabilities": ["tools", "structured_output", "vision"],
                "input": Decimal("1"),
                "output": Decimal("5"),
            },
            {
                "ref": "openrouter:some/plain-model",
                "capabilities": [],
                "input": Decimal("0.2"),
                "output": Decimal("0.4"),
            },
        ],
    )
    slots = client.get("/v1/ai/slots", headers=auth).json()["slots"]
    assert [s["id"] for s in slots] == [
        "chat.conversation", "conversation.reflex", "intake.triage", "intake.extract", "briefing.compose",
        "places.research", "memory.embed", "understand.confirm", "attend.decide", "learn.answer",
    ]  # fmt: skip
    chat = slots[0]
    assert chat["needs"] == ["tools"] and chat["model"] == "openai:gpt-6-luna" and chat["fallback"] is None
    assert slots[5]["needs"] == ["structured_output"] and slots[5]["model"] == "openai:gpt-6-luna"
    assert slots[6]["model"] is None and slots[6]["needs"] == ["embeddings"]

    ok = client.put(
        "/v1/ai/slots/chat.conversation",
        headers=auth,
        json={"model": "anthropic:claude-haiku-5-5", "fallback": "openai:gpt-6-luna"},
    )
    assert ok.status_code == 200 and ok.json()["model"] == "anthropic:claude-haiku-5-5"
    assert ok.json()["fallback"] == "openai:gpt-6-luna"

    bad = client.put(
        "/v1/ai/slots/chat.conversation",
        headers=auth,
        json={"model": "openrouter:some/plain-model", "fallback": None},
    )
    assert bad.status_code == 400
    assert (
        bad.json()["error"]["code"] == "model_incompatible"
        and "lacks tools" in bad.json()["error"]["message"]
    )
    bad_fallback = client.put(
        "/v1/ai/slots/memory.embed",
        headers=auth,
        json={"model": "anthropic:claude-haiku-5-5", "fallback": "openai:gpt-6-luna"},
    )
    assert (
        bad_fallback.json()["error"]["code"] == "model_incompatible"
        and "embeddings" in bad_fallback.json()["error"]["message"]
    )
    no_needs = client.put(
        "/v1/ai/slots/briefing.compose",
        headers=auth,
        json={"model": "openrouter:some/plain-model", "fallback": None},
    )
    assert no_needs.status_code == 200

    unknown = client.put(
        "/v1/ai/slots/chat.conversation", headers=auth, json={"model": "openai:nope", "fallback": None}
    )
    assert unknown.json()["error"]["code"] == "validation_failed"
    malformed = client.put(
        "/v1/ai/slots/chat.conversation", headers=auth, json={"model": "gpt", "fallback": None}
    )
    assert malformed.json()["error"]["code"] == "validation_failed"
    assert (
        client.put(
            "/v1/ai/slots/nope", headers=auth, json={"model": "openai:gpt-6-luna", "fallback": None}
        ).status_code
        == 404
    )


def _usage(
    db: psycopg.Connection, slot: str, input_tokens: int, output_tokens: int, days_ago: int = 1
) -> None:
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, input_tokens, "
        "output_tokens, cost_eur, "
        "price_known, trace_id, created_at) VALUES (gen_random_uuid(), %s, 'openai:gpt-6-luna', "
        "'settled', 0, %s, "
        "%s, 0.01, true, 't', '2026-10-20T11:00:00Z'::timestamptz - make_interval(days => %s))",
        (slot, input_tokens, output_tokens, days_ago),
    )


def test_models_sorting_and_monthly_estimate(client, auth, db) -> None:  # type: ignore[no-untyped-def]
    seed_catalog(
        db,
        [
            {
                "ref": "openai:gpt-6-luna",
                "name": "GPT-6 Luna",
                "capabilities": ["tools"],
                "input": Decimal("0.1"),
                "output": Decimal("0.5"),
                "context_tokens": 400000,
            },
            {
                "ref": "anthropic:claude-haiku-5-5",
                "capabilities": ["tools", "vision"],
                "input": Decimal("1"),
                "output": Decimal("5"),
            },
            {
                "ref": "groq:cheap-no-tools",
                "capabilities": [],
                "input": Decimal("0.01"),
                "output": Decimal("0.02"),
            },
            {"ref": "openai:mystery", "capabilities": ["tools"]},
            {
                "ref": "deepseek:mid",
                "capabilities": ["tools"],
                "input": Decimal("0.2"),
                "output": Decimal("0.8"),
            },
        ],
    )
    # 7 days of chat usage: 7M input + 700k output tokens → 1M in / 100k out per day.
    _usage(db, "chat.conversation", 7_000_000, 700_000)
    _usage(db, "chat.conversation", 1, 1, days_ago=8)  # older than 7 days: ignored

    body = client.get("/v1/ai/models?slot=chat.conversation", headers=auth).json()
    assert body["usd_to_eur"] == 0.86 and body["catalog_refreshed_at"] is None
    refs = [m["ref"] for m in body["models"]]
    assert refs == [
        "openai:gpt-6-luna",          # compatible, output $0.5
        "deepseek:mid",               # compatible, $0.8
        "anthropic:claude-haiku-5-5", # compatible, $5
        "openai:mystery",             # compatible, price unknown: last of the compatible ones
        "groq:cheap-no-tools",        # incompatible: after every compatible model
    ]  # fmt: skip
    luna = body["models"][0]
    assert luna == {
        "ref": "openai:gpt-6-luna",
        "provider": "openai",
        "model_id": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "capabilities": ["tools"],
        "context_tokens": 400000,
        "price_eur_per_mtok": {"input": 0.086, "output": 0.43},
        "price_known": True,
        "key_set": False,
        "compatible": True,
        # 30 x (1M x $0.1 + 0.1M x $0.5) / 1M x 0.86 = 30 x $0.15 x 0.86
        "est_monthly_eur_for_slot": 3.87,
    }
    mystery = body["models"][3]
    assert mystery["price_known"] is False and mystery["price_eur_per_mtok"] is None
    assert mystery["est_monthly_eur_for_slot"] == 3.87  # priced at OpenAI's most expensive known model
    assert body["models"][4]["compatible"] is False

    no_usage = client.get("/v1/ai/models?slot=intake.triage", headers=auth).json()["models"]
    assert all(m["est_monthly_eur_for_slot"] is None for m in no_usage)
    plain = client.get("/v1/ai/models", headers=auth).json()["models"]
    assert all(m["compatible"] is True and m["est_monthly_eur_for_slot"] is None for m in plain)
    assert client.get("/v1/ai/models?slot=nope", headers=auth).status_code == 404


def test_spend_and_caps(client, auth, db) -> None:  # type: ignore[no-untyped-def]
    _usage(db, "chat.conversation", 10, 10, days_ago=0)
    body: dict[str, Any] = client.get("/v1/ai/spend", headers=auth).json()
    assert body == {
        "today_eur": 0.01,
        "month_eur": 0.01,
        "caps": {"daily_eur": 1.0, "monthly_eur": 20.0},
        "by_slot": [{"slot": "chat.conversation", "today_eur": 0.01, "month_eur": 0.01, "calls_today": 1}],
    }
    put = client.put("/v1/ai/caps", headers=auth, json={"daily_eur": 2.0, "monthly_eur": 30.0})
    assert put.json() == {"daily_eur": 2.0, "monthly_eur": 30.0}
    assert client.get("/v1/ai/spend", headers=auth).json()["caps"] == {"daily_eur": 2.0, "monthly_eur": 30.0}
    negative = client.put("/v1/ai/caps", headers=auth, json={"daily_eur": -1, "monthly_eur": 30.0})
    assert negative.status_code == 400 and negative.json()["error"]["code"] == "validation_failed"
    assert "daily_eur" in negative.json()["error"]["message"]
    # JSON doubles with float noise are rounded to cents, not rejected.
    noisy = client.put(
        "/v1/ai/caps", headers=auth, json={"daily_eur": 1.1000000000000001, "monthly_eur": 20.005}
    )
    assert noisy.status_code == 200 and noisy.json() == {"daily_eur": 1.1, "monthly_eur": 20.01}


def test_unknown_route_uses_error_shape(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/v1/nope")
    assert response.status_code == 404
    assert set(response.json()["error"]) == {"code", "message", "action"}
