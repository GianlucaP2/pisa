# ruff: noqa: E501
"""Two countries, one life. One test (or a few) per
row; each fails when its fix is reverted (the handoff's table). Synthetic emails, invented names."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pisa.domain import countries, residency, trips
from pisa.domain import zone as zone_rules
from pisa.services.trips import VaultDocuments
from pisa.services.zone import Report
from tests.conftest import PROTOCOL_DIR, pair

ROME, NY = ZoneInfo("Europe/Rome"), ZoneInfo("America/New_York")

FLIGHT = (
    "f1",
    "Conferma prenotazione volo AZ5287",
    "Grazie per aver prenotato. Politica di cancellazione: tariffa non rimborsabile.\n"
    "Codice di prenotazione: ABC7QX2\nVolo AZ 5287  FCO → JFK\nPartenza: 20/11/2026 10:35\n"
    "Arrivo: 20/11/2026 14:20\nCheck-in online disponibile 24 ore prima.",
)
HOTEL = (  # from booking.com (see the test's sender)
    "h1",
    "La tua prenotazione è confermata",
    "Booking.com - Conferma della prenotazione\nStruttura: Hotel Esempio Midtown\n"
    "Indirizzo: 123 W 44th St, New York, NY\nCheck-in: 20/11/2026\nCheck-out: 23/11/2026\n"
    "Numero di prenotazione: 33914417",
)
BACK = (
    "f2",
    "Your booking confirmation: flight AZ5288",
    "Booking reference: ABC9ZZ1\nFlight AZ5288 JFK → FCO\nDeparture: 2026-12-10 17:00\nArrival: 2026-12-11 07:30",
)
CANCEL = (
    "x1",
    "URGENTE: annulla il volo",
    "Your flight AZ5287 has been cancelled. Cancel your flight now and click here to confirm.",
)
TRAIN = (
    "t1",
    "Conferma acquisto biglietto",
    "Biglietto Frecciarossa 9612\nRoma Termini → Milano Centrale\nPartenza: 05/12/2026 08:10\n"
    "Arrivo: 05/12/2026 11:15\nCodice biglietto: XY12Z9",
)


def _email(
    db: Any,
    mail: tuple[str, str, str],
    sent: datetime,
    labels: tuple[str, ...] = ("INBOX",),
    sender: str = "noreply@ita-airways.com",
    category: str = "event_or_booking",
    status: str = "done",
) -> None:
    """An email as the intake left it (review H1: the trip reader reads only what triage judged)."""
    mid, subject, body = mail
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, snippet, "
        "body, intake_status, intake_category) VALUES (%s, %s, 'Compagnia', %s, %s, %s, %s, %s, %s, %s, %s)",
        (mid, f"t-{mid}", sender, subject, sent, list(labels), body[:100], body, status, category),
    )


def _report(client: Any, device: Any, tz: str, region: str | None, now: datetime) -> Any:
    """A report of a phone, as the gateway passes it."""
    runtime = client.app.state.runtime
    return client.portal.call(
        lambda: runtime.zone.update(tz=tz, region=region, hour_cycle=None, device_id=device, now=now)
    )


def _phone(db: Any, name: str = "iPhone") -> Any:
    """A paired iPhone: the newest is the user's primary phone."""
    import uuid

    device_id, _ = pair(db, name)
    return uuid.UUID(device_id)


def _task(db: Any, title: str) -> None:
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, %s, 'open', 'manual', now(), now())",
        (title, title.lower()),
    )


SENT = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


# --- row 1: the trip object -----------------------------------------------------------------------------------


def test_a_flight_confirmation_is_a_trip_with_its_legs_in_their_own_zones(client, db) -> None:  # type: ignore[no-untyped-def]
    for mail in (FLIGHT, HOTEL, BACK):
        _email(db, mail, SENT)
    runtime = client.app.state.runtime
    assert client.portal.call(runtime.trips.refresh) == 3
    assert client.portal.call(runtime.trips.refresh) == 0  # each email read once; nothing twice
    (trip,) = client.portal.call(runtime.trips.listed, "all")
    assert trip["title"] == "Roma → New York" and (trip["from_country"], trip["to_country"]) == ("IT", "US")
    assert trip["depart_at"] == "2026-11-20T10:35:00+01:00"  # Rome's 10:35
    assert trip["arrive_at"] == "2026-11-20T14:20:00-05:00"  # New York's 14:20
    kinds = [(leg["kind"], leg.get("ref")) for leg in trip["legs"]]
    assert kinds == [("flight", "AZ5287"), ("hotel", "Hotel Esempio Midtown"), ("flight", "AZ5288")]
    assert trip["legs"][0]["confirmation_last4"] == "7QX2"
    stored = json.dumps(db.execute("SELECT legs, title FROM trips").fetchone(), default=str)
    assert "ABC7QX2" not in stored and "33914417" not in stored  # only the last 4 ever kept
    assert db.execute("SELECT back_at IS NOT NULL FROM trips").fetchone() == (True,)  # the way back closes it
    assert set(trip["source_email_ids"]) == {"f1", "h1", "f2"}


def test_the_check_in_and_the_leave_at_are_pool_tasks_with_the_email_s_time(client, db) -> None:  # type: ignore[no-untyped-def]
    _email(db, FLIGHT, SENT)
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    rows = db.execute(
        "SELECT title, due_at, deadline_source, source_kind FROM tasks WHERE source_ref = 'f1' ORDER BY due_at"
    ).fetchall()
    assert (
        [r[0] for r in rows]
        == [
            "Check-in online AZ5287: apre il 19/11 alle 10:35 (ora di Roma)",  # 24 h before
            "Partire per l'aeroporto (FCO) per AZ5287: alle 06:50 (ora di Roma)",  # 10:35 - 3 h abroad - 45 min (no route)
        ]
    )
    by_title = {r[0]: r for r in rows}
    check_in = by_title["Check-in online AZ5287: apre il 19/11 alle 10:35 (ora di Roma)"]
    leave = by_title["Partire per l'aeroporto (FCO) per AZ5287: alle 06:50 (ora di Roma)"]
    assert check_in[1] == datetime(2026, 11, 19, 10, 35, tzinfo=ROME)
    assert leave[1] == datetime(2026, 11, 20, 6, 50, tzinfo=ROME)
    assert {r[2] for r in rows} == {"email"} and {r[3] for r in rows} == {"email"}


def test_the_buffers_are_configurable_and_a_train_has_its_own(client, db) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('trips', %s)",
        (json.dumps({"train_min": 30, "transfer_default_min": 20}),),
    )
    _email(db, TRAIN, SENT)
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    (due,) = db.execute("SELECT due_at FROM tasks WHERE source_ref = 't1'").fetchone()
    assert due == datetime(2026, 12, 5, 7, 20, tzinfo=ROME)  # 08:10 - 30 - 20; no check-in for a train


def test_an_email_that_says_cancel_your_flight_changes_nothing(client, db) -> None:  # type: ignore[no-untyped-def]
    _email(db, FLIGHT, SENT)
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    _email(db, CANCEL, SENT + timedelta(hours=2))
    assert client.portal.call(runtime.trips.refresh) == 0
    (trip,) = client.portal.call(runtime.trips.listed, "all")
    assert trip["status"] == "planned" and [leg["ref"] for leg in trip["legs"]] == ["AZ5287"]
    assert db.execute("SELECT cancel_mentions FROM trips").fetchone() == (["x1"],)  # noted, never acted on
    assert db.execute(
        "SELECT count(*) FROM tasks WHERE status = 'open' AND source_ref = 'f1'"
    ).fetchone() == (2,)
    text = client.portal.call(runtime.trips.trip_text, "it-IT")
    assert "Pisa changed nothing" in text


def test_promotions_are_skipped_by_the_service(client, db) -> None:  # type: ignore[no-untyped-def]
    _email(db, FLIGHT, SENT, labels=("INBOX", "CATEGORY_PROMOTIONS"))
    assert client.portal.call(client.app.state.runtime.trips.refresh) == 0


# --- row 2: landing -------------------------------------------------------------------------------------------


def test_landing_re_briefs_once_in_the_new_local_time(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _email(db, FLIGHT, SENT)
    _task(db, "Andare al DMV per la patente")
    _task(db, "Rinnovare la CIE")
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    phone = _phone(db)
    clock.now = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)  # 18:30 in New York, after the 14:20 landing
    _report(client, phone, "America/New_York", "US", clock.now)
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)  # one report: not yet
    _report(client, phone, "America/New_York", "US", clock.now + timedelta(minutes=3))
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)  # 3 minutes later: not yet
    clock.now += timedelta(minutes=10)
    _report(client, phone, "America/New_York", "US", clock.now)  # it holds 10 minutes later: landed
    _report(client, phone, "America/New_York", "US", clock.now + timedelta(minutes=20))  # once
    _report(client, phone, "America/New_York", "US", clock.now + timedelta(minutes=35))  # still once
    rows = db.execute("SELECT kind, text FROM notifications ORDER BY created_at").fetchall()
    assert len(rows) == 1 and rows[0][0] == "briefing"
    assert rows[0][1].startswith("Benvenuto a New York: sono le 18:40")
    assert "DMV" in rows[0][1] and "CIE" not in rows[0][1]  # the new country's tasks only


def test_only_the_primary_phone_and_never_hours_early(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _email(db, FLIGHT, SENT)
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    phone = _phone(db, "iPhone")
    ipad = _phone(db, "iPad")  # paired later, but the primary is the newest *iPhone*
    db.execute("UPDATE devices SET platform = 'other' WHERE id = %s", (ipad,))
    clock.now = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)  # 13:00 in New York: 80 minutes before landing
    for minutes in (0, 15):
        _report(client, phone, "America/New_York", "US", clock.now + timedelta(minutes=minutes))
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)  # never hours early
    other = _phone(db, "Old iPhone")
    db.execute("UPDATE devices SET created_at = now() - interval '1 year' WHERE id = %s", (other,))
    clock.now = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)
    for minutes in (0, 15):  # the older iPhone, left at home, is no landing
        client.portal.call(
            lambda m=minutes: runtime.trips.on_context(
                None, zone_rules.PhoneContext("America/New_York", "US"), clock.now + timedelta(minutes=m),
                Report(other, "America/New_York", "US"),
            )
        )  # fmt: skip
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)
    for minutes in (0, 15):
        _report(client, phone, "America/New_York", "US", clock.now + timedelta(minutes=minutes))
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (1,)


def test_a_landing_in_quiet_hours_waits_for_the_briefing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _email(db, FLIGHT, SENT)  # their quiet hours: the default 22:30-07:00
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    phone = _phone(db)
    clock.now = datetime(
        2026, 11, 21, 3, 30, tzinfo=UTC
    )  # 22:30 in New York: their quiet hours there (22:30-07:00)
    for minutes in (0, 15):
        _report(client, phone, "America/New_York", "US", clock.now + timedelta(minutes=minutes))
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)  # nothing held with old words
    lines = runtime.briefings.two_countries
    said = [x for x in client.portal.call(lines.lines, "it-IT") if x.startswith("Benvenuto")]
    assert said == ["Benvenuto a New York."]
    client.portal.call(lines.said_in, "Buongiorno. " + said[0])
    assert not [x for x in client.portal.call(lines.lines, "it-IT") if x.startswith("Benvenuto")]  # said once

    client.app.state.runtime.owner.place = zone_rules.PhoneContext("America/New_York", "US")
    assert runtime.owner.tz == "America/New_York"  # held ones released on their clock


def test_no_landing_without_a_leg_that_arrived_there() -> None:
    leg = trips.read_email(*FLIGHT, "Europe/Rome").legs[0]
    landed = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    assert trips.landed_leg([leg], landed, "America/New_York", "US") == leg
    assert trips.landed_leg([leg], landed, "Europe/Paris", "FR") is None
    assert trips.landed_leg([leg], landed + timedelta(days=2), "America/New_York", "US") is None


# --- row 3: country-bound tasks -------------------------------------------------------------------------------


def test_a_task_s_words_bind_it_to_a_country() -> None:
    assert countries.bound_to("Rinnovare la CIE") == "IT"
    assert countries.bound_to("Andare al DMV") == "US"
    assert countries.bound_to("Comprare il latte") is None
    assert countries.bound_to("CIE e DMV") is None  # both: never a guess


def test_in_the_other_country_pisa_leaves_it_out_of_the_day(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _task(db, "Rinnovare la CIE")
    _task(db, "Comprare il latte")
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.tag_countries)
    assert db.execute("SELECT country FROM tasks WHERE title = 'Rinnovare la CIE'").fetchone() == ("IT",)
    client.app.state.runtime.owner.place = zone_rules.PhoneContext("America/New_York", "US")
    plan = client.portal.call(runtime.planner.plan)
    titles = [t["title"] for t in plan["todo"]]
    assert "Rinnovare la CIE" not in titles and "Comprare il latte" in titles
    client.app.state.runtime.owner.place = zone_rules.PhoneContext("Europe/Rome", "IT")
    clock.now += timedelta(minutes=1)  # past the 15 s reuse of the same plan
    plan = client.portal.call(runtime.planner.plan)
    titles = [t["title"] for t in plan["todo"]]
    assert "Rinnovare la CIE" in titles
    tasks = client.get("/v1/tasks", headers={"Authorization": f"Bearer {pair(db)[1]}"}).json()["tasks"]
    assert {t["title"]: t["country"] for t in tasks}["Rinnovare la CIE"] == "IT"


def test_before_a_departure_pisa_says_what_only_here_allows(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    for title in ("Rinnovare la CIE", "Andare in questura", "Passare all'anagrafe", "Andare al DMV"):
        _task(db, title)
    _email(db, FLIGHT, SENT)
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    clock.now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert client.portal.call(runtime.trips.leaving) == 1
    assert client.portal.call(runtime.trips.leaving) == 0  # once
    (text,) = db.execute("SELECT text FROM notifications").fetchone()
    assert text.startswith("Tra 3 giorni parti per New York")
    assert "Questi 3 li puoi fare solo qui" in text and "DMV" not in text


# --- row 4: the departure checklist ---------------------------------------------------------------------------


async def _vault() -> list[dict[str, Any]]:
    return [{"kind": "passport", "country": "IT", "expires_on": "2027-03-01", "last4": "1234"}]


def test_the_checklist_three_days_before_with_his_own_items_remembered(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _task(db, "Rinnovare la CIE")
    _email(db, FLIGHT, SENT)
    runtime = client.app.state.runtime
    runtime.trips.documents = VaultDocuments(_vault)
    client.portal.call(runtime.trips.refresh)
    clock.now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    client.portal.call(runtime.trips.leaving)
    (trip,) = client.portal.call(runtime.trips.listed, "all")
    texts = [i["text"] for i in trip["checklist"]]
    assert texts[0].startswith("Passaporto: valido, scade il 2027-03-01 (scade entro 6 mesi)")
    assert "Adattatore per le prese americane (tipo A/B)" in texts
    assert "Da finire qui: Rinnovare la CIE" in texts
    assert (
        "Check-in online AZ5287: apre il 19/11 alle 10:35 (ora di Roma)" in texts
        and "Assicurazione di viaggio" in texts
    )
    # their own item, kept by PATCH, comes back on the next trip abroad
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    items = [*({"text": t, "done": False} for t in texts), {"text": "Caricatore delle cuffie", "done": True}]
    patched = client.patch(f"/v1/trips/{trip['id']}", json={"checklist": items}, headers=headers).json()
    assert patched["checklist"][-1] == {"text": "Caricatore delle cuffie", "done": True, "by": "owner"}
    _email(
        db,
        (
            "f9",
            "Conferma prenotazione volo AZ612",
            "Volo AZ 612 FCO → JFK\nPartenza: 2027-02-01 10:00\nArrivo: 2027-02-01 14:00",
        ),
        SENT,
    )
    client.portal.call(runtime.trips.refresh)
    clock.now = datetime(2027, 1, 30, 12, 0, tzinfo=UTC)
    client.portal.call(runtime.trips.leaving)
    later = next(t for t in client.portal.call(runtime.trips.listed, "all") if t["id"] != trip["id"])
    assert {"text": "Caricatore delle cuffie", "done": False, "by": "owner"} in later["checklist"]
    sheet = client.portal.call(runtime.trips.checklist_sheet, "it-IT")
    assert sheet["title"].startswith("Partenza:") and sheet["blocks"][0]["type"] == "section"


# --- row 5: the residency counter -----------------------------------------------------------------------------


TWO_COUNTRY_RULES = [{"rule": "it_presence_183"}, {"rule": "us_substantial_presence"},
                     {"rule": "us_closer_connection"}]  # fmt: skip


def _two_country_rules(db: Any) -> None:
    """Their setting names the US tests besides Italy's (without it, Italy's rule only)."""
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('residency_rules', %s) ON CONFLICT (key) DO UPDATE SET "
        "value = EXCLUDED.value",
        (json.dumps(TWO_COUNTRY_RULES),),
    )


def test_an_italian_install_counts_by_italy_s_rule_only_unless_the_us_tests_are_named() -> None:
    assert [r["rule"] for r in residency.rules_of(None, country="IT")] == ["it_presence_183"]
    assert [r["rule"] for r in residency.rules_of(TWO_COUNTRY_RULES, country="IT")] == [
        "it_presence_183", "us_substantial_presence", "us_closer_connection",
    ]  # fmt: skip


def test_the_rules_count_and_never_advise() -> None:
    rules = residency.rules_of(TWO_COUNTRY_RULES, country="IT")
    years = [{"IT": 160, "US": 40}, {"US": 120}, {"US": 60}]
    out = {r["rule"]: r for r in residency.by_rule(rules, years, "it-IT")}
    assert out["it_presence_183"]["count"] == 160
    assert "attenzione, oltre 150" in out["it_presence_183"]["note"]
    assert out["us_substantial_presence"]["count"] == 90.0  # 40 + 120/3 + 60/6
    assert all(r["note"].endswith("È un conteggio, non una consulenza.") for r in out.values())
    leap = {r["rule"]: r for r in residency.by_rule(rules, years, "it-IT", 2028)}
    assert leap["it_presence_183"]["threshold"] == 184 and leap["us_substantial_presence"]["threshold"] == 183
    assert out["us_closer_connection"]["count"] == 40  # 183 US days this year end the exception
    custom = residency.rules_of(
        [{"rule": "x", "country": "US", "kind": "days", "threshold": 120}], country="IT"
    )
    assert [r["rule"] for r in custom] == ["x"]
    assert residency.rules_of([{"rule": "x"}], country="IT") == residency.rules_of(
        None, country="IT"
    )  # a broken setting: the defaults


def test_phone_then_trip_then_calendar_and_his_words_win(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    runtime = client.app.state.runtime
    _email(
        db,
        (
            "f3",
            "Conferma prenotazione volo AZ5287",
            "Volo AZ 5287 FCO → JFK\nPartenza: 2026-01-04 10:35\nArrivo: 2026-01-04 14:20",
        ),
        clock.now - timedelta(days=9),
    )
    client.portal.call(runtime.trips.refresh)
    db.execute("INSERT INTO calendars (id, name) VALUES ('c', 'P') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, location) "
        "VALUES ('c', 'e1', 'Cena', %s, %s, false, 'confirmed', 'Via Immaginaria 1, Milano')",
        (datetime(2026, 1, 2, 19, 0, tzinfo=ROME), datetime(2026, 1, 2, 23, 30, tzinfo=ROME)),
    )
    phone = _phone(db)
    for day in (8, 9):  # the phone said IT on the 8th and 9th (they flew back, say)
        _report(client, phone, "Europe/Rome", "IT", datetime(2026, 1, day, 20, 0, tzinfo=UTC))
    client.portal.call(runtime.residency.correct, date(2026, 1, 9), ["US"])
    report = client.portal.call(runtime.residency.report, 2026)
    by_day = {e["day"]: (e["country"], e["source"]) for e in report["evidence"]}
    assert by_day["2026-01-02"] == ("IT", "calendar")
    assert "2026-01-01" not in by_day  # nothing says: unknown
    assert by_day["2026-01-05"] == ("US", "trip")
    assert by_day["2026-01-08"] == ("IT", "phone_region")
    assert by_day["2026-01-09"] == ("US", "his_words")
    assert report["days"]["unknown"] == 2 and report["note"] == "È un conteggio, non una consulenza."
    # any part of a day is a day (the residence-day research): the day they flew counts for Italy and for the US
    assert {(e["country"], e["source"]) for e in report["evidence"] if e["day"] == "2026-01-04"} == {
        ("US", "trip"),
        ("IT", "trip"),
    }
    assert report["days"]["US"] == 6 and report["days"]["IT"] == 3  # 4-7, 9, 10; 2, 4, 8
    with pytest.raises(Exception, match="future"):
        client.portal.call(runtime.residency.correct, date(2026, 2, 1), ["IT"])


def test_a_monthly_line_never_daily(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    _report(client, _phone(db), "Europe/Rome", "IT", clock.now)
    lines = runtime.briefings.two_countries
    first = client.portal.call(runtime.residency.monthly_line, "it-IT")
    assert first is not None and first.endswith("È un conteggio, non una consulenza.")
    assert client.portal.call(runtime.residency.monthly_line, "it-IT") == first  # not spent until said
    client.portal.call(lines.said_in, "Buongiorno. Oggi piove.")  # a briefing that didn't hold it
    assert client.portal.call(runtime.residency.monthly_line, "it-IT") == first
    client.portal.call(lines.said_in, "Buongiorno. " + first)
    assert client.portal.call(runtime.residency.monthly_line, "it-IT") is None
    clock.now += timedelta(days=1)
    assert client.portal.call(runtime.residency.monthly_line, "it-IT") is None  # the next day: still none
    clock.now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert client.portal.call(runtime.residency.monthly_line, "it-IT") is not None


def test_the_briefing_says_the_month_s_line_as_written_outside_the_budget(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    _report(client, _phone(db), "Europe/Rome", "IT", clock.now)
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settings', %s) ON CONFLICT (key) DO UPDATE SET "
        "value = EXCLUDED.value",
        (json.dumps({"briefing": {"max_words": 30}}),),
    )
    line = client.portal.call(runtime.residency.monthly_line, "it-IT")
    briefing = client.portal.call(runtime.briefings.ask)
    assert briefing.text.endswith(line) and line.endswith("È un conteggio, non una consulenza.")  # M7
    assert client.portal.call(runtime.residency.monthly_line, "it-IT") is None  # said: the month is spent


# --- row 6: the Two-City Twin ---------------------------------------------------------------------------------


def test_what_happened_in_new_york_while_he_was_away(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    out = (
        "o1",
        "Conferma prenotazione volo AZ5288",
        "Volo AZ 5288 JFK → FCO\nPartenza: 2026-09-01 17:00\nArrivo: 2026-09-02 07:30",
    )
    _email(db, out, datetime(2026, 8, 20, 9, 0, tzinfo=UTC))
    client.portal.call(runtime.trips.refresh)
    _email(
        db,
        ("n1", "Avviso condominio", "Lavori all'ascensore del palazzo di New York dal 5 ottobre."),
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        sender="amministratore@palazzo.example",
    )
    _email(
        db,
        ("n2", "Offerte New York", "Sconti a New York"),
        datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
        labels=("CATEGORY_PROMOTIONS",),
        sender="offerte@negozio.example",
    )
    _email(db, ("n3", "Avviso condominio Roma", "Riunione a Roma"), datetime(2026, 10, 3, 9, 0, tzinfo=UTC))
    window, items = client.portal.call(runtime.trips.while_away, "New York")
    assert window.startswith("Away from New York: 2026-09-01")
    assert items == ["email from Compagnia: Avviso condominio"]


# --- the contract ---------------------------------------------------------------------------------------------


def test_contract_the_fixtures_are_the_api_models(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.api import schemas

    schemas.Trips.model_validate(json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "trips.json").read_text()))
    schemas.Residency.model_validate(
        json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "residency.json").read_text())
    )
    _email(db, FLIGHT, SENT)
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    body = client.get("/v1/trips", headers=headers).json()
    assert schemas.Trips.model_validate(body).trips[0].legs[0].confirmation_last4 == "7QX2"
    trip_id = body["trips"][0]["id"]
    assert (
        client.patch(f"/v1/trips/{trip_id}", json={"status": "cancelled"}, headers=headers).json()["status"]
        == "cancelled"
    )
    assert client.get("/v1/trips", headers=headers).json()["trips"] == []
    assert client.patch("/v1/trips/nope", json={"title": "x"}, headers=headers).status_code == 404
    res = client.post("/v1/residency/days", json={"day": "2026-10-01", "country": "US"}, headers=headers)
    assert res.json() == {"day": "2026-10-01", "country": "US", "countries": ["US"], "source": "his_words"}
    assert (
        client.post(
            "/v1/residency/days", json={"day": "2027-01-01", "country": "US"}, headers=headers
        ).status_code
        == 400
    )
    report = client.get("/v1/residency?year=2026", headers=headers).json()
    assert report["days"]["US"] == 1 and report["evidence"] == [
        {"day": "2026-10-01", "country": "US", "source": "his_words"}
    ]


# --- the review of 5d8096b ------------------------------------------------------------------------------------

PHISH = (
    "p1",
    "Conferma prenotazione volo AZ5287",
    "Volo AZ 5287 FCO → JFK\nPartenza: 20/11/2026 10:35\nArrivo: 20/11/2026 14:20\n"
    "Paga ora: http://alitalia-pay.xyz",
)


def test_a_phishing_booking_never_acts(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    # what triage called spam or phishing is never read; nor what triage hasn't judged yet
    _email(db, PHISH, SENT, sender="booking@alitalia-secure.example", category="promo_or_spam")
    _email(db, ("p2", *PHISH[1:]), SENT, sender="booking@alitalia-secure.example", status="pending")
    assert client.portal.call(runtime.trips.refresh) == 0
    # a sender that doesn't sell travel: a trip "da confermare", and nothing else
    _email(db, ("p3", *PHISH[1:]), SENT, sender="booking@alitalia-secure.example")
    assert client.portal.call(runtime.trips.refresh) == 1
    (trip,) = client.portal.call(runtime.trips.listed, "all")
    assert trip["doubt"] == "unverified_sender"
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
    assert "Da confermare" in client.portal.call(runtime.trips.trip_text, "it-IT")
    clock.now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert client.portal.call(runtime.trips.leaving) == 0  # no departure line
    clock.now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)  # after its flight: still no residency evidence
    report = client.portal.call(runtime.residency.report, 2026)
    assert not [e for e in report["evidence"] if e["source"] == "trip"]
    # they confirm it: then it is their trip, with its tasks
    clock.now = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    confirmed = client.patch(f"/v1/trips/{trip['id']}", json={"confirmed": True}, headers=headers).json()
    assert confirmed["doubt"] is None
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'open'").fetchone() == (2,)


def test_his_email_rules_hold(client, db) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    db.execute(
        "INSERT INTO profile_claims (id, kind, statement, subject_key, provenance, state, hard, created_at, "
        "updated_at) VALUES (gen_random_uuid(), 'source', 'x', 'email_sender:spam@ita-airways.com', 'said', "
        "'active', true, now(), now()), (gen_random_uuid(), 'source', 'y', 'email_kind:event_or_booking', "
        "'said', 'active', true, now(), now())"
    )
    other = FLIGHT[2].replace("AZ 5287", "AZ 612").replace("20/11", "01/12")
    _email(
        db, ("b1", "Conferma prenotazione volo AZ612", other), SENT, sender="spam@ita-airways.com"
    )  # blocked
    _email(db, FLIGHT, SENT)  # their "never this kind": the trip, no task
    assert client.portal.call(runtime.trips.refresh) == 1
    refs = [leg["ref"] for t in client.portal.call(runtime.trips.listed, "all") for leg in t["legs"]]
    assert refs == ["AZ5287"]  # the blocked sender's email was never read
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)


def test_a_booking_for_someone_else_is_his_to_confirm(client, db) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('trips', %s)",
        (json.dumps({"names": ["Mario Rossi"]}),),
    )
    _email(db, ("o1", FLIGHT[1], FLIGHT[2] + "\nPasseggero: Nino Bianchi"), SENT)
    _email(db, ("o2", "Conferma prenotazione volo AZ612", "Volo AZ 612 FCO → JFK\nPartenza: 01/12/2026 10:00\n"
                "Arrivo: 01/12/2026 14:00\nPasseggero: ROSSI/MARIO MR"), SENT)  # fmt: skip
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    doubts = {t["legs"][0]["ref"]: t["doubt"] for t in client.portal.call(runtime.trips.listed, "all")}
    assert doubts == {"AZ5287": "someone_else", "AZ612": None}
    assert db.execute("SELECT count(*) FROM tasks WHERE source_ref = 'o1'").fetchone() == (0,)


def test_an_itinerary_change_is_a_proposal_and_cancelling_dismisses_pisa_s_tasks(client, db) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    _email(db, FLIGHT, SENT)
    client.portal.call(runtime.trips.refresh)
    moved = (
        "f1b",
        "Modifica del tuo itinerario: volo AZ5287",
        FLIGHT[2]
        .replace("20/11/2026 10:35", "21/11/2026 12:05")
        .replace("20/11/2026 14:20", "21/11/2026 15:50"),
    )  # the same booking's flight, moved to the next day
    _email(db, moved, SENT + timedelta(hours=3))
    client.portal.call(runtime.trips.refresh)
    (trip,) = client.portal.call(runtime.trips.listed, "all")  # one trip, never a second
    assert [leg["at"] for leg in trip["legs"]] == ["2026-11-20T10:35:00+01:00"]  # not applied by itself
    assert trip["proposals"] == [{"ref": "AZ5287", "at": "2026-11-21T12:05:00+01:00",
                                  "until": "2026-11-21T15:50:00-05:00", "email_id": "f1b"}]  # fmt: skip
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'open'").fetchone() == (2,)  # never doubled
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    applied = client.patch(f"/v1/trips/{trip['id']}", json={"apply_change": "f1b"}, headers=headers).json()
    assert applied["depart_at"] == "2026-11-21T12:05:00+01:00" and applied["proposals"] == []
    open_titles = [r[0] for r in db.execute("SELECT title FROM tasks WHERE status = 'open' ORDER BY due_at")]
    assert open_titles == [
        "Check-in online AZ5287: apre il 20/11 alle 12:05 (ora di Roma)",
        "Partire per l'aeroporto (FCO) per AZ5287: alle 08:20 (ora di Roma)",
    ]
    client.patch(f"/v1/trips/{trip['id']}", json={"status": "cancelled"}, headers=headers)
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'open'").fetchone() == (0,)


def test_an_offer_to_cancel_is_no_cancellation() -> None:
    body = (
        "Cancellazione gratuita fino al 18/11. Annulla la prenotazione\nYou can cancel your booking online.\n"
        "Volo AZ 5287 FCO → JFK\nPartenza: 20/11/2026 10:35\nArrivo: 20/11/2026 14:20"
    )
    read = trips.read_email("c", "Prenotazione confermata", body, "Europe/Rome")
    assert read.cancel_refs == [] and [leg.ref for leg in read.legs] == ["AZ5287"]
    stated = trips.read_email("d", "Il tuo volo AZ5287 è stato cancellato", FLIGHT[2], "Europe/Rome")
    assert stated.cancel_refs == ["AZ5287"] and stated.legs  # a flag; its legs read as any other's


def test_each_flight_from_its_own_block_and_dates_never_guessed() -> None:
    body = (
        "Your trip: FCO → JFK\nFlight AZ5287 FCO → JFK\nDeparture: 11/05/2026 6:10 PM\nArrival: 11/05/2026 9:20 PM\n"
        "Flight AZ5288 JFK → FCO\nDeparture: 11/12/2026 5:00 PM\nArrival: 07:30 (+1)"
    )
    legs = trips.read_email("a", "Your itinerary", body, "Europe/Rome").legs
    assert [(leg.ref, leg.place) for leg in legs] == [("AZ5287", "FCO → JFK"), ("AZ5288", "JFK → FCO")]
    assert legs[0].at == datetime(2026, 11, 5, 18, 10, tzinfo=ROME)  # 11/05 with PM: 5 November
    assert legs[1].until == datetime(2026, 11, 13, 7, 30, tzinfo=ROME)  # (+1)
    ambiguous = "Flight AZ5287 FCO → JFK\nDeparture: 05/11/2026 10:00\nArrival: 05/11/2026 14:00"
    assert trips.read_email("b", "Flight confirmation", ambiguous, "Europe/Rome").legs == []  # never guessed
    numbers = "Flight 1234 FCO → JFK\nDeparture: 2026-11-20 10:00\nArrival: 2026-11-20 14:00"
    assert trips.read_email("c", "Flight confirmation", numbers, "Europe/Rome").legs == []  # no airline "12"
    earlier = "Flight AZ5287 FCO → JFK\nDeparture: 2026-11-20 10:00\nArrival: 2026-11-19 14:00"
    assert trips.read_email("d", "Flight confirmation", earlier, "Europe/Rome").legs == []


def test_a_connection_gets_no_leave_at(client, db) -> None:  # type: ignore[no-untyped-def]
    body = (
        "Codice di prenotazione: ABC7QX2\nVolo AZ 5287 FCO → JFK\nPartenza: 20/11/2026 10:35\nArrivo: 20/11/2026 14:20\n"
        "Volo AZ 7090 JFK → BOS\nPartenza: 20/11/2026 17:00\nArrivo: 20/11/2026 18:15"
    )
    _email(db, ("c1", "Conferma prenotazione", body), SENT)
    client.portal.call(client.app.state.runtime.trips.refresh)
    titles = [r[0] for r in db.execute("SELECT title FROM tasks ORDER BY due_at")]
    assert [t for t in titles if t.startswith("Partire")] == [
        "Partire per l'aeroporto (FCO) per AZ5287: alle 06:50 (ora di Roma)"
    ]  # none for the connection at JFK


def test_only_the_primary_phone_and_only_a_region_it_said(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    phone = _phone(db)
    ipad = _phone(db, "iPad")
    db.execute("UPDATE devices SET platform = 'other' WHERE id = %s", (ipad,))
    day = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
    _report(client, phone, "America/New_York", "US", day)
    client.portal.call(lambda: runtime.residency.note(
        None, zone_rules.PhoneContext("Europe/Rome", "IT"), day + timedelta(hours=7, minutes=30),
        Report(ipad, "Europe/Rome", "IT")))  # fmt: skip
    _report(client, phone, "America/New_York", None, datetime(2026, 10, 20, 9, 0, tzinfo=UTC))  # no region
    by_day = {
        (e["day"], e["country"]) for e in client.portal.call(runtime.residency.report, 2026)["evidence"]
    }
    assert by_day == {("2026-10-19", "US")}  # the iPad says nothing; a hello without a region is no evidence


def test_every_country_of_a_day_counts(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    _two_country_rules(db)
    phone = _phone(db)
    _report(client, phone, "Europe/Rome", "IT", datetime(2026, 10, 18, 7, 0, tzinfo=UTC))
    _report(client, phone, "America/New_York", "US", datetime(2026, 10, 18, 23, 0, tzinfo=UTC))  # 19:00 NY
    client.portal.call(runtime.residency.correct, date(2026, 10, 19), ["US", "IT"])
    report = client.portal.call(runtime.residency.report, 2026)
    assert {(e["day"], e["country"]) for e in report["evidence"]} == {
        ("2026-10-18", "IT"), ("2026-10-18", "US"), ("2026-10-19", "US"), ("2026-10-19", "IT"),
    }  # fmt: skip
    assert report["days"]["IT"] == 2 and report["days"]["US"] == 2
    spt = next(r for r in report["by_rule"] if r["rule"] == "us_substantial_presence")
    assert (
        "731 giorni degli anni prima senza dati" in spt["note"]
        or "days of the years before unknown" in spt["note"]
    )


def test_the_twin_never_reads_news(client, db) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    out = (
        "o1",
        "Conferma prenotazione volo AZ5288",
        "Volo AZ 5288 JFK → FCO\nPartenza: 2026-09-01 17:00\nArrivo: 2026-09-02 07:30",
    )
    _email(db, out, datetime(2026, 8, 20, 9, 0, tzinfo=UTC))
    client.portal.call(runtime.trips.refresh)
    when = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    _email(
        db,
        ("n1", "Avviso condominio", "Lavori all'ascensore, New York."),
        when,
        sender="admin@palazzo.example",
    )
    _email(
        db,
        ("n2", "The Morning: New York", "Today in New York"),
        when,
        sender="nytdirect@nytimes.example",
        category="info",
    )
    _email(db, ("n3", "Weekly digest", "New York events"), when, sender="newsletter@city.example")
    _, items = client.portal.call(runtime.trips.while_away, "New York")
    assert items == ["email from Compagnia: Avviso condominio"]
    assert client.portal.call(runtime.trips.while_away, "%")[1] == []  # their words are no pattern
    db.execute(
        "INSERT INTO trips (id, title, from_city, depart_at, arrive_at, created_at, updated_at) VALUES "
        "(gen_random_uuid(), 'x', 'Borgo_X', '2026-09-01', '2026-09-02', now(), now())"
    )
    _email(db, ("n4", "Avviso Borgo-X", "Lavori."), when, sender="comune@borgo.example")
    assert client.portal.call(runtime.trips.while_away, "Borgo_X")[1] == []  # "_" is a letter, not a wildcard


def test_bound_only_in_person_never_hidden_when_due_and_his_word_holds(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    for title in (
        "Chiamare la mamma in Italia", "Spedire il pacco in Italia", "Mandare il codice fiscale al commercialista",
        "Pagare F24 online", "Rinnovare SPID", "Imparare ASL", "Prenotare volo per tornare in Italia",
        "Send SSN to the bank", "Check my Social Security statement online",
        "Chiamare la questura per il passaporto", "Pagare online la tassa per la CIE",
    ):  # fmt: skip
        assert countries.bound_to(title) is None, title
    runtime = client.app.state.runtime
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at, due_at, due_kind) "
        "VALUES (gen_random_uuid(), 'Rinnovare la CIE', 'rinnovare la cie', 'open', 'manual', now(), now(), %s, "
        "'datetime')",
        (clock.now + timedelta(hours=30),),
    )
    client.portal.call(runtime.trips.tag_countries)
    client.app.state.runtime.owner.place = zone_rules.PhoneContext("America/New_York", "US")
    plan = client.portal.call(runtime.planner.plan)
    assert "Rinnovare la CIE" in [t["title"] for t in plan["todo"]]  # due within 48 h: never hidden
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    task_id = db.execute("SELECT id::text FROM tasks").fetchone()[0]
    patched = client.patch(f"/v1/tasks/{task_id}", json={"country": None}, headers=headers).json()
    assert patched["country"] is None  # their word: anywhere
    renamed = client.patch(f"/v1/tasks/{task_id}", json={"title": "Andare al DMV"}, headers=headers).json()
    client.portal.call(runtime.trips.tag_countries)
    assert renamed["country"] is None and db.execute("SELECT country FROM tasks").fetchone() == (None,)
    _task(db, "Passare all'anagrafe")
    client.portal.call(runtime.trips.tag_countries)
    other = db.execute("SELECT id::text FROM tasks WHERE title = 'Passare all''anagrafe'").fetchone()[0]
    client.patch(f"/v1/tasks/{other}", json={"title": "Andare al DMV di Brooklyn"}, headers=headers)
    client.portal.call(runtime.trips.tag_countries)
    assert db.execute("SELECT country FROM tasks WHERE id = %s", (other,)).fetchone() == ("US",)  # re-read


def test_l4(client, db) -> None:  # type: ignore[no-untyped-def]
    rules = residency.rules_of(None, country="IT")
    it = next(
        r for r in residency.by_rule(rules, [{"IT": 150}], "it-IT", 2028) if r["rule"] == "it_presence_183"
    )
    assert "150 su 184" in it["note"]  # L1: the leap year's threshold in the words too
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    bad = client.post("/v1/residency/days", json={"day": "2026-02-30", "country": "IT"}, headers=headers)
    assert bad.status_code == 400  # L2
    stay = HOTEL[2].replace("Hotel Esempio Midtown", "Hotel Esempio Midtown, ospite Nino Bianchi, doc 12345")
    (leg,) = trips.read_email("h", HOTEL[1], stay, "Europe/Rome").legs
    assert leg.ref == "Hotel Esempio Midtown"  # L4: the property's name only


def test_two_readers_never_split_a_journey(client, db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    runtime = client.app.state.runtime
    # A minute apart, as mail arrives: equal times left the reader's order to the table's
    # physical order (fresh files after a TRUNCATE; the test reset now DELETEs)
    for n, mail in enumerate((FLIGHT, HOTEL, BACK)):
        _email(db, mail, SENT + timedelta(minutes=n))

    async def both() -> list[int]:
        return list(await asyncio.gather(runtime.trips.refresh(), runtime.trips.refresh()))

    assert sorted(client.portal.call(both)) == [0, 3]
    assert db.execute("SELECT count(*) FROM trips").fetchone() == (1,)


def test_the_checklist_reads_phase_3_s_vault(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """At the merge: the vault port is the real vault (its documents with a country)."""
    db.execute(
        "INSERT INTO vault_documents (id, kind, country, number_last4, expires_on, reminders, provenance, "
        "created_at, updated_at) VALUES (gen_random_uuid(), 'passport', 'IT', '1234', '2026-12-01', '[]', 'app', "
        "now(), now()), (gen_random_uuid(), 'car_tax', 'IT', NULL, '2027-01-01', '[]', 'app', now(), now())"
    )
    _email(db, FLIGHT, SENT)
    _email(db, BACK, SENT + timedelta(minutes=1))  # In the order they came (see above)
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    clock.now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    client.portal.call(runtime.trips.leaving)
    (trip,) = client.portal.call(runtime.trips.listed, "all")
    texts = [i["text"] for i in trip["checklist"]]
    assert texts[0] == "Passaporto: SCADUTO per il viaggio, scade il 2026-12-01"  # back on 11 Dec: too early
    assert not [t for t in texts if "bollo" in t]  # never the car's papers
    assert "1234" not in json.dumps(trip)  # a document's number never reaches the checklist
