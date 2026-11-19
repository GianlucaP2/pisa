"""Tasks (contract B) and agenda (contract C), across a Europe/Rome day boundary and the 2026-10-25 DST
change."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.conftest import pair


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def add(client, auth, **body) -> dict:  # type: ignore[no-untyped-def]
    response = client.post("/v1/tasks", headers=auth, json=body)
    assert response.status_code == 201, response.json()
    return response.json()


def event(db: psycopg.Connection, eid: str, title: str, start: str, end: str, all_day: bool = False) -> None:
    db.execute("INSERT INTO calendars (id, name) VALUES ('cal', 'Personale') ON CONFLICT DO NOTHING")
    starts, ends = datetime.fromisoformat(start), datetime.fromisoformat(end)
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, "
        "start_date, end_date, "
        "location, status) VALUES ('cal', %s, %s, %s, %s, %s, %s, %s, NULL, 'confirmed')",
        (
            eid,
            title,
            starts,
            ends,
            all_day,
            starts.date() if all_day else None,
            ends.date() if all_day else None,
        ),
    )


def test_create_shape_and_validation(client, auth) -> None:  # type: ignore[no-untyped-def]
    task = add(
        client,
        auth,
        title="  Ritirare   il pacco ",
        due_at="2026-10-24",
        due_kind="date",
        place_text="Via Immaginaria 1",
    )
    assert set(task) == {
        "id", "title", "notes", "status", "due_at", "due_kind", "duration_min", "place_text", "place_id",
        "priority", "priority_reason", "priority_source", "repeat", "source", "confidence", "created_at",
        "updated_at", "deadline_source", "start_at", "country", "private",
    }  # fmt: skip
    assert (task["priority_reason"], task["priority_source"]) == (None, "pisa")
    assert task["title"] == "Ritirare il pacco" and task["status"] == "open" and task["priority"] == 2
    assert task["due_at"] == "2026-10-24T23:59:00+02:00" and task["due_kind"] == "date"
    assert task["source"] == {"kind": "manual", "ref": None, "label": None} and task["confidence"] is None
    dst = add(client, auth, title="After the clock change", due_at="2026-10-25", due_kind="date")
    assert dst["due_at"] == "2026-10-25T23:59:00+01:00"
    timed = add(client, auth, title="Call the bank", due_at="2026-10-21T09:30:00")
    assert timed["due_kind"] == "datetime" and timed["due_at"] == "2026-10-21T09:30:00+02:00"
    for bad, message in (
        ({"title": "x"}, "A task title must be 3-120 characters long."),
        ({"title": "Valid", "due_kind": "date"}, "A task with due_kind 'date' needs a due_at."),
        (
            {"title": "Valid", "due_at": "tomorrow"},
            "The due date 'tomorrow' is not an RFC 3339 date or date-time.",
        ),
        (
            {"title": "Valid", "due_at": "2029-01-01", "due_kind": "date"},
            "A due date can be at most 2 years ahead.",
        ),
    ):
        response = client.post("/v1/tasks", headers=auth, json=bad)
        assert response.status_code == 400 and response.json()["error"]["message"] == message, bad
    assert client.post("/v1/tasks", headers=auth, json={"title": "Valid", "priority": 7}).status_code == 400


def test_patch_subset_and_404(client, auth, clock) -> None:  # type: ignore[no-untyped-def]
    task = add(client, auth, title="Pagare la bolletta", due_at="2026-10-24", due_kind="date")
    clock.now += timedelta(minutes=5)
    patched = client.patch(f"/v1/tasks/{task['id']}", headers=auth, json={"status": "done", "priority": 1})
    assert patched.status_code == 200
    body = patched.json()
    assert (body["status"], body["priority"], body["due_at"]) == ("done", 1, "2026-10-24T23:59:00+02:00")
    assert body["updated_at"] > body["created_at"]
    cleared = client.patch(f"/v1/tasks/{task['id']}", headers=auth, json={"due_at": None}).json()
    assert (cleared["due_at"], cleared["due_kind"]) == (None, "none")
    timed = client.patch(
        f"/v1/tasks/{task['id']}", headers=auth, json={"due_at": "2026-10-22T18:00:00+02:00"}
    )
    assert timed.json()["due_kind"] == "datetime"
    missing = client.patch(
        "/v1/tasks/00000000-0000-4000-8000-000000000000", headers=auth, json={"status": "done"}
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert client.patch("/v1/tasks/not-a-uuid", headers=auth, json={"status": "done"}).status_code == 404
    assert [t["id"] for t in client.get("/v1/tasks?status=done", headers=auth).json()["tasks"]] == [
        task["id"]
    ]
    assert client.get("/v1/tasks", headers=auth).json()["tasks"] == []
    assert client.get("/v1/tasks?status=bogus", headers=auth).status_code == 400


def test_ordering(client, auth, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)  # 12:00 in Rome
    add(client, auth, title="No date, low", priority=3)
    add(client, auth, title="No date, high", priority=1)
    add(client, auth, title="Friday", due_at="2026-10-23", due_kind="date")
    add(client, auth, title="Overdue since yesterday", due_at="2026-10-19", due_kind="date")
    add(client, auth, title="Tomorrow 9:00, low", due_at="2026-10-21T09:00:00+02:00", priority=3)
    add(client, auth, title="Tomorrow 9:00, high", due_at="2026-10-21T09:00:00+02:00", priority=1)
    add(client, auth, title="Overdue this morning", due_at="2026-10-20T08:00:00+02:00")
    titles = [t["title"] for t in client.get("/v1/tasks", headers=auth).json()["tasks"]]
    assert titles == [
        "Overdue since yesterday",
        "Overdue this morning",
        "Tomorrow 9:00, high",
        "Tomorrow 9:00, low",
        "Friday",
        "No date, high",
        "No date, low",
    ]


def test_agenda_groups_by_rome_day_with_overdue_under_today(client, auth, clock, db) -> None:  # type: ignore[no-untyped-def]
    clock.now = datetime(2026, 10, 20, 21, 45, tzinfo=UTC)  # 23:45 on the 20th in Rome
    add(client, auth, title="Overdue", due_at="2026-10-18", due_kind="date")
    add(client, auth, title="Late tonight", due_at="2026-10-20T23:30:00+02:00")
    add(
        client, auth, title="Just after midnight", due_at="2026-10-21T00:30:00+02:00"
    )  # 22:30 UTC on the 20th
    done = add(client, auth, title="Already done", due_at="2026-10-20T10:00:00+02:00")
    client.patch(f"/v1/tasks/{done['id']}", headers=auth, json={"status": "done"})
    event(db, "late", "Late call", "2026-10-20T23:30:00+02:00", "2026-10-20T23:45:00+02:00")
    event(db, "early", "Early run", "2026-10-21T00:30:00+02:00", "2026-10-21T01:00:00+02:00")
    event(
        db, "trip", "Weekend a Torino", "2026-10-20T00:00:00+02:00", "2026-10-22T00:00:00+02:00", all_day=True
    )
    event(db, "lunch", "Pranzo", "2026-10-20T13:00:00+02:00", "2026-10-20T14:00:00+02:00")

    body = client.get("/v1/agenda", headers=auth).json()
    assert body["timezone"] == "Europe/Rome" and [d["date"] for d in body["days"]] == [
        "2026-10-20",
        "2026-10-21",
    ]
    today, tomorrow = body["days"]
    assert [e["title"] for e in today["events"]] == [
        "Weekend a Torino",
        "Pranzo",
        "Late call",
    ]  # all-day first
    assert [e["title"] for e in tomorrow["events"]] == ["Weekend a Torino", "Early run"]
    assert [t["title"] for t in today["tasks_due"]] == ["Overdue", "Late tonight"]
    assert [t["title"] for t in tomorrow["tasks_due"]] == ["Just after midnight"]
    lunch = today["events"][1]
    assert lunch == {
        "id": "lunch",
        "title": "Pranzo",
        "start": "2026-10-20T13:00:00+02:00",
        "end": "2026-10-20T14:00:00+02:00",
        "all_day": False,
        "location": None,
        "calendar": "Personale",
        "place_id": None,  # Set when the location resolves to one place
    }

    clock.now = datetime(2026, 10, 20, 22, 30, tzinfo=UTC)  # 00:30 on the 21st in Rome: "today" moved
    moved = client.get("/v1/agenda?days=1", headers=auth).json()["days"]
    assert [d["date"] for d in moved] == ["2026-10-21"]
    assert [t["title"] for t in moved[0]["tasks_due"]] == ["Overdue", "Late tonight", "Just after midnight"]


def test_agenda_across_the_dst_change(client, auth, clock, db) -> None:  # type: ignore[no-untyped-def]
    clock.now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    add(client, auth, title="Due on the 25th", due_at="2026-10-25", due_kind="date")
    event(db, "night", "Night shift", "2026-10-25T23:30:00+01:00", "2026-10-25T23:59:00+01:00")  # 22:30 UTC
    event(db, "small-hours", "02:30, second time", "2026-10-25T02:30:00+01:00", "2026-10-25T02:45:00+01:00")
    days = client.get("/v1/agenda?from=2026-10-25&days=2", headers=auth).json()["days"]
    sunday, monday = days
    assert [e["title"] for e in sunday["events"]] == ["02:30, second time", "Night shift"]
    assert [t["due_at"] for t in sunday["tasks_due"]] == ["2026-10-25T23:59:00+01:00"]
    assert monday["events"] == [] and monday["tasks_due"] == []
    assert client.get("/v1/agenda?days=15", headers=auth).status_code == 400
    assert client.get("/v1/agenda?from=nope", headers=auth).status_code == 400


def test_duration_min_contract_l(client, auth) -> None:  # type: ignore[no-untyped-def]
    task = add(client, auth, title="Rinnovare il passaporto", duration_min=120)
    assert task["duration_min"] == 120
    patched = client.patch(f"/v1/tasks/{task['id']}", headers=auth, json={"duration_min": 45}).json()
    assert patched["duration_min"] == 45
    cleared = client.patch(f"/v1/tasks/{task['id']}", headers=auth, json={"duration_min": None}).json()
    assert cleared["duration_min"] is None
    for bad in (4, 481, 0):
        response = client.patch(f"/v1/tasks/{task['id']}", headers=auth, json={"duration_min": bad})
        assert response.status_code == 400
        assert (
            response.json()["error"]["message"]
            == "duration_min must be a whole number from 5 to 480 minutes."
        )
