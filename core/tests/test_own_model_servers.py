"""LOCAL-1 L1: the operator's own model servers, against the quirks L0 measured on llama.cpp and Ollama
(`scripts/local-models/findings.md`): a round that may use no tool, leading blank lines, Ollama's silent cut,
the read timeout and no SDK retries, the probe on thinking models, llama.cpp's error bodies, thinking off per
slot, embeddings on an own server, and `pisa setup --ai own` filling every slot it can."""

import asyncio
import io
import json
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import ModelRequestParameters

from pisa.brain.models import own_wire
from pisa.brain.models.compat import (
    Server,
    _Lead,
    build_compat_model,
    cut_notice_once,
    cut_of,
    for_slot,
    probe_model,
)
from pisa.brain.models.gateway import CallContext, map_provider_error
from pisa.brain.spoken import MAX_ROUNDS, Loop, Round
from pisa.domain.ai import ModelRef
from pisa.domain.model_caps import ModelCaps
from tests.conftest import dsn
from tests.gateway_env import gateway, run
from tests.openai_compat_fake import FakeServer, capture

HOST = "models.home.test"
SPEC = Server("home-gpu", "Home GPU", f"http://{HOST}:8000/v1", None, "user").spec()
TOOLS_ON = ModelCaps(overrides={"tools": True, "json_object": True})


@pytest.fixture(autouse=True)
def _no_cuts() -> Any:
    own_wire.reset()
    yield
    own_wire.reset()


def model_on(
    fake: FakeServer, caps: ModelCaps = TOOLS_ON, spec: Any = SPEC, model: str = "test-model"
) -> Any:
    return build_compat_model(
        spec, model, None, caps, http_client=httpx.AsyncClient(transport=fake.transport())
    )


def weather_agent(model: Any) -> tuple[Agent[None, str], list[str]]:
    ran: list[str] = []
    agent: Agent[None, str] = Agent(model)

    @agent.tool_plain
    def get_weather(city: str) -> str:
        """Get the current weather in a city."""
        ran.append(city)
        return "sunny, 21 C"

    return agent, ran


async def streamed(agent: Agent[None, str], prompt: str, **settings: Any) -> str:
    out: list[str] = []
    async with agent.run_stream(prompt, model_settings=settings or None) as result:  # type: ignore[arg-type]
        async for delta in result.stream_text(delta=True, debounce_by=None):
            out.append(delta)
    return "".join(out)


# --- 1. a round that may use no tool is sent without tools; markup and stray calls never get through ---


@pytest.mark.parametrize("preset", ["llamacpp-jinja", "ollama-real"])
def test_a_no_tool_round_sends_no_tools_and_says_no_markup(preset: str) -> None:
    fake = FakeServer.preset(preset, models=("test-model",))

    async def go() -> tuple[str, list[str]]:
        agent, ran = weather_agent(model_on(fake))
        return await streamed(agent, "Use get_weather for Paris.", tool_choice="none"), ran

    said, ran = asyncio.run(go())
    body = fake.requests[-1][1]
    assert "tools" not in body and "tool_choice" not in body and "parallel_tool_calls" not in body
    assert ran == [] and "<tool_call>" not in said


@pytest.mark.parametrize("none_choice", ["text", "call"])
def test_a_server_that_still_calls_or_writes_the_call_is_refused(none_choice: str) -> None:
    """The backstop, if a server answered a no-tool round with a call anyway (Ollama) or its markup
    (llama.cpp): the call is refused and the markup is never text, streamed or not."""
    fake = FakeServer.preset("vllm", none_choice=none_choice)

    def call_anyway(body: dict[str, Any]) -> dict[str, Any] | None:
        if none_choice == "text":
            return {"content": 'Checking.<tool_call>\n{"name": "get_weather", "arguments": {}}\n</tool_call>'}
        call = {"name": "get_weather", "arguments": '{"city":"Paris"}'}
        return {
            "content": "Checking.",
            "tool_calls": [{"id": "call_1", "type": "function", "function": call}],
        }

    fake.reply = call_anyway

    async def go() -> tuple[str, str, list[str]]:
        model = model_on(fake)
        agent, ran = weather_agent(model)
        text = await streamed(agent, "Weather in Paris?", tool_choice="none")
        whole = (await agent.run("Weather in Paris?", model_settings={"tool_choice": "none"})).output
        return text, whole, ran

    text, whole, ran = asyncio.run(go())
    assert ran == []
    assert text == "Checking." and whole == "Checking."


def test_the_round_caps_last_call_on_an_own_server_has_no_tools() -> None:
    """spoken.py's last round (`tool_choice: "none"` past MAX_ROUNDS) reaches an own server without tools."""
    fake = FakeServer.preset("ollama-real", models=("test-model",))

    async def go() -> str:
        loop = Round(rounds=MAX_ROUNDS)
        agent, ran = weather_agent(Loop(model_on(fake), loop))
        said = await streamed(agent, "Use get_weather for Paris.")
        assert ran == []
        return said

    asyncio.run(go())
    assert all("tools" not in body for _, body in fake.requests)


@pytest.mark.parametrize(
    ("pieces", "said"),
    [
        (["Ciao <tool", '_call>{"name": "x"}</tool', "_call> a te."], "Ciao  a te."),
        (["a < b and ", "<", "b > a"], "a < b and <b > a"),
        (['[TOOL_CALLS] [{"name": "x"}]'], ""),
        (['Fine.<function=get_weather>{"city": "Rome"}</function>'], "Fine."),
        (['Open <tool_call>{"name"'], "Open "),
    ],
)
def test_markup_is_taken_out_of_streamed_text(pieces: list[str], said: str) -> None:
    markup = own_wire.Markup()
    assert "".join(markup.feed(p) for p in pieces) + markup.flush() == said


# --- 2. leading blank lines never end a tool turn, never spoken ---


def _completed(events: list[Any], middle: list[Any]) -> list[Any]:
    """A capture's stream with its cut middle ("<n events cut>") filled in."""
    at = next(i for i, e in enumerate(events) if isinstance(e, str) and e.endswith("events cut>"))
    return [*events[:at], *middle, *events[at + 1 :]]


def test_blank_lines_after_thinking_do_not_end_the_tool_turn() -> None:
    """llama.cpp --reasoning-format none, captured: `<think>…</think>`, then "\\n\\n", then the call. The
    blank lines are not the turn's answer: the tool runs and its result is read."""
    first, follow = capture("llama.cpp-reasoning-none-qwen3-0.6b-q8/c")[2:4]
    chunk = first["sse"][1]

    def delta(**d: Any) -> dict[str, Any]:
        return {**chunk, "choices": [{"finish_reason": None, "index": 0, "delta": d}]}

    middle = [
        delta(content=" is get_weather."),
        delta(content="</think>"),
        delta(content="\n\n"),
        delta(tool_calls=[{"index": 0, "id": "Y7HzVnHnmKqokLCt7hQY3NpPWCaixIR0", "type": "function",
                           "function": {"name": "get_weather", "arguments": "{\""}}]),
    ]  # fmt: skip
    answer = [delta(content="."), delta(content="</think>"), delta(content="\n\n"),
              delta(content="It is sunny in Paris, a good"), delta(content=" day for a walk; a")]  # fmt: skip
    fake = FakeServer.preset("llamacpp-reasoning-none", models=("test-model",))
    fake.replay = [_completed(first["sse"], middle), _completed(follow["sse"], answer)]

    async def go() -> tuple[str, list[str]]:
        agent, ran = weather_agent(model_on(fake))
        return await streamed(agent, "What is the weather in Paris right now? Use the tool."), ran

    said, ran = asyncio.run(go())
    assert ran == ["Paris"]
    assert said == "It is sunny in Paris, a good day for a walk; a day to be outdoors!"


def test_leading_blank_lines_are_dropped_after_thinking_only() -> None:
    lead = _Lead()
    assert lead.feed("\n\n") == "" and lead.feed("\nHello\n\nthere") == "Hello\n\nthere"
    lead = _Lead()
    assert lead.feed("<think>a</think>\n\n Yes") == "<think>a</think>Yes"
    fake = FakeServer.preset("llamacpp-reasoning-none", models=("test-model",))

    async def go() -> str:
        return (await Agent(model_on(fake)).run("Say hello.")).output

    assert asyncio.run(go()) == "Hello!"


def test_the_profile_ignores_streamed_leading_whitespace() -> None:
    from pisa.brain.models.compat import compat_profile

    assert compat_profile(ModelCaps())["ignore_streamed_leading_whitespace"] is True


# --- 3. Ollama's silent cut ---


LONG = "Record {n}: the lamp in room {n} is green and stays on.\n"


def long_prompt(tokens: int) -> str:
    return "".join(LONG.format(n=f"{i:05d}") for i in range(tokens * 4 // len(LONG.format(n="00000")) + 1))


@pytest.mark.parametrize("stream", [True, False])
def test_a_prompt_the_server_cut_is_seen_and_told_once(stream: bool) -> None:
    fake = FakeServer.preset("ollama-real", models=("test-model",))

    async def go() -> Any:
        model = model_on(fake)
        if stream:
            await streamed(Agent(model), long_prompt(8000))
        else:
            await Agent(model).run(long_prompt(8000))
        return model

    model = asyncio.run(go())
    cut = cut_of(model)
    assert cut is not None and cut["read_tokens"] == 2050 and cut["sent_tokens"] > 7000
    assert own_wire.cuts()["home-gpu:test-model"]["read_tokens"] == 2050  # /readyz
    assert cut_notice_once(model, "en-US") == own_wire.CUT_NOTICE["en-US"]
    assert cut_notice_once(model, "en-US") is None  # once


def test_a_prompt_within_the_context_is_not_a_cut() -> None:
    fake = FakeServer.preset("ollama-real", models=("test-model",))

    async def go() -> Any:
        model = model_on(fake)
        await Agent(model).run(long_prompt(3000))
        return model

    model = asyncio.run(go())
    assert cut_of(model) is None and cut_notice_once(model, "it-IT") is None and own_wire.cuts() == {}


def test_readyz_lists_the_cuts(client) -> None:  # type: ignore[no-untyped-def]
    own_wire.record_cut("home-gpu:test-model", {"sent_tokens": 9000, "read_tokens": 2050})
    body = client.get("/readyz").json()
    assert body["model_truncations"]["home-gpu:test-model"]["read_tokens"] == 2050


# --- 4/5. the read timeout, no SDK retries, a timeout worded as one ---


def test_the_servers_read_timeout_is_every_requests_and_the_sdk_never_retries() -> None:
    attempts: list[float] = []

    def hang(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content).get("max_tokens") or 0)
        raise httpx.ReadTimeout("no byte", request=request)

    spec = Server("home-gpu", "Home GPU", f"http://{HOST}/v1", None, "user", 300).spec()
    http = httpx.AsyncClient(transport=httpx.MockTransport(hang))
    model = build_compat_model(spec, "test-model", None, TOOLS_ON, http_client=http)
    assert model.read_timeout_s == 300  # type: ignore[attr-defined]
    settings, _ = model.prepare_request({"timeout": 20.0}, ModelRequestParameters())
    assert (settings or {}).get("timeout") == 300  # the conversation's 20 s does not apply to an own server

    async def go() -> Exception:
        try:
            await Agent(model).run("hello", model_settings={"timeout": 20.0})
        except Exception as exc:
            return exc
        raise AssertionError("no timeout")

    exc = asyncio.run(go())
    assert len(attempts) == 1  # one attempt, not three
    err = map_provider_error(exc, spec, ModelRef("home-gpu", "test-model"), None)
    assert err.message == "Home GPU did not answer within 300 s."
    assert "--read-timeout" in err.action and err.detail == "timeout"
    stalled = map_provider_error(httpx.ReadTimeout("x"), spec, ModelRef("home-gpu", "m"), None)
    assert stalled.message == "Home GPU did not answer within 300 s."
    down = map_provider_error(httpx.ConnectError("x"), spec, ModelRef("home-gpu", "m"), None)
    assert "did not answer" not in down.message


# --- 6. the probe on thinking models ---


@pytest.mark.parametrize(
    ("preset", "server", "ctx"),
    [("llamacpp-thinking", "llama.cpp", 32768), ("ollama-thinking", "ollama", 4096)],
)
def test_a_thinking_model_is_probed_with_room_to_think(preset: str, server: str, ctx: int) -> None:
    fake = FakeServer.preset(preset)
    model = fake.quirks.models[0]

    async def go() -> Any:
        async with httpx.AsyncClient(transport=fake.transport()) as http:
            return await probe_model(SPEC, None, model, http)

    found = asyncio.run(go())
    assert found.caps["tools"] and found.caps["json_schema"] and found.caps["json_object"]
    assert found.facts == {"thinking": True, "server": server, "context_tokens": ctx}
    sizes = [body.get("max_tokens") for kind, body in fake.requests if kind == "chat"]
    assert 256 in sizes and 2048 in sizes  # cut while thinking at 256, then once more with room


def test_a_model_that_doesnt_think_is_probed_once_per_capability() -> None:
    fake = FakeServer.preset("llamacpp-jinja")

    async def go() -> Any:
        async with httpx.AsyncClient(transport=fake.transport()) as http:
            return await probe_model(SPEC, None, "qwen2.5-7b-instruct-q4_k_m.gguf", http)

    found = asyncio.run(go())
    assert found.facts == {"thinking": False, "server": "llama.cpp", "context_tokens": 32768}
    assert all(body.get("max_tokens") == 256 for kind, body in fake.requests if kind == "chat")


# --- 7. llama.cpp's error bodies ---


def test_llamacpp_errors_are_worded_from_their_bodies() -> None:
    fake = FakeServer.preset("llamacpp-jinja", ctx=2048, models=("test-model",))
    no_jinja = FakeServer.preset("llamacpp", models=("test-model",))

    async def failure(server: FakeServer, prompt: str) -> Exception:
        agent, _ = weather_agent(model_on(server))
        try:
            await agent.run(prompt)
        except ModelHTTPError as exc:
            return exc
        raise AssertionError("no error")

    ref = ModelRef("home-gpu", "test-model")
    big = map_provider_error(asyncio.run(failure(fake, long_prompt(5000))), SPEC, ref, None)
    assert big.detail == "context_exceeded" and "its context is 2048" in big.message
    assert "Record" not in big.message and "--ctx-size" in big.action
    jinja = map_provider_error(asyncio.run(failure(no_jinja, "hello")), SPEC, ref, None)
    assert jinja.detail == "needs_jinja" and "--jinja" in jinja.message


# --- 8. thinking off per slot ---


@pytest.mark.parametrize(
    ("preset", "off"),
    [
        ("llamacpp-thinking", {"chat_template_kwargs": {"enable_thinking": False}}),
        ("ollama-thinking", {"reasoning_effort": "none", "think": False}),
    ],
)
def test_the_fast_slots_ask_the_server_not_to_think(preset: str, off: dict[str, Any]) -> None:
    fake = FakeServer.preset(preset, models=("test-model",))
    server = "ollama" if fake.quirks.ollama else "llama.cpp"
    caps = ModelCaps(overrides={"json_object": True}, facts={"thinking": True, "server": server})

    async def ask(slot: str, spec: Any = SPEC) -> dict[str, Any]:
        model = model_on(fake, caps, spec)
        for_slot(model, slot, spec)
        await Agent(model).run("hello")
        return fake.requests[-1][1]

    reflex = asyncio.run(ask("conversation.reflex"))
    assert {k: reflex[k] for k in off} == off
    conversation = asyncio.run(ask("chat.conversation"))
    assert not set(off) & set(conversation)
    turned_off = Server(
        "home-gpu", "Home GPU", SPEC.base_url, None, "user", 60, (("chat.conversation", False),)
    )
    chosen = asyncio.run(ask("chat.conversation", turned_off.spec()))
    assert {k: chosen[k] for k in off} == off


def test_a_model_that_doesnt_think_gets_no_thinking_fields() -> None:
    fake = FakeServer.preset("llamacpp-jinja", models=("test-model",))

    async def go() -> dict[str, Any]:
        model = model_on(fake, ModelCaps(overrides={"json_object": True}))
        for_slot(model, "conversation.reflex", SPEC)
        await Agent(model).run("hello")
        return fake.requests[-1][1]

    assert "chat_template_kwargs" not in asyncio.run(go())


# --- the gateway: slot, cut kept for the operator ---


@pytest.fixture
def own_slot(db: psycopg.Connection) -> None:
    db.execute(
        "INSERT INTO model_servers (id, name, base_url) VALUES ('home-gpu', 'Home GPU', 'http://models.home.test/v1')"
    )
    db.execute("UPDATE model_slots SET model_ref = 'home-gpu:test-model', fallback_ref = NULL")
    db.execute(
        "INSERT INTO model_capabilities (provider, model_id, overrides, facts) "
        "VALUES ('home-gpu', 'test-model', "
        '\'{"tools": true, "json_object": true}\', \'{"server": "ollama"}\')'
    )


@pytest.mark.usefixtures("own_slot")
def test_the_gateway_keeps_a_cut_for_the_operator(db: psycopg.Connection, secrets_key: str, clock) -> None:  # type: ignore[no-untyped-def]
    fake = FakeServer.preset("ollama-real", models=("test-model",))

    def factory(spec: Any, model_id: str, key: str | None, caps: Any) -> Any:
        return model_on(fake, caps, spec, model_id)

    async def ask(ctx: CallContext) -> str:
        result = await Agent(ctx.model).run(long_prompt(8000))
        ctx.usage.update(result.usage)
        return result.output

    async def go() -> str:
        async with gateway(dsn(db), secrets_key, factory, clock) as (gw, _, _):
            return await gw.run("briefing.compose", ask, prompt_chars=32000, max_output_tokens=64)

    run(go)
    row = db.execute("SELECT truncated FROM model_capabilities WHERE provider = 'home-gpu'").fetchone()
    assert (
        row is not None
        and row[0]["read_tokens"] == 2050
        and set(row[0]) == {"sent_tokens", "read_tokens", "at"}
    )


# --- 9. embeddings on an own server ---


@pytest.mark.usefixtures("own_slot")
def test_memory_search_embeds_on_an_own_server(db: psycopg.Connection, secrets_key: str, clock) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.db.pool import open_pool
    from pisa.infra.hard_limit import DEFAULT
    from pisa.infra.keys import KeyStore
    from pisa.services.skills import gateway_embed
    from tests.owner import owner_at

    fake = FakeServer.preset("ollama-real", embeddings=True)
    db.execute("UPDATE model_slots SET model_ref = 'home-gpu:embedder' WHERE id = 'memory.embed'")

    async def go() -> Any:
        pool = await open_pool(dsn(db), max_size=2)
        try:
            http = httpx.AsyncClient(transport=fake.transport())
            embed = gateway_embed(
                pool, KeyStore(SecretStr(secrets_key)), clock, owner_at(), DEFAULT, http_client=http
            )
            unprobed = await embed(["note"], "document")
            db.execute(
                "INSERT INTO model_capabilities (provider, model_id, probe, probed_at) VALUES ('home-gpu', "
                "'embedder', '{\"embeddings\": true}', now())"
            )
            return unprobed, await embed(["a note", "another"], "document")
        finally:
            await pool.close()

    unprobed, (ref, vectors) = asyncio.run(go())
    assert unprobed is None  # no probe said it embeds: no call
    assert ref == "home-gpu:embedder" and len(vectors) == 2 and len(vectors[0]) == 16
    assert (
        fake.requests[-1] == ("embeddings", fake.requests[-1][1])
        and fake.requests[-1][1]["model"] == "embedder"
    )
    row = db.execute("SELECT slot, status, cost_eur FROM llm_calls WHERE slot = 'memory.embed'").fetchone()
    assert row == ("memory.embed", "settled", Decimal(0))


# --- the API and `pisa setup --ai own` ---


def test_a_servers_timeout_and_thinking_through_the_api(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/v1/ai/providers",
        headers=auth,
        json={
            "type": "openai_compatible",
            "id": "home-gpu",
            "base_url": f"http://{HOST}/v1",
            "read_timeout_s": 300,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["read_timeout_s"] == 300
    assert created.json()["thinking"]["conversation.reflex"] is False
    assert created.json()["thinking"]["chat.conversation"] is True
    changed = client.patch(
        "/v1/ai/providers/home-gpu", headers=auth, json={"thinking": {"chat.conversation": False}}
    ).json()
    assert changed["thinking"]["chat.conversation"] is False and changed["read_timeout_s"] == 300
    for bad in ({"read_timeout_s": 5}, {"thinking": {"memory.embed": True}}, {"thinking": {"nope": False}}):
        assert client.patch("/v1/ai/providers/home-gpu", headers=auth, json=bad).status_code in (400, 422)
    row = db.execute("SELECT read_timeout_s, thinking FROM model_servers WHERE id = 'home-gpu'").fetchone()
    assert row == (300, {"chat.conversation": False})


def test_setup_own_fills_every_slot_the_server_can_serve(app_settings: Any, db: psycopg.Connection) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup
    from pisa.domain import guide
    from tests.test_pair_cli import setup_args

    out = io.StringIO()
    db.execute("UPDATE model_slots SET model_ref = 'anthropic:claude-x' WHERE id = 'learn.answer'")
    args = setup_args(lang="en", skip=["place,push,pair"], ai="own", server_url=f"http://{HOST}:8000/v1",
                      server_id="home-llm", model="home-llm:test-model", read_timeout=240)  # fmt: skip
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    fake = FakeServer.preset("vllm", models=("test-model", "vectors"), embeddings=True)
    vectors_only = fake.reply

    def reply(body: dict[str, Any]) -> dict[str, Any] | None:
        return (
            {"content": ""} if body["model"] == "vectors" else (vectors_only(body) if vectors_only else None)
        )

    fake.reply = reply
    real_embed = fake.embed

    async def embed_only_vectors(request: Any) -> Any:
        body = json.loads(await request.body())
        if body.get("model") != "vectors":
            return fake._error(404, "Not Found")
        return await real_embed(request)

    fake.embed = embed_only_vectors  # type: ignore[method-assign]
    http = httpx.AsyncClient(transport=fake.transport())
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out), http=http)) == 0
    text = out.getvalue()
    slots = dict(db.execute("SELECT id, model_ref FROM model_slots").fetchall())
    assert slots["chat.conversation"] == "home-llm:test-model"
    assert (
        slots["intake.extract"] == "home-llm:test-model"
        and slots["conversation.reflex"] == "home-llm:test-model"
    )
    assert slots["memory.embed"] == "home-llm:vectors"
    assert guide.text("ai.own_embed", "en", model="home-llm:vectors") in text
    assert "240 s" in text
    assert db.execute("SELECT read_timeout_s FROM model_servers WHERE id = 'home-llm'").fetchone() == (240,)


# --- a turn on an own server that cut its prompt: the user is told once ---


class _OwnModels:
    """The test client's model factory: every model is a compat model on Ollama's fake."""

    def __init__(self, fake: FakeServer) -> None:
        self.fake = fake

    def __call__(self, spec: Any, model_id: str, key: str | None, caps: Any) -> Any:
        return model_on(self.fake, caps or TOOLS_ON, spec, model_id)


@pytest.fixture
def fake_models() -> _OwnModels:  # the `client` fixture's factory
    return _OwnModels(FakeServer.preset("ollama-real", models=("test-model",)))


@pytest.mark.usefixtures("own_slot")
def test_a_turn_whose_prompt_the_server_cut_says_so_once(client, db, fake_models) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair_v2
    from tests.test_abilities import _ask

    db.execute("UPDATE model_slots SET model_ref = NULL WHERE id <> 'chat.conversation'")
    device_id, token = pair_v2(db)
    first = _ask(client, token, device_id, "Ciao, come va?")
    second = _ask(client, token, device_id, "E domani?")
    notice = own_wire.CUT_NOTICE["it-IT"]
    assert first.startswith("Hello!") and first.endswith(notice)
    assert notice not in second  # once
    row = db.execute("SELECT truncated FROM model_capabilities WHERE model_id = 'test-model'").fetchone()
    assert row is not None and row[0]["read_tokens"] == 2050


@pytest.mark.usefixtures("own_slot")
def test_a_cut_stays_until_a_probe_finds_a_context_that_holds_it(db: psycopg.Connection) -> None:
    import datetime as dt

    from pisa.brain.models.compat import get_caps, save_probe, save_truncation
    from pisa.cli import probe_lines
    from pisa.services.ai import capabilities_view

    cut = {"sent_tokens": 9000, "read_tokens": 2050, "at": 1}
    at = dt.datetime(2026, 11, 24, tzinfo=dt.UTC)

    async def go(context: int) -> Any:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await save_truncation(conn, "home-gpu", "test-model", cut)
            await save_probe(conn, "home-gpu", "test-model", {"tools": True}, at, {"context_tokens": context})
            return await get_caps(conn, "home-gpu", "test-model")

    kept = asyncio.run(go(4096))
    assert kept.truncated == cut and kept.context_tokens == 4096
    lines = probe_lines("home-gpu:test-model", capabilities_view("test-model", kept))
    assert "2050 tokens read of about 9000 sent" in lines and "context of 4096 tokens" in lines
    assert asyncio.run(go(32768)).truncated is None
