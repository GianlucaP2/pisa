"""Async connection pool for the API process (psycopg 3)."""

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


async def open_pool(database_url: str, *, max_size: int = 10) -> AsyncConnectionPool:
    """Open without waiting: the API starts (and /health answers) even while the database is down."""
    pool = AsyncConnectionPool(
        database_url,
        connection_class=AsyncConnection,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True},
    )
    await pool.open(wait=False)
    return pool


async def _unlock_all(conn: AsyncConnection) -> None:
    await conn.execute("SELECT pg_advisory_unlock_all()")


async def open_lock_pool(database_url: str, *, max_size: int) -> AsyncConnectionPool:
    """The worker's job locks on connections of their own, so
    a long job never pins one of the runtime's; each connection returns with every session lock released, even
    when a cancel landed in a job's unlock."""
    pool = AsyncConnectionPool(
        database_url,
        connection_class=AsyncConnection,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True},
        reset=_unlock_all,
    )
    await pool.open(wait=False)
    return pool
