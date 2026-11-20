"""Pairing without a lockout of an address (CONTRACT-2 §4.3 step 5; the first security review's finding P2),
and with it:
- a failed `POST /v1/pair` names no token, so it never spends or blocks one, and no one behind the same
  address is locked out: the reviewer's proof of concept, inverted;
- failures count only toward a high address backstop (the IPv6 /64, the IPv4 address);
- a device id and a client address never meet in a log event or a stored record;
- a revoked device's offers stop holding the 5-open cap; `pisa pair --signer` on a fresh install says that it
  added no signer key; `pisa doctor` warns about pairing codes stored in plaintext."""

import io
import json
import logging
import uuid
from contextlib import redirect_stdout
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from pisa.cli_pair import answer_waiting
from pisa.domain import guide
from pisa.domain import pairing as rules
from pisa.services import endpoints, pairing
from tests.conftest import pair_v2_key
from tests.test_pairing_review_fixes import offer_and_pair
from tests.test_privacy_v2 import Phone, app_settings, make_token, on_db, send, tls  # noqa: F401 (fixtures)

USER_ADDRESS = "203.0.113.7"


def pin(client: Any) -> str:
    return endpoints.pins(client.app.state.runtime.settings)[0]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def from_address(client: Any, monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    """Requests that reach Pisa through a trusted proxy (10.0.0.1) for this client address."""
    monkeypatch.setattr(client._transport, "client", ("10.0.0.1", 50000))
    monkeypatch.setattr(client.app.state.runtime.settings, "trusted_proxies", "10.0.0.0/8")
    client.headers["X-Forwarded-For"] = address


def junk(client: Any, pairing_id: str | None = None) -> Any:
    """A pairing request no open token matches, on a begun pairing (or on `pairing_id` when given)."""
    phone = Phone("A" * 26, "x" * 44)
    if pairing_id is None:
        return send(client, phone)
    return client.post("/v1/pair", json=phone.body | {"pairingId": pairing_id})


# --- no lockout, the PoC inverted -------------------------------------------------------------------------
def test_five_junk_pairing_requests_no_longer_lock_pairing_out_for_the_address(
    client: Any,
    db: psycopg.Connection,
    secrets_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's `test_p2_five_junk_pairing_requests_lock_pairing_out_for_the_address`, inverted: a
    stranger behind the user's NAT sends 5 (and more) junk requests; the user, at the same address, still
    pairs, with the code that was open all along."""
    from_address(client, monkeypatch, USER_ADDRESS)
    open_code = make_token(db, secrets_key)
    for _ in range(8):
        refused = junk(client)
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "pairing_code_invalid"
    assert client.post("/v1/pair/begin").status_code == 201  # was 429 for 10 minutes
    assert send(client, Phone(open_code.token, pin(client))).status_code == 201  # the code was never touched
    assert on_db(db, lambda c: pairing.token_state(c, open_code.id)) == "used"  # by the user's own request


def test_a_failure_never_touches_a_token(client: Any, db: psycopg.Connection, secrets_key: str) -> None:
    made = make_token(db, secrets_key)
    for _ in range(6):
        assert junk(client).status_code == 400
        assert junk(client, "pair_unknown_but_long_enough").status_code == 400
    assert db.execute("SELECT used_at, token IS NOT NULL FROM pair_tokens").fetchall() == [(None, True)]
    assert send(client, Phone(made.token, pin(client))).status_code == 201


# --- the address backstop ---------------------------------------------------------------------------------
def test_failures_count_only_toward_a_high_backstop_per_address(
    client: Any,
    db: psycopg.Connection,
    secrets_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = pairing.MAX_ADDRESS_FAILURES
    assert limit >= 50  # "much higher than 5" (CONTRACT-2 §4.3 step 5)
    from_address(client, monkeypatch, "198.51.100.9")
    for _ in range(limit):
        assert junk(client, "pair_unknown_but_long_enough").status_code == 400
    for answer in (client.post("/v1/pair/begin"), junk(client, "pair_unknown_but_long_enough")):
        assert answer.status_code == 429 and answer.json()["error"]["code"] == "rate_limited"
    from_address(client, monkeypatch, "198.51.100.10")  # another address: untouched
    assert client.post("/v1/pair/begin").status_code == 201
    # IPv6 counts by its /64
    from_address(client, monkeypatch, "2001:db8:7:7::1")
    for i in range(limit):
        assert junk(client, "pair_unknown_but_long_enough").status_code == 400, i
    from_address(client, monkeypatch, "2001:db8:7:7:ffff::2")  # the same /64
    assert client.post("/v1/pair/begin").status_code == 429
    from_address(client, monkeypatch, "2001:db8:7:8::1")  # the next /64
    assert client.post("/v1/pair/begin").status_code == 201
    # and the codes were never touched: one made now pairs from a fresh address
    from_address(client, monkeypatch, "198.51.100.11")
    assert send(client, Phone(make_token(db, secrets_key).token, pin(client))).status_code == 201


# --- a device id never beside a client address ------------------------------------------------------------
def test_no_log_event_or_record_carries_a_device_id_and_a_client_address(
    client: Any,
    db: psycopg.Connection,
    secrets_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known device id pushes out that device's bound session nonces: the nonce, session and pairing paths
    never write one next to a client address, in a log event or a row."""
    from_address(client, monkeypatch, USER_ADDRESS)
    with capture_logs() as events:
        phone_id, phone_token, key = pair_v2_key(db, "iPhone")
        phone = Phone(rules.new_token(), "x" * 8)
        phone.key = key
        nonce = client.get("/v1/devices/session/nonce", params={"deviceId": phone_id}).json()["nonce"]
        signed = {"deviceId": phone_id, "nonce": nonce, "signature": phone.sign(phone_id, nonce)}
        assert client.post("/v1/devices/session", json=signed).status_code == 200
        assert client.get("/v1/devices/session/nonce").status_code == 200  # unbound
        new = send(client, Phone(make_token(db, secrets_key).token, pin(client))).json()
        new_id = on_db(db, lambda c: pairing.decide(c, new["pairingId"], True))
        confirmed, _, offered = offer_and_pair(client, phone_token, "Tablet")
        assert confirmed.status_code == 200
        assert junk(client).status_code == 400
    ids = {phone_id, str(new_id), offered["deviceId"], new["deviceId"]}
    assert len(events) > 3
    for event in events:
        text = json.dumps(event, default=str)
        assert not (USER_ADDRESS in text and any(i in text for i in ids)), event
    # the records: a bound nonce keeps no address; a pairing keeps one only while begun
    nonces = db.execute("SELECT device_id IS NOT NULL, address IS NOT NULL FROM session_nonces").fetchall()
    assert (True, False) in nonces and (False, True) in nonces and (True, True) not in nonces
    kept = db.execute(
        "SELECT count(*) FROM pairings WHERE state <> 'begun' AND address IS NOT NULL"
    ).fetchone()
    assert kept == (0,)
    stored = json.dumps(db.execute("SELECT to_jsonb(p) FROM pairings p").fetchall())
    assert not (USER_ADDRESS in stored and any(i in stored for i in ids))


def test_an_access_log_line_names_no_id_and_no_query() -> None:
    from pisa.logging import _NoQueryStrings

    device = str(uuid.uuid4())
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        (f"{USER_ADDRESS}:5000", "GET", f"/v1/devices/{device}/x?deviceId={device}", "1.1", 200), None,
    )  # fmt: skip
    assert _NoQueryStrings().filter(record)
    line = record.getMessage()
    assert device not in line and USER_ADDRESS in line and "/v1/devices/{id}/x" in line


# --- a revoked device's offers ----------------------------------------------------------------------------
def test_revoking_a_device_withdraws_its_offers_and_frees_the_open_cap(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    """The 5-open cap counts offers too (security review finding P5): a device holding them is ended by
    revoking it."""
    from pisa.services.devices import revoke_device

    phone_id, phone_token, _ = pair_v2_key(db, "Old phone")
    for _ in range(pairing.MAX_OPEN_TOKENS):
        assert client.post("/v1/pair/offers", json={}, headers=auth(phone_token)).status_code == 201
    with pytest.raises(Exception, match="already open"):
        make_token(db, secrets_key)  # `pisa pair` is held up
    on_db(db, lambda c: revoke_device(c, uuid.UUID(phone_id)))
    left = db.execute("SELECT count(*) FROM pair_tokens WHERE used_at IS NULL OR token IS NOT NULL")
    assert left.fetchone() == (0,)
    assert make_token(db, secrets_key).token  # free again


# --- `pisa pair --signer` on a fresh install --------------------------------------------------------------
@pytest.mark.parametrize("lang", ["en", "it"])
def test_a_signer_paired_before_any_face_id_key_is_told_its_own_device_key_signs(
    client: Any, db: psycopg.Connection, secrets_key: str, lang: str
) -> None:
    made = make_token(db, secrets_key, "CLI", signer=True)
    answer = send(client, Phone(made.token, pin(client), name="CLI", platform="cli")).json()
    (waiting,) = on_db(db, lambda c: pairing.waiting(c))
    out = io.StringIO()
    with redirect_stdout(out):
        assert on_db(db, lambda c: answer_waiting(c, waiting.sas, True, lang=lang)) == 0
    device_id = db.execute("SELECT device_id FROM pairings WHERE id = %s", (answer["pairingId"],)).fetchone()
    assert device_id is not None and db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)
    told = guide.pair_text("signer_not_added", lang, name="CLI", device_id=device_id[0])
    assert told in out.getvalue()
    # no signer key is added before a Face ID key (unchanged), and none is needed:
    # the client signs with its own device key, which the CLI names
    assert f"device-{device_id[0]}" in told and "--signer" in told


def test_a_signer_paired_beside_a_face_id_key_hears_nothing_more(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    from tests.phone_signing import register

    _, iphone_token, _ = pair_v2_key(db, "iPhone")
    register(client, auth(iphone_token), "face")
    made = make_token(db, secrets_key, "CLI", signer=True)
    send(client, Phone(made.token, pin(client), name="CLI", platform="cli"))
    (waiting,) = on_db(db, lambda c: pairing.waiting(c))
    out = io.StringIO()
    with redirect_stdout(out):
        assert on_db(db, lambda c: answer_waiting(c, waiting.sas, True, lang="en")) == 0
    assert "cannot sign device ops" not in out.getvalue()
    assert db.execute("SELECT count(*) FROM phone_keys WHERE kind = 'signer'").fetchone() == (1,)


# --- `pisa doctor` ----------------------------------------------------------------------------------------
def test_pisa_doctor_warns_when_pairing_codes_would_be_stored_in_plaintext(
    client: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pisa import cli

    settings = client.app.state.runtime.settings

    monkeypatch.setattr(cli, "configure_logging", lambda level, **_: None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings.model_copy(update={"secrets_key": None}))
    assert cli.main(["doctor"]) == 1
    err = capsys.readouterr().err
    assert (
        err.startswith("warning: ")
        and guide.doctor_text("plaintext_pairing_tokens", settings.language) in err
    )
    assert "PISA_SECRETS_KEY" in err
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["doctor"]) == 0
    assert guide.doctor_text("ok", settings.language) in capsys.readouterr().out


def test_a_token_sealed_under_another_key_is_no_match_and_a_bug_is_not() -> None:
    """`_unseal` reads "can't decrypt" (another secrets key, bytes that aren't text) as no match, and nothing
    else: a bug in the guard surfaces instead of passing as a token that didn't match."""
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore

    ours, theirs = (KeyStore(SecretStr(Fernet.generate_key().decode())) for _ in range(2))
    with capture_logs() as logs:
        assert pairing._unseal(ours, theirs.seal("tok-synthetic")) is None
        assert pairing._unseal(KeyStore(None), b"\xff\xfe") is None
    assert [e["cause"] for e in logs] == ["PisaError", "UnicodeDecodeError"]
    with pytest.raises(TypeError):
        pairing._unseal(ours, None)  # type: ignore[arg-type]
