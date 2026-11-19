"""The migration runner: a baseline numbered 1 builds a fresh database's whole schema; later files follow it.

`schema.txt` beside this module is the expected schema, as `schema_fingerprint` lists it: every table's
columns, constraints, indexes and triggers, the views' columns, the functions' bodies, the sequences, the
extensions and the seed rows. It was made from the database the earlier migration history left, and the
baseline reproduces it. A reviewed schema change regenerates it:
`uv run python -m tests.test_migrate postgresql://.../a_migrated_database`.
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from pisa.db import migrate as runner
from pisa.db.migrate import MigrationError, applied_versions, latest_version, load_migrations, migrate

EXPECTED = Path(__file__).with_name("schema.txt")

# One line per schema object, in the server's own words; deparsed text that changes between PostgreSQL
# versions (a view's query) is left out, the view's columns are kept.
_FINGERPRINT = """
WITH rel AS (
    SELECT c.oid, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname <> 'schema_migrations'
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
)
SELECT 'extension ' || extname FROM pg_extension WHERE extname <> 'plpgsql'
UNION ALL
SELECT CASE r.relkind WHEN 'v' THEN 'view ' ELSE 'table ' END || r.relname || ' column ' || a.attname || ' '
       || format_type(a.atttypid, a.atttypmod) || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END
       || CASE a.attidentity WHEN 'a' THEN ' identity always' WHEN 'd' THEN ' identity by default' ELSE '' END
       || CASE WHEN a.attgenerated = 's' THEN ' generated' ELSE '' END
       || coalesce(CASE WHEN a.atthasdef THEN ' default ' END || pg_get_expr(d.adbin, d.adrelid), '')
FROM rel r JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid = r.oid AND d.adnum = a.attnum
WHERE r.relkind IN ('r', 'v')
UNION ALL
SELECT 'table ' || r.relname || ' constraint ' || c.conname || ' ' || pg_get_constraintdef(c.oid)
FROM rel r JOIN pg_constraint c ON c.conrelid = r.oid WHERE c.contype IN ('p', 'u', 'f', 'c', 'x')
UNION ALL
SELECT 'index ' || pg_get_indexdef(i.indexrelid)
FROM rel r JOIN pg_index i ON i.indrelid = r.oid
WHERE NOT EXISTS (
    SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid AND c.contype IN ('p', 'u', 'x')
)
UNION ALL
SELECT 'trigger ' || pg_get_triggerdef(t.oid) FROM rel r JOIN pg_trigger t ON t.tgrelid = r.oid
WHERE NOT t.tgisinternal
UNION ALL
SELECT 'sequence ' || r.relname || ' ' || format_type(s.seqtypid, NULL) || ' start ' || s.seqstart
       || ' increment ' || s.seqincrement || ' cache ' || s.seqcache
FROM rel r JOIN pg_sequence s ON s.seqrelid = r.oid
UNION ALL
SELECT 'function ' || p.proname || '(' || pg_get_function_arguments(p.oid) || ') '
       || pg_get_function_result(p.oid) || ' ' || l.lanname || ' ' || md5(p.prosrc)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = 'public'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
UNION ALL
SELECT 'row app_meta ' || key || ' = ' || value FROM app_meta
UNION ALL
SELECT 'row model_slots ' || id || ' = ' || coalesce(model_ref, 'NULL') || ' / '
       || coalesce(fallback_ref, 'NULL')
FROM model_slots
"""


def schema_fingerprint(conn: psycopg.Connection) -> list[str]:
    return sorted(str(row[0]) for row in conn.execute(_FINGERPRINT).fetchall())


@pytest.fixture
def own_database_url(test_database_url: str) -> Iterator[str]:
    """An empty database of this test's own, with pgvector (as deploy/postgres/init makes it), not migrated:
    the session's shared database keeps its schema whatever a test does here."""
    parsed = urlparse(test_database_url)
    name = parsed.path.lstrip("/") + "_own"
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


def test_the_migrations_start_with_the_baseline_and_have_no_gaps() -> None:
    migrations = load_migrations()
    assert migrations[0].name == "0001_baseline.sql"
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert latest_version() == migrations[-1].version


def test_the_baseline_on_an_empty_database_gives_the_expected_schema(own_database_url: str) -> None:
    assert migrate(own_database_url)[0] == "0001_baseline.sql"
    with psycopg.connect(own_database_url) as conn:
        got = schema_fingerprint(conn)
    expected = EXPECTED.read_text().splitlines()
    missing, extra = sorted(set(expected) - set(got)), sorted(set(got) - set(expected))
    assert not missing and not extra, f"missing: {missing[:20]}\nextra: {extra[:20]}"
    assert len(got) == len(expected)


def test_the_test_template_has_the_baseline_s_schema(test_database_url: str) -> None:
    # Only the schema: other tests on this worker may already have written rows (seed rows are checked on a
    # fresh database by the test above).
    with psycopg.connect(test_database_url) as conn:
        got = [line for line in schema_fingerprint(conn) if not line.startswith("row ")]
    expected = [line for line in EXPECTED.read_text().splitlines() if not line.startswith("row ")]
    missing, extra = sorted(set(expected) - set(got)), sorted(set(got) - set(expected))
    assert not missing and not extra, f"missing: {missing[:20]}\nextra: {extra[:20]}"
    assert got == expected


def test_the_baseline_holds_no_role_or_grant() -> None:
    """Roles and their privileges are deploy/postgres/init's (superuser); the migration role makes none."""
    baseline = load_migrations()[0].sql.upper()
    for word in ("CREATE ROLE", "GRANT ", "REVOKE ", "ALTER DEFAULT PRIVILEGES", "OWNER TO"):
        assert word not in baseline, word


def test_migrate_is_idempotent(test_database_url: str) -> None:
    assert migrate(test_database_url) == []
    with psycopg.connect(test_database_url) as conn:
        assert max(applied_versions(conn)) == latest_version()


def test_changed_migration_is_refused(test_database_url: str) -> None:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    try:
        with pytest.raises(MigrationError, match="changed after it was applied"):
            migrate(test_database_url)
    finally:
        first = load_migrations()[0]
        with psycopg.connect(test_database_url, autocommit=True) as conn:
            conn.execute("UPDATE schema_migrations SET checksum = %s WHERE version = 1", (first.checksum,))


@pytest.mark.parametrize(
    ("version", "name"),
    [(1, "0001_init.sql"), (111, "0111_event_native.sql")],  # another history's first file, or a later one
)
def test_a_database_from_another_migration_history_is_refused(
    own_database_url: str, version: int, name: str
) -> None:
    migrate(own_database_url)
    with psycopg.connect(own_database_url, autocommit=True) as conn:
        if version == 1:
            conn.execute("UPDATE schema_migrations SET name = %s WHERE version = 1", (name,))
        else:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, 'x')",
                (version, name),
            )
    with pytest.raises(MigrationError, match="another schema history"):
        migrate(own_database_url)


def test_a_later_migration_runs_once_after_the_baseline(
    own_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = load_migrations()
    assert migrate(own_database_url) == [m.name for m in baseline]
    assert baseline[0].name == "0001_baseline.sql"
    n = len(baseline) + 1
    later = runner.Migration(
        n, f"{n:04d}_example.sql", "CREATE TABLE example_later (id integer PRIMARY KEY);"
    )
    monkeypatch.setattr(runner, "load_migrations", lambda: [*baseline, later])
    assert migrate(own_database_url) == [f"{n:04d}_example.sql"]
    assert migrate(own_database_url) == []
    with psycopg.connect(own_database_url) as conn:
        assert sorted(applied_versions(conn)) == list(range(1, n + 1))
        assert conn.execute("SELECT to_regclass('public.example_later') IS NOT NULL").fetchone() == (True,)


def test_a_failed_migration_leaves_nothing_behind(
    own_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = load_migrations()
    migrate(own_database_url)
    n = len(baseline) + 1
    broken = runner.Migration(n, f"{n:04d}_broken.sql", "CREATE TABLE half_made (id integer); SELECT 1 / 0;")
    monkeypatch.setattr(runner, "load_migrations", lambda: [*baseline, broken])
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(own_database_url)
    with psycopg.connect(own_database_url) as conn:
        assert sorted(applied_versions(conn)) == list(range(1, n))
        assert conn.execute("SELECT to_regclass('public.half_made')").fetchone() == (None,)


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (["0001_baseline.sql", "0003_skipped.sql"], "without gaps"),
        (["0002_no_baseline.sql"], "without gaps"),
        (["0001_baseline.sql", "0002_Bad-Name.sql"], "bad migration file name"),
    ],
)
def test_the_files_must_be_numbered_from_one_without_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: list[str], error: str
) -> None:
    folder = tmp_path / "migrations"
    folder.mkdir()
    for name in files:
        (folder / name).write_text("SELECT 1;")
    monkeypatch.setattr(runner.resources, "files", lambda _package: tmp_path)
    with pytest.raises(MigrationError, match=error):
        load_migrations()


def test_readyz_names_the_pending_version(test_database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from pisa.api import app as api
    from pisa.settings import Settings

    monkeypatch.setattr(api, "latest_version", lambda: latest_version() + 1)
    response = TestClient(
        api.create_app(Settings(timezone="Europe/Rome", database_url=test_database_url))
    ).get("/readyz")
    assert response.status_code == 503
    expected = f"at {latest_version()}, expected {latest_version() + 1}: run pisa migrate"
    assert response.json()["checks"]["schema"] == expected


if __name__ == "__main__":  # regenerate the expected schema from a migrated database
    with psycopg.connect(sys.argv[1]) as conn:
        EXPECTED.write_text("\n".join(schema_fingerprint(conn)) + "\n")
