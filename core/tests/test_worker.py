"""Worker: DBOS schedules in Europe/Rome, and the housekeeping jobs."""

import re
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql

from pisa.settings import get_settings
from pisa.workers.main import SCHEDULES
from tests.conftest import dsn


def test_schedules() -> None:
    assert [(s.name, s.cron) for s in SCHEDULES] == [
        ("catalog-refresh", "0 0 3 * * *"),  # 03:00 daily
        ("ledger-ghost-sweep", "0 */5 * * * *"),  # every 5 minutes
        ("gmail-sync", "0 */2 * * * *"),  # every 2 minutes, then intake
        ("calendar-sync", "30 */5 * * * *"),  # every 5 minutes
        ("prune-history", "0 30 3 * * *"),  # daily: The closed list and their message setting only
        ("prune-places", "0 */15 * * * *"),  # provider terms: cache 24 h, Google coordinates 30 days
        # The minute in three workflows (was every-minute)
        ("minute-on-time", "0 * * * * *"),  # the briefing time trigger, reminders, the end of quiet mode
        ("minute-plan", "0 * * * * *"),  # done, the plan, check-ins, attend
        ("minute-initiative", "0 * * * * *"),  # Pisa's own initiative, the night last
    ]


@pytest.fixture
def device_row(db: psycopg.Connection) -> str:
    from tests.conftest import pair

    return pair(db)[0]


@pytest.fixture
def dbos_url(test_database_url: str, worker_id: str):  # type: ignore[no-untyped-def]
    name = "pisa_dbos_test_" + re.sub(r"[^a-z0-9]", "", worker_id.lower())
    admin = urlunparse(urlparse(test_database_url)._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield urlunparse(urlparse(test_database_url)._replace(path=f"/{name}"))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_dbos_launch_schedules_and_sweep(db: psycopg.Connection, dbos_url: str, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from dbos import DBOS

    from pisa.workers.main import sweep_ghosts_workflow

    monkeypatch.setenv("PISA_DATABASE_URL", dsn(db))
    get_settings.cache_clear()
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, price_known, trace_id, "
        "created_at) "
        "VALUES (gen_random_uuid(), 's', 'openai:x', 'reserved', 0.01, true, 't', now() - interval "
        "'11 minutes')"
    )
    DBOS(config={"name": "pisa-worker-test", "system_database_url": dbos_url})
    try:
        DBOS.launch()
        DBOS.apply_schedules(
            [
                {
                    "schedule_name": s.name,
                    "workflow_fn": s.workflow,
                    "schedule": s.cron,
                    "cron_timezone": "Europe/Rome",
                }
                for s in SCHEDULES
            ]
        )
        stored = {s["schedule_name"]: s for s in DBOS.list_schedules()}
        assert set(stored) == {s.name for s in SCHEDULES}
        assert all(s["cron_timezone"] == "Europe/Rome" for s in stored.values())
        DBOS.start_workflow(sweep_ghosts_workflow, datetime.now(UTC), None).get_result()
    finally:
        DBOS.destroy(destroy_registry=False)
        get_settings.cache_clear()
    assert db.execute("SELECT status FROM llm_calls").fetchone() == ("swept",)
