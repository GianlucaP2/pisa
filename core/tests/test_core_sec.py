"""The security audits' server fixes."""

import hashlib
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from pisa.cli import main
from pisa.domain import pairing as rules
from pisa.services.phone_keys import digest
from tests.conftest import dsn, pair
from tests.phone_signing import device_headers, new_key, register, shown_digest, signed_approval
from tests.test_privacy_v2 import Phone, app_settings, send, tls  # noqa: F401 (the fixtures)

# --- One device token takes over --------------------------------------------------------------------


@pytest.fixture
def phone(client, db) -> tuple[str, dict[str, str], Any]:  # type: ignore[no-untyped-def]
    """The user's iPhone, paired with a host code, its Face ID key registered."""
    device_id, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    return device_id, auth, register(client, auth)


def _refused(response: Any) -> bool:
    return response.status_code == 403 and response.json()["error"]["code"] == "biometric_required"


def _offer(client: Any, headers: dict[str, str], name: str) -> Any:
    """`POST /v1/pair/offers` with `{"name": name}`, the body sent as signed (its SHA-256 is the target)."""
    return client.post(
        "/v1/pair/offers", content=_body(name), headers=headers | {"content-type": "application/json"}
    )


def _body(name: str) -> str:
    return json.dumps({"name": name})


def _target(name: str) -> str:
    return hashlib.sha256(_body(name).encode()).hexdigest()


def test_a_pairing_offer_needs_the_phone_s_signature(client, phone) -> None:  # type: ignore[no-untyped-def]
    _, auth, private = phone
    assert _refused(_offer(client, auth, "iPad"))
    other = device_headers(client, auth, private, "pairing_offer", _target("Mac"))  # signed for another body
    assert _refused(_offer(client, other, "iPad"))
    signed = device_headers(client, auth, private, "pairing_offer", _target("iPad"))
    made = _offer(client, signed, "iPad")
    assert made.status_code == 201
    assert _refused(_offer(client, signed, "iPad"))  # once


def _paired_from_offer(
    client: Any, auth: dict[str, str], private: Any, name: str
) -> tuple[str, dict[str, str]]:
    """An iPhone paired from the phone's offer, confirmed by its SAS (signed when `private` is given): its id
    and its session's headers."""
    from tests.test_pairing_review_fixes import session

    headers = device_headers(client, auth, private, "pairing_offer", _target(name)) if private else auth
    made = _offer(client, headers, name).json()
    device = Phone(made["token"], rules.parse_qr(made["qr"])["spki"], name=name, platform="ios")
    paired = send(client, device).json()
    t = client.get(f"/v1/pair/offers/{made['offerId']}", headers=auth).json()["transcript"]
    sas = rules.sas(rules.transcript(t["spki"], t["deviceKey"], t["clientNonce"]), t["serverNonce"])
    target = f"{paired['pairingId']}:{sas}"
    confirm = device_headers(client, auth, private, "pairing_confirm", target) if private else auth
    confirmed = client.post(f"/v1/pair/offers/{made['offerId']}/confirm",
                            json={"pairingId": paired["pairingId"], "sas": sas}, headers=confirm)  # fmt: skip
    assert confirmed.status_code == 200, confirmed.text
    token = session(client, device, paired["deviceId"]).json()["token"]
    return paired["deviceId"], {"Authorization": f"Bearer {token}"}


def test_the_stolen_token_s_takeover_is_refused(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    """The audit's probe: a token alone pairs a fake "iPhone", revokes the user's, and registers its key."""
    phone_id, auth, private = phone
    thief_id, thief = _paired_from_offer(client, auth, private, "iPhone")
    assert db.execute("SELECT paired_via FROM devices WHERE id = %s", (thief_id,)).fetchone() == ("app",)
    assert _refused(client.delete(f"/v1/devices/{phone_id}", headers=thief))
    assert client.get("/v1/devices", headers=auth).status_code == 200  # the user is still in
    # the user revokes it, signed; a device revokes itself without a signature
    gone = client.delete(
        f"/v1/devices/{thief_id}", headers=device_headers(client, auth, private, "revoke_device", thief_id)
    )
    assert gone.status_code == 204


def test_a_device_revokes_itself_unsigned(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    other_id, token = pair(db, "iPad")
    assert (
        client.delete(f"/v1/devices/{other_id}", headers={"Authorization": f"Bearer {token}"}).status_code
        == 204
    )


def test_provider_keys_and_google_need_the_signature(client, phone) -> None:  # type: ignore[no-untyped-def]
    _, auth, private = phone
    key = "sk-attacker-0000000000"
    assert _refused(client.put("/v1/ai/providers/openai/key", headers=auth, json={"key": key}))
    other = device_headers(client, auth, private, "provider_key", f"openai:{digest('sk-owner-111111111')}")
    assert _refused(client.put("/v1/ai/providers/openai/key", headers=other, json={"key": key}))
    client.app.state.runtime.google.redirect_uri = "https://pisa.example.ts.net/oauth/google/callback"
    assert _refused(client.post("/v1/connectors/google/start", headers=auth))
    wrong = device_headers(client, auth, private, "google_connect", digest("https://attacker.example/cb"))
    assert _refused(client.post("/v1/connectors/google/start", headers=wrong))  # bound to this server's URI
    assert _refused(client.delete("/v1/connectors/google", headers=auth))
    signed = device_headers(client, auth, private, "google_disconnect", "google")
    assert client.delete("/v1/connectors/google", headers=signed).status_code == 204


def test_before_the_first_key_the_token_still_sets_up_pisa(client, db) -> None:  # type: ignore[no-untyped-def]
    """A fresh install: no Face ID key yet, the onboarding works with the session token (only a host-paired
    iPhone registers the first key)."""
    _, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    assert _offer(client, auth, "iPad").status_code == 201


def test_the_first_key_only_from_a_host_paired_iphone(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    _, app_paired = _paired_from_offer(client, auth, None, "iPhone")
    _, public = new_key()
    refused = client.post("/v1/phone/keys", json={"key_id": "k1", "public_key": public}, headers=app_paired)
    assert refused.status_code == 400 and "server" in refused.json()["error"]["message"]
    register(client, auth)  # the host-paired iPhone does


def test_the_primary_phone_is_host_paired_and_has_the_key(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.zone import primary_phone

    owner, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    register(client, auth)
    newer, _ = pair(db, "iPhone 2")  # host-paired, newer, no key: not primary while the user's has one
    db.execute("UPDATE devices SET created_at = now() + interval '1 minute' WHERE id = %s", (newer,))
    app_id = db.execute(
        "INSERT INTO devices (id, name, platform, created_at, paired_via) VALUES "
        "(gen_random_uuid(), 'iPhone', 'ios', now() + interval '2 minutes', 'app') RETURNING id"
    ).fetchone()
    assert app_id is not None

    async def go() -> Any:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await primary_phone(conn)

    assert str(client.portal.call(go)) == owner


def test_phone_keys_reset_keeps_one_device_and_revokes_the_rest(  # type: ignore[no-untyped-def]
    db,
    app_settings,  # noqa: F811 (the TLS one, imported above)
    monkeypatch,
    capsys,
) -> None:
    phone, _ = pair(db, "iPhone")
    other, _ = pair(db, "iPhone")  # the thief's
    ipad, _ = pair(db, "iPad", "other")
    db.execute(
        "INSERT INTO phone_keys (key_id, public_key, device_id, created_at) "
        "VALUES ('k1', '\\x00', %s, now())",
        (other,),
    )
    monkeypatch.setenv("PISA_DATABASE_URL", app_settings.database_url)
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level, **_: None)
    import pisa.cli

    seen: list[str] = []  # the database error the CLI says as one sentence, kept for a failure's message
    said = pisa.cli._unreachable  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(pisa.cli, "_unreachable", lambda exc: seen.append(repr(exc)) or said(exc))
    with pytest.raises(SystemExit):
        main(["phone-keys", "reset"])  # --keep is required
    assert main(["phone-keys", "reset", "--keep", ipad]) == 2, seen  # not an iPhone
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (1,)
    assert main(["phone-keys", "reset", "--keep", phone]) == 0
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)
    live = db.execute("SELECT id::text FROM devices WHERE revoked_at IS NULL").fetchall()
    assert live == [(phone,)]


# --- Approval signing ----------------------------------------------------------------------------------


def test_the_server_builds_the_shared_fixture_byte_for_byte() -> None:
    import json

    from pisa.domain import approvals as rules

    fx = json.loads(
        (Path(__file__).parents[2] / "protocol" / "fixtures" / "rest" / "k30_canonical.json").read_text()
    )
    a = fx["approval"]
    assert rules.payload_sha256(a["payload"]) == fx["payload_sha256"]
    assert rules.approval_digest(a["kind"], a["payload"], a["recipients"]) == fx["digest"]
    # `heard` (the C1 hotfix's bookkeeping) is outside the digest; any other change is in it
    bare = {k: v for k, v in a["payload"].items() if k != "heard"}
    assert rules.approval_digest(a["kind"], bare, a["recipients"]) == fx["digest"]
    assert rules.approval_digest(a["kind"], bare | {"version": 3}, a["recipients"]) != fx["digest"]
    assert rules.approval_digest(a["kind"], bare, [*a["recipients"], "x@evil.example"]) != fx["digest"]


def test_a_changed_effect_after_the_challenge_is_refused(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    _, auth, private = phone
    approval_id = db.execute(
        "INSERT INTO approvals (id, kind, summary, details, payload, requires_biometric, status, created_at, "
        "expires_at) VALUES (gen_random_uuid(), 'calendar_move', 'Sposto la cena', '[]', "
        '\'{"title": "Cena", "send_updates": "none"}\', true, \'pending\', now(), '
        "now() + interval '10 minutes') RETURNING id::text"
    ).fetchone()[0]
    body = signed_approval(client, auth, private, approval_id)  # pisa-approval/2 over the effect as shown
    assert body["digest"] == shown_digest(client, auth, approval_id)
    db.execute("UPDATE approvals SET payload = payload || '{\"heard\": true}' WHERE id = %s", (approval_id,))
    db.execute(
        'UPDATE approvals SET payload = payload || \'{"send_updates": "all"}\' WHERE id = %s', (approval_id,)
    )
    changed = client.post(f"/v1/approvals/{approval_id}/resume", headers=auth, json=body)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "approval_changed"
    assert db.execute("SELECT status FROM approvals").fetchone() == ("pending",)
    ordinary = _pending(db, phone[0])  # no Face ID: no signed challenge
    refused = client.post(
        "/v1/phone/challenge",
        headers=auth,
        json={"action": "approval", "approval_id": ordinary, "interrupt_id": f"int-{ordinary}"},
    )
    assert refused.status_code == 400
    v1 = {"action": "approval", "approval_id": approval_id}  # v1's challenge, without the interrupt: gone
    assert client.post("/v1/phone/challenge", headers=auth, json=v1).status_code == 400


def test_contract_fixtures_match_the_schemas() -> None:
    import json
    from pathlib import Path

    from pisa.api import autopilot_schemas as s

    rest = Path(__file__).parents[2] / "protocol" / "fixtures" / "rest"
    approval = s.Challenge.model_validate_json((rest / "approval_challenge.json").read_text())
    shared = json.loads((rest / "k30_canonical.json").read_text())
    assert approval.canonical == shared["canonical"]
    device = s.Challenge.model_validate_json((rest / "device_challenge.json").read_text())
    assert device.canonical.startswith("pisa-device\nop=revoke_device\ntarget=")


# --- the locked phone (the server's half) ------------------------------------------


def _set_locked(client: Any, device_id: str, locked: bool) -> None:
    import uuid

    from pisa.services import presence

    runtime = client.app.state.runtime

    async def go() -> None:
        async with runtime.pool.connection() as conn:
            await presence.update(
                conn, uuid.UUID(device_id), "app_background", runtime.clock(), {"locked": locked}
            )

    client.portal.call(go)


def _pending(db: psycopg.Connection, device_id: str, biometric: bool = False) -> str:
    row = db.execute(
        "INSERT INTO approvals (id, kind, summary, details, payload, requires_biometric, status, created_at, "
        "expires_at, device_id, lang) VALUES (gen_random_uuid(), 'calendar_create', 'Cena', '[]', '{}', %s, "
        "'pending', now(), now() + interval '10 minutes', %s, 'it-IT') RETURNING id::text",
        (biometric, device_id),
    ).fetchone()
    assert row is not None
    return row[0]


def test_locked_a_spoken_yes_approves_nothing(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.errors import PisaError

    device_id, _ = pair(db, "iPhone")
    approval = _pending(db, device_id)
    _set_locked(client, device_id, True)
    approvals = client.app.state.runtime.approvals
    with pytest.raises(PisaError) as err:
        client.portal.call(lambda: approvals.decide(approval, "approve", "voice", in_conversation=True))
    assert err.value.code == "biometric_required" and err.value.message == "Sblocca il telefono per questo."
    with pytest.raises(PisaError):  # A tap on the talk's card, the same
        client.portal.call(lambda: approvals.decide(approval, "approve", "tap", talk=True))
    assert db.execute("SELECT status FROM approvals").fetchone() == ("pending",)
    # a no is always fine; and unlocked once this session (then pocketed: driving), a yes counts again
    _set_locked(client, device_id, False)
    _set_locked(client, device_id, True)
    assert client.portal.call(lambda: approvals.decide(approval, "reject", "voice", in_conversation=True))


def test_locked_the_conversation_s_tools_are_read_only() -> None:
    import asyncio
    from types import SimpleNamespace

    from pisa.brain.receipts import LockedPhone
    from pisa.domain import results
    from pisa.domain.locked import LOCKED_OK

    ran: list[str] = []

    async def handler(args: Any) -> str:
        ran.append("ran")
        return "ok"

    def call(name: str, locked: bool) -> Any:
        guard = LockedPhone(lambda: locked, lambda: "it-IT")
        part = SimpleNamespace(tool_name=name)
        return asyncio.run(guard.wrap_tool_execute(None, call=part, tool_def=None, args={}, handler=handler))  # type: ignore[arg-type]

    for name in ("add_task", "search_my_email", "list_documents", "list_bills", "about_person", "send_draft",
                 "answer_proposal", "get_briefing", "thing"):  # fmt: skip
        out = call(name, True)
        assert results.status_of(out) == results.ERROR and "Sblocca il telefono per questo." in out, name
    assert ran == []
    assert call("get_weather", True) == "ok" and call("add_task", False) == "ok"
    assert "add_task" not in LOCKED_OK and "search_my_email" not in LOCKED_OK


def test_locked_the_turn_knows_and_says_why(client, db) -> None:  # type: ignore[no-untyped-def]
    import uuid

    device_id, _ = pair(db, "iPhone")
    conversation = client.app.state.runtime.conversations
    assert client.portal.call(conversation.phone_locked, uuid.UUID(device_id)) is False
    _set_locked(client, device_id, True)
    assert client.portal.call(conversation.phone_locked, uuid.UUID(device_id)) is True
    from pisa.domain.locked import is_locked

    now = client.app.state.runtime.clock()
    state = {"locked": True, "_at": {"locked": now.isoformat()}}
    assert is_locked(state, now) and not is_locked(state, now, companion=True)  # companion: the user's choice
    assert not is_locked(state | {"unlocked_seen": True}, now)
    from pisa.domain.locked import instruction

    assert "Sblocca il telefono per questo." in instruction("it-IT") and "LOCKED" in instruction("en-US")


# --- the LOWs ---------------------------------------------------------------------------------------------


def test_no_api_map_without_a_token_in_production(secrets_key) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.settings import Settings

    def paths(env: str) -> dict[str, int]:
        unused = "postgresql://unused@127.0.0.1:1/unused"
        settings = Settings(env=env, database_url=unused, secrets_key=secrets_key)  # type: ignore[arg-type]
        app = create_app(settings, runtime_factory=lambda s: None)  # type: ignore[arg-type, return-value]
        client = TestClient(app)  # no lifespan: the routes only
        assert app.openapi()["paths"]  # CI's generator still works
        return {p: client.get(p).status_code for p in ("/openapi.json", "/docs", "/redoc")}

    assert set(paths("prod").values()) == {404}
    assert paths("dev")["/openapi.json"] == 200


def test_place_keys_and_google_credentials_need_the_signature(client, phone) -> None:  # type: ignore[no-untyped-def]
    _, auth, private = phone
    assert _refused(
        client.put("/v1/places/providers/google/key", headers=auth, json={"key": "AIza-attacker"})
    )
    from tests.test_google import CREDENTIALS as creds

    assert _refused(client.put("/v1/connectors/google/credentials", headers=auth, json=creds))
    target = digest(f"{creds['web_client_id']}\n{creds['web_client_secret']}")
    signed = device_headers(client, auth, private, "google_credentials", target)
    assert client.put("/v1/connectors/google/credentials", headers=signed, json=creds).status_code == 204


def test_a_device_target_is_one_line_and_a_device_id_lowercase() -> None:
    from pisa.services.phone_keys import canonical, device_subject

    subject = device_subject("pairing_offer", "iPad\nop=revoke_device")
    assert "\n" not in subject["target"]
    assert canonical("device", subject, "n", "e").count("\nop=") == 1
    assert device_subject("revoke_device", "6A1D0C3E-9B2F")["target"] == "6a1d0c3e-9b2f"


def test_the_digest_is_checked_again_under_the_row_lock(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.errors import PisaError

    approval = db.execute(
        "INSERT INTO approvals (id, kind, summary, details, payload, requires_biometric, status, created_at, "
        "expires_at) VALUES (gen_random_uuid(), 'calendar_move', 'Sposto', '[]', '{}', true, 'pending', "
        "now(), "
        "now() + interval '10 minutes') RETURNING id::text"
    ).fetchone()[0]
    approvals = client.app.state.runtime.approvals
    with pytest.raises(PisaError) as err:  # signed for an effect the row no longer has
        client.portal.call(
            lambda: approvals.decide(approval, "approve", "biometric", signed=True, digest2="0" * 64)
        )
    assert err.value.code == "approval_changed"
    assert db.execute("SELECT status FROM approvals").fetchone() == ("pending",)


def test_the_hints_name_the_real_commands() -> None:
    text = (Path(__file__).parents[1] / "pisa" / "services" / "phone_keys.py").read_text()
    assert "pisa phone-keys reset --keep" in text and "`pisa pair`" in text
