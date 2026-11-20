"""A trip question: Pisa told the user about the boat to Veenmond but said it
couldn't find a link to buy the tickets. Now every web answer comes with its links on the phone (a `sources`
card: only pages this turn's searches returned, those it read first), the prompt teaches "where do I buy it"
(the operator's official page, read to confirm; or the page that says there's nothing to buy online), and Pisa
never states a web fact without its link."""

from tests.research_world import ResearchWorld
from tests.test_research_eval import research

FERRY = "https://www.veerdienst.example/en/ferry/veenmond-tickets"
BLOG = "https://amsterdam.example.com/ferry-veenmond"


def boat_world() -> ResearchWorld:
    world = ResearchWorld()
    world.web += [
        {"title": "Ferry to Veenmond: tickets", "url": FERRY,
         "content": "The ferry to Veenmond: a ticket in the Veerdienst app, or pay on board by bank card.",
         "words": "ferry traghetto veenmond tickets biglietti boot"},
        {"title": "How to get to the HOLLOW BELL festival", "url": BLOG,
         "content": "Take the ferry from Centraal, then a 10-minute walk.",
         "words": "ferry traghetto veenmond festival boot"},
    ]  # fmt: skip
    world.pages[FERRY] = (
        "Ferry to Veenmond. Tickets: Veerdienst app, or pay on board with a contactless bank card."
    )
    return world


def test_the_boat_tickets_come_with_their_links(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    turn = research(
        db,
        secrets_key,
        "Dove compro i biglietti per il traghetto per Veenmond?",
        [("web_search", {"query": "traghetto Veenmond biglietti"}), ("read_page", {"url": FERRY})],
        "Si paga a bordo con la carta o nell'app Veerdienst: ti ho messo i link sul telefono.",
        world=boat_world(),
    )
    (card,) = turn.sources
    assert card["kind"] == "sources" and card["title"] == "Fonti"
    # The page it read first (the operator's own), then the other result it saw; each with its host.
    assert [(s["url"], s["host"]) for s in card["sources"]] == [
        (FERRY, "veerdienst.example"),
        (BLOG, "amsterdam.example.com"),
    ]
    assert card["sources"][0]["title"] == "Ferry to Veenmond: tickets"
    assert turn.cards == []  # no places card: the links go on their own card


def test_no_web_no_sources_card(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    turn = research(db, secrets_key, "Che ore sono?", [], "Sono le 13.")
    assert turn.sources == []


def test_the_prompt_teaches_where_to_buy_and_links_for_every_web_fact() -> None:
    from pisa.brain.conversation import RESEARCH_GUIDE, system_prompt

    prompt = " ".join(
        (system_prompt(lang="it-IT") + RESEARCH_GUIDE).split()
    )  # the guide: research turns only
    assert '"Where do I buy it"' in prompt and "official site" in prompt
    assert "Never state a web fact you can't link" in prompt
    assert "says so itself once the phone has them" in prompt  # Never the model's own claim
