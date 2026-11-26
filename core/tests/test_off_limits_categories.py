"""The queued rows: the user's off-limits words in Italian and Dutch cover the map providers' English
categories. Each test fails without its fix."""

import uuid
from datetime import date, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from pisa.domain import profile as profile_rules
from tests.assistant_env import add_event
from tests.conftest import Clock, pair
from tests.test_learning import at, claims, learning, stay
from tests.test_learning_fixes import MONDAY, place
from tests.test_small_fixes_places import profile_call


def evidence(db: psycopg.Connection, subject: str, when: datetime, scope: dict[str, str], ref: str) -> None:
    db.execute(
        "INSERT INTO profile_evidence (id, signal, ref, weight, direction, scope, observed_at, summary, "
        "kind, subject_key) VALUES (%s, 'S3', %s, 1, 1, %s, %s, 'x', 'like', %s)",
        (uuid.uuid4(), ref, Jsonb(scope | {"where": "home"}), when, subject),
    )


# --- N4: an expired scoped like comes back only with newer evidence of its own scope ------------------------


def test_an_expired_weekday_like_doesnt_come_back_on_an_older_weekend_visit(db) -> None:  # type: ignore[no-untyped-def]
    pid = place(db, "Trattoria Milanese", "restaurant", (45.47, 9.20))
    subject = f"place:{pid}"
    weekday = {"day": "weekday", "time": "afternoon"}
    for week in range(4):  # four weekday lunches…
        evidence(db, subject, at(MONDAY + timedelta(days=7 * week), 13), weekday, f"wd{week}")
    last_weekday = at(MONDAY + timedelta(days=21), 13)
    evidence(db, subject, at(MONDAY + timedelta(days=26), 20), {"day": "weekend", "time": "evening"}, "we")
    db.execute(  # … the weekday like, expired, its last evidence the last weekday lunch
        "INSERT INTO profile_claims (id, kind, statement, subject, polarity, scope, provenance, support, "
        "against, confidence, state, sensitive, hard, created_at, updated_at, subject_key, last_evidence_at, "
        "retired_by) VALUES (%s, 'like', 'Gli piace «Trattoria Milanese»', '{}', 1, %s, 'observed', 4, 0, "
        "0.67, 'retired', false, false, now(), now(), %s, %s, 'expired')",
        (uuid.uuid4(), Jsonb(weekday | {"where": "home"}), subject, last_weekday),
    )
    learning(db, at(date(2026, 10, 26), 3, 30), lambda s: s.consolidate())
    # The Saturday dinner was always there, after the last weekday lunch: not news about the weekday like.
    assert claims(db)[subject]["state"] == "retired"


# --- N5: no lateness at a sensitive place -------------------------------------------------------------------


def test_lateness_is_never_measured_at_a_hospital(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    hospital = place(db, "Ospedale Niguarda", "hospital", (45.51, 9.19))
    trattoria = place(db, "Trattoria Milanese", "restaurant", (45.47, 9.20))
    for i, (pid, coord) in enumerate([(hospital, (45.51, 9.19)), (trattoria, (45.47, 9.20))] * 3):
        day = MONDAY + timedelta(days=i)
        event = add_event(db, "Visita" if pid == hospital else "Cena", at(day, 10), minutes=60)
        db.execute("UPDATE calendar_events SET place_id = %s, accepted = true WHERE id = %s", (pid, event))
        stay(db, device, at(day, 10, 12), at(day, 11), coord, pid)
    learning(db, at(date(2026, 10, 25), 3, 30), lambda s: s.nightly())
    rows = db.execute("SELECT summary FROM profile_evidence WHERE signal = 'S2'").fetchall()
    assert len(rows) == 3  # the dinners only: an accepted "Visita" at a hospital is never evidence


# --- N6: a low reasoning effort only for models that reason by default --------------------------------------


# --- off-limits words in Italian and Dutch cover English categories -----------------------------------------


def test_palestra_and_sportschool_cover_a_place_filed_as_a_gym() -> None:
    gym = "Gli piace «FitLab» gym"
    assert profile_rules.touches("palestra", "like", gym)  # Italian
    assert profile_rules.touches("sportschool", "like", gym)  # Dutch
    assert profile_rules.touches("gym", "like", gym)  # English, as before
    assert profile_rules.touches("discoteca", "like", "Gli piace «Noord» night club")
    assert profile_rules.touches("kerk", "like", "Di solito va a «Westerkerk» church")
    assert profile_rules.touches("chiesa", "like", "Gli piace «San Marco» place of worship")
    assert not profile_rules.touches("palestra", "like", "Gli piace «Caffè Lento» cafe")  # nothing else


def test_an_italian_off_limits_word_forgets_a_gym_and_it_isnt_learned_again(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    gym = place(db, "FitLab", "gym", (45.46, 9.18))
    for week in range(4):
        day = MONDAY + timedelta(days=7 * week)
        stay(db, device, at(day, 18), at(day, 19, 30), (45.46, 9.18), gym)
    learning(db, at(date(2026, 10, 25), 3, 30), lambda s: s.nightly())
    assert [k for k in claims(db) if gym in k]
    profile_call(db, Clock(at(date(2026, 10, 25), 9)), lambda p: p.off_limits("palestra"))
    assert not [k for k in claims(db) if gym in k]
    stay(db, device, at(date(2026, 10, 26), 18), at(date(2026, 10, 26), 19, 30), (45.46, 9.18), gym)
    learning(db, at(date(2026, 10, 27), 3, 30), lambda s: s.nightly())
    assert not [k for k in claims(db) if gym in k]
