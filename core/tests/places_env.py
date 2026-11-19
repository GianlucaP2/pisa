"""Drive the place services against the test database and the fake providers (tests/places_fake.py)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from pisa.brain.models.gateway import ModelGateway
from pisa.db.pool import open_pool
from pisa.infra.hard_limit import DEFAULT, HardLimits
from pisa.infra.keys import KeyStore
from pisa.services.place_assistant import PlaceAssistantData
from pisa.services.place_providers import PlaceProviderService, set_maps_key
from pisa.services.places import PlacesService
from pisa.services.weather import WeatherService
from tests.conftest import FAKE_KEY, Clock, FakeModels, dsn, pair
from tests.owner import owner_at
from tests.places_fake import FakePlaces
from tests.test_push import p8
from tests.weather_fake import fake_meteo

TZ = "Europe/Rome"
HERE_MILAN = (45.465, 9.0920)
MAPS_KEY_ID = "MAPSKEY123"
GOOGLE_KEY = "AIza-test-google-key-0000"  # fake
TAVILY_KEY = "tvly-test-key-0000"  # fake


@dataclass
class PlaceEnv:
    pool: AsyncConnectionPool
    providers: PlaceProviderService
    places: PlacesService
    assistant: PlaceAssistantData
    fake: FakePlaces
    clock: Clock
    weather: WeatherService


@asynccontextmanager
async def place_env(
    dsn_: str,
    secrets_key: str,
    fake: FakePlaces,
    clock: Clock,
    models: FakeModels | None = None,
    *,
    keys: tuple[str, ...] = ("apple", "google", "tavily"),
    hard: HardLimits = DEFAULT,
) -> AsyncIterator[PlaceEnv]:
    pool = await open_pool(dsn_, max_size=6)
    await pool.wait()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    try:
        store = KeyStore(SecretStr(secrets_key))
        gateway = ModelGateway(
            pool,
            store,
            owner=owner_at(TZ),
            model_factory=models or FakeModels(),
            clock=clock,
            hard_limits=hard,
        )
        providers = PlaceProviderService(
            pool, store, http, team_id="EXAMPLETEAM", owner=owner_at(TZ), clock=clock, hard_limits=hard
        )
        async with pool.connection() as conn:
            await store.set(conn, "openai", FAKE_KEY)
            if "apple" in keys:
                await set_maps_key(conn, store, MAPS_KEY_ID, p8()[0])
        if "google" in keys:
            await providers.set_key("google", GOOGLE_KEY)
        if "tavily" in keys:
            await providers.set_key("tavily", TAVILY_KEY)
        places = PlacesService(pool, providers, gateway, owner=owner_at(TZ), clock=clock)
        meteo = fake_meteo(clock().astimezone(ZoneInfo(TZ)).date())
        weather = WeatherService(pool, meteo, places, owner=owner_at(TZ), clock=clock)
        assistant = PlaceAssistantData(pool, places, owner=owner_at(TZ), clock=clock, weather=weather)
        yield PlaceEnv(pool, providers, places, assistant, fake, clock, weather)
    finally:
        await http.aclose()
        await pool.close()


def run_places[T](
    db: psycopg.Connection,
    secrets_key: str,
    fake: FakePlaces,
    now: datetime,
    action: Callable[[PlaceEnv], Awaitable[T]],
    models: FakeModels | None = None,
    **kw: Any,
) -> T:
    async def go() -> T:
        async with place_env(dsn(db), secrets_key, fake, Clock(now), models, **kw) as env:
            return await action(env)

    return asyncio.run(go())


def at_location(db: psycopg.Connection, where: tuple[float, float], measured_at: datetime) -> str:
    """A paired device that sent this location. Returns its id."""
    row = db.execute("SELECT id::text FROM devices WHERE revoked_at IS NULL LIMIT 1").fetchone()
    device_id = row[0] if row else pair(db)[0]
    db.execute(
        "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) VALUES (%s, %s, "
        "%s, 10, %s)",
        (device_id, where[0], where[1], measured_at),
    )
    return device_id


def place_calls(db: psycopg.Connection) -> list[tuple[str, str, int, bool]]:
    return db.execute("SELECT provider, sku, units, ok FROM place_calls ORDER BY id").fetchall()
