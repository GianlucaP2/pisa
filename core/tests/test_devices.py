"""Devices: session-token authentication, revocation, and the CLI's list and revoke (pairing by QR is
`test_privacy_v2.py`, `test_pairing_*.py` and `test_pair_cli.py`)."""

import asyncio
import uuid

import psycopg
import pytest

from pisa.domain.errors import PisaError
from pisa.services.devices import authenticate, list_devices, revoke_device
from tests.conftest import dsn, pair


def run(db: psycopg.Connection, fn):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await fn(conn)

    return asyncio.run(go())


def test_a_session_token_authenticates_and_only_its_hash_is_kept(db: psycopg.Connection) -> None:
    device_id, token = pair(db)
    device = run(db, lambda c: authenticate(c, token))
    assert str(device.id) == device_id and device.name == "iPhone" and device.session_expires_at is not None
    hashes = [r[0] for r in db.execute("SELECT token_hash FROM device_sessions").fetchall()]
    assert hashes and token not in hashes


def test_revoke_and_unknown_tokens_fail_auth(db: psycopg.Connection) -> None:
    device_id, token = pair(db)
    run(db, lambda c: revoke_device(c, uuid.UUID(device_id)))
    assert run(db, list_devices)[0].revoked_at is not None
    with pytest.raises(PisaError) as info:
        run(db, lambda c: authenticate(c, token))
    assert info.value.code == "device_revoked"
    for unknown in ("not-a-token", None):
        with pytest.raises(PisaError) as info:
            run(db, lambda c, unknown=unknown: authenticate(c, unknown))
        assert info.value.code == "auth_failed"
    with pytest.raises(PisaError) as info:
        run(db, lambda c: revoke_device(c, uuid.uuid4()))
    assert info.value.code == "not_found"


def test_bearer_auth_on_rest(client, db: psycopg.Connection) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    assert client.get("/v1/ai/providers").json()["error"]["code"] == "auth_failed"
    assert client.get("/v1/ai/providers", headers={"Authorization": "Bearer nope"}).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/ai/providers", headers=headers).status_code == 200
    run(db, lambda c: revoke_device(c, uuid.UUID(device_id)))
    revoked = client.get("/v1/ai/providers", headers=headers)
    assert revoked.status_code == 401 and "revoked" in revoked.json()["error"]["message"]


def test_cli_list_revoke(db: psycopg.Connection, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from pisa.cli import main
    from pisa.settings import get_settings

    monkeypatch.setenv("PISA_DATABASE_URL", dsn(db))
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level, **_: None)  # structlog stays
    get_settings.cache_clear()
    try:
        device_id, _ = pair(db)
        assert main(["devices", "list"]) == 0
        assert device_id in capsys.readouterr().out
        assert main(["devices", "revoke", device_id]) == 0
        assert db.execute("SELECT revoked_at IS NOT NULL FROM devices").fetchone() == (True,)
    finally:
        get_settings.cache_clear()


def test_the_cli_pairs_by_qr_only(capsys) -> None:  # type: ignore[no-untyped-def]
    """`pisa devices pair` (v1's six-character code) is gone: `pisa pair` pairs."""
    from pisa.cli import main

    with pytest.raises(SystemExit):
        main(["devices", "pair", "--name", "iPhone"])
    assert "invalid choice" in capsys.readouterr().err
