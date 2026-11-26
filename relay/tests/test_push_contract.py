# SPDX-License-Identifier: AGPL-3.0-only
"""The relay's copy of the push contract against the Pisa server's (`core/pisa/domain/push_contract.py`), read
from the file: the relay never imports the server, and the two must not drift (token limits, environments and
hosts, the APNs answers that mean a token is gone, the provider token's lifetime)."""

import ast
import re
from typing import Any, get_args

import pytest

from pisa_relay import apns
from pisa_relay.app import ActivityTokenBody, RegisterBody
from tests.conftest import REPO

CONTRACT = REPO / "core" / "pisa" / "domain" / "push_contract.py"


def _contract() -> dict[str, Any]:
    if not CONTRACT.exists():
        pytest.skip("the Pisa server's code (core/) is not beside the relay")
    values: dict[str, Any] = {}
    for node in ast.parse(CONTRACT.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def _limits(pattern: str) -> tuple[int, int]:
    match = re.fullmatch(r"\^\[0-9a-fA-F\]\{(\d+),(\d+)\}\$", pattern)
    assert match, pattern
    return int(match[1]), int(match[2])


def _pattern(model: type, field: str) -> str:
    (pattern,) = [m.pattern for m in model.model_fields[field].metadata if hasattr(m, "pattern")]
    return pattern


def test_the_relay_keeps_the_server_s_push_contract() -> None:
    core = _contract()
    assert apns.DEVICE_TOKEN_HEX == core["DEVICE_TOKEN_HEX"]
    assert apns.ACTIVITY_TOKEN_HEX == core["ACTIVITY_TOKEN_HEX"]
    assert _limits(_pattern(RegisterBody, "apns_token")) == core["DEVICE_TOKEN_HEX"]
    assert _limits(_pattern(ActivityTokenBody, "token")) == core["ACTIVITY_TOKEN_HEX"]
    assert apns.HOSTS == core["APNS_HOSTS"]  # the relay names environments as the wire does
    assert set(get_args(RegisterBody.model_fields["environment"].annotation)) == set(
        core["APNS_ENVIRONMENTS"]
    )
    assert apns.TOKEN_GONE == core["APNS_TOKEN_GONE"]
    assert apns.TOKEN_TTL_S == core["APNS_JWT_RENEW_S"]
    assert 20 * 60 <= apns.TOKEN_TTL_S < 60 * 60  # Apple's window for refreshing a provider token
