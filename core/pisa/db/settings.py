"""app_settings: small JSON values keyed by name (caps, exchange rate, catalog refresh time…).

Each key has one owning service, which reads and writes it here; other services ask that owner."""

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


async def get_setting(conn: AsyncConnection, key: str) -> Any:
    row = await (await conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,))).fetchone()
    return row[0] if row else None


async def set_setting(conn: AsyncConnection, key: str, value: Jsonb) -> None:
    await conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value),
    )


async def delete_setting(conn: AsyncConnection, *keys: str) -> None:
    """The keys gone (a key not there is no error)."""
    if keys:
        await conn.execute("DELETE FROM app_settings WHERE key = ANY(%s)", (list(keys),))


async def delete_settings_under(conn: AsyncConnection, prefix: str) -> None:
    """Every key that starts with `prefix` gone (one value per device, per server…)."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    await conn.execute("DELETE FROM app_settings WHERE key LIKE %s", (escaped + "%",))


async def updated_at(conn: AsyncConnection, key: str) -> str:
    """When the key last changed, as text ('' when it is not there): a version for a cache."""
    row = await (
        await conn.execute("SELECT updated_at::text FROM app_settings WHERE key = %s", (key,))
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else ""


async def patch_setting(
    conn: AsyncConnection, key: str, patch: Mapping[str, Any], *, when: Mapping[str, str | None] | None = None
) -> Any:
    """`patch` merged into the key's JSON object, only while each field of `when` reads as given (None: not
    set), in one statement, so two callers never both win. The new value, or None when nothing matched."""
    sql = "UPDATE app_settings SET value = value || %s, updated_at = now() WHERE key = %s"
    params: list[Any] = [Jsonb(dict(patch)), key]
    for field, value in (when or {}).items():
        if value is None:
            sql += " AND value->>%s IS NULL"
            params.append(field)
        else:
            sql += " AND value->>%s = %s"
            params += [field, value]
    row = await (await conn.execute(sql + " RETURNING value", params)).fetchone()
    return row[0] if row else None
