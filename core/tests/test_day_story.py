# ruff: noqa: E501
"""The rest of the day as a story and two countries. One test (or a few) per behaviour; each fails when its
fix is reverted. Synthetic places and words, invented names."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from pisa.domain import countries, decisions, explore, trips, walks
from pisa.domain import vault as vault_rules
from pisa.domain.sheet import SheetData
from pisa.domain.stays import Fix
from tests.conftest import PROTOCOL_DIR, pair

TZ = ZoneInfo("Europe/Rome")
HOME = (45.533, 9.1730)  # an invented home in Milan
NORTH_M = 1 / 111_320.0  # degrees of latitude in a metre


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _settings(db: Any, **patch: Any) -> None:
    from pisa.domain import settings as setting_rules

    row = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
    value = setting_rules.apply_patch(setting_rules.with_defaults(row[0] if row else None, language="it-IT"),
                                      {"language": "it-IT", "quiet": {"hours": None}, **patch})  # fmt: skip
    db.execute("INSERT INTO app_settings (key, value) VALUES ('settings', %s) ON CONFLICT (key) DO UPDATE SET "
               "value = EXCLUDED.value", (Jsonb(value),))  # fmt: skip


def _home(db: Any, at: tuple[float, float] = HOME) -> None:
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('profile_home', %s) ON CONFLICT (key) DO UPDATE SET "
        "value = EXCLUDED.value",
        (Jsonb({"lat": at[0], "lon": at[1], "name": "Casa", "source": "owner", "confirmed": True}),),
    )


def _phone(db: Any) -> uuid.UUID:
    return uuid.UUID(pair(db, "iPhone")[0])


def _place(db: Any, name: str, at: tuple[float, float], category: str | None = None) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, category, lat, lon, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, "
        "now(), now())",
        (pid, name, category, at[0], at[1]),
    )
    return pid


def _stay(db: Any, device: uuid.UUID, at: tuple[float, float], arrived: datetime, departed: datetime | None,
          place: uuid.UUID | None = None) -> None:  # fmt: skip
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, departed_at, lat, lon, accuracy_m, last_seen_at, place_id) "
        "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 10, %s, %s)",
        (device, arrived, departed, at[0], at[1], departed or arrived + timedelta(minutes=10), place),
    )


def _walk(db: Any, device: uuid.UUID, start: datetime, minutes: int, mps: float = 1.3,
          origin: tuple[float, float] = HOME) -> tuple[float, float]:  # fmt: skip
    """A fix a minute, walking north at `mps`: where it ends."""
    lat, lon = origin
    for m in range(minutes + 1):
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) VALUES (%s, %s, %s, 12, %s)",
            (device, lat + m * 60 * mps * NORTH_M, lon, start + timedelta(minutes=m)),
        )
    return (lat + minutes * 60 * mps * NORTH_M, lon)


def _voice_task(db: Any, title: str, at: datetime, source: str = "voice") -> None:
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, %s, 'open', %s, %s, %s)",
        (title, title.casefold(), source, at, at),
    )


def _task(db: Any, title: str) -> str:
    task_id = uuid.uuid4()
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'open', 'manual', now(), now())",
        (task_id, title, title.casefold()),
    )
    return str(task_id)


# --- the contract ------------------------------------------------------------------------------------------


def test_contract_k20_gains_walk_recap_and_life_map(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.api import schemas

    hdr = {"Authorization": f"Bearer {pair(db)[1]}"}
    got = client.get("/v1/rituals", headers=hdr).json()
    assert got == json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "rituals.json").read_text())
    assert got["walk_recap"] == {"on": True} and got["life_map"] == {"on": True}
    schemas.Rituals.model_validate(got)
    put = client.put("/v1/rituals", headers=hdr, json={"walk_recap": {"on": False}})
    assert put.status_code == 200 and put.json()["walk_recap"] == {"on": False}
    assert put.json()["life_map"] == {"on": True}


# --- row 1: the end-of-walk recap ----------------------------------------------------------------------------


def _fixes(minutes: int, mps: float, gap: int = 1) -> list[Fix]:
    start = local("2026-11-18T17:00")
    return [Fix(HOME[0] + m * 60 * mps * NORTH_M, HOME[1], 12, start + timedelta(minutes=m))
            for m in range(0, minutes + 1, gap)]  # fmt: skip


def test_a_walk_is_15_minutes_on_foot_never_a_bus() -> None:
    since, end = local("2026-11-18T16:59"), local("2026-11-18T17:40")
    walk = walks.walk_before(_fixes(25, 1.3), since, end)
    assert walk is not None and walk.minutes == 25 and 1800 < walk.metres < 2100
    assert walks.walk_before(_fixes(12, 1.3), since, end) is None  # 12 minutes: no walk
    assert walks.walk_before(_fixes(25, 9.0), since, end) is None  # a bus
    assert walks.walk_before(_fixes(25, 0.1), since, end) is None  # standing about
    broken = [f for f in _fixes(30, 1.3) if not 10 < (f.at - since).total_seconds() / 60 < 23]
    assert walks.walk_before(broken, since, end) is None  # the stream broken for 12 minutes
    mixed = _fixes(25, 1.3)
    rode = [Fix(f.lat + (n % 5 == 0) * 0.01, f.lon, f.accuracy_m, f.at) for n, f in enumerate(mixed)]
    assert (
        walks.walk_before(rode, since, end) is None
    )  # a walking median, but 40% of the stretches a vehicle's
    blurred = [Fix(f.lat, f.lon, 300, f.at) for f in _fixes(25, 1.3)]
    assert walks.walk_before(blurred, since, end) is None  # 300 m fixes say nothing of a walk
    # review LOW: a ride, then 20 minutes on foot: only the walk after the ride counts
    ride = [Fix(HOME[0] - 0.05 + m * 0.002, HOME[1], 12, since + timedelta(minutes=m)) for m in range(1, 6)]
    walked = [f for f in _fixes(25, 1.3) if f.at >= local("2026-11-18T17:05")]
    after = walks.walk_before([*ride, *walked], since, end)
    assert after is not None and after.start == local("2026-11-18T17:05") and after.minutes == 20


def test_the_recap_is_his_records_in_20_seconds() -> None:
    assert walks.recap([], 25, "it-IT") is None  # nothing said: nothing invented
    items = [walks.Item("task", "Comprare il pane"), walks.Item("task", "Chiamare Nino"),
             walks.Item("note", "il bagnoschiuma da Esselunga")]  # fmt: skip
    line = walks.recap(items, 25, "it-IT")
    assert line == (
        "Nella camminata di 25 minuti mi hai lasciato 2 cose da fare: Comprare il pane, Chiamare Nino; "
        "una nota su un posto: il bagnoschiuma da Esselunga."
    )
    many = [
        walks.Item("task", f"Cosa numero {n} da ricordare assolutamente domani mattina") for n in range(9)
    ]
    long = walks.recap(many, 40, "it-IT")
    assert long is not None and len(long.split()) <= walks.WORDS_MAX + 8 and long.endswith("tue liste.")


def test_after_a_walk_home_pisa_recaps_once_a_day_from_the_records(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _home(db)
    phone = _phone(db)
    runtime = client.app.state.runtime
    start = local("2026-11-18T17:00")
    _stay(db, phone, HOME, start - timedelta(hours=2), start)  # at home before
    end = _walk(db, phone, start, 25)
    _place(db, "Bar Invented", end)
    arrived = start + timedelta(minutes=25)
    _stay(db, phone, HOME, arrived, None)  # back home (a loop): stopped at a known place
    _voice_task(db, "Comprare il pane", start + timedelta(minutes=5))
    _voice_task(db, "Pagare la bolletta", start + timedelta(minutes=8), source="email")  # not their words
    _voice_task(db, "Chiamare Nino", start - timedelta(hours=1))  # before the walk
    db.execute(
        "INSERT INTO place_notes (id, text, lat, lon, radius_m, place_name, turn_id, created_at) VALUES "
        "(gen_random_uuid(), 'il bagnoschiuma', 45.537, 9.17, 150, 'Esselunga', gen_random_uuid(), %s)",
        (start + timedelta(minutes=12),),
    )
    db.execute(  # review MED 3: a note made in the app (no turn) is no word said on the walk
        "INSERT INTO place_notes (id, text, lat, lon, radius_m, created_at) VALUES "
        "(gen_random_uuid(), 'la nota dall app', 45.537, 9.17, 150, %s)",
        (start + timedelta(minutes=13),),
    )
    clock.now = (arrived + timedelta(minutes=6)).astimezone(UTC)
    assert client.portal.call(runtime.rituals.tick) == ["walk_recap"]
    (text,) = db.execute("SELECT text FROM notifications").fetchone()
    assert text.startswith("Nella camminata di 25 minuti mi hai lasciato una cosa da fare: Comprare il pane")
    assert "il bagnoschiuma" in text
    assert "bolletta" not in text and "Nino" not in text
    assert "dall app" not in text
    clock.now += timedelta(minutes=1)
    assert client.portal.call(runtime.rituals.walks.tick) is False  # once a day


def test_no_recap_in_quiet_hours_after_a_ride_or_away_from_known_places(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db, quiet={"hours": {"start": "17:00", "end": "08:00"}})
    _home(db)
    phone = _phone(db)
    runtime = client.app.state.runtime
    start = local("2026-11-18T17:00")
    _walk(db, phone, start, 25)
    arrived = start + timedelta(minutes=25)
    _stay(db, phone, HOME, arrived, None)
    _voice_task(db, "Comprare il pane", start + timedelta(minutes=5))
    clock.now = (arrived + timedelta(minutes=6)).astimezone(UTC)
    assert client.portal.call(runtime.rituals.tick) == []  # quiet hours: nothing at all, never held
    _settings(db)
    db.execute("DELETE FROM stays")
    far = (HOME[0] + 0.05, HOME[1])  # 5.5 km away, nowhere they know
    _stay(db, phone, far, arrived, None)
    assert client.portal.call(runtime.rituals.walks.tick) is False
    db.execute("DELETE FROM stays; DELETE FROM device_locations")
    _walk(db, phone, start, 25, mps=9.0)  # a ride, then home
    _stay(db, phone, HOME, arrived, None)
    assert client.portal.call(runtime.rituals.walks.tick) is False
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)


# --- row 2: fog of war and one glint ---------------------------------------------------------------------------


def _north(metres: float, east: float = 0.0) -> tuple[float, float]:
    import math

    return (HOME[0] + metres * NORTH_M, HOME[1] + east * NORTH_M / math.cos(math.radians(HOME[0])))


def test_the_fog_counts_cells_by_direction_and_never_visited_places() -> None:
    stays = [_north(0), _north(600), _north(1200), _north(1800)] * 3
    seen = [
        *stays,
        _north(300),
        _north(900),
        _north(-500),
        _north(30_000),
    ]  # a fix far away explores nothing here
    known = [("Bar Mai Visto", _north(0, 1500), False), ("Bar Solito", _north(600), True)]
    f = explore.fog(HOME, stays, seen, known)
    assert f.radius_m == 2000 and f.explored == 7 and f.cells > 150
    assert f.never == ("Bar Mai Visto",)  # the one they stayed at isn't listed
    assert [e for e, _ in f.by_sector] == [5, 0, 0, 0, 1, 0, 0, 0]  # the north, one cell south
    sheet = explore.fog_sheet(f, "it-IT")
    SheetData.model_validate(sheet)
    assert sheet["title"] == "La tua nebbia di guerra"
    assert {"type": "stat", "label": "Esplorato", "value": str(f.share), "unit": "%"} in sheet["blocks"]
    assert {"type": "list", "items": ["Bar Mai Visto"]} in sheet["blocks"]
    glint = explore.glint_cell(f, seen)
    assert glint is not None and 400 <= explore.distance_m(HOME, glint) <= 600
    assert explore.cell_of(glint, HOME) not in {explore.cell_of(p, HOME) for p in seen}


class _Places:
    def __init__(self, found: list[dict[str, Any]]) -> None:
        self.found, self.calls = found, 0

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return self.found


def test_one_glint_a_week_as_something_different(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _home(db)
    phone = _phone(db)
    runtime = client.app.state.runtime
    bar = _place(db, "Bar Solito", _north(600), "cafe")
    for n in range(4):
        _stay(db, phone, _north(600), local(f"2026-11-1{n}T09:00"), local(f"2026-11-1{n}T09:30"), bar)
    _stay(db, phone, _north(1800), local("2026-11-14T09:00"), local("2026-11-14T09:30"))
    new_place = {"id": str(uuid.uuid4()), "name": "Caffè Nuovo", "address": "via Inventata 1",
                 "lat": _north(0, 500)[0], "lon": _north(0, 500)[1]}  # fmt: skip
    seen_place = {"id": str(uuid.uuid4()), "name": "Bar Visto", "address": "via Nota 2",
                  "lat": _north(600)[0], "lon": _north(600)[1]}  # fmt: skip
    fake = _Places([seen_place, new_place])
    runtime.rituals.explore._places = fake
    runtime.rituals._ideas = None  # only the glint can answer
    clock.now = local("2026-11-18T19:00").astimezone(UTC)  # a free Wednesday evening
    assert client.portal.call(runtime.rituals.something_different) is True
    (text,) = db.execute("SELECT text FROM notifications").fetchone()
    assert text == (
        "Vuoi provare qualcosa di diverso oggi? Poco lontano da te, in una zona dove non sei mai stato, c'è "
        "Caffè Nuovo, via Inventata 1."
    )  # never "Bar Visto": its cell is explored
    clock.now = local("2026-11-21T11:00").astimezone(UTC)  # Saturday: a second idea this week is no glint
    assert client.portal.call(runtime.rituals.explore.glint, set(), set()) == []
    assert fake.calls == 1
    clock.now = local("2026-11-23T19:00").astimezone(UTC)  # next week: the same place is never offered again
    used = {r[0] for r in db.execute("SELECT idea_key FROM ritual_asks").fetchall()}
    assert client.portal.call(runtime.rituals.explore.glint, set(), used) == []
    clock.now = local("2026-11-23T02:00").astimezone(UTC)  # never at night
    assert client.portal.call(runtime.rituals.something_different) is False


def test_the_fog_sheet_from_his_stays(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _home(db)
    phone = _phone(db)
    runtime = client.app.state.runtime
    assert client.portal.call(runtime.explore.fog_sheet, "it-IT") is not None  # home alone: all fog
    _stay(db, phone, _north(900), local("2026-11-10T09:00"), local("2026-11-10T10:00"))
    _place(db, "Libreria Mai Vista", _north(-700))
    clock.now = local("2026-11-18T12:00").astimezone(UTC)
    sheet = client.portal.call(runtime.explore.fog_sheet, "it-IT")
    SheetData.model_validate(sheet)
    assert {"type": "list", "items": ["Libreria Mai Vista"]} in sheet["blocks"]
    db.execute("DELETE FROM app_settings WHERE key = 'profile_home'; DELETE FROM stays")
    assert client.portal.call(runtime.explore.fog_sheet, "it-IT") is None  # Pisa doesn't know where they go


# --- row 3: the Map of the Life ---------------------------------------------------------------------------------


def test_seasons_and_the_counts() -> None:
    assert explore.season_of(date(2026, 11, 3)) == explore.Season(
        "autumn", date(2026, 9, 1), date(2026, 12, 1)
    )
    winter = explore.season_of(date(2027, 1, 10))
    assert winter.start == date(2026, 12, 1) and winter.label("it-IT") == "inverno 2026/27"
    assert explore.season_before(winter).name == "autumn"
    visits = [explore.Visit("a", "Bar Solito", date(2026, 9, d), d == 1) for d in (1, 2, 3, 3)]
    visits += [explore.Visit("b", "Museo Nuovo", date(2026, 10, 5), True)]
    m = explore.life_map(explore.season_of(date(2026, 10, 1)), visits,
                         [explore.TripLine("New York", "US", date(2026, 11, 20))], ["IT", "US", "IT"], 12)  # fmt: skip
    assert (m.places, m.firsts, m.top[0], m.countries) == (
        2,
        ("Bar Solito", "Museo Nuovo"),
        ("Bar Solito", 3),
        ("IT", "US"),
    )
    sheet = explore.life_sheet(m, "it-IT")
    SheetData.model_validate(sheet)
    assert sheet["title"] == "La mappa della tua vita: autunno 2026"
    assert {"type": "fact", "label": "Bar Solito", "value": "3 giorni"} in sheet["blocks"]
    assert {"type": "list", "items": ["New York (20/11)"]} in sheet["blocks"]


def test_the_season_s_map_once_on_its_first_day(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _home(db)
    phone = _phone(db)
    runtime = client.app.state.runtime
    bar = _place(db, "Bar Solito", _north(600))
    museum = _place(db, "Museo Nuovo", _north(2000))
    _stay(
        db, phone, _north(600), local("2026-08-20T09:00"), local("2026-08-20T09:30"), bar
    )  # summer: not a first
    for day in ("2026-09-02", "2026-09-09", "2026-10-01"):
        _stay(db, phone, _north(600), local(day + "T09:00"), local(day + "T09:30"), bar)
    _stay(db, phone, _north(2000), local("2026-10-11T15:00"), local("2026-10-11T17:00"), museum)
    clock.now = local("2026-11-30T19:00").astimezone(UTC)
    assert client.portal.call(runtime.rituals.tick) == []  # not the season's first day
    clock.now = local("2026-12-01T19:05").astimezone(UTC)
    assert "life_map" in client.portal.call(runtime.rituals.tick)
    assert "life_map" not in client.portal.call(runtime.rituals.tick)  # once a season
    (text, ritual) = db.execute("SELECT text, ritual FROM notifications").fetchone()
    assert text.startswith("La mappa della tua vita, autunno 2026, è pronta: 2 luoghi, 1 prime volte")
    assert ritual is None  # the schema's enum doesn't name it yet
    data = db.execute("SELECT data FROM ritual_sheets WHERE ritual = 'life_map'").fetchone()[0]
    assert {"type": "fact", "label": "Bar Solito", "value": "3 giorni"} in data["blocks"]
    assert {"type": "list", "items": ["Museo Nuovo"]} in data["blocks"]
    sheet = client.portal.call(runtime.explore.life_sheet, "it-IT", "last")
    assert sheet["title"] == "La mappa della tua vita: autunno 2026"
    assert client.portal.call(runtime.explore.life_sheet, "it-IT", "current") is None  # winter: nothing yet


# --- row 4: country-bound tasks ---------------------------------------------------------------------------------


def test_his_words_bind_a_task_to_a_country_a_bare_place_never() -> None:
    assert countries.bound_to("Rinnovare la patente solo in Italia") == "IT"
    assert countries.bound_to("Passare in banca quando torno negli Stati Uniti") == "US"
    assert countries.bound_to("pick up the parcel when I'm back in the US") == "US"
    assert countries.bound_to("Ritirare il pacco quando sono a New York") == "US"
    assert countries.bound_to("Chiamare la mamma in Italia") is None  # review H3: a bare place
    assert countries.bound_to("Prenotare il volo per l'Italia") is None
    assert countries.named_in_words("questo lo posso fare solo in Italia", "IT")
    assert not countries.named_in_words("ricordalo a noi, remind us", "US")  # the pronoun never counts
    assert countries.named_in_words("si può fare ovunque", None)
    assert countries.named_in_words("lo faccio solo quando sono a New York", "US")
    assert not countries.named_in_words("l'email dice USA", None)


def test_a_new_task_in_his_words_is_tagged(client, db) -> None:  # type: ignore[no-untyped-def]
    task_id = _task(db, "Ritirare la raccomandata solo in Italia")
    assert client.portal.call(client.app.state.runtime.trips.tag_countries) == 1
    assert db.execute("SELECT country FROM tasks WHERE id = %s", (task_id,)).fetchone() == ("IT",)


def test_the_vault_binds_in_person_renewals_and_aire_frees_the_cie(client, db) -> None:  # type: ignore[no-untyped-def]
    assert vault_rules.bound_country("cie", "IT", None, "IT") == "IT"
    assert vault_rules.bound_country("cie", "IT", True, "IT") is None  # AIRE: the consulate renews it
    assert vault_rules.bound_country("us_licence", None, None, "IT") == "US"
    assert vault_rules.bound_country("passport", "IT", None, "IT") is None  # a consulate, from anywhere
    assert vault_rules.bound_country("car_insurance", "IT", None, "IT") is None
    _settings(db)
    vault = client.app.state.runtime.autopilot.vault
    doc = client.portal.call(
        lambda: vault.add({"kind": "us_licence", "expires_on": "2027-03-01"}, provenance="said", lang="it-IT")
    )
    assert db.execute(
        "SELECT country, country_checked FROM tasks WHERE id = %s", (doc["task_id"],)
    ).fetchone() == ("US", True)
    client.portal.call(lambda: vault.set_aire(True, "words"))
    cie = client.portal.call(
        lambda: vault.add({"kind": "cie", "expires_on": "2027-02-01"}, provenance="said", lang="it-IT")
    )
    assert db.execute("SELECT country FROM tasks WHERE id = %s", (cie["task_id"],)).fetchone() == (None,)
    client.portal.call(client.app.state.runtime.trips.tag_countries)  # the words never re-bind it
    assert db.execute("SELECT country FROM tasks WHERE id = %s", (cie["task_id"],)).fetchone() == (None,)


def test_landing_says_these_3_you_can_only_do_here() -> None:
    line = trips.welcome("New York", "18:40", ["Cena"], ["DMV", "Real ID", "Social Security office"], "it-IT")
    assert line == ("Benvenuto a New York: sono le 18:40, domani hai Cena. Questi 3 li puoi fare solo qui: DMV, "
                    "Real ID, Social Security office.")  # fmt: skip
    assert trips.welcome_back_line("New York", ["DMV"], "en-US") == (
        "Welcome to New York. This one you can only do here: DMV."
    )


def test_the_checklist_holds_the_open_bound_tasks_as_they_are_now(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.test_two_countries import FLIGHT, SENT, _email

    cie = _task(db, "Rinnovare la CIE")
    _email(db, FLIGHT, SENT)
    runtime = client.app.state.runtime
    client.portal.call(runtime.trips.refresh)
    clock.now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    client.portal.call(runtime.trips.leaving)
    db.execute("UPDATE tasks SET status = 'done', done_at = now() WHERE id = %s", (cie,))
    _task(db, "Passare all'anagrafe")
    client.portal.call(runtime.trips.tag_countries)
    sheet = client.portal.call(runtime.trips.checklist_sheet, "it-IT")
    left = [i for b in sheet["blocks"] if b["type"] == "list" for i in b["items"]]
    assert "Da finire qui: Passare all'anagrafe" in left
    assert "Da finire qui: Rinnovare la CIE" not in left
    items = [
        {"text": "Da finire qui: X", "done": True, "by": "pisa"},
        {"text": "Mio", "done": False, "by": "owner"},
    ]
    assert trips.sync_bound(items, ["Y"], "it-IT") == [
        {"text": "Da finire qui: X", "done": True, "by": "pisa"},  # they ticked it: it stays
        {"text": "Da finire qui: Y", "done": False, "by": "pisa"},
        {"text": "Mio", "done": False, "by": "owner"},
    ]


def test_set_task_country_only_on_his_words_naming_it() -> None:
    with decisions.collecting():
        import asyncio
        from types import SimpleNamespace

        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        from pisa.brain.task_tools import add_task_control_tools

        task = SimpleNamespace(id=str(uuid.uuid4()), title="Ritirare la raccomandata", status="open")
        changed: list[dict[str, Any]] = []

        async def tasks(status: str) -> list[Any]:
            return [task]

        async def update_task(task_id: str, changes: dict[str, Any]) -> Any:
            changed.append(changes)
            # The store's row, read back (its country as stored, not the call's)
            return SimpleNamespace(
                id=task.id, title=task.title, status="open", due_at=None, due_kind="none", expected_from=None,
                duration_min=None, priority=2, place_text=None, country="IT", repeat=None, notes=None,
            )  # fmt: skip

        words = {"now": ""}
        agent: Agent[None, str] = Agent(TestModel())
        data = SimpleNamespace(tasks=tasks, update_task=update_task, timezone="Europe/Rome")
        add_task_control_tools(agent, data, None,  # type: ignore[arg-type]
                               lambda: "it-IT", lambda: words["now"])  # fmt: skip
        tool: Any = agent._function_toolset.tools["set_task_country"].function  # pyright: ignore[reportPrivateUsage]
        words["now"] = "l'email dice che va fatta negli Stati Uniti? fallo tu"
        assert asyncio.run(tool(task_ref="raccomandata", country="IT")).startswith("Not changed")
        assert changed == []  # their words name another country, never this one
        words["now"] = "la raccomandata solo in Italia"
        assert "Now: «Ritirare la raccomandata», open, only in IT." in asyncio.run(
            tool(task_ref="raccomandata", country="IT")
        )
        assert changed == [{"country": "IT"}]


# --- the review of 940c4bb ------------------------------------------------------------------------


def test_an_email_s_words_never_bind_a_task(client, db) -> None:  # type: ignore[no-untyped-def]
    assert countries.bound_to("Pagare la fattura", "Da pagare solo in USA", his_words=False) is None
    assert countries.bound_to("Ritirare pacco", "consegna solo a Milano", his_words=False) is None
    assert countries.bound_to("this is only to us") is None  # the pronoun is no country
    db.execute(
        "INSERT INTO tasks (id, title, title_key, notes, status, source_kind, created_at, updated_at) VALUES "
        "(gen_random_uuid(), 'Ritirare pacco', 'ritirare pacco', 'consegna solo a Milano', 'open', 'email', now(), now())"
    )
    mine = _task(db, "Ritirare la raccomandata solo in Italia")
    assert client.portal.call(client.app.state.runtime.trips.tag_countries) == 1
    assert db.execute("SELECT title FROM tasks WHERE country IS NOT NULL").fetchall() == [
        ("Ritirare la raccomandata solo in Italia",)
    ]
    assert mine


def _glint_world(db: Any) -> tuple[uuid.UUID, Any]:
    _settings(db)
    _home(db)
    phone = _phone(db)
    bar = _place(db, "Bar Solito", _north(600), "cafe")
    for n in range(4):
        _stay(db, phone, _north(600), local(f"2026-11-1{n}T09:00"), local(f"2026-11-1{n}T09:30"), bar)
    return phone, bar


def test_no_glint_away_from_home_and_his_zone_s_evening(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import zone as zone_rules

    phone, _ = _glint_world(db)
    runtime = client.app.state.runtime
    new_place = {"id": str(uuid.uuid4()), "name": "Caffè Nuovo", "address": "via Inventata 1",
                 "lat": _north(0, 500)[0], "lon": _north(0, 500)[1]}  # fmt: skip
    fake = _Places([new_place])
    runtime.rituals.explore._places = fake

    async def an_idea(declined: set[str], used: set[str]) -> list[dict[str, str]]:
        return [{"kind": "museum", "key": "museum:mudec", "text": "il Mudec"}]

    runtime.rituals._ideas = an_idea
    clock.now = local("2026-11-18T19:00").astimezone(UTC)
    client.app.state.runtime.owner.place = zone_rules.PhoneContext("America/New_York", "US")
    # 19:00 in Rome is 13:00 in New York: not their evening there; and they are away: no glint
    assert client.portal.call(runtime.rituals.something_different) is False
    assert client.portal.call(runtime.rituals.explore.glint, set(), set()) == [] and fake.calls == 0
    client.app.state.runtime.owner.place = None
    _stay(db, phone, (40.75, -73.99), local("2026-11-18T10:00"), None)  # their last stay: New York
    assert client.portal.call(runtime.rituals.explore.glint, set(), set()) == [] and fake.calls == 0


def test_the_walk_s_day_is_his_zone_s(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import zone as zone_rules

    _settings(db)
    _home(db)
    phone = _phone(db)
    runtime = client.app.state.runtime
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 18, 18, 30, tzinfo=ny)  # 00:30 on the 19th in Rome
    _walk(db, phone, start, 25)
    arrived = start + timedelta(minutes=25)
    _stay(db, phone, HOME, arrived, None)
    _voice_task(db, "Comprare il pane", start + timedelta(minutes=5))
    db.execute("INSERT INTO walk_recaps (day, walk_start, walk_end, items, created_at) VALUES "
               "('2026-11-18', now(), now(), 1, now())")  # fmt: skip  # one already today, New York's day
    client.app.state.runtime.owner.place = zone_rules.PhoneContext("America/New_York", "US")
    clock.now = (arrived + timedelta(minutes=5)).astimezone(UTC)
    assert client.portal.call(runtime.rituals.walks.tick) is False


def test_the_glint_never_offers_a_place_he_stayed_at(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    phone, _ = _glint_world(db)
    runtime = client.app.state.runtime
    old = _place(db, "Caffè Di Una Volta", _north(0, 500))
    _stay(
        db, phone, _north(0, 500), local("2025-06-01T09:00"), local("2025-06-01T10:00"), old
    )  # over a year ago
    db.execute("UPDATE stays SET lat = %s, lon = %s WHERE place_id = %s", (*_north(3000), old))
    fake = _Places([{"id": str(old), "name": "Caffè Di Una Volta", "address": "via Vecchia 1",
                     "lat": _north(0, 500)[0], "lon": _north(0, 500)[1]}])  # fmt: skip
    runtime.rituals.explore._places = fake
    clock.now = local("2026-11-18T19:00").astimezone(UTC)
    assert client.portal.call(runtime.rituals.explore.glint, set(), set()) == []
    assert fake.calls == 1
    clock.now = local("2026-11-20T19:00").astimezone(UTC)  # review LOW: one search a week, found or not
    assert client.portal.call(runtime.rituals.explore.glint, set(), set()) == [] and fake.calls == 1


def test_the_glint_s_point_and_key() -> None:
    from pisa.domain import rituals as ritual_rules

    moved = explore.jitter(HOME, (1.0, -1.0))
    assert 130 <= explore.distance_m(HOME, moved) <= 145  # 100 m each way
    assert explore.jitter(HOME, (0.0, 0.0)) == (round(HOME[0], 6), round(HOME[1], 6))
    assert ritual_rules.idea_key("cafe", "p1") == "cafe:p1"  # the glint's key is every idea's for that place


def test_the_map_counts_only_his_confirmed_trips(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _home(db)
    _phone(db)
    runtime = client.app.state.runtime
    for title, status, doubt, day in (("Roma → New York", "planned", None, "2026-10-01"),
                                      ("Roma → Parigi", "cancelled", None, "2026-10-05"),
                                      ("Roma → Londra", "planned", "someone_else", "2026-10-07")):  # fmt: skip
        db.execute(
            "INSERT INTO trips (id, title, to_city, to_country, depart_at, arrive_at, status, doubt, created_at, "
            "updated_at) VALUES (gen_random_uuid(), %s, %s, 'US', %s, %s, %s, %s, now(), now())",
            (title, title.split(" → ")[1], local(day + "T10:00"), local(day + "T18:00"), status, doubt),
        )
    clock.now = local("2026-12-02T12:00").astimezone(UTC)
    m = client.portal.call(runtime.explore.life_map, explore.season_of(date(2026, 10, 1)))
    assert [t.city for t in m.trips] == ["New York"]


def test_aire_said_later_frees_the_open_cie_task(client, db) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    vault = client.app.state.runtime.autopilot.vault
    cie = client.portal.call(
        lambda: vault.add({"kind": "cie", "expires_on": "2027-02-01"}, provenance="said", lang="it-IT")
    )
    assert db.execute("SELECT country FROM tasks WHERE id = %s", (cie["task_id"],)).fetchone() == ("IT",)
    client.portal.call(lambda: vault.set_aire(True, "words"))
    assert db.execute("SELECT country FROM tasks WHERE id = %s", (cie["task_id"],)).fetchone() == (None,)
    client.portal.call(lambda: vault.set_aire(False, "words"))
    assert db.execute("SELECT country FROM tasks WHERE id = %s", (cie["task_id"],)).fetchone() == ("IT",)
