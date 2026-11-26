"""App events (shared contract): the companion's refusals, retries, recoveries and stops, on the
diagnostics upload. Allowlisted fields only, each string at most 64 characters, at most 50 a day per device,
never text the user said; 30 days; listed by the usage report."""

from datetime import datetime

from pisa.domain.app_events import sanitize
from tests.conftest import pair

AT = "2026-10-20T12:58:00+02:00"


def event(**extra: object) -> dict[str, object]:
    return {"event": "companion_refused", "code": "!int", "site": "startEngine()", "state": "listening",
            "background": True, "at": AT} | extra  # fmt: skip


def test_only_the_allowlisted_fields_are_kept() -> None:
    kept = sanitize([
        event(text="chiama Marta al 333 1234567", reply="Ok"),  # extra fields: dropped
        event(event="said", at=AT),  # not an event of the contract: dropped
        event(at="yesterday"),  # no valid time: dropped
        event(site="x" * 100, state="<b>listening</b>\n", code=1852797029),
        event(state="comprare il latte da Luigi alle 5", site="detector start", code="!int"),
        event(event=["companion_refused"]),  # not a string: dropped, not a 500
        "not an object",
    ])  # fmt: skip
    assert kept[0] == {
        "event": "companion_refused", "code": "!int", "site": "startEngine()", "state": "listening",
        "background": True, "at": datetime.fromisoformat(AT), "notification_id": None,
    }  # fmt: skip
    assert len(kept) == 3
    # A value outside its field's pattern is dropped, never trimmed into something readable (review L3):
    # free text in any field ("comprare il latte da Luigi alle 5") has no way through.
    assert (kept[1]["site"], kept[1]["state"], kept[1]["code"]) == (None, None, None)
    assert (kept[2]["site"], kept[2]["state"], kept[2]["code"]) == (None, None, "!int")


def test_the_upload_stores_them_and_keeps_50_a_day(client, db) -> None:  # type: ignore[no-untyped-def]
    device, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    events = [event(event="companion_retry"), event(event="companion_recovered", background=False)]
    response = client.post("/v1/devices/me/diagnostics", headers=auth, json={"appEvents": events})
    assert response.status_code == 204
    rows = db.execute(
        "SELECT device_id::text, event, code, site, state, background FROM device_app_events ORDER BY event"
    ).fetchall()
    assert rows == [
        (device, "companion_recovered", "!int", "startEngine()", "listening", False),
        (device, "companion_retry", "!int", "startEngine()", "listening", True),
    ]
    many = [event() for _ in range(60)]
    assert (
        client.post("/v1/devices/me/diagnostics", headers=auth, json={"appEvents": many}).status_code == 204
    )
    assert db.execute("SELECT count(*) FROM device_app_events").fetchone() == (50,)  # 2 + 48: the day's 50
    assert db.execute("SELECT count(*) FROM device_diagnostics").fetchone() == (0,)  # not a MetricKit payload


def test_the_testflight_11_envelope_is_accepted(client, db) -> None:  # type: ignore[no-untyped-def]
    """Build 11 wraps the events like a diagnostic; they are stored, not refused."""
    _, token = pair(db)
    body = {
        "kind": "events",
        "app_version": "0.1.0 (11)",
        "os_version": "26.0",
        "payload": {"appEvents": [event()]},
    }
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/devices/me/diagnostics", headers=auth, json=body).status_code == 204
    assert db.execute("SELECT event, code FROM device_app_events").fetchall() == [
        ("companion_refused", "!int")
    ]


def test_a_batch_too_big_is_refused(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    response = client.post("/v1/devices/me/diagnostics", headers=auth, json={"appEvents": [event()] * 201})
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "The app events must be a list of at most 200."
