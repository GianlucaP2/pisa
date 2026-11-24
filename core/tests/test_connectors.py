# ruff: noqa: E501 (the tests' signatures carry their fixtures and type ignores, as the suite's)
"""Every source a connector, switched
in Settings. Every word here is synthetic; every key is a fake (`sk-test-…`) or made by the test.

Each test fails on revert of the code it names (the review request's table)."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pisa.brain import tool_search
from pisa.brain.conversation import TurnContext, build_agent, system_prompt
from pisa.connectors.sources.garmin import BASE as GARMIN
from pisa.connectors.sources.garmin import PATHS
from pisa.domain import connectors as rules
from pisa.domain import effects, tool_choice
from pisa.domain import health_days as health_rules
from pisa.domain import night as night_rules
from pisa.domain import obligations as radar
from pisa.services.phone_keys import digest
from tests.conftest import pair
from tests.owner import owner_at
from tests.phone_signing import device_headers, register

GARMIN_KEY = "sk-test-garmin-0000-aaaa"
PEC_PASSWORD = "sk-test-pec-password-bbbb"


def rt(client: Any) -> Any:
    return client.app.state.runtime


@pytest.fixture
def phone(client, db) -> tuple[dict[str, str], Any]:  # type: ignore[no-untyped-def]
    """The user's iPhone, paired with a host code, its Face ID key registered: every owner action signed."""
    _, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    return auth, register(client, auth)


def signed(client: Any, auth: dict[str, str], private: Any, op: str, cid: str, body: Any) -> tuple[dict, str]:
    raw = json.dumps(body)
    headers = device_headers(client, auth, private, op, f"{cid}:{digest(raw)}")
    return headers | {"Content-Type": "application/json"}, raw


def patch(client: Any, phone: Any, cid: str, body: dict[str, Any]) -> httpx.Response:
    auth, private = phone
    headers, raw = signed(client, auth, private, "connector", cid, body)
    return client.patch(f"/v1/connectors/{cid}", headers=headers, content=raw)


def put_credential(client: Any, phone: Any, cid: str, fields: dict[str, str]) -> httpx.Response:
    auth, private = phone
    headers, raw = signed(client, auth, private, "connector_credential", cid, {"fields": fields})
    return client.put(f"/v1/connectors/{cid}/credential", headers=headers, content=raw)


def get(client: Any, phone: Any, cid: str) -> dict[str, Any]:
    return client.get(f"/v1/connectors/{cid}", headers=phone[0]).json()


def tick(client: Any) -> dict[str, int]:
    return client.portal.call(rt(client).connectors.tick)


def calls(http_handler: dict[str, Any]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    http_handler["handler"] = refuse
    return seen


# --- 1. the registry ---------------------------------------------------------------------------------------


def test_the_registry_lists_every_connector_with_its_state_and_schema(client, phone) -> None:  # type: ignore[no-untyped-def]
    listed = client.get("/v1/connectors", headers=phone[0])
    assert listed.status_code == 200
    by_id = {c["id"]: c for c in listed.json()["connectors"]}
    assert set(by_id) == {s.id for s in rules.REGISTRY}
    states = {cid: (c["state"], c["missing"]) for cid, c in by_id.items()}
    assert states["apple_health"] == ("needs_owner", ["phone_permission"])  # on, the phone's permission first
    assert states["garmin"] == ("off", ["credential"])  # off, needs their Personal API key
    assert states["bank"] == ("off", ["credential", "consent"])
    assert states["admin_mail"] == ("needs_owner", ["google_connect"])  # It reads Google's copy
    assert states["admin_documents"] == ("on", [])
    assert states["admin_calendar"] == ("on", [])
    assert states["admin_pec"] == ("needs_owner", ["credential"])  # On
    assert by_id["admin_pec"]["data_out"] is None  # their own mailbox
    assert states["ade_cassetto"] == (
        "off",
        ["credential"],
    )
    assert states["polar"] == ("off", ["provider_not_built"]) and not by_id["polar"]["built"]
    assert by_id["bank"]["data_out"]["provider"].startswith("Enable Banking")
    assert by_id["admin_calendar"]["settings_schema"]["properties"]["car"]["type"] == "boolean"
    assert by_id["google"]["family"] == "mail" and by_id["google"]["google"]["status"] == "not_connected"


def test_google_moves_into_the_registry_and_keeps_its_own_detail(client, phone) -> None:  # type: ignore[no-untyped-def]
    listed = client.get("/v1/connectors", headers=phone[0]).json()["connectors"]
    google = next(c for c in listed if c["id"] == "google")
    assert google["google"] == client.get("/v1/connectors/google", headers=phone[0]).json()
    assert google["state"] == "needs_owner" and google["missing"] == ["google_connect"]


# --- 2. signed PATCH only -----------------------------------------------------------------------------------


def _refused(response: httpx.Response) -> bool:
    return response.status_code == 403 and response.json()["error"]["code"] == "biometric_required"


@pytest.mark.parametrize("cid", ["garmin", "bank", "admin_pec", "ade_cassetto", "admin_mail", "apple_health"])
def test_a_switch_or_a_setting_needs_the_phone_s_signature_over_its_own_body(client, phone, cid) -> None:  # type: ignore[no-untyped-def]
    auth, private = phone
    body = {"enabled": True, "data_out_seen": True}
    assert _refused(client.patch(f"/v1/connectors/{cid}", headers=auth, json=body))  # unsigned
    other, _ = signed(client, auth, private, "connector", cid, {"enabled": False})
    assert _refused(client.patch(f"/v1/connectors/{cid}", headers=other, content=json.dumps(body)))
    elsewhere, _ = signed(client, auth, private, "connector", "google", body)  # another connector's
    assert _refused(client.patch(f"/v1/connectors/{cid}", headers=elsewhere, content=json.dumps(body)))
    assert patch(client, phone, cid, body).status_code == 200


def test_a_credential_and_its_removal_are_signed(client, phone) -> None:  # type: ignore[no-untyped-def]
    auth, _ = phone
    body = {"fields": {"api_key": GARMIN_KEY, "expires_on": "2027-01-31"}}
    assert _refused(client.put("/v1/connectors/garmin/credential", headers=auth, json=body))
    assert put_credential(client, phone, "garmin", body["fields"]).status_code == 200
    assert _refused(client.delete("/v1/connectors/garmin/credential", headers=auth))
    removal = device_headers(client, auth, phone[1], "connector_credential", "garmin:delete")
    assert (
        client.delete("/v1/connectors/garmin/credential", headers=removal).json()["credential"]["set"]
        is False
    )


def test_switching_on_a_connector_that_sends_data_out_shows_where_first(client, phone) -> None:  # type: ignore[no-untyped-def]
    refused = patch(client, phone, "garmin", {"enabled": True})
    assert refused.status_code == 400 and "Garmin" in refused.json()["error"]["message"]
    on = patch(client, phone, "garmin", {"enabled": True, "data_out_seen": True})
    assert on.json()["enabled"] and on.json()["data_out_seen"]


def test_a_setting_is_validated_by_its_own_schema(client, phone) -> None:  # type: ignore[no-untyped-def]
    bad = patch(client, phone, "bank", {"settings": {"consent_days": 400}})
    assert bad.status_code == 400 and "consent_days" in bad.json()["error"]["message"]
    unknown = patch(client, phone, "admin_calendar", {"settings": {"yacht": True}})
    assert unknown.status_code == 400
    ok = patch(client, phone, "admin_calendar", {"settings": {"car": True, "property": True}})
    assert ok.json()["settings"]["car"] is True and ok.json()["settings"]["isee"] is False


def test_a_provider_not_built_can_t_be_switched_on(client, phone) -> None:  # type: ignore[no-untyped-def]
    refused = patch(client, phone, "polar", {"enabled": True, "data_out_seen": True})
    assert refused.status_code == 400 and "provider" in refused.json()["error"]["message"]


# --- 3. the credential never appears in a response or a log ------------------------------------------------


def test_the_credential_never_appears_in_a_response_a_log_or_the_table(client, db, phone, capfd) -> None:  # type: ignore[no-untyped-def]
    patch(client, phone, "admin_pec", {"enabled": True, "data_out_seen": True,
                                       "settings": {"imap_host": "pec.example.invalid"}})  # fmt: skip
    made = put_credential(client, phone, "admin_pec", {"username": "owner@pec.example.invalid",
                                                       "password": PEC_PASSWORD})  # fmt: skip
    listed = client.get("/v1/connectors", headers=phone[0])
    one = client.get("/v1/connectors/admin_pec", headers=phone[0])
    for response in (made, listed, one):
        assert PEC_PASSWORD not in response.text and "owner@pec" not in response.text
    assert made.json()["credential"]["set"] is True
    stored = db.execute("SELECT secret_enc FROM connectors WHERE id = 'admin_pec'").fetchone()[0]
    assert PEC_PASSWORD.encode() not in bytes(stored)
    rt(client).connectors.mailbox = _Mailbox(fail=True)  # a provider that echoes the password back
    tick(client)
    failed = client.get("/v1/connectors/admin_pec", headers=phone[0])
    assert failed.json()["state"] == "error" and "login refused" in failed.json()["message"]
    assert PEC_PASSWORD not in failed.text  # the provider's own words, their password cut out
    out = capfd.readouterr()
    assert PEC_PASSWORD not in out.out + out.err
    assert "connector_credential_set" in out.out + out.err  # the log line is there, with ids only


# --- 4. off means off: no call, no sync, no tool, for each connector -----------------------------------------


class _Mailbox:
    def __init__(self, mails: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.calls = 0
        self.mails = mails or []
        self.fail = fail

    async def since(
        self, secret: dict[str, str], settings: dict[str, Any], day: date
    ) -> list[dict[str, Any]]:
        from pisa.connectors.sources.errors import SourceError
        from pisa.failures import redact

        self.calls += 1
        if self.fail:
            raise SourceError(redact(f"login refused for {secret['password']}", *secret.values()), auth=True)
        return self.mails


class _Tax:
    calls = 0

    async def documents(self, secret: dict[str, str], years: int) -> list[dict[str, Any]]:
        self.calls += 1
        return []


def _email(db: Any, mail_id: str, sender: str, subject: str, body: str, at: datetime) -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_address, subject, sent_at, snippet, body) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (mail_id, mail_id, sender, subject, at, body[:100], body),
    )


PAGOPA = ("Avviso di pagamento pagoPA - TARI", "Codice avviso 3020 0000 1234 5678 90. Importo € 123,45. "
          "Da pagare entro il 30/11/2026.")  # fmt: skip


def google_connected(db: Any) -> None:
    """Google connected (the radar reads its copy of his mail)."""
    db.execute("INSERT INTO connector_accounts (connector, account, status) "
               "VALUES ('google', 'owner@example.invalid', 'connected')")  # fmt: skip


def test_off_means_off_the_admin_mail(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    _email(db, "m1", "tributi@comune.milano.it", *PAGOPA, datetime(2026, 10, 20, 9, tzinfo=UTC))
    patch(client, phone, "admin_mail", {"enabled": False})
    tick(client)
    assert db.execute("SELECT count(*) FROM obligations WHERE source = 'mail'").fetchone()[0] == 0
    patch(client, phone, "admin_mail", {"enabled": True})
    tick(client)
    row = db.execute("SELECT kind, amount_text, due_on FROM obligations WHERE source = 'mail'").fetchone()
    assert row == ("pagopa_notice", "€ 123,45", date(2026, 11, 30))  # copied as printed, never computed


def test_off_means_off_the_pec(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    box = rt(client).connectors.mailbox = _Mailbox()
    put_credential(
        client, phone, "admin_pec", {"username": "owner@pec.example.invalid", "password": PEC_PASSWORD}
    )
    patch(client, phone, "admin_pec", {"enabled": False, "settings": {"imap_host": "pec.example.invalid"}})
    tick(client)
    assert box.calls == 0  # credential there, switch off: no call
    patch(client, phone, "admin_pec", {"enabled": True, "data_out_seen": True})
    tick(client)
    assert box.calls == 1


def test_off_means_off_the_documents(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 20, tzinfo=UTC)
    db.execute(
        "INSERT INTO vault_documents (id, kind, country, expires_on, reminders, provenance, created_at, updated_at) "
        "VALUES (%s, 'passport', 'IT', '2027-03-01', '[]', 'app', %s, %s)",
        (uuid.uuid4(), now, now),
    )
    patch(client, phone, "admin_documents", {"enabled": False})
    tick(client)
    assert db.execute("SELECT count(*) FROM obligations WHERE source = 'vault'").fetchone()[0] == 0
    patch(client, phone, "admin_documents", {"enabled": True})
    tick(client)
    assert db.execute("SELECT kind, due_on FROM obligations WHERE source = 'vault'").fetchone() == (
        "passport", date(2027, 3, 1))  # fmt: skip


def test_off_means_off_the_calendar(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    patch(client, phone, "admin_calendar", {"enabled": False})
    tick(client)
    assert db.execute("SELECT count(*) FROM obligations WHERE source = 'calendar'").fetchone()[0] == 0
    patch(client, phone, "admin_calendar", {"enabled": True, "settings": {"property": True}})
    tick(client)
    kinds = {r[0] for r in db.execute("SELECT kind FROM obligations WHERE source = 'calendar'").fetchall()}
    assert {"irpef_acconto", "imu_saldo"} <= kinds and "isee" not in kinds  # their list: no ISEE


def test_off_means_off_the_cassetto_fiscale(client, phone) -> None:  # type: ignore[no-untyped-def]
    tax = rt(client).connectors.tax = _Tax()
    put_credential(client, phone, "ade_cassetto", {"username": "AAAAAA00A00A000A", "password": "sk-test-x1",
                                                   "pin": "sk-test-pin"})  # fmt: skip
    tick(client)
    assert tax.calls == 0
    patch(client, phone, "ade_cassetto", {"enabled": True, "data_out_seen": True})
    tick(client)
    assert tax.calls == 1


def test_off_means_off_garmin_and_the_bank_make_no_call(client, phone, http_handler) -> None:  # type: ignore[no-untyped-def]
    seen = calls(http_handler)
    put_credential(client, phone, "garmin", {"api_key": GARMIN_KEY, "expires_on": "2027-01-31"})
    put_credential(client, phone, "bank", {"application_id": "app-test", "private_key": _pem()})
    tick(client)
    assert seen == []
    refused = client.post("/v1/connectors/bank/consent", headers=device_headers(
        client, phone[0], phone[1], "connector_consent", "bank:start"))  # fmt: skip
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connector_off" and seen == []


def test_off_means_off_apple_health_keeps_nothing(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    patch(client, phone, "apple_health", {"enabled": False})
    refused = client.post("/v1/health/summary", headers=phone[0], json={"day": "2026-10-19", "steps": 9000})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connector_off"
    assert db.execute("SELECT count(*) FROM health_days").fetchone()[0] == 0


def test_off_means_off_google_no_sync_and_no_call(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.google_sync import sync_gmail

    runtime = rt(client)
    db.execute("INSERT INTO connector_accounts (connector, account, status, refresh_token_enc) "
               "VALUES ('google', 'owner@example.invalid', 'connected', '\\x00')")  # fmt: skip
    patch(client, phone, "google", {"enabled": False})
    assert client.portal.call(sync_gmail, runtime.pool, runtime.google).ran is False
    with pytest.raises(Exception, match="switched off"):
        client.portal.call(runtime.google.access_token)


def test_off_means_no_tool_offered(client, phone) -> None:  # type: ignore[no-untyped-def]
    off = client.portal.call(rt(client).connectors.tools_off)
    assert "money_recent" in off and "health_summary" not in off and "obligations" not in off
    for cid in ("admin_mail", "admin_pec", "admin_documents", "admin_calendar"):
        patch(client, phone, cid, {"enabled": False})
    patch(client, phone, "apple_health", {"enabled": False})
    off = client.portal.call(rt(client).connectors.tools_off)
    assert {"health_summary", "money_recent", "obligations"} <= off
    patch(client, phone, "google", {"enabled": False})
    assert "search_my_email" in client.portal.call(rt(client).connectors.tools_off)
    names = _agent_tools(sources=rt(client).connectors, withheld=frozenset(off))
    assert not {"health_summary", "money_recent", "obligations"} & names
    assert {"health_summary", "money_recent", "obligations"} <= _agent_tools(sources=rt(client).connectors)


def _agent_tools(**kwargs: Any) -> set[str]:
    import asyncio

    seen: list[Any] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(info.function_tools)
        return ModelResponse(parts=[TextPart(content="ok")])

    agent = build_agent(FunctionModel(capture), TurnContext(owner=owner_at(), clock=lambda: datetime.now(UTC),
                        defer=False, **kwargs))  # fmt: skip
    asyncio.run(agent.run("x"))
    return {t.name for t in seen}


# --- 5. the state machine: needs_owner → on → error → on ------------------------------------------------------


def _garmin(fail: list[bool]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(GARMIN)
        if request.url.path == PATHS["token"]:
            if fail and fail[0]:
                return httpx.Response(401, json={"error": "invalid key"})
            return httpx.Response(200, json={"access_token": "tok-test", "expires_in": 86400})
        if request.url.path == PATHS["dailies"]:
            return httpx.Response(200, json=[{"calendarDate": "2026-10-19", "steps": 8123,
                                              "restingHeartRateInBeatsPerMinute": 55}])  # fmt: skip
        return httpx.Response(200, json=[])

    return handle


def test_the_state_machine_needs_owner_on_error_on(client, db, phone, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    fail = [False]
    http_handler["handler"] = _garmin(fail)
    on = patch(client, phone, "garmin", {"enabled": True, "data_out_seen": True}).json()
    assert (on["state"], on["missing"]) == ("needs_owner", ["credential"])
    keyed = put_credential(
        client, phone, "garmin", {"api_key": GARMIN_KEY, "expires_on": "2027-01-31"}
    ).json()
    assert keyed["state"] == "on" and keyed["credential"]["expires_on"] == "2027-01-31"
    tick(client)
    assert db.execute("SELECT steps FROM health_days WHERE source = 'garmin'").fetchone() == (8123,)
    fail[0] = True
    db.execute("UPDATE connectors SET access_enc = NULL, last_sync_at = NULL WHERE id = 'garmin'")
    tick(client)
    broken = get(client, phone, "garmin")
    assert broken["state"] == "error" and "Personal API key" in broken["message"]
    fail[0] = False
    clock.now += timedelta(hours=21)
    tick(client)
    assert get(client, phone, "garmin")["state"] == "on"
    clock.now = datetime(2027, 2, 1, 10, tzinfo=UTC)  # the key's own expiry passed
    assert get(client, phone, "garmin")["missing"] == ["credential_expired"]


# --- 6. health: derived numbers only; never a diagnosis word -------------------------------------------------


def test_the_phone_s_summary_is_kept_and_turns_the_connector_on(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    body = {"day": "2026-10-19", "sleep": {"asleep_min": 412, "bedtime": "23:40:00", "wake": "07:10:00"},
            "steps": 8450, "workouts": [{"kind": "running", "minutes": 42, "distance_m": 8100}],
            "resting_hr": 56, "hrv_sdnn_ms": 48, "vo2max": 44.1, "doses": {"taken": 2, "missed": 0}}  # fmt: skip
    made = client.post("/v1/health/summary", headers=phone[0], json=body)
    assert made.status_code == 200 and made.json()["state"] == "on"
    patch(client, phone, "apple_health", {"settings": {"medications": False}})
    client.post("/v1/health/summary", headers=phone[0], json=body)  # the same day again: replaced
    row = db.execute("SELECT steps, doses_taken, sleep_min FROM health_days").fetchall()
    assert row == [(8450, None, 412)]  # an area they switched off: nothing of it kept


@pytest.mark.parametrize(
    "extra",
    [
        {"medications": [{"name": "Farmaco Finto 10 mg", "taken": True}]},  # a medication's name
        {"samples": [{"at": "2026-10-19T08:00:00Z", "bpm": 61}]},  # raw samples
        {"menstrual_flow": "light"},  # cycle data
        {"place": "Farmacia Esempio"},  # a place
    ],
)
def test_the_summary_refuses_names_samples_cycle_data_and_places(client, db, phone, extra) -> None:  # type: ignore[no-untyped-def]
    refused = client.post("/v1/health/summary", headers=phone[0], json={"day": "2026-10-19", **extra})
    assert refused.status_code == 400
    named = {"day": "2026-10-19", "doses": {"taken": 1, "missed": 0, "name": "Farmaco Finto"}}
    assert client.post("/v1/health/summary", headers=phone[0], json=named).status_code == 400
    assert db.execute("SELECT count(*) FROM health_days").fetchone()[0] == 0


def _day(d: date, **kw: Any) -> health_rules.Day:
    base: dict[str, Any] = {"sleep_min": None, "bedtime": None, "wake_time": None, "steps": None,
                            "exercise_min": None, "resting_hr": None, "hrv_sdnn_ms": None, "vo2max": None,
                            "workouts": [], "doses_taken": None, "doses_missed": None}  # fmt: skip
    return health_rules.Day(d, "apple_health", **(base | kw))


@pytest.mark.parametrize("lang", ["it-IT", "en-US"])
def test_no_diagnosis_word_in_anything_pisa_says_about_his_body(lang: str) -> None:
    from datetime import time

    extreme = [_day(date(2026, 10, 19), sleep_min=95, bedtime=time(4, 50), wake_time=time(6, 25), steps=50,
                    exercise_min=0, resting_hr=118, hrv_sdnn_ms=6, vo2max=18.0, doses_taken=0, doses_missed=4,
                    workouts=[{"kind": "running", "minutes": 300, "distance_m": 42000}])]  # fmt: skip
    earlier = [
        _day(date(2026, 10, d), sleep_min=480, steps=9000, resting_hr=55, hrv_sdnn_ms=60)
        for d in range(1, 18)
    ]
    lines = health_rules.describe(extreme, earlier, lang)
    assert lines and all(health_rules.clean(line) for line in lines), lines
    from pisa.brain.source_tools import HEALTH_RULE

    tool_doc = _agent_definitions()["health_summary"]
    for word in health_rules.FORBIDDEN:
        assert word not in tool_doc.lower(), word
    assert "doctor" in HEALTH_RULE  # Pisa suggests their doctor; it never names a condition itself
    assert not health_rules.clean("possibile apnea notturna") and not health_rules.clean("abnormal rhythm")


def _agent_definitions() -> dict[str, str]:
    import asyncio
    from unittest.mock import MagicMock

    seen: list[Any] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(info.function_tools)
        return ModelResponse(parts=[TextPart(content="ok")])

    agent = build_agent(FunctionModel(capture), TurnContext(owner=owner_at(), clock=lambda: datetime.now(UTC),
                        defer=False, sources=MagicMock()))  # fmt: skip
    asyncio.run(agent.run("x"))
    return {t.name: t.description or "" for t in seen}


# --- 7. the admin radar: SEND's clock, phishing flagged ------------------------------------------------------


def test_a_send_notice_starts_its_legal_clock() -> None:
    mail = radar.Mail("pec:1", "noreply@pec.notifichedigitali.it", "Notifica digitale a valore legale",
                      "Hai ricevuto una notifica su SEND.", datetime(2026, 11, 2, 10, tzinfo=UTC))  # fmt: skip
    o = radar.classify(mail, "pec", {})
    assert o is not None and o.kind == "send_notice"
    assert o.due_on == date(2026, 11, 9)  # made available + 7 days: delivered by law, opened or not
    assert o.remind_on == date(2026, 11, 2)  # the clock runs: the reminder at once
    assert radar.send_clock(datetime(2026, 12, 28, 23, tzinfo=UTC)) == date(2027, 1, 4)


def test_a_send_notice_in_his_mail_becomes_a_task_at_once(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    _email(db, "s1", "noreply@notifichedigitali.it", "Notifica digitale a valore legale",
           "Una notifica SEND è disponibile per te.", datetime(2026, 10, 20, 8, tzinfo=UTC))  # fmt: skip
    tick(client)
    row = db.execute("SELECT o.due_on, t.title FROM obligations o JOIN tasks t ON t.id = o.task_id "
                     "WHERE o.kind = 'send_notice'").fetchone()  # fmt: skip
    assert row is not None and row[0] == date(2026, 10, 27)
    assert "SEND" in row[1] and "notifichedigitali" not in row[1]  # code's own words


@pytest.mark.parametrize(
    ("sender", "subject", "body"),
    [
        (
            "rinnovi@tessera-sanitaria-online.example",
            "La tua tessera sanitaria è scaduta",
            "Rinnova subito la tessera sanitaria cliccando qui: https://tessera-rinnovo.example/login",
        ),
        (
            "servizio@agenzia-entrate-rimborsi.example",
            "Agenzia delle Entrate: rimborso in scadenza",
            "Gentile contribuente, il tuo rimborso scade il 30/11/2026: https://rimborso-ade.example",
        ),
        (
            "avvisi@pagopa-pagamenti.example",
            "Avviso di pagamento pagoPA",
            "Codice avviso 3020 0000 1234 5678 90, importo € 89,00 entro il 25/11/2026: https://paga.example",
        ),
    ],
)
def test_a_phishing_renewal_is_flagged_not_tracked(client, db, sender, subject, body) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    _email(db, "p1", sender, subject, body, datetime(2026, 10, 20, 8, tzinfo=UTC))
    tick(client)
    row = db.execute("SELECT state, due_on, amount_text, task_id, flag_reason FROM obligations "
                     "WHERE source_ref = 'gmail:p1'").fetchone()  # fmt: skip
    assert row[:4] == ("flagged", None, None, None) and row[4]
    listed = client.portal.call(rt(client).connectors.obligations, 90, "it-IT")
    assert listed["flagged"] == 1 and all(i["source"] != "mail" for i in listed["items"])
    assert db.execute("SELECT count(*) FROM tasks WHERE source_ref LIKE 'obligation:%%' "
                      "AND title ILIKE '%%tessera%%'").fetchone()[0] == 0  # fmt: skip


def test_an_official_notice_from_a_comune_is_not_flagged() -> None:
    mail = radar.Mail("m", "tributi@comune.milano.it", *PAGOPA, datetime(2026, 10, 20, tzinfo=UTC))
    o = radar.classify(mail, "mail", {})
    assert o is not None and o.state == "upcoming" and o.kind == "pagopa_notice"


# --- 8. PSD2: the consent's expiry and its reminder ----------------------------------------------------------


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()  # fmt: skip


def _bank(until: datetime) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path == "/auth":
            return httpx.Response(
                200, json={"url": "https://bank.example.invalid/consent", "authorization_id": "a1"}
            )
        if request.url.path == "/sessions":
            return httpx.Response(200, json={
                "session_id": "sess-test", "access": {"valid_until": until.isoformat()},
                "accounts": [{"uid": "acc-1", "name": "Conto Esempio", "currency": "EUR",
                              "account_id": {"iban": "IT76L9999999999000000001234"}}],
            })  # fmt: skip
        if request.url.path == "/accounts/acc-1/transactions":
            return httpx.Response(200, json={"transactions": [
                {"entry_reference": "t1", "transaction_amount": {"amount": "12.50", "currency": "EUR"},
                 "credit_debit_indicator": "DBIT", "booking_date": "2026-10-19", "status": "BOOK",
                 "creditor": {"name": "Bar Esempio"}},
            ]})  # fmt: skip
        return httpx.Response(404)

    return handle


def test_psd2_consent_expiry_and_its_reminder(client, db, phone, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    until = datetime(2026, 11, 19, 11, tzinfo=UTC)
    http_handler["handler"] = _bank(until)
    patch(client, phone, "bank", {"enabled": True, "data_out_seen": True, "settings": {
        "aspsp_name": "Banca Esempio", "redirect_url": "https://pisa.example.invalid/bank"}})  # fmt: skip
    put_credential(client, phone, "bank", {"application_id": "app-test", "private_key": _pem()})
    start = client.post("/v1/connectors/bank/consent", headers=device_headers(
        client, phone[0], phone[1], "connector_consent", "bank:start")).json()  # fmt: skip
    assert start["url"] == "https://bank.example.invalid/consent"
    code = "code-test-123"
    given = client.post("/v1/connectors/bank/consent/code", json={"code": code, "state": start["state"]},
                        headers=device_headers(client, phone[0], phone[1], "connector_consent",
                                               f"bank:{digest(code)}"))  # fmt: skip
    assert given.json()["state"] == "on" and given.json()["consent"]["expires_at"].startswith("2026-11-19")
    assert db.execute("SELECT iban_last4 FROM bank_accounts").fetchone() == ("1234",)

    def reminders() -> int:
        return db.execute("SELECT count(*) FROM tasks WHERE source_ref IN (SELECT 'obligation:' || id FROM "
                          "obligations WHERE kind = 'psd2_consent')").fetchone()[0]  # fmt: skip

    clock.now = until - timedelta(days=8)
    tick(client)
    assert reminders() == 0
    assert db.execute("SELECT amount, direction FROM bank_transactions").fetchone() == (12.5, "out")
    clock.now = until - timedelta(days=7)
    tick(client)
    assert reminders() == 1
    tick(client)
    assert reminders() == 1  # once
    assert get(client, phone, "bank")["consent"]["renew_from"].startswith("2026-11-12")
    clock.now = until + timedelta(hours=1)
    gone = get(client, phone, "bank")
    assert (gone["state"], gone["missing"]) == ("needs_owner", ["consent_expired"])
    seen = calls(http_handler)
    tick(client)
    assert seen == []  # an expired consent: no call


# --- 9. what the brain gets: search_tools, the night, nothing in the prompt ----------------------------------


def test_the_three_tools_are_found_by_search_never_offered_at_once_and_never_in_the_prompt() -> None:
    names = {"health_summary", "money_recent", "obligations"}
    assert not names & (tool_search.ALWAYS | tool_search.VOICE_ALWAYS)
    assert names <= effects.READ
    for name in names:
        assert name not in system_prompt(lang="it-IT")
    assert tool_choice.tools_for("che tempo fa domani?") & names == frozenset()
    assert "health_summary" in tool_choice.tools_for("come ho dormito stanotte?")
    assert "money_recent" in tool_choice.tools_for("quanto ho speso questa settimana?")
    assert "obligations" in tool_choice.tools_for("quando scade il bollo?")


def test_the_night_reads_the_same(client, db) -> None:  # type: ignore[no-untyped-def]
    assert "n10_sources" in night_rules.STEPS and "n10_sources" in rt(client).night.steps()
    db.execute("INSERT INTO health_days (day, source, steps, received_at) VALUES ('2026-10-20', "
               "'apple_health', 5000, now())")  # fmt: skip
    assert client.portal.call(rt(client).night.steps()["n10_sources"], date(2026, 10, 20)) >= 1


def test_the_tools_read_what_the_connectors_keep(client, db) -> None:  # type: ignore[no-untyped-def]
    db.execute("INSERT INTO health_days (day, source, steps, sleep_min, received_at) VALUES "
               "('2026-10-19', 'apple_health', 7000, 420, now()), ('2026-10-10', 'apple_health', 10000, 480, now())")  # fmt: skip
    got = client.portal.call(rt(client).connectors.health_summary, 3, "it-IT")
    assert got["days"] == 1 and "passi 7000" in got["lines"][0] and "sotto la tua media" in got["lines"][0]


def test_the_connectors_words_are_english_with_italian_through_the_language_table() -> None:
    bank = rules.BY_ID["bank"]
    assert rules.say(bank.name, "en-US") == "Bank (read-only)"
    assert rules.say(bank.name, "it-IT") == "Banca (sola lettura)"
    assert rules.schema(bank, "en")["properties"]["consent_days"]["title"] == "Consent duration (days)"
    assert rules.schema(bank, "it")["properties"]["consent_days"]["title"] == "Durata del consenso (giorni)"
    # every user-visible string has its Italian: a name, a field's title and description, a credential, data_out
    shown: set[str] = set()
    for s in rules.REGISTRY:
        shown.add(s.name)
        shown.update(f.title for f in s.credential)
        if s.data_out:
            shown.update((s.data_out.provider, s.data_out.what, s.data_out.where))
        for prop in s.settings.model_json_schema().get("properties", {}).values():
            shown.update(prop[k] for k in ("title", "description") if k in prop)
    names = {"Apple Health", "Garmin", "Polar", "Withings", "ISEE", "Google (Gmail + Calendar)"}
    names |= {"Agenzia delle Entrate", "Garmin Connect", "Polar Flow", "agenziaentrate.gov.it", "IMU"}
    names |= {"Password Fisconline", "PIN Fisconline"}  # the same words in both languages
    assert shown - set(rules.WORDS["it"]) <= names, shown - set(rules.WORDS["it"])
