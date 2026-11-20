"""Fixes at the merge: each fails without its fix."""

from datetime import date, timedelta

import pytest

from pisa.domain import learning as rules
from tests.conftest import pair
from tests.test_learning import (
    AMSTERDAM,
    HOME,
    NIGHT1,
    NIGHT2,
    at,
    learning,
    owner,  # noqa: F401 (the fixture: three weeks of a life)
    stay,
)

# --- N1: health, worship and politics with a generic category ----------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Centro Islamico di Milano",
        "Sala del Regno dei Testimoni di Geova",
        "Kingdom Hall",
        "Gurdwara Sahib",
        "Movimento 5 Stelle",
        "Fratelli d'Italia Circolo",
        "Forza Italia",
        "PvdA Amsterdam",
        "GroenLinks",
        "Polikliniek Oost",
        "Fysio Centrum",
        "Fisio Center",
        "Consultorio familiare",
        "Laboratorio Analisi Cliniche",
        "Centro Diagnostico Italiano",
        "AVIS Milano",
        "GGZ inGeest",
    ],
)
def test_sensitive_names_with_a_generic_category_are_never_taste(name: str) -> None:
    assert rules.sensitive_place(name, "point_of_interest")
    assert not rules.taste_place(name, "point_of_interest")


@pytest.mark.parametrize("name", ["Trattoria da Mario", "Caffè Lento", "Italia Bar", "Forza Pizza"])
def test_ordinary_places_stay_taste(name: str) -> None:
    assert rules.taste_place(name, "restaurant")


# --- N2: the user's `where` holds through the nightly run --------------------------------------------------


def test_a_like_the_owner_moved_to_travel_doesnt_come_back_as_a_home_like(db, owner) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    subject = f"place:{owner['cafe']}"
    learning(db, NIGHT1, lambda s: s.nightly())
    row = db.execute("SELECT id::text FROM profile_claims WHERE subject_key = %s", (subject,)).fetchone()
    assert row is not None
    learning(
        db, NIGHT1, lambda s: s._profile.edit(row[0], statement=None, scope={"where": "travel"}, hard=None)
    )
    learning(db, NIGHT2, lambda s: s.nightly())
    rows = db.execute(
        "SELECT id::text, scope->>'where' FROM profile_claims WHERE subject_key = %s", (subject,)
    ).fetchall()
    assert rows == [(row[0], "travel")]


# --- N3: a trip that never ends is a move -------------------------------------------------------------------


def test_a_month_in_one_city_far_from_home_is_a_move_and_ends_the_trip(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    start = date(2026, 8, 1)
    for d in range(10):
        day = start + timedelta(days=d)
        stay(db, device, at(day, 20), at(day + timedelta(days=1), 7), HOME)
    for d in range(10, 50):  # 40 nights in Amsterdam: the user lives there now
        day = start + timedelta(days=d)
        stay(db, device, at(day, 21), at(day + timedelta(days=1), 8), AMSTERDAM)
    travelling = None
    for d in range(11, 51):
        travelling = learning(db, at(start + timedelta(days=d), 7), lambda s: s.update_travelling())
    assert travelling is None
