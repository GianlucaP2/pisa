"""Parallel searches meeting the same new places (the fan-out): one row each, no failed turn."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from pisa.ports.places import ProviderPlace
from tests.places_env import run_places
from tests.places_fake import FakePlaces


@pytest.mark.db
def test_parallel_searches_meeting_one_new_place_share_its_row(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """As `search` does: each search saves its places inside one open transaction, so a second search can't
    see the first one's new row until it commits, and its own insert of that place must not fail."""
    found = ProviderPlace("google", "g-race-1", "Forno Antico", 45.465, 9.092)

    async def all_of_them(env: Any) -> list[uuid.UUID]:
        async def one() -> uuid.UUID:
            async with env.pool.connection() as conn, conn.transaction():
                place_id = await env.places.upsert(conn, found)
                await asyncio.sleep(0.2)  # the rest of that search's places
                return place_id

        return list(await asyncio.gather(*(one() for _ in range(4))))

    ids = run_places(db, secrets_key, FakePlaces(), datetime(2026, 11, 10, 9, tzinfo=UTC), all_of_them)
    assert len(set(ids)) == 1
    assert db.execute("SELECT count(*) FROM places WHERE google_place_id = 'g-race-1'").fetchone() == (1,)
