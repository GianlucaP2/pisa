"""the LOW line (L1-L4), each with a test that fails without its fix."""

import asyncio
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from tests.assistant_env import add_event, rome, set_settings
from tests.owner import owner_at
from tests.places_env import HERE_MILAN, at_location
from tests.test_planner import place, with_place

# --- L1: in the rain the switch skips the bike --------------------------------------------------------------


def rainy_plan(db: psycopg.Connection, secrets_key: str, modes: list[str]) -> list[dict[str, Any]]:
    """As test_weather's rain rule: a 2.5 km walk to a 14:00 event in the rainy hour (13:00-14:00)."""
    from pisa.services.planner import PlannerService
    from pisa.services.tasks import TasksService
    from tests.conftest import Clock, dsn
    from tests.places_env import place_env
    from tests.places_fake import FakePlaces

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}, "places": {"modes": modes}})
    far = place(db, "Fondazione Inventata", (45.445, 9.108))
    wet = add_event(db, "Mostra", rome(2026, 10, 20, 14), minutes=60)
    with_place(db, "calendar_events", wet, far)
    now = rome(2026, 10, 20, 12, 30)
    at_location(db, HERE_MILAN, now - timedelta(minutes=1))

    async def go():  # type: ignore[no-untyped-def]
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(), clock=env.clock,
                                     weather=env.weather)  # fmt: skip
            return await planner.plan()

    return [i for i in asyncio.run(go())["items"] if i["kind"] == "travel"]


def test_the_rain_never_switches_to_the_bike(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    (trip,) = rainy_plan(db, secrets_key, ["walk", "bike", "transit"])
    assert trip["mode"] == "transit" and trip["title"] == f"Piove: {trip['minutes']} min con i mezzi"
    assert trip["rain"] is True


def test_walk_and_bike_only_stay_on_foot_in_the_rain(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    (trip,) = rainy_plan(db, secrets_key, ["walk", "bike"])
    assert trip["mode"] == "walk" and "rain" not in trip and "bici" not in trip["title"]


def test_a_long_dry_walk_still_takes_the_bike() -> None:
    from pisa.services.planner import next_mode

    settings = {"places": {"modes": ["walk", "bike", "transit"]}}
    assert next_mode(settings) == ("bike", "bike")
    assert next_mode(settings, rain=True) == ("transit", "transit")
    assert next_mode({"places": {"modes": ["walk", "bike"]}}, rain=True) is None


# --- L2: the sources card's links: web pages only, whole or not at all --------------------------------------


def test_a_link_too_long_or_a_host_too_long_is_dropped_not_cut() -> None:
    from pisa.brain.research_turn import ResearchTurn
    from pisa.services.session_messages import Card, SourceLink

    long_url = "https://www.veerdienst.example/en/ferry?" + "q=" + "x" * 2000
    long_host = "https://" + "a" * 250 + ".example.com/page"
    good = "https://www.veerdienst.example/en/ferry/veenmond-tickets"
    turn = ResearchTurn(lang="it-IT", prompt_chars=0)
    turn.urls = {
        long_url: "Too long",
        long_host: "Host too long",
        good: "Ferry tickets",
        "ftp://x.nl/a": "ftp",
    }
    sources = turn.sources()
    assert sources == [
        {"title": "Ferry tickets", "url": good, "host": "veerdienst.example"}
    ]  # whole, never cut
    card = Card(card_id="00000000-0000-4000-8000-000000000441", title="Fonti", lines=[], kind="sources",
                sources=[SourceLink.model_validate(x) for x in sources])  # fmt: skip
    assert card.sources is not None


def test_the_contract_takes_web_links_only() -> None:
    import pydantic

    from pisa.services.session_messages import SourceLink

    with pytest.raises(pydantic.ValidationError):
        SourceLink(title="x", url="javascript:alert(1)", host="x")
    assert SourceLink(title="x", url="http://a.nl/", host="a.nl").url == "http://a.nl/"


# --- L4: a docstring belongs to what it documents -----------------------------------------------------------


def test_send_places_keeps_its_docstring() -> None:
    import ast
    from pathlib import Path

    import pisa.services.conversation as module

    body = ast.parse(Path(module.__file__).read_text()).body
    at = next(
        i for i, n in enumerate(body)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SendPlaces" for t in n.targets)
    )  # fmt: skip
    after = body[at + 1]
    assert isinstance(after, ast.Expr) and isinstance(after.value, ast.Constant)
    assert "places" in str(after.value.value)


# --- L3: plan changes in a turn that read outside text wait for the user's yes -----------------------------


def l3_turn(
    db: psycopg.Connection, secrets_key: str, words: str, steps: list[Any], device: Any = None
) -> Any:
    from tests.test_research_eval import research
    from tests.test_sources_card import boat_world

    return research(db, secrets_key, words, steps, "Ok.", world=boat_world(), device=device, with_data=True)


READ_WEB = ("web_search", {"query": "traghetto Veenmond biglietti"})  # results: untrusted text


def test_add_task_after_reading_the_web_waits_for_yes(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A task the user named acts even after the web (test_acting); one the page
    suggested waits for their yes."""
    t = l3_turn(db, secrets_key, "cerca il traghetto per Veenmond",
                [READ_WEB, ("add_task", {"title": "Comprare i biglietti del traghetto"})])  # fmt: skip
    assert t.result("add_task").startswith("Not done yet: this turn read text from outside")
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
    assert t.reply.endswith("Posso aggiungere il compito «Comprare i biglietti del traghetto»?")
    yes = l3_turn(db, secrets_key, "sì", [("confirm_remember", {})], device=t.device)
    assert "\nAdded: «Comprare i biglietti del traghetto», open." in yes.result("confirm_remember")
    assert db.execute("SELECT title FROM tasks").fetchall() == [("Comprare i biglietti del traghetto",)]


def test_skip_and_stop_after_reading_wait_too_and_a_no_drops_them(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = l3_turn(db, secrets_key, "cosa dice il sito?", [READ_WEB, ("skip_item", {"item": "pranzo"})])
    assert t.result("skip_item").startswith("Not done yet")
    no = l3_turn(db, secrets_key, "no lascia stare", [("confirm_remember", {})], device=t.device)
    assert no.result("confirm_remember").startswith("Nothing done")  # a no (or anything else) drops it
    lunch = {"when": "before", "what": "pranzo", "relative_to": "festival"}
    stop = l3_turn(db, secrets_key, "che c'è al festival?", [READ_WEB, ("plan_stop", lunch)])
    assert stop.result("plan_stop").startswith("Not done yet")
    assert stop.reply.endswith("Posso mettere nel piano «pranzo» prima di «festival»?")
    assert db.execute("SELECT count(*) FROM plan_adjustments").fetchone() == (0,)


def test_without_outside_text_the_change_is_done_at_once(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = l3_turn(db, secrets_key, "segnati di chiamare Paola", [("add_task", {"title": "Chiamare Paola"})])
    assert '"status": "done"' in t.result("add_task")  # Done at once; the model says it
