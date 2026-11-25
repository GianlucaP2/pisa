"""Where the user's home is stored (pisa.domain.home), and whether they are travelling from it: two
settings owned here, read by the profile, the learning and the lexicon. The user's home wins; a learned one
is a guess until the user confirms it; forgetting it also forgets the nights before, so Pisa doesn't learn
the same place again the next night."""

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.db.settings import delete_setting, get_setting, set_setting, updated_at
from pisa.domain import home as rules

HOME_KEY = "profile_home"  # pisa.domain.home.Home.stored(), or {"forgotten_at"} after the user forgot it
TRAVELLING_KEY = "profile_travelling"  # {"since", "city"} while the user sleeps > 50 km from home


async def version(conn: AsyncConnection) -> str:
    """When their home last changed (a cache's key)."""
    return await updated_at(conn, HOME_KEY)


async def travelling(conn: AsyncConnection) -> dict[str, Any] | None:
    """{"since", "city"} while they are travelling (sleeping more than 50 km from home), else None."""
    return await get_setting(conn, TRAVELLING_KEY) or None


async def set_travelling(conn: AsyncConnection, trip: dict[str, Any]) -> None:
    await set_setting(conn, TRAVELLING_KEY, Jsonb(trip))


async def end_trip(conn: AsyncConnection) -> None:
    """Not travelling (back home, or no home to measure from)."""
    await delete_setting(conn, TRAVELLING_KEY)


async def read(conn: AsyncConnection) -> rules.Home | None:
    return rules.parse(await get_setting(conn, HOME_KEY))


async def forgotten_at(conn: AsyncConnection) -> datetime | None:
    """When the user last forgot their home: nights before it never teach a home again."""
    value = await get_setting(conn, HOME_KEY)
    if isinstance(value, dict) and value.get("forgotten_at"):
        return datetime.fromisoformat(value["forgotten_at"])
    return None


async def write(conn: AsyncConnection, home: rules.Home, *, forgotten: datetime | None = None) -> None:
    stored: dict[str, Any] = home.stored()
    if forgotten is not None:
        stored["forgotten_at"] = forgotten.isoformat()
    await set_setting(conn, HOME_KEY, Jsonb(stored))


async def set_owner(
    conn: AsyncConnection, *, lat: float, lon: float, name: str, place_id: str | None
) -> rules.Home:
    """The user's home: it replaces any learned one, and a trip measured from the old one ends (the next
    night far from this one starts a new trip)."""
    home = rules.Home(lat, lon, name[: rules.NAME_MAX], "owner", True, place_id)
    await write(conn, home)
    await end_trip(conn)
    return home


async def forget(conn: AsyncConnection, now: datetime) -> None:
    """No home: nothing is scoped home or travel and no trip goes on until the user sets one, or Pisa has seen
    LEARN_NIGHTS more nights."""
    await set_setting(conn, HOME_KEY, Jsonb({"forgotten_at": now.isoformat()}))
    await end_trip(conn)


async def confirm(conn: AsyncConnection) -> rules.Home | None:
    home = await read(conn)
    if home is None:
        return None
    confirmed = rules.Home(home.lat, home.lon, home.name, home.source, True, home.place_id)
    await write(conn, confirmed, forgotten=await forgotten_at(conn))
    return confirmed
