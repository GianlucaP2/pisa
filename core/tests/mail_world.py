"""A small synthetic world for the storyline tests: mail rows, a fake events model, and one intake call.

Everything here is invented (example.com and example.com, made-up names and order numbers). The fake model
answers the events call with a scripted `{"events": [...], "effects": [...]}` per message subject, and records
what it was sent, so a test can check what reached the model and how many calls were made."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pisa.infra import ledger
from pisa.services.intake import IntakeService
from tests.conftest import FAKE_KEY, Clock, dsn
from tests.gateway_env import gateway, run
from tests.owner import owner_at

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
OWNER = "ada.demo@example.com"
Answer = dict[str, Any] | Callable[[], dict[str, Any]]


def ev(
    kind: str,
    quote: str,
    *,
    keys: tuple[tuple[str, str], ...] = (),
    playbook: str = "none",
    actor: str = "other",
    debtor: str = "none",
    at: str | None = None,
) -> dict[str, Any]:
    """One scripted event as the model would answer it."""
    return {
        "kind": kind,
        "playbook": playbook,
        "actor": actor,
        "debtor": debtor,
        "keys": [{"kind": k, "value": v} for k, v in keys],
        "occurred_at": at,
        "quote": quote,
    }


def effect(kind: str, key: str, evidence: str, reaction: tuple[str, str] | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": key,
        "evidence": evidence,
        "reaction": {"kind": reaction[0], "key": reaction[1]} if reaction else None,
    }


class EventsModel:
    """The fake model of the events call: `answers[subject]` is the scripted answer."""

    def __init__(self) -> None:
        self.answers: dict[str, Answer] = {}
        self.seen: list[dict[str, Any]] = []

    def say(
        self, subject: str, events: list[dict[str, Any]], effects: list[dict[str, Any]] | None = None
    ) -> None:
        self.answers[subject] = {"events": events, "effects": effects or []}

    def __call__(self, spec, model_id, api_key, ollama_base_url) -> FunctionModel:  # type: ignore[no-untyped-def]
        return FunctionModel(self._answer, model_name=f"fake-events-{model_id}")

    def _answer(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = next(
            str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        )
        subject = next(line[9:] for line in prompt.splitlines() if line.startswith("Subject: ")).replace(
            "ˆ", " "
        )
        self.seen.append(
            {
                "subject": subject,
                "prompt": prompt,
                "instructions": info.instructions or "",
                "function_tools": [t.name for t in info.function_tools],
            }
        )
        answer = self.answers.get(subject, {"events": [], "effects": []})
        args = answer() if callable(answer) else answer
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(args))])


def add_mail(
    db: psycopg.Connection,
    mid: str,
    *,
    sender: str,
    subject: str,
    text: str,
    quoted: str = "",
    thread: str | None = None,
    minutes: int = 0,
    labels: tuple[str, ...] = ("INBOX",),
    owner: bool = False,
    name: str | None = None,
) -> None:
    """A message as the sync stores it: the new text and the quoted history apart, `body` both together."""
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet, body, new_text, quoted, cut_version, owner_authored) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, 1, %s)",
        (
            mid,
            thread or f"t-{mid}",
            name or sender.split("@")[0],
            sender,
            subject,
            NOW + timedelta(minutes=minutes),
            list(labels),
            text[:100],
            text + ("\n\n" + quoted if quoted else ""),
            text,
            quoted,
            owner,
        ),
    )
    if owner:
        db.execute("UPDATE email_messages SET labels = %s WHERE id = %s", (list({*labels, "SENT"}), mid))


def run_intake(
    db: psycopg.Connection,
    secrets_key: str,
    model: EventsModel,
    *,
    now: datetime = NOW + timedelta(days=1),
    caps: ledger.Caps | None = None,
    action: Callable[[IntakeService], Any] | None = None,
) -> Any:
    """One intake round (or `action(service)`) with the fake model, at `now`."""

    async def go() -> Any:
        async with gateway(dsn(db), secrets_key, model, Clock(now)) as (gw, pool, keys):
            async with pool.connection() as conn:
                if not await keys.statuses(conn):
                    await keys.set(conn, "openai", FAKE_KEY)
                if caps is not None:
                    await ledger.set_caps(conn, caps)
            service = IntakeService(pool, gw, owner=owner_at(), clock=lambda: now)
            return await (action(service) if action else service.run())

    return run(go)


def rows(db: psycopg.Connection, query: str, *args: Any) -> list[tuple[Any, ...]]:
    return db.execute(query, args).fetchall()  # type: ignore[arg-type]


def task_rows(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    """(title, status, state, intent) of every task, by title."""
    return db.execute("SELECT title, status, state, intent FROM tasks ORDER BY title").fetchall()


def google_account(db: psycopg.Connection, address: str = OWNER) -> None:
    """The owner's Google account, so the owner model knows his address."""
    db.execute(
        "INSERT INTO connector_accounts (connector, account, scopes, status) "
        "VALUES ('google', %s, '{}', 'connected') "
        "ON CONFLICT (connector) DO UPDATE SET account = EXCLUDED.account",
        (address,),
    )


def with_conn(db: psycopg.Connection, fn: Callable[[Any], Any]) -> Any:
    """Run `fn(conn)` (async) on a pooled connection of the test database, inside a transaction."""
    from pisa.db.pool import open_pool

    async def go() -> Any:
        pool = await open_pool(dsn(db), max_size=2)
        await pool.wait()
        try:
            async with pool.connection() as conn, conn.transaction():
                return await fn(conn)
        finally:
            await pool.close()

    return run(go)


# --- helpers older tests share (they once lived in the old intake tests) ----------------------------------

OLD_NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


def add_email(db: psycopg.Connection, mid: str, subject: str, sender: str, sent: datetime = OLD_NOW) -> None:
    """A plain inbox message an hour before `sent`, as the tests of rules and tasks need one."""
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet, body) VALUES (%s, %s, %s, %s, %s, %s, '{INBOX}', %s, %s)",
        (mid, f"t-{mid}", sender.split("@")[0], sender, subject, sent - timedelta(hours=1), subject, subject),
    )


def service_call(db: psycopg.Connection, secrets_key: str, model: EventsModel, action: Any) -> Any:
    """`action(intake, tasks)` (async) with the intake service on the fake events model, at OLD_NOW."""
    from pisa.services.tasks import TasksService

    async def go() -> Any:
        async with gateway(dsn(db), secrets_key, model, Clock(OLD_NOW)) as (gw, pool, keys):
            async with pool.connection() as conn:
                if not await keys.statuses(conn):
                    await keys.set(conn, "openai", FAKE_KEY)
            intake = IntakeService(pool, gw, owner=owner_at(), clock=lambda: OLD_NOW)
            tasks = TasksService(pool, owner=owner_at(), clock=lambda: OLD_NOW)
            return await action(intake, tasks)

    return run(go)
