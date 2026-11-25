# ruff: noqa: E501
"""The second review's findings.
Synthetic emails, invented names."""

import itertools
import time
from typing import Any

import pytest

from tests.owner import owner_at
from tests.test_order_fixes import _link

# --- items 1, 2 and 9: one order class; legacy keys; the choice by the lowest key ---------------------------

MIXED = {  # an order, a "reference" of another order (the model's label), and the invoice naming both
    "o1": ("t-o1", [{"kind": "order", "value": "402-1111111"}], "Ordine 1", 0),
    "o2": ("t-o2", [{"kind": "reference", "value": "402-2222222"}], "Ordine 2", 1),
    "inv": ("t-inv", [{"kind": "order", "value": "402-1111111"}, {"kind": "reference", "value": "402-2222222"}],
            "Fattura", 2),
}  # fmt: skip


def _shape(db: Any) -> list[tuple[list[str], list[str]]]:
    """Each thing as (its emails, its keys), sorted: the result, whatever the ids."""
    rows = db.execute(
        "SELECT (SELECT array_agg(item_ref ORDER BY item_ref) FROM thing_items i WHERE i.thing_id = t.id "
        "AND item_kind = 'email'), (SELECT array_agg(split_part(key, '|', 2) ORDER BY key) FROM thing_keys k "
        "WHERE k.thing_id = t.id) FROM things t"
    ).fetchall()
    return sorted((list(e or []), list(k or [])) for e, k in rows)


@pytest.mark.parametrize("order", list(itertools.permutations(MIXED)))
def test_an_order_and_a_reference_are_one_class_never_one_thing(db, order) -> None:  # type: ignore[no-untyped-def]
    """Item 1 (and 9): `order` and `reference` are one identity class; the invoice naming both never bridges
    them, and it always goes with its lowest number's thing: the same two things in every reading order."""
    _link(db, MIXED, order)
    assert _shape(db) == [(["inv", "o1"], ["402-1111111"]), (["o2"], ["402-2222222"])]


@pytest.mark.parametrize("kinds", [("order", "booking"), ("ticket", "reference"), ("booking", "order")])
def test_any_two_order_class_kinds_never_join(db, kinds) -> None:  # type: ignore[no-untyped-def]
    a, b = kinds
    mails = {
        "o1": ("t-o1", [{"kind": a, "value": "402-1111111"}], "Uno", 0),
        "o2": ("t-o2", [{"kind": b, "value": "402-2222222"}], "Due", 1),
        "inv": (
            "t-inv",
            [{"kind": a, "value": "402-1111111"}, {"kind": b, "value": "402-2222222"}],
            "Fattura",
            2,
        ),
    }
    _link(db, mails, ("o1", "o2", "inv"))
    assert db.execute("SELECT count(*) FROM things").fetchone() == (2,)


def test_a_legacy_key_without_kind_is_order_class(db) -> None:  # type: ignore[no-untyped-def]
    """Item 2: o2's key was kept before kinds were (NULL); the invoice, then o1: two things."""
    _link(db, {"o2": MIXED["o2"]}, ("o2",))
    db.execute("UPDATE thing_keys SET kind = NULL")
    rest = {k: v for k, v in MIXED.items() if k != "o2"}
    _link(db, rest, ("inv", "o1"))
    assert db.execute("SELECT count(*) FROM things").fetchone() == (2,)
    assert _shape(db) == [(["inv", "o1"], ["402-1111111"]), (["o2"], ["402-2222222"])]


def test_a_legacy_key_never_counts_as_a_second_order_in_the_attach(db) -> None:  # type: ignore[no-untyped-def]
    """Item 2, the attach loop alone: o2's NULL-kinded key, and an email naming o2 and a new order: the
    thing never takes the second order."""
    from pisa.domain import things as rules

    assert not rules.joinable([("order", "A1")], [(None, "B2")])
    assert rules.joinable([("tracking", "T1")], [(None, "B2")])
    _link(db, {"o2": MIXED["o2"]}, ("o2",))
    db.execute("UPDATE thing_keys SET kind = NULL")
    mails = {"x": ("t-o2", [{"kind": "order", "value": "402-3333333"}], "Re: Ordine 2", 2)}  # same thread
    _link(db, mails, ("x",))
    # the merge: another order number in the same thread is another order, never o2's thing
    assert _shape(db) == [(["o2"], ["402-2222222"]), (["x"], ["402-3333333"])]


def test_an_order_and_its_tracking_still_bridge(db) -> None:  # type: ignore[no-untyped-def]
    """The one bridge left: an order-class key and a tracking key (a legacy tracking key takes its kind from
    the email that names it)."""
    from tests.test_core_order import THING_MAILS

    _link(db, {"tb": THING_MAILS["tb"]}, ("tb",))
    db.execute("UPDATE thing_keys SET kind = NULL")
    _link(db, {k: v for k, v in THING_MAILS.items() if k != "tb"}, ("ta", "tc"))
    assert db.execute("SELECT count(*) FROM things").fetchone() == (1,)


# --- trips: helpers ------------------------------------------------------------------------------------------

from datetime import UTC, datetime  # noqa: E402

from tests.test_order_fixes import HOT, OUT, RET, _refresh  # noqa: E402
from tests.test_two_countries import SENT, _email  # noqa: E402

DEC_OUT = ("d1", "Conferma prenotazione volo AZ612",
           "Volo AZ 612  FCO → JFK\nPartenza: 14/12/2026 10:35\nArrivo: 14/12/2026 14:20")  # fmt: skip
DEC_BACK = ("d2", "Conferma prenotazione volo AZ613",
            "Volo AZ 613  JFK → FCO\nPartenza: 19/12/2026 17:00\nArrivo: 20/12/2026 07:30")  # fmt: skip


def _legs(db: Any) -> dict[str, dict[str, Any]]:
    return {x["ref"]: x for (legs,) in db.execute("SELECT legs FROM trips") for x in legs}


def _arrange(client: Any, db: Any, groups: list[list[str]]) -> list[str]:
    """The trips as an older Pisa grouped them (by the order it read the emails in): each group of leg refs one
    trip, written as the reader writes one."""
    import uuid

    from pisa.domain import trips as rules

    legs = {ref: rules.from_stored(x) for ref, x in _legs(db).items()}
    db.execute("DELETE FROM trips")
    service = client.app.state.runtime.trips
    now = service._clock()
    ids: list[str] = []

    async def go() -> None:
        async with service._pool.connection() as conn:
            for refs in groups:
                shape = rules.rebuild(str(uuid.uuid4()), [legs[r] for r in refs])
                await service._insert(conn, shape, now)
                await service._save(conn, shape, now)
                ids.append(shape.id)

    client.portal.call(go)
    return ids


def _holding(db: Any, ref: str) -> Any:
    return db.execute(
        "SELECT id FROM trips t WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(t.legs) l WHERE l->>'ref' = %s)",
        (ref,),
    ).fetchone()[0]


# --- item 3: regroup only what can still change --------------------------------------------------------------


def test_a_finished_trip_grouped_by_the_old_rules_stays_as_it_is(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """A past trip that an older Pisa grouped as one (OUT, RET and the December hotel: the order it read them
    in), landed and welcomed: a refresh in January, reading a new flight, never reshapes it."""
    for mail in (OUT, RET, HOT):
        _email(db, mail, SENT)
    _refresh(client)
    (past,) = _arrange(client, db, [["AZ608", "AZ609", "Hotel Esempio Downtown"]])
    before = db.execute("SELECT legs, back_at, landed_keys FROM trips WHERE id = %s", (past,)).fetchone()
    clock.now = datetime(2027, 1, 10, 9, tzinfo=UTC)
    _email(db, ("n1", "Conferma prenotazione volo AZ700",
                "Volo AZ 700  FCO → JFK\nPartenza: 20/02/2027 10:35\nArrivo: 20/02/2027 14:20"),
           datetime(2027, 1, 9, 9, tzinfo=UTC))  # fmt: skip
    assert _refresh(client) == 1
    after = db.execute("SELECT legs, back_at, landed_keys FROM trips WHERE id = %s", (past,)).fetchone()
    assert after == before  # exactly as it was
    assert db.execute("SELECT count(*) FROM trips").fetchone() == (2,)  # the new flight, a trip of its own


def test_a_live_trip_is_still_regrouped(client, db) -> None:  # type: ignore[no-untyped-def]
    """The same trips before they are over: the regroup still makes them what the set of legs gives."""
    for mail in (OUT, RET, HOT):
        _email(db, mail, SENT)
    _refresh(client)
    _arrange(client, db, [["AZ608", "AZ609", "Hotel Esempio Downtown"]])
    _email(db, DEC_OUT, SENT)
    _refresh(client)
    groups = sorted(sorted(x["ref"] for x in legs) for (legs,) in db.execute("SELECT legs FROM trips"))
    assert groups == [["AZ608", "AZ609"], ["AZ612", "Hotel Esempio Downtown"]]


# --- item 4: records follow their leg ------------------------------------------------------------------------


@pytest.mark.parametrize("his_y", [False, True])
def test_a_legs_records_follow_it_before_its_trip_is_absorbed(client, db, his_y) -> None:  # type: ignore[no-untyped-def]
    """Y=[AZ608], X=[AZ609, the NY hotel], Z=[AZ612 14 Dec], then AZ613 is read. X goes (into Y, which took
    AZ609); the hotel goes to Z, and its proposal, landed key and list item with it. The cancellation note
    naming AZ609 goes to Y only. Y's applied change (when they applied one) is not lost."""
    from psycopg.types.json import Jsonb

    for mail in (OUT, RET, HOT, DEC_OUT):
        _email(db, mail, SENT)
    _email(
        db,
        ("x1", "Il volo AZ609 è stato cancellato", "Ci scusiamo: il volo AZ 609 è stato cancellato."),
        SENT,
    )
    _refresh(client)
    legs = _legs(db)
    y, x, z = _arrange(client, db, [["AZ608"], ["AZ609", "Hotel Esempio Downtown"], ["AZ612"]])
    hotel = legs["Hotel Esempio Downtown"]["key"]
    proposal = {
        "key": hotel,
        "ref": "Hotel Esempio Downtown",
        "email_id": "h10",
        "at": "2026-12-16T15:00:00+00:00",
    }
    db.execute(
        "UPDATE trips SET proposals = %s, landed_keys = %s, cancel_mentions = ARRAY['x1'], checklist = %s "
        "WHERE id = %s",
        (Jsonb([proposal]), [hotel],
         Jsonb([{"text": "Check-in Hotel Esempio Downtown", "done": True, "by": "owner"},
                {"text": "Assicurazione di viaggio", "done": False, "by": "pisa"}]), x),
    )  # fmt: skip
    applied = "2026-11-20T11:35:00+01:00"
    if his_y:  # they applied a later email's new time to AZ608
        db.execute(
            "UPDATE trips SET proposals = %s WHERE id = %s",
            (Jsonb([{"key": legs["AZ608"]["key"], "ref": "AZ608", "email_id": "chg", "at": applied}]), y),
        )
        client.portal.call(lambda: client.app.state.runtime.trips.patch(y, apply_change="chg"))
    _email(db, DEC_BACK, SENT)
    _refresh(client)
    groups = sorted(sorted(x["ref"] for x in ls) for (ls,) in db.execute("SELECT legs FROM trips"))
    if not his_y:  # all Pisa's: the set of legs gives these two trips; X went into Y (which took AZ609)
        assert groups == [["AZ608", "AZ609"], ["AZ612", "AZ613", "Hotel Esempio Downtown"]]
        assert {str(_holding(db, "Hotel Esempio Downtown")), str(_holding(db, "AZ609"))} == {z, y}
    on_hotel, on_609 = _holding(db, "Hotel Esempio Downtown"), _holding(db, "AZ609")
    assert on_hotel != on_609
    row = db.execute(
        "SELECT proposals, landed_keys, cancel_mentions, checklist FROM trips WHERE id = %s", (on_hotel,)
    ).fetchone()
    assert row[0] == [proposal] and row[1] == [hotel]  # the hotel's records, with the hotel
    assert row[2] == []  # the note names AZ609, which is not here
    assert [i["text"] for i in row[3]] == ["Check-in Hotel Esempio Downtown"] and row[3][0]["done"]
    row = db.execute(
        "SELECT proposals, landed_keys, cancel_mentions, checklist FROM trips WHERE id = %s", (on_609,)
    ).fetchone()
    assert row[0] == [] and row[1] == [] and row[2] == ["x1"]
    assert [i["text"] for i in row[3]] == ["Assicurazione di viaggio"]  # what X held besides, absorbed
    if his_y:
        (ls,) = db.execute("SELECT legs FROM trips WHERE id = %s", (y,)).fetchone()  # their trip stays
        az608 = next(leg for leg in ls if leg["ref"] == "AZ608")
        assert datetime.fromisoformat(az608["at"]) == datetime.fromisoformat(applied)  # their change, kept


# --- item 5: their answer is never deleted ---------------------------------------------------------------------


def test_his_no_survives_a_merge_and_still_holds_the_thought_off(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """Both trips have a welcome-back row; the hotel's (absorbed) holds his "no". After the merge their row is
    still there, and the welcome-back thought is still held off."""
    from datetime import date, timedelta

    from pisa.services.surprise import SurpriseService
    from tests.test_two_countries import FLIGHT, HOTEL

    _email(db, HOTEL, SENT)
    _refresh(client)
    (hotel,) = db.execute("SELECT id FROM trips").fetchone()
    now = clock()
    db.execute(
        "INSERT INTO morning_thoughts (id, day, kind, ref, answer, answered_at, created_at) VALUES "
        "(gen_random_uuid(), %s, 'welcome_back', %s, 'no', %s, %s)",
        (date(2026, 10, 19), hotel, now - timedelta(days=1), now - timedelta(days=1)),
    )
    from pisa.services.trips import TripsService

    original = TripsService._regroup

    async def with_its_thought(self: TripsService, conn: Any, trips: Any, now: Any, added: Any) -> None:
        other = next(t.id for t in trips if t.id != str(hotel))
        await conn.execute(
            "INSERT INTO morning_thoughts (id, day, kind, ref, created_at) VALUES (gen_random_uuid(), "
            "'2026-10-18', 'welcome_back', %s, now())",
            (other,),
        )
        await original(self, conn, trips, now, added)

    TripsService._regroup = with_its_thought  # type: ignore[method-assign]
    try:
        _email(db, FLIGHT, SENT)
        _refresh(client)
    finally:
        TripsService._regroup = original  # type: ignore[method-assign]
    assert db.execute("SELECT count(*) FROM trips").fetchone() == (1,)
    assert db.execute("SELECT answer FROM morning_thoughts WHERE ref = %s", (hotel,)).fetchone() == ("no",)
    surprise = SurpriseService(owner=owner_at(), clock=clock)

    async def declined() -> set[str]:
        async with client.app.state.runtime.pool.connection() as conn:
            return await surprise.declined(conn, now)

    assert client.portal.call(declined) == {"welcome_back"}


def test_a_merge_repoints_only_the_welcome_back_thought(client, db) -> None:  # type: ignore[no-untyped-def]
    """A stuck-task thought whose (task) id happens to equal the absorbed trip's never moves."""
    from datetime import date

    from tests.test_two_countries import FLIGHT, HOTEL

    _email(db, HOTEL, SENT)
    _refresh(client)
    (hotel,) = db.execute("SELECT id FROM trips").fetchone()
    db.execute(
        "INSERT INTO morning_thoughts (id, day, kind, ref, created_at) VALUES (gen_random_uuid(), %s, "
        "'stuck_task', %s, now())",
        (date(2026, 10, 1), hotel),
    )
    _email(db, FLIGHT, SENT)
    _refresh(client)
    assert db.execute("SELECT ref FROM morning_thoughts").fetchall() == [(hotel,)]


# --- item 7: no deadlock between a refresh and a landing ------------------------------------------------------


def test_a_landing_takes_the_readers_lock_before_any_trip_row(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """`on_context` takes the shared TRIPS lock first: while a refresh holds the reader's lock, a landing
    waits for it holding no trip row (so the two never wait on each other's rows), and no row is locked."""
    import asyncio

    from pisa.domain import zone as zone_rules
    from pisa.infra import locks
    from pisa.services.zone import Report
    from tests.test_two_countries import FLIGHT, _phone

    _email(db, FLIGHT, SENT)
    _refresh(client)
    phone = _phone(db, "iPhone")
    clock.now = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)
    trips = client.app.state.runtime.trips

    async def attempt(budget: float) -> str:
        try:
            await asyncio.wait_for(
                trips.on_context(None, zone_rules.PhoneContext("America/New_York", "US"), clock.now,
                                 Report(phone, "America/New_York", "US")),
                budget,
            )  # fmt: skip
        except TimeoutError:
            return "waited"
        return "done"

    def others_on_the_lock() -> int:
        """Other sessions of this database holding or waiting on the TRIPS lock (the cancelled landing's
        transaction, until the pool has rolled it back)."""
        row = db.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 AND objid = %s "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND pid <> pg_backend_pid()",
            (locks.TRIPS,),
        ).fetchone()
        return int(row[0]) if row else 0

    db.execute("SELECT pg_advisory_lock(%s)", (locks.TRIPS,))  # a refresh holds it
    try:
        assert client.portal.call(attempt, 1.0) == "waited"
    finally:
        db.execute("SELECT pg_advisory_unlock(%s)", (locks.TRIPS,))
    # the cancelled landing's transaction ends first (its wait cancelled, the pool rolls it back): run 1 of
    # 45552775090 started the second landing while it was still being torn down, on a loaded runner
    deadline = time.monotonic() + 30
    while others_on_the_lock() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert others_on_the_lock() == 0, "the cancelled landing still holds or waits on the lock"
    # no lock held by anyone: the landing goes through; the budget is for a loaded runner, not the lock
    assert client.portal.call(attempt, 30.0) == "done"


def test_the_readers_snapshot_is_locked_in_id_order(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`_shapes` locks the snapshot's rows (`FOR UPDATE`, by id) before the reader writes: a writer that
    doesn't take the reader's lock waits for the rows (the earlier `_recheck` is gone: nothing can change)."""
    from tests.test_order_fixes import _between
    from tests.test_two_countries import FLIGHT, HOTEL

    _email(db, HOTEL, SENT)
    _refresh(client)
    (hotel,) = db.execute("SELECT id FROM trips").fetchone()
    _email(db, FLIGHT, SENT)
    seen = _between(monkeypatch, db, f"UPDATE trips SET title = 'x' WHERE id = '{hotel}'")
    _refresh(client)
    assert seen == ["waited"]


# --- item 6: their own words on a flight, under the reader's lock ----------------------------------------------


def test_his_words_claim_takes_the_readers_lock_and_its_trip_inside_it(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`said_claim` takes the shared TRIPS lock and looks the trip up inside it: a stale trip id (the trip
    merged into another since `flight_by_words`) never reaches the row."""
    import uuid

    from pisa.infra import locks
    from pisa.services.money_back import MoneyBackService
    from tests.test_money import a_trip, rt

    held: list[bool] = []
    original = MoneyBackService._upsert_claim

    async def seen(self: MoneyBackService, conn: Any, **kw: Any) -> Any:
        row = await (
            await conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objid = %s "
                "AND mode = 'ShareLock' AND granted AND pid = pg_backend_pid())",
                (locks.TRIPS,),
            )
        ).fetchone()
        held.append(bool(row[0]))
        return await original(self, conn, **kw)

    monkeypatch.setattr(MoneyBackService, "_upsert_claim", seen)
    a_trip(client, db)
    (trip,) = db.execute("SELECT id FROM trips").fetchone()
    money = rt(client).money_back
    _, leg = client.portal.call(lambda: money.flight_by_words("AZ5287"))
    client.portal.call(lambda: money.said_claim(leg, uuid.uuid4(), "delay", 250))  # a trip id gone since
    assert held == [True]
    assert db.execute("SELECT trip_id, delay_min FROM flight_claims").fetchone() == (trip, 250)


def test_his_words_claim_whose_trip_went_is_kept_never_a_500(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The trip goes between the lookup and the write (an FK error): his words are kept, no error."""
    import uuid

    from pisa.services.money_back import MoneyBackService
    from tests.test_money import a_trip, rt

    a_trip(client, db)
    money = rt(client).money_back
    _, leg = client.portal.call(lambda: money.flight_by_words("AZ5287"))
    original = MoneyBackService._flight_legs

    async def a_merged_trip(self: MoneyBackService, conn: Any) -> Any:
        return [(uuid.uuid4(), x) for _, x in await original(self, conn)]

    monkeypatch.setattr(MoneyBackService, "_flight_legs", a_merged_trip)
    claim = client.portal.call(lambda: money.said_claim(leg, None, "delay", 250))
    assert claim["id"]
    assert db.execute("SELECT trip_id, delay_min, source FROM flight_claims").fetchone() == (
        None,
        250,
        "said",
    )


# --- item 8: 0094 keys a keyless leg's memory as the night does ----------------------------------------------


# --- the merge (the review's later fixes) ---------------------------------------------------------------

_THREADED = {
    "o1": ("t-o1", [{"kind": "order", "value": "402-1111111"}], "Ordine 1", 0),
    "o2": ("t-o2", [{"kind": "order", "value": "402-2222222"}], "Ordine 2", 1),
    "inv": ("t-o2", [{"kind": "order", "value": "402-1111111"}, {"kind": "order", "value": "402-2222222"}],
            "Re: Ordine 2", 2),
}  # fmt: skip


def _link_as_intake(db: Any, mails: dict[str, Any], order: tuple[str, ...]) -> None:
    """As intake does: an email is linked, then marked done (`_finish`), one at a time in reading order."""
    import asyncio
    from datetime import timedelta

    from pisa.services import things
    from tests.test_proactivity import T0, _attend

    for mid, (thread, _, subject, hours) in mails.items():
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
            "intake_status) VALUES (%s, %s, 'Shop', 'ordini@shop.example.invalid', %s, %s, ARRAY['INBOX'], "
            "'pending')",
            (mid, thread, subject, T0 - timedelta(days=1) + timedelta(hours=hours)),
        )

    async def go() -> None:
        async with _attend(db, T0) as env, env.pool.connection() as conn:
            for mid in order:
                thread, keys, subject, _ = mails[mid]
                await things.link_email(conn, message_id=mid, thread_id=thread, keys=keys, subject=subject,
                                        gist=None, now=T0, sender="ordini@shop.example.invalid")  # fmt: skip
                await conn.execute("UPDATE email_messages SET intake_status = 'done' WHERE id = %s", (mid,))

    asyncio.run(go())


@pytest.mark.parametrize("order", list(itertools.permutations(_THREADED)))
def test_two_orders_in_one_thread_stay_two_things(db: Any, order: tuple[str, ...]) -> None:
    """H1: a shop's same-subject emails share a Gmail thread; the invoice naming both orders sits in o2's.
    In every reading order (as intake reads: linked, then done): the invoice with its lowest order, o2 on its
    own."""
    _link_as_intake(db, _THREADED, order)
    assert _shape(db) == [(["inv", "o1"], ["402-1111111"]), (["o2"], ["402-2222222"])]


def test_a_cancel_note_goes_only_with_its_leg(client: Any, db: Any) -> None:
    """Review item 4 (`_absorb`'s `skip`): a cancellation naming AZ613 follows AZ613, never AZ609."""
    for mail in (OUT, RET, DEC_OUT, DEC_BACK):
        _email(db, mail, SENT)
    _email(
        db,
        ("x1", "Il volo AZ613 è stato cancellato", "Ci scusiamo: il volo AZ 613 è stato cancellato."),
        SENT,
    )
    _refresh(client)
    _y, x, _z = _arrange(client, db, [["AZ608"], ["AZ609", "AZ613"], ["AZ612"]])
    db.execute("UPDATE trips SET cancel_mentions = ARRAY['x1'] WHERE id = %s", (x,))
    _email(db, HOT, SENT)
    _refresh(client)
    on613, on609 = _holding(db, "AZ613"), _holding(db, "AZ609")
    assert on613 != on609
    mentions = dict(db.execute("SELECT id, cancel_mentions FROM trips").fetchall())
    assert mentions[on613] == ["x1"] and mentions[on609] == []


def test_a_miss_s_reason_follows_the_trip_that_stays(client: Any, db: Any) -> None:
    """M1: voice6's `pisa_unknowns.reason = 'trip:<id>'` is repointed when its trip is absorbed: after
    a regroup that removes a trip, every reason still names a trip that exists."""
    import uuid

    for mail in (OUT, RET, DEC_OUT, DEC_BACK):
        _email(db, mail, SENT)
    _refresh(client)
    groups = _arrange(client, db, [["AZ608"], ["AZ609", "AZ613"], ["AZ612"]])
    for n, tid in enumerate(groups):
        db.execute(
            "INSERT INTO pisa_unknowns (id, source, ref, question, about, answer_by, opened_at, updated_at, reason) "
            "VALUES (%s, 'miss', %s, 'Perché mi sono sbagliato?', 'self', 'his_data', now(), now(), %s)",
            (uuid.uuid4(), f"p{n}", f"trip:{tid}"),
        )
    before = {str(r[0]) for r in db.execute("SELECT id FROM trips").fetchall()}
    _email(db, HOT, SENT)
    _refresh(client)
    after = {str(r[0]) for r in db.execute("SELECT id FROM trips").fetchall()}
    assert before - after, "the regroup removed no trip: the test proves nothing"
    reasons = [r[0].removeprefix("trip:") for r in db.execute("SELECT reason FROM pisa_unknowns").fetchall()]
    assert reasons and all(r in after for r in reasons)
