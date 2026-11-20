"""Money that comes back, promises, things that run out. One test per behaviour, each failing when its fix is
undone."""

import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from pydantic_ai import Agent
from structlog.testing import capture_logs

from pisa.api import money_schemas as s
from pisa.brain.money_tools import add_money_tools
from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.untrusted import data_block, watch_turn
from pisa.domain import decisions
from pisa.domain import flight_claims as fc
from pisa.domain import handoffs as handoff_rules
from pisa.domain import money_back as rules
from pisa.domain import money_links as links
from pisa.domain.sheet import SheetData
from pisa.services import worker_steps
from tests.actions_fake import OWNER, connect
from tests.assistant_env import rome, set_settings
from tests.conftest import PROTOCOL_DIR, pair

REST = PROTOCOL_DIR / "fixtures" / "rest"
NOW = rome(2026, 10, 20, 13)  # the clock fixture's time (Tuesday)


def rt(client: Any) -> Any:
    return client.app.state.runtime


def hdr(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


@pytest.fixture
def owner(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    connect(db, secrets_key)


def email(
    db: psycopg.Connection,
    mid: str,
    name: str,
    address: str,
    subject: str,
    body: str,
    *,
    at: datetime = NOW - timedelta(hours=2),
    labels: tuple[str, ...] = ("INBOX",),
    to: tuple[str, ...] = (),
    triaged: bool = True,
    category: str = "event_or_booking",
) -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, to_addresses, subject, sent_at, "
        "labels, snippet, body, intake_status, intake_category) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, "
        "%s, %s)",
        (mid, f"t-{mid}", name, address, list(to), subject, at, list(labels), body[:100], body,
         "done" if triaged else "pending", category if triaged else None),
    )  # fmt: skip


FLIGHT = (
    "Codice di prenotazione: ABC7QX2\nVolo AZ 5287  FCO → JFK\nPartenza: 15/10/2026 10:35\n"
    "Arrivo: 15/10/2026 14:20\nCheck-in online disponibile 24 ore prima."
)
DELAYED = (
    "Il volo AZ5287 del 15/10 è arrivato con un ritardo di 4 ore e 10 minuti. Ci scusiamo per il disagio."
)


def a_trip(client: Any, db: psycopg.Connection) -> None:
    """A flight, AZ5287 FCO → JFK on 15 October, from its confirmation email."""
    email(db, "f1", "ITA Airways", "noreply@ita-airways.com", "Conferma prenotazione volo AZ5287", FLIGHT,
          at=rome(2026, 9, 1, 9))  # fmt: skip
    client.portal.call(rt(client).trips.refresh)


def scan(client: Any) -> int:
    return client.portal.call(rt(client).money_back.scan)


class Screen:
    """The phone, as the tools see it: what the sheets they showed held."""

    def __init__(self) -> None:
        self.sheets: list[dict[str, Any]] = []

    def held(self, *_: Any) -> None:
        return None

    async def show(self, *_: Any, data: dict[str, Any] | None = None, **__: Any) -> str:
        self.sheets.append(data or {})
        return "shown"


def tools(money: Any, turn: ProfileTurn, screen: Screen | None = None) -> dict[str, Any]:
    agent: Agent[None, str] = Agent(None)
    add_money_tools(agent, money, turn, (lambda: screen) if screen else (lambda: None))  # type: ignore[arg-type,return-value]
    return {name: t.function for ts in agent.toolsets for name, t in getattr(ts, "tools", {}).items()}


def in_turn(client: Any, fn: Any, *args: Any, outside: bool = False, **kwargs: Any) -> Any:
    async def run() -> Any:
        with decisions.collecting():
            watch_turn()
            if outside:
                data_block("email-results", "il volo AZ5287 ha fatto 9 ore di ritardo", what="an email")
            return await fn(*args, **kwargs)

    return client.portal.call(run)


def tasks(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    """Pisa's money tasks (a trip's own check-in tasks aside)."""
    return db.execute(
        "SELECT title, status, source_kind, source_ref, notes FROM tasks t WHERE EXISTS (SELECT 1 FROM "
        "pisa_decisions d WHERE d.tool = 'money_task' AND d.ref = t.id::text) ORDER BY title"
    ).fetchall()


# --- the contract -----------------------------------------------------------------------------------


def test_contract_the_rest_fixtures_match_the_api_models() -> None:
    claims = s.Claims.model_validate_json((REST / "money_claims.json").read_text())
    assert {c.rule for c in claims.claims} == {"eu261", "us_dot"}
    assert all(c.amount is None for c in claims.claims if c.rule == "us_dot")  # a US refund: never a sum
    draft = s.Draft.model_validate_json((REST / "money_draft.json").read_text())
    SheetData.model_validate({"title": "x", "blocks": draft.buttons})  # handoff blocks
    for b in draft.buttons:
        assert handoff_rules.safe_url(b["url"], money=True)
    s.Refunds.model_validate_json((REST / "money_refunds.json").read_text())
    subs = s.Subs.model_validate_json((REST / "money_subscriptions.json").read_text())
    assert subs.subscriptions[0].cancel and subs.subscriptions[0].cancel.url
    s.BillChecks.model_validate_json((REST / "money_bill_checks.json").read_text())
    s.Warranties.model_validate_json((REST / "money_warranties.json").read_text())
    s.Promises.model_validate_json((REST / "money_promises.json").read_text())
    supplies = s.Supplies.model_validate_json((REST / "money_supplies.json").read_text())
    SheetData.model_validate({"title": "x", "blocks": [supplies.supplies[0].handoff]})


def test_contract_routes_need_the_device_token_and_answer_404(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    for path in ("claims", "refunds", "subscriptions", "bill_checks", "warranties", "promises", "supplies"):
        assert client.get(f"/v1/money/{path}").status_code == 401
        assert client.get(f"/v1/money/{path}", headers=hdr(db)).status_code == 200
    h = hdr(db)
    assert (
        client.patch(f"/v1/money/claims/{uuid.uuid4()}", json={"status": "sent"}, headers=h).status_code
        == 404
    )
    assert client.get("/v1/money/claims/x/draft", headers=h).status_code == 404
    assert client.patch("/v1/money/refunds/x", json={"status": "received"}, headers=h).status_code == 404
    assert client.patch("/v1/money/subscriptions/zz", json={"used": True}, headers=h).status_code == 404
    assert client.delete(f"/v1/money/warranties/{uuid.uuid4()}", headers=h).status_code == 404
    assert client.patch("/v1/money/claims/x", json={"status": "eligible"}, headers=h).status_code == 400


# --- row 1: EU261 / US DOT --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, flight, rule, amount",
    [
        ("FCO", "JFK", "AZ5287", "eu261", 600),  # leaves the EU, > 3,500 km
        ("JFK", "FCO", "AZ5288", "eu261", 600),  # arrives in the EU on an EU airline
        ("JFK", "FCO", "DL400", "us_dot", None),  # arrives in the EU on a US airline: not EU261
        ("FCO", "LHR", "FR100", "eu261", 250),  # ≤ 1,500 km
        ("CTA", "AMS", "KL1700", "eu261", 400),  # within the EU, > 1,500 km
        ("MXP", "IST", "TK1874", "eu261", 400),  # leaves the EU, 1,500-3,500 km
        ("JFK", "LAX", "AA1", "us_dot", None),
        ("IST", "LHR", "TK1", "none", None),
        ("FCO", "XXX", "AZ1", "none", None),  # an airport Pisa doesn't know: never a guess
    ],
)
def test_who_is_covered_and_the_law_s_fixed_sum(a, b, flight, rule, amount) -> None:  # type: ignore[no-untyped-def]
    assert fc.rule_for(a, b, flight) == rule
    v = fc.assess(flight=flight, origin=a, destination=b, disruption="denied_boarding", delay_min=None,
                  notice_days=None, scheduled_arr=NOW, scheduled_at=NOW - timedelta(hours=8),
                  now=NOW)  # fmt: skip
    assert v.amount == (Decimal(amount) if amount is not None else None)
    assert 6800 < (fc.distance_km("FCO", "JFK") or 0) < 6950
    assert fc.eu261_amount(3800, True) == 400 and fc.eu261_amount(3800, False) == 600  # within the EU: 400


@pytest.mark.parametrize(
    "disruption, delay, notice, arrived, status, reason",
    [
        ("delay", 250, None, True, "eligible", "delay_3h"),
        ("delay", 170, None, True, "not_eligible", "under_3h"),
        ("delay", None, None, True, "possible", "delay_unknown"),
        ("delay", 250, None, False, "possible", "delay_announced"),  # the arrival counts (Sturgeon)
        ("cancellation", None, 20, True, "not_eligible", "notice_14_days"),
        ("cancellation", None, 3, True, "eligible", "cancelled_late"),
        ("cancellation", None, None, True, "possible", "notice_unknown"),  # review MED1
    ],
)
def test_what_the_law_says_of_a_disruption(disruption, delay, notice, arrived, status, reason) -> None:  # type: ignore[no-untyped-def]
    arr = NOW - timedelta(hours=6) if arrived else NOW + timedelta(hours=5)
    v = fc.assess(flight="AZ5287", origin="FCO", destination="JFK", disruption=disruption, delay_min=delay,
                  notice_days=notice, scheduled_arr=arr, scheduled_at=arr - timedelta(hours=9),
                  now=NOW)  # fmt: skip
    assert (v.status, v.reason) == (status, reason)
    old = fc.assess(flight="AZ5287", origin="FCO", destination="JFK", disruption="delay", delay_min=300,
                    notice_days=None, scheduled_arr=NOW - timedelta(days=800),
                    scheduled_at=NOW - timedelta(days=800), now=NOW)  # fmt: skip
    assert old.reason == "older_than_2_years"  # "check first"
    us = fc.assess(flight="AA1", origin="JFK", destination="LAX", disruption="cancellation", delay_min=None,
                   notice_days=1, scheduled_arr=NOW, scheduled_at=NOW, now=NOW)  # fmt: skip
    assert (us.status, us.reason, us.amount) == ("eligible", "refund_due", None)


@pytest.mark.parametrize(
    "subject, body, kind, delay, arrival",
    [
        ("Volo AZ5287 cancellato", "Il tuo volo AZ5287 è stato cancellato.", "cancellation", None, None),
        ("AZ5287", "Your flight AZ5287 has been delayed by 4 hours and 20 minutes.", "delay", 260, None),
        (
            "Aggiornamento AZ5287",
            "Il volo AZ5287 è in ritardo. Nuovo orario di arrivo: 18:45",
            "delay",
            None,
            (18, 45),
        ),
        ("Aggiornamento AZ5287", "Volo AZ5287: ritardo previsto di 190 minuti", "delay", 190, None),
        ("AZ5287 overbooked", "We denied boarding on flight AZ5287", "denied_boarding", None, None),
        # an offer, a button, a "what if" are no disruption
        (
            "La tua prenotazione AZ5287",
            "Cancellazione gratuita entro 24 ore. Puoi annullare la prenotazione.",
            None,
            None,
            None,
        ),
        ("AZ5287", "If your flight is cancelled, you can rebook online.", None, None, None),
        ("Offerte", "Voli a partire da 29 euro", None, None, None),
    ],
)
def test_what_an_airline_s_email_states(subject, body, kind, delay, arrival) -> None:  # type: ignore[no-untyped-def]
    notice = fc.read_notice(subject, body)
    if kind is None:
        assert notice is None
        return
    assert notice is not None and notice.disruption == kind and "AZ5287" in notice.flights
    assert notice.delay_min == delay and notice.new_arrival == arrival


def test_a_delay_email_on_his_flight_is_a_claim_with_its_task(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "Il tuo volo AZ5287", DELAYED,
          at=rome(2026, 10, 15, 20))  # fmt: skip
    with capture_logs() as logs:
        assert scan(client) >= 1
    (claim,) = client.get("/v1/money/claims", headers=hdr(db)).json()["claims"]
    assert (claim["flight"], claim["place"], claim["status"], claim["reason"]) == (
        "AZ5287", "FCO → JFK", "eligible", "delay_3h",
    )  # fmt: skip
    assert (claim["amount"], claim["currency"], claim["delay_min"], claim["rule"]) == (
        "600.00",
        "EUR",
        250,
        "eu261",
    )
    assert claim["deadline_on"] == "2027-10-15" and claim["scheduled_at"].startswith("2026-10-15T10:35")
    ((title, status, kind, ref, _),) = tasks(db)
    assert title == "Chiedere a ITA Airways la compensazione del volo AZ5287 (600 €)"
    assert (status, kind, ref) == ("open", "email", "d1")
    assert scan(client) == 0  # each email once
    assert len(tasks(db)) == 1
    text = json.dumps(logs, default=str)
    assert "ITA" not in text and "600" not in text and "AZ5287" not in text  # ids and counts only
    lines = [ln for d in client.get("/v1/ledger", headers=hdr(db)).json()["days"] for ln in d["lines"]]
    assert [(ln["kind"], ln["source"]) for ln in lines] == [("money", "rule")]  # Undoable 24 h
    assert lines[0]["undo"]["available"] is True


def test_a_notice_from_a_sender_that_sells_no_travel_is_nothing(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(
        db,
        "x1",
        "Avviso",
        "alert@example.invalid",
        "AZ5287 cancellato",
        "Il tuo volo AZ5287 è stato cancellato.",
    )
    email(db, "x2", "ITA Airways", "noreply@ita-airways.com", "AZ5287", DELAYED, category="promo_or_spam")
    scan(client)
    assert db.execute("SELECT count(*) FROM flight_claims").fetchone() == (0,)


def test_an_airline_s_notice_waits_for_the_intake(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "AZ5287", DELAYED, triaged=False)
    scan(client)
    assert db.execute("SELECT count(*) FROM flight_claims").fetchone() == (0,)
    db.execute("UPDATE email_messages SET intake_status = 'done', intake_category = 'event_or_booking'")
    scan(client)
    assert db.execute("SELECT status FROM flight_claims").fetchone() == ("eligible",)


def test_a_delay_announced_is_owed_once_it_landed(client, db, owner, clock) -> None:  # type: ignore[no-untyped-def]
    email(db, "f2", "ITA Airways", "noreply@ita-airways.com", "Conferma AZ5287",
          FLIGHT.replace("15/10/2026", "22/10/2026"), at=rome(2026, 9, 1, 9))  # fmt: skip
    client.portal.call(rt(client).trips.refresh)
    email(
        db, "d2", "ITA Airways", "noreply@ita-airways.com", "AZ5287", "Il volo AZ5287 è in ritardo di 5 ore."
    )
    scan(client)
    assert db.execute("SELECT status, reason FROM flight_claims").fetchone() == (
        "possible",
        "delay_announced",
    )
    assert tasks(db) == []
    clock.now = rome(2026, 10, 23, 9).astimezone(clock.now.tzinfo)
    client.portal.call(rt(client).money_back.tick)
    assert db.execute("SELECT status, reason FROM flight_claims").fetchone() == ("eligible", "delay_3h")
    assert len(tasks(db)) == 1


def test_his_status_is_never_changed_by_pisa(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "AZ5287", DELAYED, at=rome(2026, 10, 15, 20))
    scan(client)
    (claim,) = client.get("/v1/money/claims", headers=hdr(db)).json()["claims"]
    sent = client.patch(f"/v1/money/claims/{claim['id']}", json={"status": "sent"}, headers=hdr(db)).json()
    assert sent["status"] == "sent"
    assert tasks(db)[0][1] == "done"  # their "sent" closes Pisa's task
    email(db, "d3", "ITA Airways", "noreply@ita-airways.com", "AZ5287", "Il volo AZ5287 è stato cancellato.",
          at=rome(2026, 10, 16, 9))  # fmt: skip
    scan(client)
    client.portal.call(rt(client).money_back.tick)
    assert db.execute("SELECT status FROM flight_claims").fetchone() == ("sent",)


def test_the_law_s_sums_only_in_the_database(db) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO flight_claims (id, flight, day, scheduled_at, disruption, rule, status, reason, "
            "amount, "
            "currency, source, created_at, updated_at) VALUES (gen_random_uuid(), 'AZ1', '2026-10-01', "
            "now(), "
            "'delay', 'eu261', 'eligible', 'delay_3h', 500, 'EUR', 'said', now(), now())"
        )


def test_the_draft_keeps_the_booking_s_last_4_and_its_buttons_are_pisa_s(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "AZ5287", DELAYED, at=rome(2026, 10, 15, 20))
    scan(client)
    (claim,) = client.get("/v1/money/claims", headers=hdr(db)).json()["claims"]
    for lang in ("it-IT", "en-US"):
        draft = client.get(f"/v1/money/claims/{claim['id']}/draft?lang={lang}", headers=hdr(db)).json()
        assert "QX2" in draft["subject"] and "ABC7QX2" not in json.dumps(draft)  # The last 4 only
        assert ("600" in draft["text"]) and "[IBAN]" in draft["text"]  # their IBAN: theirs to write
        assert "4 ore e 10 minuti" in draft["text"] or "4 hours and 10 minutes" in draft["text"]
        urls = [b["url"] for b in draft["buttons"]]
        assert urls == ["https://www.ita-airways.com/", links.EU_FORM]
        assert all(handoff_rules.safe_url(u, money=True) for u in urls)
        assert not handoff_rules.safe_url(urls[0])  # review LOW: an airline's site only on a money button
        SheetData.model_validate({"title": "x", "blocks": draft["buttons"]})
    # the buttons are a handoff preparation (purpose money), kept once, outside the brain's waiting list
    assert db.execute("SELECT purpose, object_id FROM handoff_preps").fetchall() == [
        ("money", f"claim:{claim['id']}")
    ]
    assert client.portal.call(rt(client).handoffs.open_preps) == []


def test_his_words_make_the_claim_never_a_tool_argument(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    turn = ProfileTurn(words="il volo AZ5287 ha fatto 4 ore di ritardo, prepara il reclamo", lang="it-IT")
    out = in_turn(client, tools(rt(client).money_back, turn)["flight_claim"], "AZ5287")
    assert "eligible" in out and "600.00 EUR" in out and "Nothing is sent" in out
    assert db.execute("SELECT delay_min, delay_source, source FROM flight_claims").fetchone() == (
        240,
        "said",
        "said",
    )
    assert tasks(db)[0][2] == "voice"
    # no words about what happened: Pisa asks; it never records a delay of its own
    db.execute("DELETE FROM flight_claims")
    asked = in_turn(
        client,
        tools(rt(client).money_back, ProfileTurn(words="prepara il reclamo", lang="it-IT"))["flight_claim"],
    )
    assert "ask them what happened" in asked
    assert db.execute("SELECT count(*) FROM flight_claims").fetchone() == (0,)


def test_outside_text_can_t_record_a_delay(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    turn = ProfileTurn(words="leggi l'email e prepara il reclamo", lang="it-IT")
    out = in_turn(client, tools(rt(client).money_back, turn)["flight_claim"], "AZ5287", outside=True)
    assert "ask them what happened" in out  # the email's "9 ore di ritardo" is no word of theirs
    assert db.execute("SELECT count(*) FROM flight_claims").fetchone() == (0,)


# --- row 2: refunds promised, not received ----------------------------------------------------------------


@pytest.mark.parametrize(
    "subject, body, kind, amount, by",
    [
        (
            "Reso ricevuto",
            "Riceverai il rimborso di € 49,90 entro il 25/10/2026.",
            "promised",
            "49.90",
            date(2026, 10, 25),
        ),
        (
            "Refund",
            "Your refund of $20.00 has been issued. You'll see it within 5 business days.",
            "promised",
            "20.00",
            date(2026, 10, 27),
        ),
        ("Ordine annullato", "Abbiamo avviato il rimborso.", "promised", None, None),
        ("Rimborso", "Il rimborso di € 49,90 è stato accreditato.", None, None, None),
        ("Rimborso accreditato", "Rimborso accreditato: € 49,90", "credited", "49.90", None),
        ("Novità", "Scopri le offerte", None, None, None),
    ],
)
def test_a_refund_promised_or_credited(subject, body, kind, amount, by) -> None:  # type: ignore[no-untyped-def]
    found = rules.read_refund(subject, body, date(2026, 10, 20))
    if kind is None and "accreditato" not in subject:
        assert found is None or found.kind == "credited"
        return
    assert found is not None and found.kind == (kind or "credited")
    assert (str(found.amount) if found.amount else None) == amount and found.expected_by == by


def test_late_refund_becomes_a_task_with_the_reminder_and_a_credit_closes_it(
    client, db, owner, clock
) -> None:  # type: ignore[no-untyped-def]
    email(db, "r1", "Zalando", "service@zalando.example.invalid", "Reso ricevuto",
          "Riceverai il rimborso di € 49,90 entro il 13/10/2026.", at=rome(2026, 10, 6, 10),
          category="other")  # fmt: skip
    email(db, "r2", "Zalando", "service@zalando.example.invalid", "Altro reso",
          "Riceverai il rimborso di € 12,00 entro il 18/10/2026.", at=rome(2026, 10, 8, 10),
          category="other")  # fmt: skip
    scan(client)
    by = {r["amount"]: r for r in client.get("/v1/money/refunds", headers=hdr(db)).json()["refunds"]}
    assert (
        by["49.90"]["status"] == "overdue" and by["12.00"]["status"] == "waiting"
    )  # 13 + 5 < 20; 18 + 5 > 20
    ((title, _, kind, ref, notes),) = tasks(db)
    assert title == "Sollecitare il rimborso di 49,90 € da Zalando" and (kind, ref) == ("email", "r1")
    # the reminder Pisa drafted, theirs to send
    assert "Rimborso non ricevuto" in notes and "13/10/2026" in notes
    email(db, "r3", "Zalando", "service@zalando.example.invalid", "Rimborso accreditato",
          "Rimborso accreditato: € 12,50", at=NOW - timedelta(hours=1), category="other")  # fmt: skip
    scan(client)  # another amount: not this refund
    assert {r["status"] for r in client.get("/v1/money/refunds", headers=hdr(db)).json()["refunds"]} == {
        "overdue", "waiting",
    }  # fmt: skip
    email(db, "r4", "Zalando", "service@zalando.example.invalid", "Rimborso accreditato",
          "Rimborso accreditato: € 49,90", at=NOW - timedelta(minutes=30), category="other")  # fmt: skip
    scan(client)
    assert db.execute("SELECT status FROM refunds_promised WHERE amount = 49.90").fetchone() == ("received",)
    assert tasks(db)[0][1] == "dismissed"


# --- row 3: subscriptions ---------------------------------------------------------------------------------


def _receipts(
    db: psycopg.Connection, sender: str, name: str, amounts: list[str], subject: str = "Ricevuta"
) -> None:
    for n, amount in enumerate(amounts):
        day = NOW - timedelta(days=30 * (len(amounts) - n))
        db.execute(
            "INSERT INTO money_seen (id, email_id, kind, sender_key, from_name, subject, amount, currency, "
            "paid, "
            "sent_at, seen_at) VALUES (gen_random_uuid(), %s, 'receipt', %s, %s, %s, %s, 'EUR', true, %s, "
            "%s)",
            (f"{sender}-{n}", sender, name, subject, Decimal(amount), day, day),
        )


def test_duplicate_unused_price_up_and_the_page_to_cancel(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    _receipts(db, "info@account.netflix.com", "Netflix", ["12.99", "12.99", "13.99"])
    _receipts(db, "no_reply@email.apple.com", "Apple", ["13.99"] * 3, subject="Ricevuta: Netflix")
    subs = {
        x["name"]: x for x in client.get("/v1/money/subscriptions", headers=hdr(db)).json()["subscriptions"]
    }
    assert subs["Netflix"]["service"] == subs["Apple"]["service"] == "netflix"
    assert set(subs["Netflix"]["flags"]) == {"duplicate", "unused", "price_up"}
    assert subs["Netflix"]["duplicate_of"] == subs["Apple"]["id"]
    assert subs["Netflix"]["cancel"]["url"] == "https://www.netflix.com/cancelplan"
    assert "due volte" in subs["Netflix"]["text"]
    # a use signal from the service (a new login) clears "unused"
    email(db, "u1", "Netflix", "info@account.netflix.com", "Nuovo accesso al tuo account",
          "Un nuovo dispositivo",
          at=NOW - timedelta(days=3), category="other")  # fmt: skip
    subs = {
        x["name"]: x for x in client.get("/v1/money/subscriptions", headers=hdr(db)).json()["subscriptions"]
    }
    assert "unused" not in subs["Netflix"]["flags"] and subs["Netflix"]["last_use_on"] is not None
    # their "tienilo": no unused flag for 90 days
    apple = client.patch(
        f"/v1/money/subscriptions/{subs['Apple']['id']}", json={"keep": True}, headers=hdr(db)
    )
    assert apple.status_code == 200 and "unused" not in apple.json()["flags"]


def test_the_cancel_page_is_a_button_he_taps_pisa_never_cancels(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    _receipts(db, "info@account.netflix.com", "Netflix", ["12.99"] * 3)
    turn = ProfileTurn(words="come disdico netflix?", lang="it-IT")
    out = in_turn(client, tools(rt(client).money_back, turn)["show_cancel_page"], "Netflix")
    assert "Pisa never does" in out
    ((url,),) = db.execute("SELECT url FROM handoffs").fetchall()
    assert url == "https://www.netflix.com/cancelplan" and handoff_rules.safe_url(url, money=True)
    hosts = {sv.url.split("/")[2] for sv in links.SERVICES if sv.url}
    assert hosts <= links.HOSTS and not hosts & handoff_rules.TEMPLATE_HOSTS  # scoped to money buttons


# --- row 4: the Italian bill watchdog ---------------------------------------------------------------------

OLD_BILL = (
    "Bolletta di conguaglio. Periodo di fatturazione dal 01/01/2023 al 30/06/2024 (letture effettive). "
    "Importo da pagare € 412,30, scadenza 15/11/2026."
)


def test_a_conguaglio_over_2_years_old_is_flagged_with_the_letter(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    email(db, "e1", "Enel Energia", "bollette@enel.it", "La tua bolletta luce", OLD_BILL,
          category="bill_or_deadline")  # fmt: skip
    email(db, "e2", "Enel Energia", "bollette@enel.it", "La tua bolletta luce",
          OLD_BILL.replace("2023", "2025").replace("2024", "2026"), category="bill_or_deadline")  # fmt: skip
    email(db, "e3", "Rossi", "rossi@example.invalid", "conguaglio luce", OLD_BILL, category="other")
    scan(client)
    (check,) = client.get("/v1/money/bill_checks", headers=hdr(db)).json()["bill_checks"]
    assert (check["supplier"], check["utility"], check["period_to"], check["reason"]) == (
        "Enel Energia", "luce", "2024-06-30", "older_than_2_years",
    )  # fmt: skip
    assert check["amount"] == "412.30" and check["old_amount"] is None  # as stated, never computed
    ((title, _, _, ref, notes),) = tasks(db)
    assert title == "Contestare il conguaglio Enel Energia (prescrizione biennale)" and ref == "e1"
    assert "L. 205/2017" in notes or "Legge 27 dicembre 2017 n. 205" in notes
    draft = client.get(f"/v1/money/bill_checks/{check['id']}/draft", headers=hdr(db)).json()
    assert (
        "€ 412,30" in draft["text"] and "€ [IMPORTO PRESCRITTO]" in draft["text"] and draft["buttons"] == []
    )


def test_the_supplier_s_own_words_and_what_is_never_a_flag() -> None:
    stated = rules.read_conguaglio(
        "bollette@a2a.eu", "Bolletta gas: conguaglio",
        "Sono presenti importi prescritti per consumi risalenti a più di due anni: € 120,00. PDR 123.",
        date(2026, 10, 20),
    )  # fmt: skip
    assert stated is not None and stated.reason == "stated_by_supplier" and stated.utility == "gas"
    assert stated.old_amount == Decimal("120.00")
    offer = rules.read_conguaglio("news@enel.it", "Offerta luce: conguaglio scontato", OLD_BILL,
                                  date(2026, 10, 20))  # fmt: skip
    assert offer is None


# --- row 5: warranties ------------------------------------------------------------------------------------


def test_a_delivery_email_is_a_warranty_and_is_it_still_under_warranty(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    email(db, "o1", "Amazon.it", "conferma-spedizione@amazon.it", 'Ordinato: "Lavatrice Samsung WW90T"',
          "Totale ordine: € 449,00", at=rome(2025, 3, 2, 10), category="other")  # fmt: skip
    email(db, "o2", "Amazon.it", "conferma-spedizione@amazon.it", 'Consegnato: "Lavatrice Samsung WW90T"',
          "Il tuo pacco è stato consegnato.", at=rome(2025, 3, 4, 15), category="other")  # fmt: skip
    db.execute("UPDATE email_messages SET sent_at = %s WHERE id IN ('o1', 'o2')", (NOW - timedelta(days=10),))
    scan(client)
    db.execute("UPDATE purchases SET ordered_on = '2025-03-02', delivered_on = '2025-03-04'")
    (w,) = client.get("/v1/money/warranties?q=lavatrice", headers=hdr(db)).json()["warranties"]
    assert (w["item"], w["price"], w["region"], w["in_warranty"]) == (
        "Lavatrice Samsung WW90T",
        "449.00",
        "EU",
        True,
    )
    assert (w["presumption_until"], w["ends_on"], w["action_until"]) == (
        "2026-03-04",
        "2027-03-04",
        "2027-05-04",
    )
    asked = ProfileTurn(words="è ancora in garanzia la lavatrice?", lang="it-IT")
    out = in_turn(client, tools(rt(client).money_back, asked)["warranty_check"], "la lavatrice")
    assert "fino al 04/03/2027" in out and "<email-" in out  # an email's words: data
    none = in_turn(
        client, tools(rt(client).money_back, ProfileTurn(lang="it-IT"))["warranty_check"], "il frigo"
    )
    assert "ask when and where they bought it" in none


def test_a_delivery_read_before_its_order_is_still_one_purchase(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    """The merge with retention: the delivery and the order in the same minute (or the delivery first, a
    scan's boundary): the order joins the delivery's purchase, never a second (it hung on the plan's row
    order)."""
    email(db, "o1", "Amazon.it", "conferma-spedizione@amazon.it", 'Ordinato: "Lavatrice Samsung WW90T"',
          "Totale ordine: € 449,00", at=rome(2025, 3, 2, 10), category="other")  # fmt: skip
    email(db, "o2", "Amazon.it", "conferma-spedizione@amazon.it", 'Consegnato: "Lavatrice Samsung WW90T"',
          "Il tuo pacco è stato consegnato.", at=rome(2025, 3, 4, 15), category="other")  # fmt: skip
    db.execute("UPDATE email_messages SET sent_at = %s WHERE id = 'o2'", (NOW - timedelta(days=10),))
    db.execute(
        "UPDATE email_messages SET sent_at = %s WHERE id = 'o1'", (NOW - timedelta(days=10, minutes=-1),)
    )
    scan(client)
    rows = db.execute("SELECT item_key, price, delivered_on IS NOT NULL FROM purchases").fetchall()
    assert len(rows) == 1 and rows[0][1] is not None and rows[0][2]


def test_a_purchase_only_on_his_words(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    said = ProfileTurn(words="ho comprato la lavatrice da Unieuro il 3 marzo 2025", lang="it-IT")
    add = tools(rt(client).money_back, said)["keep_warranty"]
    assert "Not kept" in in_turn(client, add, "lavatrice", "2025-03-03", "Unieuro", outside=True)
    other = tools(rt(client).money_back, ProfileTurn(words="la lavatrice del 3 marzo 2025", lang="it-IT"))[
        "keep_warranty"
    ]
    assert "Not kept" in in_turn(client, other, "lavatrice", "2025-03-03")
    assert "legal guarantee until 2027-03-03" in in_turn(client, add, "lavatrice", "2025-03-03", "Unieuro")
    posted = client.post("/v1/money/warranties", json={"item": "Toaster", "purchased_on": "2026-08-10",
                         "region": "US"}, headers=hdr(db))  # fmt: skip
    assert posted.status_code == 201 and posted.json()["ends_on"] is None  # the US: the maker's term
    assert client.delete(f"/v1/money/warranties/{posted.json()['id']}", headers=hdr(db)).status_code == 204


# --- row 6: the promise keeper ----------------------------------------------------------------------------

PROMISE = (
    "Ciao Marco,\nti mando il contratto domani, e anche le foto venerdì.\nA presto\n\n"
    "Il giorno lun 19 ott 2026 Marco ha scritto:\n> ti mando la fattura appena posso"
)


def test_a_promise_in_his_own_sent_email_is_proposed_to_him(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    email(db, "s1", "Owner", OWNER, "Re: contratto", PROMISE, labels=("SENT",),
          to=("Marco Rossi <marco@example.invalid>",), triaged=False)  # fmt: skip
    # the same words received, or "sent" by someone else, are no promise of theirs
    email(
        db,
        "s2",
        "Marco",
        "marco@example.invalid",
        "contratto",
        "ti mando il contratto domani",
        category="other",
    )
    email(db, "s3", "Altro", "other@example.invalid", "x", "ti mando il preventivo domani", labels=("SENT",),
          to=("Anna <anna@example.invalid>",))  # fmt: skip
    scan(client)
    # Their promises are proposed intentions for their yes (no task on their own); Marco's own words in
    # the mail they received are only a waiting-for item; the other sender's "sent" words are nothing of
    # theirs
    got = set(db.execute("SELECT kind, state, what, source_ref FROM intentions").fetchall())
    assert got == {
        ("mine", "proposed", "Mandare il contratto a Marco Rossi", "s1"),
        ("mine", "proposed", "Mandare le foto a Marco Rossi", "s1"),
        ("waiting_for", "open", "il contratto", "s2"),
    }
    assert tasks(db) == []
    promises = client.get("/v1/money/promises", headers=hdr(db)).json()["promises"]
    due = {p["what"]: p["due_on"] for p in promises}
    assert due == {"il contratto": "2026-10-21", "le foto": "2026-10-23"}  # domani; venerdì
    assert not any("fattura" in p["what"] for p in promises)  # the quoted part is Marco's


@pytest.mark.parametrize(
    "body, found",
    [
        ("Ti invio il preventivo entro venerdì.", [("il preventivo", date(2026, 10, 23))]),
        ("I'll send you the slides tomorrow.", [("the slides", date(2026, 10, 21))]),
        ("Te lo mando domani.", []),  # no object Pisa can name
        ("Ti mando tutto domani.", []),  # an object that is only a pronoun
        ("> ti mando il file\nOk", []),
    ],
)
def test_what_is_a_promise(body, found) -> None:  # type: ignore[no-untyped-def]
    assert [(p.what, p.due_on) for p in rules.promises(body, date(2026, 10, 20))] == found


# --- row 7: things that run out ---------------------------------------------------------------------------


def test_bought_three_times_a_task_the_day_before_with_the_store(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    for n, day in enumerate((rome(2026, 7, 23, 9), rome(2026, 8, 22, 9), rome(2026, 9, 21, 9))):
        email(db, f"c{n}", "Amazon.it", "auto-confirm@amazon.it",
              'Il tuo ordine di "Capsule caffè Lavazza A Modo Mio"',
              "Ordine confermato.", at=NOW - timedelta(days=5), category="other")  # fmt: skip
        scan(client)
        db.execute("UPDATE purchases SET ordered_on = %s WHERE source_email_id = %s", (day.date(), f"c{n}"))
    client.portal.call(lambda: rt(client).money_back.tick(force=True))
    ((title, _, kind, _, _),) = tasks(db)
    assert title == "Ricomprare Capsule caffè Lavazza A Modo Mio" and kind == "email"
    (supply,) = client.get("/v1/money/supplies", headers=hdr(db)).json()["supplies"]
    assert (supply["every_days"], supply["next_on"], supply["count"]) == (30, "2026-10-21", 3)
    assert supply["handoff"]["url"] == "https://www.amazon.it/gp/buyagain" and supply["task_id"]
    assert db.execute("SELECT object_id FROM handoff_preps").fetchone() == (f"task:{supply['task_id']}",)
    db.execute("UPDATE tasks SET status = 'dismissed'")  # they didn't need it: Pisa never makes it again
    client.portal.call(lambda: rt(client).money_back.tick(force=True))
    assert len(tasks(db)) == 1  # once per run-out
    db.execute("DELETE FROM tasks")  # even once the pool's own record of it is gone
    client.portal.call(lambda: rt(client).money_back.tick(force=True))
    assert tasks(db) == []


def test_two_buys_are_no_supply() -> None:
    assert rules.supply("x", "x", [date(2026, 9, 1), date(2026, 10, 1)]) is None
    assert rules.supply("x", "x", [date(2026, 1, 1), date(2026, 9, 1), date(2026, 10, 1)]) is not None


# --- the rails --------------------------------------------------------------------------------------------


def test_rail_a_stop_pauses_pisa_s_money_tasks() -> None:
    assert "money_back" in worker_steps.INITIATIVE_STEPS


def test_rail_his_rules_say_no_tasks_from_email(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO profile_claims (id, kind, statement, subject_key, provenance, state, hard, created_at, "
        "updated_at) VALUES (gen_random_uuid(), 'constraint', 'non creare mai attività dalle email', 'x', "
        "'said', 'active', true, now(), now())"
    )
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "AZ5287", DELAYED, at=rome(2026, 10, 15, 20))
    email(db, "r1", "Zalando", "service@zalando.example.invalid", "Reso",
          "Riceverai il rimborso entro il 1/10/2026.", at=rome(2026, 9, 25, 10),
          category="other")  # fmt: skip
    scan(client)
    assert db.execute("SELECT status FROM flight_claims").fetchone() == ("eligible",)  # still listed
    assert db.execute("SELECT status FROM refunds_promised").fetchone() == ("overdue",)
    assert tasks(db) == []


def test_rail_money_records_are_kept_until_he_deletes_them(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    """A purchase years past its warranty stays (he deletes it, family `money`)."""
    import asyncio

    from pisa.services import maintenance

    db.execute(
        "INSERT INTO purchases (id, item, item_key, ordered_on, region, source, created_at, updated_at) "
        "VALUES "
        "(gen_random_uuid(), 'Vecchio', 'vecchio', '2023-01-01', 'EU', 'said', now(), now()), "
        "(gen_random_uuid(), 'Nuovo', 'nuovo', '2025-01-01', 'EU', 'said', now(), now())"
    )
    counts = asyncio.run(maintenance.prune_history(client.app.state.runtime.settings, NOW))
    assert "purchases" not in counts
    assert db.execute("SELECT count(*) FROM purchases").fetchone() == (2,)


def test_rail_the_mail_sync_reads_money_after_the_trips_unless_stopped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from pisa.services import runtime as runtime_module
    from pisa.services.google_sync import SyncResult

    ran: list[str] = []

    async def synced(*_: Any, **__: Any) -> SyncResult:
        return SyncResult(ran=True)

    def step(name: str, result: Any = None) -> Any:
        async def run(*_: Any, **__: Any) -> Any:
            with decisions.collecting():
                ran.append(name)
                return result

        return run

    for stopped in (False, True):
        ran.clear()
        fake: Any = SimpleNamespace()
        fake.google, fake.pool, fake.clock = None, None, lambda: NOW
        bills = type("M", (), {"scan": staticmethod(step("bills"))})()
        fake.autopilot = type("A", (), {"stopped": staticmethod(step("stopped?", stopped)), "money": bills})()
        fake.intake = type("I", (), {"run": staticmethod(step("intake"))})()
        fake.trips = type("T", (), {"refresh": staticmethod(step("trips"))})()
        fake.money_back = type("K", (), {"scan": staticmethod(step("money_back"))})()
        fake.resolve_places = step("places")
        monkeypatch.setattr(runtime_module, "sync_gmail", synced)
        asyncio.run(runtime_module.Runtime.sync_gmail_and_intake(fake))
        got = [r for r in ran if r != "stopped?"]
        running = ["intake", "bills", "places", "trips", "money_back"]
        assert got == (["places", "trips"] if stopped else running)


def test_rail_pisa_s_money_task_is_undone_from_the_ledger(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "AZ5287", DELAYED, at=rome(2026, 10, 15, 20))
    scan(client)
    (line,) = [ln for d in client.get("/v1/ledger", headers=hdr(db)).json()["days"] for ln in d["lines"]]
    undone = client.post(f"/v1/ledger/{line['id']}/undo", headers=hdr(db))
    assert undone.status_code == 200 and tasks(db)[0][1] == "dismissed"


def test_his_words_pick_the_flight_never_the_model_s(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    back = FLIGHT.replace("AZ 5287  FCO → JFK", "AZ 5288  JFK → FCO").replace("15/10/2026", "17/10/2026")
    email(
        db, "f3", "ITA Airways", "noreply@ita-airways.com", "Conferma AZ5288", back, at=rome(2026, 9, 1, 10)
    )
    client.portal.call(rt(client).trips.refresh)
    turn = ProfileTurn(words="il volo AZ5287 ha fatto 4 ore di ritardo", lang="it-IT")
    in_turn(client, tools(rt(client).money_back, turn)["flight_claim"], "AZ5288")
    assert db.execute("SELECT flight FROM flight_claims").fetchall() == [("AZ5287",)]


# --- the review -------------------------------------------------------------------------


def _letter(lang: str, **kw: Any) -> tuple[str, str]:
    args: dict[str, Any] = {
        "lang": lang, "flight": "AZ5287", "origin": "FCO", "destination": "JFK",
        "scheduled_at": rome(2026, 10, 15, 10, 35), "scheduled_arr": rome(2026, 10, 15, 20, 20),
        "disruption": "delay", "delay_min": 250, "rule": "eu261", "amount": Decimal(600), "distance_km": 6881,
        "booking_last4": "7QX2",
    }  # fmt: skip
    return fc.draft(**(args | kw))


def test_a_cancellation_with_an_unknown_notice_is_possible_and_the_letter_states_nothing_unknown(
    client, db, owner
) -> None:  # type: ignore[no-untyped-def]
    _, text = _letter("it-IT", disruption="cancellation", notice_days=None)
    assert "[Se vero: meno di 14 giorni" in text and "[DATA DELL'AVVISO]" in text
    assert "meno di 14 giorni prima della partenza, senza" not in text.replace("[Se vero: meno di 14", "")
    _, known = _letter("it-IT", disruption="cancellation", notice_days=3)
    assert "preavviso di 3 giorni" in known
    a_trip(client, db)
    turn = ProfileTurn(words="mi hanno cancellato il volo AZ5287", lang="it-IT")
    in_turn(client, tools(rt(client).money_back, turn)["flight_claim"], "AZ5287")
    assert db.execute("SELECT status, reason FROM flight_claims").fetchone() == ("possible", "notice_unknown")
    assert tasks(db) == []  # nothing owed yet: no task


def test_over_3500_km_and_under_4_hours_the_airline_may_halve_it() -> None:
    assert fc.reducible(6881, 200) and not fc.reducible(6881, 250) and not fc.reducible(1200, 200)
    assert "50% (art. 7.2)" in fc.why("delay_3h", "it-IT", reducible=True)
    _, it = _letter("it-IT", delay_min=200)
    _, en = _letter("en-US", delay_min=200)
    assert "50% (art. 7.2)" in it and "50% (art. 7(2))" in en
    assert "50%" not in _letter("it-IT", delay_min=250)[1]


def test_the_latest_notice_wins_and_a_claim_is_owed_only_once_it_landed(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "AZ5287",
          "Il volo AZ5287 è in ritardo di 5 ore.", at=rome(2026, 10, 15, 9))  # fmt: skip
    email(db, "d2", "ITA Airways", "noreply@ita-airways.com", "AZ5287",
          "Il volo AZ5287 è in ritardo: ritardo previsto di 200 minuti.",
          at=rome(2026, 10, 15, 11))  # fmt: skip
    scan(client)
    assert db.execute("SELECT delay_min FROM flight_claims").fetchone() == (200,)
    mid_air = fc.assess(flight="AZ5287", origin="FCO", destination="JFK", disruption="delay", delay_min=250,
                        notice_days=None, scheduled_arr=NOW - timedelta(hours=1),
                        scheduled_at=NOW - timedelta(hours=10), now=NOW)  # fmt: skip
    assert (mid_air.status, mid_air.reason) == ("possible", "delay_announced")  # lands in 3 h 10


def test_every_reader_only_on_triaged_mail_and_a_utility_s_own_domain(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    email(db, "p1", "Rimborsi", "rimborsi@phish.example.invalid", "Rimborso",
          "Riceverai il rimborso di € 499,00.",
          category="promo_or_spam")  # fmt: skip
    email(db, "p2", "Zalando", "service@zalando.example.invalid", "Reso",
          "Riceverai il rimborso di € 49,90 entro il 25/10/2026.", triaged=False)  # fmt: skip
    email(
        db,
        "p3",
        "Enel",
        "bollette@enel.phish.example",
        "Bolletta luce: conguaglio",
        OLD_BILL,
        category="other",
    )
    scan(client)
    assert db.execute("SELECT count(*) FROM refunds_promised").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM bill_checks").fetchone() == (0,)
    db.execute("UPDATE email_messages SET intake_status = 'done', intake_category = 'other' WHERE id = 'p2'")
    scan(client)  # read once the intake judged it
    assert db.execute("SELECT amount FROM refunds_promised").fetchone() == (Decimal("49.90"),)
    assert rules.read_conguaglio(
        "bollette@clienti.enel.it", "Bolletta luce: conguaglio", OLD_BILL, date(2026, 10, 20)
    )


def test_every_sum_is_a_count_not_advice(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path

    a_trip(client, db)
    turn = ProfileTurn(words="il volo AZ5287 ha fatto 4 ore di ritardo", lang="it-IT")
    phone = Screen()
    out = in_turn(client, tools(rt(client).money_back, turn, phone)["flight_claim"], "AZ5287")
    assert "non una consulenza" in out and "circostanze eccezionali" in out
    texts = [b.get("text", "") for b in phone.sheets[0]["blocks"] if b["type"] == "text"]
    assert any("non una consulenza" in t for t in texts)  # on the sheet too
    email(db, "e1", "Enel Energia", "bollette@enel.it", "La tua bolletta luce", OLD_BILL, category="other")
    scan(client)
    bill = in_turn(
        client,
        tools(rt(client).money_back, ProfileTurn(words="contesta il conguaglio", lang="it-IT"))[
            "contest_bill"
        ],
    )
    assert "non una consulenza" in bill and "undisputed" not in bill
    listed = in_turn(client, tools(rt(client).money_back, ProfileTurn(lang="it-IT"))["money_back"])
    assert "non una consulenza" in listed
    text = (Path(__file__).parents[1] / "pisa/brain/skills/money-back.md").read_text()
    assert "potrebbero spettarti" in text.lower() and "ti spettano" not in text.lower()


def test_the_whole_letter_is_on_the_button_to_copy(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    email(db, "d1", "ITA Airways", "noreply@ita-airways.com", "AZ5287", DELAYED, at=rome(2026, 10, 15, 20))
    scan(client)
    (claim,) = client.get("/v1/money/claims", headers=hdr(db)).json()["claims"]
    draft = client.get(f"/v1/money/claims/{claim['id']}/draft?lang=en-US", headers=hdr(db)).json()
    assert len(draft["text"]) > 1000 and draft["buttons"][0]["text"].endswith(draft["text"])
    SheetData.model_validate({"title": "x", "blocks": draft["buttons"]})


def test_a_money_button_is_checked_at_runtime(client) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        rt(client).money_back._button("k", "https://evil.example/", "x", "it-IT")


def test_a_stop_holds_the_task_his_words_would_make(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    db.execute("INSERT INTO pisa_stops (stopped_at, stopped_by) VALUES (now(), 'app')")
    a_trip(client, db)
    turn = ProfileTurn(words="il volo AZ5287 ha fatto 4 ore di ritardo", lang="it-IT")
    in_turn(client, tools(rt(client).money_back, turn)["flight_claim"], "AZ5287")
    assert db.execute("SELECT status FROM flight_claims").fetchone() == ("eligible",) and tasks(db) == []
    db.execute("UPDATE pisa_stops SET resumed_at = now(), resumed_by = 'app'")
    client.portal.call(lambda: rt(client).money_back.tick(force=True))
    assert len(tasks(db)) == 1


@pytest.mark.parametrize(
    "words, minutes",
    [
        ("il volo di 9 ore ha fatto 4 ore di ritardo", 240),
        ("il volo AZ5287 ha fatto 3 ore e mezza di ritardo", 210),
        ("ritardo di 5 ore sul volo di 2 ore", 300),
    ],
)
def test_the_number_his_delay_names(words, minutes) -> None:  # type: ignore[no-untyped-def]
    assert fc.said_disruption(words) == ("delay", minutes)


def test_keep_warranty_s_date_and_seller_are_his_words(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    said = ProfileTurn(words="ho comprato la lavatrice il 3 marzo 2025", lang="it-IT")
    keep = tools(rt(client).money_back, said)["keep_warranty"]
    assert "Not kept" in in_turn(client, keep, "lavatrice", "2025-04-03")  # the model's guess
    assert "Not kept" in in_turn(client, keep, "lavatrice", "2024-03-03")
    assert "Kept" in in_turn(client, keep, "lavatrice", "2025-03-03", "MediaWorld")
    assert db.execute("SELECT seller FROM purchases").fetchone() == (None,)  # a seller they didn't say


def test_weak_subscription_used_and_the_cancel_page_after_outside_text(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    _receipts(db, "info@account.netflix.com", "Netflix", ["12.99"] * 3)
    used = tools(rt(client).money_back, ProfileTurn(words="netflix lo uso ancora", lang="it-IT"))[
        "subscription_used"
    ]
    assert "Not kept" in in_turn(client, used, "Netflix", outside=True)
    assert db.execute("SELECT count(*) FROM subscription_marks").fetchone() == (0,)
    page = tools(rt(client).money_back, ProfileTurn(words="leggi le email di netflix", lang="it-IT"))[
        "show_cancel_page"
    ]
    assert "Not shown" in in_turn(client, page, "Netflix", outside=True)
    assert db.execute("SELECT count(*) FROM handoffs").fetchone() == (0,)


def test_weak_his_never_bills_holds_the_watchdog_s_task(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import email_rules

    async def rule() -> None:
        async with rt(client).pool.connection() as conn:
            await email_rules.add(conn, "kind", "bill_or_deadline", "it-IT", NOW)

    client.portal.call(rule)
    email(db, "e1", "Enel Energia", "bollette@enel.it", "La tua bolletta luce", OLD_BILL, category="other")
    scan(client)
    assert db.execute("SELECT status FROM bill_checks").fetchone() == ("flagged",) and tasks(db) == []


def test_weak_a_us_refund_never_has_a_sum_in_the_database(db) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO flight_claims (id, flight, day, scheduled_at, disruption, rule, status, reason, "
            "amount, "
            "currency, source, created_at, updated_at) VALUES (gen_random_uuid(), 'AA1', '2026-10-01', "
            "now(), "
            "'cancellation', 'us_dot', 'eligible', 'refund_due', 600, 'EUR', 'said', now(), now())"
        )
