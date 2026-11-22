"""The phone's breakpoints reach the guardian. A call, the motion state
and a Focus make Pisa busy and their ends are breakpoints, read by each event's own time; a notice opened
counts as a tap; each family of app events has its own day; what Pisa holds now has a light endpoint; the
rules they said in words are listed. Every word here is synthetic."""

import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from pisa.api.schemas import AppEvent
from pisa.domain import app_events as app_rules
from pisa.domain import guardian as rules
from pisa.services import guardian
from tests.assistant_env import set_settings
from tests.conftest import pair
from tests.owner import owner_at
from tests.test_guardian import NOW, TZ, deliver, item, item_row, meetings, notice, run, tick

NEW = ("call_started", "call_ended", "activity", "focus_on", "focus_off", "notification_opened")


def said(
    db: psycopg.Connection, device: str, event: str, at: datetime, *, site: str | None = None,
    state: str | None = None, received: datetime = NOW, notification_id: uuid.UUID | None = None,
) -> None:  # fmt: skip
    db.execute(
        "INSERT INTO device_app_events (id, device_id, event, site, state, at, received_at, notification_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), device, event, site, state, at, received, notification_id),
    )


def moment(db: psycopg.Connection, now: datetime = NOW) -> rules.Moment:
    async def job(pool: Any) -> rules.Moment:
        async with pool.connection() as conn:
            return await guardian.moment_for(conn, "info", now, owner_at(TZ))

    return run(db, job)


def ev(event: str, minutes_ago: float, site: str | None = None, state: str | None = None) -> rules.PhoneEvent:
    return rules.PhoneEvent(event, site, state, NOW - timedelta(minutes=minutes_ago))


# --- 1. the event names -------------------------------------------------------------------------------------


def test_1_the_new_names_are_taken_everywhere() -> None:
    assert set(NEW) <= app_rules.EVENTS
    assert set(AppEvent.model_fields["event"].annotation.__args__) == app_rules.EVENTS  # type: ignore[union-attr]
    at = NOW.isoformat()
    tap = str(uuid.uuid4())
    kept = app_rules.sanitize([
        {"event": "call_started", "site": "CallKit", "state": "on", "background": True, "at": at},
        {"event": "activity", "site": "motion.walking", "state": "stationary", "background": False, "at": at},
        {"event": "focus_off", "site": "Focus", "state": "off", "at": at},
        {"event": "notification_opened", "site": "push", "state": "opened", "notification_id": tap, "at": at},
        {"event": "notification_opened", "site": "push", "state": "opened", "at": at},  # no notice: dropped
    ])  # fmt: skip
    assert [k["event"] for k in kept] == ["call_started", "activity", "focus_off", "notification_opened"]
    assert (kept[1]["site"], kept[1]["state"]) == ("motion.walking", "stationary")
    assert kept[3]["notification_id"] == tap and kept[0]["notification_id"] is None


def test_1_the_upload_stores_each_new_name(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    at = "2026-10-20T12:58:00+02:00"
    nid = str(uuid.uuid4())
    events = [
        {"event": name, "site": "CallKit", "state": "on", "at": at}
        | ({"notification_id": nid} if name == "notification_opened" else {})
        for name in NEW
    ]
    response = client.post(
        "/v1/devices/me/diagnostics", headers={"Authorization": f"Bearer {token}"}, json={"appEvents": events}
    )
    assert response.status_code == 204
    stored = {r[0] for r in db.execute("SELECT event FROM device_app_events").fetchall()}
    assert stored == set(NEW)


def test_1_a_notice_opened_is_not_a_dismissal_for_attend(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    nid = uuid.uuid4()
    candidate = uuid.uuid4()
    db.execute(
        "INSERT INTO attend_candidates (id, key, source, kind, text, lang, urgency, created_at, decided_at, "
        "decision, notification_id) VALUES (%s, 'r', 'reminder', 'reminder', 'Una prova.', 'it-IT', "
        "'breakpoint', now(), now(), '{}', %s)",
        (candidate, nid),
    )
    tap = {"event": "notification_opened", "site": "push", "state": "opened", "notification_id": str(nid),
           "at": "2026-10-20T12:58:00+02:00"}  # fmt: skip
    client.post("/v1/devices/me/diagnostics", headers={"Authorization": f"Bearer {token}"},
                json={"appEvents": [tap]})  # fmt: skip
    assert db.execute("SELECT outcome FROM attend_candidates WHERE id = %s", (candidate,)).fetchone() == (
        None,
    )


# --- 2. reading them: busy, expiry, breakpoints -----------------------------------------------------------


def test_2_a_call_is_busy_until_it_ends_and_its_end_is_a_breakpoint() -> None:
    on = rules.phone_state([ev("call_started", 10, "CallKit", "on")], NOW)
    assert on.call == NOW - timedelta(minutes=10)
    found = rules.busy([], NOW, phone=on)
    assert found == rules.Busy("call", NOW + rules.RECHECK)
    assert rules.at_breakpoint([], NOW, phone=on) is None  # no breakpoint on a call
    off = rules.phone_state([ev("call_started", 10), ev("call_ended", 2)], NOW)
    assert off.call is None and rules.busy([], NOW, phone=off) is None
    assert rules.at_breakpoint([], NOW, phone=off) == "call_end"


def test_2_driving_from_the_phone_s_motion_until_another_state() -> None:
    drive = rules.phone_state([ev("activity", 30, "motion.stationary", "automotive")], NOW)
    assert rules.busy([], NOW, phone=drive) == rules.Busy("driving", NOW + rules.RECHECK)
    assert rules.at_breakpoint([], NOW, phone=drive) is None
    parked = rules.phone_state(
        [
            ev("activity", 30, "motion.stationary", "automotive"),
            ev("activity", 1, "motion.automotive", "walking"),
        ],
        NOW,
    )
    assert parked.driving is None and rules.busy([], NOW, phone=parked) is None


def test_2_focus_counts_as_busy_but_his_always_and_the_floors_go() -> None:
    focus = rules.phone_state([ev("focus_on", 20, "Focus", "on")], NOW)
    found = rules.busy([], NOW, phone=focus)
    assert found == rules.Busy("focus", NOW + rules.RECHECK)
    assert rules.phone_state([ev("focus_on", 20), ev("focus_off", 5)], NOW).focus is None
    assert found is not None
    m = rules.Moment(NOW, busy=found, until=found.until)
    cap = item("places.cap")  # 65: now when they are free
    held = rules.attend(cap, m)
    assert (held.decision, held.reason, held.released) == ("breakpoint", "busy_focus", False)
    assert rules.attend(cap, rules.Moment(NOW)).decision == "now"
    his = rules.attend(cap, rules.Moment(NOW, rules=[rules.Rule("always", "alert")], busy=found))
    assert his.decision == "now"
    leave = item("attend.leave", floor=True)
    assert rules.attend(leave, m).decision == "now"


def test_2_every_busy_state_expires_when_not_confirmed() -> None:
    late = rules.HOLD_MAX + timedelta(minutes=1)
    old = [
        rules.PhoneEvent("call_started", "CallKit", "on", NOW - late),
        rules.PhoneEvent("activity", "motion.walking", "automotive", NOW - late),
        rules.PhoneEvent("focus_on", "Focus", "on", NOW - late),
    ]
    gone = rules.phone_state(old, NOW)
    assert (gone.call, gone.driving, gone.focus) == (None, None, None)
    # inside the window, its re-check never passes its own expiry
    near = rules.phone_state([ev("focus_on", 115)], NOW)
    assert rules.busy([], NOW, phone=near) == rules.Busy("focus", NOW + timedelta(minutes=5))
    # a later start confirms it
    again = rules.phone_state([rules.PhoneEvent("focus_on", None, None, NOW - late), ev("focus_on", 10)], NOW)
    assert again.focus == NOW - timedelta(minutes=10)


def test_2_a_walk_that_ended_is_a_breakpoint_only_when_just_ended() -> None:
    walked = rules.phone_state([ev("activity", 3, "motion.walking", "stationary")], NOW)
    assert rules.at_breakpoint([], NOW, phone=walked) == "walk_end"
    late = rules.phone_state([ev("activity", 20, "motion.walking", "stationary")], NOW)
    assert rules.at_breakpoint([], NOW, phone=late) == "idle"
    from_car = rules.phone_state([ev("activity", 3, "motion.automotive", "stationary")], NOW)
    assert rules.at_breakpoint([], NOW, phone=from_car) == "idle"
    unknown = rules.phone_state(
        [
            ev("activity", 9, "motion.stationary", "walking"),
            ev("activity", 3, "motion.unknown", "stationary"),
        ],
        NOW,
    )
    assert unknown.ended == "walk_end"
    old_call = rules.phone_state([ev("call_ended", 20)], NOW)
    assert rules.at_breakpoint([], NOW, phone=old_call) == "idle"


def test_2_the_moment_reads_the_events_by_their_own_time(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    # uploaded late, out of order: the call ended (at 09:40) arrives after a newer call began (09:50)
    said(db, device, "call_started", NOW - timedelta(minutes=10), site="CallKit", state="on",
         received=NOW - timedelta(minutes=9))  # fmt: skip
    said(db, device, "call_ended", NOW - timedelta(minutes=20), site="CallKit", state="off", received=NOW)
    m = moment(db)
    assert m.busy is not None and m.busy.reason == "call" and m.breakpoint is None
    db.execute("DELETE FROM device_app_events")
    # a call that ended 20 minutes ago, uploaded only now: no breakpoint of its own
    said(db, device, "call_ended", NOW - timedelta(minutes=20), received=NOW)
    assert moment(db).breakpoint == "idle"
    said(db, device, "call_ended", NOW - timedelta(minutes=1), received=NOW - timedelta(hours=3))
    assert moment(db).breakpoint == "call_end"
    # an event stamped after now is not yet
    db.execute("DELETE FROM device_app_events")
    said(db, device, "focus_on", NOW + timedelta(minutes=5))
    assert moment(db).busy is None


def test_2_motion_reads_the_phone_s_driving(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    said(db, device, "activity", NOW - timedelta(minutes=30), site="motion.walking", state="automotive")

    async def job(pool: Any) -> tuple[bool, bool]:
        async with pool.connection() as conn:
            return await guardian.motion(conn, NOW)

    assert run(db, job) == (True, True)
    m = moment(db)
    assert m.busy is not None and m.busy.reason == "driving" and m.breakpoint is None


def test_2_the_gate_holds_through_a_focus_and_his_always_goes(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    said(db, device, "focus_on", NOW - timedelta(minutes=20), site="Focus", state="on")
    held = notice(db, "places.cap", kind="alert")
    assert deliver(db, held, device)[0] is None
    assert (item_row(db, held)["decision"], item_row(db, held)["reason"]) == ("breakpoint", "busy_focus")
    set_settings(db, {"guardian": {"rules": [{"mode": "always", "kind": "alert"}]}})
    his = notice(db, "places.cap", kind="alert")
    assert deliver(db, his, device)[0] == "ws"


def test_2_a_call_s_end_releases_what_it_held(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    said(db, device, "call_started", NOW - timedelta(minutes=5), site="CallKit", state="on")
    nid = notice(db, "places.cap", kind="alert")
    assert deliver(db, nid, device)[0] is None
    assert item_row(db, nid)["reason"] == "busy_call"
    later = NOW + timedelta(minutes=3)
    assert tick(db, later) == 0  # still on the call
    said(db, device, "call_ended", NOW + timedelta(minutes=4), site="CallKit", state="off")
    assert tick(db, NOW + timedelta(minutes=5)) == 1
    assert item_row(db, nid)["released_how"] == "breakpoint:call_end"


def test_2_a_meeting_s_end_still_names_the_breakpoint_first() -> None:
    meeting = rules.Event(NOW - timedelta(hours=1), NOW - timedelta(minutes=2), True)
    phone = rules.phone_state([ev("call_ended", 1)], NOW)
    assert rules.at_breakpoint([meeting], NOW, phone=phone) == "calendar_gap"


def test_2_a_meeting_and_a_focus_the_meeting_s_end_decides(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    meetings(db, (NOW - timedelta(minutes=30), NOW + timedelta(minutes=30)))
    said(db, device, "focus_on", NOW - timedelta(minutes=20))
    m = moment(db)
    assert m.busy == rules.Busy("meeting", NOW + timedelta(minutes=30))


# --- 3. a notice opened counts as tapped --------------------------------------------------------------------


def test_3_a_notice_opened_is_tapped_before_the_dismissal(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    opened, swiped = (notice(db, "attend.replan", kind="replan") for _ in range(2))
    for nid in (opened, swiped):
        assert deliver(db, nid, device)[0] == "ws"
        db.execute(
            "UPDATE notifications SET delivered_at = %s WHERE id = %s", (NOW - timedelta(hours=1), nid)
        )
    said(db, device, "notification_opened", NOW, site="push", state="opened", notification_id=opened)
    said(db, device, "notification_dismissed", NOW, notification_id=opened)  # opened, then swiped away
    said(db, device, "notification_dismissed", NOW, notification_id=swiped)
    tick(db, NOW + timedelta(minutes=10))
    outcomes = dict(db.execute("SELECT ref, outcome FROM guardian_items").fetchall())
    assert outcomes[f"notification:{opened}"] == "tapped"
    assert outcomes[f"notification:{swiped}"] == "dismissed"


# --- 4. a daily cap per family ------------------------------------------------------------------------------


def test_4_drives_and_calls_never_crowd_out_a_dismissal(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    at = "2026-10-20T12:58:00+02:00"
    moves = [{"event": "activity", "site": "motion.walking", "state": "stationary", "at": at}] * 150
    assert (
        client.post("/v1/devices/me/diagnostics", headers=auth, json={"appEvents": moves}).status_code == 204
    )
    swipes = [{"event": "notification_dismissed", "notification_id": str(uuid.uuid4()), "at": at}
              for _ in range(60)]  # fmt: skip
    companion = [{"event": "companion_retry", "at": at}] * 60
    client.post("/v1/devices/me/diagnostics", headers=auth, json={"appEvents": swipes + companion})
    counts = dict(db.execute("SELECT event, count(*) FROM device_app_events GROUP BY event").fetchall())
    assert counts == {
        "activity": app_rules.PER_DAY["breakpoints"],
        "notification_dismissed": app_rules.PER_DAY["notifications"],
        "companion_retry": app_rules.PER_DAY["companion"],
    }
    assert app_rules.PER_DAY == {"companion": 50, "notifications": 50, "breakpoints": 100, "microphone": 20}


# --- 5. what waits now --------------------------------------------------------------------------------------


@pytest.fixture
def items(db: psycopg.Connection) -> None:
    rows = [
        ("breakpoint", None, None, None),  # held now
        ("digest", None, None, None),  # held now
        ("now", NOW, None, None),  # went at once
        ("breakpoint", NOW, None, None),  # out
        ("breakpoint", None, NOW, None),  # lapsed
        ("breakpoint", None, None, "breakpoint:idle"),  # let out, the dispatcher not yet
    ]
    for n, (decision, released, lapsed, how) in enumerate(rows):
        db.execute(
            "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, decided_at, "
            "released_at, lapsed_at, released_how) VALUES (%s, %s, 'attend.replan', 'replan', %s, 'score', "
            "'{}', %s, %s, %s, %s)",
            (uuid.uuid4(), f"t:{n}", decision, NOW - timedelta(days=9), released, lapsed, how),
        )


def test_5_what_waits_now_on_its_light_endpoint(client, db, items) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    before = db.execute("SELECT count(*) FROM guardian_items WHERE released_at IS NULL").fetchone()
    response = client.get("/v1/guardian/holding", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and response.json() == {"holding": 2}
    assert db.execute("SELECT count(*) FROM guardian_items WHERE released_at IS NULL").fetchone() == before
    assert client.get("/v1/guardian/holding").status_code == 401


def test_5_the_ledger_summary_carries_what_waits_now(client, db, items) -> None:  # type: ignore[no-untyped-def]
    from tests.test_life_admin import WEEK, hdr

    summary = client.get(f"/v1/ledger/summary?week={WEEK}", headers=hdr(db)).json()
    assert summary["interruptions"]["holding"] == 2  # not the week's: decided nine days ago


# --- 6. the rules they said in words ------------------------------------------------------------------------


def test_6_his_rules_in_words_are_listed_and_forgotten(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    marta, claim, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.execute(
        "INSERT INTO people (id, name, name_key, source, emails, created_at, updated_at) "
        "VALUES (%s, 'Marta Rossi', 'marta rossi', 'owner', '{}', now(), now())",
        (marta,),
    )
    for cid, statement in ((claim, "Interrompimi sempre per Marta"), (other, "Mi piace il caffè amaro")):
        db.execute(
            "INSERT INTO profile_claims (id, kind, statement, subject, polarity, scope, provenance, support, "
            "against, confidence, state, sensitive, hard, created_at, updated_at) VALUES (%s, 'constraint', "
            "%s, '{}', 1, '{}', 'said', 1, 0, 0.33, 'active', false, false, now(), now())",
            (cid, statement),
        )
    listed = client.get("/v1/profile/rules", headers=auth).json()
    assert listed["interruptions"] == [
        {"claim_id": str(claim), "mode": "always", "kind": None, "person_id": str(marta)}
    ]
    assert client.delete(f"/v1/profile/claims/{claim}", headers=auth).status_code in (200, 204)
    assert client.get("/v1/profile/rules", headers=auth).json()["interruptions"] == []


# --- 7. the merge -----------------------------------------------------------------------------


def test_7_an_event_dated_after_now_is_never_kept(db) -> None:  # type: ignore[no-untyped-def]
    """A phone whose clock is months ahead would make Pisa hold his things then, for a call that never was."""
    from pisa.services import app_events

    device, _ = pair(db)
    ahead = NOW + timedelta(days=120)
    batch = [
        {"event": "call_started", "site": "CallKit", "state": "on", "at": ahead.isoformat()},
        {"event": "focus_on", "site": "Focus", "state": "on", "at": (NOW + timedelta(minutes=3)).isoformat()},
        {
            "event": "focus_off",
            "site": "Focus",
            "state": "off",
            "at": (NOW + timedelta(minutes=1)).isoformat(),
        },
        {"event": "call_ended", "site": "CallKit", "state": "off", "at": NOW.isoformat()},
    ]

    async def job(pool: Any) -> int:
        async with pool.connection() as conn:
            return await app_events.store(conn, uuid.UUID(device), batch, now=NOW, timezone=TZ)

    assert run(db, job) == 2  # a minute's skew is a clock, months is not
    assert {r[0] for r in db.execute("SELECT event FROM device_app_events").fetchall()} == {
        "focus_off", "call_ended",
    }  # fmt: skip
    assert moment(db, ahead + timedelta(minutes=5)).busy is None


def test_7_a_revoked_phone_s_last_signal_no_longer_counts(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    said(db, device, "call_started", NOW - timedelta(minutes=5), site="CallKit", state="on")
    said(db, device, "activity", NOW - timedelta(minutes=6), site="motion.walking", state="automotive")
    m = moment(db)
    assert m.busy is not None and m.busy.reason == "call"
    db.execute("UPDATE devices SET revoked_at = %s WHERE id = %s", (NOW - timedelta(minutes=1), device))
    assert moment(db).busy is None

    async def job(pool: Any) -> tuple[bool, bool]:
        async with pool.connection() as conn:
            return await guardian.motion(conn, NOW)

    assert run(db, job) == (False, False)


def test_7_a_start_and_end_in_the_same_second_the_end_wins() -> None:
    at = NOW - timedelta(minutes=1)
    for order in ((0, 1), (1, 0)):
        pair_ = [
            rules.PhoneEvent("call_started", "CallKit", "on", at),
            rules.PhoneEvent("call_ended", "CallKit", "off", at),
        ]
        phone = rules.phone_state([pair_[i] for i in order], NOW)
        assert phone.call is None and phone.ended == "call_end"
        focus = [
            rules.PhoneEvent("focus_on", "Focus", "on", at),
            rules.PhoneEvent("focus_off", "Focus", "off", at),
        ]
        assert rules.phone_state([focus[i] for i in order], NOW).focus is None


def test_7_an_approval_he_already_answered_is_not_held(client, db, items) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    for status in ("pending", "approved", "rejected"):
        approval = uuid.uuid4()
        db.execute(
            "INSERT INTO approvals (id, kind, summary, payload, status, created_at, expires_at) "
            "VALUES (%s, 'calendar_create', 'Una prova', '{}', %s, %s, %s)",
            (approval, status, NOW - timedelta(hours=1), NOW + timedelta(hours=1)),
        )
        db.execute(
            "INSERT INTO guardian_items (id, ref, path, kind, decision, reason, score, decided_at) "
            "VALUES (%s, %s, 'approvals.deliver', 'approval', 'breakpoint', 'score', '{}', %s)",
            (uuid.uuid4(), f"approval:{approval}", NOW - timedelta(minutes=30)),
        )
    response = client.get("/v1/guardian/holding", headers={"Authorization": f"Bearer {token}"})
    assert response.json() == {"holding": 3}  # the fixture's two and the approval still waiting
