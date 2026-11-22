"""The user's home. Once, Pisa could learn home as a trip's hotel, because every night it had seen since the
phone was paired was there. Now home is the user's first; a learned
one needs 21 nights and stays a guess until confirmed; while home is unknown nothing is scoped home or travel
and no trip starts."""

from datetime import date, timedelta
from typing import Any

import psycopg
import pytest

from tests.conftest import pair
from tests.test_learning import AMSTERDAM, HOME, at, learning, place, stay
from tests.test_profile import said

ROME = (41.886, 12.557)  # Testaccio
HOTEL = (52.415, 4.932)  # the trip's hotel, an invented point
PAIRED = date(2026, 10, 19)  # the phone is paired on the Monday of the trip


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


def nights(db: psycopg.Connection, device: str, first: date, count: int, coord: tuple[float, float]) -> None:
    for d in range(count):
        day = first + timedelta(days=d)
        stay(db, device, at(day, 22), at(day + timedelta(days=1), 8), coord)


def a_visit(db: psycopg.Connection, device: str, day: date, pid: str, coord: tuple[float, float]) -> None:
    for back in range(3):  # went back three times: S1 evidence
        when = day - timedelta(days=back)
        stay(db, device, at(when, 13), at(when, 14), coord, pid)


def setting(db: psycopg.Connection, key: str) -> Any:
    row = db.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def test_a_phone_paired_during_a_trip_learns_no_home(db, client, auth) -> None:  # type: ignore[no-untyped-def]
    """A trip: six nights at the Golfslag hotel are all Pisa has seen. No home, no trip, nothing scoped."""
    device = pair(db)[0]
    nights(db, device, PAIRED, 6, HOTEL)
    cafe = place(db, "Café de Wolk", AMSTERDAM, "Nepdam 1, 1012 LG Amsterdam, Netherlands")
    a_visit(db, device, date(2026, 10, 24), cafe, AMSTERDAM)
    night = at(date(2026, 10, 25), 3, 30)
    learning(db, night, lambda s: s.nightly())
    assert setting(db, "profile_home") is None and setting(db, "profile_travelling") is None
    scopes = [s for (s,) in db.execute("SELECT scope FROM profile_evidence").fetchall()]
    assert scopes and not any("where" in s for s in scopes)  # neither home nor travel
    assert all("where" not in s for (s,) in db.execute("SELECT scope FROM profile_claims").fetchall())
    profile = client.get("/v1/profile", headers=auth).json()
    assert profile["home"] == {"place": None, "source": None, "confirmed": False}
    assert profile["travelling"] is None


def test_the_owner_sets_home_then_a_night_far_away_is_a_trip(db, client, auth) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    nights(db, device, PAIRED, 6, HOTEL)
    casa = place(db, "Casa", ROME, "Via del Marmo Finto 1, 00153 Roma, Italy")
    put = client.put("/v1/profile/home", headers=auth, json={"place_id": casa})
    assert put.status_code == 200, put.text
    assert put.json() == {
        "place": {"name": "Casa", "lat": ROME[0], "lon": ROME[1]},
        "source": "owner",
        "confirmed": True,
    }
    assert client.get("/v1/profile", headers=auth).json()["home"] == put.json()
    # Last night at the hotel, 1,300 km from the user's home: a trip.
    trip = learning(db, at(date(2026, 10, 25), 3, 30), lambda s: s.update_travelling())
    assert trip is not None and trip["since"] == "2026-10-19"
    # Nights don't move the user's home, however many.
    nights(db, device, PAIRED + timedelta(days=6), 30, HOTEL)
    learning(db, at(date(2026, 11, 24), 3, 30), lambda s: s.nightly())
    assert setting(db, "profile_home")["source"] == "owner" and setting(db, "profile_home")["lat"] == ROME[0]
    bad = client.put("/v1/profile/home", headers=auth, json={"place_id": casa, "query": "Roma"})
    assert bad.status_code == 400  # one of the two


def test_a_learned_home_after_21_nights_is_a_guess_until_confirmed(db, client, auth) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    first = date(2026, 10, 1)
    place(db, "Bar Sotto Casa", (HOME[0] + 0.0005, HOME[1]), "Via Finta Seconda 1, 20127 Milano, Italy")
    nights(db, device, first, 20, HOME)
    learning(db, at(first + timedelta(days=20), 3, 30), lambda s: s.nightly())
    assert client.get("/v1/profile", headers=auth).json()["home"]["place"] is None  # 20 nights: not yet
    nights(db, device, first + timedelta(days=20), 1, HOME)
    learning(db, at(first + timedelta(days=21), 3, 30), lambda s: s.nightly())
    home = client.get("/v1/profile", headers=auth).json()["home"]
    assert home["source"] == "learned" and home["confirmed"] is False  # "I think you live in Milano"
    assert home["place"]["name"] == "Milano"
    assert abs(home["place"]["lat"] - HOME[0]) < 0.001
    confirmed = client.post("/v1/profile/home/confirm", headers=auth)
    assert confirmed.status_code == 200 and confirmed.json()["confirmed"] is True
    assert confirmed.json()["source"] == "learned"
    # Confirmed: a month away doesn't make the other city home.
    nights(db, device, first + timedelta(days=21), 31, AMSTERDAM)
    learning(db, at(first + timedelta(days=52), 3, 30), lambda s: s.nightly())
    assert client.get("/v1/profile", headers=auth).json()["home"] == confirmed.json()


def test_a_learned_home_needs_most_nights_in_one_place(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    first = date(2026, 10, 1)
    for d in range(24):  # a different city every few nights: no home
        coord = (HOME, ROME, AMSTERDAM)[d // 4 % 3]
        nights(db, device, first + timedelta(days=d), 1, coord)
    learning(db, at(first + timedelta(days=24), 3, 30), lambda s: s.nightly())
    assert setting(db, "profile_home") is None


def test_the_owner_forgets_home(db, client, auth) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    first = date(2026, 10, 1)
    nights(db, device, first, 22, HOME)
    nights(db, device, first + timedelta(days=22), 2, AMSTERDAM)
    night = at(first + timedelta(days=24), 3, 30)
    learning(db, night, lambda s: s.nightly())
    assert setting(db, "profile_travelling") is not None  # learned home, then two nights away
    assert client.delete("/v1/profile/home", headers=auth).status_code == 204
    profile = client.get("/v1/profile", headers=auth).json()
    assert profile["home"] == {"place": None, "source": None, "confirmed": False}
    assert profile["travelling"] is None
    # The nights before don't teach the same home again the next night.
    learning(db, night + timedelta(days=1), lambda s: s.nightly())
    assert client.get("/v1/profile", headers=auth).json()["home"]["place"] is None
    assert (
        learning(db, night + timedelta(days=1), lambda s: s.update_travelling()) is None
    )  # no home: no trip
    assert client.post("/v1/profile/home/confirm", headers=auth).status_code == 404


def test_by_voice_abito_a(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = said(
        db,
        secrets_key,
        "abito in via Inventata 40, Milano",
        [("set_home", {"place": "via Inventata 40, Milano"})],
    )
    assert t.result("set_home").startswith("Home set: Via Inventata 40")
    home = setting(db, "profile_home")
    assert home["source"] == "owner" and home["confirmed"] is True
    # Words that aren't the user's, or don't say where they live, set nothing.
    other = said(db, secrets_key, "che tempo fa a Milano?", [("set_home", {"place": "Milano"})])
    assert other.result("set_home").startswith("Refused")
