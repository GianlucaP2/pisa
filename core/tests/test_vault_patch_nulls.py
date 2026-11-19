"""Found by Schemathesis (`scripts/ci/api_fuzz.py`): PATCH /v1/vault/documents/{id} with `country` (or kind,
expires_on, reminders) null was a 500, a NOT NULL violation. Those can be changed, never cleared: null is a
400. number_last4, issued_on and notes still clear with null."""

import pytest

from tests.conftest import pair


@pytest.mark.db
def test_a_document_s_required_fields_are_never_cleared(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    doc = client.post(
        "/v1/vault/documents",
        json={"kind": "passport", "expires_on": "2030-05-01", "notes": "Questura"},
        headers=auth,
    ).json()
    for field in ("kind", "country", "expires_on", "reminders"):
        answer = client.patch(f"/v1/vault/documents/{doc['id']}", json={field: None}, headers=auth)
        assert answer.status_code == 400, field
        assert answer.json()["error"]["code"] == "validation_failed"
    cleared = client.patch(f"/v1/vault/documents/{doc['id']}", json={"notes": None}, headers=auth)
    assert cleared.status_code == 200 and cleared.json()["notes"] is None
    assert cleared.json()["country"] == doc["country"] and cleared.json()["kind"] == "passport"
