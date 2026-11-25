# SPDX-License-Identifier: AGPL-3.0-only
"""App Store Server Notifications V2: a verified notice of an expiry, a refund or a revocation ends the
subscription's handles; a renewal extends them; a stale notice about an older period, a notice for another
app, product or a forged one changes nothing."""

from typing import Any

import pytest

from tests.conftest import BUNDLE, DAY_MS, NOW, Relay, transaction
from tests.pki import make_pki

NOW_MS = int(NOW.timestamp() * 1000)


def notify(relay: Relay, kind: str, signer: Any = None, **tx: Any) -> int:
    signer = signer or relay.pki
    payload = {
        "notificationType": kind,
        "notificationUUID": "11111111-1111-4111-8111-111111111111",
        "version": "2.0",
        "signedDate": NOW_MS,
        "data": {
            "bundleId": tx.pop("bundle", BUNDLE),
            "environment": "Production",
            "signedTransactionInfo": signer.sign(transaction(**tx)),
        },
    }
    return relay.client.post(
        "/v1/appstore/notifications", json={"signedPayload": signer.sign(payload)}
    ).status_code


@pytest.mark.parametrize("kind", ["EXPIRED", "GRACE_PERIOD_EXPIRED", "REFUND", "REVOKE"])
def test_the_end_of_a_subscription_ends_its_handles(relay: Relay, kind: str) -> None:
    first, first_secret = relay.register()
    second, second_secret = relay.register(token="ef" * 32)
    other, other_secret = relay.register(token="01" * 32, originalTransactionId="3000000000000001")
    assert notify(relay, kind) == 200
    assert relay.push(first, first_secret).status_code == 410
    assert relay.push(second, second_secret).status_code == 410
    assert relay.push(other, other_secret).status_code == 202  # another subscription


def test_a_renewal_extends_and_a_stale_expiry_then_changes_nothing(relay: Relay) -> None:
    handle, secret = relay.register(expiresDate=NOW_MS + DAY_MS)
    assert notify(relay, "DID_RENEW", expiresDate=NOW_MS + 31 * DAY_MS) == 200
    assert notify(relay, "EXPIRED", expiresDate=NOW_MS + DAY_MS) == 200  # about the earlier period
    relay.clock.now += 10 * 86_400
    assert relay.push(handle, secret).status_code == 202


@pytest.mark.parametrize("change", ["forged", "bundle", "product", "garbage"])
def test_a_notification_not_for_this_relay_changes_nothing(relay: Relay, change: str) -> None:
    handle, secret = relay.register()
    if change == "forged":
        assert notify(relay, "REFUND", signer=make_pki(NOW)) == 400
    elif change == "bundle":
        assert notify(relay, "REFUND", bundle="com.example.other") == 400
    elif change == "product":
        assert notify(relay, "REFUND", productId="com.example.pisa.coins") == 200
    else:
        response = relay.client.post("/v1/appstore/notifications", json={"signedPayload": "x.y.z"})
        assert response.status_code == 400
    assert relay.push(handle, secret).status_code == 202


def test_a_test_notification_or_a_summary_is_acknowledged(relay: Relay) -> None:
    payload = {"notificationType": "TEST", "version": "2.0", "signedDate": NOW_MS}
    signed = relay.pki.sign(payload)
    assert relay.client.post("/v1/appstore/notifications", json={"signedPayload": signed}).status_code == 200
