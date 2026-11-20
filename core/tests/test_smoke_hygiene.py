"""Smoke hygiene: the smoke test's throwaway device is removed with everything it made (`pisa devices purge`),
and the user's data stays as it was; the smoke script asks for no stored briefing."""

import asyncio
import uuid
from pathlib import Path

import psycopg

from pisa.cli import main
from pisa.services.settings import SettingsService
from pisa.services.tasks import TasksService
from tests.assistant_env import assistant_data, conversation, rome
from tests.conftest import Clock, FakeModels, dsn, pair
from tests.owner import owner_at
from tests.places_env import place_env
from tests.places_fake import FakePlaces
from tests.test_place_tools import calls_tool

SMOKE = Path(__file__).resolve().parents[2] / "scripts" / "smoke" / "e2e.py"
NOW = rome(2026, 10, 21, 12)


def talk(db: psycopg.Connection, secrets_key: str, device: str, tool: str, args: dict[str, object]) -> None:
    models = FakeModels()
    models.default = calls_tool(tool, args)

    async def go() -> None:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(NOW), models, keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(), clock=env.clock)

            async def brief() -> str:
                return ""

            data = assistant_data(
                env.pool, tasks, clock=env.clock, places=env.places,
                settings=SettingsService(env.pool, owner=owner_at()), brief=brief,
            )  # fmt: skip
            service = conversation(
                env.pool, env.places._gateway, owner=owner_at(), clock=env.clock, data=data
            )

            async def emit(delta: str, final: bool) -> None:
                pass

            await service.respond(uuid.UUID(device), "Pisa?", "it-IT", emit, trace_id="t")

    asyncio.run(go())


def counts(db: psycopg.Connection) -> dict[str, int]:
    tables = ("conversations", "messages", "tasks", "notifications", "briefings", "devices", "plans")
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}  # type: ignore[index]


def test_purging_the_smoke_device_leaves_the_owners_data(db, secrets_key, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    owner, _ = pair(db, "iPhone")
    talk(db, secrets_key, owner, "add_task", {"title": "Comprare il pane"})
    for target in (None, owner):  # a broadcast and one for the user's phone, with a briefing
        notification = uuid.uuid4()
        db.execute(
            "INSERT INTO notifications (id, kind, text, lang, priority, device_id, created_at, queued_at, "
            "delivered_via) VALUES (%s, 'briefing', 'x', 'it-IT', 'normal', %s, now(), now(), 'ws')",
            (notification, target),
        )
        db.execute(
            "INSERT INTO briefings (id, day, trigger, text, lang, created_at, notification_id) VALUES (%s, "
            "'2026-10-21', 'app_open', 'x', 'it-IT', now(), %s)",
            (uuid.uuid4(), notification),
        )
    before = counts(db)

    smoke, _ = pair(db, "smoke-test", "other")
    talk(db, secrets_key, smoke, "add_task", {"title": "Smoke task from the test device"})
    db.execute(
        "INSERT INTO notifications (id, kind, text, lang, priority, device_id, created_at, "
        "queued_at, delivered_via) "
        "VALUES (%s, 'info', 'x', 'it-IT', 'normal', %s, now(), now(), 'ws')",
        (uuid.uuid4(), smoke),
    )
    db.execute(  # GET /v1/plan made today's plan
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan, requested_by) "
        "VALUES (%s, '2026-10-21', now(), 'ask', 'f', '{}', %s)",
        (uuid.uuid4(), smoke),
    )
    assert counts(db) != before
    monkeypatch.setenv("PISA_DATABASE_URL", app_settings.database_url)
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level, **_: None)  # structlog stays
    assert main(["devices", "purge", smoke]) == 0
    assert counts(db) == before
    assert db.execute("SELECT title, source_ref FROM tasks").fetchall() == [
        ("Comprare il pane", f"device:{owner}")
    ]


def test_the_smoke_script_asks_for_no_stored_briefing() -> None:
    script = SMOKE.read_text()
    for stored in ('"/v1/briefings"', "/v1/briefings/latest", "Buongiorno Pisa"):
        assert stored not in script


def test_ios_b_m5_phone_keys_reset_on_the_host_lets_the_iphone_register_again(  # type: ignore[no-untyped-def]
    db, app_settings, monkeypatch
) -> None:
    """After re-pairing the iPhone, its old Face ID key (on a revoked device) signs nothing
    and blocks a new first key; `pisa phone-keys reset` (host only) removes the
    keys."""
    phone, _ = pair(db, "iPhone")
    db.execute(
        "INSERT INTO phone_keys (key_id, public_key, device_id, created_at) "
        "VALUES ('k1', '\\x00', %s, now())",
        (phone,),
    )
    monkeypatch.setenv("PISA_DATABASE_URL", app_settings.database_url)
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level, **_: None)
    assert main(["phone-keys", "reset", "--keep", phone]) == 0
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)
