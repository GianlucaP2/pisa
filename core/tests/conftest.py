"""Test fixtures.

Database safety rule: DB tests run ONLY against a throwaway Postgres named explicitly in
PISA_TEST_ADMIN_URL, e.g. `docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg17`
then `PISA_TEST_ADMIN_URL=postgresql://postgres:test@127.0.0.1:55432/postgres`.
No default, no local socket: tests that need a DB are skipped without it, and refuse a non-local host.
"""

import contextlib
import os
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

# An f-string over a `Personal` in a door module raises in every test that runs it
pytest_plugins = ("tests.taint_check",)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DIR = REPO_ROOT / "protocol"

_ALLOWED_TEST_HOSTS = {"127.0.0.1", "localhost", "postgres", "db"}

# The sealed box is off on a server until its device checks pass; the suite tests it on
os.environ.setdefault("PISA_DRAWING_BOX", "on")

# The suite's user lives in Italy and speaks Italian: most tests were written against those settings, and
# they hold whatever the shell running the tests has set. A test about another language, country or zone
# says so (its own Settings, or an Owner of its own: `tests/owner.py`).
ITALIAN = {"PISA_TIMEZONE": "Europe/Rome", "PISA_LANGUAGE": "it", "PISA_COUNTRY": "IT"}
os.environ.update(ITALIAN)


def _plain_tracebacks() -> None:
    """The tests' log lines keep structlog's console renderer, with a plain traceback: the
    rich one (syntax-highlighted panels) was ~11% of the replay's CPU, for a logged exception nobody reads."""
    import structlog

    config = structlog.get_config()
    config["processors"] = [
        structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
        if isinstance(p, structlog.dev.ConsoleRenderer)
        else p
        for p in config["processors"]
    ]
    structlog.configure(processors=config["processors"])


_plain_tracebacks()


def _shared_route_state() -> None:
    """Every test builds its own app (`client`), and FastAPI builds each included route's
    validation state (its pydantic fields and handler) on that app's first request: ~20% of a client test's
    CPU, the same objects each time (Pisa's routers and endpoints are module-level). In the tests it is built
    once per route and shared by the apps. An app with dependency overrides (none in Pisa's tests) gets its
    own, as FastAPI builds it."""
    from fastapi import routing

    context = cast(Any, routing)._EffectiveRouteContext
    build = context.from_api_route.__func__
    built: dict[tuple[int, str, int], tuple[Any, Any]] = {}

    def from_api_route(cls: Any, *, original_route: Any, include_context: Any) -> Any:
        if getattr(include_context.dependency_overrides_provider, "dependency_overrides", None):
            return build(cls, original_route=original_route, include_context=include_context)
        key = (
            id(original_route),
            include_context.path_for(original_route),
            id(include_context.included_router),
        )
        hit = built.get(key)
        if hit is None or hit[0] is not original_route:
            hit = built[key] = (
                original_route,
                build(cls, original_route=original_route, include_context=include_context),
            )
        return hit[1]

    context.from_api_route = classmethod(from_api_route)


_shared_route_state()


def _admin_url() -> str | None:
    url = os.environ.get("PISA_TEST_ADMIN_URL")
    if not url:
        return None
    host = urlparse(url).hostname
    if not host:
        raise pytest.UsageError("PISA_TEST_ADMIN_URL must name a host and port (unix sockets are refused)")
    if host not in _ALLOWED_TEST_HOSTS:
        raise pytest.UsageError(f"PISA_TEST_ADMIN_URL host {host!r} is not a throwaway test host")
    return url


@pytest.fixture(autouse=True)
def roomy_plan_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The planner's wall-clock budgets (opening hours 5 s, the whole plan 10 s) are for real providers. On a
    loaded CI machine the fakes can overrun them and add a "took too long" note a test didn't expect, so tests
    get roomy budgets; a test about a budget sets its own (after this fixture)."""
    from pisa.services import planner

    monkeypatch.setattr(planner, "DETAILS_BUDGET_S", 60.0)
    monkeypatch.setattr(planner, "PLAN_BUDGET_S", 120.0)


# the standard loggers `configure_logging` sets
_LOGGERS_CONFIGURED = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def cli_keeps_test_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The CLI configures structlog for production (cached loggers), which would leave every later test in the
    process unable to capture logs. Tests keep structlog's test defaults. A test about the logging itself
    calls `pisa.logging.configure_logging`, and gets the test defaults back afterwards, whatever it restored:
    left configured, its INFO filter hid every later debug event in that worker from `capture_logs` (a test
    passed alone and failed after it, under -n), and so did its cache for every logger first used after it."""
    import logging

    import structlog

    monkeypatch.setattr("pisa.cli.configure_logging", lambda level, **_: None)
    config = structlog.get_config()
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    named = {
        name: (lg.handlers[:], lg.filters[:], lg.level, lg.propagate)
        for name in _LOGGERS_CONFIGURED
        for lg in (logging.getLogger(name),)
    }
    yield
    if structlog.get_config() != config:
        structlog.configure(**config)
    root.handlers[:], level = saved
    root.setLevel(level)
    for name, (handlers, filters, level, propagate) in named.items():
        lg = logging.getLogger(name)
        lg.handlers[:], lg.filters[:], lg.propagate = handlers, filters, propagate
        lg.setLevel(level)


@pytest.fixture(autouse=True)
def turn_reads_reset() -> Iterator[None]:
    """What a turn read (outside text) is a ContextVar a test may set at its top level; every
    test starts and ends without one, so one test's email never holds another's write (flaky under -n)."""
    from pisa.brain import untrusted

    token = untrusted._taint.set(None)  # pyright: ignore[reportPrivateUsage]
    yield
    untrusted._taint.reset(token)  # pyright: ignore[reportPrivateUsage]


def dsn(conn: psycopg.Connection) -> str:
    """Connection string for `conn` INCLUDING the password (psycopg's `info.dsn` omits it)."""
    return make_conninfo(conn.info.dsn, password=conn.info.password)


def _template_name() -> str:
    """The migrated template's name, from every migration file's name and bytes: a new or
    edited migration makes a new template, never a stale one."""
    import hashlib

    from pisa.db import migrate as migrations

    digest = hashlib.sha256(b"template v2: no autovacuum")  # the template's own recipe, then the migrations
    for path in sorted(Path(migrations.__file__).parent.joinpath("migrations").glob("*.sql")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return "pisa_test_tpl_" + digest.hexdigest()[:16]


TEMPLATE_STALE_DAYS = 7  # another branch's template on the same server is left alone until then


def _drop(conn: psycopg.Connection, name: str) -> None:
    conn.execute(sql.SQL("ALTER DATABASE {} IS_TEMPLATE false").format(sql.Identifier(name)))
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _template(admin: str) -> str:
    """One migrated database per migration set, made once (the first worker, under an advisory
    lock) and cloned by every worker's database, instead of each worker migrating its own.

    Built as `<name>_build`, then renamed and marked a template. A run that died between those steps leaves a
    database with the template's name that is not a template: it is dropped and built again. Other templates
    (another branch's migrations, on the same server) are dropped only once stale (untouched for
    TEMPLATE_STALE_DAYS) and unused; never this run's own."""
    name = _template_name()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext('pisa_test_template'))")
        try:
            state = conn.execute(
                "SELECT datistemplate FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
            if state is not None and not state[0]:  # a half-made template (a crash after the rename)
                _drop(conn, name)
                state = None
            if state is None:
                building = name + "_build"
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(building))
                )
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(building)))
                url = urlunparse(urlparse(admin)._replace(path=f"/{building}"))
                with psycopg.connect(url, autocommit=True) as tpl:
                    tpl.execute("CREATE EXTENSION IF NOT EXISTS vector")
                from pisa.db.migrate import migrate

                migrate(url)
                with psycopg.connect(url, autocommit=True) as tpl:
                    # The reset DELETEs: with no vacuum the freed space is not reused, so a test's new rows
                    # are appended. That is NOT a promise of insertion order: an UPDATE moves a row (to the
                    # end, or to another place in its page), as it did after TRUNCATE. A test that reads rows
                    # in order needs a strict ORDER BY (a ticking clock, a tiebreak), never the heap's order;
                    # tests/order_sweep.py finds the ones that don't.
                    for (table,) in tpl.execute(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm')"
                    ).fetchall():
                        off = "autovacuum_enabled = false, toast.autovacuum_enabled = false"
                        tpl.execute(sql.SQL("ALTER TABLE {} SET (" + off + ")").format(sql.Identifier(table)))
                conn.execute(
                    sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                        sql.Identifier(building), sql.Identifier(name)
                    )
                )
                conn.execute(sql.SQL("ALTER DATABASE {} IS_TEMPLATE true").format(sql.Identifier(name)))
            _drop_stale_templates(conn, keep=name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('pisa_test_template'))")
    return name


def _drop_stale_templates(conn: psycopg.Connection, keep: str) -> None:
    """Other migration sets' templates, untouched for TEMPLATE_STALE_DAYS and with no one connected. Their age
    is their directory's (pg_stat_file needs a superuser: without one nothing is dropped)."""
    try:
        stale = conn.execute(
            "SELECT d.datname FROM pg_database d WHERE starts_with(d.datname, 'pisa_test_tpl_') "
            "AND d.datistemplate AND d.datname <> %s AND d.datname <> %s "
            "AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname) "
            "AND (pg_stat_file('base/' || d.oid)).modification < now() - make_interval(days => %s)",
            (keep, keep + "_build", TEMPLATE_STALE_DAYS),
        ).fetchall()
    except psycopg.Error:
        return
    for (old,) in stale:
        _drop(conn, old)


@pytest.fixture(scope="session")
def test_database_url(worker_id: str) -> Iterator[str]:
    """A fresh, migrated database per xdist worker, dropped at the end: a clone of the migrated template,
    the same schema and seeded rows a migration leaves.

     `worker_id` comes from pytest-xdist ("master" when running without -n).
    """
    with cloned_database("pisa_test_" + re.sub(r"[^a-z0-9]", "", worker_id.lower())) as url:
        yield url


UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@contextlib.contextmanager
def uvicorn_logging_kept() -> Iterator[None]:
    """A real uvicorn server (`uvicorn.Config`) sets its loggers' levels, handlers and propagation for the
    whole process; this puts them back as they were when the server is gone, so no later test on the worker
    sees them (`test_uvicorn_logs_are_json` reads `uvicorn.access` at INFO)."""
    import logging

    saved = [
        (logger, logger.level, logger.handlers[:], logger.propagate, logger.disabled)
        for logger in map(logging.getLogger, UVICORN_LOGGERS)
    ]
    try:
        yield
    finally:
        for logger, level, handlers, propagate, disabled in saved:
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate, logger.disabled = propagate, disabled


@contextlib.contextmanager
def cloned_database(name: str) -> Iterator[str]:
    """A migrated database of its own named `name` (a clone of the template), dropped afterwards. A test that
    runs the real API under uvicorn uses one, so its process lock (one API per database) never meets another
    test's API on the worker's shared database."""
    admin = _admin_url()
    if admin is None:
        pytest.skip("PISA_TEST_ADMIN_URL not set: skipping database tests")
    template = _template(admin)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute("SELECT pg_advisory_lock(hashtext('pisa_test_template'))")  # one clone of it at a time
        try:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier(template)
                )
            )
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('pisa_test_template'))")
        # A throwaway database never waits for its commits to reach the disk (this database
        # only: the server's own settings are left as they are)
        conn.execute(sql.SQL("ALTER DATABASE {} SET synchronous_commit = off").format(sql.Identifier(name)))
    parsed = urlparse(admin)
    url = urlunparse(parsed._replace(path=f"/{name}"))
    with psycopg.connect(url, autocommit=True) as conn:
        choose_test_models(conn)
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db(test_database_url: str) -> Iterator[psycopg.Connection]:
    """Connection to the test DB. Afterwards, empties only the tables that got rows (fast reset) and puts
    the seeded slots back to their defaults."""
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        yield conn
        reset_database(conn)


def reset_database(conn: psycopg.Connection) -> None:
    # The tables that hold a row, asked of the tables themselves in one query: statistics (n_live_tup) can be
    # overwritten by another backend's late flush, which left a row for the next test.
    names = [
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename NOT IN ('schema_migrations', 'app_meta', 'model_slots')"
        ).fetchall()
    ]
    tables: list[str] = []
    if names:
        probe = sql.SQL(" UNION ALL ").join(
            sql.SQL("SELECT {} WHERE EXISTS (SELECT 1 FROM {})").format(sql.Literal(n), sql.Identifier(n))
            for n in names
        )
        tables = [r[0] for r in conn.execute(probe).fetchall()]
    if tables:
        # DELETE, not TRUNCATE: a TRUNCATE gives every table a new file and syncs it at commit
        # (15 tables: 265 ms with fsync on, 2 ms as DELETE). Every table with a row is emptied, so the
        # CASCADE had nothing more to reach; the replica role skips the foreign keys' order and the tables'
        # own triggers (TRUNCATE fired neither).
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            for table in tables:
                conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
    # Every sequence a test drew from starts over, an empty table's too (as RESTART IDENTITY of every table):
    # setval, not ALTER SEQUENCE … RESTART, which rewrites the sequence's file like a TRUNCATE.
    conn.execute(
        "SELECT setval(format('%I.%I', schemaname, sequencename)::regclass, start_value, false) "
        "FROM pg_sequences WHERE schemaname = 'public' AND last_value IS NOT NULL"
    )
    choose_test_models(conn)


# The suite's model choices (Pisa itself has no default model): every slot a test may call, each test from
# the same start. A test about an unset slot clears its own.
TEST_MODELS: dict[str, str | None] = {
    "chat.conversation": "openai:gpt-6-luna",
    "conversation.reflex": "google:gemini-3.5-flash-lite",
    "intake.triage": "openai:gpt-6-luna",
    "intake.extract": "openai:gpt-6-luna",
    "briefing.compose": "openai:gpt-6-luna",
    "places.research": "openai:gpt-6-luna",
    "memory.embed": None,
    "understand.confirm": "openai:gpt-6-luna",
    "attend.decide": "openai:gpt-6-luna",
    "learn.answer": None,
}


def choose_test_models(conn: psycopg.Connection) -> None:
    from pisa.domain.ai import SLOTS

    for slot in SLOTS:
        conn.execute(
            "INSERT INTO model_slots (id, model_ref, fallback_ref) VALUES (%s, %s, NULL) "
            "ON CONFLICT (id) DO UPDATE SET model_ref = EXCLUDED.model_ref, fallback_ref = NULL",
            (slot.id, TEST_MODELS[slot.id]),
        )


# --- application fixtures (no network, no paid calls) -------------------------------------------------------

FAKE_KEY = "sk-test-planted-0123456789abcdef"  # gitleaks:allow (a fake key); tests assert it never leaks


class FakeModels:
    """A model factory for the gateway: every model is a Pydantic AI FunctionModel.

    `replies[model_id]` is the text to stream (or an exception to raise); `calls` records each build.
    """

    def __init__(self) -> None:
        self.replies: dict[str, object] = {}
        self.default: object = "Ciao. Come posso aiutarti?"
        self.calls: list[tuple[str, str, str | None]] = []
        self.chunk_delay = 0.0
        self.seen_messages: list[object] = []

    def __call__(self, spec, model_id, api_key, ollama_base_url):  # type: ignore[no-untyped-def]
        import asyncio

        from pydantic_ai.models.function import FunctionModel

        self.calls.append((spec.id, model_id, api_key))
        reply = self.replies.get(model_id, self.default)

        def anthropic_would_take(messages):  # type: ignore[no-untyped-def]
            # A real-model replay (HTTP 400 on p5-04, m-01): the provider drops a request with no parts (only
            # instructions), so the call ends on the assistant's own reply, a prefill, which Anthropic
            # rejects with thinking on. Every fake call refuses both shapes, as the provider would.
            from pydantic_ai.messages import ModelRequest, ModelResponse

            last = messages[-1] if messages else None
            if isinstance(last, ModelResponse) or (isinstance(last, ModelRequest) and not last.parts):
                raise AssertionError("a request that ends on the model's own reply (the provider's HTTP 400)")

        async def stream(messages, info):  # type: ignore[no-untyped-def]
            anthropic_would_take(messages)
            self.seen_messages.append(messages)
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply):
                held: list[Any] = []
                offered = {t.name for t in info.function_tools}
                async for item in cast(Any, reply)(undiscovered(messages), info):
                    if isinstance(item, dict) or held:
                        held.append(item)  # tool calls: found first when deferred (below)
                    else:
                        yield item
                missing = [
                    d.name for item in held if isinstance(item, dict) for d in item.values()
                    if getattr(d, "name", None) and d.name not in offered
                ]  # fmt: skip
                if missing and SEARCH in offered:
                    yield {0: delta_call(SEARCH, {"queries": missing})}
                    return
                for item in held:
                    yield item
                return
            for word in str(reply).split(" "):
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                yield word + " " if not word.endswith("\n") else word

        async def complete(messages, info):  # type: ignore[no-untyped-def]
            import inspect

            from pydantic_ai.messages import ModelResponse, TextPart

            anthropic_would_take(messages)
            self.seen_messages.append(messages)
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply) and not inspect.isasyncgenfunction(reply):  # a plain or async function
                out = cast(Any, reply)(undiscovered(messages), info)
                out = await out if inspect.isawaitable(out) else out
                out = out if isinstance(out, ModelResponse) else ModelResponse(parts=[TextPart(out)])
                return found_first(out, info)
            return ModelResponse(parts=[TextPart(" ".join(str(reply).split(" ")) + " ")])

        return FunctionModel(complete, stream_function=stream, model_name=f"fake-{model_id}")


SEARCH = "search_tools"  # batch E: Pydantic AI's tool search; the deferred tools are found through it


def delta_call(name: str, args: dict[str, Any]) -> Any:
    import json
    import uuid

    from pydantic_ai.models.function import DeltaToolCall

    return DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"s{uuid.uuid4().hex[:8]}")


def found_first(response: Any, info: Any) -> Any:
    """A scripted fake calls Pisa's tools as if all were loaded; like a real model, it finds a deferred one
    first."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    offered = {t.name for t in info.function_tools}
    missing = [
        p.tool_name for p in response.parts if isinstance(p, ToolCallPart) and p.tool_name not in offered
    ]
    if missing and SEARCH in offered:
        return ModelResponse(parts=[ToolCallPart(SEARCH, {"queries": missing})])
    return response


def undiscovered(messages: list[Any]) -> list[Any]:
    """The messages without the search rounds (`found_first`'s): the scripted fake counts its own rounds."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart

    out: list[Any] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            parts = [p for p in message.parts if not (isinstance(p, ToolCallPart) and p.tool_name == SEARCH)]
            if parts or not message.parts:
                out.append(replace(message, parts=parts))
        elif isinstance(message, ModelRequest):
            parts = [p for p in message.parts if getattr(p, "tool_name", None) != SEARCH]
            if parts or not message.parts:
                out.append(replace(message, parts=parts))
        else:
            out.append(message)
    return out


def offline_transport(handler=None):  # type: ignore[no-untyped-def]
    """httpx transport: answers with `handler(request)` or fails like a network that is down."""
    import httpx

    def default(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network disabled in tests", request=request)

    return httpx.MockTransport(handler or default)


class Clock:
    def __init__(self, now):  # type: ignore[no-untyped-def]
        self.now = now

    def __call__(self):  # type: ignore[no-untyped-def]
        return self.now


@pytest.fixture
def secrets_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@pytest.fixture
def fake_models() -> FakeModels:
    return FakeModels()


@pytest.fixture
def clock() -> Clock:
    from datetime import UTC, datetime

    return Clock(datetime(2026, 10, 20, 11, 0, tzinfo=UTC))


@pytest.fixture
def http_handler() -> dict[str, object]:
    """Tests put an httpx handler under "handler" to answer provider/ECB/OpenRouter calls."""
    return {}


@pytest.fixture
def app_settings(test_database_url: str, secrets_key: str):  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.settings import Settings

    return Settings(
        env="test",
        timezone="Europe/Rome",  # the suite's Italian user (ITALIAN above)
        language="it",
        country="IT",
        database_url=test_database_url,
        secrets_key=SecretStr(secrets_key),
        apns_team_id="EXAMPLETEAM",
        apns_topic="com.example.pisa",
    )


@pytest.fixture
def client(app_settings, db, fake_models, clock, http_handler):  # type: ignore[no-untyped-def]
    import httpx
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    async def factory(settings):  # type: ignore[no-untyped-def]
        transport = httpx.MockTransport(lambda request: _dispatch(http_handler, request))
        return await Runtime.start(settings, model_factory=fake_models, http_transport=transport, clock=clock)

    with TestClient(create_app(app_settings, runtime_factory=factory)) as test_client:
        # the holding phrase fires after 0.7 s: on a loaded test machine any turn may take that long, so
        # the tests that check exact replies don't see it (test_talking_like_a_person sets it back).
        test_client.app.state.runtime.conversations.hold_after_s = 60.0  # type: ignore[attr-defined]
        yield test_client


def _dispatch(http_handler, request):  # type: ignore[no-untyped-def]
    import httpx

    handler = http_handler.get("handler")
    if handler is None:
        raise httpx.ConnectError("network disabled in tests", request=request)
    return handler(request)


def pair(db: psycopg.Connection, name: str = "iPhone", platform: str = "ios") -> tuple[str, str]:
    """The user's device, made directly in the test DB as `pair_v2` makes it, with the user's AI consent for
    health too (the tests of the pipeline talk about the dentist and the pharmacy). Returns (device_id,
    session token)."""
    return pair_v2_key(db, name, platform, health=True)[:2]


def set_key(db: psycopg.Connection, secrets_key: str, provider: str = "openai", key: str = FAKE_KEY) -> None:
    """A provider key stored as the app stores it (encrypted with the secrets key)."""
    import asyncio

    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await KeyStore(SecretStr(secrets_key)).set(conn, provider, key)

    asyncio.run(go())


def device_key() -> Any:
    """A fresh P-256 device key (what the app keeps in the Secure Enclave; the CLI's software key)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def session_for(db: psycopg.Connection, device_id: str, key: Any) -> str:
    """A 1 h session token: a nonce from the server, signed with the device key (CONTRACT-2 §4.3)."""
    import asyncio

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    from pisa.domain import pairing as rules
    from pisa.services import pairing

    async def go() -> str:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            nonce = (await pairing.session_nonce(conn, "test", device_id=device_id))["nonce"]
            signature = key.sign(rules.session_text(device_id, nonce), ec.ECDSA(hashes.SHA256()))
            made = await pairing.create_session(conn, device_id, nonce, rules.b64url(signature))
            return made["token"]

    return asyncio.run(go())


def pair_v2(
    db: psycopg.Connection, name: str = "iPhone", platform: str = "ios", *, consent: bool = True
) -> tuple[str, str]:
    """A device paired by QR (contract 2): made as `pisa pair` makes it once the user confirms, with its
    device key; a session token; and the user's AI consent recorded for the disclosure as it is now. Returns
    (device_id, session token)."""
    return pair_v2_key(db, name, platform, consent=consent)[:2]


def pair_v2_key(
    db: psycopg.Connection,
    name: str = "iPhone",
    platform: str = "ios",
    *,
    consent: bool = True,
    health: bool = False,
) -> tuple[str, str, Any]:
    """`pair_v2`, with the device's private key (to sign further sessions)."""
    import asyncio

    from cryptography.hazmat.primitives import serialization

    from pisa.services import disclosure

    key = device_key()
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    device_id = str(uuid4())

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO devices (id, name, platform, created_at, paired_via, device_key) VALUES (%s, "
                "%s, %s, now(), 'host', %s)",
                (device_id, name, platform, der),
            )
            if consent:
                revision = (await disclosure.current(conn, None))["revision"]
                await conn.execute(
                    "UPDATE devices SET ai_consent_revision = %s, ai_consent_health = %s, ai_told = %s "
                    "WHERE id = %s",
                    (revision, health, revision, device_id),
                )

    asyncio.run(go())
    return device_id, session_for(db, device_id, key), key


def seed_catalog(db: psycopg.Connection, rows: list[dict[str, object]]) -> None:
    from datetime import UTC, datetime

    for r in rows:
        provider, _, model_id = str(r["ref"]).partition(":")
        db.execute(
            "INSERT INTO model_catalog (ref, provider, model_id, name, capabilities, context_tokens, "
            "input_usd_per_mtok, output_usd_per_mtok, price_source, refreshed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                r["ref"],
                provider,
                model_id,
                r.get("name", model_id),
                r.get("capabilities", []),
                r.get("context_tokens"),
                r.get("input"),
                r.get("output"),
                r.get("source", "genai-prices" if r.get("input") is not None else None),
                datetime.now(UTC),
            ),
        )


@contextlib.contextmanager
def deadline(seconds: float) -> Iterator[None]:
    """Fail instead of hanging (a socket test that waits for a frame that never comes):
    SIGALRM interrupts the blocking receive and the test fails with TimeoutError."""
    import signal

    def expired(signum: int, frame: Any) -> None:
        raise TimeoutError(f"no answer within {seconds} s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def owner_plans_every_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pisa never times a task by itself; a task is in the plan at a time when the user
    asks Pisa to plan it ("Plan it"). The planner's own tests (placing, travel, hours, leave-now) are about
    how a task is placed once it is to be planned: here the user asked to plan every open task of the day (not
    the low ones, never planned), as a `plan` adjustment each."""
    from pisa.services import planner

    real = planner.load_adjustments

    async def every_task(conn: Any, day: Any) -> Any:
        rows = await (
            await conn.execute("SELECT id::text FROM tasks WHERE status = 'open' AND priority < 3")
        ).fetchall()
        named = await real(conn, day)
        return named + [planner.Change("plan", None, task_id, None, None, None, None) for (task_id,) in rows]

    monkeypatch.setattr(planner, "load_adjustments", every_task)


def pause_outbox(client: Any) -> None:
    """Stop the app's outbox listener (its NOTIFY deliveries and its minute sweep) for the rest of a test, so
    a test that drives its own Dispatcher or checks what is still queued never races it. The deliveries it had
    started finish first."""
    import asyncio

    listener = client.app.state.outbox_listener

    async def stop() -> None:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    client.portal.call(stop)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test that reaches the test database (through any fixture) is marked `db`, so CI
    runs the rest on their own, without Postgres (`-m "not db"`), and the database tests after them."""
    for item in items:
        if "test_database_url" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)
