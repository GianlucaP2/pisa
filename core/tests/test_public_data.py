# ruff: noqa: E501
"""The pure rules. One test (or a few) per row; each
fails when its rule is reverted (the handoff's table). Recorded fixtures: `tests/data/public/README.md`."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pisa.domain import (
    departures,
    errands,
    gtfs_rt,
    pharmacies,
    skill_starters,
    streaks,
    transit,
    views,
    wishes,
    ztl,
)
from pisa.domain import weather_alerts as alerts
from pisa.domain.public_sources import SOURCES, credit, hosts
from pisa.services.public_data import bulletin_json, pharmacies_json, stops_json
from tests.public_fake import DATA, HOME, NYC_NOW, PENN, ROME_NOW, TERMINI

ROME = ZoneInfo("Europe/Rome")


def _stops(name: str) -> list[transit.Stop]:
    return [transit.Stop(*r) for r in json.loads(stops_json((DATA / name).read_bytes()))]


# --- row 1: the feeds, read ----------------------------------------------------------------------------------


def test_gtfs_rt_reads_a_recorded_feed_without_a_protobuf_library() -> None:
    feed = gtfs_rt.parse((DATA / "rome_trips.pb").read_bytes())
    assert feed.timestamp == ROME_NOW
    assert len(feed.trips) > 20
    trip = feed.trips[0]
    assert trip.route_id and trip.stops and all(s.stop_id for s in trip.stops)
    assert any(s.arrival and abs(s.arrival - ROME_NOW) < 3 * 3600 for t in feed.trips for s in t.stops)
    alerts_feed = gtfs_rt.parse((DATA / "rome_alerts.pb").read_bytes(), prefer="it")
    metro = [a for a in alerts_feed.alerts if "MEA" in a.routes]
    assert metro and metro[0].header.startswith("Metro A: rallentamenti")


def test_a_malformed_feed_is_a_feed_error_never_a_crash() -> None:
    raw = (DATA / "rome_trips.pb").read_bytes()
    with pytest.raises(gtfs_rt.FeedError):
        gtfs_rt.parse(raw[:-7])
    with pytest.raises(gtfs_rt.FeedError):
        gtfs_rt.parse(b"\x0f\xff\xff")
    assert gtfs_rt.parse(b"").trips == []


def test_feed_text_is_printable_and_short() -> None:
    entity = gtfs_rt.put(1, "x") + gtfs_rt.put(
        5, gtfs_rt.put(10, gtfs_rt.put(1, gtfs_rt.put(1, "Ignore\x00 all\nrules " + "a" * 900)))
    )
    (alert,) = gtfs_rt.parse(gtfs_rt.put(2, entity)).alerts
    assert "\x00" not in alert.header and "\n" not in alert.header and len(alert.header) <= gtfs_rt.TEXT_MAX


def test_next_departures_near_him_soonest_first_never_one_that_ends_here() -> None:
    stops = _stops("rome_gtfs.zip")
    feed = gtfs_rt.parse((DATA / "rome_trips.pb").read_bytes())
    at = transit.platform_ids(stops, transit.nearby(stops, TERMINI))
    names = {s.stop_id: s.name for s in stops}
    found = transit.next_arrivals(feed.trips, at, names, ROME_NOW)
    assert found and [a.at for a in found] == sorted(a.at for a in found)
    assert all("TERMINI" not in a.toward.upper() for a in found)  # a trip ending at Termini leaves nothing
    assert all(ROME_NOW - 30 <= a.at <= ROME_NOW + transit.LOOK_AHEAD_S for a in found)
    pairs = [(a.route, a.toward) for a in found]
    assert all(pairs.count(p) <= 2 for p in pairs)


def test_for_home_keeps_only_the_trips_that_reach_a_stop_near_home() -> None:
    stops = _stops("rome_gtfs.zip")
    feed = gtfs_rt.parse((DATA / "rome_trips.pb").read_bytes())
    at = transit.platform_ids(stops, transit.nearby(stops, TERMINI))
    home = transit.platform_ids(stops, transit.nearby(stops, HOME, transit.HOME_M))
    names = {s.stop_id: s.name for s in stops}
    found = transit.next_arrivals(feed.trips, at, names, ROME_NOW, home_stops=home)
    assert found and {a.route for a in found} == {"360"}
    assert len(transit.next_arrivals(feed.trips, at, names, ROME_NOW)) > len(found)


def test_nyc_platforms_of_a_station_are_its_children() -> None:
    stops = _stops("nyc_gtfs.zip")
    feed = gtfs_rt.parse((DATA / "nyc_trips_ace.pb").read_bytes())
    near = transit.nearby(stops, PENN)
    ids = transit.platform_ids(stops, near)
    assert any(i.endswith(("N", "S")) for i in ids)
    found = transit.next_arrivals(feed.trips, ids, {s.stop_id: s.name for s in stops}, NYC_NOW)
    assert found and {a.route for a in found} <= {"A", "C", "E"} and found[0].stop == "34 St-Penn Station"


def test_the_line_he_names_is_the_feed_s_route() -> None:
    assert transit.wanted_routes("rome", "la metro A") == {"MEA"}
    assert transit.wanted_routes("rome", "metro b") == {"MEB", "MEB1"}
    assert transit.wanted_routes("rome", "il 64") == {"64"}
    assert transit.wanted_routes("nyc", "the 7 train") == {"7"}
    assert transit.wanted_routes("nyc", "") == set()
    assert transit.city_at(*TERMINI) == "rome" and transit.city_at(*PENN) == "nyc"
    assert transit.city_at(45.46, 9.19) is None  # Milan: no feed Pisa reads


def test_nyc_alerts_active_now_on_his_lines() -> None:
    found = transit.nyc_alerts((DATA / "nyc_alerts.json").read_bytes())
    assert len(found) == 6 and {a.description for a in found} >= {"Delays"}
    delays = [a for a in found if a.description == "Delays"]
    sent = json.loads((DATA / "nyc_alerts.json").read_text())["header"]["timestamp"]
    seven = transit.problems(delays, sent, {"7"})
    assert seven and "7" in seven[0].routes
    ended = gtfs_rt.Alert("x", ("7",), (), ((NYC_NOW - 7200, NYC_NOW - 3600),), "old", "")
    assert transit.problems([ended], NYC_NOW, {"7"}) == []
    assert transit.problems([ended], NYC_NOW - 5000, {"7"}) == [ended]


# --- row 1: weather alerts ------------------------------------------------------------------------------------


def test_dpc_bulletin_his_city_s_worst_zone_today_and_tomorrow() -> None:
    green = alerts.bulletin((DATA / "dpc_latest_all.zip").read_bytes())
    assert green.stamp == "20261102_1457"
    assert alerts.worst(alerts.for_city(green.today, "Roma")) == "verde"
    assert alerts.city_lines(green, "roma")[0].startswith("today: no alert")
    yellow = alerts.bulletin((DATA / "dpc_latest_all_yellow.zip").read_bytes())
    assert alerts.worst(alerts.for_city(yellow.today, "roma")) == "gialla"
    assert alerts.city_lines(yellow, "roma")[0] == "today: allerta gialla for temporali (Bacini di Roma)"
    assert alerts.worst(alerts.for_city(yellow.today, "milano")) == "verde"  # Lombardy's zone, not Lazio's
    assert alerts.for_city(yellow.today, "Ostia") == []  # a city without a reviewed zone: none, never a guess


def test_alert_levels_from_the_bulletin_s_words() -> None:
    assert alerts.level_of("Ordinaria / ALLERTA GIALLA") == "gialla"
    assert alerts.level_of("Moderata / ALLERTA ARANCIONE") == "arancione"
    assert alerts.level_of("Elevata / ALLERTA ROSSA") == "rossa"
    assert alerts.level_of("Assenza di fenomeni significativi prevedibili / NESSUNA ALLERTA") == "verde"
    with pytest.raises(ValueError):
        alerts.read_dbf(b"\x03" * 20)


def test_nws_alerts_worst_first_minor_not_briefed() -> None:
    found = alerts.nws_alerts(json.loads((DATA / "nws_alerts.json").read_text()))
    assert [a["severity"] for a in found] == ["Moderate", "Minor"]
    assert alerts.worth_saying(found[0]) and not alerts.worth_saying(found[1])


# --- row 1: Rome's ZTL (reviewed table) ----------------------------------------------------------------------


def _zone(key: str) -> ztl.Zone:
    return next(z for z in ztl.ZONES if z.key == key)


def test_ztl_centro_storico_by_day_and_hour() -> None:
    centro = _zone("centro_diurna")
    on, until = ztl.status(centro, datetime(2026, 11, 3, 10, 0, tzinfo=ROME))  # Tuesday
    assert on and until == datetime(2026, 11, 3, 18, 0, tzinfo=ROME)
    on, next_on = ztl.status(centro, datetime(2026, 11, 7, 12, 0, tzinfo=ROME))  # Saturday: 14-18
    assert not on and next_on == datetime(2026, 11, 7, 14, 0, tzinfo=ROME)
    assert ztl.status(centro, datetime(2026, 11, 8, 12, 0, tzinfo=ROME))[0] is False  # Sunday
    assert (
        ztl.status(centro, datetime(2026, 12, 8, 10, 0, tzinfo=ROME))[0] is False
    )  # Immacolata: esclusi i festivi


def test_ztl_night_windows_cross_midnight_and_rest_in_august() -> None:
    night = _zone("centro_notturna")
    assert ztl.status(night, datetime(2026, 11, 6, 23, 30, tzinfo=ROME)) == (
        True,
        datetime(2026, 11, 7, 3, 0, tzinfo=ROME),
    )
    assert (
        ztl.status(night, datetime(2026, 11, 7, 1, 0, tzinfo=ROME))[0] is True
    )  # Friday's window, after midnight
    assert ztl.status(night, datetime(2026, 11, 8, 1, 0, tzinfo=ROME))[0] is True  # Saturday's
    assert ztl.status(night, datetime(2026, 11, 9, 1, 0, tzinfo=ROME))[0] is False  # Sunday 01:00
    assert ztl.status(night, datetime(2026, 8, 7, 23, 30, tzinfo=ROME))[0] is False  # August: not active
    trastevere = _zone("trastevere_notturna")
    assert ztl.status(trastevere, datetime(2026, 5, 13, 22, 0, tzinfo=ROME))[0] is True  # a May Wednesday
    assert ztl.status(trastevere, datetime(2026, 11, 4, 22, 0, tzinfo=ROME))[0] is False  # a November one
    assert (
        ztl.status(trastevere, datetime(2026, 5, 1, 22, 0, tzinfo=ROME))[0] is True
    )  # Friday 1 May: even on public holidays


def test_ztl_names_the_zone_he_said_else_all() -> None:
    assert {z.key for z in ztl.named("la ZTL di Trastevere")} == {"trastevere_diurna", "trastevere_notturna"}
    assert len(ztl.named("posso entrare nella ZTL adesso?")) == len(ztl.ZONES)
    lines = ztl.lines([_zone("tridente")], datetime(2026, 11, 3, 10, 0, tzinfo=ROME))
    assert lines == ["ZTL Tridente (A1): ACTIVE now (no entry without a permit) until Tue 19:00"]


# --- row 1: pharmacies, airport ---------------------------------------------------------------------------------


def test_pharmacies_active_in_rome_with_a_believable_point_nearest_first() -> None:
    found = [
        pharmacies.Pharmacy(*r)
        for r in json.loads(pharmacies_json((DATA / "lazio_pharmacies.csv").read_bytes()))
    ]
    codes = {p.code for p in found}
    assert len(found) == 4  # of 11 rows: the closed ones, the one 50 km off and the one outside Rome are out
    assert "9083" not in codes and all(41.65 <= p.lat <= 42.10 for p in found)
    near = pharmacies.nearest(found, TERMINI)
    assert near and [d for d, _ in near] == sorted(d for d, _ in near) and len(near) <= 3
    assert "hours unknown" in pharmacies.line(*near[0])
    assert pharmacies.link(near[0][1]).startswith("https://maps.apple.com/?q=Farmacia")


def test_airport_leave_by_buffer_and_the_way_there() -> None:
    leg = {"kind": "flight", "at": "2026-11-05T10:35:00+01:00", "ref": "AZ5287", "place": "FCO → JFK"}
    flight = departures.from_leg(leg)
    assert flight is not None and flight.airport == "FCO" and flight.abroad
    buffers = {"international_min": 180, "domestic_min": 120, "train_min": 20, "transfer_default_min": 45}
    leave, way, fallback = departures.leave_by(flight, None, buffers)
    assert (way, fallback) == (55, True) and leave == flight.at - timedelta(minutes=235)
    leave, way, fallback = departures.leave_by(flight, 40, buffers)
    assert (way, fallback) == (40, False) and leave == flight.at - timedelta(minutes=220)
    assert departures.from_leg({"kind": "train", "at": leg["at"], "place": "Roma → Milano"}) is None
    event = departures.from_event(
        "Volo AZ1234 per Milano", "Fiumicino FCO", datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    )
    assert event is not None and event.airport == "FCO" and not event.abroad
    assert (
        departures.from_event("Cena con Nino", "Trattoria", datetime(2026, 11, 5, 20, 0, tzinfo=UTC)) is None
    )


def test_every_source_has_a_licence_and_an_attribution() -> None:
    assert all(s.licence and s.attribution and s.url.startswith("https://") for s in SOURCES.values())
    assert SOURCES["rome_trips"].fresh == timedelta(seconds=30)  # the rate limit: one call per window
    assert hosts() == {"romamobilita.it", "api-endpoint.mta.info", "rrgtfsfeeds.s3.amazonaws.com",
                       "raw.githubusercontent.com", "api.weather.gov", "dati.lazio.it"}  # fmt: skip
    assert (
        "CC BY-SA" in credit(["rome_trips", "rome_static"])
        and credit(["rome_trips", "rome_static"]).count(";") == 0
    )


# --- row 2: the skills, from them --------------------------------------------------------------------------------


def test_the_public_reads_are_on_the_views_allowlist_with_fixed_arguments() -> None:
    assert set(views.PUBLIC_READS) <= set(views.READS)
    ok = views.validate({"reads": [{"tool": "next_departures", "args": {"to": "home"}}],
                         "blocks": [{"type": "text", "text": "{{0.summary}}"}]}, set())  # fmt: skip
    assert isinstance(ok, views.Recipe)
    bad = views.validate({"reads": [{"tool": "next_departures", "args": {"to": "{{0.x}}"}}],
                          "blocks": [{"type": "text", "text": "x"}]}, set())  # fmt: skip
    assert isinstance(bad, str) and "placeholders" in bad
    assert isinstance(views.validate({"reads": [{"tool": "ztl_status", "args": {"url": "x"}}],
                                      "blocks": [{"type": "text", "text": "x"}]}, set()), str)  # fmt: skip


def test_every_starter_is_a_valid_k22_recipe_with_checks() -> None:
    assert [s.tool for s in skill_starters.STARTERS] == list(views.PUBLIC_READS)
    for s in skill_starters.STARTERS:
        assert isinstance(views.validate(s.recipe, set(), s.made_from), views.Recipe), s.name
        assert wishes.checks_of(list(s.checks)) == [dict(c) for c in s.checks]
        assert wishes.asks_wish(s.made_from), s.made_from
    assert skill_starters.named("Bus per  casa", country="IT") is skill_starters.STARTERS[0]
    assert (
        skill_starters.for_tool("nearest_pharmacy", country="IT") is not None
        and skill_starters.for_tool("web_search", country="IT") is None
    )


def test_public_reads_are_outside_data_and_may_run_on_his_schedule() -> None:
    public = [{"tool": "transit_alerts", "args": {}}]
    assert wishes.read_kind(public) == "public-data"
    assert wishes.read_kind([*public, {"tool": "web_search", "args": {}}]) == "web-results"
    assert not wishes.reads_outside(public)  # "dimmi alle 8 se la metro ha problemi"
    assert wishes.reads_outside([{"tool": "read_page", "args": {}}])  # unchanged


# --- row 3: errand combos ----------------------------------------------------------------------------------------


E1 = errands.Errand("t1", "Ritirare le camicie", "Lavanderia Rossi", 41.9830, 12.4640)
E2 = errands.Errand("t2", "Comprare il pane", "Forno Bianchi", 41.9830, 12.4600)  # ~390 m from E1
FAR = errands.Errand("t3", "Ferramenta", "Ferramenta Verdi", 41.9360, 12.4110)


def test_two_errands_a_short_walk_apart_are_one_suggestion() -> None:
    combo = errands.best([E1, E2, FAR], [], set())
    assert combo is not None and {e.task_id for e in combo.errands} == {"t1", "t2"} and combo.minutes <= 30
    assert (
        errands.question(combo, "it-IT")
        == "Passi da Lavanderia Rossi e Forno Bianchi in 15 minuti? Sono vicini."
    )
    assert errands.best([E1, E2], [], {combo.key}) is None  # never the same pair again within the week
    assert errands.best([E1, FAR], [], set()) is None


def test_an_errand_on_the_way_to_today_s_place() -> None:
    dentist = errands.Stopover("Dentista", "Studio Dentistico", 41.936, 12.409)
    combo = errands.best([E1, FAR], [dentist], set())
    assert combo is not None and combo.near is dentist and combo.errands == (FAR,)
    assert (
        errands.question(combo, "en-US")
        == "You're at Studio Dentistico today: stop by Ferramenta Verdi too? About 10 minutes."
    )


# --- row 4: streaks ------------------------------------------------------------------------------------------------


def test_streaks_in_weeks_from_his_calendar_and_done_tasks() -> None:
    today = date(2026, 11, 3)  # a Tuesday
    sessions = [("Palestra", today - timedelta(weeks=w)) for w in (0, 1, 2)] + [
        ("Corsa al parco", today - timedelta(weeks=1)), ("Corsa", today - timedelta(weeks=2)),
        ("Leggere 30 minuti", today - timedelta(weeks=3)),
        ("Rinnovare l'abbonamento della palestra", today), ("Comprare un libro", today),
    ]  # fmt: skip
    found = {s.kind: s for s in streaks.streaks(sessions, today)}
    assert found["gym"] == streaks.Streak("gym", 3, 1)
    assert found["run"] == streaks.Streak("run", 2, 0)  # this week has none yet: last week's keeps it
    assert found["reading"].weeks == 0  # three weeks ago: no streak (never "you broke it")
    blocks = streaks.poster_blocks(list(found.values()), "it-IT")
    assert [b["label"] for b in blocks] == ["Palestra, di fila", "Corsa, di fila"]
    assert blocks[0] == {
        "type": "stat",
        "label": "Palestra, di fila",
        "value": "3",
        "unit": "sett.",
        "trend": "up",
    }


def test_streaks_go_after_the_poster_s_gym_stat() -> None:
    poster = [{"type": "section", "title": "Le costanti"}, {"type": "stat", "label": "Palestra", "value": "3 su 4"},
              {"type": "stat", "label": "Fatti", "value": "5"}]  # fmt: skip
    out = streaks.into_poster(poster, [streaks.Streak("run", 4, 1)], "it-IT")
    assert [b.get("label") for b in out] == [None, "Palestra", "Corsa, di fila", "Fatti"]
    assert streaks.into_poster(poster, [streaks.Streak("run", 1, 1)], "it-IT") == poster


def test_dpc_and_static_reductions_keep_only_what_pisa_reads() -> None:
    zones = json.loads(bulletin_json((DATA / "dpc_latest_all.zip").read_bytes()))
    assert set(zones) == {"stamp", "today", "tomorrow"} and zones["today"][0][0].startswith(
        ("Lazi", "Pugl", "Lomb")
    )
    stops = json.loads(stops_json((DATA / "rome_gtfs.zip").read_bytes()))
    assert all(len(row) == 5 for row in stops)
    with pytest.raises(ValueError):
        pharmacies_json(b"CODICECOMUNEISTAT;DATAFINEVALIDITA\n12345;-\n")


def test_the_connector_reads_get_only_with_pisa_s_agent_capped_and_never_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import httpx

    from pisa.connectors import public_feeds
    from pisa.ports.public_data import PublicDataError

    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * 2048)
        if request.url.path == "/moved":
            return httpx.Response(301, headers={"location": "https://evil.example/"})
        return httpx.Response(200, content=b"ok")

    async def go() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), follow_redirects=False) as http:
            feeds = public_feeds.PublicFeeds(http)
            out: list[object] = [
                await feeds.get("https://api.weather.gov/ok", params={"point": "40.71,-74.01"})
            ]
            for path in ("/big", "/moved"):
                try:
                    await feeds.get(f"https://api.weather.gov{path}", cap=1024)
                except PublicDataError as err:
                    out.append(err.message)
            return out

    ok, big, moved = asyncio.run(go())
    assert ok == b"ok" and seen[0].method == "GET" and seen[0].headers["user-agent"].startswith("J-personal")
    assert "more than Pisa keeps" in str(big) and "HTTP 301" in str(moved)
    assert all(r.url.host == "api.weather.gov" for r in seen)  # the redirect was never followed
