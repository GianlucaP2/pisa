"""Collegamenti (Connections), v0.1.4: `GET /v1/connections` in six categories, the DELETE of every key kind,
an AI key that still serves slots (409, or `replace_with`), the signed paid OK (`paid_ack`), and the choke
point: a paid maps or web provider is never called without it, an install from before v0.1.4 included.

The suite's user has given the paid OK to every paid provider (`tests/conftest.ack_paid_providers`); the tests
about the OK delete it first."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from pisa.brain.research_turn import NEEDS_ACK, failed_block
from pisa.domain import connections as rules
from pisa.domain.errors import PisaError
from pisa.domain.place_prices import PRICES
from pisa.infra.keys import KeyStore
from pisa.services.place_providers import set_maps_key
from tests.assistant_env import set_settings
from tests.conftest import FAKE_KEY, TEST_MODELS, dsn, pair, pair_v2_key, seed_catalog, set_key
from tests.phone_signing import challenge, device_headers, register, sign
from tests.places_env import PlaceEnv, place_calls, run_places
from tests.places_fake import FakePlaces
from tests.support.fake_secrets import google_api_key, openrouteservice_key
from tests.test_place_providers import search
from tests.test_push import p8

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
GOOGLE_KEY = google_api_key("-test-conn-7788")  # fake, built at run time
ENTRY_KEYS = {
    "id", "kind", "category", "name", "provider", "state", "paid", "paid_ack", "needs_paid_ack",
    "month_spent_cents", "data_out", "docs", "actions", "key_last4", "slots",
}  # fmt: skip
NEEDS_ACK_SEARCH = (
    "No provider for place search is ready: Google Maps costs money and waits for your OK of its monthly cap "
    "in Settings → Connections."
)
NEEDS_ACK_GOOGLE = (
    "Google Maps costs money and waits for your OK of its monthly cap in Settings → Connections: Pisa "
    "doesn't call it until then."
)


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


@pytest.fixture
def fake(http_handler: dict[str, object]) -> FakePlaces:
    fake = FakePlaces()
    http_handler["handler"] = fake
    return fake


def unack(db: psycopg.Connection, provider: str = "google") -> None:
    db.execute("DELETE FROM paid_acks WHERE provider = %s", (provider,))


def view(client: Any, auth: dict[str, str]) -> dict[str, Any]:
    got = client.get("/v1/connections", headers=auth)
    assert got.status_code == 200, got.text
    return got.json()


def entry(body: dict[str, Any], entry_id: str) -> dict[str, Any]:
    return next(e for c in body["categories"] for e in c["entries"] if e["id"] == entry_id)


def ids(body: dict[str, Any], category: str) -> list[str]:
    return [e["id"] for e in next(c for c in body["categories"] if c["id"] == category)["entries"]]


# --- GET /v1/connections ------------------------------------------------------------------------------------


def test_six_categories_every_entry_shaped_alike_and_no_key_material(client, auth, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_key(db, secrets_key, "openai")
    body = view(client, auth)
    assert [c["id"] for c in body["categories"]] == [
        "ai",
        "voice",
        "maps",
        "web",
        "accounts",
        "notifications",
    ]
    assert [c["name"] for c in body["categories"]] == [
        "AI models", "Voice", "Maps & places", "Web research", "Accounts", "Notifications",
    ]  # fmt: skip
    assert body["hard_limit_monthly_cents"] == 3000  # the default hard limit, €30 a month
    for category in body["categories"]:
        for e in category["entries"]:
            assert set(e) == ENTRY_KEYS, e["id"]
            assert e["category"] == category["id"] and e["id"].startswith(f"{category['id']}.")
            assert e["state"] in ("on", "off", "not_set_up", "error", "needs_reconnect")
            assert set(e["actions"]) <= set(rules.ACTIONS)
            assert e["data_out"] and e["docs"].endswith(".md")
    assert "ai.openai" in ids(body, "ai") and "ai.elevenlabs" not in ids(body, "ai")  # speech only: a voice
    assert ids(body, "voice") == [
        "voice.device",
        "voice.openai",
        "voice.elevenlabs",
        "voice.gemini",
        "voice.xai",
    ]
    assert ids(body, "maps") == [
        "maps.phone",
        "maps.apple",
        "maps.google",
        "maps.osm",
        "maps.openrouteservice",
    ]
    assert ids(body, "web") == ["web.tavily"]
    assert ids(body, "accounts") == ["accounts.google"]
    assert ids(body, "notifications") == [
        "notifications.phone",
        "notifications.relay",
        "notifications.direct",
    ]
    openai = entry(body, "ai.openai")
    on_openai = sorted(s for s, m in TEST_MODELS.items() if m and m.startswith("openai:"))
    assert (openai["state"], openai["paid"], openai["key_last4"]) == ("on", True, FAKE_KEY[-4:])
    assert sorted(openai["slots"]) == on_openai and openai["paid_ack"] is None  # the AI keeps its own caps
    assert openai["actions"] == ["set_key", "delete_key", "test"]
    assert openai["data_out"] == "Your words and the context Pisa sends with them, to OpenAI"
    # the free fallbacks say plainly what reaches a third party (security review E1, v0.1.4)
    assert entry(body, "maps.osm")["data_out"] == (
        "On by default: the words of each place search and a point rounded to about 100 m, to the public "
        "OpenStreetMap services Photon, Nominatim and Overpass, or to your server's own instances of them"
    )
    assert entry(body, "maps.openrouteservice")["data_out"] == (
        "The real start and end points of each trip (to about 1 m, which can be your home), to "
        "OpenRouteService"
    )
    anthropic = entry(body, "ai.anthropic")
    assert (anthropic["state"], anthropic["actions"], anthropic["slots"]) == ("not_set_up", ["set_key"], [])
    assert entry(body, "voice.openai")["state"] == "on"  # the same key
    assert entry(body, "voice.device") | {"actions": []} == entry(body, "voice.device")
    assert FAKE_KEY not in json.dumps(body)


def test_states_of_maps_web_account_and_notifications(client, auth, db, fake) -> None:  # type: ignore[no-untyped-def]
    body = view(client, auth)
    google = entry(body, "maps.google")
    assert (google["state"], google["paid"], google["actions"]) == (
        "not_set_up",
        True,
        ["set_key", "ack_paid"],
    )
    assert (
        google["paid_ack"]["cap_cents"] == 1_000_000 and google["needs_paid_ack"] is False
    )  # the suite's OK
    assert (entry(body, "maps.apple")["paid"], entry(body, "maps.osm")["state"]) == (False, "off")
    assert entry(body, "maps.openrouteservice")["paid"] is False
    assert entry(body, "maps.phone")["state"] == "on"
    assert entry(body, "web.tavily")["paid"] is True
    assert entry(body, "accounts.google")["state"] == "not_set_up"
    assert entry(body, "notifications.relay")["state"] == "not_set_up"  # no PISA_PUSH_RELAY_URL in the suite
    assert entry(body, "notifications.direct")["state"] == "not_set_up"  # no APNs key
    assert entry(body, "notifications.phone")["state"] == "not_set_up"  # no phone has acked a schedule

    assert (
        client.put("/v1/places/providers/google/key", headers=auth, json={"key": GOOGLE_KEY}).status_code
        == 204
    )
    google = entry(view(client, auth), "maps.google")
    assert (google["state"], google["key_last4"]) == ("on", "7788")
    assert google["actions"] == ["set_key", "delete_key", "test", "ack_paid"]
    unack(db)
    google = entry(view(client, auth), "maps.google")
    assert (google["state"], google["needs_paid_ack"], google["paid_ack"]) == ("not_set_up", True, None)
    assert google["actions"] == ["set_key", "delete_key", "ack_paid"]  # no test: a test is a call too

    db.execute(
        "INSERT INTO connector_accounts (connector, account, status, needs_reconnect) "
        "VALUES ('google', 'someone@example.invalid', 'error', true)"
    )
    account = entry(view(client, auth), "accounts.google")
    assert (account["state"], account["actions"]) == ("needs_reconnect", ["disconnect"])
    assert "example.invalid" not in json.dumps(view(client, auth))  # the account isn't in the view
    db.execute("UPDATE connector_accounts SET status = 'connected', needs_reconnect = false")
    assert entry(view(client, auth), "accounts.google")["state"] == "on"


def _settings_client(
    app_settings: Any, db: Any, fake_models: Any, clock: Any, http_handler: Any, **update: Any
) -> Any:
    import httpx
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    async def factory(settings: Any) -> Any:
        transport = httpx.MockTransport(lambda request: httpx.Response(599))
        return await Runtime.start(settings, model_factory=fake_models, http_transport=transport, clock=clock)

    return TestClient(create_app(app_settings.model_copy(update=update), runtime_factory=factory))


def test_an_experimental_feature_off_is_absent(app_settings, db, fake_models, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    with _settings_client(app_settings, db, fake_models, clock, http_handler, experimental="") as client:
        body = view(client, {"Authorization": f"Bearer {pair(db)[1]}"})
    assert "voice.xai" not in ids(body, "voice")  # the xAI talker is experimental
    assert ids(body, "voice")[:3] == ["voice.device", "voice.openai", "voice.elevenlabs"]  # supported: on
    assert ids(body, "maps") and ids(body, "accounts") == ["accounts.google"]


# --- DELETE of every key kind -------------------------------------------------------------------------------


def test_every_key_kind_with_a_put_has_a_delete(client, auth, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_key(db, secrets_key, "elevenlabs")  # a voice key: no slot uses it
    assert client.delete("/v1/ai/providers/elevenlabs/key", headers=auth).status_code == 204
    assert db.execute("SELECT count(*) FROM provider_keys WHERE provider = 'elevenlabs'").fetchone() == (0,)
    keys = {
        "google": GOOGLE_KEY,
        "tavily": "tvly-" + "x" * 24,
        "openrouteservice": openrouteservice_key("0" * 28 + "1234"),
    }
    for provider, key in keys.items():
        assert (
            client.put(f"/v1/places/providers/{provider}/key", headers=auth, json={"key": key}).status_code
            == 204
        )
        assert client.delete(f"/v1/places/providers/{provider}/key", headers=auth).status_code == 204
    assert db.execute("SELECT count(*) FROM place_providers").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM paid_acks").fetchone() == (2,)  # the OK is of the provider: kept

    async def apple() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await set_maps_key(conn, KeyStore(SecretStr(secrets_key)), "MAPSKEY123", p8()[0])

    asyncio.run(apple())
    assert client.delete("/v1/places/providers/apple/key", headers=auth).status_code == 204
    assert db.execute("SELECT count(*) FROM server_secrets WHERE name = 'maps'").fetchone() == (0,)
    osm = client.delete("/v1/places/providers/osm/key", headers=auth)
    assert osm.status_code == 400 and "no key to delete" in osm.json()["error"]["message"]

    pem, key_id = p8()[0], "ABCDE12345"
    assert client.put("/v1/push/key", headers=auth, json={"key_id": key_id, "key": pem}).status_code == 204
    assert client.delete("/v1/push/key", headers=auth).status_code == 204

    client_id = "1234-example.apps.googleusercontent.com"
    creds = {"web_client_id": client_id, "web_client_secret": "GOCSPX-" + "s" * 20}
    assert client.put("/v1/connectors/google/credentials", headers=auth, json=creds).status_code == 204
    db.execute(
        "INSERT INTO connector_accounts (connector, account, status) VALUES ('google', NULL, 'connected')"
    )
    in_use = client.delete("/v1/connectors/google/credentials", headers=auth)
    assert in_use.status_code == 409 and in_use.json()["error"]["code"] == "provider_in_use"
    db.execute("DELETE FROM connector_accounts")
    assert client.delete("/v1/connectors/google/credentials", headers=auth).status_code == 204
    assert db.execute("SELECT count(*) FROM connector_credentials").fetchone() == (0,)


def _catalog(db: psycopg.Connection) -> None:
    seed_catalog(db, [
        {"ref": "openai:gpt-6-luna", "capabilities": ["tools", "structured_output"],
         "input": Decimal("0.1"), "output": Decimal("0.5")},
        {"ref": "google:gemini-3.5-flash-lite", "capabilities": ["structured_output"],
         "input": Decimal("0.1"), "output": Decimal("0.4")},
        {"ref": "anthropic:claude-test-5", "capabilities": ["tools", "structured_output"],
         "input": Decimal("1"), "output": Decimal("5")},
        {"ref": "openrouter:some/plain-model", "capabilities": [],
         "input": Decimal("0.2"), "output": Decimal("0.4")},
    ])  # fmt: skip


def test_an_ai_key_serving_slots_is_refused_naming_them_or_replaced_at_once(
    client, auth, db, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    for provider in ("openai", "anthropic", "openrouter"):
        set_key(db, secrets_key, provider)
    _catalog(db)
    db.execute("UPDATE model_slots SET fallback_ref = 'openai:gpt-6-luna' WHERE id = 'conversation.reflex'")
    on_openai = [s for s, m in TEST_MODELS.items() if m and m.startswith("openai:")] + ["conversation.reflex"]
    refused = client.delete("/v1/ai/providers/openai/key", headers=auth)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "provider_in_use" and sorted(error["slots"]) == sorted(on_openai)
    assert error["provider"] == "openai" and "chat.conversation" in error["message"]
    assert db.execute("SELECT count(*) FROM provider_keys WHERE provider = 'openai'").fetchone() == (1,)

    before = db.execute("SELECT id, model_ref, fallback_ref FROM model_slots ORDER BY id").fetchall()
    unfit = client.delete("/v1/ai/providers/openai/key?replace_with=openrouter", headers=auth)
    assert unfit.status_code == 400 and unfit.json()["error"]["code"] == "model_incompatible"
    assert "chat.conversation" in unfit.json()["error"]["slots"]  # needs tools: the plain model can't
    assert db.execute("SELECT id, model_ref, fallback_ref FROM model_slots ORDER BY id").fetchall() == before
    assert db.execute("SELECT count(*) FROM provider_keys WHERE provider = 'openai'").fetchone() == (1,)
    for bad in ("openai", "deepseek", "nope"):  # itself, a provider without a key, an unknown one
        refusal = client.delete(f"/v1/ai/providers/openai/key?replace_with={bad}", headers=auth)
        assert refusal.status_code == 400 and refusal.json()["error"]["code"] == "validation_failed", bad

    moved = client.delete("/v1/ai/providers/openai/key?replace_with=anthropic", headers=auth)
    assert moved.status_code == 204, moved.text
    slots = dict(db.execute("SELECT id, model_ref FROM model_slots").fetchall())
    assert all(slots[s] == "anthropic:claude-test-5" for s in on_openai if s != "conversation.reflex")
    reflex = db.execute("SELECT model_ref, fallback_ref FROM model_slots WHERE id = 'conversation.reflex'")
    assert reflex.fetchone() == ("google:gemini-3.5-flash-lite", None)  # only its fallback was on OpenAI
    assert db.execute("SELECT count(*) FROM provider_keys WHERE provider = 'openai'").fetchone() == (0,)


# --- the signed paid OK -------------------------------------------------------------------------------------


def _ack(client: Any, headers: dict[str, str], cap: int, provider: str = "google") -> Any:
    return client.put(f"/v1/connections/paid/{provider}", headers=headers, json={"cap_cents": cap})


def _cap(db: psycopg.Connection, provider: str = "google") -> Any:
    row = db.execute("SELECT cap_cents FROM paid_acks WHERE provider = %s", (provider,)).fetchone()
    return row[0] if row else None


def test_the_paid_ok_is_signed_with_its_cap_and_bounded_by_the_hard_limit(client, auth, db) -> None:  # type: ignore[no-untyped-def]
    unack(db)
    assert _ack(client, auth, 500).status_code == 403  # always signed, a fresh install too
    private = register(client, auth)
    good = device_headers(client, auth, private, "paid_ack", "google:500")
    done = _ack(client, good, 500)
    assert done.status_code == 200 and done.json()["cap_cents"] == 500 and _cap(db) == 500
    assert _ack(client, good, 500).status_code == 403  # replayed: the nonce is spent
    other_cap = device_headers(client, auth, private, "paid_ack", "google:400")
    assert _ack(client, other_cap, 900).status_code == 403 and _cap(db) == 500  # signed another cap
    c = challenge(client, auth, action="device", op="paid_ack", target="google:700")
    forged = auth | {"X-Pisa-Nonce": c["nonce"], "X-Pisa-Signature": sign(register_other(), c["canonical"]),
                     "X-Pisa-Key-Id": "k1"}  # fmt: skip
    assert _ack(client, forged, 700).status_code == 403 and _cap(db) == 500  # a bad signature
    over = _ack(client, device_headers(client, auth, private, "paid_ack", "google:3001"), 3001)
    assert over.status_code == 400 and "hard monthly limit (3000 cents)" in over.json()["error"]["message"]
    assert _ack(client, auth, 0).status_code == 400 and _ack(client, auth, "500").status_code == 400  # type: ignore[arg-type]
    free = _ack(client, device_headers(client, auth, private, "paid_ack", "apple:100"), 100, "apple")
    assert free.status_code == 404  # Apple Maps never bills: nothing to acknowledge
    tavily = _ack(client, device_headers(client, auth, private, "paid_ack", "tavily:3000"), 3000, "tavily")
    assert tavily.status_code == 200 and _cap(db, "tavily") == 3000
    entry_ = entry(view(client, auth), "maps.google")
    assert entry_["paid_ack"]["cap_cents"] == 500 and entry_["month_spent_cents"] == 0
    assert client.delete("/v1/connections/paid/google", headers=auth).status_code == 204 and _cap(db) is None


def register_other() -> Any:
    from tests.phone_signing import new_key

    return new_key()[0]


def test_a_device_key_signs_its_own_devices_ok_only(client, db) -> None:  # type: ignore[no-untyped-def]
    unack(db)
    device_a, token_a, key_a = pair_v2_key(db, "Phone A")
    device_b, _, key_b = pair_v2_key(db, "Phone B")
    auth_a = {"Authorization": f"Bearer {token_a}"}

    def signed_by(device: str, key: Any, target: str) -> dict[str, str]:
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        c = challenge(client, auth_a, action="device", op="paid_ack", target=target)
        signature = base64.b64encode(key.sign(c["canonical"].encode(), ec.ECDSA(hashes.SHA256()))).decode()
        return auth_a | {
            "X-Pisa-Nonce": c["nonce"],
            "X-Pisa-Signature": signature,
            "X-Pisa-Key-Id": f"device-{device}",
        }

    assert (
        _ack(client, signed_by(device_b, key_b, "google:250"), 250).status_code == 403
    )  # another device's key
    assert _cap(db) is None
    assert _ack(client, signed_by(device_a, key_a, "google:250"), 250).status_code == 200
    assert _cap(db) == 250
    signer = db.execute("SELECT device_id::text FROM paid_acks WHERE provider = 'google'").fetchone()
    assert signer == (device_a,)


# --- the choke point: never called without the OK -----------------------------------------------------------


def test_a_paid_provider_without_the_ok_is_never_called_then_is_after_it(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    unack(db)
    unack(db, "tavily")
    set_settings(db, {"places": {"search": ["google"]}})
    fake = FakePlaces()
    with pytest.raises(PisaError) as err:
        run_places(db, secrets_key, fake, NOW, search(), keys=("google",))
    assert err.value.code == "place_provider_not_configured" and err.value.http_status == 409
    assert err.value.message == NEEDS_ACK_SEARCH
    assert err.value.detail == "paid_ack_required" and err.value.extra["reason"] == "paid_ack_required"
    assert fake.routes() == [] and place_calls(db) == []  # nothing reached Google, nothing metered

    async def test_and_night(env: PlaceEnv) -> tuple[Any, Any]:
        try:
            await env.providers.test("google")
            tested = None
        except PisaError as e:
            tested = e
        night = await env.providers.night_search("q", max_results=5, cap_eur=Decimal(1))
        return tested, night

    tested, night = run_places(db, secrets_key, fake, NOW, test_and_night, keys=("google", "tavily"))
    assert tested is not None and tested.message == NEEDS_ACK_GOOGLE and night is None
    assert fake.routes() == []  # neither its test nor the night's search called anyone

    async def ack(env: PlaceEnv) -> Any:
        return await env.providers.acknowledge("google", 200)

    run_places(db, secrets_key, fake, NOW, ack, keys=())
    choice = run_places(db, secrets_key, fake, NOW, search(), keys=("google",))
    assert choice.provider == "google" and fake.routes() == ["google.search"]


def test_with_a_free_provider_the_unacked_one_is_passed_over_quietly(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    unack(db)
    set_settings(db, {"places": {"search": ["google", "apple"]}})
    choice = run_places(db, secrets_key, FakePlaces(), NOW, search())
    assert (choice.provider, choice.skipped) == ("apple", ["google: waits for the paid OK"])


def test_the_acknowledged_cap_stops_the_provider_past_it(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    db.execute("UPDATE paid_acks SET cap_cents = 1 WHERE provider = 'google'")
    set_settings(db, {"places": {"search": ["google", "apple"]}})
    db.execute(  # the free Text Search Pro allowance is used up: the next search costs, past 1 cent
        "INSERT INTO place_calls (provider, sku, units, at, ok) "
        "VALUES ('google', 'google.text_search_pro', %s, %s, true)",
        (PRICES["google.text_search_pro"].free_per_month, NOW),
    )
    choice = run_places(db, secrets_key, FakePlaces(), NOW, search())
    assert (choice.provider, choice.skipped) == ("apple", ["google: its own monthly cap reached"])
    set_settings(db, {"places": {"search": ["google"]}})
    with pytest.raises(PisaError) as err:
        run_places(db, secrets_key, FakePlaces(), NOW, search())
    assert err.value.code == "places_cap_reached"
    assert err.value.message.startswith("The monthly cap you acknowledged for Google Maps is reached")


def test_the_rest_error_and_the_models_line_say_it_plainly(client, auth, db, fake) -> None:  # type: ignore[no-untyped-def]
    unack(db)
    set_settings(db, {"places": {"search": ["google"]}})
    assert (
        client.put("/v1/places/providers/google/key", headers=auth, json={"key": GOOGLE_KEY}).status_code
        == 204
    )
    got = client.get("/v1/places/search?q=trattoria&near=45.46,9.19", headers=auth)
    assert got.status_code == 409
    assert got.json()["error"] == {
        "code": "place_provider_not_configured",
        "message": NEEDS_ACK_SEARCH,
        "action": "Acknowledge it and its monthly cap in Settings → Connections (Collegamenti)",
        "reason": "paid_ack_required",
    }
    assert fake.routes() == []
    tested = client.post("/v1/places/providers/google/test", headers=auth)
    assert tested.status_code == 409 and tested.json()["error"]["message"] == NEEDS_ACK_GOOGLE
    err = PisaError("place_provider_not_configured", NEEDS_ACK_SEARCH, detail="paid_ack_required")
    line = failed_block("search", err)
    assert f"Waiting for the user's OK: {NEEDS_ACK_SEARCH}" in line and NEEDS_ACK in line
    assert NEEDS_ACK == (
        "a paid provider waits for the user's OK of its monthly cap in Collegamenti (Settings → "
        "Connections), which is no failure: tell the user so plainly in one line"
    )


# --- an install from before v0.1.4 ------------------------------------------------------------------------


def test_a_key_from_before_the_migration_is_not_called_until_acknowledged(  # type: ignore[no-untyped-def]
    db, secrets_key, monkeypatch
) -> None:
    from psycopg import sql

    from pisa.db import migrate as runner

    admin = dsn(db)  # the worker's database: a sibling database made from scratch beside it
    name = "pisa_w3a_before_0026"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(admin, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        every = runner.load_migrations()
        monkeypatch.setattr(runner, "load_migrations", lambda: [m for m in every if m.version < 26])
        runner.migrate(url)
        with psycopg.connect(url, autocommit=True) as conn:  # v0.1.3: a Google Maps key, as stored then
            conn.execute(
                "INSERT INTO place_providers (provider, ciphertext, last4, set_at) "
                "VALUES ('google', %s, %s, now())",
                (KeyStore(SecretStr(secrets_key)).seal(GOOGLE_KEY), GOOGLE_KEY[-4:]),
            )
        monkeypatch.setattr(runner, "load_migrations", lambda: every)
        assert next(m.split("_")[0] for m in runner.migrate(url)) == "0026"
        with psycopg.connect(url, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM paid_acks").fetchone() == (0,)

            async def adapter() -> Any:
                import httpx

                from pisa.db.pool import open_pool
                from pisa.services.place_providers import PlaceProviderService
                from tests.owner import owner_at

                pool = await open_pool(url, max_size=2)
                try:
                    async with httpx.AsyncClient(transport=httpx.MockTransport(FakePlaces())) as http:
                        service = PlaceProviderService(
                            pool,
                            KeyStore(SecretStr(secrets_key)),
                            http,
                            team_id=None,
                            owner=owner_at("Europe/Rome"),
                        )
                        try:
                            await service.adapter("google")
                        except PisaError as e:
                            return e
                        return None
                finally:
                    await pool.close()

            refused = asyncio.run(adapter())
            assert refused is not None and refused.detail == "paid_ack_required"
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_the_logs_carry_no_key_and_no_user_data(client, auth, db, capsys) -> None:  # type: ignore[no-untyped-def]
    unack(db)
    assert (
        client.put("/v1/places/providers/google/key", headers=auth, json={"key": GOOGLE_KEY}).status_code
        == 204
    )
    private = register(client, auth)
    assert (
        _ack(client, device_headers(client, auth, private, "paid_ack", "google:500"), 500).status_code == 200
    )
    client.get("/v1/places/search?q=trattoria segreta&near=45.46,9.19", headers=auth)
    view(client, auth)
    out = capsys.readouterr()
    logged = out.out + out.err
    assert "paid_ack_set" in logged
    assert GOOGLE_KEY not in logged and "trattoria segreta" not in logged
