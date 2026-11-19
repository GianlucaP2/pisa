"""The briefing knows the place: the travel time to the first timed event with a place and when to
leave, and a warning when a task's place closes before the task's time is up; both in the template too, and
the word limit holds."""

import asyncio
import math
import uuid
from datetime import timedelta
from typing import Any

import psycopg
from pydantic_ai.messages import ModelRequest, UserPromptPart

from pisa.services.briefing import BriefingService
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, add_task, rome, set_settings
from tests.conftest import Clock, FakeModels, dsn
from tests.owner import owner_at
from tests.places_env import at_location, place_env
from tests.places_fake import SPEED_M_PER_MIN, FakePlaces, _metres

NOW = rome(2026, 10, 20, 7, 30)  # Tuesday morning
HOME = (45.479, 9.102)
MARIO = (45.465, 9.094)


def briefing(db: psycopg.Connection, secrets_key: str, models: FakeModels) -> str:
    async def go() -> str:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(NOW), models) as env:
            tasks = TasksService(env.pool, owner=owner_at(), clock=env.clock)
            service = BriefingService(
                env.pool,
                env.places._gateway,
                tasks,
                owner=owner_at(),
                clock=env.clock,
                places=env.places,
            )
            return (await service.ask()).text

    return asyncio.run(go())


def facts(models: FakeModels) -> str:
    request = next(m for m in models.seen_messages[0] if isinstance(m, ModelRequest))  # type: ignore[attr-defined]
    return "".join(str(p.content) for p in request.parts if isinstance(p, UserPromptPart))


def place_for(db: psycopg.Connection, secrets_key: str, query: str) -> str:
    async def go() -> Any:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(NOW)) as env:
            return (await env.places.search(query, near=None, limit=1))[0]["id"]

    return asyncio.run(go())


def transit_minutes(a: tuple[float, float], b: tuple[float, float]) -> int:
    return math.ceil(round(_metres(a, b) / SPEED_M_PER_MIN["transit"] * 60) / 60)


def test_travel_time_and_when_to_leave(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    at_location(db, HOME, NOW - timedelta(minutes=1))
    mario = place_for(db, secrets_key, "Trattoria da Mario")
    event = add_event(db, "Dentista", rome(2026, 10, 20, 9), location="Via Immaginaria 1")
    add_event(db, "Riunione", rome(2026, 10, 20, 8, 30))  # earlier, but no place: not the one
    db.execute("UPDATE calendars SET access_role = 'owner'")  # Their own calendar, synced
    db.execute("UPDATE calendar_events SET place_id = %s WHERE id = %s", (mario, event))
    models = FakeModels()
    models.default = "Alle 9 hai il dentista: esci alle 8:52."
    assert briefing(db, secrets_key, models) == "Alle 9 hai il dentista: esci alle 8:52."
    minutes = transit_minutes(HOME, MARIO)
    leave = rome(2026, 10, 20, 9) - timedelta(minutes=minutes)
    assert (
        f"Travel to Dentista (Trattoria da Mario) from where the user is: {minutes} min by public "
        f"transport; "
        f"leave at {leave:%H:%M}." in facts(models)
    )


def test_the_template_says_it_too(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    at_location(db, HOME, NOW - timedelta(minutes=1))
    mario = place_for(db, secrets_key, "Trattoria da Mario")
    event = add_event(db, "Dentista", rome(2026, 10, 20, 9))
    db.execute("UPDATE calendar_events SET place_id = %s WHERE id = %s", (mario, event))
    models = FakeModels()
    models.default = RuntimeError("provider down")
    minutes = transit_minutes(HOME, MARIO)
    leave = rome(2026, 10, 20, 9) - timedelta(minutes=minutes)
    assert briefing(db, secrets_key, models) == (
        f"Oggi hai 1 evento; il primo alle 09:00: Dentista. Per Dentista esci alle {leave:%H:%M} "
        f"({minutes} min con i mezzi)."
    )


def test_no_fresh_location_no_travel(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    at_location(db, HOME, NOW - timedelta(minutes=20))
    mario = place_for(db, secrets_key, "Trattoria da Mario")
    event = add_event(db, "Dentista", rome(2026, 10, 20, 9))
    db.execute("UPDATE calendar_events SET place_id = %s WHERE id = %s", (mario, event))
    models = FakeModels()
    models.default = "Ok."
    briefing(db, secrets_key, models)
    assert "Travel to" not in facts(models)


def test_a_place_that_closes_before_the_task_is_due(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"search": ["google", "apple"]}})
    poste = place_for(db, secrets_key, "Poste Italiane")
    task = add_task(db, "Spedire il pacco", rome(2026, 10, 20).date())  # due today (by 23:59)
    other = add_task(db, "Ritirare la raccomandata", rome(2026, 10, 20, 11))  # due before closing: fine
    db.execute(
        "UPDATE tasks SET place_id = %s WHERE id = ANY(%s)", (poste, [uuid.UUID(task), uuid.UUID(other)])
    )
    models = FakeModels()
    models.default = "La posta chiude alle 13:35: spedisci il pacco prima."
    briefing(db, secrets_key, models)
    text = facts(models)
    assert "Warning: Poste Italiane closes at 13:35 (task: Spedire il pacco)." in text
    assert "task: Ritirare la raccomandata" not in text
    models = FakeModels()
    models.default = RuntimeError("provider down")
    assert briefing(db, secrets_key, models).endswith("Poste Italiane chiude alle 13:35.")


def test_the_word_limit_holds(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"max_words": 20}})
    at_location(db, HOME, NOW - timedelta(minutes=1))
    mario = place_for(db, secrets_key, "Trattoria da Mario")
    event = add_event(db, "Dentista", rome(2026, 10, 20, 9))
    db.execute("UPDATE calendar_events SET place_id = %s WHERE id = %s", (mario, event))
    models = FakeModels()
    models.default = "Alle nove hai il dentista in via Immaginaria. " * 4  # 32 words, twice
    text = briefing(db, secrets_key, models)
    assert len(text.split()) <= 20
    request = next(m for m in models.seen_messages[0] if isinstance(m, ModelRequest))  # type: ignore[attr-defined]
    assert "at most 20 words" in (request.instructions or "")
