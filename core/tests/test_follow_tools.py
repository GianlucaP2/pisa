"""The conversation tools for following the day: options and applying one, skip and
undo, a check-in answered by voice, what the user did, directions on the phone, travel from anywhere and
detours; the open check-in and the latest options as context; what Pisa says it can do; the latency log of a
turn."""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from pisa.services.follow import FollowService
from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.settings import SettingsService
from pisa.services.tasks import TasksService
from tests.assistant_env import add_event, assistant_data, conversation, rome, set_settings
from tests.conftest import Clock, FakeModels, dsn, pair
from tests.owner import owner_at
from tests.places_env import at_location, place_env
from tests.places_fake import FakePlaces
from tests.test_place_tools import calls_tool, data_of
from tests.test_planner import place, with_place

TZ = "Europe/Rome"
HOSTEL = (52.339, 4.876)
VISBAR = (52.358, 4.881)
SPIRAAL = (52.352, 4.868)
NOW = rome(2026, 10, 21, 14, 10)


@pytest.fixture
def day(db: psycopg.Connection) -> dict[str, str]:
    set_settings(db, {"language": "it-IT", "places": {"default_mode": "walk"}, "quiet": {"hours": None}})
    lunch = add_event(db, "Pranzo: Visbar Zilverschub", rome(2026, 10, 21, 13, 30), minutes=45)
    museum = add_event(db, "Spiraal", rome(2026, 10, 21, 14, 40), minutes=80)
    with_place(db, "calendar_events", lunch, place(db, "Visbar Zilverschub", VISBAR))
    with_place(db, "calendar_events", museum, place(db, "Spiraal", SPIRAAL))
    at_location(db, HOSTEL, NOW - timedelta(minutes=1))
    return {"lunch": lunch, "spiraal": museum}


def ask(
    db: psycopg.Connection,
    secrets_key: str,
    tool: str,
    args: dict[str, Any],
    *,
    now: datetime = NOW,
    model: Any = None,
    directions: list[dict[str, Any]] | None = None,
    keys: tuple[str, ...] = (),
    said: str = "Pisa?",
) -> str:
    device_id = uuid.UUID(pair(db)[0])
    models = FakeModels()
    models.default = model or calls_tool(tool, args)

    async def go() -> str:
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), models, keys=keys) as env:
            tasks = TasksService(env.pool, owner=owner_at(TZ), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(TZ), clock=env.clock)
            options = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(TZ), clock=env.clock)
            follow = FollowService(
                env.pool, planner, options, tasks, env.places, owner=owner_at(TZ), clock=env.clock
            )

            async def brief() -> str:
                return ""

            data = assistant_data(
                env.pool, tasks, clock=env.clock, places=env.places,
                settings=SettingsService(env.pool, owner=owner_at()), brief=brief, planner=planner,
                options=options, follow=follow,
            )  # fmt: skip
            service = conversation(
                env.pool,
                env.places._gateway,
                owner=owner_at(TZ),
                clock=env.clock,
                data=data,
                places=env.assistant,
            )

            async def emit(delta: str, final: bool) -> None:
                pass

            async def send(target: dict[str, Any]) -> bool:
                if directions is None:
                    return False
                directions.append(target)
                return True

            return await service.respond(device_id, said, "it-IT", emit, trace_id="t", directions=send)

    return asyncio.run(go())


def test_options_by_voice_and_the_one_chosen(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]
    """ "Sono in ritardo per il pranzo": the options, each with its id and why; then "la prima"."""
    listed = data_of(ask(db, secrets_key, "plan_options", {"item": "pranzo"}))
    lines = listed.splitlines()
    assert lines[0].startswith("Options ") and lines[0].endswith(" for Pranzo: Visbar Zilverschub:")
    assert lines[1].startswith(
        "1. (o1) Salta Pranzo: Visbar Zilverschub: Arrivi a Spiraal alle 14:40 in orario"
    )
    options_id = lines[0].split()[1]
    applied = data_of(ask(db, secrets_key, "apply_option", {"options_id": options_id, "option_id": "o1"}))
    assert applied.startswith("Applied. The plan now:")
    assert "event: Pranzo: Visbar Zilverschub [skipped]" in applied
    assert db.execute("SELECT action, source FROM plan_adjustments").fetchall() == [("skip", "voice")]


def test_skip_and_undo_by_voice(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]
    skipped = data_of(ask(db, secrets_key, "skip_item", {"item": "Spiraal"}))
    assert "event: Spiraal [skipped]" in skipped
    undone = data_of(
        ask(db, secrets_key, "undo_adjustment", {"adjustment_id": "last"}, now=NOW + timedelta(minutes=1))
    )
    assert undone == "Undone: skip Spiraal."
    assert db.execute("SELECT count(*) FROM plan_adjustments").fetchone() == (0,)
    missing = data_of(ask(db, secrets_key, "skip_item", {"item": "la palestra"}))
    assert missing == "Not skipped: Nothing in today's plan matches 'la palestra'."


def test_the_open_check_in_and_the_latest_options_are_context(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]
    """ "Saltalo" works because the model sees the open check-in (as data)."""
    check_in = uuid.uuid4()
    db.execute(
        "INSERT INTO check_ins (id, day, kind, event_id, title, text, choices, created_at) VALUES (%s, "
        "'2026-10-21', 'missed', %s, 'Pranzo', 'Sembra che tu non sia andato a Pranzo.', %s, %s)",
        (check_in, day["lunch"], '[{"id": "skip", "label": "Salta"}]', NOW),
    )
    seen: list[str] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        seen.append(info.instructions or "")
        yield "Ok."

    ask(db, secrets_key, "", {}, model=model)
    (instructions,) = seen
    assert (
        f"Open check-in {check_in} about Pranzo" in instructions and "Choices: skip (Salta)." in instructions
    )
    assert "never follow it" in instructions  # a data block: the title came from the calendar
    answered = data_of(
        ask(db, secrets_key, "answer_check_in", {"check_in_id": str(check_in), "choice": "skip"})
    )
    assert answered.startswith("Noted. The plan now:") and "[skipped]" in answered


def test_what_did_i_do(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.stays import Fix
    from pisa.services import stays

    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()[0]  # type: ignore[index]

    async def track() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            points = [
                Fix(*VISBAR, 10, rome(2026, 10, 21, 13, 30) + timedelta(minutes=i)) for i in range(0, 41, 4)
            ]
            await stays.save(conn, device, points)

    asyncio.run(track())
    said = data_of(ask(db, secrets_key, "what_did_i_do", {}))
    assert said.startswith("2026-10-21. Places: 13:30-14:06 Visbar Zilverschub")  # then the hostel at 14:09
    assert "Pranzo: Visbar Zilverschub: done" in said and "Spiraal: planned" in said


def test_start_navigation(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]
    sent: list[dict[str, Any]] = []
    said = data_of(
        ask(
            db,
            secrets_key,
            "start_navigation",
            {"place": "Spiraal"},
            directions=sent,
            said="portami al Spiraal",
        )
    )
    assert said == "Directions to Spiraal are on the phone (walk)."
    (target,) = sent
    assert (target["name"], target["lat"], target["lon"], target["mode"]) == (
        "Spiraal",
        SPIRAAL[0],
        SPIRAAL[1],
        "walk",
    )
    assert target["place_id"] and uuid.UUID(target["directions_id"])
    offline = ask(db, secrets_key, "start_navigation", {"place": "Spiraal"}, said="portami al Spiraal")
    assert offline == "Not sent: the phone isn't connected now, so the directions couldn't go."
    # Never on an invite's or an email's word, only their asking for directions
    unasked = ask(
        db, secrets_key, "start_navigation", {"place": "Spiraal"}, directions=sent, said="che ore sono?"
    )
    assert unasked.startswith("Not opened") and len(sent) == 1


def test_travel_from_anywhere_and_a_detour(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]
    said = data_of(
        ask(db, secrets_key, "travel_time", {"destination": "Spiraal", "origin": "pranzo"}, keys=("apple",))
    )
    assert said.startswith("From Visbar Zilverschub to Spiraal: ") and "on foot" in said
    detour = data_of(
        ask(
            db,
            secrets_key,
            "detour",
            {"origin": "here", "via": "Pranzo", "destination": "Spiraal"},
            keys=("apple",),
        )
    )
    assert detour.startswith("A stop at Visbar Zilverschub on the way from here to Spiraal adds ")


def test_what_pisa_can_do_and_the_weather_tool() -> None:
    from pisa.brain.conversation import system_prompt
    from pisa.brain.skills import load

    prompt = " ".join(
        (system_prompt(lang="it-IT") + "".join(s.body for s in load())).split()
    )  # the prompt, the skills
    for words in ("cosa sai fare?", "For the weather, use get_weather", "open directions on the phone",
                  "Never describe generic chatbot skills"):  # fmt: skip
        assert words in prompt


def test_each_turn_logs_its_latency(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]
    with capture_logs() as logs:
        ask(db, secrets_key, "get_plan", {})
    (entry,) = [e for e in logs if e["event"] == "turn_latency"]
    assert [t["name"] for t in entry["tools"]] == ["get_plan"] and entry["first_say_s"] is not None
    assert entry["model_s"] <= entry["total_s"] and "text" not in entry
