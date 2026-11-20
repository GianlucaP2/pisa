"""Helpers to drive the model gateway directly against the test database (sync tests, asyncio.run)."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic import SecretStr

from pisa.brain.models.gateway import ModelGateway
from pisa.db.pool import open_pool
from pisa.infra.hard_limit import DEFAULT, HardLimits
from pisa.infra.keys import KeyStore
from tests.owner import owner_at


@asynccontextmanager
async def gateway(
    dsn: str, secrets_key: str, models: Any, clock: Any, http: Any = None, hard: HardLimits = DEFAULT
):  # type: ignore[no-untyped-def]
    """`http`: the client a never-probed model is probed with; without one (the default) none is probed.
    `hard`: the server's hard limit."""
    pool = await open_pool(dsn, max_size=4)
    await pool.wait()
    try:
        keys = KeyStore(SecretStr(secrets_key))
        gw = ModelGateway(
            pool, keys, owner=owner_at(), model_factory=models, clock=clock, http=http, hard_limits=hard
        )
        yield gw, pool, keys
    finally:
        await pool.close()


def run[T](fn: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(fn())  # type: ignore[arg-type]
