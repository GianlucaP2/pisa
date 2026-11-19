"""A slot points only where a signed op (or the server itself) let the user's words go: a built-in provider
with a key, or a model server Pisa knows. Any slot change, from the app or from `pisa models set` in another
process, tells every connected device the new disclosure (`pg_notify`), and a model call is refused until the
user consents to it again (Q2, D-139)."""

# ruff: noqa: F811  (pytest fixtures imported by name)
from typing import Any

import psycopg
import pytest

from tests.conftest import FakeModels, pair_v2, seed_catalog, set_key
from tests.test_live_v2_socket import connect, hello, opened, run
from tests.test_models_cli import cli  # noqa: F401 (fixture)
from tests.test_privacy_v2 import auth, finished

TOOLS = ["tools", "structured_output"]


def _put(client: Any, token: str, model: str) -> Any:
    return client.put(
        "/v1/ai/slots/chat.conversation", json={"model": model, "fallback": None}, headers=auth(token)
    )


@pytest.mark.parametrize(
    "model",
    [
        "anthropic:other-model",  # a built-in provider whose key was never set
        "nowhere:other-model",  # a model server nobody added
        "ollama:other-model",  # the settings' Ollama, not configured here
    ],
)
def test_a_slot_points_only_at_a_provider_with_a_key_or_a_known_server(
    client: Any, db: psycopg.Connection, model: str
) -> None:
    _, token = pair_v2(db)
    seed_catalog(db, [{"ref": model, "capabilities": TOOLS}])
    before = db.execute("SELECT model_ref FROM model_slots WHERE id = 'chat.conversation'").fetchone()
    refused = _put(client, token, model)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "validation_failed", refused.text
    after = db.execute("SELECT model_ref FROM model_slots WHERE id = 'chat.conversation'").fetchone()
    assert after == before


def test_a_slot_on_a_provider_whose_key_was_set_is_taken(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    _, token = pair_v2(db)
    seed_catalog(db, [{"ref": "anthropic:other-model", "capabilities": TOOLS}])
    set_key(db, secrets_key, "anthropic")
    assert _put(client, token, "anthropic:other-model").status_code == 200


def test_the_cli_s_slot_change_reaches_the_socket_and_a_model_call_waits_for_consent_again(
    cli: None,
    client: Any,
    db: psycopg.Connection,
    secrets_key: str,
    fake_models: FakeModels,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pisa import cli as pisa_cli

    set_key(db, secrets_key)
    set_key(db, secrets_key, "anthropic")
    seed_catalog(db, [{"ref": "anthropic:other-model", "capabilities": TOOLS, "input": 1.0, "output": 2.0}])
    device_id, token = pair_v2(db)  # consent for the disclosure as it is now
    revision = client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"]
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        # another process (the server's CLI) moves the conversation to another provider
        assert pisa_cli.main(["models", "set", "chat.conversation", "anthropic:other-model"]) == 0
        capsys.readouterr()
        frames = app.until(lambda f: f.get("event", {}).get("name") == "pisa.ai.disclosure")
        told = frames[-1]["event"]["value"]
        assert told["revision"] != revision
        assert any(p["provider"]["id"] == "anthropic" for p in told["processors"])
        frame = run("ciao")
        app.send(frame)
        end = app.run_ends(frame["input"]["runId"])[-1]["event"]
        assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required", end
    before = len(fake_models.seen_messages)
    end = finished(client, token, "ciao")  # the recorded consent is for the old revision
    assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required", end
    assert len(fake_models.seen_messages) == before  # no model call
    stale = client.post("/v1/ai/consent", json={"revision": revision, "health": False}, headers=auth(token))
    assert stale.status_code == 409
    given = client.post(
        "/v1/ai/consent", json={"revision": told["revision"], "health": False}, headers=auth(token)
    )
    assert given.status_code == 204
    last = finished(client, token, "ciao")
    assert last["type"] == "RUN_FINISHED", last
