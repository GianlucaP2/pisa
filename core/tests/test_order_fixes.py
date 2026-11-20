# ruff: noqa: E501
"""The review's findings. Synthetic
emails, invented names."""

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest

from pisa.services.trips import TripsService
from tests.test_money import owner as _owner
from tests.test_two_countries import FLIGHT, HOTEL, SENT, _email

owner = _owner  # test_money's fixture: their language, Google connected

OUT = ("o1", "Conferma prenotazione volo AZ608",
       "Volo AZ 608  FCO → JFK\nPartenza: 20/11/2026 10:35\nArrivo: 20/11/2026 14:20")  # fmt: skip
RET = ("r1", "Conferma prenotazione volo AZ609",
       "Volo AZ 609  JFK → FCO\nPartenza: 25/11/2026 17:00\nArrivo: 26/11/2026 07:30")  # fmt: skip
HOT = ("h9", "La tua prenotazione è confermata",
       "Booking.com - Conferma della prenotazione\nStruttura: Hotel Esempio Downtown\n"
       "Indirizzo: 1 Imaginary St, New York, NY\nCheck-in: 15/12/2026\nCheck-out: 18/12/2026")  # fmt: skip
AZ400 = ("c1", "Conferma prenotazione volo AZ400",
         "Codice di prenotazione: PNR4XY9\nVolo AZ 400  FCO → FRA\nPartenza: 20/11/2026 07:00\n"
         "Arrivo: 20/11/2026 09:00")  # fmt: skip
LH400 = ("c2", "Conferma prenotazione volo LH400",
         "Codice di prenotazione: PNR4XY9\nVolo LH 400  FRA → JFK\nPartenza: 20/11/2026 10:30\n"
         "Arrivo: 20/11/2026 13:20")  # fmt: skip
STRANGER = "prenotazioni@viaggi-esempio.example.invalid"  # sells no travel Pisa knows: "da confermare"


def _refresh(client: Any) -> int:
    return client.portal.call(client.app.state.runtime.trips.refresh)


def _trips(db: Any) -> list[tuple[Any, ...]]:
    rows = db.execute("SELECT title, legs FROM trips ORDER BY depart_at").fetchall()
    return [(title, [leg.get("ref") for leg in legs]) for title, legs in rows]


# --- item 1: a merge never deletes a trip they are editing ------------------------------------------------------


def _between(monkeypatch: Any, db: Any, sql: str) -> list[str]:
    """Their change, tried after the reader took its snapshot of the trips and before it merges (a writer that
    doesn't wait for the reader's advisory lock). Review item 7: the snapshot's rows are locked, so the
    change waits for the reader (`waited`); it can never land mid-read."""
    import psycopg

    original = TripsService._shapes
    seen: list[str] = []

    async def snapshot_then_his_change(self: TripsService, conn: Any) -> Any:
        shapes = await original(self, conn)
        db.execute("SET lock_timeout = '300ms'")
        try:
            db.execute(sql)
            seen.append("written")
        except psycopg.errors.LockNotAvailable:
            seen.append("waited")
        finally:
            db.execute("RESET lock_timeout")
        return shapes

    monkeypatch.setattr(TripsService, "_shapes", snapshot_then_his_change)
    return seen


@pytest.mark.parametrize(
    "change",
    [
        "title = 'Vacanza a NY', title_by_owner = true",
        "status = 'cancelled'",
    ],
)
def test_a_trip_he_changes_mid_read_survives_the_merge(client, db, monkeypatch, change) -> None:  # type: ignore[no-untyped-def]
    """The hotel's trip (Pisa's) and the flight that would absorb it: they rename or cancel the hotel's trip
    while the flight is read. Review item 7: their change waits for the reader (the snapshot's rows are
    locked); a change made before the read is their trip's, and it survives (`by_owner`)."""
    _email(db, HOTEL, SENT)
    _refresh(client)
    (hotel, before) = db.execute("SELECT id, legs FROM trips").fetchone()
    _email(db, FLIGHT, SENT)
    seen = _between(monkeypatch, db, f"UPDATE trips SET {change}, by_owner_at = now() WHERE id = '{hotel}'")
    _refresh(client)
    assert seen == ["waited"]  # never mid-read
    monkeypatch.undo()
    # the same change committed before the read: their trip survives it
    db.execute("DELETE FROM trips")
    db.execute("DELETE FROM email_messages WHERE id = 'f1'")
    db.execute("UPDATE email_messages SET trip_read_at = NULL")
    _refresh(client)
    (hotel, before) = db.execute("SELECT id, legs FROM trips").fetchone()
    db.execute(f"UPDATE trips SET {change}, by_owner_at = now() WHERE id = '{hotel}'")
    _email(db, FLIGHT, SENT)
    _refresh(client)
    row = db.execute("SELECT title, status, legs FROM trips WHERE id = %s", (hotel,)).fetchone()
    assert row is not None  # their trip was not the one that went
    if "cancelled" in change:
        assert row[1] == "cancelled" and row[2] == before  # never merged: the same legs
        assert db.execute("SELECT count(*) FROM trips").fetchone() == (2,)
    else:
        assert row[0] == "Vacanza a NY"
        assert [x["ref"] for x in row[2]] == ["AZ5287", "Hotel Esempio Midtown"]  # the flight joined theirs
        assert db.execute("SELECT count(*) FROM trips").fetchone() == (1,)


def test_a_trip_he_confirms_mid_read_survives_the_merge(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Two bookings from a sender Pisa doesn't know ("da confermare"); they confirm the hotel's while the flight
    is read: their confirm waits for the reader (item 7), and is never lost: it lands after."""
    _email(db, HOTEL, SENT, sender=STRANGER)
    _refresh(client)
    (hotel,) = db.execute("SELECT id FROM trips").fetchone()
    _email(db, FLIGHT, SENT, sender=STRANGER)
    seen = _between(
        monkeypatch, db, f"UPDATE trips SET doubt = NULL, by_owner_at = now() WHERE id = '{hotel}'"
    )
    _refresh(client)
    assert seen == ["waited"]


def test_a_patch_waits_for_the_reader(client, db) -> None:  # type: ignore[no-untyped-def]
    """`patch` takes the reader's lock: while a refresh holds it, his change waits for it."""
    import asyncio

    from pisa.infra import locks

    _email(db, HOTEL, SENT)
    _refresh(client)
    (hotel,) = db.execute("SELECT id FROM trips").fetchone()
    trips = client.app.state.runtime.trips

    async def attempt() -> str:
        try:
            await asyncio.wait_for(trips.patch(str(hotel), title="Vacanza"), 1.0)
        except TimeoutError:
            return "waited"
        return "done"

    db.execute("SELECT pg_advisory_lock(%s)", (locks.TRIPS,))  # a refresh holds it
    try:
        assert client.portal.call(attempt) == "waited"
    finally:
        db.execute("SELECT pg_advisory_unlock(%s)", (locks.TRIPS,))
    assert db.execute("SELECT title_by_owner FROM trips").fetchone() == (False,)


# --- item 2: grouping is a function of the set of legs -------------------------------------------------------


@pytest.mark.parametrize("order", list(itertools.permutations((OUT, RET, HOT))))
def test_a_one_way_trip_gives_back_a_later_stay_when_its_return_arrives(client, db, order) -> None:  # type: ignore[no-untyped-def]
    """OUT (one-way), RET five days later, a New York hotel in December: in every order, the same two trips
    (the round trip; the December stay of its own)."""
    for mail in order:
        _email(db, mail, SENT)
        _refresh(client)
    assert _trips(db) == [
        ("Roma → New York", ["AZ608", "AZ609"]),
        ("Soggiorno a New York", ["Hotel Esempio Downtown"]),
    ]
    tasks = db.execute("SELECT title FROM tasks WHERE status = 'open' ORDER BY title").fetchall()
    assert len(tasks) == 4  # each flight's check-in and leave-at, whichever order


# --- item 3: Pisa's tasks don't depend on the order either ------------------------------------------------------


@pytest.mark.parametrize("order", [(AZ400, LH400), (LH400, AZ400)])
def test_a_connection_read_first_has_no_leave_at_task(client, db, order) -> None:  # type: ignore[no-untyped-def]
    for mail in order:
        _email(db, mail, SENT)
        _refresh(client)
    assert db.execute("SELECT count(*) FROM trips").fetchone() == (1,)
    tasks = [r[0] for r in db.execute("SELECT title FROM tasks WHERE status = 'open' ORDER BY title")]
    assert tasks == [
        "Check-in online AZ400: apre il 19/11 alle 07:00 (ora di Roma)",
        "Check-in online LH400: apre il 19/11 alle 11:30 (ora di Francoforte)",
        "Partire per l'aeroporto (FCO) per AZ400: alle 03:15 (ora di Roma)",
    ]
    assert not any("(FRA)" in t for t in tasks)


# --- item 4: two orders are never merged into one thing ------------------------------------------------------

ORDERS = {  # two orders of one shop, and the invoice that names both (sent in this order)
    "o1": ("t-o1", [{"kind": "order", "value": "402-1111111"}], "Ordine 1", 0),
    "o2": ("t-o2", [{"kind": "order", "value": "402-2222222"}], "Ordine 2", 1),
    "inv": ("t-inv", [{"kind": "order", "value": "402-1111111"}, {"kind": "order", "value": "402-2222222"}],
            "Fattura", 2),
}  # fmt: skip


def _link(db: Any, mails: dict[str, Any], order: tuple[str, ...]) -> None:
    import asyncio
    from datetime import timedelta

    from pisa.services import things
    from tests.test_proactivity import T0, _attend

    for mid, (thread, _, subject, hours) in mails.items():
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
            "intake_status) VALUES (%s, %s, 'Shop', 'ordini@shop.example.invalid', %s, %s, ARRAY['INBOX'], "
            "'done')",
            (mid, thread, subject, T0 - timedelta(days=1) + timedelta(hours=hours)),
        )

    async def go() -> None:
        async with _attend(db, T0) as env, env.pool.connection() as conn:
            for mid in order:
                thread, keys, subject, _ = mails[mid]
                await things.link_email(conn, message_id=mid, thread_id=thread, keys=keys, subject=subject,
                                        gist=None, now=T0, sender="ordini@shop.example.invalid")  # fmt: skip

    asyncio.run(go())


@pytest.mark.parametrize("order", list(itertools.permutations(ORDERS)))
def test_an_invoice_naming_two_orders_never_makes_them_one_thing(db, order) -> None:  # type: ignore[no-untyped-def]
    _link(db, ORDERS, order)
    assert db.execute("SELECT count(*) FROM things").fetchone() == (2,)
    held = db.execute(
        "SELECT thing_id, array_agg(split_part(key, '|', 2) ORDER BY key) FROM thing_keys GROUP BY thing_id "
        "ORDER BY 2"
    ).fetchall()
    assert [keys for _, keys in held] == [["402-1111111"], ["402-2222222"]]  # one order each, always
    emails = dict(
        db.execute("SELECT item_ref, thing_id FROM thing_items WHERE item_kind = 'email'").fetchall()
    )
    assert emails["o1"] != emails["o2"] and emails["inv"] in (emails["o1"], emails["o2"])


@pytest.mark.parametrize("order", list(itertools.permutations(("ta", "tb", "tc"))))
def test_an_order_and_its_parcel_bridged_by_a_shipping_email_are_one_thing(db, order) -> None:  # type: ignore[no-untyped-def]
    from tests.test_core_order import THING_MAILS

    _link(db, THING_MAILS, order)
    assert db.execute("SELECT count(*) FROM things").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM thing_items WHERE item_kind = 'email'").fetchone() == (3,)


# --- item 5: the backfill marks every trip they touched; a merge never drops their ticks -------------------------


def test_a_merge_combines_the_lists_and_keeps_his_ticks(client, db) -> None:  # type: ignore[no-untyped-def]
    """The trip that goes held a tick of theirs (from before their ticks marked a trip): the two lists are one,
    their tick kept."""
    from tests.test_two_countries import TRAIN

    _email(db, FLIGHT, SENT)
    _email(db, HOTEL, SENT, sender=STRANGER)  # its own trip: "da confermare"
    _refresh(client)
    db.execute(
        'UPDATE trips SET checklist = \'[{"text": "Adattatore", "done": false, "by": "pisa"}]\' '
        "WHERE doubt IS NULL"
    )
    db.execute(  # confirmed before their changes were marked: Pisa's own as far as the merge knows
        'UPDATE trips SET doubt = NULL, checklist = \'[{"text": "Passaporto", "done": true, "by": "pisa"}, '
        '{"text": "Adattatore", "done": true, "by": "owner"}]\' WHERE doubt IS NOT NULL'
    )
    _email(db, TRAIN, SENT)  # any new leg: the reader groups Pisa's trips again
    _refresh(client)
    rows = db.execute('SELECT checklist FROM trips WHERE legs @> \'[{"ref": "AZ5287"}]\'').fetchall()
    assert rows == [
        (
            [
                {"text": "Adattatore", "done": True, "by": "owner"},
                {"text": "Passaporto", "done": True, "by": "pisa"},
            ],
        )
    ]


def test_the_lists_combined_his_items_win() -> None:
    from pisa.domain import trips as rules

    keep = [
        {"text": "Adattatore", "done": False, "by": "pisa"},
        {"text": "Ombrello", "done": False, "by": "pisa"},
    ]
    gone = [
        {"text": "adattatore", "done": True, "by": "owner"},
        {"text": "Passaporto", "done": True, "by": "pisa"},
    ]
    assert rules.combine_checklists(keep, gone) == [
        {"text": "adattatore", "done": True, "by": "owner"},
        {"text": "Ombrello", "done": False, "by": "pisa"},
        {"text": "Passaporto", "done": True, "by": "pisa"},
    ]


# --- item 6: the people watermark moves past a newsletter ----------------------------------------------------


def test_a_strangers_newsletter_never_holds_the_people_watermark(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.test_people_moments import plan, received, rome

    clock.now = rome(2026, 11, 2, 11)
    received(
        db, "n1", "Notizie", "news@giornale.example.invalid", rome(2026, 11, 2, 9), "Le notizie di oggi."
    )
    plan(client, db, clock)
    (at,) = db.execute("SELECT value FROM app_settings WHERE key = 'people_moments_at'").fetchone()
    assert datetime.fromisoformat(at) == rome(2026, 11, 2, 9)  # moved past it: nothing to read again


# --- item 7: references to an absorbed trip are repointed; a leg is recorded once ----------------------------


@pytest.mark.parametrize("both", [False, True])
def test_the_welcome_back_thought_follows_the_trip_that_stays(client, db, both) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    _email(db, HOTEL, SENT)
    _refresh(client)
    (hotel,) = db.execute("SELECT id FROM trips").fetchone()

    def thought(day: date, ref: Any) -> None:
        db.execute(
            "INSERT INTO morning_thoughts (id, day, kind, ref, created_at) VALUES (gen_random_uuid(), %s, "
            "'welcome_back', %s, now())",
            (day, ref),
        )

    thought(date(2026, 10, 1), hotel)
    _email(db, FLIGHT, SENT)
    if both:  # the flight's trip had one too (made between the two reads): one is kept
        from pisa.services.trips import TripsService

        original = TripsService._regroup

        async def with_its_thought(self: TripsService, conn: Any, trips: Any, now: Any, added: Any) -> None:
            other = next(t.id for t in trips if t.id != str(hotel))
            await conn.execute(
                "INSERT INTO morning_thoughts (id, day, kind, ref, created_at) VALUES (gen_random_uuid(), "
                "'2026-10-02', 'welcome_back', %s, now())",
                (other,),
            )
            await original(self, conn, trips, now, added)

        TripsService._regroup = with_its_thought  # type: ignore[method-assign]
        try:
            _refresh(client)
        finally:
            TripsService._regroup = original  # type: ignore[method-assign]
    else:
        _refresh(client)
    (trip,) = db.execute("SELECT id FROM trips").fetchone()
    assert trip != hotel  # the flight's trip stays (it starts first); the hotel's went into it
    refs = {r[0] for r in db.execute("SELECT ref FROM morning_thoughts")}
    # Item 5: when both had one, the absorbed trip's row stays where it is (never deleted)
    assert refs == ({trip, hotel} if both else {trip})


def test_a_leg_whose_trip_merged_is_recorded_once_by_the_night(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """The night writes a trip leg's episode by the leg, never by its trip: the trip it was in merged into
    another since (a new id), and the next night writes nothing twice."""
    from datetime import date

    from tests.test_episodes import _segment

    night = date(2026, 11, 21)
    _email(db, FLIGHT, SENT)
    clock.now = datetime(2026, 10, 20, tzinfo=UTC)
    _refresh(client)
    db.execute("UPDATE trips SET by_owner_at = NULL")
    assert _segment(client, clock, night) >= 1
    trip_leg = "kind = 'trip_leg'"
    first = db.execute(f"SELECT key FROM episodes WHERE {trip_leg}").fetchall()
    assert first
    # the same leg, now in another trip (a merge gave it a new home)
    db.execute("UPDATE trips SET id = gen_random_uuid()")
    _segment(client, clock, night)
    assert db.execute(f"SELECT key FROM episodes WHERE {trip_leg}").fetchall() == first


# --- item 8: the money scan and a trip merge don't race on `flight_claims.trip_id` ----------------------------

AZ = ("ITA Airways", "noreply@ita-airways.com")


def test_the_money_scan_writes_a_claim_under_the_trip_readers_lock(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.infra import locks
    from pisa.services.money_back import MoneyBackService
    from tests.test_money import a_trip, email, rome, scan

    held: list[bool] = []
    original = MoneyBackService._upsert_claim

    async def seen(self: MoneyBackService, conn: Any, **kw: Any) -> None:
        row = await (
            await conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objid = %s "
                "AND mode = 'ShareLock' AND granted AND pid = pg_backend_pid())",
                (locks.TRIPS,),
            )
        ).fetchone()
        held.append(bool(row[0]))
        await original(self, conn, **kw)

    monkeypatch.setattr(MoneyBackService, "_upsert_claim", seen)
    a_trip(client, db)
    email(db, "d1", *AZ, "AZ5287", "Il volo AZ5287 è in ritardo di 5 ore.", at=rome(2026, 10, 15, 9))
    scan(client)
    assert held == [True]


def test_a_claim_whose_trip_went_meanwhile_leaves_the_notice_waiting(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import uuid

    from pisa.services.money_back import MoneyBackService
    from tests.test_money import a_trip, email, rome, scan

    a_trip(client, db)
    email(db, "d1", *AZ, "AZ5287", "Il volo AZ5287 è in ritardo di 5 ore.", at=rome(2026, 10, 15, 9))
    original = MoneyBackService._flight_legs

    async def a_merged_trip(self: MoneyBackService, conn: Any) -> Any:  # its trip went after it was read
        return [(uuid.uuid4(), leg) for _, leg in await original(self, conn)]

    monkeypatch.setattr(MoneyBackService, "_flight_legs", a_merged_trip)
    scan(client)
    assert db.execute("SELECT waiting FROM money_reads WHERE email_id = 'd1'").fetchone() == ("flight",)
    monkeypatch.setattr(MoneyBackService, "_flight_legs", original)
    scan(client)  # read again: its claim, on the trip that is there
    assert db.execute("SELECT delay_min, trip_id IS NOT NULL FROM flight_claims").fetchone() == (300, True)


# --- item 9: an order takes back its delivery: a move, never a loss -------------------------------------------


def test_an_order_read_again_never_loses_the_older_order_s_delivery(client, db, owner) -> None:  # type: ignore[no-untyped-def]
    """O1 holds the delivery; O2's email is read again although its row is there already (no new row): the
    delivery O2 would take back stays with O1."""
    from datetime import date

    from tests.test_money import email, rome, scan

    amazon = ("Amazon.it", "conferma-spedizione@amazon.it")
    item = 'Ordinato: "Detersivo Esempio 3L"'
    email(db, "o1", *amazon, item, "Totale ordine: € 12,00", at=rome(2026, 9, 20, 10), category="other")
    email(db, "d2", *amazon, 'Consegnato: "Detersivo Esempio 3L"', "Il tuo pacco è stato consegnato.",
          at=rome(2026, 10, 12, 15), category="other")  # fmt: skip
    email(db, "o2", *amazon, item, "Totale ordine: € 12,00", at=rome(2026, 10, 10, 10), category="other")
    db.execute("INSERT INTO money_reads (email_id, read_at) VALUES ('o2', now())")  # fetched late
    scan(client)  # the delivery goes to O1, the newest order known
    db.execute(  # O2's row is there already (an earlier read), its email read once more
        "INSERT INTO purchases (id, item, item_key, seller, seller_key, ordered_on, region, source, "
        "source_email_id, created_at, updated_at) SELECT gen_random_uuid(), item, item_key, seller, seller_key, "
        "'2026-10-10', region, 'email', 'o2', now(), now() FROM purchases LIMIT 1"
    )
    db.execute("DELETE FROM money_reads WHERE email_id = 'o2'")
    scan(client)
    rows = db.execute("SELECT ordered_on, delivered_on FROM purchases ORDER BY ordered_on").fetchall()
    assert rows == [(date(2026, 9, 20), date(2026, 10, 12)), (date(2026, 10, 10), None)]
