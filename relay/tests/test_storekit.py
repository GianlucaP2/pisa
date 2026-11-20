# SPDX-License-Identifier: AGPL-3.0-only
"""App Store JWS verification: a good chain passes; a chain that does not end in the pinned root, a missing
OID, an expired or misissued certificate, a wrong signature, the wrong app or product, an expired or revoked
subscription are all refused. And the pinned root is Apple Root CA - G3."""

import hashlib
from datetime import timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding

from pisa_relay import storekit
from tests.conftest import BUNDLE, DAY_MS, NOW, PRODUCT, transaction
from tests.pki import Pki, make_pki

PRODUCTS = frozenset({PRODUCT})


def verify(pki: Pki, token: str, **changes: Any) -> storekit.Transaction:
    args: dict[str, Any] = {
        "root": pki.root,
        "now": NOW,
        "bundle_id": BUNDLE,
        "products": PRODUCTS,
        "environments": frozenset({"Production"}),
    }
    return storekit.transaction(token, **(args | changes))


def test_the_pinned_root_is_apple_root_ca_g3() -> None:
    root = storekit.apple_root()
    digest = hashlib.sha256(root.public_bytes(Encoding.DER)).hexdigest().upper()
    assert digest == "63343ABFB89A6A03EBB57E9B3F5FA7BE7C4F5C756F3017B3A8C488C3653E9179"
    assert (
        root.subject.rfc4514_string()
        == "C=US,O=Apple Inc.,OU=Apple Certification Authority,CN=Apple Root CA - G3"
    )


def test_a_good_transaction_verifies() -> None:
    pki = make_pki(NOW)
    tx = verify(pki, pki.sign(transaction()))
    assert tx == storekit.Transaction(
        "2000000000000001", PRODUCT, int(NOW.timestamp()) + 30 * 86_400, "Production"
    )


def test_a_chain_that_does_not_end_in_the_pinned_root_is_refused() -> None:
    pinned, other = make_pki(NOW), make_pki(NOW, root_name="Test Root CA")  # same name, another key
    with pytest.raises(storekit.StoreKitError, match="Apple's root"):
        verify(pinned, other.sign(transaction()))
    # the attacker's chain with the pinned root appended as a fourth certificate, or swapped in at the top
    with pytest.raises(storekit.StoreKitError):
        verify(pinned, other.sign(transaction(), chain=[*other.chain(), pinned.root]))
    with pytest.raises(storekit.StoreKitError, match="not issued"):
        verify(pinned, other.sign(transaction(), chain=[other.leaf, other.intermediate, pinned.root]))


def test_the_real_apple_root_refuses_a_test_chain() -> None:
    pki = make_pki(NOW)
    with pytest.raises(storekit.StoreKitError, match="Apple's root"):
        verify(pki, pki.sign(transaction()), root=storekit.apple_root())


@pytest.mark.parametrize("which", ["intermediate", "leaf"])
def test_a_certificate_without_apple_s_oid_is_refused(which: str) -> None:
    pki = make_pki(NOW, intermediate_oid=which != "intermediate", leaf_oid=which != "leaf")
    with pytest.raises(storekit.StoreKitError, match=r"not Apple's WWDR|not an App Store signing"):
        verify(pki, pki.sign(transaction()))


def test_an_expired_certificate_is_refused() -> None:
    pki = make_pki(NOW - timedelta(days=30), leaf_days=10)
    with pytest.raises(storekit.StoreKitError, match="not valid now"):
        verify(pki, pki.sign(transaction()))


def test_a_signature_by_another_key_or_a_changed_payload_is_refused() -> None:
    pki = make_pki(NOW)
    with pytest.raises(storekit.StoreKitError, match="does not verify"):
        verify(pki, pki.sign(transaction(), key=ec.generate_private_key(ec.SECP256R1())))
    head, _, sig = pki.sign(transaction()).split(".")
    forged = jwt.encode(transaction(productId=PRODUCT, expiresDate=2**41), "k" * 32, algorithm="HS256").split(
        "."
    )[1]
    with pytest.raises(storekit.StoreKitError):
        verify(pki, f"{head}.{forged}.{sig}")


@pytest.mark.parametrize("token", ["", "a.b.c", "not a jws"])
def test_garbage_is_refused(token: str) -> None:
    with pytest.raises(storekit.StoreKitError):
        verify(make_pki(NOW), token)


def test_another_algorithm_or_a_short_chain_is_refused() -> None:
    pki = make_pki(NOW)
    with pytest.raises(storekit.StoreKitError, match="ES256"):
        verify(pki, pki.sign(transaction(), chain=[pki.leaf, pki.root]))
    hs = jwt.encode(
        transaction(), "secret-key-for-hs256-at-least-32-bytes", algorithm="HS256", headers={"x5c": []}
    )
    with pytest.raises(storekit.StoreKitError, match="ES256"):
        verify(pki, hs)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"bundleId": "com.example.other"}, "not for this app"),
        ({"productId": "com.example.pisa.coins"}, "not for this app"),
        ({"environment": "Sandbox"}, "environment"),
        ({"environment": "Xcode"}, "environment"),
        ({"expiresDate": int(NOW.timestamp() * 1000) - DAY_MS}, "expired"),
        ({"revocationDate": int(NOW.timestamp() * 1000) - DAY_MS}, "revoked"),
        ({"expiresDate": None}, "no expiry"),
    ],
)
def test_a_transaction_that_does_not_entitle_this_app_is_refused(
    changes: dict[str, Any], message: str
) -> None:
    pki = make_pki(NOW)
    with pytest.raises(storekit.StoreKitError, match=message):
        verify(pki, pki.sign(transaction(**changes)))


def test_sandbox_passes_only_when_allowed() -> None:
    pki = make_pki(NOW)
    token = pki.sign(transaction(environment="Sandbox"))
    assert verify(pki, token, environments=frozenset({"Production", "Sandbox"})).product_id == PRODUCT
