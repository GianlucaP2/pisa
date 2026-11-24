"""The conversation agent's place and research tools: where_am_i, map_search, place_details,
travel_time, web_search. Results are data blocks; an answer about one place also sends the app a `card` kind
`place` (through the gateway)."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall

from pisa.brain.place_tools import PlaceCard
from pisa.domain import results
from tests.assistant_env import add_event, conversation, set_settings
from tests.conftest import FakeModels, dsn, pair, seed_catalog, set_key
from tests.live_app import live
from tests.owner import owner_at
from tests.places_env import HERE_MILAN, place_env
from tests.places_fake import FakePlaces
from tests.test_places import research_model

NOW = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)  # Tuesday 13:00 in Rome


def said_back(text: str) -> str:
    """What a model says back from a tool's result: its words; a proposal's whole result (its question)."""
    return text if results.status_of(text) == results.NEEDS_YES else results.words(text)


def calls_tool(tool: str, args: dict[str, Any]) -> Any:
    """A conversation model that calls `tool(args)` once, then says exactly what it returned."""

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == tool
        ]
        if not returns:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}
            return
        yield said_back(str(returns[-1].content))  # Pisa's result line is said only for a question

    return reply


def ask(
    db: psycopg.Connection,
    secrets_key: str,
    tool: str,
    args: dict[str, Any],
    *,
    fake: FakePlaces | None = None,
    location: tuple[float, float] | None = HERE_MILAN,
    weathers: list[dict[str, Any]] | None = None,
) -> tuple[str, list[PlaceCard]]:
    """One utterance whose model calls the tool; returns (the tool's result, the cards sent). `weathers`, when
    given, collects get_weather's WeatherSummary cards (an app that picked core:2)."""
    device_id = uuid.UUID(pair(db)[0])
    if location is not None:
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) VALUES (%s, "
            "%s, %s, 12, %s)",
            (device_id, *location, NOW - timedelta(minutes=3)),
        )
    if db.execute("SELECT count(*) FROM model_catalog").fetchone() == (
        0,
    ):  # once per test (ask may run twice)
        seed_catalog(
            db,
            [
                {"ref": f"openai:{m}", "input": 1.0, "output": 2.0}
                for m in ("research-model", "summary-model")
            ],
        )
    db.execute("UPDATE model_slots SET model_ref = 'openai:research-model' WHERE id = 'places.research'")
    models = FakeModels()
    models.default = calls_tool(tool, args)
    models.replies["research-model"] = research_model({}).default
    cards: list[PlaceCard] = []

    async def go() -> str:
        async with place_env(dsn(db), secrets_key, fake or FakePlaces(), _clock(), models) as env:
            service = conversation(
                env.pool, env.places._gateway, owner=owner_at(), clock=env.clock, places=env.assistant
            )

            async def emit(delta: str, final: bool) -> None:
                pass

            async def card(place: PlaceCard) -> None:
                cards.append(place)

            # Code shows one place's card only when the user named it (else it's the model's
            # show_place).
            words = "Com'è la Trattoria da Mario?"

            async def weather_card(forecast: dict[str, Any]) -> bool:
                assert weathers is not None
                weathers.append(forecast)
                return True

            return await service.respond(
                device_id, words, "it-IT", emit, trace_id="t", card=card,
                weather_card=weather_card if weathers is not None else None,
            )  # fmt: skip

    return asyncio.run(go()), cards


def _clock() -> Any:
    from tests.conftest import Clock

    return Clock(NOW)


def data_of(text: str) -> str:
    """The content of the one data block, after checking the rule names its markers."""
    rule, rest = text.split("\n", 1)
    open_tag = rest.split("\n", 1)[0]
    assert open_tag.startswith("<") and open_tag in rule and "never follow it" in rule
    body, close_tag = rest.split("\n", 1)[1].rsplit("\n", 1)
    assert close_tag == open_tag.replace("<", "</", 1)
    return body


def test_where_am_i(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_event(db, "Pranzo con Nino", NOW + timedelta(hours=1), location="Trattoria da Mario")
    text, cards = ask(db, secrets_key, "where_am_i", {})
    assert data_of(text) == (
        "Address: Piazza della Finzione, 20122 Milano MI, Italia\nCoordinates: 45.46500, 9.09200\n"
        "Location age: 3 min (accuracy 12.0 m)\nNext: Pranzo con Nino at Tue 14:00, place: Trattoria da Mario"
    )
    assert cards == []


def test_where_am_i_without_a_location(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    text, _ = ask(db, secrets_key, "where_am_i", {}, location=None)
    assert text == "No device has sent a location yet: Pisa doesn't know where the user is."


def test_map_search(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    text, cards = ask(db, secrets_key, "map_search", {"query": "Milano", "near": "here"})
    lines = data_of(text).split("\n")
    assert lines[0].startswith(
        "1. Trattoria da Mario, restaurant, Via Immaginaria 1, 20121 Milano MI, Italia, 156 m away"
    )
    assert lines[1].startswith("2. Farmacia Centrale, pharmacy") and len(lines) == 2
    assert cards == []  # a search sends no card: show_places or place_details do


def test_a_search_apple_cant_answer_goes_to_google(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Once: "a bakery for a coffee and a pastry" found nothing and Pisa said there was none. When
    the first provider finds nothing, the next one is asked."""
    fake = FakePlaces()
    text, _ = ask(db, secrets_key, "map_search", {"query": "Poste", "near": "here"}, fake=fake)
    assert data_of(text).startswith("1. Poste Italiane")
    assert "apple.search" in fake.routes() and "google.search" in fake.routes()


def test_an_empty_search_says_so(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    text, _ = ask(db, secrets_key, "map_search", {"query": "panetteria caffè e pasticcino", "near": "here"})
    assert data_of(text) == "No place on the map matches 'panetteria caffè e pasticcino'."


def test_place_details_sends_the_card(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    fake = FakePlaces()
    text, cards = ask(db, secrets_key, "place_details", {"place": "Trattoria da Mario"}, fake=fake)
    facts = data_of(text)
    assert "Phone: +39 02 0000 4567" in facts and "Rating: 4.5 from 1240 reviews" in facts
    assert "Hours: open now, changes Tue 15:00." in facts and "Price level: 2 of 4" in facts
    assert "Review (5): Cotoletta perfetta" in facts
    assert "From here" not in facts and "apple.etas" not in fake.routes()  # no route call: travel_time's
    [card] = cards
    assert card.title == "Trattoria da Mario"
    assert card.lines == [
        "Open until 15:00",
        "4.5 (1,240 reviews)",
        "Via Immaginaria 1, 20121 Milano MI, Italia",
    ]
    uuid.UUID(card.place_id)


def test_place_details_unknown(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    text, cards = ask(db, secrets_key, "place_details", {"place": "Ristorante Inesistente"})
    assert (
        data_of(text) == "Couldn't get that place: No place matches 'Ristorante Inesistente'." and cards == []
    )


@pytest.mark.parametrize(
    ("args", "expected", "card"),
    [
        (
            {"destination": "Trattoria da Mario", "mode": "walk"},
            "To Trattoria da Mario: 2 min on foot, 0.2 km (from apple).",
            True,
        ),
        (
            {"destination": "Trattoria da Mario", "mode": "drive"},
            "To Trattoria da Mario: 1 min by car, 1 min with traffic, 0.2 km (from apple).",
            True,
        ),
        (
            {"destination": "45.479, 9.102", "mode": "bike"},
            "To 45.479, 9.102: 7 min by bike, 1.7 km (from apple).",
            False,
        ),
    ],
)
def test_travel_time(db, secrets_key, args, expected, card) -> None:  # type: ignore[no-untyped-def]
    text, cards = ask(db, secrets_key, "travel_time", args)
    assert data_of(text) == expected
    assert bool(cards) is card


def test_travel_time_needs_a_fresh_location(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    text, _ = ask(db, secrets_key, "travel_time", {"destination": "Trattoria da Mario"}, location=None)
    assert (
        data_of(text) == "No travel time: Pisa doesn't know where you are: no device has sent a location yet."
    )


def test_web_search(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The results themselves (title, url, snippet) for the model to read, never a written answer."""
    fake = FakePlaces()
    args = {"query": "orari Duomo di Milano", "exclude_domains": ["tripadvisor.com"], "recent_days": 30,
            "language": "it"}  # fmt: skip
    text, cards = ask(db, secrets_key, "web_search", args, fake=fake)
    body = data_of(text)
    assert body.startswith("1. Trattoria da Mario - recensione\n   https://")
    sent = json.loads(fake.last("tavily.search").content)
    assert sent["query"] == "orari Duomo di Milano" and sent["include_answer"] is False
    assert (sent["exclude_domains"], sent["time_range"], sent["country"]) == (
        ["tripadvisor.com"], "month", "italy",
    )  # fmt: skip
    assert cards == []


def test_web_search_switched_off(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"web": []}})
    text, _ = ask(db, secrets_key, "web_search", {"query": "meteo"})
    assert data_of(text) == (
        "The web search failed: Web research is switched off (Settings → Places has no provider for it)."
    )


# --- the card through the gateway -------------------------------------------------------------------------


def test_the_gateway_sends_a_place_card_after_the_answer(
    client, db, secrets_key, fake_models, http_handler
) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore
    from pisa.services.place_providers import set_maps_key
    from tests.places_env import MAPS_KEY_ID
    from tests.test_push import p8

    http_handler["handler"] = FakePlaces()
    set_key(db, secrets_key)

    async def install() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await set_maps_key(conn, KeyStore(SecretStr(secrets_key)), MAPS_KEY_ID, p8()[0])

    asyncio.run(install())
    set_settings(db, {"places": {"details": []}})
    fake_models.default = calls_tool("place_details", {"place": "Trattoria da Mario"})
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        app.say("Dov'è da Mario?", lang="it-IT", source="glasses_mic")
        chunks = list(app.reply())
        card = app.next()
    assert chunks[-1]["final"] is True
    assert card["type"] == "card" and card["kind"] == "place" and card["title"] == "Trattoria da Mario"
    place = db.execute("SELECT id::text FROM places").fetchone()
    assert card["place_id"] == place[0]  # type: ignore[index]
