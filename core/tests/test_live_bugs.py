"""Three bugs from a usage report, each test built from what went wrong.

1. The briefing's model call failed every morning ("OpenAI returned a response Pisa could not use"): a
   reasoning model spent the whole output limit (60 words x 4 = 240 tokens) thinking and answered nothing.
2. "Esci ora" at 00:01 and 01:18 for "Acquista i biglietti per Nachtlicht…", an online task with no real
   place.
3. A three-hour walk to the next town in the morning briefing.
"""

from datetime import timedelta
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
from structlog.testing import capture_logs

from tests.conftest import FakeModels
from tests.places_env import at_location
from tests.test_briefing_compose import NOW, compose, day  # noqa: F401 (the fixture)

THINKING = 1500  # what the fake reasoning model spends thinking before it writes a word


def reasoning_model(answer: str) -> Any:
    """Like a reasoning model: its thinking counts against max_tokens; when that runs out, no text at all."""

    def reply(messages: list[Any], info: Any) -> ModelResponse:
        limit = (info.model_settings or {}).get("max_tokens") or 10**6
        if limit <= THINKING:
            return ModelResponse(parts=[ThinkingPart("…")], finish_reason="length")
        return ModelResponse(parts=[ThinkingPart("…"), TextPart(answer)], finish_reason="stop")

    return reply


def test_the_briefing_leaves_room_for_a_reasoning_models_thinking(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    fake = FakeModels()
    fake.default = reasoning_model("Alle 9 hai il dentista in Via Immaginaria 1.")
    with capture_logs() as logs:
        text = compose(db, secrets_key, fake)
    assert text == "Alle 9 hai il dentista in Via Immaginaria 1."  # the model's words, not the fixed fallback
    assert not [e for e in logs if e["event"] == "briefing_fallback"]


def test_an_unusable_answer_logs_its_cause_without_content(db, secrets_key, day) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    fake = FakeModels()

    def always_thinking(messages: list[Any], info: Any) -> ModelResponse:
        return ModelResponse(parts=[ThinkingPart("Il dentista…")], finish_reason="length")

    fake.default = always_thinking
    with capture_logs() as logs:
        text = compose(db, secrets_key, fake)
    assert "Dentista" in text  # the fixed fallback still says the day
    (fallback,) = [e for e in logs if e["event"] == "briefing_fallback"]
    assert (fallback["cause"], fallback["detail"]) == ("provider_error", "token_limit")
    assert "dentista" not in str(fallback).lower()


# --- 2. an online task has no travel and no "leave now" -----------------------------------------------------


@pytest.mark.usefixtures("owner_plans_every_task")
def test_an_online_task_from_an_email_gets_no_travel_and_no_leave_now(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome, set_settings
    from tests.test_planner import OFFICE, place, run, with_place

    set_settings(db, {"quiet": {"hours": None}, "places": {"default_mode": "walk"}})
    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    venue = place(db, "Arena Noord", (45.4690, 9.1120))  # the event's venue: not where buying tickets happens
    tickets = add_task(db, "Acquista i biglietti per Nachtlicht", rome(2026, 10, 20, 18), place="Arena Noord")
    pens = add_task(db, "Comprare le penne", rome(2026, 10, 20, 18), place="Cartoleria")  # the user's own
    db.execute("UPDATE tasks SET source_kind = 'email' WHERE id = %s", (tickets,))
    with_place(db, "tasks", tickets, venue)
    with_place(db, "tasks", pens, place(db, "Cartoleria", (45.468, 9.09)))
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    items = plan["items"]
    by_title = {i.get("title"): i for i in items if i["kind"] == "task"}
    assert by_title["Acquista i biglietti per Nachtlicht"].get("place_id") is None
    trips_to = [items[k + 1].get("title") for k, i in enumerate(items[:-1]) if i["kind"] == "travel"]
    assert "Acquista i biglietti per Nachtlicht" not in trips_to  # no trip, so no "Esci ora" for it
    assert "Comprare le penne" in trips_to  # a place the user gave still has its trip


def test_intake_gives_an_online_task_no_place(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.intake_fake import IntakeModel, task
    from tests.test_intake import add_email, intake, tasks

    add_email(db, "m-tickets", "Nachtlicht: i biglietti sono in vendita", "Sabato al Arena Noord, Amsterdam.")
    fake = IntakeModel()
    fake.triage["Nachtlicht: i biglietti sono in vendita"] = "action_required"
    fake.extract["Nachtlicht: i biglietti sono in vendita"] = {
        "tasks": [task("Acquista i biglietti per Nachtlicht", place_text="Arena Noord, Amsterdam")]
    }
    intake(db, secrets_key, fake)
    assert [(row[0], row[3]) for row in tasks(db)] == [("Acquista i biglietti per Nachtlicht", None)]


# --- 3. never a three-hour walk -----------------------------------------------------------------------------

VEENMOND = (45.6020, 9.0870)  # ~15 km north of here: about 3 hours on foot, 50 minutes by transit


def test_a_walk_over_45_minutes_is_planned_by_transit(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_event, rome, set_settings
    from tests.places_env import HERE_MILAN
    from tests.test_planner import place, run, with_place

    set_settings(db, {"quiet": {"hours": None}, "places": {"default_mode": "walk"}})
    at_location(db, HERE_MILAN, rome(2026, 10, 20, 8, 29))
    event = add_event(db, "Colazione a Veenmond", rome(2026, 10, 20, 10), minutes=60)
    with_place(db, "calendar_events", event, place(db, "Veenmond", VEENMOND))
    plan = run(db, secrets_key, rome(2026, 10, 20, 8, 30), lambda p, env: p.plan())
    (trip,) = [i for i in plan["items"] if i["kind"] == "travel"]
    assert trip["mode"] == "transit" and trip["minutes"] < 60 and "public transport" in trip["title"]


def test_the_briefing_never_proposes_a_walk_that_long(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_event, rome, set_settings
    from tests.test_briefing_places import NOW as MORNING
    from tests.test_briefing_places import briefing
    from tests.test_planner import place

    set_settings(db, {"places": {"default_mode": "walk"}})
    at_location(db, (45.465, 9.0920), MORNING - timedelta(minutes=1))
    event = add_event(db, "Colazione a Veenmond", rome(2026, 10, 20, 10), minutes=60)
    db.execute(
        "UPDATE calendar_events SET place_id = %s WHERE id = %s", (place(db, "Veenmond", VEENMOND), event)
    )
    models = FakeModels()
    models.default = RuntimeError("provider down")  # the fixed template: deterministic
    text = briefing(db, secrets_key, models)
    assert "con i mezzi" in text and "a piedi" not in text
    minutes = int(text.split("(")[1].split(" min")[0])
    assert minutes < 60
