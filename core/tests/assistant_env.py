"""Helpers for the "Pisa speaks first" tests (briefing, reminders, quiet mode): seed the user's day, set
settings, and drive the services against the test database with a controllable clock."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from pisa.brain.models.gateway import ModelGateway
from pisa.db.pool import open_pool
from pisa.domain import settings as rules
from pisa.domain.tasks import end_of_day, title_key
from pisa.infra.keys import KeyStore
from pisa.services.briefing import BriefingService
from pisa.services.reminders import RemindersService
from pisa.services.tasks import TasksService
from tests.conftest import FAKE_KEY, Clock, FakeModels
from tests.native import native
from tests.owner import owner_at

TZ = "Europe/Rome"
ROME = ZoneInfo(TZ)


def rome(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ROME)


def set_settings(db: psycopg.Connection, patch: dict[str, Any]) -> dict[str, Any]:
    row = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
    current = rules.with_defaults(row[0] if row else None, language="it-IT")
    updated = rules.apply_patch(current, patch)
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settings', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb(updated),),
    )
    return updated


def only_trigger(db: psycopg.Connection, trigger: str, **briefing: Any) -> None:
    triggers = {t: t == trigger for t in rules.TRIGGERS}
    set_settings(db, {"briefing": {"triggers": triggers, **briefing}})


def add_event(
    db: psycopg.Connection,
    title: str,
    start: datetime,
    *,
    minutes: int = 60,
    location: str | None = None,
    all_day: bool = False,
    calendar: str = "primary",
    event_id: str | None = None,
) -> str:
    event_id = event_id or uuid.uuid4().hex
    db.execute(
        "INSERT INTO calendars (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (calendar, calendar.title()),
    )
    day: date = start.astimezone(ROME).date()
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, "
        "start_date, end_date, "
        "location, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed') "
        "ON CONFLICT (calendar_id, id) DO UPDATE SET starts_at = EXCLUDED.starts_at, "
        "ends_at = EXCLUDED.ends_at, title = EXCLUDED.title",
        (
            calendar,
            event_id,
            title,
            start,
            start + timedelta(minutes=minutes) if not all_day else start + timedelta(days=1),
            all_day,
            day if all_day else None,
            day + timedelta(days=1) if all_day else None,
            location,
        ),
    )
    native(db, event_id)  # Their own Google event (the calendar's access role still decides)
    return event_id


def add_task(
    db: psycopg.Connection,
    title: str,
    due: datetime | date | None = None,
    *,
    status: str = "open",
    priority: int = 2,
    place: str | None = None,
    created: datetime | None = None,
) -> str:
    task_id = str(uuid.uuid4())
    if isinstance(due, datetime):
        due_at, kind = due, "datetime"
    elif isinstance(due, date):
        due_at, kind = end_of_day(due, TZ), "date"
    else:
        due_at, kind = None, "none"
    created = created or rome(2026, 10, 1, 9)
    db.execute(
        "INSERT INTO tasks (id, title, status, due_at, due_kind, place_text, priority, source_kind, "
        "title_key, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'manual', %s, %s, %s)",
        (task_id, title, status, due_at, kind, place, priority, title_key(title), created, created),
    )
    return task_id


def add_mail(
    db: psycopg.Connection,
    sender: str,
    subject: str,
    triaged_at: datetime,
    *,
    category: str = "action_required",
    labels: tuple[str, ...] = ("INBOX",),
) -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "intake_status, intake_category, intake_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'done', %s, %s)",
        (
            uuid.uuid4().hex,
            uuid.uuid4().hex,
            sender,
            "sender@example.com",
            subject,
            triaged_at - timedelta(minutes=1),
            list(labels),
            category,
            triaged_at,
        ),
    )


@dataclass
class Env:
    pool: AsyncConnectionPool
    briefings: BriefingService
    reminders: RemindersService
    clock: Clock
    keys: KeyStore


@asynccontextmanager
async def services(
    dsn: str,
    secrets_key: str,
    models: FakeModels,
    clock: Clock,
    *,
    with_key: bool = True,
    deadline_s: float = 6.0,
) -> AsyncIterator[Env]:
    pool = await open_pool(dsn, max_size=8)
    await pool.wait()
    try:
        keys = KeyStore(SecretStr(secrets_key))
        if with_key:
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
        gateway = ModelGateway(pool, keys, owner=owner_at(TZ), model_factory=models, clock=clock)
        tasks = TasksService(pool, owner=owner_at(TZ), clock=clock)
        yield Env(
            pool,
            BriefingService(pool, gateway, tasks, owner=owner_at(TZ), clock=clock, deadline_s=deadline_s),
            RemindersService(pool, owner=owner_at(TZ), clock=clock),
            clock,
            keys,
        )
    finally:
        await pool.close()


def notifications(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    """(kind, text, device_id, delivered_via) in order."""
    return db.execute(
        "SELECT kind, text, device_id, delivered_via FROM notifications ORDER BY created_at, id"
    ).fetchall()


def briefings(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    """(trigger, day, text) in order."""
    return db.execute("SELECT trigger, day, text FROM briefings ORDER BY created_at").fetchall()


def assistant_data(
    pool: AsyncConnectionPool,
    tasks: TasksService,
    *,
    clock: Any,
    places: Any = None,
    settings: Any = None,
    brief: Any = None,
    planner: Any = None,
    options: Any = None,
    follow: Any = None,
) -> Any:
    """The tasks and plan tools' data (pisa.services.assistant) with every collaborator the runtime gives it:
    the planner, the options, the day's follow-up, done and the bulk changes (stand-in places when the test
    has none)."""
    from unittest.mock import MagicMock

    from pisa.services.assistant import AssistantDataService
    from pisa.services.done import DoneService
    from pisa.services.follow import FollowService
    from pisa.services.plan_options import PlanOptionsService
    from pisa.services.planner import PlannerService
    from pisa.services.tasks_bulk import TasksBulkService

    owner = tasks.owner
    places = places if places is not None else MagicMock()
    planner = planner or PlannerService(pool, tasks, places, owner=owner, clock=clock)
    options = options or PlanOptionsService(pool, planner, places, owner=owner, clock=clock)
    follow = follow or FollowService(pool, planner, options, tasks, places, owner=owner, clock=clock)
    done = DoneService(pool, tasks, planner, owner=owner, clock=clock)

    async def no_brief() -> str:
        return ""

    return AssistantDataService(
        pool,
        tasks,
        settings,
        brief or no_brief,
        planner,
        options,
        follow,
        done,
        TasksBulkService(pool, tasks),
    )


def conversation(
    pool: AsyncConnectionPool,
    gateway: Any,
    *,
    owner: Any,
    clock: Any,
    profile: Any = None,
    lexicon: Any = None,
    **tools: Any,
) -> Any:
    """A conversation with these tool families (`ToolFamilies` names) and, as the runtime gives it, the
    profile both as a tool family and for the conversation's own reads."""
    from pisa.brain.conversation import ToolFamilies
    from pisa.services.conversation import ConversationService
    from pisa.services.conversation_deps import ConversationDeps

    deps = ConversationDeps(tools=ToolFamilies(profile=profile, **tools), profile=profile, lexicon=lexicon)
    return ConversationService(pool, gateway, owner=owner, clock=clock, deps=deps)
