"""One row in any table, every date in it `at` (the guard: decade-old rows in every KEPT table).

Built from the catalog: a NOT NULL column without a default gets a value of its type, the first value its
CHECK's `= ANY (ARRAY[...])` allows, or a parent row's key (seeded first) for a foreign key. Every timestamp
and date column, nullable or not, is `at`; a row a CHECK refuses that way is tried again with the nullable
ones left NULL. `OVERRIDES` covers the few tables the generic values can't satisfy.
"""

import re
import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

# table -> column -> value (a CHECK the generic values don't meet); a nullable column named here is set too
OVERRIDES: dict[str, dict[str, Any]] = {
    "ai_disclosures": {"revision": "a" * 32},  # a disclosure revision is hex
    "google_oauth_states": {"state_hash": "a" * 64},  # a SHA-256 hex
    "check_ins": {"event_id": "e1"},
    "device_push_relay": {"public_key": bytes(32)},  # an X25519 key is 32 bytes
    "done_by_presence": {"event_id": "e1"},
    "flight_claims": {"flight": "AZ123"},
    "pisa_explicit": {"key_hash": "a" * 64},
    "pisa_owner_reports": {"week": "2016-W44"},
    "pisa_skills": {"kind": "tool"},
    "pisa_views": {"provenance": "said"},
    "place_notes": {"radius_m": 100},
    "plan_adjustments": {"event_id": "e1"},
    "profile_off_limits": {"topic": "x"},
    "residency_days": {"phone_region": "IT"},
    "subscription_marks": {"id": "a" * 16},
    "ucp_merchants": {"host": "shop.example.com"},
    "pisa_replay_runs": {"passed": 1, "scenarios": 1},
    "walk_recaps": {"items": 1},
    "person_dates": {"month": 3, "day": 7},
    "bank_transactions": {"currency": "EUR"},  # a connector's table
    "intentions": {"state": "open"},  # Only their mail's or their dictation's promises are proposed
    "skill_vectors": {"embedding": "[" + ",".join(["0.1"] * 1024) + "]"},
}
_counter = iter(range(1, 10**9))


def _columns(db: psycopg.Connection, table: str) -> list[tuple[str, str, bool, bool, str]]:
    """(name, udt, not null, has default, data_type) of each column."""
    rows = db.execute(
        "SELECT column_name, udt_name, is_nullable = 'NO', "
        "column_default IS NOT NULL OR is_identity = 'YES', data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def _allowed(db: psycopg.Connection, table: str) -> dict[str, list[str]]:
    """column -> the values a CHECK (col = ANY (ARRAY['a', 'b'])) or (col IN ('a', 'b')) allows."""
    out: dict[str, list[str]] = {}
    for (definition,) in db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'c' AND conrelid = %s::regclass",
        (table,),
    ).fetchall():
        m = re.match(r"CHECK \(\(?\(?(\w+)\)?(?:::text)? = ANY \(\(?ARRAY\[(.*?)\]", definition)
        if m and m.group(1) not in out:
            out[m.group(1)] = re.findall(r"'((?:[^']|'')*)'", m.group(2))
    return out


def _foreign(db: psycopg.Connection, table: str) -> dict[str, tuple[str, str]]:
    """column -> (parent table, parent column), single-column foreign keys."""
    rows = db.execute(
        "SELECT a.attname, c.confrelid::regclass::text, pa.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
        "JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = c.confkey[1] "
        "WHERE c.contype = 'f' AND c.conrelid = %s::regclass AND cardinality(c.conkey) = 1",
        (table,),
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _value(udt: str, data_type: str, at: datetime, n: int) -> Any:
    if udt == "timestamptz" or udt == "timestamp":
        return at
    if udt == "date":
        return at.date()
    if udt in ("int2", "int4", "int8"):
        return next(_counter)
    if udt in ("float4", "float8"):
        return 0.5  # a probability's CHECK (0 < p < 1) too
    if udt == "numeric":
        return 1
    if udt == "bool":
        return False
    if udt == "uuid":
        return uuid.uuid4()
    if udt in ("json", "jsonb"):
        return Jsonb({})
    if udt == "bytea":
        return b"\x00"
    if udt == "interval":
        return "1 day"
    if udt == "vector":
        return None
    if data_type == "ARRAY":
        return []
    return f"x{next(_counter)}"


def seed(db: psycopg.Connection, table: str, at: datetime, n: int = 1, *, _depth: int = 0) -> dict[str, Any]:
    """Insert one row; returns its values (a parent's key for a child's foreign key)."""
    assert _depth < 8, f"{table}: foreign keys too deep"
    allowed, foreign = _allowed(db, table), _foreign(db, table)
    for keep_nullable_dates in (True, False):
        row: dict[str, Any] = {}
        for name, udt, not_null, has_default, data_type in _columns(db, table):
            if name in OVERRIDES.get(table, {}):
                row[name] = OVERRIDES[table][name]
            elif udt in ("timestamptz", "timestamp", "date") and (not_null or keep_nullable_dates):
                row[name] = _value(udt, data_type, at, n)
            elif not not_null or has_default:
                continue
            elif name in foreign:
                parent, column = foreign[name]
                row[name] = seed(db, parent, at, n, _depth=_depth + 1)[column]
            elif name in allowed:
                row[name] = allowed[name][0]
            else:
                row[name] = _value(udt, data_type, at, n)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in row),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        )
        try:
            with db.transaction():
                cur = db.execute(query, list(row.values()))
                values = cur.fetchone()
                assert cur.description is not None and values is not None
                return {d.name: v for d, v in zip(cur.description, values, strict=True)}
        except psycopg.errors.CheckViolation:
            if not keep_nullable_dates:
                raise
    raise AssertionError(table)
