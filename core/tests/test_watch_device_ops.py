"""A watch signs no `device` op (a watch only rejects): its own pairing key is refused for
every op, the rule is in `phone_keys` only, and another client's own key still signs the same op."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, get_args

import psycopg
import pytest

from pisa.db.pool import open_pool
from pisa.domain.errors import PisaError
from pisa.services import phone_keys
from pisa.services.phone_keys import DeviceOp, PhoneKeys, device_subject
from tests.conftest import dsn, pair_v2_key
from tests.owner import owner_at
from tests.phone_signing import sign

TARGET = "synthetic-target"


async def _guard(db: psycopg.Connection, op: Any, device_id: str, key: Any) -> None:
    """`device_id`'s own key signs `op` over its challenge."""
    async with await open_pool(dsn(db), max_size=2) as pool:
        keys = PhoneKeys(pool, owner=owner_at(), clock=lambda: datetime.now(UTC))
        made = await keys.challenge("device", device_subject(op, TARGET))
        await keys.guard(
            op,
            TARGET,
            made["nonce"],
            sign(key, made["canonical"]),
            f"device-{device_id}",  # the key id a client signs with its own device key
            always=True,
            device_id=uuid.UUID(device_id),
        )


async def _guard_unsigned(db: psycopg.Connection, op: Any, device_id: str) -> None:
    async with await open_pool(dsn(db), max_size=2) as pool:
        keys = PhoneKeys(pool, owner=owner_at(), clock=lambda: datetime.now(UTC))
        await keys.guard(op, TARGET, None, None, None, device_id=uuid.UUID(device_id))


def _refused(run: Any) -> bool:
    with pytest.raises(PisaError) as refused:
        asyncio.run(run)
    return refused.value.code == "biometric_required"


@pytest.mark.parametrize("op", get_args(DeviceOp))
def test_a_watch_s_own_key_signs_no_device_op(db: psycopg.Connection, op: str) -> None:
    watch_id, _, watch_key = pair_v2_key(db, "Watch", "watchos")
    assert _refused(_guard(db, op, watch_id, watch_key))
    cli_id, _, cli_key = pair_v2_key(db, "Laptop", "cli")
    asyncio.run(_guard(db, op, cli_id, cli_key))  # the same op, signed by another client's own key, holds


@pytest.mark.parametrize("op", get_args(DeviceOp))
def test_a_watch_is_refused_a_device_op_unsigned_on_a_fresh_install(db: psycopg.Connection, op: str) -> None:
    watch_id, _, _ = pair_v2_key(db, "Watch", "watchos")
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)  # no Face ID key yet
    assert _refused(_guard_unsigned(db, op, watch_id))


def test_device_is_in_what_a_watch_never_signs() -> None:
    assert "device" in phone_keys.WATCH_REFUSED
