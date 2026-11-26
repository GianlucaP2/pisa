"""OpenAI-compatible servers: the probe against each server's quirks, the profile and output modes, the
capability errors, the ledger at no price, TLS with a private CA, and the REST endpoints."""

import asyncio
import datetime as dt
import json
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import BaseModel
from pydantic_ai import Agent

from pisa.brain.models.compat import (
    Server,
    build_compat_model,
    check_ca_bundle,
    compat_profile,
    http_for,
    output_spec,
    probe_model,
    settings_server,
)
from pisa.brain.models.gateway import CallContext
from pisa.brain.models.pricing import call_cost_usd
from pisa.brain.models.providers import ProviderCallError, build_model, list_models
from pisa.domain.ai import SLOTS, ModelRef
from pisa.domain.errors import PisaError
from pisa.domain.model_caps import SERVER_CAPABILITIES, THINKING_OFF_SLOTS, ModelCaps, features_needing_tools
from tests.conftest import FakeModels, dsn, pair, uvicorn_logging_kept
from tests.gateway_env import gateway, run
from tests.openai_compat_fake import TEST_KEY, FakeServer

HOST = "models.home.test"
SPEC = Server("home-gpu", "Home GPU", f"http://{HOST}:8000/v1", None, "user").spec()
NONE = dict.fromkeys(SERVER_CAPABILITIES, False)
SLOT_IDS = [s.id for s in SLOTS if s.id != "memory.embed"]


def caps(**on: bool) -> dict[str, bool]:
    return {**NONE, **on}


async def probe(fake: FakeServer, model: str = "test-model", key: str | None = None) -> dict[str, bool]:
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        return {str(k): v for k, v in (await probe_model(SPEC, key, model, http)).caps.items()}


# --- the probe, one quirk at a time ---


@pytest.mark.parametrize(
    ("preset", "changes", "model", "expected"),
    [
        ("vllm", {}, "test-model", caps(tools=True, tool_choice=True, json_schema=True, json_object=True)),
        ("vllm-no-tools", {}, "test-model", caps(json_schema=True, json_object=True)),
        ("tools-as-text", {}, "test-model", caps(json_schema=True, json_object=True)),
        ("ollama", {}, "test-model", caps(tools=True, json_schema=True, json_object=True)),
        (
            "ollama",
            {"tool_choice_rejected": True},
            "test-model",
            caps(tools=True, json_schema=True, json_object=True),
        ),
        ("json-object-only", {}, "test-model", caps(json_object=True)),
        (  # b12679 ignores "required": the probe says so
            "llamacpp-jinja",
            {},
            "qwen2.5-7b-instruct-q4_k_m.gguf",
            caps(tools=True, json_schema=True, json_object=True),
        ),
        ("llamacpp", {}, "model.gguf", caps(json_schema=True, json_object=True)),
        (
            "vllm",
            {"vision": True},
            "test-model",
            caps(tools=True, tool_choice=True, json_schema=True, json_object=True, vision=True),
        ),
        ("json-object-only", {"embeddings": True}, "test-model", caps(json_object=True, embeddings=True)),
        ("json-object-only", {"audio": True}, "test-model", caps(json_object=True, audio=True)),
        (
            "llamacpp",
            {"embeddings": True},
            "model.gguf",
            caps(json_schema=True, json_object=True, embeddings=True),
        ),
    ],
)
def test_the_probe_finds_each_quirk(
    preset: str, changes: dict[str, Any], model: str, expected: dict[str, bool]
):
    assert asyncio.run(probe(FakeServer.preset(preset, **changes), model)) == expected


def test_the_probe_sends_tiny_real_requests_and_checks_them_strictly() -> None:
    fake = FakeServer.preset("vllm", embeddings=True)
    asyncio.run(probe(fake))
    kinds = [k for k, _ in fake.requests]
    # tools, json_schema, json_object, tool_choice, vision (chat), audio, embeddings
    assert kinds == ["chat"] * 5 + ["transcriptions", "embeddings"]
    assert fake.requests[5][1] == {"model": "test-model"}
    tools, schema, mode, choice, vision = (body for _, body in fake.requests[:5])
    assert "tool_choice" not in tools and tools["tools"][0]["function"]["name"] == "pisa_probe_add"
    assert choice["tool_choice"] == "required" and choice["messages"][0]["content"] == "Say hello."
    assert schema["response_format"]["type"] == "json_schema"
    assert "pisa_probe_sum" not in schema["messages"][0]["content"]  # only an enforced schema yields the key
    assert mode["response_format"] == {"type": "json_object"}
    assert vision["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert all(body.get("max_tokens") == 256 for _, body in fake.requests[:5])


def test_a_call_written_as_text_or_wrong_arguments_fail_the_tools_probe() -> None:
    def wrong_args(body: dict[str, Any]) -> dict[str, Any] | None:
        if body.get("tools"):
            call = {"name": "pisa_probe_add", "arguments": json.dumps({"a": 2, "b": 4})}
            return {"content": None, "tool_calls": [{"id": "c", "type": "function", "function": call}]}
        return None

    fake = FakeServer.preset("vllm")
    fake.reply = wrong_args
    assert asyncio.run(probe(fake))["tools"] is False


def test_the_probe_sends_the_key_and_never_reports_it() -> None:
    fake = FakeServer.preset("vllm", api_key=TEST_KEY)
    assert asyncio.run(probe(fake, key=TEST_KEY))["tools"] is True
    refused = FakeServer.preset("vllm", api_key=TEST_KEY)
    assert asyncio.run(probe(refused, key="sk-wrong-key-0000")) == NONE


def test_models_come_from_the_servers_list() -> None:
    async def listed(fake: FakeServer) -> list[tuple[str, int | None]]:
        async with httpx.AsyncClient(transport=fake.transport()) as http:
            return [(m.model_id, m.context_tokens) for m in await list_models(SPEC, None, http)]

    assert asyncio.run(listed(FakeServer.preset("vllm", models=("a", "b")))) == [("a", 8192), ("b", 8192)]
    assert asyncio.run(listed(FakeServer.preset("llamacpp"))) == [("model.gguf", 32768)]


# --- capabilities, profile and output modes ---


def test_effective_capabilities_are_the_probe_with_overrides() -> None:
    assert ModelCaps().effective() == NONE  # never probed: nothing assumed
    assert ModelCaps().capabilities() == []
    probed = ModelCaps({"tools": True, "json_object": True}, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    assert probed.capabilities() == ["tools", "structured_output"]
    overridden = ModelCaps(probed.probe, probed.probed_at, {"tools": False, "vision": True})
    assert overridden.effective() == caps(json_object=True, vision=True)
    assert overridden.capabilities() == ["structured_output", "vision"]
    assert ModelCaps(overrides={"audio": True, "embeddings": True}).capabilities() == [
        "embeddings",
        "audio_in",
    ]
    assert features_needing_tools() == ["conversation"]


@pytest.mark.parametrize(
    ("on", "mode"),
    [
        ({"json_schema": True, "tools": True}, "native"),
        ({"tools": True, "tool_choice": True, "json_object": True}, "tool"),
        ({"json_object": True}, "prompted"),
        ({}, "prompted"),
    ],
)
def test_the_profile_follows_the_capabilities(on: dict[str, bool], mode: str) -> None:
    model_caps = ModelCaps(overrides=on)
    profile = compat_profile(model_caps)
    assert model_caps.output_mode() == mode
    assert profile["default_structured_output_mode"] == mode
    assert profile["supports_tools"] is on.get("tools", False)
    assert profile["supports_json_schema_output"] is on.get("json_schema", False)
    assert profile["supports_json_object_output"] is on.get("json_object", False)
    assert profile["supports_forced_tool_choice"] is on.get("tool_choice", False)


class Answer(BaseModel):
    city: str
    count: int


@pytest.mark.parametrize(
    ("on", "output_class", "check"),
    [
        (
            {"json_schema": True},
            "NativeOutput",
            lambda body: body["response_format"]["type"] == "json_schema",
        ),
        (
            {"tools": True, "tool_choice": True},
            "ToolOutput",
            lambda body: body["tool_choice"] not in (None, "auto") and "response_format" not in body,
        ),
        (
            {"json_object": True},
            "PromptedOutput",
            lambda body: body["response_format"] == {"type": "json_object"} and "tools" not in body,
        ),
    ],
)
def test_each_use_gets_its_output_mode_from_the_capabilities(
    on: dict[str, bool], output_class: str, check: Any
):
    fake = FakeServer.preset("vllm")

    def answer(body: dict[str, Any]) -> dict[str, Any] | None:
        if body.get("tools"):
            return None  # the canned tool call fills the output tool's schema
        return {"content": json.dumps({"city": "Springfield", "count": 5})}

    fake.reply = answer

    async def go() -> Answer:
        async with httpx.AsyncClient(transport=fake.transport()) as http:
            model = build_compat_model(SPEC, "test-model", None, ModelCaps(overrides=on), http_client=http)
            spec = output_spec(model, Answer)
            assert type(spec).__name__ == output_class
            result = await Agent(model, output_type=spec).run("Which city, and how many?")
            return result.output

    output = asyncio.run(go())
    assert isinstance(output, Answer) and output.count == 5
    body = fake.requests[-1][1]
    assert check(body), body
    assert "max_completion_tokens" not in body  # every server reads max_tokens


def test_a_built_in_providers_model_keeps_its_output_type() -> None:
    from pydantic_ai.models.test import TestModel

    assert output_spec(TestModel(), Answer) is Answer


# --- the gateway: capability errors and the ledger ---


@pytest.fixture
def server_slot(db: psycopg.Connection) -> None:
    db.execute(
        "INSERT INTO model_servers (id, name, base_url) VALUES ('home-gpu', 'Home GPU', 'http://models.home.test/v1')"
    )
    db.execute("UPDATE model_slots SET model_ref = 'home-gpu:test-model', fallback_ref = NULL")


async def ask(ctx: CallContext) -> str:
    result = await Agent(ctx.model).run("hello")
    ctx.usage.update(result.usage)
    return result.output


@pytest.mark.usefixtures("server_slot")
def test_a_use_needing_a_capability_fails_before_the_call(
    db: psycopg.Connection, secrets_key: str, clock
) -> None:  # type: ignore[no-untyped-def]
    fake = FakeModels()

    async def go(slot: str, tools: bool = False) -> PisaError:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, _, _):
            with pytest.raises(PisaError) as info:
                await gw.run(slot, ask, prompt_chars=40, max_output_tokens=64, tools=tools)
            return info.value

    err = run(lambda: go("chat.conversation"))
    assert (err.code, err.http_status) == ("model_capability_missing", 409)
    assert err.extra == {"capability": "tools", "model": "home-gpu:test-model"}
    assert "home-gpu:test-model" in err.message and "tool calling" in err.message
    assert "never probed" in err.message and "Probe the model" in err.action

    db.execute(
        "INSERT INTO model_capabilities (provider, model_id, probe, probed_at) "
        "VALUES ('home-gpu', 'test-model', '{\"json_object\": true}', now())"
    )
    err = run(lambda: go("chat.conversation"))
    assert err.extra["capability"] == "tools" and "--enable-auto-tool-choice" in err.action
    err = run(lambda: go("briefing.compose", tools=True))  # a use that asks for tools needs them too
    assert err.extra["capability"] == "tools"
    assert fake.calls == []  # no model was even built
    assert db.execute("SELECT count(*) FROM llm_calls").fetchone() == (0,)


@pytest.mark.usefixtures("server_slot")
def test_a_servers_calls_cost_nothing_and_count_their_tokens(
    db: psycopg.Connection, secrets_key: str, clock
) -> None:  # type: ignore[no-untyped-def]
    fake = FakeModels()
    db.execute(
        "INSERT INTO model_capabilities (provider, model_id, overrides) VALUES ('home-gpu', 'test-model', "
        "'{\"tools\": true}')"
    )

    async def go() -> str:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, _, _):
            return await gw.run("chat.conversation", ask, prompt_chars=4000, max_output_tokens=1024)

    assert run(go).startswith("Ciao")
    assert fake.calls == [("home-gpu", "test-model", None)]
    row = db.execute(
        "SELECT status, reserved_eur, cost_eur, price_known, input_tokens > 0, output_tokens > 0 "
        "FROM llm_calls"
    ).fetchone()
    assert row == ("settled", Decimal(0), Decimal(0), True, True, True)

    async def price() -> Decimal:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            return (await call_cost_usd(conn, SPEC, ModelRef("home-gpu", "x"), 10**6, 10**6)).usd

    assert run(price) == 0


def test_the_ollama_setting_is_one_such_server() -> None:
    server = settings_server("http://127.0.0.1:11434/")
    assert server is not None and (server.id, server.base_url, server.source) == (
        "ollama",
        "http://127.0.0.1:11434/v1",
        "settings",
    )
    assert settings_server("http://127.0.0.1:11434/v1").base_url == "http://127.0.0.1:11434/v1"  # type: ignore[union-attr]
    assert server.spec().compatible and not server.spec().needs_key
    assert settings_server(None) is None


# --- TLS with a private CA ---


def _certificates(folder: Path) -> tuple[Path, Path, Path]:
    """A test CA and a server certificate it signs for 127.0.0.1 (generated here, valid one day)."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = dt.datetime.now(dt.UTC)

    def name(text: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(name("Pisa Test CA"))
        .issuer_name(name("Pisa Test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(name("127.0.0.1"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False
        )
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, cert_file, key_file = folder / "ca.pem", folder / "server.pem", folder / "server.key"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return ca_file, cert_file, key_file


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[str, Path, FakeServer]]:
    import uvicorn

    ca_file, cert_file, key_file = _certificates(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fake = FakeServer.preset("vllm")
    with uvicorn_logging_kept():  # log_level="warning" sets uvicorn's loggers for the process: put them back
        config = uvicorn.Config(
            fake.app(),
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(cert_file),
            ssl_keyfile=str(key_file),
            log_level="warning",
            lifespan="off",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        try:
            yield f"https://127.0.0.1:{port}/v1", ca_file, fake
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def test_a_self_signed_server_is_trusted_through_its_ca_bundle(
    tls_server: tuple[str, Path, FakeServer],
) -> None:
    base_url, ca_file, _ = tls_server
    trusted = Server("lab", "Lab", base_url, check_ca_bundle(str(ca_file)), "user").spec()
    untrusted = Server("lab", "Lab", base_url, None, "user").spec()

    async def go() -> tuple[list[str], str, dict[str, bool]]:
        async with httpx.AsyncClient() as shared:
            listed = [m.model_id for m in await list_models(trusted, None, http_for(trusted, shared))]
            with pytest.raises(ProviderCallError) as refused:
                await list_models(untrusted, None, http_for(untrusted, shared))
            found = await probe_model(trusted, None, "test-model", http_for(trusted, shared))
            await http_for(trusted, shared).aclose()
            # the model's own client trusts the bundle too
            model = build_model(trusted, "test-model", None, ModelCaps())
            said = (await Agent(model).run("hello")).output
        return listed, refused.value.message + "|" + said, {str(k): v for k, v in found.caps.items()}

    listed, refused, found = asyncio.run(go())
    assert refused.endswith("|Hello!")
    assert listed == ["test-model"]
    assert "TLS certificate is not trusted" in refused
    assert found == caps(tools=True, tool_choice=True, json_schema=True, json_object=True)


def test_a_ca_bundle_must_be_a_readable_pem_file(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    for path in ("relative/ca.pem", str(tmp_path / "missing.pem"), str(junk)):
        with pytest.raises(PisaError) as info:
            check_ca_bundle(path)
        assert info.value.code == "validation_failed"
    assert check_ca_bundle("  ") is None
    with pytest.raises(ssl.SSLError):
        ssl.create_default_context(cafile=str(junk))


# --- the REST endpoints ---


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def test_a_server_through_the_api(client, auth, db, http_handler) -> None:  # type: ignore[no-untyped-def]
    fake = FakeServer.preset("ollama", models=("qwen3:8b", "nomic-embed-text"), api_key=TEST_KEY)
    to_fake = fake.handler()

    async def network(request: httpx.Request) -> httpx.Response:
        if request.url.host == HOST:
            return await to_fake(request)
        raise httpx.ConnectError("offline", request=request)

    http_handler["handler"] = network
    created = client.post(
        "/v1/ai/providers",
        headers=auth,
        json={
            "type": "openai_compatible",
            "id": "home-gpu",
            "name": "Home GPU",
            "base_url": f"http://{HOST}/v1/",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json() == {
        "id": "home-gpu",
        "name": "Home GPU",
        "type": "openai_compatible",
        "key_set": False,
        "key_last4": None,
        "last_test": None,
        "keys_url": None,
        "base_url": f"http://{HOST}/v1",
        "ca_bundle": None,
        "editable": True,
        "read_timeout_s": 60,
        "thinking": {**dict.fromkeys(SLOT_IDS, True), **dict.fromkeys(THINKING_OFF_SLOTS, False)},
    }
    again = client.post(
        "/v1/ai/providers",
        headers=auth,
        json={"type": "openai_compatible", "id": "home-gpu", "base_url": "http://x/v1"},
    )
    assert again.status_code == 400
    builtin = client.post(
        "/v1/ai/providers",
        headers=auth,
        json={"type": "openai_compatible", "id": "openai", "base_url": "http://x/v1"},
    )
    assert builtin.status_code == 400 and builtin.json()["error"]["code"] == "validation_failed"

    # the server wants a key: refused without, listed with
    refused = client.post("/v1/ai/providers/home-gpu/test", headers=auth).json()
    assert refused == {"ok": False, "message": "Home GPU rejected the key (HTTP 401).", "models_found": 0}
    assert (
        client.put("/v1/ai/providers/home-gpu/key", headers=auth, json={"key": TEST_KEY}).status_code == 204
    )
    tested = client.post("/v1/ai/providers/home-gpu/test", headers=auth).json()
    assert tested == {"ok": True, "message": "2 models are available.", "models_found": 2}
    client.portal.call(client.app.state.runtime.ai.wait_background)  # the refresh a good test starts
    listed = {p["id"]: p for p in client.get("/v1/ai/providers", headers=auth).json()["providers"]}
    assert listed["home-gpu"]["key_set"] is True and listed["home-gpu"]["last_test"]["ok"] is True

    # never probed: everything false, nothing assumed
    path = "/v1/ai/providers/home-gpu/capabilities"
    unprobed = client.get(path, headers=auth, params={"model": "qwen3:8b"}).json()
    assert unprobed == {
        "model": "qwen3:8b",
        "probed_at": None,
        **NONE,
        "overrides": {},
        "features_needing_tools": ["conversation"],
        "thinking": False,
        "context_tokens": None,
        "server": None,
        "truncated": None,
    }
    several = client.get(path, headers=auth)  # two models: the request must name one
    assert several.status_code == 400

    probed = client.post("/v1/ai/providers/home-gpu/probe", headers=auth, params={"model": "qwen3:8b"}).json()
    assert probed["probed_at"] is not None
    assert {k: probed[k] for k in SERVER_CAPABILITIES} == caps(tools=True, json_schema=True, json_object=True)
    assert client.get(path, headers=auth, params={"model": "qwen3:8b"}).json() == probed

    # the catalog lists it at no price with the probe's capabilities; a slot can take it
    models = {m["ref"]: m for m in client.get("/v1/ai/models", headers=auth).json()["models"]}
    qwen = models["home-gpu:qwen3:8b"]
    assert (qwen["capabilities"], qwen["price_eur_per_mtok"], qwen["key_set"]) == (
        ["tools", "structured_output"],
        {"input": 0.0, "output": 0.0},
        True,
    )
    assert models["home-gpu:nomic-embed-text"]["capabilities"] == []
    slot = client.put(
        "/v1/ai/slots/chat.conversation", headers=auth, json={"model": "home-gpu:qwen3:8b", "fallback": None}
    )
    assert slot.status_code == 200, slot.text

    # overrides: set by hand, reported apart, applied to the effective values and the catalog
    overridden = client.put(
        path + "/overrides", headers=auth, params={"model": "qwen3:8b"}, json={"tools": False, "vision": True}
    ).json()
    assert overridden["overrides"] == {"tools": False, "vision": True}
    assert (overridden["tools"], overridden["vision"], overridden["json_schema"]) == (False, True, True)
    refused_slot = client.put(
        "/v1/ai/slots/chat.conversation", headers=auth, json={"model": "home-gpu:qwen3:8b", "fallback": None}
    )
    assert refused_slot.json()["error"]["code"] == "model_incompatible"
    bad = client.put(
        path + "/overrides", headers=auth, params={"model": "qwen3:8b"}, json={"telepathy": True}
    )
    assert bad.status_code == 400
    cleared = client.put(path + "/overrides", headers=auth, params={"model": "qwen3:8b"}, json={}).json()
    assert cleared["overrides"] == {} and cleared["tools"] is True

    # built-in providers are not probed
    assert client.get("/v1/ai/providers/openai/capabilities", headers=auth).status_code == 400
    assert client.get("/v1/ai/providers/nope/capabilities", headers=auth).status_code == 404

    # a new address forgets the probe; a CA bundle that isn't a file is refused
    changed = client.patch(
        "/v1/ai/providers/home-gpu", headers=auth, json={"base_url": f"http://{HOST}:9000/v1"}
    )
    assert changed.status_code == 200 and changed.json()["base_url"] == f"http://{HOST}:9000/v1"
    assert client.get(path, headers=auth, params={"model": "qwen3:8b"}).json()["probed_at"] is None
    bad_ca = client.patch(
        "/v1/ai/providers/home-gpu", headers=auth, json={"ca_bundle": "/nonexistent/ca.pem"}
    )
    assert bad_ca.status_code == 400

    # removed with its key, probes and models
    assert client.delete("/v1/ai/providers/home-gpu", headers=auth).status_code == 204
    for table in ("model_servers", "model_capabilities", "provider_keys"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM model_catalog WHERE provider = 'home-gpu'").fetchone() == (0,)
    assert client.delete("/v1/ai/providers/home-gpu", headers=auth).status_code == 404


def test_the_ollama_setting_is_listed_and_not_editable(app_settings, db, fake_models, clock, auth) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    settings = app_settings.model_copy(update={"ollama_base_url": "http://127.0.0.1:11434"})

    async def factory(s):  # type: ignore[no-untyped-def]
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        return await Runtime.start(s, model_factory=fake_models, http_transport=transport, clock=clock)

    with TestClient(create_app(settings, runtime_factory=factory)) as c:
        listed = {p["id"]: p for p in c.get("/v1/ai/providers", headers=auth).json()["providers"]}
        patched = c.patch("/v1/ai/providers/ollama", headers=auth, json={"name": "Mine"})
        taken = c.post(
            "/v1/ai/providers",
            headers=auth,
            json={"type": "openai_compatible", "id": "ollama", "base_url": "http://x/v1"},
        )
    ollama = listed["ollama"]
    assert (ollama["type"], ollama["base_url"], ollama["editable"]) == (
        "openai_compatible",
        "http://127.0.0.1:11434/v1",
        False,
    )
    assert patched.status_code == 400 and "PISA_OLLAMA_BASE_URL" in patched.json()["error"]["message"]
    assert taken.status_code == 400
