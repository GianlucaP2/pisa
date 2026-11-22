"""Every key and device is managed from the app. The APNs key is pasted as text (lines may arrive
joined), shown only as "installed + key id", and can be removed; a paired device offers a pairing to
another one, lists the devices, and revokes any device but the last active one."""

import psycopg
import pytest

from tests.conftest import pair
from tests.test_privacy_v2 import app_settings, tls  # noqa: F401 (the fixtures: a TLS pin to pair with)
from tests.test_push import p8


@pytest.fixture
def phone(db: psycopg.Connection) -> tuple[str, dict[str, str]]:
    device_id, token = pair(db, "iPhone")
    return device_id, {"Authorization": f"Bearer {token}"}


def test_push_key_pasted_in_the_app(client, phone, db) -> None:  # type: ignore[no-untyped-def]
    _, auth = phone
    assert client.get("/v1/push/key", headers=auth).json() == {
        "configured": False,
        "key_id": None,
        "set_at": None,
    }
    joined = " ".join(p8()[0].splitlines())  # a password manager joined the lines
    assert (
        client.put("/v1/push/key", headers=auth, json={"key": joined, "key_id": "ABCDE12345"}).status_code
        == 204
    )
    status = client.get("/v1/push/key", headers=auth).json()
    assert status["configured"] is True and status["key_id"] == "ABCDE12345" and status["set_at"]
    assert "PRIVATE" not in str(status)  # never sent back
    stored = db.execute("SELECT ciphertext FROM server_secrets WHERE name = 'apns'").fetchone()
    assert stored is not None and b"PRIVATE KEY" not in bytes(stored[0])  # Fernet-encrypted
    bad = client.put("/v1/push/key", headers=auth, json={"key": "hello", "key_id": "ABCDE12345"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_failed"
    bad_id = client.put("/v1/push/key", headers=auth, json={"key": joined, "key_id": "short"})
    assert bad_id.status_code == 400
    assert client.delete("/v1/push/key", headers=auth).status_code == 204
    assert client.get("/v1/push/key", headers=auth).json()["configured"] is False


def test_pair_another_device_from_the_app(client, phone) -> None:  # type: ignore[no-untyped-def]
    from tests.test_pairing_review_fixes import offer_and_pair

    _, auth = phone
    confirmed, _, paired = offer_and_pair(client, auth["Authorization"].removeprefix("Bearer "), "iPad")
    assert confirmed.status_code == 200, confirmed.text
    devices = client.get("/v1/devices", headers=auth).json()["devices"]
    assert [(d["name"], d["this_device"], d["revoked_at"]) for d in devices] == [
        ("iPhone", True, None),
        ("iPad", False, None),
    ]
    ipad = paired["deviceId"]
    assert client.delete(f"/v1/devices/{ipad}", headers=auth).status_code == 204
    assert client.get("/v1/devices", headers=auth).json()["devices"][1]["revoked_at"] is not None


def test_the_last_active_device_is_not_revoked_from_the_app(client, phone) -> None:  # type: ignore[no-untyped-def]
    phone_id, auth = phone
    response = client.delete(f"/v1/devices/{phone_id}", headers=auth)
    assert response.status_code == 409 and response.json()["error"]["code"] == "last_device"
    assert client.get("/v1/devices", headers=auth).status_code == 200  # still paired
    missing = client.delete("/v1/devices/00000000-0000-4000-8000-000000000999", headers=auth)
    assert missing.status_code == 404


def test_endpoints_need_a_paired_device(client) -> None:  # type: ignore[no-untyped-def]
    for method, path in (
        ("GET", "/v1/push/key"),
        ("GET", "/v1/devices"),
        ("POST", "/v1/pair/offers"),
    ):
        assert client.request(method, path, json={"name": "x"}).status_code == 401
