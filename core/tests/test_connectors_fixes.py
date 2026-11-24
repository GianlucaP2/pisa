# ruff: noqa: E501 (the tests' signatures carry their fixtures and type ignores, as the suite's)
"""The review (H2, M1-M7, LOW). Every word synthetic; each test fails on
revert of its fix (`tests/mutate_nb1_connectors.py`, rows "r2")."""

import email
import email.policy
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest

from pisa.connectors.sources import pec
from pisa.domain import connectors as rules
from pisa.domain import obligations as radar
from pisa.domain import tool_choice
from tests.conftest import dsn, pair
from tests.phone_signing import device_headers, register
from tests.test_connectors import (
    PAGOPA,
    _bank,
    _email,
    _pem,
    get,
    google_connected,
    patch,
    put_credential,
    rt,
    tick,
)

NOW = datetime(2026, 10, 20, 11, tzinfo=UTC)


@pytest.fixture
def phone(client, db) -> tuple[dict[str, str], Any]:  # type: ignore[no-untyped-def]
    """The user's iPhone, host-paired, its Face ID key registered: every owner action signed."""
    _, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    return auth, register(client, auth)


def _vault(db: Any, kind: str, expires: str = "2027-03-01") -> None:
    db.execute(
        "INSERT INTO vault_documents (id, kind, country, expires_on, reminders, provenance, created_at, "
        "updated_at) VALUES (%s, %s, 'IT', %s, '[]', 'app', %s, %s)",
        (uuid.uuid4(), kind, expires, NOW, NOW),
    )


# --- H2: an amount never crosses a line; one message never stops the radar ------------------------------


def test_an_amount_is_copied_on_its_own_line_and_a_no_break_space_is_a_space() -> None:
    assert radar.amount_as_printed("Importo: € 85\n100 rate da pagare") == "€ 85"
    assert radar.amount_as_printed("Totale € 1\u00a0234,56 entro il 30/11/2026") == "€ 1 234,56"
    assert radar.amount_as_printed("Totale € 1.234,56") == "€ 1.234,56"


def test_a_message_that_can_t_be_kept_never_stops_the_radar(client, db, monkeypatch, capfd) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    _vault(db, "passport")
    _email(db, "bad", "tributi@comune.milano.it", *PAGOPA, NOW - timedelta(hours=2))
    _email(db, "good", "tributi@comune.milano.it", *PAGOPA, NOW - timedelta(hours=1))
    real = radar.classify

    def broken(mail: radar.Mail, source: Any, wanted: Any) -> Any:
        o = real(mail, source, wanted)
        if mail.ref == "gmail:bad" and o is not None:  # an amount the table's CHECK refuses
            return radar.Obligation(**{**o.__dict__, "amount_text": "€ 85\n100"})
        return o

    monkeypatch.setattr(radar, "classify", broken)
    tick(client)
    refs = {r[0] for r in db.execute("SELECT source_ref FROM obligations").fetchall()}
    assert "gmail:good" in refs and "gmail:bad" not in refs
    assert db.execute("SELECT count(*) FROM obligations WHERE source = 'vault'").fetchone()[0] == 1
    unread = db.execute("SELECT count(*) FROM email_messages WHERE radar_read_at IS NULL").fetchone()[0]
    assert unread == 0  # the bad one is past: never retried every minute
    out = capfd.readouterr()
    assert "85\n100" not in out.out + out.err and "Failing row" not in out.out + out.err


def test_a_connector_s_unexpected_error_is_counted_and_the_others_still_run(
    client, db, phone, clock, http_handler, capfd
) -> None:  # type: ignore[no-untyped-def]
    until = NOW + timedelta(days=60)
    handle = _bank(until)

    def odd(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):  # a booking date Pisa can't read
            return httpx.Response(200, json={"transactions": [
                {"entry_reference": "x1", "transaction_amount": {"amount": "1.00", "currency": "EUR"},
                 "credit_debit_indicator": "DBIT", "booking_date": "2026-13-45"}]})  # fmt: skip
        return handle(request)

    http_handler["handler"] = odd
    _consent(client, phone)
    _vault(db, "cie")
    tick(client)
    bank = get(client, phone, "bank")
    assert db.execute("SELECT failures FROM connectors WHERE id = 'bank'").fetchone()[0] == 1
    assert "ValueError" in (
        db.execute("SELECT message FROM connectors WHERE id = 'bank'").fetchone()[0] or ""
    )
    assert bank["state"] == "on"  # one failure: not yet `error`
    assert db.execute("SELECT count(*) FROM obligations WHERE source = 'vault'").fetchone()[0] == 1
    out = capfd.readouterr()
    assert "2026-13-45" not in out.out + out.err


# --- M1: off means off on the read side -------------------------------------------------------------------


def test_money_recent_reads_only_banks_that_are_on(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO bank_transactions (id, connector_id, account_uid, provider_ref, booked_on, amount, currency, "
        "direction, seen_at) VALUES (gen_random_uuid(), 'bank', 'a', 't1', '2026-10-19', 9.5, 'EUR', 'out', now())"
    )
    assert client.portal.call(rt(client).connectors.money_recent, 7, "it-IT")["totals"] == []
    patch(client, phone, "bank", {"enabled": True, "data_out_seen": True})
    assert client.portal.call(rt(client).connectors.money_recent, 7, "it-IT")["totals"]


def test_health_summary_reads_only_sources_that_are_on(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO health_days (day, source, steps, received_at) VALUES ('2026-10-20', 'garmin', 4321, now())"
    )
    got = client.portal.call(rt(client).connectors.health_summary, 1, "it-IT")
    assert got["lines"] == [] and "garmin" not in got["sources"]
    patch(client, phone, "garmin", {"enabled": True, "data_out_seen": True})
    assert "4321" in client.portal.call(rt(client).connectors.health_summary, 1, "it-IT")["lines"][0]


def test_obligations_read_only_sources_that_are_on(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    db.execute(
        "INSERT INTO obligations (id, kind, what, due_on, source, source_ref, state, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'pagopa_notice', 'Avviso', '2026-11-01', 'mail', 'gmail:x', 'upcoming', now(), now())"
    )
    assert len(client.portal.call(rt(client).connectors.obligations, 30, "it-IT")["items"]) == 1
    patch(client, phone, "admin_mail", {"enabled": False})
    assert client.portal.call(rt(client).connectors.obligations, 30, "it-IT")["items"] == []


def test_off_means_off_google_s_calendar_sync(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.google_sync import sync_calendar

    runtime = rt(client)
    db.execute("INSERT INTO connector_accounts (connector, account, status, refresh_token_enc) "
               "VALUES ('google', 'owner@example.invalid', 'connected', '\\x00')")  # fmt: skip
    patch(client, phone, "google", {"enabled": False})
    assert (
        client.portal.call(lambda: sync_calendar(runtime.pool, runtime.google, timezone="Europe/Rome")).ran
        is False
    )


def test_a_consent_code_is_refused_while_the_bank_is_off(client, phone, http_handler) -> None:  # type: ignore[no-untyped-def]
    seen: list[Any] = []
    http_handler["handler"] = lambda r: seen.append(r) or httpx.Response(500)
    code = "code-test-off"
    from pisa.services.phone_keys import digest

    refused = client.post("/v1/connectors/bank/consent/code", json={"code": code, "state": "s"},
                          headers=device_headers(client, phone[0], phone[1], "connector_consent",
                                                 f"bank:{digest(code)}"))  # fmt: skip
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connector_off" and seen == []


def test_medications_are_off_by_default(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    assert rules.HealthSettings().medications is False
    client.post("/v1/health/summary", headers=phone[0],
                json={"day": "2026-10-19", "steps": 100, "doses": {"taken": 1, "missed": 1}})  # fmt: skip
    assert db.execute("SELECT doses_taken, doses_missed FROM health_days").fetchone() == (None, None)


# --- M2: late-delivered mail is still read ------------------------------------------------------------------


def test_an_email_fetched_late_with_an_earlier_date_is_still_classified(client, db) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    _email(db, "later", "tributi@comune.milano.it", *PAGOPA, NOW.replace(hour=10, minute=10))
    tick(client)
    _email(db, "late", "tributi@comune.milano.it", *PAGOPA, NOW.replace(hour=10, minute=0))  # fetched after
    tick(client)
    refs = {r[0] for r in db.execute("SELECT source_ref FROM obligations WHERE source = 'mail'").fetchall()}
    assert refs == {"gmail:later", "gmail:late"}


# --- M3: the readers decide the state now; the replay's world writes Pisa's own state --------------------------


def test_the_readers_never_trust_a_stale_stored_state(client, db) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO obligations (id, kind, what, due_on, source, source_ref, state, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'irpef_acconto', 'IRPEF', '2026-11-01', 'calendar', 'irpef_acconto:x', "
        "'upcoming', now(), now())"
    )
    db.execute("UPDATE connectors SET state = 'needs_owner' WHERE id = 'admin_calendar'")  # stale
    assert len(client.portal.call(rt(client).connectors.obligations, 30, "it-IT")["items"]) == 1


def test_the_replay_s_world_writes_the_state_pisa_decides_and_the_tool_returns_items(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.replay import harness
    from tests.replay.scenarios import SCENARIOS

    scenario = next(s for s in SCENARIOS if s["id"] == "nb1-03-what-is-due-this-month")

    async def seed() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await conn.execute("DELETE FROM connectors")
            await harness.connectors_world(conn, scenario["world"], harness.local(scenario["now"]))

    client.portal.call(seed)
    assert db.execute("SELECT state FROM connectors WHERE id = 'admin_calendar'").fetchone() == ("on",)
    rt(client).connectors._clock = lambda: harness.local(scenario["now"])
    got = client.portal.call(rt(client).connectors.obligations, 30, "it-IT")
    assert [i["kind"] for i in got["items"]] == ["irpef_acconto"]


# --- M4: a PEC envelope is read through to its original --------------------------------------------------


def _envelope(inner_from: str) -> bytes:
    inner = (
        f"From: {inner_from}\r\nSubject: Comunicazione esito controllo\r\nDate: Mon, 19 Oct 2026 09:00:00 +0200\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\nGentile contribuente, l'Agenzia delle Entrate comunica "
        "l'esito del controllo. Scadenza: 30/11/2026.\r\n"
    )
    return (
        'From: "Per conto di: ' + inner_from + '" <posta-certificata@gestore-esempio.example>\r\n'
        "Subject: POSTA CERTIFICATA: Comunicazione esito controllo\r\nDate: Mon, 19 Oct 2026 09:01:00 +0200\r\n"
        "Message-ID: <env-1@gestore-esempio.example>\r\nX-Trasporto: posta-certificata\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nMessaggio di posta certificata: il giorno 19/10/2026 "
        "il messaggio è stato inviato.\r\n"
        '--B\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="postacert.eml"\r\n\r\n'
        + inner
        + "\r\n--B--\r\n"
    ).encode()


def test_a_pec_envelope_is_read_through_to_its_original() -> None:
    message = email.message_from_bytes(
        _envelope("avvisi@pec.agenziaentrate.it"), policy=email.policy.compat32
    )
    sender, subject, text = pec.unwrap(message)
    assert sender == "avvisi@pec.agenziaentrate.it" and subject == "Comunicazione esito controllo"
    assert "esito del controllo" in text
    o = radar.classify(radar.Mail("pec:1", sender, subject, text, NOW), "pec", {})
    assert o is not None and o.state != "flagged" and o.kind == "ade_inps" and o.due_on == date(2026, 11, 30)


def test_without_the_attachment_per_conto_di_names_the_sender() -> None:
    message = email.message_from_bytes(
        b'From: "Per conto di: avvisi@pec.agenziaentrate.it" <posta-certificata@gestore-esempio.example>\r\n'
        b"X-Trasporto: posta-certificata\r\n"
        b"Subject: Avviso\r\nContent-Type: text/plain\r\n\r\nTesto.\r\n",
        policy=email.policy.compat32,
    )
    assert pec.unwrap(message)[0] == "avvisi@pec.agenziaentrate.it"


# --- M5: only real public-administration domains -------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "ok"),
    [
        ("comune.roma.it", True), ("comune.san-miniato.pi.it", False), ("pec.comune.milano.it", True),
        ("regione.toscana.it", True), ("agenziaentrate.gov.it", True), ("comune.pagamenti-tari.it", False),
        ("regione.pagamenti.it", False), ("comune.it", False), ("comune.roma.it.example.com", False),
    ],
)  # fmt: skip
def test_only_real_public_bodies_are_official(domain: str, ok: bool) -> None:
    assert radar.official(domain) is ok


def test_a_notice_from_an_open_registration_comune_domain_is_not_tracked() -> None:
    mail = radar.Mail("m", "tributi@comune.pagamenti-tari.it", *PAGOPA, NOW)
    o = radar.classify(mail, "mail", {})
    assert o is not None and o.state == "flagged"


# --- M7: the bank's movements are cleaned ---------------------------------------------------------------


def _consent(client: Any, phone: Any) -> None:
    patch(client, phone, "bank", {"enabled": True, "data_out_seen": True, "settings": {
        "aspsp_name": "Banca Esempio", "redirect_url": "https://pisa.example.invalid/bank"}})  # fmt: skip
    put_credential(client, phone, "bank", {"application_id": "app-test", "private_key": _pem()})
    start = client.post("/v1/connectors/bank/consent", headers=device_headers(
        client, phone[0], phone[1], "connector_consent", "bank:start")).json()  # fmt: skip
    from pisa.services.phone_keys import digest

    client.post("/v1/connectors/bank/consent/code", json={"code": "c1", "state": start["state"]},
                headers=device_headers(client, phone[0], phone[1], "connector_consent", f"bank:{digest('c1')}"))  # fmt: skip


def test_a_pending_movement_the_bank_no_longer_returns_goes(client, db, phone, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    pending = {"entry_reference": "p1", "transaction_amount": {"amount": "20.00", "currency": "EUR"},
               "credit_debit_indicator": "DBIT", "booking_date": "2026-10-19", "status": "PDNG"}  # fmt: skip
    booked = {**pending, "entry_reference": "b1", "status": "BOOK"}
    returned = [pending]
    base = _bank(NOW + timedelta(days=60))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):
            return httpx.Response(200, json={"transactions": returned})
        return base(request)

    http_handler["handler"] = handle
    _consent(client, phone)
    tick(client)
    assert db.execute("SELECT provider_ref FROM bank_transactions").fetchall() == [("p1",)]
    returned[:] = [booked]  # booked under another id
    clock.now += timedelta(hours=7)
    tick(client)
    assert db.execute("SELECT provider_ref, pending FROM bank_transactions").fetchall() == [("b1", False)]
    assert client.portal.call(rt(client).connectors.money_recent, 7, "it-IT")["totals"][0]["count"] == 1


# --- LOW --------------------------------------------------------------------------------------------------


def test_a_renewed_consent_closes_the_older_consent_s_line(client, db, phone, http_handler) -> None:  # type: ignore[no-untyped-def]
    http_handler["handler"] = _bank(NOW + timedelta(days=30))
    _consent(client, phone)
    tick(client)
    db.execute(
        "UPDATE connectors SET consent_expires_at = %s WHERE id = 'bank'", (NOW + timedelta(days=120),)
    )
    tick(client)
    lines = db.execute("SELECT state FROM obligations WHERE kind = 'psd2_consent' ORDER BY due_on").fetchall()
    assert lines == [("done",), ("upcoming",)]


def test_a_missing_car_date_closes_when_the_vault_has_it_or_there_is_no_car(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    patch(client, phone, "admin_calendar", {"settings": {"car": True}})
    tick(client)
    missing = "SELECT subject FROM obligations WHERE kind = 'missing_document' AND state <> 'done' ORDER BY 1"
    assert [r[0] for r in db.execute(missing).fetchall()] == ["car_insurance", "car_revision", "car_tax"]
    _vault(db, "car_tax")
    db.execute("UPDATE connectors SET last_sync_at = NULL WHERE id = 'admin_calendar'")
    tick(client)
    assert [r[0] for r in db.execute(missing).fetchall()] == ["car_insurance", "car_revision"]
    patch(client, phone, "admin_calendar", {"settings": {"car": False}})
    assert not any(i["kind"] == "missing_document" for i in
                   client.portal.call(rt(client).connectors.obligations, 400, "it-IT")["items"])  # fmt: skip
    db.execute("UPDATE connectors SET last_sync_at = NULL WHERE id = 'admin_calendar'")
    tick(client)
    assert db.execute(missing).fetchall() == []


@pytest.mark.parametrize(
    ("words", "tool"),
    [
        ("come ho dormito stanotte?", "health_summary"), ("quanti passi ho fatto ieri?", "health_summary"),
        ("quanto ho speso questa settimana?", "money_recent"), ("movimenti del conto", "money_recent"),
        ("quando scade il bollo?", "obligations"), ("scadenze fiscali di dicembre", "obligations"),
        ("ho scadenze questo mese?", "obligations"), ("che scadenze ho oggi?", "obligations"),
        ("le mie scadenze della settimana", "obligations"),
    ],
)  # fmt: skip
def test_his_words_offer_the_tool_at_once(words: str, tool: str) -> None:
    assert tool in tool_choice.tools_for(words)


@pytest.mark.parametrize(
    "words",
    ["ho sonno", "revisione del contratto", "movimenti di stretching", "bonifico da fare a Marco"],
)  # fmt: skip
def test_words_that_aren_t_about_them_offer_nothing(words: str) -> None:
    assert not tool_choice.tools_for(words) & {"health_summary", "money_recent", "obligations"}


def test_the_mail_radar_follows_google(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    _email(db, "g1", "tributi@comune.milano.it", *PAGOPA, NOW - timedelta(hours=1))
    tick(client)  # Google never connected
    assert get(client, phone, "admin_mail")["missing"] == ["google_connect"]
    google_connected(db)
    patch(client, phone, "google", {"enabled": False})
    assert get(client, phone, "admin_mail")["missing"] == ["google_off"]
    tick(client)
    assert db.execute("SELECT count(*) FROM obligations WHERE source = 'mail'").fetchone()[0] == 0
    patch(client, phone, "google", {"enabled": True})
    tick(client)
    assert db.execute("SELECT count(*) FROM obligations WHERE source = 'mail'").fetchone()[0] == 1


def test_flagged_respects_the_source_switches(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    db.execute(
        "INSERT INTO obligations (id, kind, what, source, source_ref, state, flag_reason, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'suspect', 'Sospetto', 'mail', 'gmail:f', 'flagged', 'x', now(), now())"
    )
    assert len(client.portal.call(rt(client).connectors.flagged)) == 1
    patch(client, phone, "admin_mail", {"enabled": False})
    assert client.portal.call(rt(client).connectors.flagged) == []


def test_the_registry_s_shape_at_merge() -> None:
    assert rules.BY_ID["admin_pec"].default_enabled and rules.BY_ID["admin_pec"].data_out is None
    assert not rules.BY_ID["ade_cassetto"].default_enabled


# --- The merge (the review's fixes) ---------------------------------------------------------------

PAGOPA_LINK = (PAGOPA[0], PAGOPA[1] + " Paga qui: https://pagopa-avvisi.example.com/paga")


def test_a_forged_per_conto_di_in_ordinary_mail_stays_flagged() -> None:
    """Review N1: ordinary mail writing "Per conto di: <an office>" in its own display name was read as the
    office and its pagoPA notice tracked. Only the provider's certified envelope is read through."""
    raw = (
        'From: "Per conto di: avvisi@pec.agenziaentrate.it" <x@evil.example>\r\n'
        f"Subject: {PAGOPA_LINK[0]}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{PAGOPA_LINK[1]}\r\n"
    ).encode()
    sender, subject, text = pec.unwrap(email.message_from_bytes(raw, policy=email.policy.compat32))
    assert radar.domain_of(sender) == "evil.example"
    o = radar.classify(radar.Mail("pec:f", sender, subject, text, NOW), "pec", {})
    assert o is not None and o.state == "flagged"


def test_an_anomaly_envelope_is_never_read_through() -> None:
    """An anomaly envelope (`X-Trasporto: errore`) wraps ordinary mail: its inner sender is nobody's word."""
    raw = _envelope("avvisi@pec.agenziaentrate.it").replace(
        b"X-Trasporto: posta-certificata", b"X-Trasporto: errore"
    )
    sender, _, _ = pec.unwrap(email.message_from_bytes(raw, policy=email.policy.compat32))
    assert radar.domain_of(sender) == "gestore-esempio.example"


class _FakeImap:
    """An IMAP server holding the given messages: read-only, as `pec.read` uses it."""

    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def select(self, box: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly
        return "OK", [b""]

    def search(self, charset: Any, *criteria: str) -> tuple[str, list[bytes]]:
        return "OK", [b" ".join(str(n + 1).encode() for n in range(len(self.messages)))]

    def fetch(self, num: bytes, parts: str) -> tuple[str, list[Any]]:
        assert "PEEK" in parts
        return "OK", [(b"1", self.messages[int(num) - 1])]

    def logout(self) -> None:
        return None


def test_the_pec_reader_reads_a_certified_envelope_through_and_a_forged_one_as_it_is() -> None:
    """Review N7: only `unwrap` was tested; the reader's own call is guarded here."""
    forged = (
        b'From: "Per conto di: avvisi@pec.agenziaentrate.it" <x@evil.example>\r\nSubject: Avviso\r\n'
        b"Message-ID: <f@evil.example>\r\nContent-Type: text/plain\r\n\r\nTesto.\r\n"
    )
    imap = _FakeImap([_envelope("avvisi@pec.agenziaentrate.it"), forged])
    got = pec.read(lambda host, port: imap, {"username": "u", "password": "p"}, {"imap_host": "imap.example"},
                   date(2026, 10, 1))  # fmt: skip
    assert [g["from_address"] for g in got] == [
        "avvisi@pec.agenziaentrate.it",
        '"Per conto di: avvisi@pec.agenziaentrate.it" <x@evil.example>',
    ]
    assert got[0]["subject"] == "Comunicazione esito controllo"


def test_a_comune_under_a_province_code_only_from_a_certified_sender() -> None:
    """Review N2: `comune.<x>.<prov>.it` can be registered by anyone; only a PEC (the provider vouches for its
    sender) from it is official. Ordinary mail from it claiming an office is flagged."""
    assert radar.official("comune.san-miniato.pi.it", certified=True) is True
    assert radar.official("comune.pagamenti.mi.it") is False
    mail = radar.Mail("m", "tributi@comune.pagamenti.mi.it", *PAGOPA_LINK, NOW)
    o = radar.classify(mail, "mail", {})
    assert o is not None and o.state == "flagged"
    certified = radar.classify(radar.Mail("p", "tributi@comune.san-miniato.pi.it", *PAGOPA, NOW), "pec", {})
    assert certified is not None and certified.state != "flagged"


def test_an_older_pending_hold_booked_later_is_never_counted_twice(
    client, db, phone, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """Review N3: a hold pending from the 12th, a movement booked on the 19th; the hold booked on the 20th
    under another id. The window read reaches back to the oldest pending movement, so the hold goes."""
    hold = {"entry_reference": "p1", "transaction_amount": {"amount": "80.00", "currency": "EUR"},
            "credit_debit_indicator": "DBIT", "booking_date": "2026-10-12", "status": "PDNG"}  # fmt: skip
    other = {**hold, "entry_reference": "t2", "booking_date": "2026-10-19", "status": "BOOK"}
    returned = [hold, other]
    base = _bank(NOW + timedelta(days=60))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):
            return httpx.Response(200, json={"transactions": returned})
        return base(request)

    http_handler["handler"] = handle
    _consent(client, phone)
    tick(client)
    returned[:] = [other, {**hold, "entry_reference": "b1", "booking_date": "2026-10-20", "status": "BOOK"}]
    clock.now += timedelta(hours=7)
    tick(client)
    rows = db.execute("SELECT provider_ref, pending FROM bank_transactions ORDER BY 1").fetchall()
    assert rows == [("b1", False), ("t2", False)]


def test_a_closed_missing_car_date_opens_again(client, db, phone) -> None:  # type: ignore[no-untyped-def]
    """Review N4: car on, off (the lines close), on again: the dates are still missing, so the lines open."""
    missing = "SELECT subject FROM obligations WHERE kind = 'missing_document' AND state <> 'done' ORDER BY 1"
    patch(client, phone, "admin_calendar", {"settings": {"car": True}})
    tick(client)
    patch(client, phone, "admin_calendar", {"settings": {"car": False}})
    db.execute("UPDATE connectors SET last_sync_at = NULL WHERE id = 'admin_calendar'")
    tick(client)
    assert db.execute(missing).fetchall() == []
    patch(client, phone, "admin_calendar", {"settings": {"car": True}})
    db.execute("UPDATE connectors SET last_sync_at = NULL WHERE id = 'admin_calendar'")
    tick(client)
    assert [r[0] for r in db.execute(missing).fetchall()] == ["car_insurance", "car_revision", "car_tax"]


def test_any_error_classifying_one_message_skips_only_it(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    google_connected(db)
    _email(db, "bad", "tributi@comune.milano.it", *PAGOPA, NOW - timedelta(hours=2))
    _email(db, "good", "tributi@comune.milano.it", *PAGOPA, NOW - timedelta(hours=1))
    real = radar.classify

    def broken(mail: radar.Mail, source: Any, wanted: Any) -> Any:
        if mail.ref == "gmail:bad":
            raise RuntimeError("synthetic")
        return real(mail, source, wanted)

    monkeypatch.setattr(radar, "classify", broken)
    tick(client)
    refs = {r[0] for r in db.execute("SELECT source_ref FROM obligations").fetchall()}
    assert "gmail:good" in refs
    assert db.execute("SELECT count(*) FROM email_messages WHERE radar_read_at IS NULL").fetchone()[0] == 0


# --- the bank's return address: Pisa hands the consent back to the app -----------------------------------------


def test_bank_return_hands_the_code_and_state_to_the_app_and_nothing_else(client) -> None:  # type: ignore[no-untyped-def]
    """The bank's https return (`redirect_url`) is Pisa's `/v1/connectors/bank/return`: a 302 to the app's
    `pisa://bank-consent` with the code and the state unchanged; no other parameter passes, and nothing
    that isn't a plain token does. No session is needed (their browser comes back from the bank)."""
    got = client.get(
        "/v1/connectors/bank/return",
        params={"code": "c-1.a_B~", "state": "s1", "extra": "x", "url": "https://elsewhere.example"},
        follow_redirects=False,
    )
    assert got.status_code == 302 and got.headers["cache-control"] == "no-store"
    assert got.headers["location"] == "pisa://bank-consent?code=c-1.a_B~&state=s1"
    refused = client.get(
        "/v1/connectors/bank/return", params={"error": "access_denied", "state": "s1"}, follow_redirects=False
    )
    assert refused.headers["location"] == "pisa://bank-consent?error=denied&state=s1"
    odd = client.get(
        "/v1/connectors/bank/return", params={"code": "a b<script>", "state": "s1"}, follow_redirects=False
    )
    assert odd.headers["location"] == "pisa://bank-consent?error=denied&state=s1"
