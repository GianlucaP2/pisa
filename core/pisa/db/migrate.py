"""Apply plain SQL migrations in order.

Files live in `pisa/db/migrations/NNNN_name.sql`, numbered from 1 without gaps: `0001_baseline.sql` creates
the whole schema of a fresh database, and later changes follow as `0002_*`, `0003_*`, ... Each file runs in
its own transaction and is recorded in `schema_migrations` with its name and checksum. Editing an applied file
is refused: add a new migration instead. A database whose recorded history is not this one (an applied version
with no file, or under another name) is refused too: the baseline starts from an empty database.
"""

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

import psycopg

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


class MigrationError(RuntimeError):
    pass


def load_migrations() -> list[Migration]:
    folder = resources.files("pisa.db") / "migrations"
    found: list[Migration] = []
    for entry in folder.iterdir():
        match = _NAME.match(entry.name)
        if entry.name.endswith(".sql") and not match:
            raise MigrationError(f"bad migration file name {entry.name!r}: expected NNNN_snake_case.sql")
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text()))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migrations must be numbered 1, 2, 3, ... without gaps or repeats: {versions}")
    return found


def latest_version() -> int:
    migrations = load_migrations()
    return migrations[-1].version if migrations else 0


def applied_versions(conn: psycopg.Connection) -> dict[int, str]:
    """Applied version -> checksum; empty before the first migration."""
    return {v: checksum for v, (_, checksum) in _applied(conn).items()}


def _applied(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    exists = conn.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()
    if not exists or not exists[0]:
        return {}
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    return {int(v): (str(n), str(c)) for v, n, c in rows}


def migrate(database_url: str) -> list[str]:
    """Apply pending migrations. Returns the names applied."""
    migrations = load_migrations()
    by_version = {m.version: m for m in migrations}
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    integer PRIMARY KEY,
                name       text NOT NULL,
                checksum   text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        done = _applied(conn)
        for version, (name, _) in sorted(done.items()):
            m = by_version.get(version)
            if m is None or m.name != name:
                raise MigrationError(
                    f"this database records migration {name!r} (version {version}), which is not in this "
                    "release's history: it was made by another schema history; start from an empty database"
                )
        for m in migrations:
            if m.version in done:
                if done[m.version][1] != m.checksum:
                    raise MigrationError(
                        f"migration {m.name} was changed after it was applied; add a new migration instead"
                    )
                continue
            with conn.transaction():
                conn.execute(m.sql.encode())  # trusted file from the package, not user input
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
            applied_now.append(m.name)
    return applied_now
