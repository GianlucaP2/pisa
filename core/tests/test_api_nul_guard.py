"""Found by Schemathesis (`scripts/ci/api_fuzz.py`): U+0000 in a request's text reached Postgres, which can't
store it, and half the write routes failed with a 500 (places notes, learn sources, off-limits, the views
report, memory corrections ...). It is refused at the door now (`app.NulGuard`, 400 validation_failed), in the
path, the query and a JSON body, before any route runs."""

from typing import Any

import pytest

from tests.conftest import pair


def refused_nul(response: Any) -> bool:
    return response.status_code == 400 and "U+0000" in response.json()["error"]["message"]


@pytest.mark.db
def test_nul_is_refused_at_the_door(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    assert refused_nul(client.post("/v1/tasks", headers=auth, json={"title": "Buy\u0000 milk"}))
    assert refused_nul(client.post("/v1/places/notes", headers=auth, json={"text": "a\u0000"}))
    assert refused_nul(client.get("/v1/views/report?view=%00", headers=auth))
    assert refused_nul(client.post("/v1/tasks/%00/dismiss", headers=auth))
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
    # a backslash before "u0000" is text, not NUL: it passes, and the body reaches the route intact
    made = client.post("/v1/tasks", headers=auth, json={"title": "C:\\u0000 folder"})
    assert made.status_code == 201 and made.json()["title"] == "C:\\u0000 folder"
