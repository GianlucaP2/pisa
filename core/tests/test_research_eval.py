"""Pisa researches like a person would: 12 recorded requests against a fake web and fake maps
(tests/research_world.py), each run through the real conversation service with a scripted model.

The strategy belongs to the model, so these don't test one: each script is a plausible plan, and the tests
check what Pisa's code owns: the tools offered (the general ones, not the old pipelines), the data blocks, the
say-at-once line, the budget, the costs, the `places` card (only places and sources the turn saw) and that
hostile content stays data. The live run with the real model is the operator's, after deploy.
"""

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import psycopg
import pytest
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall

from pisa.brain import research_turn
from pisa.brain.place_tools import PlaceCard
from pisa.domain import results
from pisa.infra.hard_limit import DEFAULT, HardLimits
from pisa.services.lexicon import LexiconService
from pisa.services.profile import ProfileService
from pisa.services.session_messages import Card, Correction, ShownPlace, SourceLink, Understood, new_id
from tests.assistant_env import conversation, set_settings
from tests.conftest import Clock, FakeModels, dsn, pair, seed_catalog
from tests.live_app import wire_errors
from tests.owner import owner_at
from tests.places_env import HERE_MILAN, place_env
from tests.research_world import ResearchWorld, ids

NOW = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)  # Tuesday 13:00 in Rome
RESEARCH_TOOLS = {"map_search", "place_details", "open_at", "web_search", "read_page", "show_places"}
OLD_TOOLS = {"search_places", "place_info", "research_place"}

Returns = list[tuple[str, str]]
Args = dict[str, Any] | Callable[[Returns], dict[str, Any]]


@dataclass
class Turn:
    reply: str
    said: list[str]
    cards: list[dict[str, Any]]
    place_cards: list[PlaceCard]
    offered: set[str]
    returns: Returns
    world: ResearchWorld
    calls: list[str] = field(default_factory=list)
    device: uuid.UUID | None = None
    instructions: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)  # sources cards sent
    understood: list[dict[str, Any]] = field(default_factory=list)  # understood events sent
    prompt: str = ""  # the user's turn as the model received it this turn (its last UserPromptPart)

    def result(self, tool: str, n: int = 0) -> str:
        return [text for name, text in self.returns if name == tool][n]

    def words(self, tool: str, n: int = 0) -> str:
        """The tool's words, without Pisa's result line (every write gives one)."""
        return results.words(self.result(tool, n))


def script(steps: list[tuple[str, Args]], final: str | Callable[[Returns], str], offered: set[str]) -> Any:
    """A conversation model that follows `steps` (one tool call per response, the arguments possibly built
    from this turn's results so far), then says `final`."""

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        offered.update(t.name for t in info.function_tools)
        start = max(i for i, m in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in m.parts))
        returns = [
            (p.tool_name, str(p.content))
            for m in messages[start:]
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        ]
        if len(returns) < len(steps):
            tool, args = steps[len(returns)]
            built = args(returns) if callable(args) else args
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(built))}
            return
        yield final(returns) if callable(final) else final

    return reply


def research(
    db: psycopg.Connection,
    secrets_key: str,
    utterance: str,
    steps: list[tuple[str, Args]],
    final: str | Callable[[Returns], str] = "Ecco.",
    *,
    world: ResearchWorld | None = None,
    show: bool = True,
    device: uuid.UUID | None = None,
    now: datetime = NOW,
    with_data: bool = False,
    lexicon: bool = False,
    replies: dict[str, Any] | None = None,
    hard: HardLimits = DEFAULT,
) -> Turn:
    """One turn of the user's words through the conversation service (with the user's profile).
    `device`: the same device as an earlier turn (one conversation); else a new one, located at the square."""
    world = world or ResearchWorld()
    device_id = device or uuid.UUID(pair(db)[0])
    if device is None:
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
            "VALUES (%s, %s, %s, 10, %s)",
            (device_id, *HERE_MILAN, now - timedelta(minutes=2)),
        )
    if db.execute("SELECT count(*) FROM model_catalog").fetchone() == (0,):
        seed_catalog(db, [{"ref": "openai:research-model", "input": 1.0, "output": 2.0}])
    offered: set[str] = set()
    models = FakeModels()
    models.default = script(steps, final, offered)
    models.replies.update(replies or {})
    understood: list[dict[str, Any]] = []
    said: list[str] = []
    cards: list[dict[str, Any]] = []
    place_cards: list[PlaceCard] = []
    sources: list[dict[str, Any]] = []
    returns: Returns = []

    async def go() -> str:
        async with place_env(dsn(db), secrets_key, world, Clock(now), models, hard=hard) as env:
            profile = ProfileService(env.pool, owner=owner_at(), clock=env.clock, places=env.places)
            service = conversation(
                env.pool,
                env.places._gateway,
                owner=owner_at(),
                clock=env.clock,
                places=env.assistant,
                profile=profile,
                data=assistant_data(env) if with_data else None,
                lexicon=LexiconService(env.pool, owner=owner_at(), clock=env.clock) if lexicon else None,
            )

            async def emit(delta: str, final: bool) -> None:
                said.append(delta)

            async def card(place: PlaceCard) -> None:
                place_cards.append(place)

            async def places_card(body: dict[str, Any]) -> bool:
                # As the session sends it: its message, then contract 2 as `/v2/live` writes it.
                message = Card(
                    card_id=body.get("card_id") or new_id(),
                    title=body["title"],
                    lines=body["lines"],
                    kind="places",
                    request=body["request"],
                    places=[ShownPlace.model_validate(p) for p in body["places"]],
                )
                assert wire_errors(message) == []
                wire = message.model_dump(mode="json", by_alias=True)
                cards.append(wire)
                return show

            async def sources_card(body: dict[str, Any]) -> bool:
                # As the session sends it: its message, then contract 2 as `/v2/live` writes it.
                message = Card(card_id=new_id(), title=body["title"], lines=body["lines"], kind="sources",
                               sources=[SourceLink.model_validate(s) for s in body["sources"]])  # fmt: skip
                assert wire_errors(message) == []
                wire = message.model_dump(mode="json", by_alias=True)
                sources.append(wire)
                return True

            async def understood_event(body: dict[str, Any]) -> bool:
                # As the session sends it: its message, then contract 2 as `/v2/live` writes it.
                message = Understood(
                    meant=body["meant"], raw=body["raw"], ask=body["ask"],
                    corrections=[Correction.model_validate(c) for c in body["corrections"]],
                )  # fmt: skip
                assert wire_errors(message) == []
                wire = message.model_dump(mode="json", by_alias=True)
                understood.append(wire)
                return True

            return await service.respond(
                device_id, utterance, "it-IT", emit, trace_id="t", card=card, places_card=places_card,
                sources_card=sources_card, understood=understood_event,
            )  # fmt: skip

    reply = asyncio.run(go())
    for messages in cast(list[list[Any]], models.seen_messages[-1:]):  # the last request saw the whole turn
        # The history carries earlier turns' results too: this turn's start after the user's words
        start = max(
            (i for i, m in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in m.parts)),
            default=0,
        )
        returns += [
            (p.tool_name, str(p.content))
            for m in messages[start:]
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        ]
    last = cast(list[Any], models.seen_messages[-1]) if models.seen_messages else []
    instructions = next((str(m.instructions) for m in reversed(last) if getattr(m, "instructions", None)), "")
    prompt = next(
        (str(p.content) for m in reversed(last) for p in m.parts if isinstance(p, UserPromptPart)), ""
    )
    return Turn(
        reply,
        said,
        cards,
        place_cards,
        offered,
        returns,
        world,
        [],
        device_id,
        instructions,
        sources,
        understood,
        prompt,
    )


def assistant_data(env: Any) -> Any:
    """The tasks and plan tools' data (pisa.services.assistant), as the runtime wires it."""
    from pisa.services.assistant import AssistantDataService
    from pisa.services.done import DoneService
    from pisa.services.follow import FollowService
    from pisa.services.plan_options import PlanOptionsService
    from pisa.services.planner import PlannerService
    from pisa.services.settings import SettingsService
    from pisa.services.tasks import TasksService
    from pisa.services.tasks_bulk import TasksBulkService

    tz = "Europe/Rome"
    tasks = TasksService(env.pool, owner=owner_at(tz), clock=env.clock)
    planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(tz), clock=env.clock)
    options = PlanOptionsService(env.pool, planner, env.places, owner=owner_at(tz), clock=env.clock)
    follow = FollowService(env.pool, planner, options, tasks, env.places, owner=owner_at(tz), clock=env.clock)

    async def brief() -> str:
        return ""

    done = DoneService(env.pool, tasks, planner, owner=owner_at(tz), clock=env.clock)
    return AssistantDataService(
        env.pool,
        tasks,
        SettingsService(env.pool, owner=owner_at()),
        brief,
        planner,
        options,
        follow,
        done,
        TasksBulkService(env.pool, tasks),
    )


def data_lines(result: str) -> list[str]:
    """The lines inside a result's data block (after the rule and the opening marker)."""
    return result.split("\n")[2:-1]


def first_id(returns: Returns) -> str:
    return ids([text for name, text in returns if name == "map_search"][-1])[0]


def check(requirement: str, status: str, source: str = "") -> dict[str, str]:
    return {"requirement": requirement, "status": status, "source": source}


# --- 1. a terrace on the river, open now, Italian food ------------------------------------------------------


def test_01_a_terrace_on_the_river_open_now_italian(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    guide = "https://www.guidamilano.it/terrazze-naviglio"
    steps: list[tuple[str, Args]] = [
        ("where_am_i", {}),
        (
            "map_search",
            {"query": "ristorante terrazza naviglio", "near": "here", "open_now": True, "provider": "google"},
        ),
        ("place_details", lambda r: {"place": first_id(r)}),
        ("web_search", {"query": "terrazza naviglio vista", "language": "it"}),
        ("read_page", {"url": guide}),
        (
            "show_places",
            lambda r: {
                "title": "Terrazze sul Naviglio, aperte ora",
                "request": "terrazza sul fiume, aperta, italiana",
                "places": [
                    {
                        "place_id": first_id(r),
                        "why": "Terrazza sul Naviglio, cucina italiana, aperta",
                        "checks": [
                            check("terrazza", "verified", "Google (outdoor seating)"),
                            check("vista sul fiume", "verified", guide),
                            check("aperta ora", "verified", "Google"),
                            check("cucina italiana", "likely", "recensioni"),
                        ],
                        "sources": [guide],
                    }
                ],
            },
        ),
    ]
    turn = research(
        db,
        secrets_key,
        "Una terrazza sul fiume, aperta ora, cucina italiana?",
        steps,
        "Ti consiglio l'Osteria sul Ponte: terrazza sul Naviglio, aperta fino alle 23.",
    )
    # Each tool this script calls was offered (open_at isn't called here: Pydantic AI's tool
    # search now finds walk_times beside place_details in its five)
    assert {t for t, _ in steps} <= turn.offered and not (OLD_TOOLS & turn.offered)
    assert turn.said[0] == "Cerco, un attimo."  # at once, before the answer
    found = turn.result("map_search")
    assert (
        "Osteria sul Ponte" in found and "has outdoor seating" in found and "rated 4.5 (820 reviews)" in found
    )
    assert "Il Cortile" not in found  # not on the Naviglio: the query's words decide, as on a real map
    assert "vista sull'acqua" in turn.result("place_details")
    [card] = turn.cards
    [place] = card["places"]
    assert place["name"] == "Osteria sul Ponte" and place["open_now"] is True and place["rating"] == 4.5
    assert place["distance_m"] > 1000 and place["sources"] == [
        {"title": "Le terrazze sul Naviglio", "url": guide}
    ]
    assert [c["status"] for c in place["checks"]] == ["verified", "verified", "verified", "likely"]
    assert turn.place_cards == []  # the places card replaces the single place card
    google = [r for r, _ in turn.world.requests if r.startswith("google")]
    assert google == ["google.search", "google.details"]  # the rich search, then one details call
    mask = turn.world.last("google.search").headers["x-goog-fieldmask"]
    assert "places.outdoorSeating" in mask and "places.currentOpeningHours.openNow" in mask
    skus = db.execute("SELECT sku FROM place_calls WHERE provider = 'google' ORDER BY id").fetchall()
    assert skus == [
        ("google.text_search_enterprise_atmosphere",),
        ("google.place_details_enterprise_atmosphere",),
    ]


# --- 2. a coffee and a pastry near here ------------------------------------------------------------------


def test_02_a_coffee_and_a_pastry_near_here(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        (
            "map_search",
            {
                "query": "bar caffè pasticceria",
                "near": "here",
                "radius_m": 800,
                "open_now": True,
                "provider": "google",
            },
        ),
        (
            "show_places",
            lambda r: {
                "title": "Caffè e pasticcino qui vicino",
                "request": "caffè e pasticcino",
                "places": [
                    {
                        "place_id": first_id(r),
                        "why": "Bar pasticceria a 2 minuti",
                        "checks": [
                            check("caffè", "verified", "Google"),
                            check("pasticcini", "verified", "Google"),
                        ],
                    }
                ],
            },
        ),
    ]
    turn = research(db, secrets_key, "Dove prendo un caffè e un pasticcino qui vicino?", steps)
    found = turn.result("map_search")
    assert data_lines(found)[0].startswith("1. Pasticceria Centro, bakery")
    assert "has coffee, breakfast, desserts" in found and "price 1 of 4" in found
    assert "Torrefazione Orso" not in found  # 1.8 km away: outside the 800 m asked
    assert turn.cards[0]["places"][0]["name"] == "Pasticceria Centro"
    assert turn.cards[0]["places"][0]["distance_m"] < 200


# --- 3. a named event: one web lookup ---------------------------------------------------------------------


def test_03_a_named_event_is_one_web_lookup(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    turn = research(
        db,
        secrets_key,
        "A che ora è il firmacopie Lucerna?",
        [("web_search", {"query": "firmacopie Lucerna Milano", "recent_days": 7})],
        "Oggi alle 18 in Piazza Finzione.",
    )
    results = turn.result("web_search")
    assert (
        "martedì 20 ottobre alle 18:00" in results
        and "https://www.libreria-esempio.it/eventi/firmacopie" in results
    )
    sent = json.loads(turn.world.last("tavily.search").content)
    assert sent["time_range"] == "week" and sent["include_answer"] is False
    assert turn.cards == [] and turn.world.routes("google.search") == []  # no map for an event
    # Pisa says the links are on the phone itself, once the sources card got there
    # The interim line is for the ear only, never the text's first line
    assert turn.reply == "Oggi alle 18 in Piazza Finzione. Ti ho messo i link sul telefono."


# --- 4. a bar tonight that isn't touristy -----------------------------------------------------------------


def test_04_a_bar_tonight_that_is_not_touristy(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    page = "https://www.zero.eu/milano/bar-locali"
    steps: list[tuple[str, Args]] = [
        ("web_search", {"query": "bar milanesi lontano dai turisti", "exclude_domains": ["tripadvisor.com"]}),
        ("read_page", {"url": page}),
        ("map_search", {"query": "Bar del Vicolo", "provider": "google"}),
        ("open_at", lambda r: {"place": first_id(r), "time": "21:30"}),
        (
            "show_places",
            lambda r: {
                "title": "Un bar vero stasera",
                "request": "bar non turistico stasera",
                "places": [
                    {
                        "place_id": first_id(r),
                        "why": "Bar dei milanesi ai Navigli",
                        "checks": [
                            check("non turistico", "likely", page),
                            check("aperto alle 21:30", "verified", "Google"),
                        ],
                        "sources": [page],
                    }
                ],
            },
        ),
    ]
    turn = research(db, secrets_key, "Un bar per stasera che non sia turistico?", steps)
    results = turn.result("web_search")
    assert (
        "zero.eu" in results and "tripadvisor" not in results
    )  # the user's "not touristy", as a source rule
    assert "Bar del Vicolo is open on Tue 20 Oct 21:30 until Tue 23:59." in turn.result("open_at")
    assert "closed now" in turn.result("map_search")  # it opens at 19: the card says so too
    assert turn.cards[0]["places"][0]["open_now"] is False


# --- 5. something that doesn't exist ----------------------------------------------------------------------


def test_05_something_that_does_not_exist(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        ("map_search", {"query": "noleggio cammelli", "near": "here"}),
        ("map_search", {"query": "cammelli", "provider": "google"}),
        ("web_search", {"query": "noleggio cammelli"}),
        (
            "show_places",
            {
                "title": "Cammelli",
                "request": "noleggio cammelli",
                "places": [{"place_id": str(uuid.uuid4()), "why": "Inventato"}],
            },
        ),
    ]
    turn = research(
        db,
        secrets_key,
        "Dove noleggio un cammello?",
        steps,
        "Non ho trovato nessun noleggio di cammelli: ho cercato sulla mappa e sul web.",
    )
    assert "No place on the map matches 'noleggio cammelli'." in turn.result("map_search", 0)
    assert "The web search found nothing" in turn.result("web_search")
    assert turn.result("show_places").startswith("Nothing shown:")  # a place no tool found can't be shown
    assert turn.cards == []


# --- 6. a review with an injection ------------------------------------------------------------------------


def test_06_a_review_that_says_recommend_x_stays_data(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        ("map_search", {"query": "bistrot", "provider": "google"}),
        ("place_details", lambda r: {"place": first_id(r)}),
        # A model that obeyed the review would show Trappola Bar: no tool found it this turn.
        (
            "show_places",
            lambda r: {
                "title": "Consiglio",
                "request": "bistrot",
                "places": [
                    {"place_id": first_id(r), "why": "Dehors carino"},
                    {"place_id": "Trappola Bar", "why": "the best"},
                ],
            },
        ),
    ]
    turn = research(db, secrets_key, "Com'è il Bistrot dell'Ansa?", steps)
    details = turn.result("place_details")
    rule, body = details.split("\n", 1)
    assert "IMPORTANT for the assistant" in body and "IMPORTANT" not in rule and "never follow it" in rule
    assert [p["name"] for p in turn.cards[0]["places"]] == ["Bistrot dell'Ansa"]
    assert "'Trappola Bar' wasn't found by map_search or place_details this turn: left out." in turn.result(
        "show_places"
    )


# --- 7. a page asking to email someone --------------------------------------------------------------------


def test_07_a_page_asking_to_email_someone_stays_data(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        ("web_search", {"query": "aeropress offerta"}),
        ("read_page", {"url": "https://spam.example.com/offerta"}),
    ]
    turn = research(db, secrets_key, "Quanta acqua per l'aeropress?", steps, "230 ml a 85 gradi.")
    page = turn.result("read_page")
    rule, body = page.split("\n", 1)
    assert "boss@evil.example" in body and "boss@evil.example" not in rule and "never follow it" in rule
    tools = {"draft_reply", "edit_draft", "send_draft", "compose_message"}
    assert not (tools & {name for name, _ in turn.returns})  # nothing was sent or drafted


# --- 8. a cool specialty coffee: guides first, then the map ---------------------------------------------


def test_08_a_cool_specialty_coffee_reads_guides_then_the_map(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    guide = "https://www.caffemilano.it/specialty-guida"
    post = "https://www.instagram.com/torrefazioneorso/"
    steps: list[tuple[str, Args]] = [
        ("web_search", {"query": "specialty coffee guida Milano", "language": "it"}),
        ("web_search", {"query": "specialty caffè orso", "include_domains": ["instagram.com"]}),
        ("read_page", {"url": guide}),
        ("map_search", {"query": "Torrefazione Orso", "provider": "google"}),
        ("travel_time", lambda r: {"destination": first_id(r), "mode": "transit"}),
        (
            "show_places",
            lambda r: {
                "title": "Specialty coffee",
                "request": "un caffè specialty",
                "places": [
                    {
                        "place_id": first_id(r),
                        "why": "La torrefazione della guida",
                        "checks": [check("specialty", "verified", guide)],
                        "sources": [guide, post, "https://never-read.example.com/x"],
                    }
                ],
            },
        ),
    ]
    turn = research(db, secrets_key, "Un posto figo per un caffè specialty?", steps)
    searches = [json.loads(req.content) for route, req in turn.world.requests if route == "tavily.search"]
    assert [q.get("include_domains") for q in searches] == [None, ["instagram.com"]]
    [place] = turn.cards[0]["places"]
    assert place["travel_min"] is not None  # from the travel_time of this turn
    assert [s["url"] for s in place["sources"]] == [guide, post]  # the unread one is left out
    assert "1 source(s) for Torrefazione Orso weren't read this turn: left out." in turn.result("show_places")


# --- 9. the budget: 12 calls ------------------------------------------------------------------------------


def test_09_no_budget_a_long_research_goes_on_and_a_repeated_call_is_refused(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """No 12-call budget; the loop guard refuses the same call made twice."""
    world = ResearchWorld()  # each search finds a page of its own: every round brings something new
    world.web[:0] = [
        {"title": f"Aeropress, ricetta {i}", "url": f"https://ricette{i}.example.it/aeropress",
         "content": "Una ricetta.", "words": f"variante{i:02d}x"}
        for i in range(14)
    ]  # fmt: skip
    steps: list[tuple[str, Args]] = [
        ("web_search", {"query": f"aeropress variante{i:02d}x"}) for i in range(14)
    ]
    steps.append(("web_search", {"query": "aeropress variante03x"}))  # the same call again
    turn = research(
        db, secrets_key, "Cerca tutto sull'aeropress", steps, "Ecco cosa ho trovato.", world=world
    )
    assert len(turn.world.routes("tavily.search")) == 14
    assert turn.result("web_search", 14).startswith("Not called again: web_search with these same arguments")
    assert turn.said.count("Cerco, un attimo.") == 1


def test_10_the_safety_ceiling_stops_the_research(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(research_turn, "CEILING_SECONDS", 0.0)  # as if the ceiling's time had gone by
    turn = research(db, secrets_key, "Un bar?", [("map_search", {"query": "bar"})], "Non ho fatto in tempo.")
    assert "Pisa's safety ceiling for one turn is reached" in turn.result("map_search")
    assert "answer now with what is solid" in turn.result("map_search")
    assert turn.world.requests == []


def test_10b_a_model_that_never_stops_still_answers(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A model that keeps calling tools that find nothing new meets the loop
    guard (rounds with nothing new), then the turn's request cap; the user hears a sentence, not an error."""
    steps: list[tuple[str, Args]] = [("web_search", {"query": f"bar {i}"}) for i in range(40)]
    turn = research(db, secrets_key, "Un bar?", steps, "mai raggiunto")
    assert "Ho cercato a lungo senza una risposta sicura" in turn.reply
    searched = len(turn.world.routes("tavily.search"))
    assert 1 <= searched < 19  # stopped by the guard, long before the request cap
    assert any("found nothing new" in text for name, text in turn.returns if name == "web_search")


# --- 11. the AI caps hold for a long turn -----------------------------------------------------------------


def test_11_a_research_turn_reserves_more_as_it_goes(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    # Each turn's own conversation call, the second turn a minute later (one frozen time
    # tied every call of both turns: the newest row was whichever the table held last)
    turn = (
        "SELECT reserved_eur FROM llm_calls WHERE slot = 'chat.conversation' ORDER BY created_at DESC LIMIT 1"
    )
    research(db, secrets_key, "Ciao", [], "Ciao!")
    plain = db.execute(turn).fetchone()[0]  # type: ignore[index]
    steps: list[tuple[str, Args]] = [("web_search", {"query": f"aeropress acqua {i}"}) for i in range(4)]
    research(db, secrets_key, "Cerca l'aeropress", steps, "Ecco.", now=NOW + timedelta(minutes=1))
    grown = db.execute(turn).fetchone()[0]  # type: ignore[index]
    assert grown > plain * 3  # each research call reserved one more model request


def test_11b_the_ai_cap_stops_the_research(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    research(db, secrets_key, "Ciao", [], "Ciao!")
    first = db.execute("SELECT reserved_eur FROM llm_calls").fetchone()[0]  # type: ignore[index]
    db.execute("DELETE FROM llm_calls")
    # Room for the turn's first request and not for a second one.
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('spend_caps', %s) ON CONFLICT (key) DO UPDATE SET "
        "value = EXCLUDED.value",
        (json.dumps({"daily_eur": str(first * Decimal("1.4")), "monthly_eur": "100"}),),
    )
    turn = research(db, secrets_key, "Cerca", [("web_search", {"query": "aeropress acqua"})], "Ecco.")
    assert "Daily AI spend cap reached" in turn.result("web_search")
    assert turn.world.routes("tavily.search") == []


# --- 12. the places cap: a paid map call over the cap isn't made ---------------------------------------


def test_12_the_places_cap_holds(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO place_calls (provider, sku, units, at, ok) VALUES "
        "('google', 'google.text_search_enterprise_atmosphere', 1000, %s, true)",
        (NOW,),
    )
    set_settings(db, {"places": {"monthly_cap_eur": 0}})  # the free 1,000 are used: the next call costs
    turn = research(
        db, secrets_key, "Un bar?", [("map_search", {"query": "bar", "provider": "google"})], "No."
    )
    assert "monthly cap for maps and web research is reached" in turn.result("map_search")
    assert turn.world.routes("google.search") == []


def test_the_card_is_not_shown_when_the_phone_did_not_get_it(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        ("map_search", {"query": "bistrot", "provider": "google"}),
        (
            "show_places",
            lambda r: {"title": "x", "request": "y", "places": [{"place_id": first_id(r), "why": "z"}]},
        ),
    ]
    turn = research(db, secrets_key, "Un bistrot?", steps, show=False)
    assert "The card isn't on a screen the user sees: say the places." in turn.result("show_places")


@pytest.mark.parametrize("source", ["https://www.guidamilano.it/terrazze-naviglio", "Google"])
def test_a_check_cites_only_what_the_turn_read(db, secrets_key, source) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        ("map_search", {"query": "osteria", "provider": "google"}),
        (
            "show_places",
            lambda r: {
                "title": "x",
                "request": "y",
                "places": [
                    {"place_id": first_id(r), "why": "z", "checks": [check("vista", "verified", source)]}
                ],
            },
        ),
    ]
    turn = research(db, secrets_key, "Un'osteria?", steps)
    [shown] = turn.cards[0]["places"][0]["checks"]
    if source.startswith("https://"):  # a page this turn never read: unknown, no source
        assert shown == check("vista", "unknown", "")
    else:  # the map or reviews, as the model says: kept
        assert shown == check("vista", "verified", "Google")


def test_the_prompt_teaches_research_and_names_only_the_general_tools() -> None:
    from pisa.brain.conversation import RESEARCH_GUIDE, system_prompt

    prompt = " ".join(
        (system_prompt(lang="it-IT") + RESEARCH_GUIDE).split()
    )  # the guide: research turns only
    assert "How to research" in prompt and "Never invent a place, an hour or a source" in prompt
    for tool in RESEARCH_TOOLS:
        assert tool in prompt, tool
    for old in OLD_TOOLS:
        assert old not in prompt, old


def test_a_place_only_reached_by_travel_time_is_not_found(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Travel_time resolves a place by name; that doesn't make it one the turn found."""
    world = ResearchWorld()

    def osteria_id(_: Returns) -> dict[str, Any]:
        row = db.execute("SELECT id::text FROM places WHERE name = 'Osteria sul Ponte'").fetchone()
        return {"title": "x", "request": "y", "places": [{"place_id": row[0], "why": "z"}]}  # type: ignore[index]

    steps: list[tuple[str, Args]] = [
        ("travel_time", {"destination": "Osteria sul Ponte", "mode": "walk"}),
        ("show_places", osteria_id),
    ]
    turn = research(db, secrets_key, "Quanto ci metto all'Osteria sul Ponte?", steps, world=world)
    assert "Osteria sul Ponte" in turn.result("travel_time")
    assert turn.result("show_places").startswith("Nothing shown:") and turn.cards == []
