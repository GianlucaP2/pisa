"""Pisa decides when to speak. One gate for every unprompted word (`attend`), the
score from the content first, breakpoints, their rules, the day's density, decay per kind, the record, the
Sunday card's line and the user report's measures. Every word here is synthetic."""

import ast
import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from pisa.db.pool import open_pool
from pisa.domain import guardian as rules
from pisa.domain import ledger as ledger_rules
from pisa.domain import settings as setting_rules
from pisa.domain.attend import SOURCES
from pisa.domain.errors import PisaError
from pisa.domain.text import people_name_key
from pisa.infra.keys import KeyStore
from pisa.services import guardian
from pisa.services.guardian_tick import GuardianService, measures, week
from pisa.services.notifications import Dispatcher, Kind, enqueue
from pisa.services.push import PushService
from tests.assistant_env import rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.test_push import Live

PISA = Path(__file__).resolve().parent.parent / "pisa"
NOW = datetime(
    2026, 11, 10, 10, 0, tzinfo=UTC
)  # Tuesday 11:00 in Rome: no quiet hours, after the 08:30 digest
TZ = "Europe/Rome"


# --- helpers ------------------------------------------------------------------------------------------------


def run(db: psycopg.Connection, job: Callable[[Any], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        pool = await open_pool(dsn(db), max_size=4)
        await pool.wait()
        try:
            return await job(pool)
        finally:
            await pool.close()

    return asyncio.run(go())


def notice(
    db: psycopg.Connection, path: str | None, *, kind: str = "info", now: datetime = NOW, **extra: Any
) -> uuid.UUID:
    async def job(pool: Any) -> uuid.UUID:
        async with pool.connection() as conn:
            nid, _ = await enqueue(
                conn, kind=cast(Kind, kind), text="Una cosa di prova.", lang="it-IT", now=now, timezone=TZ,
                path=path,
                **extra,
            )  # fmt: skip
            return nid

    return run(db, job)


def deliver(
    db: psycopg.Connection, nid: uuid.UUID, device: str, now: datetime = NOW
) -> tuple[str | None, Live]:
    live = Live({device})

    async def job(pool: Any) -> str | None:
        keys = KeyStore(SecretStr(Fernet.generate_key().decode()))  # no APNs key stored: sockets only
        push = PushService(pool, keys, httpx.AsyncClient(), team_id="T", topic="t")
        return await Dispatcher(pool, live, push, clock=Clock(now), owner=owner_at(TZ)).deliver(nid)

    return run(db, job), live


def tick(db: psycopg.Connection, now: datetime) -> int:
    return run(db, lambda pool: GuardianService(pool, owner=owner_at(TZ), clock=Clock(now)).tick())


def item_row(db: psycopg.Connection, nid: uuid.UUID) -> dict[str, Any]:
    cur = db.execute(
        "SELECT path, kind, decision, reason, score, override, rule_source, released_at, released_how, "
        "counted FROM guardian_items WHERE ref = %s",
        (f"notification:{nid}",),
    )
    row = cur.fetchone()
    assert row is not None, "the gate kept no record of this notice"
    names = [d.name for d in cur.description or []]
    return dict(zip(names, row, strict=True))


def meetings(db: psycopg.Connection, *spans: tuple[datetime, datetime], title: str = "Riunione") -> None:
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES ('cal', 'Lavoro', 'owner') "
        "ON CONFLICT DO NOTHING"
    )
    for i, (start, end) in enumerate(spans):
        db.execute(
            "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, "
            "attendees) VALUES ('cal', %s, %s, %s, %s, false, 'confirmed', 2)",
            (f"m{i}", title, start, end),
        )


def digest_went(db: psycopg.Connection, at: datetime) -> None:
    """The last digest went at `at` (a day's earlier slot)."""
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('guardian.digest_at', %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb(at.isoformat()),),
    )


def item(
    path: str, *, deadline: datetime | None = None, floor: bool = False, person: str | None = None
) -> rules.Item:
    found, outside = rules.path_of(path)
    return rules.Item("i", found, deadline, floor, person, outside)


# --- 1. one gate for every unprompted word ------------------------------------------------------------------


def _enqueue_calls() -> list[tuple[str, int, ast.Call]]:
    calls = []
    for file in PISA.rglob("*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
                if name == "enqueue":
                    calls.append((str(file.relative_to(PISA)), node.lineno, node))
    return calls


def _names(value: ast.expr) -> list[str]:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.IfExp):
        return _names(value.body) + _names(value.orelse)
    if isinstance(value, ast.BinOp):  # attend: f"attend.{source}" + (":outside" if … else "")
        return _names(value.left)
    if isinstance(value, ast.JoinedStr):
        head = value.values[0]
        assert isinstance(head, ast.Constant) and head.value == "attend.", "only attend's path is built"
        return [f"attend.{source}" for source in SOURCES]
    raise AssertionError(f"a path must be a name of the map, not {ast.dump(value)}")


def test_1_every_production_notice_names_a_path_of_the_map() -> None:
    """The map is complete: each `enqueue` in Pisa names its path, and each name is a row of `PATHS`."""
    seen: set[str] = set()
    calls = _enqueue_calls()
    assert len(calls) >= 24  # (POST /v1/turns took its two with it)
    for where, line, call in calls:
        path = next((k.value for k in call.keywords if k.arg == "path"), None)
        assert path is not None, f"{where}:{line} enqueues a notice with no path (the guardian's map)"
        names = _names(path)
        unmapped = [n for n in names if rules.path_of(n)[0].name == "unmapped"]
        assert not unmapped, f"{where}:{line}: {names} not in the map"
        seen.update(names)
    unsent = {"approval.asked", "approval.request", "live.start", "live.state"}  # not notices: their own push
    unsent |= {"turn.answer"}  # POST /v1/turns' late answer: no longer sent; a queued one still delivers
    assert set(rules.PATHS) - unsent <= seen, "a row of the map no notice uses"


SENDERS = {
    "sender.send(": {"services/notifications.py", "services/approvals.py"},
    "send_live_activity(": {"services/live_activity.py", "connectors/apns.py", "ports/push.py"},
    "live.approval(": {"services/approvals.py"},
    "self._live.notify(": {"services/notifications.py"},
    ".send_notify(": {"services/session_registry.py"},
    ".proactive(": {"services/session_registry.py", "services/talk.py"},
    "TalkAskToOpen(": {
        "services/session_registry.py",
        "services/session_messages.py",
    },  # session_messages.py: its class
}


def test_1_nothing_speaks_or_pushes_around_the_gate() -> None:
    """Every send to the phone lives where the gate runs first: the dispatcher (every notice, the talk's ask
    to open, Pisa speaking inside a talk), the approval push, the lock screen."""
    for needle, allowed in SENDERS.items():
        found = {str(f.relative_to(PISA)) for f in PISA.rglob("*.py") if needle in f.read_text()}
        assert found <= allowed, f"{needle} outside the gated paths: {found - allowed}"
    for gated in ("services/notifications.py", "services/approvals.py", "services/live_activity.py"):
        assert "guardian.gate(" in (PISA / gated).read_text(), f"{gated} sends without the gate"
    gateway = (PISA / "services/session_registry.py").read_text()
    # the socket's notify, Pisa speaking in a talk and the ask to open run only inside SessionRegistry.notify,
    # which only the dispatcher calls (the LiveDevices port)
    assert gateway.count(".send_notify(") == 1 and gateway.count(".proactive(") == 2


NOTICE_PATHS = sorted(
    p for p in rules.PATHS if not p.startswith(("approval.asked", "approval.request", "live."))
)


@pytest.mark.parametrize("path", NOTICE_PATHS)
def test_1_each_path_goes_through_the_gate_and_his_never_holds_it(db, path: str) -> None:  # type: ignore[no-untyped-def]
    """A test per path: its notice is decided and kept in the record; his "never" for its kind holds it (a
    reply to their own words, and the holds' own digests, are logged and never held by a rule)."""
    device, _ = pair(db)
    kind = rules.PATHS[path].kind
    if kind in rules.RULE_KINDS:
        set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": kind}]}})
    nid = notice(db, path)
    via, live = deliver(db, nid, device)
    row = item_row(db, nid)
    assert row["path"] == path and row["kind"] == kind
    if rules.PATHS[path].whose == "his_words":
        assert row["reason"] == "his_words" and via == "ws" and not row["counted"]
    elif kind == "digest":
        assert row["reason"] == "digest:idle" and via == "ws"
    else:
        assert (row["decision"], row["reason"], row["override"]) == ("digest", "his_rule_never", "never")
        assert via is None and live.sent == []


def test_1_the_approval_push_goes_through_the_gate(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """Pisa's own proposal in a meeting that ends before it lapses is held (it stays in the app's list,
    nothing is sent); the change their own words asked for goes at once (a reply, not an interruption); their
    "never" for approvals sends one silently (its lapse comes before any digest)."""
    device, _ = pair(db)
    approvals = client.app.state.runtime.approvals
    live = Live({device})
    approvals._live = lambda: live  # the socket's approval frame, recorded
    meetings(db, (clock.now - timedelta(minutes=30), clock.now + timedelta(minutes=4)))
    details = ["Domani 10:00"]
    held = client.portal.call(
        lambda: approvals.create(
            "calendar_create", "Sposto la palestra", details, {"x": 1}, origin="proposal"
        )
    )
    asked = client.portal.call(
        lambda: approvals.create("calendar_create", "Sposto la cena", details, {"x": 2}, origin="words")
    )
    set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": "approval"}]}})
    hushed = client.portal.call(
        lambda: approvals.create("calendar_create", "Sposto il pranzo", details, {"x": 3}, origin="proposal")
    )
    rows = dict(db.execute("SELECT ref, reason FROM guardian_items WHERE ref LIKE 'approval:%'").fetchall())
    assert rows == {
        f"approval:{held['id']}": "busy_meeting",
        f"approval:{asked['id']}": "his_words",
        f"approval:{hushed['id']}": "his_rule_never_silent",
    }
    # The held one never reached the phone
    assert [a.id for _, a in live.approvals] == [asked["id"], hushed["id"]]


def test_1_the_lock_screen_s_alert_goes_through_the_gate(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Round 2 (LOW, a behaviour test): the day's first start carries an alert; with his "never" for the
    morning's kind it starts silently (no alert) and the record holds it; the state pushes are never kept."""
    from tests.test_live_activity import START, plan, pushes, store_plan
    from tests.test_live_activity import tick as live_tick
    from tests.test_planner import place
    from tests.test_push import Apple, with_token

    museum = place(db, "Museo di prova", (45.0, 9.0))
    device, token = pair(db)
    with_token(db, device)
    put = client.put("/v1/devices/me/live-activity", headers={"Authorization": f"Bearer {token}"},
                     json={"token": START, "kind": "start"})  # fmt: skip
    assert put.status_code == 204
    set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": "briefing"}]}})
    store_plan(db, plan(museum, rome(2026, 10, 22, 14, 39)), rome(2026, 10, 22, 7, 50))
    apple = Apple()
    assert live_tick(db, secrets_key, rome(2026, 10, 22, 9, 0), apple) == 1
    first = pushes(apple)[-1][1]["aps"]
    assert first["event"] == "start" and "alert" not in first  # started, silently
    kept = db.execute("SELECT path, reason FROM guardian_items WHERE ref LIKE 'live:%'").fetchall()
    assert kept == [("live.start", "his_rule_never")]
    state = rules.attend(item("live.state"), rules.Moment(NOW, busy=rules.Busy("driving", NOW)))
    assert (state.decision, state.reason) == ("now", "silent") and not rules.counted(state.reason)


def test_1_the_brain_never_sees_the_guardian() -> None:
    """Nothing is added to a turn's prompt: no brain module reads the guardian."""
    for file in (PISA / "brain").rglob("*.py"):
        assert "guardian" not in file.read_text(), f"{file.name} reads the guardian"


# --- 2. urgency from the content first, then the moment -----------------------------------------------------


def test_2_the_score_keeps_its_parts_content_first() -> None:
    soon = NOW + timedelta(minutes=10)
    leave = rules.score(item("attend.leave", deadline=soon), NOW)
    assert leave.parts() == {"what": 30, "whose": 15, "soon": 30, "cost": 25, "total": 100}
    clock = rules.score(item("vault.renewal", deadline=NOW + timedelta(days=3)), NOW)  # a legal clock
    nice = rules.score(item("errands.combo"), NOW)  # a nice-to-know
    moved = rules.score(item("attend.replan"), NOW)  # a meeting moved
    assert clock.cost == 25 and moved.cost == 15 and nice.cost == 0
    assert nice.total < rules.BREAKPOINT_AT <= moved.total and clock.total > nice.total
    # the same words from outside text are never raised by it
    outside = rules.score(item("attend.replan:outside", deadline=soon), NOW)  # Pisa's, a meeting moved: 5, 15
    assert outside.whose == 0 and outside.cost == 0


def test_2_the_gate_decides_now_breakpoint_or_digest_with_a_reason() -> None:
    idle = rules.Moment(NOW)
    assert rules.attend(item("attend.leave", deadline=NOW + timedelta(minutes=5)), idle).decision == "now"
    replan = rules.attend(item("attend.replan"), idle)
    assert (replan.decision, replan.reason, replan.released) == ("breakpoint", "score:idle", True)
    nice = rules.attend(item("errands.combo"), rules.Moment(NOW, digest_at=NOW + timedelta(hours=1)))
    assert (nice.decision, nice.reason, nice.until) == ("digest", "low_score", NOW + timedelta(hours=1))
    assert (
        rules.attend(
            item("attend.reminder", floor=True), rules.Moment(NOW, busy=rules.Busy("talk", NOW))
        ).reason
        == "floor"
    )


def test_2_a_low_notice_goes_to_the_digest_and_a_scored_one_at_the_breakpoint(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    low, mid = notice(db, "errands.combo"), notice(db, "attend.replan", kind="replan")
    assert deliver(db, low, device)[0] is None and deliver(db, mid, device)[0] == "ws"
    assert db.execute("SELECT guardian FROM notifications WHERE id = %s", (low,)).fetchone() == ("digest",)
    assert item_row(db, mid)["score"] == {"what": 20, "whose": 5, "soon": 0, "cost": 15, "total": 40}


# --- 3. breakpoints -----------------------------------------------------------------------------------------


def test_3_breakpoints_are_named_by_what_just_ended() -> None:
    meeting = rules.event(NOW - timedelta(hours=1), NOW - timedelta(minutes=5), "Riunione", 2)
    assert rules.at_breakpoint([meeting], NOW) == "calendar_gap"
    assert rules.at_breakpoint([], NOW, arrived_at=NOW - timedelta(minutes=3)) == "walk_end"
    assert rules.at_breakpoint([], NOW, last_words_at=NOW - timedelta(minutes=5)) == "talk_end"
    assert (
        rules.at_breakpoint([], NOW, talking=False, last_words_at=NOW - timedelta(seconds=10)) == "talk_end"
    )
    assert rules.at_breakpoint([], NOW) == "idle"
    now_meeting = rules.event(NOW - timedelta(minutes=5), NOW + timedelta(minutes=30), "Sync", 0)
    assert rules.at_breakpoint([now_meeting], NOW) is None
    assert rules.at_breakpoint([], NOW, moving=True) is None
    assert rules.at_breakpoint([], NOW, last_words_at=NOW - timedelta(seconds=30)) is None  # still talking


def test_3_a_held_notice_is_released_at_the_next_breakpoint(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    end = NOW + timedelta(minutes=40)
    meetings(db, (NOW - timedelta(minutes=20), end))
    nid = notice(db, "attend.replan", kind="replan")
    assert deliver(db, nid, device)[0] is None
    assert db.execute("SELECT guardian, not_before FROM notifications WHERE id = %s", (nid,)).fetchone() == (
        "breakpoint", end,
    )  # fmt: skip
    assert tick(db, NOW + timedelta(minutes=10)) == 0  # still in the meeting
    later = end + timedelta(minutes=1)
    assert tick(db, later) == 1
    row = item_row(db, nid)
    assert (row["decision"], row["released_how"]) == ("breakpoint", "breakpoint:calendar_gap")
    assert deliver(db, nid, device, later)[0] == "ws"


def test_3_held_items_fold_into_the_next_digest(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    digest_went(db, NOW - timedelta(hours=2))  # 08:31 in Rome: this morning's slot went
    a, b = notice(db, "errands.combo"), notice(db, "trip.leaving")
    alone = notice(db, "learning.weekly", weekly=True)  # its card needs its own notice
    for nid in (a, b, alone):
        assert deliver(db, nid, device)[0] is None
        assert deliver(db, nid, device)[0] is None  # the sweep's next try: still the digest's
    assert tick(db, NOW + timedelta(minutes=30)) == 0  # 11:30: no slot since 08:30's
    at = datetime(2026, 11, 10, 11, 31, tzinfo=UTC)  # 12:31 in Rome
    assert tick(db, at) == 3
    digest = db.execute("SELECT text, path FROM notifications WHERE path = 'guardian.digest'").fetchone()
    assert digest == ("Nel frattempo: Una cosa di prova. Una cosa di prova.", "guardian.digest")
    assert (
        item_row(db, a)["released_how"] == "digest" and item_row(db, alone)["released_how"] == "digest_alone"
    )
    assert tick(db, at + timedelta(minutes=5)) == 0  # once a slot


# --- 4. their rules win
# ---------------------------------------------------------------------------------------


def test_4_his_rule_fires_through_a_busy_block(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    meetings(db, (NOW - timedelta(minutes=30), NOW + timedelta(minutes=5)),
             (NOW + timedelta(minutes=10), NOW + timedelta(hours=1)))  # fmt: skip
    held = notice(db, "attend.replan", kind="replan")
    assert deliver(db, held, device)[0] is None
    assert item_row(db, held)["reason"] == "busy_back_to_back"
    set_settings(db, {"guardian": {"rules": [{"mode": "always", "kind": "replan"}]}})
    nid = notice(db, "attend.replan", kind="replan")
    via, live = deliver(db, nid, device)
    assert via == "ws" and len(live.sent) == 1
    row = item_row(db, nid)
    assert (row["decision"], row["reason"], row["override"], row["rule_source"]) == (
        "now", "his_rule_always", "always", "settings",
    )  # fmt: skip


def test_4_his_rule_said_in_words_names_one_of_his_people(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    marta = uuid.uuid4()
    db.execute(
        "INSERT INTO people (id, name, name_key, source, emails, created_at, updated_at) "
        "VALUES (%s, 'Marta Rossi', 'marta rossi', 'owner', '{}', now(), now())",
        (marta,),
    )
    db.execute(
        "INSERT INTO profile_claims (id, kind, statement, subject, polarity, scope, provenance, support, "
        "against, confidence, state, sensitive, hard, created_at, updated_at) VALUES (%s, 'constraint', %s, "
        "'{}', 1, '{}', 'said', 1, 0, 0.33, 'active', false, false, now(), now())",
        (uuid.uuid4(), "Interrompimi sempre per Marta, anche in riunione"),
    )
    meetings(db, (NOW - timedelta(minutes=30), NOW + timedelta(minutes=5)),
             (NOW + timedelta(minutes=10), NOW + timedelta(hours=1)))  # fmt: skip
    nid = notice(db, "people.offer", person_id=marta)
    assert deliver(db, nid, device)[0] == "ws"
    assert (item_row(db, nid)["override"], item_row(db, nid)["rule_source"]) == ("always", "words")


def test_4_words_become_rules_only_when_they_say_so() -> None:
    people = [("p1", "marta rossi"), ("p2", "nino")]
    assert rules.rule_from_words("Interrompimi sempre per Marta", people) == rules.Rule(
        "always", None, "p1", "words"
    )
    assert rules.rule_from_words("Non disturbarmi mai per i rituali", people) == rules.Rule(
        "never", "ritual", None, "words"
    )
    assert rules.rule_from_words("always interrupt me for reminders", people) == rules.Rule(
        "always", "reminder", None, "words"
    )
    assert rules.rule_from_words("Mi piace la pizza", people) is None
    assert rules.rule_from_words("Interrompimi sempre", people) is None  # no who, no what
    assert rules.rule_from_words("Non disturbarmi per Giorgio", people) is None  # not one of their people
    person_kind = [rules.Rule("never", "person"), rules.Rule("always", None, "p1")]
    assert rules.rule_for(item("people.offer", person="p1"), person_kind) == rules.Rule("always", None, "p1")


def test_4_settings_refuse_a_rule_that_names_nothing() -> None:
    base = setting_rules.with_defaults(None, language="it-IT")
    bad_rules = [{"mode": "always"}, {"mode": "sometimes", "kind": "info"},
                 {"mode": "never", "kind": "digest"}, {"mode": "never", "person_id": "Marta"}]  # fmt: skip
    for bad in bad_rules:
        with pytest.raises(PisaError):
            setting_rules.apply_patch(base, {"guardian": {"rules": [bad]}})
    ok = setting_rules.apply_patch(base, {"guardian": {"rules": [{"mode": "never", "kind": "info"}]}})
    assert ok["guardian"]["rules"] == [{"mode": "never", "kind": "info"}]


# --- 5. the day's density -----------------------------------------------------------------------------------


def test_5_a_busy_block_raises_the_floor() -> None:
    a = rules.event(NOW - timedelta(minutes=30), NOW + timedelta(minutes=5), "Riunione", 2)
    b = rules.event(NOW + timedelta(minutes=12), NOW + timedelta(hours=1), "Call", 0)
    found = rules.busy([a, b], NOW)
    assert found == rules.Busy("back_to_back", NOW + timedelta(hours=1))
    assert rules.busy([a], NOW) == rules.Busy("meeting", a.end)  # One meeting raises it too
    far = rules.event(NOW + timedelta(minutes=30), NOW + timedelta(hours=1), "Call", 0)
    assert rules.busy([a, far], NOW) == rules.Busy("meeting", a.end)  # a real gap: this meeting only
    keynote = rules.event(NOW - timedelta(minutes=5), NOW + timedelta(minutes=40), "Keynote", 0)
    assert rules.busy([keynote], NOW) == rules.Busy("talk", NOW + timedelta(minutes=40))
    assert rules.busy([], NOW, driving=True).reason == "driving"  # type: ignore[union-attr]
    strong = item("attend.leave")  # a departure, no deadline yet: 30 + 15 + 25 = 70
    assert rules.attend(strong, rules.Moment(NOW)).decision == "now"
    busy = rules.attend(strong, rules.Moment(NOW, busy=found, until=found.until))  # type: ignore[arg-type]
    assert (busy.decision, busy.reason, busy.until) == ("breakpoint", "busy_back_to_back", found.until)  # type: ignore[union-attr]
    # a hold never passes the item's own deadline: an event inside the block goes now
    due = item("attend.reminder", deadline=NOW + timedelta(minutes=50))
    inside = rules.attend(due, rules.Moment(NOW, busy=found, until=found.until))  # type: ignore[union-attr]
    assert (inside.decision, inside.reason) == ("now", "deadline")
    # and lasts HOLD_MAX at most
    long = rules.Busy("talk", NOW + timedelta(hours=5))
    assert rules.attend(strong, rules.Moment(NOW, busy=long, until=long.until)).until == NOW + rules.HOLD_MAX


def test_5_driving_is_read_from_the_phone_s_fixes(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    for seconds, lat in ((120, 45.0), (60, 45.005), (0, 45.01)):  # ~555 m a minute: ~9 m/s
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
            "VALUES (%s, %s, 9.0, 10, %s)",
            (device, lat, NOW - timedelta(seconds=seconds)),
        )

    async def job(pool: Any) -> tuple[bool, bool]:
        async with pool.connection() as conn:
            return await guardian.motion(conn, NOW)

    assert run(db, job) == (True, True)


# --- 6. decay per kind --------------------------------------------------------------------------------------


def test_6_three_ignored_nudges_of_a_kind_halve_its_rate() -> None:
    assert rules.decay_level(["ignored", "dismissed"]) == 0
    assert rules.decay_level(["ignored"] * 3) == 1
    assert rules.decay_level(["ignored"] * 6 + ["replied"]) == 2
    assert rules.decay_level(["replied"] + ["ignored"] * 6) == 0  # their reply restores it
    assert rules.decay_level(["tapped", "ignored", "ignored", "ignored"]) == 0
    assert rules.decay_level(["ignored"] * 30) == rules.DECAY_MAX
    it = item("attend.replan")  # Pisa's own nudge: 20 + 5 + 15, a breakpoint's
    assert rules.attend(it, rules.Moment(NOW, decay=1, decayed_since_pass=0)).reason == "decay_1"
    assert rules.attend(it, rules.Moment(NOW, decay=1, decayed_since_pass=1)).decision == "breakpoint"
    assert rules.attend(it, rules.Moment(NOW, decay=2, decayed_since_pass=2)).reason == "decay_2"


def test_6_decay_is_read_from_the_record_and_a_reply_restores_it(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    for n in range(3):
        db.execute(
            "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, decided_at, "
            "released_at, outcome, outcome_at) VALUES (%s, %s, 'attend.replan', 'replan', 'breakpoint', "
            "'score:idle', '{}', %s, %s, 'ignored', %s)",
            (uuid.uuid4(), f"old:{n}", NOW - timedelta(hours=5 - n), NOW - timedelta(hours=5 - n),
             NOW - timedelta(hours=4 - n)),
        )  # fmt: skip
    first, second = (notice(db, "attend.replan", kind="replan") for _ in range(2))
    assert deliver(db, first, device)[0] is None and item_row(db, first)["reason"] == "decay_1"
    assert deliver(db, second, device)[0] == "ws"  # half the rate: every other one goes
    db.execute(
        "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, decided_at, released_at, "
        "outcome, outcome_at) VALUES (%s, 'old:r', 'attend.replan', 'replan', 'now', 'score', '{}', %s, %s, "
        "'replied', %s)",
        (uuid.uuid4(), NOW, NOW, NOW),
    )
    third = notice(db, "attend.replan", kind="replan")
    assert deliver(db, third, device)[0] == "ws" and item_row(db, third)["reason"] == "score:idle"


def test_6_outcomes_are_read_back_for_the_decay(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    swiped, answered, quiet = (notice(db, "attend.replan", kind="replan") for _ in range(3))
    for nid in (swiped, answered, quiet):
        assert deliver(db, nid, device)[0] == "ws"
    db.execute(
        "INSERT INTO device_app_events (id, device_id, event, notification_id, at, received_at) "
        "VALUES (%s, %s, 'notification_dismissed', %s, %s, %s)",
        (uuid.uuid4(), device, swiped, NOW, NOW),
    )
    conversation = uuid.uuid4()
    db.execute(
        "INSERT INTO conversations (id, device_id, started_at) VALUES (%s, %s, %s)",
        (conversation, device, NOW),
    )
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) "
        "VALUES (%s, 'user', 'ok', 'it-IT', %s)",
        (conversation, NOW + timedelta(minutes=2)),
    )
    db.execute("UPDATE notifications SET device_id = %s WHERE id = %s", (device, answered))
    db.execute("UPDATE notifications SET delivered_at = %s WHERE id = %s", (NOW - timedelta(hours=1), quiet))
    db.execute("UPDATE notifications SET delivered_at = %s WHERE id = %s", (NOW - timedelta(hours=1), swiped))
    tick(db, NOW + timedelta(minutes=10))
    outcomes = dict(db.execute("SELECT ref, outcome FROM guardian_items").fetchall())
    assert outcomes[f"notification:{swiped}"] == "dismissed"
    assert outcomes[f"notification:{answered}"] == "replied"
    assert outcomes[f"notification:{quiet}"] == "ignored"


# --- 7. the record ------------------------------------------------------------------------------------------


def test_7_the_record_keeps_ids_kinds_numbers_and_reasons_never_the_words(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    nid = notice(db, "attend.replan", kind="replan")
    deliver(db, nid, device)
    columns = {
        r[0]
        for r in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'guardian_items'"
        ).fetchall()
    }
    assert not columns & {"text", "summary", "words", "content", "title"}
    row = db.execute("SELECT row_to_json(g)::text FROM guardian_items g").fetchone()
    assert row is not None and "prova" not in row[0]


def test_7_neglect_and_the_sunday_card_s_line(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    digest_went(db, NOW - timedelta(hours=2))
    passed = notice(db, "attend.replan", kind="replan")
    deliver(db, passed, device)
    stuck = notice(db, "errands.combo")
    deliver(db, stuck, device)
    db.execute("UPDATE guardian_items SET decided_at = %s WHERE ref = %s",
               (NOW - timedelta(hours=25), f"notification:{stuck}"))  # fmt: skip
    tick(db, NOW + timedelta(minutes=1))
    assert db.execute("SELECT lapsed_at IS NOT NULL FROM guardian_items WHERE ref = %s",
                      (f"notification:{stuck}",)).fetchone() == (True,)  # fmt: skip

    async def job(pool: Any) -> tuple[int, int, int]:
        async with pool.connection() as conn:
            return await week(conn, NOW - timedelta(days=2), NOW + timedelta(days=1))

    assert run(db, job) == (1, 1, 1)
    assert rules.card_line(1, 1, 1, "it-IT") == "Interruzioni: trattenute 1 / passate 1; mai uscite 1."
    assert rules.card_line(14, 3, 0, "en-US") == "Interruptions: held 14 / passed 3."
    line = rules.card_line(14, 3, 0, "it-IT")
    sheet = ledger_rules.sheet(
        "it-IT",
        total=0,
        undone=0,
        top=[],
        undoable=[],
        proposals=[],
        subscriptions=[],
        interruptions_line=line,
    )
    assert {"type": "text", "text": "Interruzioni: trattenute 14 / passate 3."} in sheet["blocks"]


def test_7_the_sunday_card_carries_the_line(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome
    from tests.test_life_admin import WEEK, hdr

    clock.now = rome(2026, 10, 25, 18)  # Sunday of 2026-W43
    at = rome(2026, 10, 21, 10)
    for n, (decision, released) in enumerate(
        (("now", at), ("breakpoint", at + timedelta(hours=1)), ("digest", None))
    ):
        db.execute(
            "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, decided_at, "
            "released_at) VALUES (%s, %s, 'attend.replan', 'replan', %s, 'score', '{}', %s, %s)",
            (uuid.uuid4(), f"t:{n}", decision, at, released),
        )
    summary = client.get(f"/v1/ledger/summary?week={WEEK}", headers=hdr(db)).json()
    assert summary["interruptions"] == {"held": 2, "passed": 1, "never_out": 0, "holding": 1}
    texts = [b.get("text", "") for b in summary["sheet"]["blocks"]]
    assert "Interruzioni: trattenute 2 / passate 1." in texts


# --- 8. measures in the user report ------------------------------------------------------------------------


def test_8_the_owner_report_measures_dismissals_holds_and_his_overrides(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome
    from tests.test_life_admin import WEEK

    device, _ = pair(db)
    at = rome(2026, 10, 20, 10)
    hour = timedelta(hours=1)
    rows = [  # (decision, reason, override, released after, lapsed): the first went at once, three waited
        ("now", "his_rule_always", "always", timedelta(0), False),
        ("breakpoint", "score", None, hour, False),
        ("digest", "low_score", None, None, True),
        ("digest", "his_rule_never", "never", hour, False),
    ]
    for n, (decision, reason, override, after, lapsed) in enumerate(rows):
        db.execute(
            "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, override, decided_at, "
            "released_at, lapsed_at) VALUES (%s, %s, 'errands.combo', 'info', %s, %s, '{}', %s, %s, %s, %s)",
            (uuid.uuid4(), f"m:{n}", decision, reason, override, at,
             at + after if after is not None else None, at if lapsed else None),
        )  # fmt: skip
    for _ in range(7):
        db.execute(
            "INSERT INTO device_app_events (id, device_id, event, notification_id, at, received_at) "
            "VALUES (%s, %s, 'notification_dismissed', %s, %s, %s)",
            (uuid.uuid4(), device, uuid.uuid4(), at, at),
        )
    report = client.portal.call(client.app.state.runtime.autopilot.owner_loop.weekly, WEEK)
    assert report["interruptions"] == {
        "items": 4, "passed": 1, "held": 3, "released": 2, "never_out": 1, "held_to_released": 1.5,
        "overrides": {"always": 1, "never": 1}, "dismissals_per_day": 3.5, "never_out_by_kind": {"info": 1},
    }  # fmt: skip
    from pisa.domain import owner_loop

    facts = {
        b["label"]: b["value"] for b in owner_loop.sheet(report, "en-US")["blocks"] if b["type"] == "fact"
    }
    # the week so far: Monday and Tuesday (the client's clock), 7 swipes
    assert facts["Dismissals a day"] == "3.5" and facts["Held / released"] == "3/2 (1.5)"
    assert facts["Your rules: always / never"] == "1 / 1"


def test_8_measures_read_counted_items_only(db) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, counted, decided_at) "
        "VALUES (%s, 'x', 'talk.last_words', 'info', 'now', 'his_words', '{}', false, %s)",
        (uuid.uuid4(), NOW),
    )

    async def job(pool: Any) -> dict[str, Any]:
        async with pool.connection() as conn:
            return await measures(conn, NOW - timedelta(days=1), NOW + timedelta(days=1))

    assert run(db, job)["items"] == 0


def test_digest_slots_are_his_local_time() -> None:
    assert rules.next_digest(NOW, TZ) == datetime(2026, 11, 10, 12, 30, tzinfo=rules.ZoneInfo(TZ))
    late = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)  # 22:00 in Rome
    assert rules.next_digest(late, TZ) == datetime(2026, 11, 11, 8, 30, tzinfo=rules.ZoneInfo(TZ))
    early = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)  # 07:00
    assert rules.last_digest_slot(early, TZ) == datetime(2026, 11, 9, 21, 30, tzinfo=rules.ZoneInfo(TZ))


def test_the_guardian_minute_is_pisa_s_initiative() -> None:
    from pisa.services.worker_steps import INITIATIVE_STEPS, MINUTE_GROUPS

    assert "guardian" in INITIATIVE_STEPS
    assert "guardian" in MINUTE_GROUPS["on_time"]


# --- round 2 (the review) -----------------------------------------------------------------------


def _ago(db: psycopg.Connection, nid: uuid.UUID, minutes: int) -> None:
    db.execute(
        "UPDATE notifications SET delivered_at = %s WHERE id = %s", (NOW - timedelta(minutes=minutes), nid)
    )


def test_decay_thins_only_pisa_s_own_nudges() -> None:
    """His reminder, his briefing, what he scheduled, and anything with a deadline never decay."""
    tired = rules.Moment(NOW, decay=3, decayed_since_pass=0)
    for his in ("attend.reminder", "briefing", "wishes.scheduled"):
        assert not rules.attend(item(his), tired).reason.startswith("decay"), his
    due = item("attend.replan", deadline=NOW + timedelta(hours=3))
    assert not rules.attend(due, tired).reason.startswith("decay")
    assert rules.attend(item("attend.replan"), tired).reason == "decay_3"


def test_a_notice_of_his_is_never_counted_ignored(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    his, mine = notice(db, "attend.reminder", kind="reminder"), notice(db, "attend.replan", kind="replan")
    for nid in (his, mine):
        assert deliver(db, nid, device)[0] == "ws"
        _ago(db, nid, 60)
    tick(db, NOW + timedelta(minutes=1))
    outcomes = dict(db.execute("SELECT ref, outcome FROM guardian_items").fetchall())
    assert outcomes[f"notification:{his}"] is None  # a reminder needs no reply: no "ignored"
    assert outcomes[f"notification:{mine}"] == "ignored"


@pytest.mark.parametrize(
    "said",
    [
        "Avvisami per i treni, non farmeli perdere",
        "Non dimenticare di avvisarmi per Marta",
        "Avvisa di Nino, no?",
    ],
)
def test_a_negation_elsewhere_is_no_never(said: str) -> None:
    rule = rules.rule_from_words(said, [("p1", "marta rossi"), ("p2", "nino")])
    assert rule is None or rule.mode != "never"


def test_the_negated_verb_is_a_never_and_treno_is_a_departure() -> None:
    people = [("p1", "marta rossi")]
    assert rules.rule_from_words("Non mi avvisare per il treno", people) == rules.Rule(
        "never", "leave", None, "words"
    )
    assert rules.rule_from_words("don't bother me about reminders", people) == rules.Rule(
        "never", "reminder", None, "words"
    )


def test_his_never_over_a_floor_shows_it_silently(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": "leave"}]}})
    nid = notice(db, "attend.leave", kind="reminder", urgency="now", event_at=NOW + timedelta(minutes=8))
    via, live = deliver(db, nid, device)
    assert via == "ws" and live.sent[0][1].voice is False  # shown, never said
    assert item_row(db, nid)["reason"] == "his_rule_never_floor"


def test_a_passive_push_has_no_sound() -> None:
    from pisa.connectors.apns import ApnsClient, ApnsKey
    from tests.test_push import KEY_ID, TEAM_ID, TOKEN, TOPIC, Apple, p8

    apple = Apple()

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(apple)) as http:
            client = ApnsClient(http, ApnsKey(KEY_ID, TEAM_ID, p8()[0]), topic=TOPIC)
            await client.send(TOKEN, "development", title="Pisa", body="x", thread_id="reminder", data={},
                              push_id="p-1", interruption="passive")  # fmt: skip

    asyncio.run(go())
    import json

    aps = json.loads(apple.requests[0].content)["aps"]
    assert aps["interruption-level"] == "passive" and "sound" not in aps


def test_an_item_due_before_the_digest_is_never_digested() -> None:
    moment = rules.Moment(
        NOW, digest_at=NOW + timedelta(hours=6), breakpoint=None, until=NOW + timedelta(hours=1)
    )
    low = item("errands.combo", deadline=NOW + timedelta(hours=5))  # 10 + 5 + 5 = 20: a digest's score
    verdict = rules.attend(low, moment)
    assert verdict.decision != "digest"
    never = rules.Moment(NOW, rules.settings_rules({"rules": [{"mode": "never", "kind": "info"}]}),
                         digest_at=NOW + timedelta(hours=6))  # fmt: skip
    assert rules.attend(low, never).decision != "digest"
    assert rules.attend(item("errands.combo", deadline=NOW + timedelta(days=1)), moment).decision == "digest"


def _block(db: psycopg.Connection) -> None:
    """Back-to-back meetings from 11:00 to 16:00 in Rome (NOW is 11:00)."""
    meetings(db, (NOW, NOW + timedelta(hours=2, minutes=30)),
             (NOW + timedelta(hours=2, minutes=35), NOW + timedelta(hours=5)))  # fmt: skip


def test_a_two_hour_hold_with_a_deadline_before_the_slot_goes_now(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    _block(db)
    nid = notice(db, "attend.replan", kind="replan", event_at=NOW + timedelta(hours=4))  # 15:00 in Rome
    assert deliver(db, nid, device)[0] is None
    assert db.execute("SELECT not_before FROM notifications").fetchone() == (NOW + rules.HOLD_MAX,)
    later = NOW + rules.HOLD_MAX + timedelta(minutes=1)  # 13:01, still in the block; the slot is 18:30
    assert tick(db, later) == 1
    row = db.execute("SELECT guardian FROM notifications").fetchone()
    assert row == ("released",) and item_row(db, nid)["released_how"] == "hold_max_deadline"


def test_the_tick_releases_a_hold_before_its_deadline(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    _block(db)
    due = NOW + timedelta(hours=4)
    nid = notice(db, "attend.replan", kind="replan", event_at=due)
    deliver(db, nid, device)
    db.execute("UPDATE guardian_items SET decided_at = %s", (due - timedelta(minutes=30),))  # no HOLD_MAX yet
    assert tick(db, due - timedelta(minutes=10)) == 0
    assert tick(db, due - timedelta(minutes=4)) == 1
    assert item_row(db, nid)["released_how"] == "deadline"


def test_the_digest_drops_an_item_whose_event_is_over(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    digest_went(db, NOW - timedelta(hours=2))
    nid = notice(db, "errands.combo")
    deliver(db, nid, device)
    db.execute("UPDATE notifications SET event_at = %s", (NOW + timedelta(minutes=30),))  # "Tra 15 minuti…"
    at = datetime(2026, 11, 10, 11, 31, tzinfo=UTC)  # 12:31: the event was at 11:30
    tick(db, at)
    assert db.execute("SELECT count(*) FROM notifications WHERE path = 'guardian.digest'").fetchone() == (0,)
    assert db.execute("SELECT lapsed_at IS NOT NULL, released_at FROM guardian_items").fetchone() == (
        True,
        None,
    )


def test_a_held_approval_is_pushed_again_at_the_next_breakpoint(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    runtime = client.app.state.runtime
    live = Live({device})
    runtime.approvals._live = lambda: live
    meetings(
        db, (clock.now - timedelta(minutes=30), clock.now + timedelta(minutes=4))
    )  # ends before the lapse
    held = client.portal.call(
        lambda: runtime.approvals.create("calendar_create", "Sposto la palestra", ["x"], {"x": 1})
    )
    assert live.approvals == [] and item_row_ref(db, f"approval:{held['id']}")[0] is None
    clock.now += timedelta(minutes=5)  # the meeting is over, the approval still pending
    client.portal.call(runtime.guardian.tick)
    assert [a.id for _, a in live.approvals] == [held["id"]]
    assert item_row_ref(db, f"approval:{held['id']}")[0] is not None


def item_row_ref(db: psycopg.Connection, ref: str) -> tuple[Any, ...]:
    row = db.execute("SELECT released_at, reason FROM guardian_items WHERE ref = %s", (ref,)).fetchone()
    assert row is not None
    return row


def test_an_approval_made_in_his_turn_is_his_words(client, db) -> None:  # type: ignore[no-untyped-def]
    pair(db)
    approvals = client.app.state.runtime.approvals
    set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": "approval"}]}})
    made = client.portal.call(
        lambda: approvals.create("calendar_create", "Sposto la palestra", ["x"], {"x": 1}, asked=True)
    )
    assert item_row_ref(db, f"approval:{made['id']}")[1] == "his_words"
    source = (PISA / "services/actions.py").read_text()
    assert source.count("asked=True") == 3  # the turn's three approvals (the plan's two, a proposal)


def test_one_meeting_holds_a_high_score_only_floors_and_his_always_pierce(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    meetings(db, (NOW - timedelta(minutes=10), NOW + timedelta(minutes=50)))
    strong = notice(db, "attend.reminder", kind="reminder", event_at=NOW + timedelta(hours=1, minutes=30))
    assert deliver(db, strong, device)[0] is None  # 30 + 15 + 10 = 55: held in the meeting
    assert item_row(db, strong)["reason"] == "busy_meeting"
    cap = notice(db, "places.cap", kind="alert")  # 35 + 5 + 25 = 65: under the meeting's 80
    assert deliver(db, cap, device)[0] is None
    # a high score (80) due after the meeting: "now" for the gate, still held by the meeting (rule 30)
    high = notice(db, "attend.leave", kind="reminder", event_at=NOW + timedelta(hours=1, minutes=10))
    assert deliver(db, high, device)[0] is None
    assert item_row(db, high)["decision"] == "now"
    held_until = db.execute("SELECT not_before FROM notifications WHERE id = %s", (high,)).fetchone()
    assert held_until == (NOW + timedelta(minutes=50),)
    floor = notice(db, "attend.leave", kind="reminder", urgency="now", event_at=NOW + timedelta(minutes=8))
    assert deliver(db, floor, device)[0] == "ws"


def test_a_cap_alert_goes_now_when_he_is_free(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    nid = notice(db, "places.cap", kind="alert")
    assert deliver(db, nid, device)[0] == "ws" and item_row(db, nid)["decision"] == "now"


def test_a_must_show_mail_is_never_digested(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    plain_mail = notice(db, "attend.intake:outside")
    shown = notice(db, "attend.intake:outside:shown")
    assert deliver(db, plain_mail, device)[0] is None and item_row(db, plain_mail)["decision"] == "digest"
    assert deliver(db, shown, device)[0] == "ws" and item_row(db, shown)["decision"] == "breakpoint"


def _marta(db: psycopg.Connection) -> uuid.UUID:
    marta = uuid.uuid4()
    db.execute(
        "INSERT INTO people (id, name, name_key, source, emails, created_at, updated_at) "
        "VALUES (%s, 'Marta Rossi', 'marta rossi', 'owner', '{marta@example.test}', now(), now())",
        (marta,),
    )
    return marta


def test_his_people_are_found_on_mail_and_on_events(db) -> None:  # type: ignore[no-untyped-def]
    marta = _marta(db)
    meetings(db, (NOW + timedelta(hours=1), NOW + timedelta(hours=2)))
    db.execute(
        "UPDATE calendar_events SET guests = %s", (Jsonb([{"email": "Marta@Example.test", "name": "M"}]),)
    )

    async def job(pool: Any) -> tuple[Any, Any, Any]:
        async with pool.connection() as conn:
            mail = await guardian.person_of(conn, ["marta@example.test"])
            nobody = await guardian.person_of(conn, ["nobody@example.test"])
            return mail, await guardian.event_person(conn, "m0"), nobody

    assert run(db, job) == (marta, marta, None)  # her mail, an event with her, a stranger
    his = [rules.Rule("always", None, str(marta))]
    assert guardian.always(his, "attend.intake:outside", str(marta))
    assert not guardian.always(his, "attend.intake:outside", None)


def test_attend_stamps_the_person_and_his_always_pierces_a_block(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.attend import Candidate
    from pisa.services.attend import deliver_now

    device, _ = pair(db)
    marta = _marta(db)
    _block(db)
    set_settings(db, {"guardian": {"rules": [{"mode": "always", "person_id": str(marta)}]}})

    async def job(pool: Any) -> uuid.UUID | None:
        async with pool.connection() as conn:
            c = Candidate("c1", "replan", "replan", "Spostato.", "it-IT", NOW)
            return await deliver_now(conn, c, {"person_id": str(marta)}, timezone=TZ)

    nid = run(db, job)
    assert nid is not None
    assert db.execute("SELECT person_id FROM notifications").fetchone() == (marta,)
    assert deliver(db, nid, device)[0] == "ws" and item_row(db, nid)["override"] == "always"


def test_quiet_hours_hold_every_release(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    end = NOW + timedelta(minutes=20)
    meetings(db, (NOW - timedelta(minutes=20), end))
    nid = notice(db, "attend.replan", kind="replan")
    assert deliver(db, nid, device)[0] is None
    later = end + timedelta(minutes=1)  # 11:21 in Rome: the meeting is over
    set_settings(db, {"quiet": {"hours": {"start": "11:00", "end": "12:00"}}})
    assert tick(db, later) == 0  # quiet: no breakpoint
    db.execute("UPDATE notifications SET event_at = %s", (later + timedelta(minutes=3),))
    assert tick(db, later) == 0  # nor a deadline release
    db.execute("UPDATE notifications SET event_at = NULL")
    assert db.execute("SELECT guardian FROM notifications").fetchone() == ("breakpoint",)
    assert deliver(db, nid, device, end + timedelta(minutes=30))[0] is None  # nor the re-check
    set_settings(db, {"quiet": {"hours": {"start": "22:30", "end": "07:00"}}})
    assert tick(db, later) == 1


def test_a_release_the_dispatcher_drops_is_neglect(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    end = NOW + timedelta(minutes=20)
    meetings(db, (NOW - timedelta(minutes=20), end))
    nid = notice(db, "attend.replan", kind="replan")
    deliver(db, nid, device)
    tick(db, end + timedelta(minutes=1))
    assert item_row(db, nid)["released_at"] is None  # let out, not yet delivered
    assert deliver(db, nid, device, end + timedelta(minutes=20))[0] == "none"  # stale: never sent
    row = db.execute("SELECT released_at, lapsed_at IS NOT NULL FROM guardian_items").fetchone()
    assert row == (None, True)


def test_the_re_check_marks_the_record_released(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    end = NOW + timedelta(minutes=20)
    meetings(db, (NOW - timedelta(minutes=20), end))
    nid = notice(db, "attend.replan", kind="replan")
    deliver(db, nid, device)
    assert deliver(db, nid, device, end + timedelta(minutes=1))[0] == "ws"  # its not_before came: re-gated
    row = item_row(db, nid)
    assert row["released_at"] == end + timedelta(minutes=1) and row["decision"] == "breakpoint"


def test_the_digest_waits_for_a_breakpoint(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    digest_went(db, NOW - timedelta(hours=2))
    nid = notice(db, "errands.combo")
    deliver(db, nid, device)
    at = datetime(2026, 11, 10, 11, 31, tzinfo=UTC)  # 12:31, the slot
    meetings(db, (at - timedelta(minutes=5), at + timedelta(minutes=30)))
    assert tick(db, at) == 0
    assert tick(db, at + timedelta(minutes=31)) == 1


def test_a_digest_is_never_counted_nor_folded(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    nid = notice(db, "guardian.digest")
    assert deliver(db, nid, device)[0] == "ws"
    assert item_row(db, nid)["counted"] is False
    assert rules.counted("score", "digest") is False


def test_neglect_counts_an_expired_approval(db) -> None:  # type: ignore[no-untyped-def]
    approval = uuid.uuid4()
    db.execute(
        "INSERT INTO approvals (id, kind, summary, details, payload, status, requires_biometric, lang, "
        "created_at, expires_at) VALUES (%s, 'calendar_create', 's', '[]', '{}', 'expired', false, 'it-IT', "
        "%s, %s)",
        (approval, NOW - timedelta(minutes=20), NOW - timedelta(minutes=10)),
    )
    db.execute(
        "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, decided_at) "
        "VALUES (%s, %s, 'approval.request', 'approval', 'breakpoint', 'busy_meeting', '{}', %s)",
        (uuid.uuid4(), f"approval:{approval}", NOW - timedelta(minutes=20)),
    )
    tick(db, NOW)
    assert db.execute("SELECT lapsed_at IS NOT NULL FROM guardian_items").fetchone() == (True,)


def test_his_never_over_an_item_due_before_the_digest_is_silent() -> None:
    never = rules.Moment(NOW, rules.settings_rules({"rules": [{"mode": "never", "kind": "approval"}]}),
                         digest_at=NOW + timedelta(hours=2))  # fmt: skip
    verdict = rules.attend(item("approval.request", deadline=NOW + timedelta(minutes=10)), never)
    assert (verdict.decision, verdict.reason, verdict.silent) == ("now", "his_rule_never_silent", True)


def test_his_always_beats_attend_s_drop(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """Mail from Marta that attend would drop (today's rules: an intake item not high is dropped) goes out
    when they said "always for Marta"."""
    from pisa.domain.attend import Candidate

    pair(db)
    runtime = client.app.state.runtime
    marta = _marta(db)

    def propose(key: str) -> None:
        async def go() -> None:
            async with runtime.pool.connection() as conn:
                c = Candidate(
                    str(uuid.uuid4()),
                    "intake",
                    "info",
                    "Marta ti ha scritto.",
                    "it-IT",
                    clock.now,
                    outside=True,
                )
                await runtime.attend.propose(conn, c, key, {"person_id": str(marta)})

        client.portal.call(go)

    propose("intake:a")
    clock.now += timedelta(minutes=3)  # past attend's batch window
    client.portal.call(runtime.attend.tick)
    decided = db.execute(
        "SELECT decision->>'choice' FROM attend_candidates WHERE key = 'intake:a'"
    ).fetchone()
    assert decided == ("drop",) and db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)
    set_settings(db, {"guardian": {"rules": [{"mode": "always", "person_id": str(marta)}]}})
    propose("intake:b")
    clock.now += timedelta(minutes=3)
    client.portal.call(runtime.attend.tick)
    assert db.execute("SELECT person_id, urgency FROM notifications").fetchall() == [(marta, "now")]


def test_a_reminder_for_an_event_with_her_carries_her(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.reminders import RemindersService

    marta = _marta(db)
    start = NOW + timedelta(minutes=15)  # the default 15-minute lead: due now
    meetings(db, (start, start + timedelta(hours=1)), title="Pranzo")
    db.execute(
        "UPDATE calendar_events SET guests = %s", (Jsonb([{"email": "marta@example.test", "name": "M"}]),)
    )
    run(db, lambda pool: RemindersService(pool, owner=owner_at(TZ), clock=Clock(NOW)).run())
    # An invite (others invited) is a stranger's title: outside text for the model
    assert db.execute("SELECT path, person_id FROM notifications").fetchall() == [
        ("attend.reminder:outside", marta)
    ]


def test_attend_stamps_a_must_show(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.attend import Candidate
    from pisa.services.attend import deliver_now

    async def job(pool: Any) -> uuid.UUID | None:
        async with pool.connection() as conn:
            c = Candidate(str(uuid.uuid4()), "intake", "info", "Mail urgente.", "it-IT", NOW, outside=True,
                          must_show=True)  # fmt: skip
            return await deliver_now(conn, c, {}, timezone=TZ)

    assert run(db, job) is not None
    assert db.execute("SELECT path FROM notifications").fetchone() == ("attend.intake:outside:shown",)


def test_mail_from_her_carries_her(db) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from pisa.brain.intake import Gist, Triage
    from pisa.domain.mail import Address
    from pisa.services.intake import IntakeService
    from tests.test_proactivity import T0, _attend

    marta = _marta(db)
    verdict = Triage(
        category="action_required", automated=False, asks_owner_to_act=True, already_done=False, reason="r",
        priority="mid", tell_owner=True, gist=Gist(what="Il libro", who="Marta", asked="rispondere"),
    )  # fmt: skip

    async def go() -> None:
        async with _attend(db, T0) as env:
            intake = IntakeService(env.pool, None, owner=owner_at(TZ), clock=env.clock, attend=env.attend)  # type: ignore[arg-type]
            tell: Any = intake._tell
            await tell(
                SimpleNamespace(id="m1", sender=Address("Marta", "Marta@Example.test")), verdict, None, T0
            )

    asyncio.run(go())
    assert db.execute("SELECT delivery->>'person_id' FROM attend_candidates").fetchone() == (str(marta),)


def test_a_first_decision_in_quiet_hours_is_not_held(db) -> None:  # type: ignore[no-untyped-def]
    """What reaches the dispatcher in quiet hours was let through by `enqueue` (urgent, or its sense checked
    quiet itself): quiet hours hold only a release or a re-check."""
    device, _ = pair(db)
    set_settings(db, {"quiet": {"hours": {"start": "10:00", "end": "12:00"}}})
    nid = notice(db, "attend.replan", kind="replan", respect_quiet=False)
    assert deliver(db, nid, device)[0] == "ws"


# --- the merge (round 2's review) ------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "Non interrompermi sempre per i rituali",
        "Mi interrompi sempre per i rituali, basta",
        "You always interrupt me for rituals, stop it",
        "Non disturbarmi tranne per Marta",
        "Non mi disturbare se non per i treni",
        "Se non mi avvisi per il treno lo perdo",
        "Non avvisarmi all'ultimo minuto per i treni",
        "Don't notify me late for trains",
        "Mai avvisarmi tardi per la partenza",
        "Avvisami sempre per Marta, non disturbarmi per i rituali",
    ],
)
def test_ma_a_sentence_whose_mode_is_unsure_makes_no_rule(said: str) -> None:
    """Review M-A: an exception, a condition, a complaint, a modifier on the moment, "sempre" under a
    negation, or both modes in one sentence: no rule (read the other way, each silenced or forced what they
    didn't mean)."""
    assert rules.rule_from_words(said, [("p1", "marta rossi")]) is None


def test_a_name_in_their_words_is_keyed_as_the_people_table_writes_it() -> None:
    """The reader's key is the writer's: "D'Amico" and "Jean-Luc" are stored as "d amico" and "jean luc"
    (services/people writes `people_name_key`), so the rule finds them (it used to key them "d'amico" and
    "jean-luc" and find no one)."""
    people = [("p1", people_name_key("D'Amico")), ("p2", people_name_key("Jean-Luc"))]
    assert rules.rule_from_words("Interrompimi sempre per D'Amico", people) == rules.Rule(
        "always", None, "p1", "words"
    )
    assert rules.rule_from_words("Interrompimi sempre per Jean-Luc", people) == rules.Rule(
        "always", None, "p2", "words"
    )


def test_ma_the_plain_rules_still_read() -> None:
    people = [("p1", "marta rossi")]
    assert rules.rule_from_words("Interrompimi sempre per Marta", people) == rules.Rule(
        "always", None, "p1", "words"
    )
    assert rules.rule_from_words("Non disturbarmi mai per i rituali", people) == rules.Rule(
        "never", "ritual", None, "words"
    )
    assert rules.rule_from_words("Never wake me for trains", people) == rules.Rule(
        "never", "leave", None, "words"
    )


def test_mb_his_never_in_the_digest_goes_before_its_deadline_when_the_digest_waits(db) -> None:  # type: ignore[no-untyped-def]
    """Review M-B: his "never" for reminders keeps one for a 12:40 event for the 12:30 digest; they are in a
    meeting from 12:15 to 13:00, so the digest waits. At 12:36 it goes, shown silently, never lapsed
    unseen."""
    device, _ = pair(db)
    digest_went(db, NOW - timedelta(hours=2))
    set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": "reminder"}]}})
    event = datetime(2026, 11, 10, 11, 40, tzinfo=UTC)  # 12:40 in Rome
    nid = notice(db, "attend.reminder", kind="reminder", event_at=event)
    assert deliver(db, nid, device)[0] is None and item_row(db, nid)["decision"] == "digest"
    meetings(db, (datetime(2026, 11, 10, 11, 15, tzinfo=UTC), datetime(2026, 11, 10, 12, 0, tzinfo=UTC)))
    tick(db, datetime(2026, 11, 10, 11, 36, tzinfo=UTC))
    assert db.execute("SELECT guardian, silent FROM notifications WHERE id = %s", (nid,)).fetchone() == (
        "released",
        True,
    )


def test_quiet_hours_hold_a_deadline_on_the_re_check_too(db) -> None:  # type: ignore[no-untyped-def]
    """Review L-1: the tick holds a deadline in quiet hours; the dispatcher's re-check now does the same."""
    device, _ = pair(db)
    end = NOW + timedelta(minutes=20)
    meetings(db, (NOW - timedelta(minutes=20), end))
    nid = notice(db, "attend.replan", kind="replan")
    assert deliver(db, nid, device)[0] is None
    later = end + timedelta(minutes=1)
    set_settings(db, {"quiet": {"hours": {"start": "11:00", "end": "12:00"}}})
    db.execute("UPDATE notifications SET event_at = %s", (later + timedelta(minutes=3),))
    assert deliver(db, nid, device, later)[0] is None
    assert db.execute("SELECT guardian FROM notifications").fetchone() == ("breakpoint",)


def test_an_approval_his_never_keeps_from_a_digest_is_pushed_silently(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """Review L-3: his "never" for approvals, at 12:25 with the 12:30 digest next and the approval lapsing
    after it: the digest carries notices only, so it was never pushed. Now it is shown at once, silently."""
    device, _ = pair(db)
    approvals = client.app.state.runtime.approvals
    live = Live({device})
    approvals._live = lambda: live
    clock.now = datetime(2026, 11, 10, 11, 25, tzinfo=UTC)
    set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": "approval"}]}})
    made = client.portal.call(
        lambda: approvals.create("calendar_create", "Sposto il pranzo", ["x"], {"x": 3}, origin="proposal")
    )
    assert [a.id for _, a in live.approvals] == [made["id"]]


def test_a_handoff_s_person_is_found_by_name(db) -> None:  # type: ignore[no-untyped-def]
    """Review M4: "Com'è andata la chiamata a Marta?" carries Marta (her first name or her whole one)."""
    marta = _marta(db)

    async def job(pool: Any) -> tuple[Any, Any, Any]:
        async with pool.connection() as conn:
            return (
                await guardian.person_named(conn, "Marta"),
                await guardian.person_named(conn, "Marta Rossi"),
                await guardian.person_named(conn, "Nino"),
            )

    assert run(db, job) == (marta, marta, None)


def test_a_handoff_s_person_is_found_by_the_key_the_people_table_wrote(db) -> None:  # type: ignore[no-untyped-def]
    """The reader keys a name as the writer does: "Jean-Luc D'Amico" is stored as "jean luc d amico", and the
    handoff's "Jean-Luc D'Amico" finds him (the reader used to key it "jean-luc d'amico" and find no one)."""
    person = uuid.uuid4()
    db.execute(
        "INSERT INTO people (id, name, name_key, source, emails, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'owner', '{}', now(), now())",
        (person, "Jean-Luc D'Amico", people_name_key("Jean-Luc D'Amico")),
    )

    async def job(pool: Any) -> tuple[Any, Any]:
        async with pool.connection() as conn:
            whole = await guardian.person_named(conn, "Jean-Luc D'Amico")
            return whole, await guardian.person_named(conn, "Jean")

    assert run(db, job) == (person, person)
