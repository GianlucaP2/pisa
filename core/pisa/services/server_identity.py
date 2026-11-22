"""This install's server id (CONTRACT-2 §4.1): an opaque random id picked once, at the first read, and kept in
`app_meta` for the server's life. 192 random bits, base64url. Never derived from hardware, the hostname, an
address or the user: it names the install, so `GET /v1/server` serves it without auth, and a signed approval
binds it (`server=`, §6.2)."""

import secrets

from psycopg import AsyncConnection

SERVER_ID_KEY = "server_id"


async def server_id(conn: AsyncConnection) -> str:
    """The install's id, made on the first call (two first calls at once keep one)."""
    found = await (
        await conn.execute("SELECT value FROM app_meta WHERE key = %s", (SERVER_ID_KEY,))
    ).fetchone()
    if found is not None:
        return str(found[0])
    await conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (SERVER_ID_KEY, secrets.token_urlsafe(24)),
    )
    found = await (
        await conn.execute("SELECT value FROM app_meta WHERE key = %s", (SERVER_ID_KEY,))
    ).fetchone()
    assert found is not None
    return str(found[0])
