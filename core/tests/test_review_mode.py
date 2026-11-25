"""App Store review mode (PISA_REVIEW_MODE=1, docs-site/app-review.md):
- off (the default), nothing changes: a code pairs only after the 6-digit SAS is confirmed on the server, a
  review code pairs nothing, no demo task is seeded, discovery lists no `review_mode`;
- on: the demo's synthetic tasks are seeded once, into a database without tasks;
- the review code (`pisa pair --review`) pairs without the SAS for 30 days, up to its number of devices, and
  pairs nothing after 30 days, after `--revoke`, or with the flag off; `pisa pair --review` is refused without
  the flag;
- every connector absent and every outbound write unreachable, even with PISA_EXPERIMENTAL=all: the Google
  routes 404, the push relay off, no web or maps tool offered, no maps provider or Google call, the app
  changes no AI key, model server, slot, capability override or cap;
- a review device offers no pairing, registers no phone key and vouches for none; `--revoke` revokes the
  devices review devices offered too, and deletes their phone keys;
- the flag on a database with data and no seed marker refuses to start (cleanly, from the CLI too), and so
  does a seed's database used for real since (a device not paired for the review, a task made since it was
  served without the flag); a start without the flag warns while review devices are left."""

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from pisa.cli_pair import review_flow
from pisa.db.pool import open_pool
from pisa.domain import experimental
from pisa.domain.errors import PisaError
from pisa.infra.keys import KeyStore
from pisa.services import pairing, review
from tests.conftest import dsn, pair_v2, set_key
from tests.test_experimental import _discovery, _offered
from tests.test_privacy_v2 import ADDRESSES, Phone, make_token, on_db, send, session, tls  # noqa: F401 (tls: a fixture)


@pytest.fixture
def review_flag() -> bool:
    return True


@pytest.fixture
def experimental_spec() -> str:
    return "all"  # whatever PISA_EXPERIMENTAL says, review mode turns every experimental feature off


@pytest.fixture
def app_settings(
    app_settings: Any,
    tls: dict[str, str],  # noqa: F811 (the imported fixture)
    review_flag: bool,
    experimental_spec: str,
) -> Any:
    return app_settings.model_copy(
        update={"tls_cert": tls["cert"], "addresses": ADDRESSES, "review_mode": review_flag,
                "experimental": experimental_spec, "push_relay_url": "https://relay.example.net"}
    )  # fmt: skip


def keys(secrets_key: str) -> KeyStore:
    return KeyStore(SecretStr(secrets_key))


def review_code(db: psycopg.Connection, secrets_key: str, **kw: Any) -> pairing.PairToken:
    return on_db(db, lambda c: pairing.create_review_token(c, keys(secrets_key), **kw))


def pin_of(client: Any) -> str:
    from pisa.services import endpoints

    return endpoints.pins(client.app.state.runtime.settings)[0]


def paired(client: Any, code: str) -> Any:
    """The app pairs with `code` as it does with any code: begin, pair, then the pairing's state."""
    phone = Phone(code, pin_of(client), name="Reviewer's iPhone")
    answer = send(client, phone)
    return phone, answer


def devices(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    return db.execute("SELECT id, name, revoked_at FROM devices ORDER BY created_at").fetchall()


# --- off: nothing changes -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("review_flag", "experimental_spec"), [(False, "")])
def test_off_a_code_still_needs_the_sas_and_nothing_else_changes(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    phone, answer = paired(client, make_token(db, secrets_key).token)
    assert answer.status_code == 201 and answer.json()["state"] == "awaiting_confirmation"
    state = client.get(f"/v1/pair/{phone.begun['pairingId']}").json()
    assert state["state"] == "awaiting_confirmation" and devices(db) == []  # no one compared the digits
    waiting = on_db(db, lambda c: pairing.waiting(c))
    assert len(waiting) == 1  # `pisa pair` asks for the SAS, as always
    device_id = on_db(db, lambda c: pairing.decide(c, waiting[0].pairing_id, True))
    assert client.get(f"/v1/pair/{phone.begun['pairingId']}").json()["state"] == "confirmed"
    assert [d[0] for d in devices(db)] == [device_id]
    # no demo data, no badge
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
    assert "review_mode" not in [f["id"] for f in _discovery(client)["features"]]


@pytest.mark.parametrize(("review_flag", "experimental_spec"), [(False, "")])
def test_off_a_review_code_pairs_nothing_and_none_is_made(
    client: Any, db: psycopg.Connection, secrets_key: str, app_settings: Any
) -> None:
    code = review_code(db, secrets_key)  # left from a time the server was in review mode
    _, answer = paired(client, code.token)
    assert answer.status_code == 400 and answer.json()["error"]["code"] == "pairing_code_invalid"
    assert devices(db) == []
    out = io.StringIO()
    assert on_db(db, lambda c: review_flow(c, app_settings, out=out, qr=False)) == 2  # refused
    assert out.getvalue() == ""
    assert db.execute("SELECT count(*) FROM pair_tokens WHERE review").fetchone() == (1,)  # only the old one


# --- the demo's data ----------------------------------------------------------------------------------------


def test_the_demo_tasks_are_seeded_once(client: Any, db: psycopg.Connection) -> None:
    rows = db.execute("SELECT title, status, due_kind, due_at FROM tasks ORDER BY title").fetchall()
    assert len(rows) == len(review.TASKS)
    titles = [r[0] for r in rows]
    assert "Water the plants" in titles and "Call the dentist (demo)" in titles
    today = client.app.state.runtime.owner.today()
    local = [
        (r[1], r[3].astimezone(client.app.state.runtime.owner.zone).date() if r[3] else None) for r in rows
    ]
    assert ("open", today) in local and ("open", today + timedelta(days=1)) in local
    assert sum(1 for status, _ in local if status == "done") == 1
    marker = db.execute("SELECT value FROM app_settings WHERE key = %s", (review.SEED_KEY,)).fetchone()
    assert marker is not None and marker[0]["tasks"] == len(review.TASKS)

    async def again() -> int:  # a second start (the worker's, a restart)
        pool = await open_pool(dsn(db), max_size=2)
        try:
            return await review.seed(pool, client.app.state.runtime.owner)
        finally:
            await pool.close()

    assert asyncio.run(again()) == 0
    db.execute("DELETE FROM tasks")  # the reviewer deleted every task: still not seeded again
    assert asyncio.run(again()) == 0
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)


def test_a_database_with_tasks_gets_no_demo_tasks_and_is_refused(db: psycopg.Connection, client: Any) -> None:
    db.execute("DELETE FROM tasks")
    db.execute("DELETE FROM app_settings WHERE key = %s", (review.SEED_KEY,))
    db.execute(
        "INSERT INTO tasks (id, title, status, due_kind, source_kind, title_key, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Their own task', 'open', 'none', 'manual', 'their own task', now(), "
        "now())"
    )

    async def start() -> int:
        pool = await open_pool(dsn(db), max_size=2)
        try:
            return await review.seed(pool, client.app.state.runtime.owner)
        finally:
            await pool.close()

    with pytest.raises(PisaError) as refused:  # someone's data, never a review server's: not a review server
        asyncio.run(start())
    assert "fresh database" in refused.value.message and "empty database" in refused.value.action
    assert db.execute("SELECT title FROM tasks").fetchall() == [("Their own task",)]


# --- the review code ----------------------------------------------------------------------------------------


def test_the_review_code_pairs_without_the_sas(client: Any, db: psycopg.Connection, secrets_key: str) -> None:
    code = review_code(db, secrets_key)
    assert code.expires_at - datetime.now(UTC) > timedelta(days=29, hours=23)
    phone, answer = paired(client, code.token)
    assert answer.status_code == 201
    body = answer.json()
    assert body["state"] == "awaiting_confirmation"  # the same answer as for any code (contract 2)
    assert on_db(db, lambda c: pairing.waiting(c)) == []  # nothing for anyone to confirm
    state = client.get(f"/v1/pair/{body['pairingId']}").json()
    assert state["state"] == "confirmed" and state["deviceId"] == body["deviceId"]
    made = session(client, phone, body["deviceId"])
    assert made.status_code == 200 and made.json()["token"]
    assert [(str(d[0]), d[1]) for d in devices(db)] == [(body["deviceId"], "Reviewer's iPhone")]


def test_the_review_code_pairs_up_to_its_devices(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    code = review_code(db, secrets_key)  # 3 by default
    for _ in range(pairing.REVIEW_MAX_USES):
        _, answer = paired(client, code.token)
        assert answer.status_code == 201
    _, fourth = paired(client, code.token)
    assert fourth.status_code == 400 and fourth.json()["error"]["code"] == "pairing_code_invalid"
    assert len(devices(db)) == pairing.REVIEW_MAX_USES
    single = review_code(db, secrets_key, devices=1)
    assert paired(client, single.token)[1].status_code == 201
    assert paired(client, single.token)[1].status_code == 400
    # never more than 3, whatever is asked
    many = review_code(db, secrets_key, devices=50)
    assert db.execute("SELECT max_uses FROM pair_tokens WHERE id = %s", (many.id,)).fetchone() == (3,)


def test_a_new_review_code_replaces_the_open_one(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    old = review_code(db, secrets_key)
    review_code(db, secrets_key)
    assert paired(client, old.token)[1].status_code == 400


def test_the_review_code_works_for_30_days_only(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    almost = review_code(db, secrets_key, now=datetime.now(UTC) - timedelta(days=29, hours=23))
    assert paired(client, almost.token)[1].status_code == 201
    late = review_code(db, secrets_key, now=datetime.now(UTC) - timedelta(days=30, minutes=1))
    _, answer = paired(client, late.token)
    assert answer.status_code == 400 and answer.json()["error"]["code"] == "pairing_code_invalid"


def test_revoking_ends_the_code_and_its_devices(
    client: Any, db: psycopg.Connection, secrets_key: str, app_settings: Any
) -> None:
    own_id, _ = pair_v2(db, "Publisher's iPhone")  # paired the usual way: never touched
    code = review_code(db, secrets_key)
    phone, answer = paired(client, code.token)
    device_id = answer.json()["deviceId"]
    out = io.StringIO()
    assert on_db(db, lambda c: review_flow(c, app_settings, revoke=True, out=out)) == 0
    assert "1 device" in out.getvalue() or "1 dispositivi" in out.getvalue()
    revoked = {str(d[0]): d[2] for d in devices(db)}
    assert revoked[device_id] is not None and revoked[own_id] is None
    assert session(client, phone, device_id).json()["error"]["code"] == "device_revoked"
    assert paired(client, code.token)[1].status_code == 400  # the code pairs nothing more


def test_pisa_pair_review_shows_the_code(db: psycopg.Connection, app_settings: Any) -> None:
    out = io.StringIO()
    assert on_db(db, lambda c: review_flow(c, app_settings, devices=2, as_json=True, out=out)) == 0
    shown = json.loads(out.getvalue())
    assert len(shown["token"]) == 26 and shown["devices"] == 2
    assert shown["qr"]["token"] == shown["token"]
    exp = datetime.fromisoformat(shown["expires_at"])
    assert timedelta(days=29) < exp - datetime.now(UTC) <= timedelta(days=30)
    text = io.StringIO()
    assert on_db(db, lambda c: review_flow(c, app_settings, out=text, qr=False)) == 0
    assert "pisa://pair?" in text.getvalue() and "--revoke" in text.getvalue()


# --- nothing connected, nothing written outward -------------------------------------------------------------


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_every_connector_is_absent_even_with_every_experimental_feature_named(
    client: Any, db: psycopg.Connection
) -> None:
    runtime = client.app.state.runtime
    assert runtime.enabled == {experimental.REVIEW}  # review mode's mark: no experimental feature
    assert experimental.enabled("all", review_mode=True) == {experimental.REVIEW}
    assert experimental.listed(runtime.enabled) == []
    _, token = pair_v2(db)
    assert client.get("/v1/connectors", headers=auth(token)).json() == {"connectors": []}
    for path in ("/v1/connectors/google", "/v1/connectors/google/setup", "/v1/connectors/garmin",
                 "/v1/connectors/bank", "/v1/obligations"):  # fmt: skip
        got = client.get(path, headers=auth(token), follow_redirects=False)
        assert got.status_code == 404, path
    assert client.post("/v1/connectors/google/start", json={}, headers=auth(token)).status_code in (404, 422)
    assert client.get("/oauth/google/callback?state=x&code=y").status_code == 404
    assert (
        client.post("/v1/health/summary", json={"day": "2026-10-20"}, headers=auth(token)).status_code == 404
    )


def test_the_push_relay_is_off_and_discovery_says_review_mode(client: Any) -> None:
    doc = _discovery(client)  # valid against contract 2's schema, the badge included
    ids = [f["id"] for f in doc["features"]]
    assert ids[-1] == "review_mode" and not [i for i in ids if i.startswith("experimental.")]
    assert {"id": "review_mode", "version": 1} in doc["features"]
    assert doc["push"]["relayConfigured"] is False and "relay" not in doc["push"]["modes"]
    assert client.get("/readyz").json()["relay"] == "off"
    assert client.app.state.runtime.push.relay_configured() is False


def test_no_outbound_tool_is_offered(
    client: Any, db: psycopg.Connection, secrets_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = pair_v2(db)
    offered = _offered(client, db, secrets_key, device, monkeypatch)
    outward = {"web_search", "read_page", "map_search", "search_my_email", "create_event", "compose_message",
               "call_place", "show_email", "shop_prices", "travel_time"}  # fmt: skip
    assert outward.isdisjoint(offered)
    # reminders are supported and stay inside the server: the reviewer can ask for one
    assert {"add_task", "get_tasks", "get_plan", "get_weather", "stop_everything", "remember_to"} <= offered


def test_no_maps_web_or_google_call_and_no_ai_change_from_the_app(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    import httpx

    from pisa.services.google import GoogleService
    from pisa.services.place_providers import PlaceProviderService

    runtime = client.app.state.runtime
    assert runtime.places.providers.disabled is True and runtime.google.disabled is True
    db.execute(  # a provider key left in the database: still never used
        "INSERT INTO place_providers (provider, ciphertext, set_at) VALUES ('tavily', %s, now())",
        (keys(secrets_key).seal("tvly-x"),),
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"an outbound call: {request.url}")

    async def try_them() -> None:
        pool = await open_pool(dsn(db), max_size=2)
        http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        try:
            owner = runtime.owner
            providers = PlaceProviderService(pool, keys(secrets_key), http, team_id=None, owner=owner,
                                             disabled=True)  # fmt: skip
            assert await providers.adapter("tavily") is None and await providers.adapter("google") is None
            with pytest.raises(PisaError) as set_up:
                await providers.set_key("tavily", "tvly-y")
            assert set_up.value.code == "connector_off"
            google = GoogleService(pool, keys(secrets_key), http, httpx.Client(), owner=owner, disabled=True)
            with pytest.raises(PisaError) as called:
                await google.access_token()
            assert called.value.code == "connector_off"
        finally:
            await http.aclose()
            await pool.close()

    asyncio.run(try_them())
    set_key(db, secrets_key)  # the operator's key, set on the server
    _, token = pair_v2(db)
    put = client.put("/v1/ai/providers/openai/key", json={"key": "sk-someone-else"}, headers=auth(token))
    assert put.status_code == 409 and put.json()["error"]["code"] == "connector_off"
    server = client.post(
        "/v1/ai/providers",
        json={"type": "openai_compatible", "id": "elsewhere", "base_url": "https://llm.example.net/v1"},
        headers=auth(token),
    )  # fmt: skip
    assert server.status_code == 409 and server.json()["error"]["code"] == "connector_off"
    assert client.delete("/v1/ai/providers/openai/key", headers=auth(token)).status_code == 409


# --- the security review's fixes (RV-1 to RV-4) ------------------------------------------------------------


def review_device(client: Any, db: psycopg.Connection, secrets_key: str) -> tuple[Phone, str, str]:
    """A device paired with the review code, its session token."""
    phone, answer = paired(client, review_code(db, secrets_key).token)
    device_id = answer.json()["deviceId"]
    token = session(client, phone, device_id).json()["token"]
    return phone, device_id, token


def device_key_id(device_id: str) -> str:
    import uuid

    from pisa.domain import pairing as rules

    return rules.device_key_id(uuid.UUID(device_id))


def test_rv1_a_review_device_offers_no_pairing_and_vouches_for_no_key(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    from tests.phone_signing import challenge, new_key, sign

    phone, device_id, token = review_device(client, db, secrets_key)
    assert db.execute("SELECT review FROM devices WHERE id = %s", (device_id,)).fetchone() == (True,)
    offer = client.post("/v1/pair/offers", json={"name": "Reviewer's iPad"}, headers=auth(token))
    assert offer.status_code == 403 and offer.json()["error"]["code"] == "biometric_required"
    assert "review code" in offer.json()["error"]["message"]
    assert db.execute("SELECT count(*) FROM pair_tokens WHERE offered_by = %s", (device_id,)).fetchone() == (
        0,
    )
    # nor does its device key vouch for another device's key (D-138's add_key)
    _, other_token = pair_v2(db, "Someone's iPhone")
    _, public = new_key()
    made = challenge(client, auth(token), action="add_key", key_id="k-vouched", public_key=public)
    body = {"key_id": "k-vouched", "public_key": public, "nonce": made["nonce"],
            "signature": sign(phone.key, made["canonical"]),
            "signer_key_id": device_key_id(device_id)}  # fmt: skip
    vouched = client.post("/v1/phone/keys", json=body, headers=auth(other_token))
    assert vouched.status_code == 403 and vouched.json()["error"]["code"] == "biometric_required"
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)


def test_rv1_revoke_follows_the_devices_a_review_device_offered(
    client: Any, db: psycopg.Connection, secrets_key: str, app_settings: Any
) -> None:
    """A device a review device offered before the refusal (made directly here, the offering device's mark
    lifted for the offer and the offered device left unmarked) is revoked with it, down the chain."""
    import uuid

    from pisa.services import endpoints

    _, review_id, _ = review_device(client, db, secrets_key)
    own_id, _ = pair_v2(db, "Publisher's iPhone")
    settings = client.app.state.runtime.settings
    pins = endpoints.pins(settings)
    db.execute("UPDATE devices SET review = false WHERE id = %s", (review_id,))
    offered = on_db(db, lambda c: pairing.offer(c, keys(secrets_key), uuid.UUID(review_id), name="iPad",
                                                addresses=settings.address_list, pins=pins))  # fmt: skip
    new = Phone(offered["token"], pin_of(client), name="Offered iPad")
    pairing_id = send(client, new).json()["pairingId"]
    t = on_db(db, lambda c: pairing.offer_state(c, offered["offerId"], uuid.UUID(review_id)))["transcript"]
    from pisa.domain import pairing as rules

    sas = rules.sas(rules.transcript(t["spki"], t["deviceKey"], t["clientNonce"]), t["serverNonce"])
    confirmed = on_db(db, lambda c: pairing.confirm_offer(c, offered["offerId"], uuid.UUID(review_id),
                                                         pairing_id, sas))  # fmt: skip
    offered_id = confirmed["deviceId"]
    db.execute("UPDATE devices SET review = true WHERE id = %s", (review_id,))
    db.execute("UPDATE devices SET review = false WHERE id = %s", (offered_id,))  # "somehow" unmarked
    assert session(client, new, offered_id).status_code == 200  # it opens sessions
    out = io.StringIO()
    assert on_db(db, lambda c: review_flow(c, app_settings, revoke=True, out=out)) == 0
    revoked = {str(d[0]): d[2] for d in devices(db)}
    assert revoked[review_id] is not None and revoked[offered_id] is not None and revoked[own_id] is None
    assert session(client, new, offered_id).json()["error"]["code"] == "device_revoked"


def test_rv1_a_device_a_review_device_offered_is_a_review_device(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    """The mark is inherited at the pairing (`decide`), so the revoke finds it without the pairing rows."""
    import uuid

    _, review_id, _ = review_device(client, db, secrets_key)
    made = on_db(db, lambda c: pairing.create_token(c, keys(secrets_key), "iPad", origin="app"))
    db.execute("UPDATE pair_tokens SET offered_by = %s WHERE id = %s", (review_id, made.id))
    new = Phone(made.token, pin_of(client), name="Offered iPad")
    answer = send(client, new).json()
    on_db(db, lambda c: pairing.decide(c, answer["pairingId"], True, offered_by=uuid.UUID(review_id)))
    assert db.execute("SELECT review FROM devices WHERE id = %s", (answer["deviceId"],)).fetchone() == (True,)


def test_rv2_a_review_iphone_gets_no_phone_key_and_its_keys_go_at_revoke(
    client: Any, db: psycopg.Connection, secrets_key: str, app_settings: Any
) -> None:
    from tests.phone_signing import new_key, register

    _, device_id, token = review_device(client, db, secrets_key)
    _, public = new_key()
    first = client.post("/v1/phone/keys", json={"key_id": "face", "public_key": public}, headers=auth(token))
    assert first.status_code == 403 and first.json()["error"]["code"] == "biometric_required"
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)
    # one taken before the fix: deleted with its device
    import base64

    db.execute(
        "INSERT INTO phone_keys (key_id, public_key, label, device_id, created_at, kind) VALUES "
        "('face-old', %s, 'Reviewer', %s, now(), 'biometric')",
        (base64.b64decode(public), device_id),
    )
    assert on_db(db, lambda c: review_flow(c, app_settings, revoke=True, out=io.StringIO())) == 0
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)
    _, own_token = pair_v2(db, "Publisher's iPhone")  # the publisher's own iPhone takes the first key
    register(client, auth(own_token), "face-own")


def test_the_app_changes_no_setting_in_review_mode_and_still_reads_them(
    client: Any, db: psycopg.Connection
) -> None:
    """PATCH /v1/settings is locked like the AI keys and caps (409 `connector_off`, nothing changed); GET
    works."""
    _, token = pair_v2(db)
    before = client.get("/v1/settings", headers=auth(token))
    assert before.status_code == 200
    stored = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchall()
    for patch in ({"quiet": {"hours": None}}, {"messages_retention": "forever"}, {"language": "en-US"}):
        changed = client.patch("/v1/settings", json=patch, headers=auth(token))
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "connector_off"
    assert db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchall() == stored
    assert client.get("/v1/settings", headers=auth(token)).json() == before.json()


@pytest.mark.parametrize(("review_flag", "experimental_spec"), [(False, "")])
def test_off_the_app_changes_its_settings(client: Any, db: psycopg.Connection) -> None:
    _, token = pair_v2(db)
    changed = client.patch("/v1/settings", json={"quiet": {"hours": None}}, headers=auth(token))
    assert changed.status_code == 200 and changed.json()["quiet"]["hours"] is None


def test_rv3_the_app_changes_no_slot_override_or_cap(client: Any, db: psycopg.Connection) -> None:
    _, token = pair_v2(db)
    before = db.execute("SELECT * FROM model_slots ORDER BY 1").fetchall()
    caps = client.put("/v1/ai/caps", json={"daily_eur": 9, "monthly_eur": 90}, headers=auth(token))
    assert caps.status_code == 409 and caps.json()["error"]["code"] == "connector_off"
    slot = client.put("/v1/ai/slots/chat.conversation", json={"model": "openai:gpt-5", "fallback": None},
                      headers=auth(token))  # fmt: skip
    assert slot.status_code == 409 and slot.json()["error"]["code"] == "connector_off"
    overrides = client.put("/v1/ai/providers/ollama/capabilities/overrides", json={"tools": True},
                           headers=auth(token))  # fmt: skip
    assert overrides.status_code == 409 and overrides.json()["error"]["code"] == "connector_off"
    assert db.execute("SELECT * FROM model_slots ORDER BY 1").fetchall() == before
    assert client.get("/v1/ai/slots", headers=auth(token)).status_code == 200  # reading is fine


def test_rv4_a_database_with_data_refuses_review_mode_cleanly(
    db: psycopg.Connection, app_settings: Any, fake_models: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    import uvicorn

    from pisa import cli
    from pisa.services.runtime import Runtime
    from pisa.workers import main as worker

    pair_v2(db, "Their iPhone")  # someone's server: a device, no review seed marker
    with pytest.raises(PisaError) as refused:
        asyncio.run(Runtime.start(app_settings, model_factory=fake_models))
    assert "fresh database" in refused.value.message
    # `pisa serve-api` and the worker exit cleanly, before uvicorn or DBOS, naming the problem and the fix
    plain = app_settings.model_copy(update={"api_tls_cert": "", "api_tls_key": "",
                                            "dbos_database_url": app_settings.database_url})  # fmt: skip
    monkeypatch.setattr(cli, "get_settings", lambda: plain)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("uvicorn started"))
    capsys.readouterr()
    assert cli._cmd_serve_api(None) == 1  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert "PISA_REVIEW_MODE=1" in err and "fresh database" in err and "new, empty database" in err
    assert "Traceback" not in err
    assert worker.run_worker(plain) == 2
    assert "fresh database" in capsys.readouterr().err
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)  # nothing seeded


@pytest.mark.parametrize(("review_flag", "experimental_spec"), [(False, "")])
def test_rv4_a_start_without_the_flag_warns_of_review_devices_left(
    client: Any, db: psycopg.Connection, secrets_key: str, app_settings: Any, fake_models: Any
) -> None:
    from structlog.testing import capture_logs

    from pisa.services.runtime import Runtime

    pair_v2(db, "Publisher's iPhone")
    left_id, _ = pair_v2(db, "Reviewer's iPhone")
    db.execute("UPDATE devices SET review = true WHERE id = %s", (left_id,))

    async def start() -> None:
        runtime = await Runtime.start(app_settings, model_factory=fake_models)
        await runtime.close()

    with capture_logs() as logs:
        asyncio.run(start())
    left = [e for e in logs if e["event"] == "review_devices_left"]
    assert len(left) == 1 and left[0]["count"] == 1 and "pisa pair --review --revoke" in left[0]["action"]
    assert left[0]["log_level"] == "warning"
    assert all(d[2] is None for d in devices(db))  # warned, never revoked


def test_rv4_a_review_seed_used_for_real_since_refuses_review_mode_again(
    client: Any, db: psycopg.Connection, secrets_key: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], app_settings: Any,
) -> None:  # fmt: skip
    """The seed marker exempts a database only while it is the review's: the reviewer's own tasks and devices
    keep it a review server; a device not paired for the review, or a task made once it was served without
    the flag, refuses a later review-mode start, with the same clean exit and message."""
    from pisa import cli

    def start(review_mode: bool) -> None:
        on_db(db, lambda c: review.check(c, review_mode=review_mode))

    assert paired(client, review_code(db, secrets_key).token)[1].status_code == 201
    assert db.execute("SELECT count(*), bool_and(review) FROM devices").fetchone() == (1, True)
    db.execute(
        "INSERT INTO tasks (id, title, status, due_kind, source_kind, title_key, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'A reviewer task (demo)', 'open', 'none', 'manual', 'a reviewer task', "
        "now(), now())"
    )
    start(True)  # the reviewer's task and device: still the review's
    # the publisher serves it without the flag after the review, and it holds their task since
    start(False)
    marker = db.execute("SELECT value FROM app_settings WHERE key = %s", (review.SEED_KEY,)).fetchone()
    assert marker is not None and "off_at" in marker[0]
    start(True)  # nothing made since: still allowed
    db.execute(
        "INSERT INTO tasks (id, title, status, due_kind, source_kind, title_key, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Their own task', 'open', 'none', 'manual', 'their own task', "
        "now() + interval '1 second', now())"
    )
    with pytest.raises(PisaError) as refused:
        start(True)
    assert "fresh database" in refused.value.message and "empty database" in refused.value.action
    db.execute("DELETE FROM tasks WHERE title = 'Their own task'")
    start(True)
    # a device not paired for the review (their own phone), even revoked
    their, _ = pair_v2(db, "Their iPhone")
    db.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (their,))
    with pytest.raises(PisaError):
        start(True)
    # `pisa serve-api` exits cleanly, before uvicorn, naming the problem and the fix
    import uvicorn

    plain = app_settings.model_copy(update={"api_tls_cert": "", "api_tls_key": ""})
    monkeypatch.setattr(cli, "get_settings", lambda: plain)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("uvicorn started"))
    capsys.readouterr()
    assert cli._cmd_serve_api(None) == 1  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert "PISA_REVIEW_MODE=1" in err and "fresh database" in err and "Traceback" not in err


# --- RV-5: Pisa Learn, the handoffs and the merchants (UCP) reach outside the server ------------------------

# Tools that reach outside the server (the web, maps, Google, the phone's messages and calls, Learn's papers,
# the merchants), named here as well as derived from the registry below: a tool added to one of these
# families, or a new tool the registry marks as slow research, outward or outside text, fails
# `test_rv5_no_tool_offered_in_review_mode_reaches_outside` until it is classified.
KNOWN_OUTBOUND = frozenset({
    "web_search", "read_page", "map_search", "place_details", "walk_times", "travel_time", "shop_prices",
    "table_slots", "events_on", "detour", "open_at", "where_am_i", "show_places", "show_place",
    "show_products", "compare_places", "start_navigation", "search_my_email", "read_my_email", "show_email",
    "thing", "create_event", "move_event", "cancel_event", "draft_email", "draft_reply", "edit_draft",
    "send_draft", "compose_message", "call_place", "learn_ask", "learn_sources", "find_products",
    "prepare_cart", "nearest_pharmacy", "next_departures", "transit_alerts", "weather_alerts", "ztl_status",
    "airport_departure",
})  # fmt: skip
# outside text Pisa already holds (mail synced before, read from its own database): no call outward now
# outside text already on the server: no call leaves it (Memoria's `find_note`: their own notes, read back as
# outside text since a note may hold anything; `app_help`: the app's own screen map, contract 2.2)
STORED_OUTSIDE_TEXT = frozenset({"get_trips", "list_bills", "while_i_was_away", "find_note", "app_help"})
# the tool families whose every tool reaches outside (`ToolFamilies` fields): Learn's papers, the handoffs and
# the merchants' carts, the public feeds
OUTBOUND_FAMILIES = frozenset({"learn", "handoffs", "world", "public"})


def outbound_tools() -> frozenset[str]:
    """Every tool that reaches outside the server: the outbound families' tools (the agent built with every
    service, less the agent built without them), the single tools of the outward features of
    `experimental.TOOLS` and Google's connector tools, and `KNOWN_OUTBOUND`."""
    from pisa.domain import connectors as connector_rules
    from tests.test_haiku_writes_and_views import every_tool

    families = set(every_tool()) - set(every_tool(OUTBOUND_FAMILIES))
    singles = {t for f in ("google", "places", "phone_actions") for t in experimental.TOOLS[f]}
    return frozenset(families | singles | set(connector_rules.GOOGLE_TOOLS) | KNOWN_OUTBOUND)


def test_rv5_the_outbound_set_covers_every_registry_tool_that_reaches_outside() -> None:
    from pisa.domain.tools import TOOLS

    outbound = outbound_tools()
    marked = {t.name for t in TOOLS if t.slow or t.effect == "outward" or t.outside is not None}
    unclassified = sorted(marked - outbound - STORED_OUTSIDE_TEXT)
    assert not unclassified, f"a tool that reaches outside: add it to KNOWN_OUTBOUND ({unclassified})"
    # the families' own tools are in it (the derivation found them)
    assert {"learn_ask", "learn_sources", "prepare_cart", "find_products", "ztl_status"} <= outbound
    assert {"add_task", "get_tasks", "get_weather", "remember_to"}.isdisjoint(outbound)


def _built_tools(monkeypatch: pytest.MonkeyPatch) -> list[set[str]]:
    """Every tool of each agent built from now on (the deferred ones too)."""
    from pydantic_ai.toolsets import FunctionToolset

    from pisa.services import conversation as conversation_service

    built: list[set[str]] = []
    original = conversation_service.build_agent

    def spy(model: Any, context: Any) -> Any:
        agent = original(model, context)
        built.append({n for t in agent.toolsets if isinstance(t, FunctionToolset) for n in t.tools})
        return agent

    monkeypatch.setattr(conversation_service, "build_agent", spy)
    return built


COURSE_TURN = "cosa dicono le fonti del corso di probing?"  # a turn about a course: Learn's tools if on


def test_rv5_no_tool_offered_in_review_mode_reaches_outside(
    client: Any, db: psycopg.Connection, secrets_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed turn and a spoken one (a talk's turn, `voice`), about a course and about a purchase, under
    review mode with every experimental feature named: no offered tool, deferred ones included, reaches
    outside."""
    import uuid

    from tests.test_live_v2_socket import connect, hello, opened, run

    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    built = _built_tools(monkeypatch)
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        for words in (COURSE_TURN, "comprami il latte su un negozio online"):
            turn = run(words)
            app.send(turn)
            app.run_ends(turn["input"]["runId"])
    typed = len(built)
    conversations = client.app.state.runtime.conversations

    async def emit(delta: str, final: bool) -> None:
        return None

    for words in (COURSE_TURN, "prepara il carrello per il latte"):
        client.portal.call(lambda w=words: conversations.respond(
            uuid.UUID(device_id), w, "it-IT", emit, trace_id=uuid.uuid4().hex, voice=True,
        ))  # fmt: skip
    assert typed >= 2 and len(built) >= typed + 2, "a turn built no agent"
    outbound = outbound_tools()
    for offered in built:
        assert offered, "an agent with no tool"
        assert not offered & outbound, sorted(offered & outbound)
        assert {"add_task", "get_tasks", "get_weather"} <= offered  # inside the server: still offered


def test_rv5_learn_routes_are_absent(client: Any, db: psycopg.Connection) -> None:
    _, token = pair_v2(db)
    for method, path, body in (
        ("post", "/v1/learn/sources", {"items": ["https://example.org/paper.pdf"]}),
        ("get", "/v1/learn/sources", None),
        ("delete", "/v1/learn/sources/00000000-0000-4000-8000-000000000001", None),
        ("post", "/v1/learn/ask", {"question": "cosa mostra la Tabella 3?"}),
    ):
        kw: dict[str, Any] = {"json": body} if body is not None else {}
        got = getattr(client, method)(path, headers=auth(token), **kw)
        assert got.status_code == 404 and got.json()["error"]["code"] == "not_found", path
    assert db.execute("SELECT count(*) FROM learn_sources").fetchone() == (0,)


def test_rv5_learn_s_fetcher_and_the_ucp_client_make_no_request(client: Any) -> None:
    import httpx

    from pisa.connectors.learn_fetch import LearnFetcher
    from pisa.connectors.ucp import UcpClient, UcpError

    runtime = client.app.state.runtime
    assert runtime.learn._fetch.disabled is True and runtime.world._ucp.disabled is True  # review mode's

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"an outbound call: {request.url.host}")

    class Signer:  # signs anything: only the disabled flag stands between it and the merchant
        agent_url = "https://pisa.example.net"

        async def headers(self, method: str, url: str, purpose: Any) -> dict[str, str]:
            return {}

    async def try_them() -> None:
        fetcher = LearnFetcher(
            contact_email="owner@example.net",
            transport=lambda _: httpx.MockTransport(refuse),
            resolver=_public,
            disabled=True,
        )
        for call in (
            fetcher.arxiv_meta("2601.00001"),
            fetcher.page("https://example.org/paper.pdf"),
            fetcher.openalex_title("Probing Gadget Models"),
            fetcher.unpaywall("10.1234/x"),
        ):
            with pytest.raises(PisaError) as off:
                await call
            assert off.value.code == "connector_off"
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
            ucp = UcpClient(http, Signer(), disabled=True)  # type: ignore[arg-type]
            for call in (ucp.discover("shop.example.com"),
                         ucp.search("https://shop.example.com/ucp", "latte", 3),
                         ucp.cart("https://shop.example.com/ucp", [("p1", 1)], "USD")):  # fmt: skip
                with pytest.raises(UcpError):
                    await call

    asyncio.run(try_them())


async def _public(host: str) -> list[str]:
    return ["93.184.215.14"]


def test_app_review_md_tells_the_publisher_to_reset_the_review_server() -> None:
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "docs-site" / "app-review.md").read_text()
    after = page.split("**After the review, reset the review server**", 1)[1].split("\n\n", 1)[0]
    assert "`pisa pair --review --revoke`" in after and "fresh database" in after
    assert "refuses review mode again" in after
