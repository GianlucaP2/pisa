"""Found by Schemathesis (`scripts/ci/api_fuzz.py`): a task due in 1425 (or a vault document expiring in 247,
whose renewal task is due then) was written with Rome's local mean time, "+00:49:56", which is no RFC 3339
offset (GET /v1/tasks, /v1/agenda, PATCH and dismiss broke their schema). Such a moment is written in UTC."""

import re
from datetime import UTC, datetime

import pytest

from pisa.domain.tasks import rfc3339
from tests.conftest import pair

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def test_rfc3339_never_writes_a_local_mean_time_offset() -> None:
    assert rfc3339(datetime(2026, 11, 18, 22, 59, tzinfo=UTC), "Europe/Rome") == "2026-11-18T23:59:00+01:00"
    old = datetime(1425, 1, 29, 23, 10, 4, tzinfo=UTC)
    assert rfc3339(old, "Europe/Rome") == "1425-01-29T23:10:04+00:00"  # Rome's LMT was +00:49:56
    assert rfc3339(old, "America/New_York") == "1425-01-29T23:10:04+00:00"  # its LMT: -04:56:02


@pytest.mark.db
def test_an_ancient_due_date_is_still_rfc3339(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    task = client.post(
        "/v1/tasks", headers=auth, json={"title": "Renew it", "due_at": "1425-01-29", "due_kind": "date"}
    )
    assert task.status_code == 201
    assert RFC3339.match(task.json()["due_at"]), task.json()["due_at"]
    for row in client.get("/v1/tasks", headers=auth).json()["tasks"]:
        for key in ("due_at", "created_at", "updated_at"):
            assert row[key] is None or RFC3339.match(row[key]), (key, row[key])
