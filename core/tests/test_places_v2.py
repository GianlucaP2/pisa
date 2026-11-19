"""Places like a map app: the card v2 (the pin, photos, price, until when, the menu, what fits the
owner), the detail sheet (Google's review summary else Pisa's, review excerpts, the menu as Pisa read it,
photos' attributions) and short-lived photo URLs through Pisa's server, in the places cap."""

import json
import uuid
from typing import Any

import psycopg
import pytest

from tests.research_world import ResearchWorld, ids
from tests.test_places import auth  # noqa: F401 (the fixture: every provider configured)
from tests.test_research_eval import Args, Returns, research

MENU = "https://www.osteriasulponte.example.it/menu"


def osteria(r: Returns) -> str:
    return ids([text for name, text in r if name == "map_search"][-1])[0]


def taste(db: psycopg.Connection, secrets_key: str) -> uuid.UUID:
    """The user told Pisa: quiet places, never chains."""
    turn = research(
        db,
        secrets_key,
        "Mi piacciono i posti tranquilli e niente catene",
        [
            (
                "remember_about_me",
                {"kind": "like", "statement": "Ama i posti tranquilli", "quote": "posti tranquilli"},
            ),
            (
                "remember_about_me",
                {"kind": "constraint", "statement": "Niente catene", "quote": "niente catene", "hard": True},
            ),
        ],
    )
    assert turn.device is not None
    return turn.device


def test_a_card_v2_cites_the_owners_taste_and_a_menu_it_read(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device = taste(db, secrets_key)
    steps: list[tuple[str, Args]] = [
        ("map_search", {"query": "osteria naviglio", "provider": "google"}),
        ("place_details", lambda r: {"place": osteria(r)}),
        ("web_search", {"query": "menu osteria ponte"}),
        ("read_page", {"url": MENU}),
        (
            "show_places",
            lambda r: {
                "title": "Cena tranquilla",
                "request": "una cena tranquilla",
                "places": [
                    {
                        "place_id": osteria(r),
                        "why": "Terrazza tranquilla sul Naviglio",
                        "fits": ["tranquillo", "non una catena", "vista mare"],
                        "menu_url": MENU,
                        "menu_highlights": ["Risotto alla milanese", "Cotoletta"],
                    }
                ],
            },
        ),
    ]
    turn = research(db, secrets_key, "Una cena tranquilla stasera?", steps, device=device)
    assert (
        "Ama i posti tranquilli" in turn.instructions
        and "HARD LIMIT] constraint: Niente catene" in turn.instructions
    )
    [place] = turn.cards[-1]["places"]
    # The fits name the user's claims; "vista mare" is in no claim: left out (never invented).
    assert place["fits"] == ["tranquillo", "non una catena"]
    assert "`fits` name only what is in the user's profile" in turn.result("show_places")
    assert (place["lat"], place["lon"], place["photo_count"], place["price_level"]) == (45.451, 9.08, 3, 2)
    assert place["closes_at"].endswith("+02:00") and place["opens_at"] is None  # open now: until 23:00
    assert place["menu_url"] == MENU
    assert db.execute("SELECT url, highlights FROM place_menus").fetchone() == (
        MENU,
        ["Risotto alla milanese", "Cotoletta"],
    )


def test_a_menu_not_read_this_turn_is_not_shown(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        ("map_search", {"query": "osteria naviglio", "provider": "google"}),
        (
            "show_places",
            lambda r: {
                "title": "x",
                "request": "y",
                "places": [
                    {"place_id": osteria(r), "why": "z", "menu_url": MENU, "menu_highlights": ["Inventato"]}
                ],
            },
        ),
    ]
    turn = research(db, secrets_key, "Un'osteria?", steps)
    assert turn.cards[-1]["places"][0]["menu_url"] is None
    assert "wasn't read this turn (read_page): no menu shown" in turn.result("show_places")
    assert db.execute("SELECT count(*) FROM place_menus").fetchone() == (0,)


def test_fits_without_a_profile_are_left_out(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    steps: list[tuple[str, Args]] = [
        ("map_search", {"query": "osteria naviglio", "provider": "google"}),
        (
            "show_places",
            lambda r: {
                "title": "x",
                "request": "y",
                "places": [{"place_id": osteria(r), "why": "z", "fits": ["tranquillo"]}],
            },
        ),
    ]
    turn = research(db, secrets_key, "Un'osteria?", steps)
    assert turn.cards[-1]["places"][0]["fits"] == []


@pytest.fixture
def world(http_handler: dict[str, object]) -> ResearchWorld:
    w = ResearchWorld()
    http_handler["handler"] = w
    return w


def found(client: Any, auth: dict[str, str], q: str) -> str:  # noqa: F811
    return client.get("/v1/places/search", headers=auth, params={"q": q}).json()["results"][0]["id"]


def test_the_detail_sheet(client, db, auth, world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    place = found(client, auth, "osteria naviglio")
    sheet = client.get(f"/v1/places/{place}/details", headers=auth).json()
    assert sheet["review_summary"] == {
        "text": "La terrazza sul canale al tramonto piace a tutti.",
        "source": "google",
        "sources": [],
        "disclosure": "Riepilogo creato con Gemini",  # Google's AI summary: its disclosure and links with it
        "flag_uri": "https://www.google.com/local/review/rap/report?postId=summary-ponte",
        "uri": "https://www.google.com/maps/place//data=!4m4!3m3!1s0x0:0x1!9m1!1b1",
    }
    # Google's generativeSummary describes the place: never shown as what the reviews say.
    assert sheet["description"] == {
        "text": "Osteria milanese con terrazza sul Naviglio Grande.",
        "disclosure": "Riepilogo creato con Gemini",
        "flag_uri": "https://www.google.com/local/review/rap/report?postId=overview-ponte",
    }
    assert sheet["reviews"][0] == {
        "author": "Un cliente",
        "author_uri": "https://www.google.com/maps/contrib/100000000000000000001",
        "rating": 5.0,
        "relative_time": "3 settimane fa",
        "text": "Terrazza proprio sul Naviglio, al tramonto la vista sull'acqua è bellissima.",
    }
    assert [p["attribution"]["name"] for p in sheet["photos"]] == ["Autore 0", "Autore 1", "Autore 2"]
    assert sheet["menu"] is None and sheet["website"] == "https://www.osteriasulponte.example.it"
    assert sheet["hours"]["open_now"] is True and sheet["apple_place_id"] is None
    mask = world.last("google.details").headers["x-goog-fieldmask"]
    assert "photos" in mask and "reviewSummary" in mask and "generativeSummary" in mask


def test_without_googles_summary_the_sheet_has_js_with_its_sources(client, db, auth, world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    place = found(client, auth, "bistrot")
    db.execute(
        "INSERT INTO place_insights (id, place_id, summary, facts, sources, created_at, expires_at) "
        "VALUES (gen_random_uuid(), %s, 'Dehors sulla Ripa; il canale si vede poco.', '[]', %s, now(), "
        "now() + interval '30 days')",
        (
            place,
            json.dumps(
                [{"title": "Le terrazze sul Naviglio", "url": "https://www.guidamilano.it/terrazze-naviglio"}]
            ),
        ),
    )
    summary = client.get(f"/v1/places/{place}/details", headers=auth).json()["review_summary"]
    assert (
        summary["source"] == "pisa"
        and summary["sources"][0]["url"] == "https://www.guidamilano.it/terrazze-naviglio"
    )


def test_a_photo_url_through_the_server(client, db, auth, world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    place = found(client, auth, "osteria naviglio")
    photo = client.get(f"/v1/places/{place}/photos/1", headers=auth, params={"w": 800}).json()
    assert photo["url"] == "https://lh3.googleusercontent.com/places/ChIJpontenaviglio01/photos/ph1abcd=w800"
    assert photo["attribution"] == {"name": "Autore 1", "uri": "https://maps.google.com/maps/contrib/1"}
    assert "AIza" not in json.dumps(photo)  # the key never leaves the server
    again = client.get(f"/v1/places/{place}/photos/1", headers=auth, params={"w": 800}).json()
    assert again["url"] == photo["url"] and world.routes("google.media") == ["google.media"]  # paid once
    skus = [r[0] for r in db.execute("SELECT sku FROM place_calls WHERE NOT reserved").fetchall()]
    assert skus.count("google.place_photo") == 1
    missing = client.get(f"/v1/places/{place}/photos/7", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "The place has 3 photos: there is no photo 7."
