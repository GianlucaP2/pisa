"""Local notifications scheduled on the phone (v0.1.4, contract 2.3's `pisa.notify`), server side.

- the schedule: the next 7 days, at most 50 items (reminders, timed tasks, calendar leads, leave alerts), the
  quiet hours left to the server; at most 20 regions (their own places), coordinates rounded; the lock screen
  minimal (the kind only, no body unless details are on, never a stranger's words); deep links the schema's;
- the frames: a revision per device that only grows; the whole schedule on a connection, only what changed
  on a change (a reminder set, cancelled with cancel_reminder, fired; a task done), a fired one cancelled
  after its grace; every frame valid contract 2.3; one sync per burst of changes;
- the ack: taken only for ids this device was sent in that revision (a stale one, another device's, a revision
  never sent: ignored); one out of the schema is `invalid_frame`, never quoting a value; `denied` noted;
- the honesty rail: "te lo ricordo alle …" only when the reminder reaches them (a push path, or the phone
  acked recently with notifications allowed); else remember_to's result and the rail say they'll see it in
  the app;
- no double alert: a phone that acked an alert gets no push for it; with no fresh ack the push goes;
- review mode or PISA_PHONE_NOTIFY_ENABLED off: never granted, never listed.

Synthetic data only: made-up titles, Testville's coordinates."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from pisa_conformance.validate import EXT_ID, client_frame_errors, errors
from psycopg.types.json import Jsonb

from pisa.api.live import EXTENSIONS, HelloFrame, negotiate
from pisa.api.server import features, served
from pisa.db.pool import open_pool
from pisa.domain import abilities
from pisa.domain import phone_notify as rules
from pisa.ports.push import PushOutcome
from pisa.services import phone_notify
from pisa.services.agui import LiveSession
from pisa.services.devices import Device
from pisa.services.notifications import Dispatcher, enqueue
from pisa.services.push import PushService
from pisa.services.session import DeviceSession, FrameError
from tests.conftest import Clock, dsn
from tests.owner import owner_at
from tests.test_live_v2_frames import FULL, checked, hello

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "v2" / "fixtures" / "gaps"
SCHEMA = EXT_ID
TZ = "Europe/Rome"
NOW = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)  # 10:00 in Rome: no quiet hours (22:30-07:00)
TESTVILLE = (45 + 0.001234, 9 + 0.003456)  # synthetic, built here: finer than the 4 decimals sent
ROUNDED = (round(TESTVILLE[0], 4), round(TESTVILLE[1], 4))
PRIVATE_WORD = "lontraviolacea"  # a reminder's words: never on the lock screen by default, never logged


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def device(db: psycopg.Connection, name: str = "iPhone") -> uuid.UUID:
    ident = uuid.uuid4()
    db.execute(
        "INSERT INTO devices (id, name, platform, created_at, paired_via) VALUES (%s, %s, 'ios', %s, 'app')",
        (ident, name, NOW),
    )
    return ident


def promise(
    db: psycopg.Connection, what: str, due: datetime | None, *, condition: str = "time",
    place: uuid.UUID | None = None, provenance: str = "owner_words", **done: datetime,
) -> uuid.UUID:  # fmt: skip
    ident = uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_promises (id, what, condition, due_at, place_id, lang, provenance, created_at, "
        "fired_at, cancelled_at) VALUES (%s, %s, %s, %s, %s, 'it-IT', %s, %s, %s, %s)",
        (ident, what, condition, due, place, provenance, NOW - timedelta(minutes=1), done.get("fired"),
         done.get("cancelled")),
    )  # fmt: skip
    return ident


def task(
    db: psycopg.Connection, title: str, due: datetime, *, priority: int = 2, source: str = "voice"
) -> uuid.UUID:
    ident = uuid.uuid4()
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at, due_at, "
        "due_kind, priority) VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, 'datetime', %s)",
        (
            ident,
            title,
            title.lower(),
            source,
            NOW - timedelta(days=1),
            NOW - timedelta(days=1),
            due,
            priority,
        ),
    )
    return ident


def event(db: psycopg.Connection, ident: str, title: str, start: datetime, *, all_day: bool = False) -> None:
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES ('c', 'Personale', 'owner') "
        "ON CONFLICT DO NOTHING"
    )
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
        "VALUES ('c', %s, %s, %s, %s, %s, 'confirmed')",
        (ident, title, start, start + timedelta(hours=1), all_day),
    )


def place(db: psycopg.Connection, name: str, lat: float, lon: float) -> uuid.UUID:
    ident = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, lat, lon, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (ident, name, lat, lon, NOW, NOW),
    )
    return ident


@asynccontextmanager
async def env(db: psycopg.Connection, clock: Clock | None = None) -> AsyncIterator[SimpleNamespace]:
    pool = await open_pool(dsn(db), max_size=4)
    await pool.wait()
    clock = clock or Clock(NOW)
    try:
        yield SimpleNamespace(
            pool=pool, clock=clock, owner=owner_at(TZ, clock=clock),
            settings=SimpleNamespace(
                phone_notify_details=False, phone_notify_enabled=True, review_mode=False
            ),
        )  # fmt: skip
    finally:
        await pool.close()


def phone(runtime: Any, device_id: uuid.UUID, negotiated: Any = FULL) -> DeviceSession:
    """A connected phone's session (the socket's sink is a list)."""
    known = Device(device_id, "iPhone", "ios", NOW, None, None)
    live = LiveSession(str(device_id))
    live.attach(lambda _text: None)
    return DeviceSession(object(), known, runtime, live, negotiated)  # type: ignore[arg-type]


def notify_frames(s: DeviceSession) -> list[tuple[str, dict[str, Any]]]:
    frames = checked(s.live.kept(), 1) if s.live.kept() else []  # the kit's checks: schema, lifecycle, seq
    return [
        (f["event"]["name"], f["event"]["value"]) for f in frames
        if f["event"]["type"] == "CUSTOM" and f["event"]["name"].startswith("pisa.notify.")
    ]  # fmt: skip


def last(s: DeviceSession, since: int) -> list[tuple[str, dict[str, Any]]]:
    return notify_frames(s)[since:]


# --- the contract's bounds ----------------------------------------------------------------------------------


def _fixtures(kind: str) -> list[Path]:
    return sorted((FIXTURES / kind).glob("notify_ack*.json"))


@pytest.mark.parametrize("path", _fixtures("valid"), ids=lambda p: p.name)
def test_an_ack_the_contract_allows_is_taken(path: Path) -> None:
    value = json.loads(path.read_text())["frame"]["value"]
    got = rules.parse_ack(value)
    assert got.revision == value["revision"] and list(got.scheduled) == value["scheduled"]


@pytest.mark.parametrize("path", _fixtures("invalid"), ids=lambda p: p.name)
def test_an_ack_out_of_the_contract_s_bounds_is_refused_whole(path: Path) -> None:
    frame = json.loads(path.read_text())["frame"]
    assert client_frame_errors(frame)  # the kit refuses it too
    with pytest.raises(rules.Refused):
        rules.parse_ack(frame["value"])


def test_more_bounds_the_server_checks_itself_and_never_quotes() -> None:
    ok = {"revision": 3, "scheduled": ["rem-1"], "failed": []}
    for bad in (
        ok | {"revision": True},  # a boolean is no number
        ok | {"revision": 0},
        ok | {"revision": 1.5},
        ok | {"scheduled": [f"{PRIVATE_WORD}\n"]},  # a trailing newline
        ok | {"scheduled": ["a" * 65]},
        ok | {"scheduled": ["rem-1", "rem-1"]},  # unique
        ok | {"scheduled": ["x"] * 51},
        ok | {"failed": [{"id": "rem-1", "reason": PRIVATE_WORD}]},
        ok | {"failed": [{"id": "rem-1", "reason": "denied", "note": PRIVATE_WORD}]},
        ok | {"extra": PRIVATE_WORD},
        {"revision": 3, "scheduled": []},  # failed is required
        [PRIVATE_WORD],
    ):
        with pytest.raises(rules.Refused) as refused:
            rules.parse_ack(bad)
        assert PRIVATE_WORD not in str(refused.value)


# --- the schedule -------------------------------------------------------------------------------------------


def test_the_schedule_holds_the_next_7_days_of_every_kind_and_leaves_the_rest(db) -> None:  # type: ignore[no-untyped-def]
    soon = promise(db, f"chiamare la {PRIVATE_WORD}", NOW + timedelta(hours=2))
    promise(db, "oltre la settimana", NOW + timedelta(days=8))
    promise(db, "annullato", NOW + timedelta(hours=3), cancelled=NOW)
    promise(db, "già detto", NOW + timedelta(hours=3), fired=NOW)
    promise(db, "di notte", datetime(2026, 11, 24, 22, 0, tzinfo=UTC))  # 23:00 in Rome: quiet hours
    dated = task(db, "Portare il modulo", NOW + timedelta(hours=5))
    task(db, "Bassa priorità", NOW + timedelta(hours=5), priority=3)  # low is never reminded
    event(db, "e1", "Riunione di prova", NOW + timedelta(days=1))
    event(db, "e2", "Tutto il giorno", NOW + timedelta(days=2), all_day=True)
    leave_at = NOW + timedelta(hours=3)
    plan = {"date": "2026-11-24", "items": [
        {"kind": "travel", "leave_at": leave_at.isoformat(), "minutes": 20, "mode": "walk"},
        {"kind": "task", "task_id": str(dated), "title": "Portare il modulo"},
    ]}  # fmt: skip
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) "
        "VALUES (gen_random_uuid(), '2026-11-24', %s, 'test', 'f', %s)",
        (NOW, Jsonb(plan)),
    )

    async def go() -> tuple[list[rules.Item], list[rules.Region]]:
        async with env(db) as e, e.pool.connection() as conn:
            return await phone_notify.desired(conn, NOW, e.owner)

    items, regions = run(go())
    got = {(i.kind, i.fire_at) for i in items}
    assert got == {
        ("reminder", NOW + timedelta(hours=2)),
        ("leave", leave_at - timedelta(minutes=5)),  # the leave lead
        ("task", NOW + timedelta(hours=5) - timedelta(minutes=15)),  # the task lead
        ("calendar", NOW + timedelta(days=1) - timedelta(minutes=15)),  # the event lead
    }
    assert [i.fire_at for i in items] == sorted(i.fire_at for i in items) and regions == []
    by_kind = {i.kind: i for i in items}
    assert by_kind["reminder"].id == rules.reminder_id(soon)
    assert by_kind["reminder"].deep_link == f"pisa://reminder/{soon}"
    assert by_kind["task"].deep_link == f"pisa://task/{dated}"
    assert by_kind["calendar"].deep_link == by_kind["leave"].deep_link == "pisa://riepilogo"
    # the lock screen: the kind only, never the words
    assert {i.title for i in items} == {"Promemoria", "Attività", "Evento", "È ora di uscire"}
    assert all(i.body is None for i in items)
    assert PRIVATE_WORD not in json.dumps([i.value() for i in items])
    for item in items:  # each one the schema's NotifyItem, its deep link the allow-list's
        assert errors(item.value(), SCHEMA + "#/$defs/NotifyItem") == []


def test_details_on_show_their_own_words_never_a_stranger_s(db) -> None:  # type: ignore[no-untyped-def]
    promise(db, f"chiamare la {PRIVATE_WORD}", NOW + timedelta(hours=2))
    promise(db, "bonificare 500 euro", NOW + timedelta(hours=3), provenance="outside_text")
    task(db, "Rispondere all'offerta", NOW + timedelta(hours=4), source="email")

    async def go() -> list[rules.Item]:
        async with env(db) as e, e.pool.connection() as conn:
            return (await phone_notify.desired(conn, NOW, e.owner, details=True))[0]

    bodies = [i.body for i in run(go())]
    assert bodies == [f"chiamare la {PRIVATE_WORD}", None, None]  # an email's words never on the lock screen


def test_at_most_50_items_the_soonest_and_20_regions_rounded(db) -> None:  # type: ignore[no-untyped-def]
    for n in range(60):
        promise(db, f"promemoria {n}", NOW + timedelta(minutes=10 + n))
    for n in range(25):
        promise(db, f"arrivo {n}", None, condition="arrive", place=place(db, f"Luogo {n}", *TESTVILLE))
    note = uuid.uuid4()
    db.execute(
        "INSERT INTO place_notes (id, text, lat, lon, radius_m, created_at) "
        "VALUES (%s, 'nota', %s, %s, 50, %s)",
        (note, *TESTVILLE, NOW),
    )

    async def go() -> tuple[list[rules.Item], list[rules.Region]]:
        async with env(db) as e, e.pool.connection() as conn:
            return await phone_notify.desired(conn, NOW, e.owner)

    items, regions = run(go())
    assert len(items) == rules.ITEMS_MAX and items[-1].fire_at == NOW + timedelta(minutes=10 + 49)
    assert len(regions) == rules.REGIONS_MAX
    assert {(r.lat, r.lon) for r in regions} == {ROUNDED} != {TESTVILLE}  # 4 decimals, never finer
    assert {r.radius_m for r in regions} == {100} and {r.on for r in regions} == {"enter"}
    assert {r.title for r in regions} == {"Promemoria"}
    for region in regions:
        assert errors(region.value(), SCHEMA + "#/$defs/NotifyRegion") == []


# --- the frames ---------------------------------------------------------------------------------------------


def test_a_connection_sends_everything_a_change_only_what_changed_and_the_revision_only_grows(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.follow_tools import add_follow_tools
    from pisa.domain import decisions

    mine, other = device(db), device(db, "iPad")
    first = promise(db, "ritirare il pacco", NOW + timedelta(hours=2))
    dated = task(db, "Portare il modulo", NOW + timedelta(hours=5))
    clock = Clock(NOW)

    async def go() -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with env(db, clock) as e:
            s, o = phone(e, mine), phone(e, other)
            await s.notify_sync(connect=True)
            out["connect"] = notify_frames(s)
            await s.notify_sync()  # nothing changed: nothing sent
            out["idle"] = last(s, len(out["connect"]))
            await o.notify_sync(connect=True)  # the other device: its own counter
            out["other"] = notify_frames(o)
            # a reminder set
            second = promise(db, "innaffiare le piante", NOW + timedelta(hours=1))
            n = len(notify_frames(s))
            await s.notify_sync()
            out["set"], out["second"] = last(s, n), second

            # cancelled with cancel_reminder, on their own words
            async def live_promises() -> list[dict[str, Any]]:
                return [{"id": str(second), "what": "innaffiare le piante", "condition": "time",
                         "due_at": NOW + timedelta(hours=1), "place": None}]  # fmt: skip

            async def cancel_promise(ident: str) -> bool:
                async with e.pool.connection() as conn:
                    got = await conn.execute(
                        "UPDATE pisa_promises SET cancelled_at = %s WHERE id = %s AND cancelled_at IS NULL "
                        "RETURNING id", (NOW, uuid.UUID(ident)),
                    )  # fmt: skip
                    return await got.fetchone() is not None

            from pydantic_ai import Agent
            from pydantic_ai.models.test import TestModel

            data = SimpleNamespace(timezone=TZ, now=lambda: NOW, live_promises=live_promises,
                                   cancel_promise=cancel_promise)  # fmt: skip
            agent: Agent[None, str] = Agent(TestModel())
            add_follow_tools(agent, data, None, None, gate=None, lang=lambda: "it-IT")  # type: ignore[arg-type]
            tool: Any = agent._function_toolset.tools["cancel_reminder"].function  # pyright: ignore[reportPrivateUsage]
            with decisions.collecting("annulla il promemoria delle piante"):
                out["cancelled"] = await tool(what="piante")
            n = len(notify_frames(s))
            await s.notify_sync()
            out["cancel"] = last(s, n)
            # the task done
            async with e.pool.connection() as conn:
                await conn.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (dated,))
            n = len(notify_frames(s))
            await s.notify_sync()
            out["done"] = last(s, n)
            # a reconnection: everything again
            n = len(notify_frames(s))
            await s.notify_sync(connect=True)
            out["again"] = last(s, n)
        return out

    out = run(go())
    ((name, value),) = out["connect"]
    assert name == "pisa.notify.schedule" and value["revision"] == 1
    assert [i["id"] for i in value["items"]] == [rules.reminder_id(first), rules.task_id(dated)]
    assert out["idle"] == [] and out["other"][0][1]["revision"] == 1  # one counter per device
    ((name, value),) = out["set"]
    assert name == "pisa.notify.schedule" and value["revision"] == 2
    assert [i["id"] for i in value["items"]] == [rules.reminder_id(out["second"])]  # only the new one
    assert out["cancelled"].startswith("Cancelled")
    assert out["cancel"] == [
        ("pisa.notify.cancel", {"revision": 3, "ids": [rules.reminder_id(out["second"])]})
    ]
    assert out["done"] == [("pisa.notify.cancel", {"revision": 4, "ids": [rules.task_id(dated)]})]
    ((name, value),) = out["again"]
    assert name == "pisa.notify.schedule" and value["revision"] == 5
    assert [i["id"] for i in value["items"]] == [rules.reminder_id(first)]
    assert db.execute("SELECT revision FROM device_notify WHERE device_id = %s", (mine,)).fetchone() == (5,)


def test_a_fired_reminder_is_cancelled_only_after_its_grace(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.reminders import RemindersService

    mine = device(db)
    due = NOW + timedelta(minutes=30)
    ident = promise(db, "ritirare il pacco", due)
    clock = Clock(NOW)

    async def go() -> list[Any]:
        seen: list[Any] = []
        async with env(db, clock) as e:
            s = phone(e, mine)
            await s.notify_sync(connect=True)
            clock.now = due + timedelta(seconds=20)  # the reminders step fires it at its minute
            await RemindersService(e.pool, owner=e.owner, clock=clock).run()
            n = len(notify_frames(s))
            seen.append(await s.notify_sync())  # within the grace: the phone shows it first
            seen.append(last(s, n))
            clock.now = due + rules.FIRED_GRACE + timedelta(seconds=1)
            seen.append(await s.notify_sync())
            seen.append(last(s, n))
        return seen

    later, during, again, after = run(go())
    assert db.execute(
        "SELECT fired_at IS NOT NULL FROM pisa_promises WHERE id = %s", (ident,)
    ).fetchone() == (True,)
    assert later is True and during == [] and again is False
    assert after == [("pisa.notify.cancel", {"revision": 2, "ids": [rules.reminder_id(ident)]})]


def test_regions_go_whole_and_only_when_they_change(db) -> None:  # type: ignore[no-untyped-def]
    mine = device(db)
    where = place(db, "Bottega di prova", *TESTVILLE)
    ident = promise(db, "comprare il pane", None, condition="leave", place=where)

    async def go() -> list[Any]:
        async with env(db) as e:
            s = phone(e, mine)
            await s.notify_sync(connect=True)
            await s.notify_sync()
            async with e.pool.connection() as conn:
                await conn.execute("UPDATE pisa_promises SET cancelled_at = %s WHERE id = %s", (NOW, ident))
            await s.notify_sync()
            return notify_frames(s)

    frames = run(go())
    assert [n for n, _ in frames] == ["pisa.notify.regions", "pisa.notify.regions"]
    (_, first), (_, emptied) = frames
    assert first["regions"] == [{
        "id": rules.region_id(ident), "lat": ROUNDED[0], "lon": ROUNDED[1], "radiusMeters": 100,
        "title": "Promemoria",
        "on": "exit", "deepLink": f"pisa://reminder/{ident}",
    }]  # fmt: skip
    assert emptied == {"revision": 2, "regions": []}  # the whole set: an empty one stops them all


def test_a_burst_of_changes_is_one_sync_per_phone() -> None:
    synced: list[bool] = []

    class Phone:
        async def notify_sync(self, *, connect: bool = False) -> bool:
            synced.append(connect)
            return False

    async def go() -> None:
        changed = phone_notify.NotifyChanged(lambda: [Phone(), Phone()], every_s=0.05)
        for _ in range(5):
            changed.changed("pisa_promises")
        await asyncio.sleep(0.2)

    run(go())
    assert synced == [False, False]


def test_a_phone_that_did_not_grant_pisa_notify_gets_nothing(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.session import Negotiated

    mine = device(db)
    promise(db, "ritirare il pacco", NOW + timedelta(hours=2))
    others = {k: 1 for k in EXTENSIONS if k != "pisa.notify"}

    async def go() -> list[Any]:
        async with env(db) as e:
            s = phone(e, mine, Negotiated(["core:1"], [0, 1], False, others))
            await s.notify_sync(connect=True)
            with pytest.raises(FrameError):
                await s.notify_ack({"revision": 1, "scheduled": [], "failed": []})
            return s.live.kept()

    assert run(go()) == []
    assert db.execute("SELECT count(*) FROM device_notify").fetchone() == (0,)


# --- the ack ------------------------------------------------------------------------------------------------


def _states(db: psycopg.Connection, device_id: uuid.UUID) -> dict[str, tuple[str, str | None]]:
    rows = db.execute(
        "SELECT id, state, reason FROM device_notify_items WHERE device_id = %s", (device_id,)
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_an_ack_counts_only_for_this_device_s_ids_of_that_revision(db) -> None:  # type: ignore[no-untyped-def]
    mine, other = device(db), device(db, "iPad")
    one = rules.reminder_id(promise(db, "ritirare il pacco", NOW + timedelta(hours=2)))
    two_id = promise(db, "innaffiare le piante", NOW + timedelta(hours=3))
    two = rules.reminder_id(two_id)

    async def go() -> None:
        async with env(db) as e:
            s, o = phone(e, mine), phone(e, other)
            await s.notify_sync(connect=True)  # revision 1: both
            await o.notify_sync(connect=True)
            await o.notify_ack({"revision": 1, "scheduled": [], "failed": []})
            async with e.pool.connection() as conn:  # the second moves: sent again in revision 2
                await conn.execute(
                    "UPDATE pisa_promises SET due_at = %s WHERE id = %s", (NOW + timedelta(hours=4), two_id)
                )
            await s.notify_sync()
            # revision 1's ack arrives late: `one` is taken, `two` is stale (re-sent in 2), an unknown id
            # is ignored
            await s.notify_ack({"revision": 1, "scheduled": [one, two, "rem-unknown"], "failed": []})
            # another device's ack never touches this one's
            await o.notify_ack({"revision": 1, "scheduled": [one], "failed": []})
            # a revision never sent: dropped whole
            await s.notify_ack({"revision": 99, "scheduled": [two], "failed": []})
            # an ack out of the schema: invalid_frame, nothing quoted
            with pytest.raises(FrameError) as bad:
                await s.notify_ack({"revision": 2, "scheduled": [f"{PRIVATE_WORD}\n"], "failed": []})
            assert PRIVATE_WORD not in str(bad.value)
            await s.notify_ack({"revision": 2, "scheduled": [], "failed": [{"id": two, "reason": "limit"}]})

    run(go())
    assert _states(db, mine) == {one: ("scheduled", None), two: ("failed", "limit")}
    assert _states(db, other) == {one: ("scheduled", None), two: ("sent", None)}
    row = db.execute(
        "SELECT acked_revision, denied_at FROM device_notify WHERE device_id = %s", (mine,)
    ).fetchone()
    assert row == (2, None)


def test_notifications_denied_are_noted_and_a_later_schedule_clears_it(db) -> None:  # type: ignore[no-untyped-def]
    mine = device(db)
    one = rules.reminder_id(promise(db, "ritirare il pacco", NOW + timedelta(hours=2)))

    async def go() -> list[bool]:
        async with env(db) as e:
            s = phone(e, mine)
            await s.notify_sync(connect=True)
            await s.notify_ack({"revision": 1, "scheduled": [], "failed": [{"id": one, "reason": "denied"}]})
            async with e.pool.connection() as conn:
                denied = not await phone_notify.phone_works(conn, NOW)
            await s.notify_sync(connect=True)
            await s.notify_ack({"revision": 2, "scheduled": [one], "failed": []})
            async with e.pool.connection() as conn:
                works = await phone_notify.phone_works(conn, NOW)
        return [denied, works]

    assert run(go()) == [True, True]


# --- the honesty rail ---------------------------------------------------------------------------------------


class Paths:
    def __init__(self, direct: bool = False, relay: bool = False) -> None:
        self.direct, self.relay = direct, relay

    def configured(self) -> bool:
        return self.direct

    def relay_configured(self) -> bool:
        return self.relay


def test_a_reminder_reaches_them_only_by_a_push_path_or_a_phone_that_acked(db) -> None:  # type: ignore[no-untyped-def]
    mine = device(db)
    soon = promise(db, "ritirare il pacco", NOW + timedelta(hours=2))
    far = promise(db, "tra dieci giorni", NOW + timedelta(days=10))

    async def go() -> list[bool]:
        got: list[bool] = []
        async with env(db) as e:
            reach = phone_notify.Reach(e.pool, e.owner, e.settings, Paths(), e.clock)
            got.append(await reach(str(soon)))  # no push, no ack: never "te lo ricordo"
            s = phone(e, mine)
            await s.notify_sync(connect=True)
            got.append(await reach(str(soon)))  # sent, not acked yet
            await s.notify_ack({"revision": 1, "scheduled": [rules.reminder_id(soon)], "failed": []})
            got.append(await reach(str(soon)))  # the phone works: a new item will be scheduled
            got.append(await reach(str(far)))  # outside the phone's 7 days
            e.clock.now = NOW + rules.ACK_FRESH + timedelta(minutes=1)
            got.append(await reach(str(soon)))  # the ack is old
            e.clock.now = NOW
            off = SimpleNamespace(phone_notify_enabled=False, review_mode=False)
            got.append(await phone_notify.Reach(e.pool, e.owner, off, Paths(), e.clock)(str(soon)))
            # a push path: the relay set up and the device registered
            relay = phone_notify.Reach(e.pool, e.owner, e.settings, Paths(relay=True), e.clock)
            got.append(await relay(str(far)))
            async with e.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO device_push_relay (device_id, handle, secret, public_key, created_at) "
                    "VALUES (%s, 'h-1', %s, %s, %s)", (mine, b"s" * 32, b"k" * 32, NOW),
                )  # fmt: skip
            got.append(await relay(str(far)))
        return got

    assert run(go()) == [False, False, True, False, False, False, False, True]


def _remember(reaches: bool | None) -> str:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pisa.brain.follow_tools import add_follow_tools
    from pisa.domain import decisions

    async def remember_to(*args: Any) -> str:
        return str(uuid.UUID(int=7))

    async def stored(_: str) -> dict[str, Any]:
        return {
            "what": "ritirare il pacco",
            "condition": "time",
            "due_at": NOW + timedelta(hours=2),
            "place": None,
        }

    async def reach(_: str) -> bool:
        return bool(reaches)

    data = SimpleNamespace(timezone=TZ, now=lambda: NOW, remember_to=remember_to, promise=stored)
    if reaches is not None:
        data.reaches = reach
    agent: Agent[None, str] = Agent(TestModel())
    add_follow_tools(agent, data, None, None, gate=None, lang=lambda: "it-IT")  # type: ignore[arg-type]
    tool: Any = agent._function_toolset.tools["remember_to"].function  # pyright: ignore[reportPrivateUsage]

    async def go() -> str:
        with decisions.collecting("ricordami alle 12 di ritirare il pacco"):
            return await tool(what="ritirare il pacco", condition="time", when="12:00")

    return run(go())


def test_remember_to_s_result_says_the_truth_both_ways() -> None:
    from pisa.brain.follow_tools import UNREACHED

    ident = uuid.UUID(int=7)
    reached = f"Reminder set: «ritirare il pacco» at 12:00 (id {ident})."
    assert _remember(True) == reached and _remember(None) == reached  # unchanged when it reaches them
    assert _remember(False) == f"{reached} {UNREACHED}"
    assert abilities.UNREACHED_MARK in UNREACHED and abilities.unreached([_remember(False)])


def test_the_rail_holds_te_lo_ricordo_and_keeps_the_true_lines() -> None:
    from pisa.brain.follow_tools import UNREACHED

    unreached = [f"Reminder set: «ritirare il pacco» at 12:00 (id x). {UNREACHED}"]
    reached = ["Reminder set: «ritirare il pacco» at 12:00 (id x)."]
    for held in ("Te lo ricordo alle 12.", "Ok, alle 12 ti ricordo di ritirare il pacco.",
                 "Ho impostato il promemoria, te lo ricordo alle 12.", "I'll remind you at 12."):  # fmt: skip
        assert abilities.promised_unreached(held, unreached), held
        assert not abilities.promised_unreached(held, reached), held
    for true in ("Ho impostato il promemoria per le 12.",
                 "Promemoria impostato: lo vedrai quando apri l'app.",
                 "Reminder set for 12: you'll see it when you open the app.", "Fatto."):  # fmt: skip
        assert not abilities.promised_unreached(true, unreached), true
    assert abilities.seen_in_app("it-IT") == "Promemoria impostato: lo vedrai quando apri l'app."
    assert abilities.seen_in_app("en-US") == "Reminder set: you'll see it when you open the app."


# --- no double alert, no lost one ---------------------------------------------------------------------------


class Sender:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, token: str, environment: str, **kw: Any) -> PushOutcome:
        self.sent.append(kw)
        return PushOutcome("sent")


class Push:
    def __init__(self) -> None:
        self.apns = Sender()

    async def sender(self, conn: Any) -> Sender:
        return self.apns


class Offline:
    async def notify(self, device_id: object, notification: Any) -> bool:
        return False


@pytest.mark.parametrize("acked", [True, False, "stale", "denied"])
def test_a_phone_that_holds_the_reminder_gets_no_push_for_it_else_the_push_goes(db, acked) -> None:  # type: ignore[no-untyped-def]
    mine = device(db)
    db.execute("UPDATE devices SET apns_token = %s, apns_environment = 'development'", ("ab" * 32,))
    due = NOW + timedelta(hours=2)
    ident = promise(db, "ritirare il pacco", due)
    local = rules.reminder_id(ident)
    push = Push()
    clock = Clock(NOW)

    async def go() -> str | None:
        async with env(db, clock) as e:
            s = phone(e, mine)
            await s.notify_sync(connect=True)
            if acked == "denied":
                await s.notify_ack(
                    {"revision": 1, "scheduled": [], "failed": [{"id": local, "reason": "denied"}]}
                )
            elif acked:
                await s.notify_ack({"revision": 1, "scheduled": [local], "failed": []})
            clock.now = due + timedelta(seconds=5)
            if acked == "stale":
                clock.now = due + rules.ACK_FRESH  # the phone's word is too old to stand on
            async with e.pool.connection() as conn:
                nid, _ = await enqueue(
                    conn, kind="reminder", text="Ritirare il pacco.", lang="it-IT", now=clock.now,
                    timezone=TZ,
                    urgency="now", path="attend.reminder", local_id=local, local_at=due,
                )  # fmt: skip
            return await Dispatcher(
                e.pool, cast(Any, Offline()), cast(PushService, push), clock=clock, owner=owner_at(TZ)
            ).deliver(nid)

    via = run(go())
    if acked is True:
        assert via == "phone" and push.apns.sent == []  # the phone's own notification is the alert
    else:
        assert via == "apns" and len(push.apns.sent) == 1  # never lost: pushed as today


def test_the_reminders_step_marks_its_notice_with_the_phone_s_item(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.reminders import RemindersService

    due = NOW + timedelta(minutes=1)
    ident = promise(db, "ritirare il pacco", due)
    dated = task(db, "Portare il modulo", NOW + timedelta(minutes=16))

    async def go() -> None:
        async with env(db, Clock(due)) as e:
            await RemindersService(e.pool, owner=e.owner, clock=e.clock).run()

    run(go())
    rows = set(db.execute("SELECT local_id, local_at FROM notifications").fetchall())
    assert rows == {(rules.reminder_id(ident), due), (rules.task_id(dated), due)}


# --- switched off -------------------------------------------------------------------------------------------


def test_off_or_in_review_mode_pisa_notify_is_never_granted_nor_listed() -> None:
    asked = HelloFrame.model_validate(
        hello(contract={"versions": ["2.3"]}, extensions={"pisa.notice": 1, "pisa.notify": 1})
    )
    on, off = negotiate(asked), negotiate(asked, phone_notify=False)
    assert not isinstance(on, str) and not isinstance(off, str)
    assert on.extensions == {"pisa.notice": 1, "pisa.notify": 1} and off.extensions == {"pisa.notice": 1}
    ids = {f["id"] for f in served()}
    assert rules.FEATURE in ids and rules.FEATURE not in {f["id"] for f in served(phone_notify=False)}
    assert rules.FEATURE not in {f["id"] for f in features(frozenset(), phone_notify=False)}
    settings = SimpleNamespace(phone_notify_enabled=True, review_mode=True)
    assert phone_notify.phone_notify_on(settings) is False
    assert phone_notify.phone_notify_on(SimpleNamespace(phone_notify_enabled=True, review_mode=False))
    assert not phone_notify.phone_notify_on(SimpleNamespace(phone_notify_enabled=False, review_mode=False))
