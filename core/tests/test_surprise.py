# ruff: noqa: E501
"""Pisa's morning thought. One test (or a few) per behaviour; each fails when its part is reverted. Invented names
and places."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from pisa.domain import rituals as ritual_rules
from pisa.domain import surprise
from tests.conftest import pair

TZ = ZoneInfo("Europe/Rome")


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _settings(db: Any, **patch: Any) -> None:
    from pisa.domain import settings as setting_rules

    row = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
    value = setting_rules.apply_patch(setting_rules.with_defaults(row[0] if row else None, language="it-IT"),
                                      {"language": "it-IT", "quiet": {"hours": None}, **patch})  # fmt: skip
    db.execute("INSERT INTO app_settings (key, value) VALUES ('settings', %s) ON CONFLICT (key) DO UPDATE SET "
               "value = EXCLUDED.value", (Jsonb(value),))  # fmt: skip


def _task(db: Any, title: str, created: datetime, **cols: Any) -> uuid.UUID:
    task_id = uuid.uuid4()
    status = cols.pop("status", "open")
    extra = "".join(f", {k}" for k in cols)
    marks = "".join(", %s" for _ in cols)
    db.execute(
        f"INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at{extra}) "
        f"VALUES (%s, %s, %s, %s, 'manual', %s, %s{marks})",
        (task_id, title, title.casefold(), status, created, created, *cols.values()),
    )
    return task_id


def _trip(db: Any, city: str, back: datetime, doubt: str | None = None, status: str = "planned",
          landed: bool = True, **cols: Any) -> uuid.UUID:  # fmt: skip
    """A trip there and back: the return leg lands home at `back`; `landed`: the phone saw it."""
    trip_id = uuid.uuid4()
    there = {"kind": "flight", "at": (back - timedelta(days=6)).isoformat(), "key": f"flight|AZ1|{trip_id}|",
             "destination": [city, "US", "America/New_York"]}  # fmt: skip
    home = {"kind": "flight", "at": (back - timedelta(hours=9)).isoformat(), "until": back.isoformat(),
            "key": f"flight|AZ2|{trip_id}|", "destination": ["Milano", "IT", "Europe/Rome"]}  # fmt: skip
    extra = "".join(f", {k}" for k in cols)
    marks = "".join(", %s" for _ in cols)
    db.execute(
        "INSERT INTO trips (id, title, from_city, to_city, to_country, tz_to, depart_at, arrive_at, back_at, "
        f"status, doubt, legs, landed_keys, created_at, updated_at{extra}) VALUES (%s, %s, 'Milano', %s, 'US', "
        f"'America/New_York', %s, %s, %s, %s, %s, %s, %s, now(), now(){marks})",
        (trip_id, f"Viaggio a {city}", city, back - timedelta(days=6), back - timedelta(days=6, hours=-9), back,
         status, doubt, Jsonb([there, home]), [home["key"]] if landed else [], *cols.values()),
    )  # fmt: skip
    return trip_id


def _event(db: Any, title: str, start: datetime) -> None:
    """An event today: the morning's one thing, so the briefing names no task."""
    db.execute("INSERT INTO calendars (id, name, access_role) VALUES ('me@example.invalid', 'Personale', 'owner') "
               "ON CONFLICT DO NOTHING")  # fmt: skip
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, attendees) "
        "VALUES ('me@example.invalid', %s, %s, %s, %s, false, 'confirmed', 0)",
        (str(uuid.uuid4()), title, start, start + timedelta(minutes=30)),
    )


def _repeat(db: Any) -> None:
    """Another automatic briefing allowed a minute later (to see the day's thought is said once)."""
    db.execute("UPDATE app_settings SET value = jsonb_set(value, '{briefing,repeat_after_min}', '1') "
               "WHERE key = 'settings'")  # fmt: skip


def _open(client: Any, db: Any, clock: Any, at: str) -> tuple[str, Any]:
    """He opens the app at `at` (his time): the automatic briefing's text and its notification's ask."""
    runtime = client.app.state.runtime
    phone = uuid.UUID(pair(db, "iPhone")[0]) if not db.execute("SELECT 1 FROM devices").fetchone() else (
        db.execute("SELECT id FROM devices").fetchone()[0])  # fmt: skip
    clock.now = local(at).astimezone(UTC)
    got = client.portal.call(runtime.briefings.on_presence, phone, "app_open")
    assert got is not None, "no briefing"
    row = db.execute("SELECT text, ask_id FROM notifications WHERE id = (SELECT notification_id FROM briefings "
                     "WHERE id = %s)", (got.id,)).fetchone()  # fmt: skip
    return row[0], row[1]


# --- the rules (pure) --------------------------------------------------------------------------------------


def test_rules_the_lines_are_templates_with_a_question_and_no_model() -> None:
    assert surprise.MODEL_CALLS == 0
    text, q = surprise.welcome_back("New York", "it-IT")
    assert text == "Bentornato da New York. Com'è andata?" and q is None
    text, q = surprise.stuck_task("Rinnovare «la» patente", 21, "it-IT")
    assert text == ("Stanotte pensavo a «Rinnovare la patente»: è in lista da 21 giorni. Ti va di trovare un "
                    "primo passo da dieci minuti?")  # fmt: skip
    assert q == "Ti va di trovare un primo passo da dieci minuti?" and text.endswith(q)
    afternoon = surprise.stuck_task("Rinnovare la patente", 21, "it-IT", morning=False)[0]
    assert (
        afternoon.startswith("Pensavo a «Rinnovare la patente»:") and "Stanotte" not in afternoon
    )  # review M2
    assert surprise.welcome_back("Boston", "en-US")[0] == "Welcome back from Boston. How did it go?"
    long = surprise.stuck_task("parola " * 30, 15, "it-IT")[0]
    assert "…»" in long and len(long) < 200


def test_rules_pick_one_in_order_skip_what_he_declined_or_too_young() -> None:
    back = surprise.Candidate("welcome_back", "t1", "New York")
    stuck = surprise.Candidate("stuck_task", "k1", "Sistemare la cantina", 20)
    young = surprise.Candidate("stuck_task", "k2", "Comprare il latte", 13)
    assert surprise.pick([stuck, back], set(), "it-IT").kind == "welcome_back"  # type: ignore[union-attr]
    assert surprise.pick([stuck, back], {"welcome_back"}, "it-IT").kind == "stuck_task"  # type: ignore[union-attr]
    assert surprise.pick([stuck], {"stuck_task"}, "it-IT") is None
    assert surprise.pick([young], set(), "it-IT") is None
    assert surprise.pick([], set(), "it-IT") is None
    assert ritual_rules.answered("thought", True, "it-IT", "Sistemare la cantina") == (
        "Bene: qual è il primo passo per «Sistemare la cantina»? Lo segno."
    )  # review M1: the yes names the task
    assert ritual_rules.answered("thought", False, "it-IT") == "Va bene, lo lascio com'è."


# --- row 1: Bentornato ---------------------------------------------------------------------------------------


def test_the_morning_after_a_confirmed_trip_pisa_asks_how_it_went(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _trip(db, "Chicago", local("2026-11-10T23:30"), doubt="unverified_sender")  # not theirs, unconfirmed
    _trip(db, "Denver", local("2026-11-10T23:00"), status="cancelled")
    _trip(db, "Seattle", local("2026-11-12T21:00"))  # not back yet: tomorrow
    trip = _trip(db, "New York", local("2026-11-10T22:00"))
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))  # a stuck task too: the trip goes first
    _event(db, "Dentista", local("2026-11-11T11:00"))
    text, ask = _open(client, db, clock, "2026-11-11T08:30")
    assert text.endswith("Bentornato da New York. Com'è andata?")
    for other in ("Chicago", "Denver", "Seattle", "pensavo"):
        assert other not in text
    assert ask is None  # an open question: their answer is a talk, nothing is kept about it
    assert db.execute("SELECT kind, ref FROM morning_thoughts").fetchall() == [("welcome_back", trip)]
    assert db.execute("SELECT count(*) FROM ritual_asks").fetchone()[0] == 0


def test_never_twice_and_never_after_18_hours(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _repeat(db)
    _trip(db, "New York", local("2026-11-11T06:00"))
    _event(db, "Dentista", local("2026-11-11T11:00"))
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    assert "Bentornato da New York" in _open(client, db, clock, "2026-11-11T08:30")[0]
    db.execute("UPDATE morning_thoughts SET day = '2026-11-10'")  # as if said on another day: the same trip …
    text, _ = _open(client, db, clock, "2026-11-11T09:00")
    assert "Bentornato" not in text  # … is never said again, even within its 18 hours
    assert "«Sistemare la cantina»" in text  # the next thought takes its place
    _trip(db, "Boston", local("2026-11-12T08:00"))  # back 25 h before the next morning
    text, _ = _open(client, db, clock, "2026-11-13T09:00")
    assert "Boston" not in text


# --- row 2: Stanotte pensavo a ------------------------------------------------------------------------------


def test_the_oldest_sleeper_in_his_pool_as_one_yes_question(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "Dentista", local("2026-11-11T11:00"))
    old = local("2026-10-20T10:00")
    _task(
        db, "Pagare la multa", old - timedelta(days=5), due_at=local("2026-11-01T10:00"), due_kind="date"
    )  # overdue: aging's
    _task(db, "Annaffiare le piante", old - timedelta(days=4), repeat=Jsonb({"every": "week"}))  # a series
    _task(
        db,
        "Ritirare il pacco",
        old - timedelta(days=3),
        expected_from=local("2026-11-19T10:00"),
        expected_until=local("2026-11-20T10:00"),
    )  # a delivery
    _task(
        db, "Chiamare l'idraulico", old - timedelta(days=2), aging_asked_at=local("2026-11-01T10:00")
    )  # asked
    _task(
        db,
        "Rispondere a Bea",
        old - timedelta(days=7),
        status="deleted",
        deleted_at=local("2026-11-02T10:00"),
    )
    _task(db, "Comprare il latte", local("2026-11-05T10:00"))  # too young (6 days)
    stuck = _task(db, "Sistemare la cantina", old)
    _task(db, "Riordinare l'armadio", old + timedelta(days=1))  # a younger sleeper: not today
    text, ask = _open(client, db, clock, "2026-11-11T08:30")
    assert text.endswith("Stanotte pensavo a «Sistemare la cantina»: è in lista da 22 giorni. Ti va di trovare "
                         "un primo passo da dieci minuti?")  # fmt: skip
    for other in ("multa", "piante", "pacco", "idraulico", "Bea", "latte", "armadio"):
        assert other not in text
    assert ask is not None
    kind, question, expires = db.execute("SELECT kind, question, expires_at FROM ritual_asks WHERE id = %s",
                                         (ask,)).fetchone()  # fmt: skip
    assert kind == "thought" and question.startswith("Ti va di trovare")
    assert expires == local("2026-11-12T00:00")  # until their day's end
    assert db.execute("SELECT kind, ref, ask_id FROM morning_thoughts").fetchall() == [
        ("stuck_task", stuck, ask)
    ]
    _repeat(db)  # one a day: the next briefing today has none (the armadio waits for tomorrow)
    text2, ask2 = _open(client, db, clock, "2026-11-11T09:00")
    assert "pensavo a" not in text2 and ask2 is None


def test_his_yes_asks_for_the_step_and_changes_nothing_his_no_silences_the_kind(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    runtime = client.app.state.runtime
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    _task(db, "Riparare la bici", local("2026-10-02T10:00"))
    _task(db, "Svuotare il garage", local("2026-10-03T10:00"))
    _task(db, "Pulire la soffitta", local("2026-10-04T10:00"))
    before = db.execute("SELECT id, title, status, updated_at FROM tasks ORDER BY id").fetchall()
    _, ask = _open(client, db, clock, "2026-11-11T08:30")  # the one thing names the cantina: the bici
    said = client.portal.call(runtime.rituals.answer, ask, True, "voice")
    assert said == "Bene: qual è il primo passo per «Riparare la bici»? Lo segno."
    assert db.execute("SELECT id, title, status, updated_at FROM tasks ORDER BY id").fetchall() == before
    assert db.execute("SELECT answer FROM morning_thoughts").fetchone()[0] == "yes"
    _, ask = _open(client, db, clock, "2026-11-12T08:30")  # the next one, the next day
    assert ask is not None
    said = client.portal.call(runtime.rituals.answer, ask, False, "voice")
    assert said == "Va bene, lo lascio com'è."
    text, ask = _open(client, db, clock, "2026-11-13T08:30")
    assert "pensavo a" not in text and ask is None  # their no: that kind is silent for 30 days
    text, ask = _open(client, db, clock, "2026-12-11T08:30")
    assert "pensavo a" not in text and ask is None  # 29 days on: still silent
    text, ask = _open(client, db, clock, "2026-12-13T08:30")
    assert "Pulire la soffitta" in text and ask is not None  # 31 days on, the kind is back


# --- the rails ---------------------------------------------------------------------------------------------


def test_rails_nothing_to_say_says_nothing_and_a_failure_never_fails_the_briefing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    runtime = client.app.state.runtime
    text, ask = _open(client, db, clock, "2026-11-11T08:30")
    assert "Bentornato" not in text and "pensavo" not in text and ask is None
    assert db.execute("SELECT count(*) FROM morning_thoughts").fetchone()[0] == 0

    async def broken(conn: Any, lang: str, said: str) -> Any:
        await conn.execute("SELECT * FROM no_such_table")

    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    runtime.briefings.surprise.offer = broken
    db.execute("DELETE FROM briefings")
    text, ask = _open(client, db, clock, "2026-11-12T08:30")
    assert text and ask is None  # the briefing went, without a thought


def test_rails_never_in_a_time_briefing_quiet_hours_hold(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, quiet={"hours": {"start": "22:00", "end": "09:00"}},
              briefing={"triggers": {"time": True}, "time": "08:00"})  # fmt: skip
    runtime = client.app.state.runtime
    pair(db, "iPhone")
    _event(db, "Dentista", local("2026-11-11T11:00"))
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    clock.now = local("2026-11-11T08:05").astimezone(UTC)
    got = client.portal.call(runtime.briefings.on_time)
    assert got is not None and "pensavo" not in got.text
    assert db.execute("SELECT count(*) FROM morning_thoughts").fetchone()[0] == 0


def test_rails_a_task_the_briefing_already_names_is_never_said_twice(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))  # the only one: the morning's one thing too
    text, ask = _open(client, db, clock, "2026-11-11T08:30")
    assert text == "Una cosa sola oggi: sistemare la cantina." and ask is None
    assert db.execute("SELECT count(*) FROM morning_thoughts").fetchone()[0] == 0


# --- the review of ee24ad6 -----------------------------------------------------------------------


def test_a_task_made_from_an_email_is_never_said_as_his(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "Dentista", local("2026-11-11T11:00"))
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, source_ref, created_at, updated_at) VALUES "
        "(gen_random_uuid(), 'Conferma il tuo account su paypa1.com', 'conferma', 'open', 'email', 'm1', %s, %s)",
        (local("2026-09-01T10:00"), local("2026-09-01T10:00")),
    )
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    text, _ = _open(client, db, clock, "2026-11-11T08:30")
    assert "«Sistemare la cantina»" in text and "paypa1" not in text


def test_bentornato_only_after_a_landing_the_phone_saw(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import zone

    _settings(db, briefing={"window": {"start": "06:00", "end": "18:00"}})
    _repeat(db)
    _event(db, "Dentista", local("2026-11-11T19:00"))
    back = local("2026-11-11T06:00")
    _trip(db, "Chicago", back, landed=False)  # booked, never seen landing (a delay, a trip they didn't take)
    _trip(db, "Denver", back, cancel_mentions=["m9"])  # an email speaks of a cancellation
    _trip(db, "Seattle", back, proposals=Jsonb([{"leg": "x", "at": "2026-11-12T06:00+01:00"}]))  # a moved leg
    text, _ = _open(client, db, clock, "2026-11-11T08:30")
    for city in ("Chicago", "Denver", "Seattle"):
        assert city not in text
    assert "Bentornato" not in text
    db.execute("DELETE FROM morning_thoughts")
    _trip(db, "Boston", back)
    client.app.state.runtime.owner.place = zone.PhoneContext(
        "America/New_York"
    )  # the phone still says New York's zone (9:00 there)
    db.execute("DELETE FROM briefings")
    text, _ = _open(client, db, clock, "2026-11-11T15:00")
    assert "Bentornato" not in text
    client.app.state.runtime.owner.place = None
    db.execute("DELETE FROM briefings")
    text, _ = _open(client, db, clock, "2026-11-11T15:10")
    assert text.endswith("Bentornato da Boston. Com'è andata?")


def test_a_thought_the_outbox_would_cut_is_never_offered(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    runtime = client.app.state.runtime
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    clock.now = local("2026-11-11T08:30").astimezone(UTC)

    async def go(said: str) -> Any:
        async with runtime.pool.connection() as conn:
            from pisa.domain import settings as setting_rules
            from pisa.services.settings import load

            got = await runtime.briefings._thought(
                conn, "app_open", await load(conn, language="it-IT"), clock.now, said
            )
            del setting_rules
            return got

    assert client.portal.call(go, "x" * 950) is None
    assert db.execute("SELECT count(*) FROM morning_thoughts").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ritual_asks").fetchone()[0] == 0
    assert client.portal.call(go, "Una cosa sola oggi: dentista alle 11.") is not None


def test_none_after_an_automatic_briefing_reached_him_today(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _repeat(db)
    _event(db, "Dentista", local("2026-11-11T11:00"))
    text, _ = _open(client, db, clock, "2026-11-11T08:30")  # nothing to think about yet
    assert "pensavo" not in text
    db.execute(
        "UPDATE notifications SET delivered_via = 'ws', delivered_at = now()"
    )  # it reached their phone
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    text, _ = _open(client, db, clock, "2026-11-11T09:30")
    assert "pensavo" not in text  # the day's first briefing has passed: tomorrow
    _event(db, "Banca", local("2026-11-12T11:00"))
    text, _ = _open(client, db, clock, "2026-11-12T08:30")
    assert "«Sistemare la cantina»" in text
    assert db.execute("SELECT day::text FROM morning_thoughts").fetchone()[0] == "2026-11-12"


def test_a_sensitive_title_is_never_said(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "Dentista", local("2026-11-11T11:00"))
    _task(db, "Prenotare la visita per la depressione", local("2026-09-01T10:00"))
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    text, _ = _open(client, db, clock, "2026-11-11T08:30")
    assert "«Sistemare la cantina»" in text and "depress" not in text


def test_no_stanotte_in_the_afternoon(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, briefing={"window": {"start": "06:00", "end": "18:00"}})
    _event(db, "Dentista", local("2026-11-11T17:00"))
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    text, _ = _open(client, db, clock, "2026-11-11T15:00")
    assert "Pensavo a «Sistemare la cantina»" in text and "Stanotte" not in text


def test_a_quiet_landing_home_welcomes_him_home(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    trip = _trip(db, "New York", local("2026-11-11T03:00"))
    db.execute("UPDATE trips SET welcome_due = landed_keys[1] WHERE id = %s", (trip,))
    ((_, line),) = client.portal.call(runtime.trips.welcome_lines, "it-IT")
    assert line.startswith("Benvenuto a Milano.") and "New York" not in line


def test_the_thought_s_day_is_the_briefing_s(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, briefing={"window": {"start": "20:00", "end": "03:00"}})  # a night owl's window
    _event(db, "Dentista", local("2026-11-12T11:00"))
    _task(db, "Sistemare la cantina", local("2026-10-01T10:00"))
    text, _ = _open(client, db, clock, "2026-11-12T01:00")  # after midnight: still the 11th's briefing
    assert "«Sistemare la cantina»" in text
    assert db.execute("SELECT day::text FROM morning_thoughts").fetchone()[0] == "2026-11-11"
