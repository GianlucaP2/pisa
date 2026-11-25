"""Collegamenti in App Store review mode (PISA_REVIEW_MODE=1, every experimental feature off whatever
PISA_EXPERIMENTAL says): the maps, web and Google account categories are empty (those features are off), the
push relay and the server's voices are absent, the AI keys are the operator's (no key action from the app),
and no paid OK can be given."""

from typing import Any

import psycopg

from tests.conftest import pair, set_key
from tests.test_connections import entry, ids, view
from tests.test_privacy_v2 import tls  # noqa: F401 (a fixture)
from tests.test_review_mode import app_settings, experimental_spec, review_flag  # noqa: F401 (fixtures)


def test_review_mode_shows_only_what_the_demo_server_does(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    set_key(db, secrets_key, "openai")
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    body = view(client, auth)
    assert [c["id"] for c in body["categories"]] == [
        "ai",
        "voice",
        "maps",
        "web",
        "accounts",
        "notifications",
    ]
    assert ids(body, "maps") == ids(body, "web") == ids(body, "accounts") == []
    assert ids(body, "voice") == ["voice.device", "voice.gemini"]  # no server voice, no xAI talker
    assert ids(body, "notifications") == ["notifications.phone", "notifications.direct"]  # no relay
    assert entry(body, "notifications.phone")["state"] == "off"  # the phone's notifications: never in review
    openai = entry(body, "ai.openai")
    assert (openai["state"], openai["actions"]) == (
        "on",
        ["test"],
    )  # the operator's key: no change from the app
    assert entry(body, "ai.anthropic")["actions"] == []
    refused = client.put("/v1/connections/paid/google", headers=auth, json={"cap_cents": 100})
    assert refused.status_code == 404  # the places feature is off: its OK doesn't exist
