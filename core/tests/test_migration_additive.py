"""The life-graph migration is additive: a database made by the migrations before it keeps every table,
every column and every row, and the rows stay valid."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql

from pisa.db import migrate as runner
from pisa.db.migrate import load_migrations, migrate

NEW = "0028_life_graph_foundation.sql"
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def own_url(test_database_url: str) -> Iterator[str]:
    parsed = urlparse(test_database_url)
    name = parsed.path.lstrip("/") + "_additive"
    admin = urlunparse(parsed._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = urlunparse(parsed._replace(path=f"/{name}"))
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _columns(conn: psycopg.Connection) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public'"
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def test_migration_additive(own_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    every = load_migrations()
    assert every[-1].name == NEW  # this is the next one after 0027
    before_files = [m for m in every if m.name != NEW]
    monkeypatch.setattr(runner, "load_migrations", lambda: before_files)
    assert migrate(own_url)[-1] == "0027_own_model_servers.sql"

    task = uuid.uuid4()
    with psycopg.connect(own_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO email_messages (id, thread_id, from_address, sent_at, labels, body) "
            "VALUES ('old-1', 'old-t', 'shop@example.com', %s, '{INBOX}', 'Your order AB-1042 shipped.')",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, source_kind, title_key, created_at, updated_at) "
            "VALUES (%s, 'Ritirare il pacco', 'open', 'email', 'ritirare pacco', %s, %s)",
            (task, NOW, NOW),
        )
        old_columns = _columns(conn)
        old_tables = {t for t, _ in old_columns}

    monkeypatch.setattr(runner, "load_migrations", lambda: every)
    assert migrate(own_url) == [NEW]

    with psycopg.connect(own_url, autocommit=True) as conn:
        new_columns = _columns(conn)
        # nothing dropped, renamed or retyped
        assert {k: v for k, v in new_columns.items() if k in old_columns} == old_columns
        assert old_tables <= {t for t, _ in new_columns}
        # the old rows are there, unchanged, with the new columns at their defaults
        message = conn.execute(
            "SELECT body, intake_status, new_text, quoted, owner_authored, cut_version FROM email_messages "
            "WHERE id = 'old-1'"
        ).fetchone()
        assert message == ("Your order AB-1042 shipped.", "pending", None, None, False, 0)
        row = conn.execute(
            "SELECT title, status, state, waiting_until, entity_id, intent FROM tasks WHERE id = %s", (task,)
        ).fetchone()
        assert row == ("Ritirare il pacco", "open", None, None, None, None)
        # and they take an update through the new columns
        conn.execute("UPDATE tasks SET state = 'waiting', waiting_until = %s WHERE id = %s", (NOW, task))
        added = {t for t, _ in new_columns} - old_tables
        assert added == {
            "owner_identity", "email_threads", "entity", "entity_key", "orgs", "mail_event", "task_evidence",
            "life_edge", "assertion", "storyline", "storyline_event",
        }  # fmt: skip
        # the old intake still finds the old message
        assert conn.execute("SELECT id FROM email_messages WHERE intake_status = 'pending'").fetchall() == [
            ("old-1",)
        ]
