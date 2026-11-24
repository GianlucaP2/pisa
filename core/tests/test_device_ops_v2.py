"""Contract 2's signed device-op vectors (`protocol/v2/fixtures/device_ops/`): the canonical text the phone
signs is byte-identical to the one the server checks, and each vector's target hashes its body as sent."""

import hashlib
import json
from pathlib import Path
from typing import get_args

import pytest

from pisa.services.phone_keys import DeviceOp, canonical

VECTORS = Path(__file__).parents[2] / "protocol" / "v2" / "fixtures" / "device_ops"
CASES = [
    pytest.param(case, id=f"{path.stem}: {case['name']}")
    for path in sorted(VECTORS.glob("*.json"))
    for case in json.loads(path.read_text())["cases"]
]


@pytest.mark.parametrize("case", CASES)
def test_the_canonical_text_matches_the_vector(case: dict) -> None:
    challenge = case["challenge"]
    subject = {"op": challenge["op"], "target": challenge["target"]}
    assert canonical(challenge["action"], subject, case["nonce"], case["expires_at"]) == case["canonical"]
    if case.get("body") is not None:
        digest = hashlib.sha256(case["body"].encode()).hexdigest()
        assert digest == case["bodySha256"]
        assert challenge["target"].endswith(":" + digest)


@pytest.mark.parametrize("case", CASES)
def test_the_server_knows_the_vector_s_op(case: dict) -> None:
    assert case["challenge"]["op"] in get_args(DeviceOp)
