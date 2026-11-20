"""Review fixes: drafts never reach a model, transient sync errors, tasks edge cases,
agenda order in the repeated DST hour."""

from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest

from pisa.domain.mail import may_go_to_model
from pisa.services.google_sync import sync_gmail
from tests.conftest import pair
from tests.gateway_env import run
from tests.google_fake import FakeGoogle
from tests.test_google import T0, google_env
from tests.test_tasks import add, event


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def test_drafts_and_chats_never_go_to_a_model() -> None:
    assert not may_go_to_model(("DRAFT",))
    assert not may_go_to_model(("CHAT",))
    assert may_go_to_model(("INBOX", "IMPORTANT"))


class Flaky:
    """FakeGoogle, except Gmail answers 503 while `fail` is set."""

    def __init__(self, inner: FakeGoogle) -> None:
        self.inner, self.fail = inner, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail and "/gmail/" in request.url.path:
            return httpx.Response(503, json={"error": {"code": 503, "message": "Backend Error"}})
        return self.inner(request)


def test_one_transient_failure_does_not_turn_the_connector_red(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    flaky = Flaky(FakeGoogle())

    def state() -> tuple[Any, ...]:
        row = db.execute("SELECT status, message, gmail_failures FROM connector_accounts").fetchone()
        assert row is not None
        return row

    async def go(fail: bool, times: int) -> None:
        flaky.fail = fail
        async with google_env(db, secrets_key, flaky, clock) as (google, pool):  # type: ignore[arg-type]
            for _ in range(times):
                await sync_gmail(pool, google, now=T0)

    run(lambda: go(False, 1))
    assert state() == ("connected", None, 0)
    run(lambda: go(True, 2))
    assert state()[0] == "connected" and state()[2] == 2  # two in a row: still fine
    run(lambda: go(True, 1))
    status, message, failures = state()
    assert (status, failures) == ("error", 3) and message
    run(lambda: go(False, 1))
    assert state() == ("connected", None, 0)


def test_bare_date_without_due_kind_is_a_date(client, auth, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    task = add(client, auth, title="Pagare la bolletta", due_at="2026-10-22")
    assert task["due_kind"] == "date" and task["due_at"] == "2026-10-22T23:59:00+02:00"
    patched = client.patch(f"/v1/tasks/{task['id']}", headers=auth, json={"due_at": "2026-10-23"}).json()
    assert patched["due_kind"] == "date" and patched["due_at"] == "2026-10-23T23:59:00+02:00"


def test_patch_priority_null_is_refused(client, auth) -> None:  # type: ignore[no-untyped-def]
    task = add(client, auth, title="Chiamare l'idraulico", priority=1)
    response = client.patch(f"/v1/tasks/{task['id']}", headers=auth, json={"priority": None})
    assert response.status_code == 400 and response.json()["error"]["code"] == "validation_failed"
    assert client.get("/v1/tasks", headers=auth).json()["tasks"][0]["priority"] == 1


def test_agenda_orders_the_repeated_dst_hour_by_time(client, auth, clock, db) -> None:  # type: ignore[no-untyped-def]
    clock.now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    event(db, "late", "02:15 second time", "2026-10-25T02:15:00+01:00", "2026-10-25T02:20:00+01:00")  # 01:15Z
    event(db, "early", "02:30 first time", "2026-10-25T02:30:00+02:00", "2026-10-25T02:35:00+02:00")  # 00:30Z
    day = client.get("/v1/agenda?from=2026-10-25&days=1", headers=auth).json()["days"][0]
    assert [e["title"] for e in day["events"]] == ["02:30 first time", "02:15 second time"]
