# SPDX-License-Identifier: AGPL-3.0-only
"""The contract's relay tables (CONTRACT-2 §9; `protocol/v2/fixtures/pairing/vectors.json`, read where the
contract keeps it), written independently of this code: the app's MACs and answers (`relay.appApi`, with
its `removalRule`), the status table (`relay.answers`), the limits (`relay.limits`), and a relay-mode Live
Activity update with its aps and what is refused (`relay.liveActivityRelayMode`,
`relay.liveActivityApsRelayMode`, `relay.liveActivityRelayModeRefused`)."""

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pisa_relay import app as relay_app
from pisa_relay.apns import live_activity_payload
from pisa_relay.app import PushBody, account_token, app_context, may_remove, verify_signature
from pisa_relay.settings import Settings
from pisa_relay.storage import ACTIVITY_TOKENS_PER_KIND, Device, Registration, hour
from tests.conftest import VECTORS, Relay

RELAY = VECTORS["relay"]
APP = RELAY["appApi"]
CORE = Path(__file__).resolve().parents[2] / "core"
# The vectors' 404 row says "delete_registration" (CONTRACT-2 §9 allows a relay to answer 404 for a gone
# handle). This relay never answers 404 on /v1/push, and the server logs one (a mistyped URL or a proxy), as
# `core/tests/test_push_relay.py SERVER_READS` does; the row changes at the next contract minor
# (`docs/pisa/LATER.md`).
SERVER_READS = {404: "log"}


def _verifies(key: bytes, headers: dict[str, str], body: str, context: bytes = b"") -> bool:
    stamp = headers["X-Pisa-Relay-Timestamp"]
    sig = headers["X-Pisa-Relay-Signature"]
    return verify_signature(key, stamp, sig, body.encode(), int(stamp), context) is not None


def test_app_api_macs_verify_on_their_own_route_only() -> None:
    key = hashlib.sha256(bytes.fromhex(APP["appSecretHex"])).digest()
    assert relay_app.APP_CONTEXT == (APP["label"] + "\n").encode()
    for request in APP["requests"]:
        context = app_context(request["method"], request["path"])
        assert _verifies(key, request["headers"], request["body"], context), request["path"]
        assert request["headers"]["Content-Type"] == "application/json"
        assert not _verifies(key, request["headers"], request["body"])  # not as a server signature
    activity = APP["requests"][0]
    other = APP["notAcceptedOnOtherRoutes"]
    for sig in (other["activityMacForAnotherPath"], other["activityWithServerConstruction"]):
        headers = activity["headers"] | {"X-Pisa-Relay-Signature": sig}
        assert not _verifies(key, headers, activity["body"], app_context("POST", "/v1/register/activity"))


def test_only_the_relay_s_410_is_read_as_a_gone_handle() -> None:
    """`relay.answers`: what the server does with each answer. The relay ends a handle with 410 and a Live
    Activity token with 422, and no other status it sends means either."""
    actions: dict[str, set[int]] = {}
    for row in RELAY["answers"]:
        action: str = SERVER_READS[row["status"]] if row["status"] in SERVER_READS else row["action"]
        actions.setdefault(action, set()).add(row["status"])
    assert actions["delete_registration"] == {410}
    assert actions["drop_activity_token"] == {422}
    assert {202, 410, 422} <= {row["status"] for row in RELAY["answers"]}


def test_delete_by_the_server_uses_section_9_s_construction() -> None:
    server_key = bytes.fromhex(RELAY["keyHex"])
    delete = APP["deleteByTheServer"]
    assert _verifies(server_key, delete["headers"], delete["body"])


def test_the_app_account_token_derivation_matches() -> None:
    vector = APP["appAccountToken"]
    assert relay_app.ACCOUNT_TOKEN_LABEL == (vector["label"] + "\n").encode()
    assert account_token(bytes.fromhex(vector["accountKeyHex"])) == vector["token"]


def test_register_and_devices_answers_have_the_contract_s_fields(relay: Relay) -> None:
    response = relay.try_register()
    answer = response.json()
    assert set(answer) == set(APP["registerAnswer"])
    for field, value in APP["registerAnswer"].items():
        assert len(answer[field]) == len(value)
    handle = answer["handle"]
    from tests.conftest import unb64url

    devices = relay.app_signed(
        "/v1/register/devices", unb64url(answer["app_secret"]), {"handle": handle}
    ).json()
    expected = APP["devicesAnswer"]["devices"][0]
    assert set(devices) == set(APP["devicesAnswer"]) and set(devices["devices"][0]) == set(expected)
    assert len(devices["devices"][0]["id"]) == len(expected["id"])


def test_the_limits_match_and_there_is_no_limit_across_all_clients(tmp_path: Path) -> None:
    from tests.conftest import settings_for

    limits = RELAY["limits"]
    settings: Settings = settings_for(tmp_path)
    assert {
        "handlesPerSubscription": settings.handles_per_subscription,
        "evictIdleDays": settings.evict_idle_days,
        "registerPerSubscriptionHour": settings.register_per_subscription_hour,
        "activityTokensKeptPerKind": ACTIVITY_TOKENS_PER_KIND,
        "transactionMaxAgeS": settings.transaction_max_age_s,
        "pushBodyMaxBytes": relay_app.LIMITS["push"],
        "replayKeepS": relay_app.ACCEPTED_KEEP_S,
    } == limits
    assert not any("per_minute" in name and "register" in name for name in Settings.model_fields)


def test_a_relay_mode_live_activity_update_and_its_aps() -> None:
    request = RELAY["liveActivityRelayMode"]
    data = PushBody.model_validate_json(json.dumps(request))
    assert data.activity is not None
    assert live_activity_payload(data.activity.model_dump()) == {"aps": RELAY["liveActivityApsRelayMode"]}
    for refused in RELAY["liveActivityRelayModeRefused"]:
        leaky: dict[str, Any] = request | {"activity": refused["activity"]}
        with pytest.raises(ValueError):
            PushBody.model_validate_json(json.dumps(leaky))


def test_the_relay_answers_the_contract_s_live_activity_update_and_refuses_the_others(relay: Relay) -> None:
    request = RELAY["liveActivityRelayMode"]
    token = request["activity"]["token"]
    handle, secret = relay.register()
    relay.clock.now = request["activity"]["timestamp"]  # the vector's own time, inside the window
    assert relay.activity_token(handle, secret, token, stamp=int(relay.clock.now) - 1).status_code == 204
    for i, refused in enumerate(RELAY["liveActivityRelayModeRefused"]):
        body = {"handle": handle, "type": "liveactivity", "activity": refused["activity"]}
        assert relay.push(handle, secret, body, stamp=int(relay.clock.now) + i).status_code == 400, refused[
            "why"
        ]
    body = {"handle": handle, "type": "liveactivity", "activity": request["activity"]}
    assert relay.push(handle, secret, body, stamp=int(relay.clock.now) + 9).status_code == 202
    assert relay.apns.payloads() == [{"aps": RELAY["liveActivityApsRelayMode"]}]


@pytest.mark.parametrize("case", APP["removalRule"], ids=lambda c: c["name"])
def test_the_removal_rule(case: dict[str, Any]) -> None:
    now = 1_763_460_000.0
    signer_created = hour(now - (100 if case["signer_registered_first"] else 1) * 86_400)
    target_created = hour(now - 50 * 86_400)
    if not case["signer_registered_first"]:
        signer_created, target_created = target_created, signer_created
    signer = Registration(
        "signer",
        "ab" * 32,
        "production",
        b"k",
        0,
        None,
        "en",
        b"a",
        b"ref",
        signer_created,
        case["signer_proved_account_key"],
    )
    target_handle = "signer" if case["target_is_signer"] else "target"
    target = Device(
        "id", target_handle, target_created, hour(now - case["target_idle_days"] * 86_400), "production"
    )
    assert may_remove(signer, target, now, RELAY["limits"]["evictIdleDays"]) is case["allowed"]


def test_the_server_s_sender_builds_the_contract_s_live_activity_update() -> None:
    if not (CORE / "pisa" / "domain" / "live_activity.py").exists():
        pytest.skip("the Pisa server's code (core/) is not beside the relay")
    sys.path.insert(0, str(CORE))
    from pisa.domain import live_activity

    activity = RELAY["liveActivityRelayMode"]["activity"]
    built = live_activity.relay_activity(
        activity["token"], activity["event"], activity["priority"], activity["timestamp"]
    )
    assert built == activity


def test_an_exact_repeat_is_answered_once(relay: Relay) -> None:
    repeat = RELAY["exactRepeat"]
    handle, secret = relay.register()
    assert [relay.push(handle, secret).status_code for _ in range(2)] == [repeat["first"], repeat["repeat"]]
    assert len(relay.apns.requests) == repeat["sentToApple"]


def test_only_a_start_carries_the_attributes() -> None:
    """The ruling for §9 (push-to-start needs them): a start sends `attributes: {}` and
    `attributes-type`; update and end send neither."""
    base = RELAY["liveActivityRelayMode"]["activity"]
    start = live_activity_payload(base | {"event": "start", "attributesType": "NextStopAttributes"})["aps"]
    assert {k: start[k] for k in ("attributes", "attributes-type")} == {
        "attributes": {},
        "attributes-type": "NextStopAttributes",
    }
    assert set(start) == {
        "timestamp",
        "event",
        "content-state",
        "attributes",
        "attributes-type",
        "input-push-token",
    }
    for event, extra in (("update", {}), ("end", {"dismissalDate": base["timestamp"]})):
        aps = live_activity_payload(base | {"event": event} | extra)["aps"]
        assert "attributes" not in aps and "attributes-type" not in aps, event


def test_the_contract_s_live_activity_start_and_what_it_refuses(relay: Relay) -> None:
    """`relay.liveActivityStartRelayMode` and `relay.liveActivityStartRefused`, through the API."""
    start = RELAY["liveActivityStartRelayMode"]
    activity = start["body"]["activity"]
    data = PushBody.model_validate_json(json.dumps(start["body"]))
    assert data.activity is not None
    assert live_activity_payload(data.activity.model_dump()) == {"aps": start["aps"]}
    handle, secret = relay.register()
    relay.clock.now = activity["timestamp"]  # the vector's own time, inside the window
    registered = relay.activity_token(
        handle, secret, activity["token"], "start", stamp=int(relay.clock.now) - 1
    )
    assert registered.status_code == 204
    for i, refused in enumerate(RELAY["liveActivityStartRefused"]):
        body = {"handle": handle, "type": "liveactivity", "activity": refused["activity"]}
        assert relay.push(handle, secret, body, stamp=int(relay.clock.now) + i).status_code == 400, refused[
            "why"
        ]
    misplaced = [
        activity | {"event": "update"},
        activity | {"event": "start", "dismissalDate": activity["timestamp"]},
    ]
    for i, wrong in enumerate(misplaced):  # attributesType only with start, dismissalDate only with end
        body = {"handle": handle, "type": "liveactivity", "activity": wrong}
        assert relay.push(handle, secret, body, stamp=int(relay.clock.now) + 5 + i).status_code == 400
    body = {"handle": handle, "type": "liveactivity", "activity": activity}
    assert relay.push(handle, secret, body, stamp=int(relay.clock.now) + 9).status_code == 202
    assert relay.apns.payloads() == [{"aps": start["aps"]}]
