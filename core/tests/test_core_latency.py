"""The latency contract and CI's speed.

C1 the cached share of input per turn is measured (the turn's parts, the talk report, the replay's summary).
C2 the cached prefix is byte-stable: a turn's offered tools never enter the cache-keyed tool list.
C3 the model sees the same instructions and tools as before (only where the offers sit changed).
C4 an 8 s turn budget: Pisa says it is taking long and the turn goes on (never cut, never a claim); a 20 s
   ceiling per model request replaces the 45 s timeout.
C5 the replay fails with one line when the brain's p95 grows more than 25% over the last stored run.
"""

import asyncio
import json
import time
import uuid
from typing import Any, cast

import pytest

from pisa.domain import latency
from tests.owner import owner_at
from tests.replay import harness

# --- C1: the cached share, measured -------------------------------------------------------------------------


def test_the_cached_share_is_read_over_written_and_fresh_input() -> None:
    rounds = [{"in": 10_000, "cr": 9_000, "cw": 0}, {"in": 12_000, "cr": 9_000, "cw": 2_900}]
    assert harness.cached_share(rounds) == pytest.approx(18_000 / 22_000)
    assert harness.cached_share([{"in": 100, "out": 3}]) is None  # a provider without the counts
    assert harness.cached_share([]) is None
    line = harness.shares_line([0.0, 0.5, 0.9, 0.95])
    assert "p50 90%" in line and "over 4 turns" in line and "1 read nothing" in line


def test_a_turn_s_parts_carry_each_round_s_cache_counts() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.usage import RequestUsage

    from pisa.services.conversation import turn_parts

    response = ModelResponse(
        parts=[TextPart("ok")],
        usage=RequestUsage(input_tokens=1000, output_tokens=5, cache_read_tokens=900, cache_write_tokens=50),
    )
    parts = turn_parts("t", 0.1, [800], [response], [])
    assert parts["rounds"] == [{"ms": 800, "in": 1000, "out": 5, "cr": 900, "cw": 50}]
    assert harness.cached_share(parts["rounds"]) == pytest.approx(0.9)


# --- C2, C3: the cached prefix is byte-stable; the model sees the same tools -------------------------------

PLAIN = "che ore sono?"
OFFERS = "salvala come meteo"  # tool_choice offers save_view at once; the cache test calls it
SCENARIO: dict[str, Any] = {
    "id": "latency-offer",
    "now": "2026-10-30T10:00",
    "lang": "it-IT",
    "world": {},
    "turns": [{"owner": PLAIN}, {"owner": OFFERS, "advance_s": 20}],
    "script": [[], []],
}


def test_without_reveal_by_reference_the_offer_comes_after_every_other_tool() -> None:
    from pydantic_ai.tools import ToolDefinition

    from pisa.brain import tool_search

    defs = [
        ToolDefinition(name="finish"),
        ToolDefinition(name="save_view", defer_loading=True),
        ToolDefinition(name="get_agenda"),
        ToolDefinition(name="note_aire", defer_loading=True),
        ToolDefinition(name="add_document", defer_loading=True),
    ]
    out = tool_search.offered(defs, frozenset({"save_view", "add_document"}))
    assert [d.name for d in out] == ["finish", "get_agenda", "note_aire", "add_document", "save_view"]
    assert [d.defer_loading for d in out] == [False, False, True, False, False]
    # the same definitions the model saw before (only their place moved): names, schemas, descriptions
    before = [
        (d.name, d.description, d.parameters_json_schema, False if d.name in {"save_view", "add_document"}
         else d.defer_loading)
        for d in defs
    ]  # fmt: skip
    after = [(d.name, d.description, d.parameters_json_schema, d.defer_loading) for d in out]
    assert sorted(before, key=str) == sorted(after, key=str)
    assert tool_search.offered(defs, frozenset()) is defs  # a turn that offers nothing: the list as it was


def test_the_scripted_model_sees_the_same_tools_and_instructions_on_an_offer_turn(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """The tests' FunctionModel reveals nothing by reference: it gets the offered tool inline, after the
    others. The set of tools it is offered is what `shown()` offered before; the system prompt
    is system_prompt() unchanged."""
    from pisa.brain.conversation import system_prompt

    seen: list[tuple[list[str], str]] = []

    async def model(messages: Any, info: Any) -> Any:
        from pydantic_ai.messages import SystemPromptPart

        system = "".join(
            p.content for m in messages for p in getattr(m, "parts", []) if isinstance(p, SystemPromptPart)
        )
        seen.append(([t.name for t in info.function_tools], (getattr(messages[-1], "instructions", "") or "")
                     + system))  # fmt: skip
        yield "Va bene."

    scenario = dict(SCENARIO, id="latency-offer-inline")
    from tests.actions_fake import ActionsGoogle
    from tests.conftest import set_key

    set_key(db, secrets_key)
    google = ActionsGoogle()
    http_handler["handler"] = harness.route(google, harness.local(scenario["now"]).date())
    fake_models.default = model
    runtime = client.app.state.runtime
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore

    client.portal.call(
        harness.run, runtime.conversations, runtime.pool, google, scenario,
        KeyStore(SecretStr(secrets_key)), clock, runtime,
    )  # fmt: skip
    brain = [s for s in seen if "finish" in s[0]]
    assert len(brain) >= 2
    plain, offer = brain[0][0], brain[-1][0]
    # the same tools; save_view after every one of Pisa's (pydantic-ai appends its search_tools last)
    assert [n for n in offer if n != "save_view"] == plain and plain[-1] == "search_tools"
    assert offer[-2:] == ["save_view", "search_tools"]
    assert all(system_prompt(lang="it-IT") in s[1] for s in brain)


# --- C4: the turn budget ------------------------------------------------------------------------------------


def test_the_budget_and_the_ceiling_come_from_the_replay_s_numbers() -> None:
    from pisa.brain import conversation

    # A Haiku replay: model turns p50 4.3 s, p95 8.9 s, max 14.5 s. The budget speaks near the p95; the
    # ceiling sits above the max.
    assert 4.3 < latency.TURN_BUDGET_S <= 8.9
    assert latency.REQUEST_TIMEOUT_S > 14.5
    assert conversation.MODEL_TIMEOUT_S == latency.REQUEST_TIMEOUT_S


def _slow_turn(
    client: Any,
    db: Any,
    fake_models: Any,
    key: str,
    *,
    budget: float,
    delay: float,
    reply: str,
    spoken: bool = True,
) -> tuple[list[str], list[tuple[str, str]], str, float]:
    from pisa.services import conversation as service
    from tests.conftest import pair, set_key

    set_key(db, key)
    conversations = client.app.state.runtime.conversations
    words: list[str] = []
    held: list[tuple[str, str]] = []

    async def slow(messages: Any, info: Any) -> Any:
        await asyncio.sleep(delay)
        yield reply

    fake_models.default = slow
    device = uuid.UUID(pair(db)[0])

    async def emit(delta: str, final: bool) -> None:
        words.append(delta)

    async def hold(phrase_id: str, text: str) -> None:
        held.append((phrase_id, text))

    async def go() -> tuple[str, float]:
        started = time.monotonic()
        old = service.TURN_BUDGET_S
        service.TURN_BUDGET_S = budget
        try:
            out = await conversations.respond(
                device, "che ore sono?", "it-IT", emit, trace_id="t", hold=hold, spoken=spoken
            )
        finally:
            service.TURN_BUDGET_S = old
        return out, time.monotonic() - started

    out, took = client.portal.call(go)
    return words, held, out, took


def test_a_slow_turn_hears_it_is_taking_long_and_still_finishes(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A holding phrase (`use` slow), never the reply's text nor its first audio."""
    from pisa.domain import holding

    words, held, out, took = _slow_turn(
        client, db, fake_models, secrets_key, budget=0.2, delay=0.6, reply="Sono le 11."
    )
    slow = holding.slow_phrase("it-IT")
    assert held == [(slow.id, slow.text)] and slow.use == "slow"  # once, while they wait
    assert slow.text not in "".join(words) and slow.text not in out  # not in the reply's words
    assert "Sono le 11." in out and took >= 0.6  # not cut: the answer comes after it
    turn = client.app.state.runtime.conversations._previous  # the turn's watch: no first audio from it
    assert all(w.held is None for w in turn.values())


def test_a_typed_turn_and_a_turn_inside_the_budget_hear_no_slow_line(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    _, held, out, _ = _slow_turn(
        client, db, fake_models, secrets_key, budget=5.0, delay=0.0, reply="Sono le 11."
    )
    assert held == [] and "Sono le 11." in out
    _, held, out, _ = _slow_turn(
        client, db, fake_models, secrets_key, budget=0.2, delay=0.6, reply="Sono le 11.", spoken=False
    )
    assert held == [] and "Sono le 11." in out  # typed: they read the answer, no line in its bubble


def test_a_step_line_and_the_slow_line_never_stack() -> None:
    """The progress loop and the budget share `watch.progress`: one line of the kind per turn."""
    import inspect

    from pisa.services.conversation import ConversationService

    source = inspect.getsource(ConversationService.respond)
    assert "if not watch.finished and not watch.progress and watch.tool is not None and quiet:" in source
    assert "if not watch.finished and not watch.progress and quiet:" in source


def test_the_slow_line_never_claims_a_change() -> None:
    from pisa.domain import holding
    from pisa.domain.claims import claim

    for lang in ("it-IT", "en-US", "fr-FR"):
        line = holding.slow_phrase(lang).text
        assert line and claim(line) is None and "fatto" not in line.lower() and "done" not in line.lower()


def _all_tools() -> dict[str, Any]:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.toolsets import FunctionToolset

    from pisa.brain.conversation import TurnContext, build_agent
    from pisa.brain.research_turn import ResearchTurn

    m = MagicMock(timezone="Europe/Rome")

    async def noop(_: Any) -> bool:
        return True

    agent = build_agent(
        FunctionModel(lambda a, b: ModelResponse(parts=[TextPart("x")])), TurnContext(owner=owner_at(),
        clock=lambda: datetime.now(UTC), data=m, places=m, actions=m, profile=m, compose=noop, call=noop,
        research=ResearchTurn(lang="it-IT", prompt_chars=0), decided=m, views=m, trips=m, people=m,
        rituals=(m, m), autopilot=m, handoffs=m, money=m, public=(m, m), world=m, explore=m,)
    )  # fmt: skip
    return {
        name: tool
        for toolset in agent.toolsets
        if isinstance(toolset, FunctionToolset)
        for name, tool in toolset.tools.items()
    }


def test_every_tool_definition_is_the_same_with_the_schema_cache() -> None:
    """The tools' schemas are built once (pisa/brain/schema_cache.py, ~100 ms of every turn
    before the model); every definition the model sees is the same bytes as when built fresh, and each turn's
    tool calls its own turn's function."""
    from pydantic_ai import _function_schema  # pyright: ignore[reportPrivateUsage]

    from pisa.brain import schema_cache

    with pytest.MonkeyPatch.context() as patch:  # Pydantic AI's own schema build, without the cache
        patch.setattr(_function_schema, "function_schema", schema_cache._build)  # pyright: ignore[reportPrivateUsage]
        fresh = _all_tools()
    schema_cache.install()
    _all_tools()
    hits = schema_cache.stats["hits"]
    cached = _all_tools()
    assert schema_cache.stats["hits"] - hits >= len(cached) * 0.9  # the second build is nearly all hits
    assert sorted(fresh) == sorted(cached) and len(cached) > 100

    def shape(tool: Any) -> str:
        d = tool.tool_def
        fs = tool.function_schema
        return json.dumps(
            [d.name, d.description, d.parameters_json_schema, d.strict, d.defer_loading, fs.takes_ctx,
             fs.is_async, fs.single_arg_name, fs.positional_fields, fs.var_positional_field,
             fs.return_schema],
            sort_keys=True, default=str,
        )  # fmt: skip

    assert {n: shape(t) for n, t in fresh.items()} == {n: shape(t) for n, t in cached.items()}
    again = _all_tools()
    for name, tool in cached.items():  # each build's own closures, and its own schema dicts
        assert tool.function is not again[name].function
        assert tool.function_schema.function is tool.function
        assert tool.function_schema.json_schema is not again[name].function_schema.json_schema


# --- D: CI under 8 minutes ----------------------------------------------------------------------------------


def test_d1_the_reset_empties_every_table_with_a_row_and_restarts_its_sequences(db) -> None:  # type: ignore[no-untyped-def]
    """The reset DELETEs (no TRUNCATE's new files and fsync), to TRUNCATE … RESTART IDENTITY CASCADE's end
    state: every table with a row emptied whatever the foreign keys' order, its own sequences restarted, the
    seeded slots back."""
    from tests.conftest import pair, reset_database

    def two_messages() -> list[int]:
        device_id, _ = pair(db)  # a device, and rows that reference it (foreign keys)
        conversation = db.execute(
            "INSERT INTO conversations (id, device_id) VALUES (gen_random_uuid(), %s) RETURNING id",
            (device_id,),
        ).fetchone()[0]
        return [
            db.execute(
                "INSERT INTO messages (conversation_id, role, content, lang) "
                "VALUES (%s, 'user', 'x', 'it-IT') "
                "RETURNING id",
                (conversation,),
            ).fetchone()[0]
            for _ in range(2)
        ]

    first = two_messages()
    db.execute("UPDATE model_slots SET model_ref = 'x:y' WHERE id = 'chat.conversation'")
    reset_database(db)
    for table in ("devices", "conversations", "messages"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
    assert db.execute("SELECT model_ref FROM model_slots WHERE id = 'chat.conversation'").fetchone() != (
        "x:y",
    )
    assert two_messages() == first  # the bigserial restarted, as RESTART IDENTITY did
    reset_database(db)
    # A sequence drawn from with no row kept (its table empty at the reset) starts over too
    seq = db.execute("SELECT pg_get_serial_sequence('messages', 'id')").fetchone()[0]
    db.execute("SELECT nextval(%s)", (seq,))
    db.execute("SELECT nextval(%s)", (seq,))
    reset_database(db)
    assert db.execute("SELECT nextval(%s)", (seq,)).fetchone() == (1,)
    reset_database(db)


def test_d2_the_template_is_named_by_the_migrations_and_cloned_per_worker(test_database_url: str) -> None:
    import psycopg

    from tests.conftest import _admin_url, _template_name

    admin = _admin_url()
    assert admin is not None
    name = _template_name()
    assert name.startswith("pisa_test_tpl_") and len(name) == len("pisa_test_tpl_") + 16
    with psycopg.connect(admin, autocommit=True) as conn:
        assert conn.execute(
            "SELECT datistemplate FROM pg_database WHERE datname = %s", (name,)
        ).fetchone() == (True,)
    with psycopg.connect(
        test_database_url, autocommit=True
    ) as conn:  # the clone: migrated, every file applied
        from pisa.db import migrate as migrations

        files = sorted(
            p.name for p in __import__("pathlib").Path(migrations.__file__).parent.glob("migrations/*.sql")
        )
        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        assert applied is not None and applied[0] >= len(files) - 1
        assert conn.execute("SHOW synchronous_commit").fetchone() == ("off",)  # this throwaway database only


def test_d3_every_test_that_reaches_the_database_is_marked_db(request) -> None:  # type: ignore[no-untyped-def]
    from tests import conftest

    class Item:
        def __init__(self, fixtures: tuple[str, ...]) -> None:
            self.fixturenames, self.marks = fixtures, []

        def add_marker(self, mark: Any) -> None:
            self.marks.append(mark.name)

    direct, through, none = (
        Item(("test_database_url",)),
        Item(("client", "db", "test_database_url")),
        Item(()),
    )
    conftest.pytest_collection_modifyitems(cast(Any, [direct, through, none]))
    assert direct.marks == ["db"] and through.marks == ["db"] and none.marks == []
    for item in request.session.items:  # and this run's own items, as collected
        reaches = "test_database_url" in getattr(item, "fixturenames", ())
        assert reaches == (item.get_closest_marker("db") is not None), item.nodeid


def test_d4_the_latency_report_s_clock_keeps_the_order_and_reports_modelled_seconds() -> None:
    from tests.latency_report import SUITE_SPEED, Clock
    from tests.virtual_time import VirtualTimeLoop

    assert Clock(SUITE_SPEED).real(0.7) == pytest.approx(0.07)

    async def race() -> list[str]:
        # The Clock reads the loop's virtual time (the wall's on a loaded machine moves on between two timers)
        clock = Clock(SUITE_SPEED, asyncio.get_running_loop().time)
        order: list[str] = []

        async def after(name: str, seconds: float) -> None:
            await clock.sleep(seconds)
            order.append(name)

        start = clock.now()
        await asyncio.gather(after("model", 0.45 * 0.85), after("hold", 0.3))
        assert clock.now() - start == pytest.approx(0.45 * 0.85)  # modelled seconds, not the wall's
        return order

    assert asyncio.run(race(), loop_factory=VirtualTimeLoop) == ["hold", "model"]


def test_d5_the_tests_without_a_database_run_alone_and_ci_runs_every_test() -> None:
    from tests.conftest import REPO_ROOT

    # ci.yml in the public export, the private CI workflow before it
    workflows = REPO_ROOT / ".github" / "workflows"
    ci = next(p for p in (workflows / "ci.yml", workflows / "pisa-ci.yml") if p.is_file()).read_text()
    assert "uv run pytest -n auto -q\n" in ci  # every test, in one run (no -m, no -k: none is left out)
    pyproject = (REPO_ROOT / "core" / "pyproject.toml").read_text()
    assert 'markers = ["db:' in pyproject


def test_d2_a_half_made_template_is_built_again_and_another_branch_s_is_left(  # type: ignore[no-untyped-def]
    test_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the rename and IS_TEMPLATE left a plain database with the template's name: it
    is dropped and built again. Another migration set's template (a branch sharing the server) is dropped only
    once stale and unused, never while fresh."""
    import psycopg
    from psycopg import sql

    from tests import conftest

    admin = conftest._admin_url()
    assert admin is not None
    name, other = f"pisa_test_tpl_probe{uuid.uuid4().hex[:8]}", f"pisa_test_tpl_other{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(conftest, "_template_name", lambda: name)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))  # half-made: no template
        conn.execute(sql.SQL("CREATE DATABASE {} IS_TEMPLATE true").format(sql.Identifier(other)))
    try:
        assert conftest._template(admin) == name
        with psycopg.connect(admin, autocommit=True) as conn:
            rows = dict(
                conn.execute(
                    "SELECT datname, datistemplate FROM pg_database WHERE datname IN (%s, %s)", (name, other)
                ).fetchall()
            )
        assert rows == {name: True, other: True}  # rebuilt as a template; the fresh foreign one kept
        url = test_database_url.rsplit("/", 1)[0] + f"/{name}"
        with psycopg.connect(url, autocommit=True) as conn:  # really migrated
            from pisa.db.migrate import latest_version

            count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
            assert count is not None and count[0] == latest_version()
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            for db_name in (name, other, name + "_build"):
                if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone():
                    conftest._drop(conn, db_name)
