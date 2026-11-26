"""OpenStreetMap's `opening_hours` (v0.1.5): the simple rules read into a week of periods, the rest kept as
the raw text; Nominatim asked for the tag (`extratags=1`); "open now" with OpenStreetMap the only map keeps
the places their listed hours say are open, drops the closed ones and keeps the unknown ones marked, and the
model is told how many of each; a filter a map can't apply is said, never an error. Synthetic data only."""

from datetime import UTC, datetime
from typing import Any

import pytest

from pisa.connectors.osm import OsmConfig
from pisa.domain.places import Period, open_state, osm_hours
from pisa.ports.places import PlaceResults
from tests.test_osm_places import NOMINATIM, OVERPASS, PHOTON, json_answer, run, setup
from tests.test_phone_maps import _map_search, maps_env  # pyright: ignore[reportPrivateUsage]

ROME = "Europe/Rome"
TUESDAY_11 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)  # Tuesday 11:00 in Rome (the maps env's clock)


# --- the parser ---------------------------------------------------------------------------------------------


def test_simple_rules_become_a_week_of_periods() -> None:
    assert osm_hours("Mo-Fr 09:00-18:00; Sa 09:00-12:30") == [
        *(Period(day, "09:00", "18:00") for day in range(1, 6)),
        Period(6, "09:00", "12:30"),
    ]
    assert osm_hours("Mo,We 08:00-12:00,14:00-18:00") == [
        Period(1, "08:00", "12:00"), Period(1, "14:00", "18:00"),
        Period(3, "08:00", "12:00"), Period(3, "14:00", "18:00"),
    ]  # fmt: skip
    assert osm_hours("10:00-20:00") == [Period(day, "10:00", "20:00") for day in range(1, 8)]  # no days: all
    assert osm_hours("Fr-Mo 10:00-12:00") == [Period(d, "10:00", "12:00") for d in (1, 5, 6, 7)]  # wraps
    assert osm_hours("Mo - Fr 9:00 - 18:00;") == [Period(d, "09:00", "18:00") for d in range(1, 6)]


def test_off_closed_and_a_later_rule_replace_the_days() -> None:
    assert osm_hours("Mo-Su 09:00-18:00; We off; Su closed") == [
        Period(d, "09:00", "18:00") for d in (1, 2, 4, 5, 6)
    ]
    assert osm_hours("Mo-Fr 09:00-18:00; Fr 09:00-13:00") == [
        *(Period(d, "09:00", "18:00") for d in range(1, 5)),
        Period(5, "09:00", "13:00"),
    ]
    assert osm_hours("off") is None  # closed every day: no week of hours to go by


def test_24_7_and_ranges_crossing_midnight() -> None:
    assert osm_hours("24/7") == [Period(1, "00:00", None)]  # around the clock, as Google says it
    assert open_state(osm_hours("24/7") or [], TUESDAY_11, ROME) == (True, None)
    assert osm_hours("24/7; Su off") == [Period(d, "00:00", "24:00") for d in range(1, 7)]
    late = osm_hours("Mo 20:00-02:00") or []
    assert late == [Period(1, "20:00", "02:00")]
    monday_night = datetime(2026, 11, 24, 0, 30, tzinfo=UTC)  # Tuesday 01:30 in Rome: Monday's night
    assert open_state(late, monday_night, ROME)[0] is True
    assert open_state(late, TUESDAY_11, ROME)[0] is False
    assert osm_hours("Sa 18:00-24:00") == [Period(6, "18:00", "24:00")]


@pytest.mark.parametrize(
    "raw",
    [
        "Mo-Fr 09:00-18:00; PH off",
        "Mo-Fr 08:00-12:00; SH off",
        "sunrise-sunset",
        "Mo-Fr 09:00-18:00 || Sa 10:00-12:00",
        'Mo-Fr 09:00-18:00 "by appointment"',
        "week 1-26 Mo-Fr 09:00-18:00",
        "Jan-Mar Mo-Fr 09:00-17:00",
        "Mo-Fr 18:00-26:00",
        "Mo-Fr 09:00+",
        "Mo-Fr 09:00-12:00, Sa 10:00-12:00",
        "Mo-Fr",
        "open",
        "",
    ],
)
def test_anything_else_is_not_guessed(raw: str) -> None:
    assert osm_hours(raw) is None


# --- Nominatim asks for the tag and reads it ----------------------------------------------------------------

NOMINATIM_CONFIG = OsmConfig("nominatim", PHOTON, NOMINATIM, OVERPASS, "https://example.org/pisa")


def _item(osm_id: int, name: str, hours: str | None) -> dict[str, Any]:
    return {
        "osm_type": "node", "osm_id": osm_id, "lat": "45.011", "lon": "9.011", "name": name,
        "type": "pharmacy",
        "address": {"road": "Via Prova", "house_number": str(osm_id), "city": "Testville"},
        "extratags": {"opening_hours": hours} if hours is not None else None,
    }  # fmt: skip


def test_nominatim_sends_extratags_and_reads_opening_hours() -> None:
    answer = [
        _item(1, "Farmacia Semplice", "Mo-Sa 08:30-19:30; Su off"),
        _item(2, "Farmacia Festiva", "Mo-Fr 09:00-13:00; PH off"),
        _item(3, "Farmacia Senza Orari", None),
        _item(4, "Farmacia Sporca", "Mo-Fr​ 09:00-18:00" + "x" * 400),
    ]
    adapter, fake, _, _ = setup({"/search": json_answer(answer)}, NOMINATIM_CONFIG)
    found = run(adapter.search("farmacia", near=None, open_now=True, limit=5, lang="it"))
    params = dict(fake.requests[0].url.params)
    assert params["extratags"] == "1"
    assert set(params) == {"q", "format", "limit", "addressdetails", "extratags", "accept-language"}
    simple, festive, none, dirty = found
    assert (
        simple.weekly == [*(Period(d, "08:30", "19:30") for d in range(1, 7))] and simple.hours_note is None
    )
    assert festive.weekly == [] and festive.hours_note == "Mo-Fr 09:00-13:00; PH off"
    assert none.weekly == [] and none.hours_note is None and none.open_now is None
    assert dirty.weekly == [] and dirty.hours_note is not None and len(dirty.hours_note) == 255
    assert "​" not in dirty.hours_note  # cleaned as every provider string


def test_photon_is_asked_as_before() -> None:
    """Photon has no request parameter for OSM's other tags (an instance returns them only when imported
    with them): its request is unchanged."""
    adapter, fake, _, _ = setup({"/api": json_answer({"type": "FeatureCollection", "features": []})})
    run(adapter.search("farmacia", near=None, open_now=True, limit=5, lang="en"))
    assert set(fake.requests[0].url.params) == {"q", "limit", "lang"}


# --- open now with OpenStreetMap the only map ---------------------------------------------------------------

ANSWER = [
    _item(11, "Farmacia Aperta", "Mo-Sa 08:30-19:30; Su off"),  # open at Tuesday 11:00
    _item(12, "Farmacia Notturna", "Mo-Su 20:00-08:00"),  # crosses midnight: closed at 11:00
    _item(13, "Farmacia Festiva", "Mo-Fr 09:00-13:00; PH off"),  # not read: the raw text
    _item(14, "Farmacia Senza Orari", None),
]


def test_open_now_keeps_the_open_drops_the_closed_and_marks_the_unknown(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with maps_env(db, secrets_key, nominatim=ANSWER) as (_, places, seen):
            found = await places.search("farmacia", near=None, open_now=True, rich=True)
            assert isinstance(found, PlaceResults)
            assert [
                (f["name"], f["open_now"], f.get("hours_unknown"), f.get("hours_note")) for f in found
            ] == [
                ("Farmacia Aperta", True, None, None),
                ("Farmacia Festiva", None, True, "Mo-Fr 09:00-13:00; PH off"),
                ("Farmacia Senza Orari", None, True, None),
            ]
            assert found.closed_left_out == 1 and found.not_applied == ("open_now", "rich")
            assert set(seen) == {"nominatim.w2a.example.test"}
            every = await places.search("farmacia", near=None, open_now=False)
            assert [(f["name"], f["open_now"]) for f in every] == [
                ("Farmacia Aperta", True), ("Farmacia Notturna", False),
                ("Farmacia Festiva", None), ("Farmacia Senza Orari", None),
            ]  # fmt: skip
            assert every.closed_left_out == 0 and every.not_applied == ()
            assert all("hours_unknown" not in f for f in every)

    run(go())


def test_the_model_reads_the_counts_and_the_hours_as_listed(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> str:
        async with maps_env(db, secrets_key, nominatim=ANSWER) as (_, places, _):
            map_search = _map_search(places, places._pool)  # pyright: ignore[reportPrivateUsage]
            return await map_search(query="farmacia", open_now=True)

    out = run(go())
    assert "Farmacia Notturna" not in out
    assert "Farmacia Aperta, pharmacy, Via Prova 11, Testville, open now (id " in out
    assert "opening hours unknown (listed as: Mo-Fr 09:00-13:00; PH off)" in out
    assert "Farmacia Senza Orari, pharmacy, Via Prova 14, Testville, opening hours unknown (id " in out
    assert (
        "; asked open now, which this map can't filter by: by each place's listed hours 1 open now, 2 with "
        "hours unknown (kept), 1 closed now (left out)"
    ) in out
    assert (
        "; its opening hours are unknown: never say one is open now, say the hours aren't known, and give "
        "the hours as listed where a place has them"
    ) in out
    # nothing asked of a rich map ("farmacia"): its absence is not said (A1: token noise, unprompted lines)
    assert "no ratings, prices or attributes" not in out


def test_every_place_closed_is_said_not_no_match(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> str:
        async with maps_env(db, secrets_key, nominatim=[ANSWER[1]]) as (_, places, _):
            map_search = _map_search(places, places._pool)  # pyright: ignore[reportPrivateUsage]
            return await map_search(query="farmacia", open_now=True)

    out = run(go())
    assert (
        "The map found 1 place(s) for 'farmacia', all closed now by their listed opening hours: none is open "
        "now."
    ) in out
    assert "No place on the map matches" not in out


def test_hours_listed_but_not_read_without_open_now(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> str:
        async with maps_env(db, secrets_key, nominatim=[ANSWER[2]]) as (_, places, _):
            map_search = _map_search(places, places._pool)  # pyright: ignore[reportPrivateUsage]
            return await map_search(query="farmacia")

    out = run(go())
    assert "opening hours listed as: Mo-Fr 09:00-13:00; PH off" in out
    assert "; hours given as listed weren't read: never say from them whether one is open now" in out


def test_a_radius_with_no_point_to_measure_from_is_said(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go() -> tuple[PlaceResults, str]:
        async with maps_env(db, secrets_key, nominatim=ANSWER[:1]) as (_, places, _):
            found = await places.search("farmacia", near=None, radius_m=500)
            map_search = _map_search(places, places._pool)  # pyright: ignore[reportPrivateUsage]
            return found, await map_search(query="farmacia", radius_m=500)

    found, out = run(go())
    assert [f["name"] for f in found] == ["Farmacia Aperta"] and found.not_applied == ("radius_m",)
    assert "; the distance limit wasn't applied: with no position to measure from, these may be far" in out


def test_no_ratings_is_said_only_when_the_search_asked_for_one() -> None:
    from pisa.brain.research_tools import asks_rich

    for asked in (
        "pizzeria economica",
        "ristorante con terrazza",
        "cena all'aperto",
        "best coffee",
        "miglior sushi",
    ):
        assert asks_rich(asked), asked
    for plain in ("farmacia", "ristorante aperto ora", "specialty coffee", "pharmacy open now"):
        assert not asks_rich(plain), plain
