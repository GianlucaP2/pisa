# ruff: noqa: E501
"""What is built from many emails doesn't depend on
the order they are read in. One booking often sends its confirmations in the same second; with an equal
`sent_at` Postgres may return them in any order. Synthetic emails, invented names."""

import itertools
from datetime import UTC, date, datetime
from typing import Any

import pytest

from pisa.domain import trips as rules
from tests.conftest import pair
from tests.test_money import a_trip, email, rome
from tests.test_money import owner as _owner
from tests.test_two_countries import BACK, FLIGHT, HOTEL, SENT, _email

ROW1 = (FLIGHT, HOTEL, BACK)
LEGS = [("flight", "AZ5287"), ("hotel", "Hotel Esempio Midtown"), ("flight", "AZ5288")]


def _legs(mails: tuple[tuple[str, str, str], ...]) -> list[rules.Leg]:
    return [
        leg
        for mid, subject, body in mails
        for leg in rules.read_email(mid, subject, body, "Europe/Rome").legs
    ]


def _place(order: list[rules.Leg]) -> list[rules.Shape]:
    """What `refresh` does with legs read one by one: place each, then merge (pure, no database)."""
    trips: list[rules.Shape] = []
    for n, leg in enumerate(order):
        trip = rules.place_leg(trips, leg)
        if trip is None:
            trip = rules.new_shape(f"t{n}", leg)
            trips.append(trip)
        else:
            rules.attach(trip, leg)
        while (found := rules.merge(trips, trip)) is not None:
            shape, gone = found
            trips = [shape if t.id == shape.id else t for t in trips if t.id != gone.id]
            trip = shape
    return trips


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_order_domain_three_confirmations_in_any_order_are_one_trip(order: tuple[int, ...]) -> None:
    legs = _legs(ROW1)
    (trip,) = _place([legs[i] for i in order])
    assert [(x.kind, x.ref) for x in trip.legs] == LEGS
    assert rules.title(trip) == "Roma → New York" and trip.back_at is not None


def test_order_domain_two_journeys_stay_two() -> None:
    """A merge needs the dates and places to fit: a trip to Milan and the New York trip stay apart."""
    from tests.test_two_countries import TRAIN

    legs = _legs((*ROW1, TRAIN))
    for order in itertools.permutations(legs):
        trips = _place(list(order))
        assert sorted(len(t.legs) for t in trips) == [1, 3]


def test_order_domain_a_trip_he_changed_never_goes_into_another() -> None:
    hotel, back, flight = _legs((HOTEL, BACK, FLIGHT))
    his = rules.new_shape("his", hotel)
    his.by_owner = True
    out = rules.new_shape("out", flight)
    shape, gone = rules.merge([his, out], out) or (None, None)
    assert shape is not None and shape.id == "his" and gone is out and shape.by_owner
    other = rules.new_shape("other", back)
    other.by_owner = True
    assert rules.merge([his, other], his) is None  # two trips they changed: never one of them dropped


def test_order_domain_one_booking_is_one_trip_even_far_from_the_dates() -> None:
    """Match on the booking reference where present: the same code's outbound and return are one trip."""
    out = rules.read_email(
        "a1", "Conferma prenotazione", "Codice di prenotazione: QWE4RT5\nVolo AZ 5287  FCO → JFK\n"
        "Partenza: 20/11/2026 10:35\nArrivo: 20/11/2026 14:20", "Europe/Rome",
    ).legs  # fmt: skip
    back = rules.read_email(
        "a2", "Conferma prenotazione", "Codice di prenotazione: QWE4RT5\nVolo AZ 5288  EWR → FCO\n"
        "Partenza: 10/12/2026 17:00\nArrivo: 11/12/2026 07:30", "Europe/Rome",
    ).legs  # fmt: skip
    for order in ((*back, *out), (*out, *back)):
        (trip,) = _place(list(order))
        assert [x.ref for x in trip.legs] == ["AZ5287", "AZ5288"]


# --- the service: emails sharing one second --------------------------------------------------------------


def _shape_of(client: Any, db: Any) -> tuple[Any, ...]:
    runtime = client.app.state.runtime
    (trip,) = client.portal.call(runtime.trips.listed, "all")
    tasks = db.execute("SELECT title, due_at FROM tasks ORDER BY title").fetchall()
    return (
        trip["title"], trip["depart_at"], trip["arrive_at"], sorted(trip["source_email_ids"]),
        [(x["kind"], x.get("ref"), x["at"], x.get("until")) for x in trip["legs"]],
        db.execute("SELECT back_at IS NOT NULL FROM trips").fetchone(), tasks,
    )  # fmt: skip


EXPECTED = (
    "Roma → New York",
    "2026-11-20T10:35:00+01:00",
    "2026-11-20T14:20:00-05:00",
    ["f1", "f2", "h1"],
    [
        ("flight", "AZ5287", "2026-11-20T10:35:00+01:00", "2026-11-20T14:20:00-05:00"),
        ("hotel", "Hotel Esempio Midtown", "2026-11-20T15:00:00-05:00", "2026-11-23T11:00:00-05:00"),
        ("flight", "AZ5288", "2026-12-10T17:00:00-05:00", "2026-12-11T07:30:00+01:00"),
    ],
    (True,),
    [
        (
            "Check-in online AZ5287: apre il 19/11 alle 10:35 (ora di Roma)",
            datetime(2026, 11, 19, 9, 35, tzinfo=UTC),
        ),
        (
            "Check-in online AZ5288: apre il 09/12 alle 17:00 (ora di New York)",
            datetime(2026, 12, 9, 22, 0, tzinfo=UTC),
        ),
        (
            "Partire per l'aeroporto (FCO) per AZ5287: alle 06:50 (ora di Roma)",
            datetime(2026, 11, 20, 5, 50, tzinfo=UTC),
        ),
        (
            "Partire per l'aeroporto (JFK) per AZ5288: alle 13:15 (ora di New York)",
            datetime(2026, 12, 10, 18, 15, tzinfo=UTC),
        ),
    ],
)  # fmt: skip  (the trip, its legs and Pisa's tasks as the emails read in the journey's order give them)


@pytest.mark.parametrize("order", list(itertools.permutations(ROW1)))
def test_order_row1_three_emails_of_one_second_in_any_order_are_one_trip(client, db, order) -> None:  # type: ignore[no-untyped-def]
    """Each email read on its own (the order Postgres happened to return): one trip, the same legs and the same
    tasks each time."""
    runtime = client.app.state.runtime
    for mail in order:
        _email(db, mail, SENT)
        client.portal.call(runtime.trips.refresh)
    assert db.execute("SELECT count(*) FROM trips").fetchone() == (1,)
    assert _shape_of(client, db) == EXPECTED  # identical whichever order, with the same tasks


@pytest.mark.parametrize("order", list(itertools.permutations(ROW1)))
def test_order_row1_three_emails_of_one_second_in_one_read_are_one_trip(client, db, order) -> None:  # type: ignore[no-untyped-def]
    """All three in one read, their ids (the tie-break) in every order: one trip with the same legs."""
    for n, (_, subject, body) in enumerate(order):
        _email(db, (f"m{n}", subject, body), SENT)
    assert client.portal.call(client.app.state.runtime.trips.refresh) == 3
    (trip,) = client.portal.call(client.app.state.runtime.trips.listed, "all")
    assert [(x["kind"], x.get("ref")) for x in trip["legs"]] == LEGS and trip["title"] == "Roma → New York"


def test_order_a_trip_he_renamed_survives_a_later_merge_unchanged(client, db) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    _email(db, HOTEL, SENT)
    client.portal.call(runtime.trips.refresh)
    (hotel,) = client.portal.call(runtime.trips.listed, "all")
    items = [{"text": "Prenotare il ristorante", "done": True}]
    client.patch(
        f"/v1/trips/{hotel['id']}", json={"title": "Vacanza a NY", "checklist": items}, headers=headers
    )
    for mail in (BACK, FLIGHT):
        _email(db, mail, SENT)
        client.portal.call(runtime.trips.refresh)
    (trip,) = client.portal.call(runtime.trips.listed, "all")
    assert trip["id"] == hotel["id"]  # their trip stays; the others' legs join it
    assert trip["title"] == "Vacanza a NY"
    assert trip["checklist"] == [{"text": "Prenotare il ristorante", "done": True, "by": "owner"}]
    assert [(x["kind"], x.get("ref")) for x in trip["legs"]] == LEGS
    assert trip["legs"][1] == hotel["legs"][0]  # their trip's leg, unchanged


def test_order_a_trip_he_cancelled_is_never_merged(client, db) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    _email(db, HOTEL, SENT)
    client.portal.call(runtime.trips.refresh)
    (hotel,) = client.portal.call(runtime.trips.listed, "all")
    client.patch(f"/v1/trips/{hotel['id']}", json={"status": "cancelled"}, headers=headers)
    before = db.execute("SELECT legs, status, title FROM trips WHERE id = %s", (hotel["id"],)).fetchone()
    for mail in (BACK, FLIGHT):
        _email(db, mail, SENT)
        client.portal.call(runtime.trips.refresh)
    assert (
        db.execute("SELECT legs, status, title FROM trips WHERE id = %s", (hotel["id"],)).fetchone() == before
    )
    assert db.execute("SELECT count(*) FROM trips").fetchone() == (2,)


def test_order_a_merge_keeps_what_the_merged_trip_held(client, db) -> None:  # type: ignore[no-untyped-def]
    """The trip that goes gives the other its emails, cancellation notes and flight claims."""
    runtime = client.app.state.runtime
    _email(db, BACK, SENT)
    client.portal.call(runtime.trips.refresh)
    (back,) = db.execute("SELECT id FROM trips").fetchone()
    db.execute("UPDATE trips SET cancel_mentions = '{x9}' WHERE id = %s", (back,))
    now = datetime(2026, 10, 19, tzinfo=UTC)
    db.execute(
        "INSERT INTO flight_claims (id, flight, day, scheduled_at, disruption, rule, status, reason, trip_id, "
        "source, created_at, updated_at) VALUES (gen_random_uuid(), 'AZ5288', '2026-12-10', %s, 'delay', 'none', "
        "'possible', 'r', %s, 'email', %s, %s)",
        (now, back, now, now),
    )
    for mail in (HOTEL, FLIGHT):
        _email(db, mail, SENT)
        client.portal.call(runtime.trips.refresh)
    (trip_id, mentions, emails) = db.execute(
        "SELECT id, cancel_mentions, source_email_ids FROM trips"
    ).fetchone()
    assert mentions == ["x9"] and sorted(emails) == ["f1", "f2", "h1"]
    assert db.execute("SELECT trip_id FROM flight_claims").fetchone() == (trip_id,)


# --- money: the other email-driven builders ----------------------------------------------------------------

AZ = ("ITA Airways", "noreply@ita-airways.com")
owner = _owner  # test_money's fixture: their language, Google connected


def _money(client: Any) -> int:
    return client.portal.call(client.app.state.runtime.money_back.scan)


@pytest.mark.parametrize("first", ["d1", "d2"])
def test_order_flight_claim_the_latest_sent_delay_wins_whichever_is_read_first(
    client, db, owner, first
) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)
    mails = {
        "d1": ("Il volo AZ5287 è in ritardo di 5 ore.", rome(2026, 10, 15, 9)),
        "d2": ("Il volo AZ5287 è in ritardo: ritardo previsto di 200 minuti.", rome(2026, 10, 15, 11)),
    }
    for mid in (first, *(m for m in mails if m != first)):
        email(db, mid, *AZ, "AZ5287", mails[mid][0], at=mails[mid][1])
        _money(client)
    assert db.execute("SELECT delay_min FROM flight_claims").fetchone() == (200,)  # d2 was sent last


@pytest.mark.parametrize("first", ["c1", "c2"])
def test_order_flight_claim_a_cancellation_counts_from_its_first_notice(client, db, owner, first) -> None:  # type: ignore[no-untyped-def]
    a_trip(client, db)  # AZ5287 on 15 October
    sent = {"c1": rome(2026, 9, 25, 9), "c2": rome(2026, 10, 10, 9)}  # 20 days' notice, then 5
    for mid in (first, *(m for m in sent if m != first)):
        email(db, mid, *AZ, "Volo AZ5287 cancellato", "Il tuo volo AZ5287 è stato cancellato.", at=sent[mid])
        _money(client)
    assert db.execute("SELECT notice_days FROM flight_claims").fetchone() == (20,)


def test_order_flight_notice_read_before_its_booking_waits_for_the_leg(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    email(db, "d1", *AZ, "AZ5287", "Il volo AZ5287 è in ritardo di 5 ore.", at=rome(2026, 10, 15, 9))
    _money(client)  # no leg yet: nothing, and the notice waits
    assert db.execute("SELECT count(*) FROM flight_claims").fetchone() == (0,)
    a_trip(client, db)
    _money(client)
    assert db.execute("SELECT delay_min, source_email_ids FROM flight_claims").fetchone() == (300, ["d1"])
    assert db.execute("SELECT waiting FROM money_reads WHERE email_id = 'd1'").fetchone() == (None,)


ZALANDO = ("Zalando", "service@zalando.example.invalid")
PROMISED = ("Reso ricevuto", "Riceverai il rimborso di € 49,90 entro il 13/10/2026.")
CREDITED = ("Rimborso accreditato", "Rimborso accreditato: € 49,90")


def test_order_refund_credit_read_before_its_promise_still_closes_it(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    email(db, "r9", *ZALANDO, *CREDITED, at=rome(2026, 10, 12, 10), category="other")
    _money(client)  # the promise isn't here yet (fetched late): the credit waits
    email(db, "r1", *ZALANDO, *PROMISED, at=rome(2026, 10, 6, 10), category="other")
    _money(client)
    assert db.execute("SELECT status, received_email_id FROM refunds_promised").fetchone() == (
        "received",
        "r9",
    )
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'open'").fetchone() == (0,)  # no wrong chase


def test_order_refund_promise_and_credit_in_one_second_in_one_scan(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    at = rome(2026, 10, 6, 10)
    email(db, "a-credit", *ZALANDO, *CREDITED, at=at, category="other")  # its id sorts first
    email(db, "b-promise", *ZALANDO, *PROMISED, at=at, category="other")
    _money(client)
    assert db.execute("SELECT status FROM refunds_promised").fetchone() == ("received",)


AMAZON = ("Amazon.it", "conferma-spedizione@amazon.it")


def test_order_supply_a_delivery_read_before_its_order_is_that_order_s(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    """O1 (20 Sep), then O2 delivered 12 Oct: the delivery is O2's whichever is read first."""
    item = 'Ordinato: "Detersivo Esempio 3L"'
    email(db, "o1", *AMAZON, item, "Totale ordine: € 12,00", at=rome(2026, 9, 20, 10), category="other")
    email(db, "d2", *AMAZON, 'Consegnato: "Detersivo Esempio 3L"', "Il tuo pacco è stato consegnato.",
          at=rome(2026, 10, 12, 15), category="other")  # fmt: skip
    _money(client)  # O2 not here yet: the delivery goes to O1, the newest order it knows
    email(db, "o2", *AMAZON, item, "Totale ordine: € 12,00", at=rome(2026, 10, 10, 10), category="other")
    _money(client)
    rows = db.execute("SELECT ordered_on, delivered_on FROM purchases ORDER BY ordered_on").fetchall()
    assert rows == [(date(2026, 9, 20), None), (date(2026, 10, 10), date(2026, 10, 12))]


# --- things: a number another email bridges ------------------------------------------------------------------

THING_MAILS = {  # one shop: the order, the parcel, and the email that names both (sent in this order)
    "ta": ("t-a", [{"kind": "order", "value": "402-1234567"}], "Ordine confermato", 0),
    "tb": ("t-b", [{"kind": "tracking", "value": "XYZ987654"}], "Il pacco è partito", 2),
    "tc": ("t-c", [{"kind": "order", "value": "402-1234567"}, {"kind": "tracking", "value": "XYZ987654"}],
           "Spedito", 1),
}  # fmt: skip


def _link_all(db: Any, order: tuple[str, ...]) -> None:
    import asyncio
    from datetime import timedelta

    from pisa.services import things
    from tests.test_proactivity import T0, _attend

    for mid in THING_MAILS:
        thread, _, subject, hours = THING_MAILS[mid]
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
            "intake_status) VALUES (%s, %s, 'Shop', 'ordini@shop.example.invalid', %s, %s, ARRAY['INBOX'], "
            "'done')",
            (mid, thread, subject, T0 - timedelta(days=1) + timedelta(hours=hours)),
        )

    async def go() -> None:
        async with _attend(db, T0) as env, env.pool.connection() as conn:
            for mid in order:
                thread, keys, subject, _ = THING_MAILS[mid]
                await things.link_email(conn, message_id=mid, thread_id=thread, keys=keys, subject=subject,
                                        gist=None, now=T0, sender="ordini@shop.example.invalid")  # fmt: skip

    asyncio.run(go())


@pytest.mark.parametrize("order", list(itertools.permutations(THING_MAILS)))
def test_order_things_an_email_naming_two_numbers_makes_one_thing_in_any_order(db, order) -> None:  # type: ignore[no-untyped-def]
    _link_all(db, order)
    assert db.execute("SELECT title FROM things").fetchall() == [("Ordine confermato",)]  # its earliest email
    emails = db.execute("SELECT item_ref FROM thing_items WHERE item_kind = 'email' ORDER BY 1").fetchall()
    assert emails == [("ta",), ("tb",), ("tc",)]
    assert db.execute("SELECT count(DISTINCT thing_id) FROM thing_keys").fetchone() == (1,)


def test_order_things_a_thread_s_earlier_email_joins_when_its_number_arrives(db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from pisa.services import things
    from tests.test_proactivity import T0, _attend

    for mid in ("e1", "e2"):
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
            "intake_status) VALUES (%s, 'th', 'Shop', 'o@shop.example.invalid', 'Ordine', %s, ARRAY['INBOX'], "
            "'done')",
            (mid, T0),
        )

    async def go() -> list[Any]:
        async with _attend(db, T0) as env, env.pool.connection() as conn:
            first = await things.link_email(conn, message_id="e1", thread_id="th", keys=[], subject="Grazie",
                                            gist=None, now=T0)  # fmt: skip
            second = await things.link_email(conn, message_id="e2", thread_id="th", subject="Ordine",
                                             keys=[{"kind": "order", "value": "402-7654321"}], gist=None,
                                             now=T0)  # fmt: skip
            return [first, second]

    first, second = asyncio.run(go())
    assert first is None and second is not None  # no number, no thread thing yet: none (v1 never guesses)
    assert db.execute("SELECT item_ref FROM thing_items ORDER BY 1").fetchall() == [("e1",), ("e2",)]


# --- people: a moment from mail doesn't hang on which sync came first ----------------------------------------


def test_order_people_a_moment_from_someone_he_writes_to_later_is_kept(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """Laura writes "sarò a Roma giovedì" before she is one of their people (they answer her the next hour): her
    moment is kept as if both had come before one sync."""
    from tests.test_people_moments import person, plan, received, rome

    clock.now = rome(2026, 11, 2, 11)
    received(db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 9), "Sarò a Roma giovedì.")
    plan(client, db, clock)
    assert db.execute("SELECT count(*) FROM person_moments").fetchone() == (0,)  # not their person yet
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    db.execute("DELETE FROM app_settings WHERE key = 'people_synced_at'")
    clock.now = rome(2026, 11, 2, 12)
    plan(client, db, clock)
    assert db.execute("SELECT count(*) FROM person_moments").fetchone() == (1,)
