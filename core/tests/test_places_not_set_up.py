"""§27 w4: with no place provider set up, the model is told so, and asked for a place it says in one line that
place search isn't set up yet, instead of asking where they are. With every job's provider set up, the prompt
is unchanged."""

from typing import Any

import pytest

from pisa.brain.conversation import UNSET_LINE, features_line
from pisa.domain import experimental
from tests.conftest import pair, pair_v2, set_key
from tests.support.fake_secrets import google_api_key
from tests.test_live_v2_socket import connect, hello, opened, run

UNSET = ("place search", "travel times", "web research")


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


def test_the_line_names_what_isnt_set_up_and_is_absent_when_all_is() -> None:
    assert features_line(experimental.ALL, UNSET) == UNSET_LINE.format(unset="; ".join(UNSET))
    assert "never ask where they are" in features_line(experimental.ALL, UNSET)
    assert features_line(experimental.ALL, ()) == ""  # every provider set up: the prompt byte-identical


def _told(client: Any, db: Any, secrets_key: str, device: tuple[str, str], fake_models: Any) -> str:
    device_id, token = device
    set_key(db, secrets_key)
    told: list[str] = []

    async def model(messages: Any, info: Any) -> Any:
        told.append(str(getattr(messages[-1], "instructions", None) or ""))
        yield "Va bene."

    fake_models.default = model
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run("Trovami un ristorante qui vicino")
        app.send(turn)
        app.run_ends(turn["input"]["runId"])
    assert told, "no model request"
    return told[0]


def test_a_turn_is_told_until_a_provider_has_a_key(  # type: ignore[no-untyped-def]
    client, db, secrets_key, device, fake_models
) -> None:
    told = _told(client, db, secrets_key, device, fake_models)
    assert UNSET_LINE.format(unset="; ".join(UNSET)) in told
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    key = google_api_key("-test-1234")  # fake
    assert client.put("/v1/places/providers/google/key", headers=auth, json={"key": key}).status_code == 204
    told = _told(client, db, secrets_key, device, fake_models)
    assert UNSET_LINE.format(unset="web research") in told  # Google does places and routes, not the web
    assert "place search" not in told.split("Not set up yet on this server:")[1].split("\n")[0]


def test_a_search_near_here_with_no_provider_is_not_set_up_before_any_location(  # type: ignore[no-untyped-def]
    client, db
) -> None:
    """§29 w4: no provider can search and no device sent a location: the search answers "not set up" (what the
    model says plainly), never location_unknown (which made it say "non so dove sei" and offer a search)."""
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    got = client.get("/v1/places/search", headers=auth, params={"q": "ristorante", "near": "here"})
    assert got.status_code == 409, got.text
    error = got.json()["error"]
    assert error["code"] == "place_provider_not_configured", error
    assert error["message"] == "No provider for place search is ready: none has a key on this server."
