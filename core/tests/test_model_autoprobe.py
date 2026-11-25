"""Automatic probing of the models on the user's own OpenAI-compatible servers: once when a slot is pointed at
a never-probed model (the app and `pisa models set`), and once on a never-probed model's first missing
capability, with one more check after it. A model probed before keeps its answer; a server that can't be
reached leaves the error standing, saying how to probe by hand; two misses at once run one probe."""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from pydantic_ai import Agent

import pisa.brain.models.gateway as gateway_module
from pisa.brain.models.gateway import CallContext
from pisa.domain.errors import PisaError
from tests.conftest import FakeModels, dsn, pair, seed_catalog
from tests.gateway_env import gateway, run
from tests.openai_compat_fake import FakeServer

HOST = "models.home.test"
REF = "home-gpu:test-model"


class Network:
    """The network as the tests see it: the fake server at HOST (or nothing, when it is down), each request
    to it recorded by its path."""

    def __init__(self, fake: FakeServer | None, delay_s: float = 0.0) -> None:
        self.fake = fake
        self.delay_s = delay_s
        self.paths: list[str] = []
        self._to_fake = fake.handler() if fake is not None else None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != HOST:
            raise httpx.ConnectError("offline", request=request)
        self.paths.append(request.url.path)
        if self._to_fake is None:
            raise httpx.ConnectError("the server is down", request=request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)  # a slow server: a probe takes a while
        return await self._to_fake(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def probes(self) -> int:
        """How many probes reached the server: each one starts with a single tool-calling request (the use's
        own calls go to a fake model, never here)."""
        requests = self.fake.requests if self.fake is not None else []
        return sum(
            1 for kind, body in requests if kind == "chat" and body.get("tools") and "tool_choice" not in body
        )


def _server(db: psycopg.Connection) -> None:
    db.execute(
        "INSERT INTO model_servers (id, name, base_url) VALUES ('home-gpu', 'Home GPU', %s)",
        (f"http://{HOST}/v1",),
    )
    seed_catalog(db, [{"ref": REF, "capabilities": [], "input": 0, "output": 0, "source": "local"}])
    db.execute("UPDATE model_slots SET model_ref = NULL, fallback_ref = NULL")


def _probed_at(db: psycopg.Connection) -> Any:
    row = db.execute(
        "SELECT probed_at FROM model_capabilities WHERE provider = 'home-gpu' AND model_id = 'test-model'"
    ).fetchone()
    return row[0] if row else None


def _slot(db: psycopg.Connection, slot: str = "chat.conversation") -> tuple[str | None, str | None]:
    row = db.execute("SELECT model_ref, fallback_ref FROM model_slots WHERE id = %s", (slot,)).fetchone()
    assert row is not None
    return row[0], row[1]


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


# --- on assignment: the app ---


def test_setting_a_slot_probes_a_never_probed_model_once(client, auth, db, http_handler) -> None:  # type: ignore[no-untyped-def]
    _server(db)
    network = Network(FakeServer.preset("vllm"))
    http_handler["handler"] = network
    put = client.put("/v1/ai/slots/chat.conversation", headers=auth, json={"model": REF, "fallback": None})
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["model"] == REF
    assert body["probes"] == [
        {
            "model": REF,
            "probed": True,
            "capabilities": ["tools", "structured_output"],
            "message": f"Probed {REF}: tools, structured_output.",
        }
    ]
    assert _probed_at(db) is not None and network.probes() == 1
    assert db.execute("SELECT capabilities FROM model_catalog WHERE ref = %s", (REF,)).fetchone() == (
        ["tools", "structured_output"],
    )

    # probed already: setting it again (here as another slot's fallback too) probes nothing
    seen = len(network.paths)
    again = client.put("/v1/ai/slots/briefing.compose", headers=auth, json={"model": REF, "fallback": REF})
    assert again.status_code == 200, again.text
    assert again.json()["probes"] == [] and len(network.paths) == seen


def test_a_probed_model_that_lacks_what_the_slot_needs_is_refused(client, auth, db, http_handler) -> None:  # type: ignore[no-untyped-def]
    _server(db)
    http_handler["handler"] = Network(FakeServer.preset("json-object-only"))
    put = client.put("/v1/ai/slots/chat.conversation", headers=auth, json={"model": REF, "fallback": None})
    assert put.status_code == 400, put.text
    assert put.json()["error"]["code"] == "model_incompatible"
    assert _probed_at(db) is not None  # the probe's answer is kept
    assert _slot(db) == (None, None)


def test_a_slot_is_set_even_when_the_server_is_down(client, auth, db, http_handler) -> None:  # type: ignore[no-untyped-def]
    _server(db)
    network = Network(None)
    http_handler["handler"] = network
    put = client.put("/v1/ai/slots/chat.conversation", headers=auth, json={"model": REF, "fallback": None})
    assert put.status_code == 200, put.text
    (probe,) = put.json()["probes"]
    assert (probe["model"], probe["probed"], probe["capabilities"]) == (REF, False, [])
    assert "Home GPU could not be reached to probe" in probe["message"]
    assert f"pisa models probe {REF}" in probe["message"] and "Settings → AI providers" in probe["message"]
    assert _slot(db) == (REF, None) and _probed_at(db) is None
    assert network.paths == ["/v1/models"]  # the reachability check, and no probe request


def test_a_built_in_providers_model_is_never_probed(client, auth, db, http_handler, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import set_key

    set_key(db, secrets_key)
    seed_catalog(db, [{"ref": "openai:gpt-test", "capabilities": ["tools", "structured_output"]}])
    seen: list[str] = []

    def network(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        raise httpx.ConnectError("offline", request=request)

    http_handler["handler"] = network
    put = client.put(
        "/v1/ai/slots/chat.conversation", headers=auth, json={"model": "openai:gpt-test", "fallback": None}
    )
    assert put.status_code == 200, put.text
    assert put.json()["probes"] == [] and seen == []
    assert db.execute("SELECT count(*) FROM model_capabilities").fetchone() == (0,)


# --- on assignment: the command line ---


@pytest.fixture
def cli(test_database_url: str, secrets_key: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pisa.settings import get_settings

    monkeypatch.setenv("PISA_DATABASE_URL", test_database_url)
    monkeypatch.setenv("PISA_SECRETS_KEY", secrets_key)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_models_set_probes_a_never_probed_model(
    cli: None, db: psycopg.Connection, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pisa import cli as pisa_cli

    _server(db)
    network = Network(FakeServer.preset("ollama"))
    monkeypatch.setattr(pisa_cli, "_http_client", network.client)
    assert pisa_cli.main(["models", "set", "chat.conversation", REF]) == 0
    out = capsys.readouterr().out
    assert f"chat.conversation now uses {REF}." in out
    assert f"Probed {REF}: tools, structured_output." in out
    assert _probed_at(db) is not None and network.probes() == 1

    assert pisa_cli.main(["models", "set", "intake.triage", REF]) == 0  # probed: not again
    assert "Probed" not in capsys.readouterr().out and network.probes() == 1

    # by hand: always probes, even a model probed before
    assert pisa_cli.main(["models", "probe", REF]) == 0
    assert f"Probed {REF}: tools, json_schema, json_object" in capsys.readouterr().out
    assert network.probes() == 2


def test_models_set_says_how_to_probe_when_the_server_is_down(
    cli: None, db: psycopg.Connection, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pisa import cli as pisa_cli

    _server(db)
    monkeypatch.setattr(pisa_cli, "_http_client", Network(None).client)
    assert pisa_cli.main(["models", "set", "chat.conversation", REF]) == 0
    out = capsys.readouterr().out
    assert f"chat.conversation now uses {REF}." in out
    assert "could not be reached" in out and f"pisa models probe {REF}" in out
    assert _slot(db) == (REF, None) and _probed_at(db) is None
    assert pisa_cli.main(["models", "probe", REF]) == 1
    assert "Home GPU is unreachable" in capsys.readouterr().err


# --- on the first missing capability: the gateway ---


async def ask(ctx: CallContext) -> str:
    result = await Agent(ctx.model).run("hello")
    ctx.usage.update(result.usage)
    return result.output


@pytest.fixture
def server_slot(db: psycopg.Connection) -> None:
    _server(db)
    db.execute("UPDATE model_slots SET model_ref = %s WHERE id = 'chat.conversation'", (REF,))


def _call(
    db: psycopg.Connection, secrets_key: str, clock: Any, network: Network, models: FakeModels, n: int = 1
):  # type: ignore[no-untyped-def]
    async def go() -> list[Any]:
        async with network.client() as http, gateway(dsn(db), secrets_key, models, clock, http) as (gw, _, _):
            calls = [
                gw.run("chat.conversation", ask, prompt_chars=40, max_output_tokens=64) for _ in range(n)
            ]
            return await asyncio.gather(*calls, return_exceptions=True)

    return run(go)


@pytest.mark.usefixtures("server_slot")
def test_a_first_miss_probes_the_model_and_the_call_goes_ahead(
    db: psycopg.Connection, secrets_key: str, clock: Any
) -> None:
    network, models = Network(FakeServer.preset("vllm")), FakeModels()
    (answer,) = _call(db, secrets_key, clock, network, models)
    assert isinstance(answer, str) and answer.startswith("Ciao")
    assert models.calls == [("home-gpu", "test-model", None)]
    assert _probed_at(db) is not None and network.probes() == 1
    (again,) = _call(db, secrets_key, clock, network, models)  # probed: no probe before the next call
    assert isinstance(again, str) and network.probes() == 1


@pytest.mark.usefixtures("server_slot")
def test_the_error_stands_when_the_probe_confirms_the_capability_is_missing(
    db: psycopg.Connection, secrets_key: str, clock: Any
) -> None:
    network, models = Network(FakeServer.preset("json-object-only")), FakeModels()
    (err,) = _call(db, secrets_key, clock, network, models)
    assert isinstance(err, PisaError) and err.code == "model_capability_missing"
    assert err.extra == {"capability": "tools", "model": REF}
    assert "never probed" not in err.message and "--enable-auto-tool-choice" in err.action
    assert _probed_at(db) is not None and network.probes() == 1 and models.calls == []

    # a probed model keeps its answer: the next miss probes nothing
    (err,) = _call(db, secrets_key, clock, network, models)
    assert isinstance(err, PisaError) and err.code == "model_capability_missing"
    assert network.probes() == 1
    assert db.execute("SELECT count(*) FROM llm_calls").fetchone() == (0,)


@pytest.mark.usefixtures("server_slot")
def test_a_server_that_cant_be_reached_leaves_the_error_with_how_to_probe(
    db: psycopg.Connection, secrets_key: str, clock: Any
) -> None:
    network, models = Network(None), FakeModels()
    (err,) = _call(db, secrets_key, clock, network, models)
    assert isinstance(err, PisaError) and err.code == "model_capability_missing"
    assert err.extra == {"capability": "tools", "model": REF}
    assert err.message == (
        f"{REF} on Home GPU can't be used for the conversation: it lacks tool calling. "
        "The model was never probed, and the server could not be reached for probing: "
        "Home GPU is unreachable (ConnectError)."
    )
    assert f"pisa models probe {REF}" in err.action and "Settings → AI providers" in err.action
    assert _probed_at(db) is None and network.paths == ["/v1/models"] and models.calls == []


@pytest.mark.usefixtures("server_slot")
def test_two_misses_at_once_run_one_probe(
    db: psycopg.Connection, secrets_key: str, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    probe_unprobed = gateway_module.probe_unprobed

    async def counted(*args: Any, **kwargs: Any) -> Any:
        asked.append(args[3])
        return await probe_unprobed(*args, **kwargs)

    monkeypatch.setattr(gateway_module, "probe_unprobed", counted)
    network, models = Network(FakeServer.preset("vllm"), delay_s=0.05), FakeModels()
    answers = _call(db, secrets_key, clock, network, models, n=2)
    assert all(isinstance(a, str) and a.startswith("Ciao") for a in answers), answers
    assert asked == ["test-model", "test-model"]  # both missed while the model was unprobed
    assert network.probes() == 1 and network.paths.count("/v1/models") == 1  # one ran, the other waited
    assert len(models.calls) == 2


def test_a_built_in_providers_model_is_never_probed_by_the_gateway(
    db: psycopg.Connection, secrets_key: str, clock: Any
) -> None:
    from tests.conftest import set_key

    set_key(db, secrets_key)
    seed_catalog(
        db,
        [{"ref": "openai:gpt-test", "capabilities": ["tools", "structured_output"], "input": 1, "output": 2}],
    )
    db.execute("UPDATE model_slots SET model_ref = 'openai:gpt-test' WHERE id = 'chat.conversation'")
    network, models = Network(FakeServer.preset("vllm")), FakeModels()
    (answer,) = _call(db, secrets_key, clock, network, models)
    assert isinstance(answer, str) and network.paths == []
    assert db.execute("SELECT count(*) FROM model_capabilities").fetchone() == (0,)
