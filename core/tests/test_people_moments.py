# ruff: noqa: E501
"""Moments, thank-yous in their style, wishes at their local hour, their clock. One test per behaviour; each
fails with its fix reverted."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from structlog.testing import capture_logs

from pisa.domain import people_offers as offers
from tests.assistant_env import set_settings
from tests.test_people import birthday, event, hdr, people, person, received, rome, run

REPO = Path(__file__).resolve().parents[2]


def home(db: Any, name: str = "Casa, Via Nuova Inventata 10, Roma") -> None:
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('profile_home', %s) ON CONFLICT (key) DO UPDATE SET "
        "value = EXCLUDED.value",
        (
            Jsonb(
                {
                    "lat": 41.87,
                    "lon": 12.52,
                    "name": name,
                    "source": "owner",
                    "confirmed": True,
                    "place_id": None,
                }
            ),
        ),
    )


def sent_body(db: Any, mid: str, to: str, at: datetime, body: str) -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_address, to_addresses, subject, sent_at, labels, body, "
        "fetched_at) VALUES (%s, %s, 'me@example.invalid', %s, 'Ciao', %s, '{SENT}', %s, %s)",
        (mid, f"t-{mid}", [to], at, body, at),
    )


def svc(client: Any) -> Any:
    return people(client).offers


def plan(client: Any, db: Any, clock: Any) -> None:
    """The hourly sync's people steps, as the sync runs them."""
    run(client, people(client).sync)


def deliver(client: Any) -> int:
    return int(run(client, svc(client).deliver))


def offers_rows(db: Any) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT kind, line, draft, sent_at IS NOT NULL FROM people_offers ORDER BY created_at"
    ).fetchall()


def notices(db: Any) -> list[tuple[Any, ...]]:
    return db.execute("SELECT text, ritual, sheet_id FROM notifications ORDER BY created_at").fetchall()


# --- row 1: the matchmaker of moments --------------------------------------------------------------------------

SENT = date(2026, 11, 2)  # a Monday


@pytest.mark.parametrize(
    "body, found",
    [("Ciao! Sarò a Roma giovedì per lavoro. Un abbraccio", [("in_city", "roma", "2026-11-05")]),
     ("I'll be in Rome on Thursday!", [("in_city", "roma", "2026-11-05")]),
     ("Giovedì sono libera, se vuoi.", [("free", None, "2026-11-05")]),
     ("Arrivo a Milano il 12 novembre.", [("in_city", "milano", "2026-11-12")]),
     ("Non sarò a Roma giovedì.", []), ("Sarai a Roma giovedì?", []), ("Sono libera giovedì?", []), ("Forse sarò a Roma giovedì.", []),
     ("Se riesco sono a Roma domani.", []), ("Sono a Roma giovedì e venerdì.", []),
     ("Ciao\n> sarò a Roma giovedì", []), ("Sarò a Gotham giovedì.", []), ("Sono a Roma.", [])],
)  # fmt: skip
def test_their_own_words_one_city_pisa_knows_one_day(body: str, found: list[tuple[Any, ...]]) -> None:
    got = offers.moments(body, SENT, "en-US" if body.startswith("I'll") else "it-IT")
    assert [(m.kind, m.city, m.first.isoformat()) for m in got] == found


def test_laura_is_in_rome_on_thursday_and_he_is_free(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"], city="Milano")
    clock.now = rome(2026, 11, 2, 11)
    received(db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10),
             "Ciao! Sarò a Roma giovedì per un convegno.")  # fmt: skip
    plan(client, db, clock)
    rows = offers_rows(db)
    assert rows == [("moment", "Laura è a Roma giovedì, ti va un caffè?",
                     "Ciao Laura! Ho visto che sei a Roma giovedì: ti va un caffè?", False)]  # fmt: skip
    assert deliver(client) == 1
    text, ritual, sheet = notices(db)[0]
    assert (text, ritual) == ("Laura è a Roma giovedì, ti va un caffè?", "people")
    got = client.get(f"/v1/sheets/sheet:{sheet}", headers=hdr(db))
    assert got.status_code == 200
    blocks = got.json()["data"]["blocks"]
    assert blocks[1] == {"type": "fact", "label": "Fonte", "value": "la sua email del 02/11"}
    buttons = [b for b in blocks if b["type"] == "handoff"]
    assert [b["url"][:21] for b in buttons] == ["https://wa.me/?text=C", "sms:&body=Ciao%20Laur"]
    # their tap is recognised (the preparation is kept when the offer goes out), and nothing was sent
    assert run(
        client, client.app.state.runtime.handoffs.opened, None, buttons[0]["handoff_id"], f"sheet:{sheet}"
    )
    assert db.execute("SELECT purpose FROM handoff_preps").fetchall() == [("message",)]


def test_said_late_at_night_for_tomorrow_still_offered_in_his_morning(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    clock.now = rome(2026, 11, 2, 22)
    received(db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 21), "Sarò a Roma domani!")
    plan(client, db, clock)
    assert deliver(client) == 0  # never at night
    clock.now = rome(2026, 11, 3, 9, 5)
    assert deliver(client) == 1
    assert notices(db)[0][0] == "Laura è a Roma oggi, ti va un caffè?"  # said as it is when it goes out


def test_never_inferred_from_where_anyone_is(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    person(
        db, "Laura Bianchi", ["laura@example.invalid"], city="Roma", tz="Europe/Rome"
    )  # lives in their city
    person(db, "Marco Rossi", ["marco@example.invalid"])
    clock.now = rome(2026, 11, 2, 11)
    received(db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10), "Ciao, a presto!")
    received(
        db, "m2", "Marco Rossi", "marco@example.invalid", rome(2026, 11, 2, 10), "Sarò a Milano giovedì."
    )
    received(db, "m3", "Marco Rossi", "marco@example.invalid", rome(2026, 11, 2, 10), "Sono libero giovedì.")
    plan(client, db, clock)
    assert (
        offers_rows(db) == []
    )  # no words: nothing; another city: nothing; free but not living in their city: nothing


def test_not_for_someone_out_of_his_orbits(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"], orbit=False)  # "non ricordarmi più di Laura"
    clock.now = rome(2026, 11, 2, 11)
    received(
        db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10), "Sarò a Roma giovedì."
    )
    plan(client, db, clock)
    assert offers_rows(db) == []


def test_free_only_when_they_live_in_his_city_and_his_day_has_a_free_hour(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"], city="Rome")
    clock.now = rome(2026, 11, 2, 11)
    for h in range(9, 22):  # Thursday booked solid
        event(db, f"b{h}", "Lavoro", rome(2026, 11, 5, h), rome(2026, 11, 5, h + 1))
    received(
        db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10), "Giovedì sono libera!"
    )
    received(
        db, "m2", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10), "E sono libera venerdì."
    )
    plan(client, db, clock)
    assert offers_rows(db) == [("moment", "Laura venerdì è libera, ti va un caffè?",
                                "Ciao Laura! Sei libera venerdì? Ti va un caffè?", False)]  # fmt: skip


def test_once_a_week_per_person_and_in_his_trip_s_city(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    clock.now = rome(2026, 11, 2, 11)
    db.execute(
        "INSERT INTO trips (id, title, from_city, to_city, depart_at, arrive_at, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'New York', 'Roma', 'New York', %s, %s, now(), now())",
        (rome(2026, 11, 6, 10), rome(2026, 11, 6, 20)),
    )
    received(db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 9), "Sarò a Roma giovedì.")
    received(
        db, "m2", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 9), "Sabato sarò a New York!"
    )
    plan(client, db, clock)
    assert [r[1] for r in offers_rows(db)] == ["Laura è a Roma giovedì, ti va un caffè?"]  # one a week
    db.execute("DELETE FROM app_settings WHERE key = 'people_synced_at'")
    clock.now = rome(2026, 11, 3, 11)
    plan(client, db, clock)
    assert len(offers_rows(db)) == 1  # the next day: still one this week
    db.execute("UPDATE people_offers SET created_at = created_at - interval '8 days'")
    plan(client, db, clock)
    assert [r[1] for r in offers_rows(db)][
        -1
    ] == "Laura è a New York sabato, ti va un caffè?"  # they're there then


def test_their_shared_calendar_names_the_city(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    clock.now = rome(2026, 11, 2, 11)
    db.execute("INSERT INTO calendars (id, name) VALUES ('laura@example.invalid', 'Laura')")
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, start_date, end_date, "
        "location, status) VALUES ('laura@example.invalid', 'e1', 'Convegno', %s, %s, true, %s, %s, 'Rome, Italy', "
        "'confirmed')",
        (rome(2026, 11, 5), rome(2026, 11, 6), date(2026, 11, 5), date(2026, 11, 6)),
    )
    event(
        db, "mine", "Viaggio a Roma", rome(2026, 11, 4, 10), rome(2026, 11, 4, 11)
    )  # HIS calendar: never theirs
    plan(client, db, clock)
    run(client, svc(client).deliver)
    sheet = db.execute("SELECT data FROM people_offers").fetchone()[0]
    assert sheet["blocks"][0]["text"] == "Laura è a Roma giovedì, ti va un caffè?"
    assert sheet["blocks"][1]["value"] == "il suo calendario"
    db.execute("DELETE FROM calendar_events WHERE calendar_id = 'laura@example.invalid'")
    db.execute("DELETE FROM app_settings WHERE key = 'people_synced_at'")
    plan(client, db, clock)
    assert db.execute("SELECT count(*) FROM person_moments").fetchone() == (0,)  # gone with the event


def test_an_email_s_moment_goes_with_its_email(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    clock.now = rome(2026, 11, 2, 11)
    received(db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 9), "Sarò a Roma giovedì.")
    plan(client, db, clock)
    assert db.execute("SELECT count(*) FROM person_moments").fetchone() == (1,)
    db.execute("DELETE FROM email_messages")
    db.execute("DELETE FROM app_settings WHERE key = 'people_synced_at'")
    plan(client, db, clock)
    assert db.execute("SELECT count(*) FROM person_moments").fetchone() == (0,)


# --- row 2: thank-you notes in their style --------------------------------------------------------------------------


def test_his_style_from_his_own_sent_emails() -> None:
    mine = offers.style(
        ["Ciao Marco!\nci vediamo.\nUn abbraccio, R.", "Ciao Marco,\nok!\nUn abbraccio, R.\n> Marco: ..."],
        "it-IT",
    )
    assert mine == offers.Style("Ciao", "Un abbraccio, R.", True, 2)
    assert offers.thanks_draft(mine, "Marco Rossi", "event", "it-IT") == (
        "Ciao Marco! Grazie ancora per ieri, è stato bellissimo!\nUn abbraccio, R."
    )
    plain = offers.style(["Caro Paolo,\nti allego il file.\n+39 333 1234567"] * 2, "it-IT")
    assert (plain.greeting, plain.sign, plain.bang) == ("Caro", None, False)  # a number is never a sign-off
    assert offers.thanks_draft(plain, "Paolo", "gift", "it-IT") == "Caro Paolo, Grazie mille per il regalo."
    assert offers.style([], "en-US") == offers.Style("Hi", None, True, 0)


@pytest.mark.parametrize(
    "body, sent",
    [("Ti ho spedito un pensierino!", True), ("Ti è arrivato il pensierino che ti ho spedito?", True),
     ("I sent you a little something for your birthday.", True), ("Non ti ho mandato il regalo, scusa.", False),
     ("Ti ho mandato il report.", False), ("Ciao\n> ti ho spedito un regalo", False)],
)  # fmt: skip
def test_their_own_words_say_a_gift_reached_him(body: str, sent: bool) -> None:
    assert offers.gift_from_sender(body) is sent


def test_the_morning_after_a_dinner_a_thank_you_with_its_button(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    person(db, "Marco Rossi", ["marco@example.invalid"])
    sent_body(
        db,
        "s1",
        "Marco Rossi <marco@example.invalid>",
        rome(2026, 10, 20, 9),
        "Ciao Marco!\nA giovedì.\nUn abbraccio, R.",
    )
    sent_body(
        db,
        "s2",
        "Marco Rossi <marco@example.invalid>",
        rome(2026, 10, 25, 9),
        "Ciao Marco!\nPerfetto.\nUn abbraccio, R.",
    )
    guests = [{"email": "marco@example.invalid", "name": "Marco"}]
    event(db, "d1", "Cena da Marco", rome(2026, 11, 2, 20), rome(2026, 11, 2, 23), guests=guests)
    event(db, "m1", "Riunione progetto", rome(2026, 11, 2, 9), rome(2026, 11, 2, 10), guests=guests)
    clock.now = rome(2026, 11, 3, 8)
    plan(client, db, clock)
    assert offers_rows(db) == [("thanks", "Ieri cena da Marco: gli mandi un grazie? Ho una bozza.",
                                "Ciao Marco! Grazie ancora per ieri, è stato bellissimo!\nUn abbraccio, R.", False)]  # fmt: skip
    assert run(client, people(client).morning_lines, "it-IT") == []  # no longer a briefing line
    clock.now = rome(2026, 11, 3, 9, 30)
    assert deliver(client) == 0  # from 10:00
    clock.now = rome(2026, 11, 3, 10, 5)
    assert deliver(client) == 1 and deliver(client) == 0  # once
    assert run(client, people(client).drafts)[-1][1] == "thanks"
    clock.now = rome(2026, 11, 4, 8)
    plan(client, db, clock)
    assert len(offers_rows(db)) == 1  # never the day after, never twice


def test_a_gift_from_their_own_email_or_a_store_naming_them(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    person(db, "Paolo Verdi", ["paolo@example.invalid"])
    clock.now = rome(2026, 11, 3, 11)
    received(
        db,
        "g1",
        "Laura Bianchi",
        "laura@example.invalid",
        rome(2026, 11, 3, 9),
        "Ciao! Ti ho spedito un pensierino, fammi sapere!",
    )
    received(
        db,
        "g2",
        "Amazon.it",
        "spedizioni@amazon.it",
        rome(2026, 11, 3, 9),
        "Paolo Verdi ti ha inviato un regalo. Arriverà venerdì.",
    )
    received(
        db,
        "g3",
        "Promo",
        "news@gifts-example.invalid",
        rome(2026, 11, 3, 9),
        "Paolo Verdi ti ha inviato un regalo: clicca qui.",
    )
    received(
        db,
        "g4",
        "Paolo Verdi",
        "paolo@example.invalid",
        rome(2026, 11, 3, 9),
        "Non ti ho mandato il regalo, scusa.",
    )
    received(
        db,
        "g5",
        "Amazon.it",
        "regali@amazon.it",
        rome(2026, 11, 3, 9),
        "Laura ti ha inviato un buono regalo.",
    )  # first name only
    plan(client, db, clock)
    rows = db.execute(
        "SELECT o.ref, p.name, o.line FROM people_offers o JOIN people p ON p.id = o.person_id ORDER BY o.ref"
    ).fetchall()
    assert rows == [("thanks:email:g1", "Laura Bianchi", "Laura ti ha mandato un regalo: gli mandi un grazie? Ho una bozza."),
                    ("thanks:email:g2", "Paolo Verdi", "Paolo ti ha mandato un regalo: gli mandi un grazie? Ho una bozza.")]  # fmt: skip


# --- row 3: wishes at their local hour -------------------------------------------------------------------------------


def test_a_birthday_wish_at_their_morning_with_their_hour_now(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    marco = person(db, "Marco Rossi", ["marco@example.invalid"], tz="America/New_York", city="New York")
    birthday(db, marco, 11, 3)
    clock.now = rome(2026, 11, 3, 8)
    plan(client, db, clock)
    due = db.execute("SELECT due_at, until, line FROM people_offers WHERE kind = 'wish'").fetchone()
    assert due[0] == rome(2026, 11, 3, 15) and due[1] == rome(2026, 11, 4, 3)  # their 9:00 → their 21:00
    assert due[2] == "A New York sono le 9: vuoi fare gli auguri a Marco?"
    # the morning briefing's line is still there, and it agrees
    assert run(client, people(client).morning_lines, "it-IT")[0].endswith(
        "glielo mandi alle 9 loro (le 15 tue)?"
    )
    assert deliver(client) == 0
    clock.now = rome(2026, 11, 3, 15, 20)
    assert deliver(client) == 1
    assert (
        notices(db)[0][0] == "A New York sono le 9:20: vuoi fare gli auguri a Marco?"
    )  # their hour as it goes out


def test_his_quiet_hours_move_it_and_their_night_drops_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": {"start": "14:00", "end": "16:30"}}})
    marco = person(db, "Marco Rossi", tz="America/New_York", city="New York")
    birthday(db, marco, 11, 3)
    anna = person(db, "Anna Neri", tz="Pacific/Auckland", city="Auckland")
    birthday(db, anna, 11, 4)  # their 9:00-21:00 of 4 Nov is 21:00 to 9:00 theirs: all their night
    clock.now = rome(2026, 11, 3, 8)
    plan(client, db, clock)
    rows = db.execute(
        "SELECT p.name, o.due_at FROM people_offers o JOIN people p ON p.id = o.person_id"
    ).fetchall()
    assert rows == [("Marco Rossi", rome(2026, 11, 3, 16, 30))]
    assert (
        offers.wish_moment("Pacific/Auckland", (11, 4), "Europe/Rome", rome(2026, 11, 3, 8), lambda _: False)
        is None
    )


def test_a_wish_in_his_style_and_same_zone_says_it_plainly() -> None:
    mine = offers.Style("Ehi", "R.", True, 3)
    assert (
        offers.wish_draft(mine, "birthday", "Marco Rossi", "it-IT")
        == "Ehi Marco! Tanti auguri di buon compleanno!\nR."
    )
    at = datetime(2026, 11, 3, 9, tzinfo=offers.ZoneInfo("Europe/Rome"))
    assert (
        offers.wish_line("Marco", "birthday", at, None, True, "it-IT")
        == "Sono le 9: vuoi fare gli auguri a Marco?"
    )
    assert (
        offers.wish_line("Marco", "birthday", at, None, False, "en-US")
        == "Where they are it's 9: wish Marco a happy birthday?"
    )


# --- row 4: Rome's clock in New York -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utc, tz, verdict, line",
    [(datetime(2026, 11, 4, 22, 40, tzinfo=UTC), "Europe/Rome", "late", "Da mamma sono le 23:40: domani, dalle 9 loro (domani alle 3 tue)?"),
     (datetime(2026, 11, 4, 20, 0, tzinfo=UTC), "Europe/Rome", "good", "Da mamma sono le 21: puoi chiamare."),
     (datetime(2026, 11, 4, 6, 0, tzinfo=UTC), "Europe/Rome", "early", "Da mamma sono le 7: più tardi, dalle 9 loro (le 3 tue)?"),
     (datetime(2026, 11, 7, 8, 30, tzinfo=UTC), "Europe/Rome", "early", "Da mamma sono le 9:30: più tardi, dalle 10 loro (le 4 tue)?")],
)  # fmt: skip
def test_a_decent_hour_to_call_there(utc: datetime, tz: str, verdict: str, line: str) -> None:
    found = offers.clock(tz, "America/New_York", utc)
    assert found is not None and found.verdict == verdict
    assert found.line(offers.their_place("Mamma", "madre", "it-IT"), "it-IT") == line


def test_their_clock_on_the_person_s_page_and_by_voice(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    person(db, "Mamma", relation="madre", tz="Europe/Rome", city="Roma", source="owner")
    person(db, "Paolo Verdi")
    from pisa.domain import zone

    client.app.state.runtime.owner.place = zone.PhoneContext("America/New_York")
    clock.now = datetime(2026, 11, 4, 22, 40, tzinfo=UTC)
    got = client.get("/v1/people", headers=hdr(db)).json()["people"]
    mamma = next(p for p in got if p["name"] == "Mamma")
    assert mamma["clock"]["call"] == "late" and mamma["clock"]["at"] == "2026-11-04T23:40:00+01:00"
    assert mamma["clock"]["call_from"] == "2026-11-05T09:00:00+01:00"
    assert mamma["clock"]["line"].startswith("Da mamma sono le 23:40: domani")
    assert next(p for p in got if p["name"] == "Paolo Verdi")["clock"] is None
    said = run(client, people(client).hour, "mamma", "it-IT")
    assert "To call: Da mamma sono le 23:40: domani" in said


# --- rails ---------------------------------------------------------------------------------------------------------------


def test_rail_never_in_quiet_hours_at_night_or_for_someone_hidden(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    ids = {}
    for name in ("Marco Rossi", "Laura Bianchi", "Paolo Verdi"):
        ids[name] = person(db, name, tz="Europe/Rome")
        birthday(db, ids[name], 11, 3)
    clock.now = rome(2026, 11, 3, 8)
    plan(client, db, clock)
    set_settings(
        db, {"quiet": {"until": rome(2026, 11, 3, 12).isoformat()}}
    )  # "basta notifiche fino alle 12"
    clock.now = rome(2026, 11, 3, 9, 30)
    assert deliver(client) == 0
    set_settings(db, {"quiet": {"until": None}})
    db.execute("UPDATE people SET hidden = true WHERE id = %s", (ids["Marco Rossi"],))
    assert deliver(client) == 1 and deliver(client) == 0  # one every 30 minutes
    db.execute("UPDATE people_offers SET sent_at = sent_at - interval '1 hour' WHERE sent_at IS NOT NULL")
    assert deliver(client) == 1
    db.execute("UPDATE people_offers SET sent_at = sent_at - interval '1 hour' WHERE sent_at IS NOT NULL")
    assert deliver(client) == 0  # Marco stays unsaid
    assert offers.daytime(rome(2026, 11, 3, 21), "Europe/Rome") is False  # never at night


def test_rail_a_stop_pauses_the_offers() -> None:
    from pisa.services import worker_steps

    assert "people" in worker_steps.INITIATIVE_STEPS  # the minute step that delivers them


def test_rail_logs_carry_ids_and_counts_only(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    clock.now = rome(2026, 11, 2, 11)
    received(
        db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10), "Sarò a Roma giovedì."
    )
    with capture_logs() as logs:
        plan(client, db, clock)
        deliver(client)
    text = json.dumps(logs, default=str)
    assert any(e["event"] == "people_offer_sent" for e in logs)
    assert all(w not in text for w in ("Laura", "laura@", "Roma", "giovedì", "caffè"))


def test_rail_google_s_disconnect_forgets_moments_and_their_offers(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.people import forget_google

    home(db)
    person(db, "Laura Bianchi", ["laura@example.invalid"], source="owner")
    clock.now = rome(2026, 11, 2, 11)
    received(
        db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10), "Sarò a Roma giovedì."
    )
    plan(client, db, clock)
    assert db.execute("SELECT count(*) FROM people_offers").fetchone() == (1,)

    async def forget() -> None:
        async with client.app.state.runtime.pool.connection() as conn:
            await forget_google(conn)

    client.portal.call(forget)
    assert db.execute(
        "SELECT (SELECT count(*) FROM person_moments), (SELECT count(*) FROM people_offers)"
    ).fetchone() == (0, 0)


def test_rail_delete_and_merge_carry_the_moments(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    a = person(db, "Laura Bianchi", ["laura@example.invalid"])
    b = person(db, "Laura B", ["laura.b@example.invalid"])
    clock.now = rome(2026, 11, 2, 11)
    received(db, "m1", "Laura B", "laura.b@example.invalid", rome(2026, 11, 2, 10), "Sarò a Roma giovedì.")
    plan(client, db, clock)
    run(client, people(client).merge, b, a)
    assert db.execute("SELECT person_id::text FROM person_moments").fetchall() == [(a,)]
    run(client, people(client).delete, a)
    assert db.execute("SELECT count(*) FROM person_moments").fetchone() == (0,)


# --- the contract ----------------------------------------------------------------------------------------------------------


def test_contract_the_fixtures_parse_and_the_schema_knows_people() -> None:
    from pisa.api import schemas
    from pisa.domain.sheet import SheetData
    from tests.live_app import message_of

    fixtures = REPO / "protocol" / "fixtures"
    note = message_of((fixtures / "valid" / "notify_people_offer.json").read_text())
    assert getattr(note, "ritual", None) == "people"
    sheet = schemas.RitualSheet.model_validate(
        json.loads((fixtures / "rest" / "people_offer_sheet.json").read_text())
    )
    data = SheetData.model_validate(sheet.data)
    assert [b.type for b in data.blocks] == ["text", "handoff", "handoff"]
    listed = schemas.People.model_validate(json.loads((fixtures / "rest" / "people.json").read_text()))
    assert listed.people[0].clock is not None and listed.people[0].clock.call == "good"
    schema = json.loads((REPO / "protocol" / "schema" / "pisa-device-protocol.v1.json").read_text())
    assert "people" in json.dumps(schema).split('"ritual"')[1][:200]


def test_contract_an_offer_s_sheet_needs_the_device_and_is_404_until_it_went_out(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    marco = person(db, "Marco Rossi", tz="Europe/Rome")
    birthday(db, marco, 11, 3)
    clock.now = rome(2026, 11, 3, 8)
    plan(client, db, clock)
    sheet = db.execute("SELECT id FROM people_offers").fetchone()[0]
    assert client.get(f"/v1/sheets/sheet:{sheet}").status_code == 401
    assert client.get(f"/v1/sheets/sheet:{sheet}", headers=hdr(db)).status_code == 404
    clock.now = rome(2026, 11, 3, 9, 5)
    assert deliver(client) == 1
    assert (
        client.get(f"/v1/sheets/sheet:{sheet}", headers=hdr(db)).json()["data"]["title"] == "Auguri a Marco"
    )


# --- the review of fc1d212 ------------------------------------------------------------------------------


def test_a_store_s_notice_only_from_its_own_exact_domain(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    assert offers.store_domain("regali@amazon.it") is True
    assert (
        offers.store_domain("seller@marketplace.amazon.it") is False
    )  # a buyer-seller relay: a stranger writes
    assert offers.store_domain("transaction@etsy.com") is False  # Etsy conversations are strangers' words
    person(db, "Paolo Verdi", ["paolo@example.invalid"])
    clock.now = rome(2026, 11, 3, 11)
    received(
        db,
        "g6",
        "Venditore",
        "abc123@marketplace.amazon.it",
        rome(2026, 11, 3, 9),
        "Paolo Verdi ti ha inviato un regalo.",
    )
    plan(client, db, clock)
    assert offers_rows(db) == []


def test_google_s_disconnect_forgets_every_unsent_offer(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.people import forget_google

    marco = person(db, "Marco Rossi", source="owner", tz="Europe/Rome")
    birthday(db, marco, 11, 3, source="owner")  # their own date: the person and the date stay
    clock.now = rome(2026, 11, 3, 8)
    plan(client, db, clock)
    assert db.execute("SELECT kind FROM people_offers").fetchall() == [("wish",)]

    async def forget() -> None:
        async with client.app.state.runtime.pool.connection() as conn:
            await forget_google(conn)

    client.portal.call(forget)
    assert db.execute("SELECT count(*) FROM people_offers").fetchone() == (
        0,
    )  # it carried their learned sign-off


def test_a_wish_lapses_when_its_date_is_gone(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    marco = person(db, "Marco Rossi", tz="Europe/Rome")
    birthday(db, marco, 11, 3)
    clock.now = rome(2026, 11, 3, 8)
    plan(client, db, clock)
    db.execute("DELETE FROM person_dates")
    clock.now = rome(2026, 11, 3, 9, 30)
    assert deliver(client) == 0
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)


def test_a_gift_s_thank_you_lapses_with_its_email(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    person(db, "Laura Bianchi", ["laura@example.invalid"])
    clock.now = rome(2026, 11, 3, 11)
    received(
        db,
        "g1",
        "Laura Bianchi",
        "laura@example.invalid",
        rome(2026, 11, 3, 9),
        "Ti ho spedito un pensierino!",
    )
    plan(client, db, clock)
    assert len(offers_rows(db)) == 1
    db.execute("DELETE FROM email_messages")  # Deleted (90 days, or by them)
    assert deliver(client) == 0
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)


def test_domani_is_the_day_after_their_day_of_writing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    person(db, "Laura Bianchi", ["laura@example.invalid"], tz="America/New_York")
    clock.now = rome(2026, 11, 3, 11)
    # 02:00 on 3 Nov in Rome is 20:00 on 2 Nov in New York, where she wrote it: her "domani" is 3 Nov
    received(db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 3, 2), "Sarò a Roma domani!")
    plan(client, db, clock)
    assert db.execute("SELECT first_day FROM person_moments").fetchall() == [(date(2026, 11, 3),)]


def test_a_sign_off_used_once_is_never_his() -> None:
    one = offers.style(["Ciao Paolo!\nti giro la mail qui sotto.\nMario B."], "it-IT")
    assert one.sign is None


def test_a_moment_out_of_his_orbits_since_is_never_said(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    home(db)
    laura = person(db, "Laura Bianchi", ["laura@example.invalid"])
    clock.now = rome(2026, 11, 2, 11)
    received(
        db, "m1", "Laura Bianchi", "laura@example.invalid", rome(2026, 11, 2, 10), "Sarò a Roma giovedì."
    )
    plan(client, db, clock)
    assert len(offers_rows(db)) == 1
    db.execute("UPDATE people SET orbit = false WHERE id = %s", (laura,))  # "non ricordarmi più di Laura"
    assert deliver(client) == 0
