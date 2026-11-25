# SPDX-License-Identifier: AGPL-3.0-only
"""App Store signed data (JWS): a subscription's transaction, and App Store Server Notifications V2.

Apple signs both with ES256 and puts the chain in the header's `x5c`: leaf, intermediate, Apple Root CA - G3.
The rules, as Apple's App Store Server Library applies them:
- exactly three certificates, and the last one is the pinned Apple root, byte for byte (never trusted from the
  request);
- each certificate is directly issued by the next one, and valid now;
- the intermediate is a CA with Apple's WWDR OID (1.2.840.113635.100.6.2.1);
- the leaf is not a CA, carries the App Store receipt-signing OID (1.2.840.113635.100.6.11.1), has an EC P-256
  key, and the JWS verifies with that key.
Only then is the payload read.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

import jwt
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding

LEAF_OID = x509.ObjectIdentifier("1.2.840.113635.100.6.11.1")
INTERMEDIATE_OID = x509.ObjectIdentifier("1.2.840.113635.100.6.2.1")


class StoreKitError(ValueError):
    """Signed data that is not Apple's, or that does not entitle this app to the relay."""


def apple_root() -> x509.Certificate:
    """Apple Root CA - G3 (SHA-256 63:34:3A:BF:...:3E:91:79), from apple.com/certificateauthority."""
    pem = (resources.files("pisa_relay") / "AppleRootCA-G3.pem").read_bytes()
    return x509.load_pem_x509_certificate(pem)


@dataclass(frozen=True)
class Transaction:
    original_transaction_id: str
    product_id: str
    expires_at: int  # Unix seconds
    environment: str  # StoreKit's: Production or Sandbox
    app_account_token: str | None = None  # the UUID the app set at purchase, lower case


@dataclass(frozen=True)
class Renewal:
    """Signed renewal info: what matters here is Apple's billing grace period, when it is on."""

    original_transaction_id: str
    environment: str
    grace_expires_at: int | None  # Unix seconds


@dataclass(frozen=True)
class Notification:
    kind: str
    subtype: str
    transaction: Transaction | None
    renewal: Renewal | None


def _check_cert(cert: x509.Certificate, issuer: x509.Certificate, now: datetime) -> None:
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature):
        raise StoreKitError("A certificate in the chain is not issued by the next one.") from None
    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
        raise StoreKitError("A certificate in the chain is not valid now.")


def _has(cert: x509.Certificate, oid: x509.ObjectIdentifier) -> bool:
    try:
        cert.extensions.get_extension_for_oid(oid)
    except x509.ExtensionNotFound:
        return False
    return True


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        return False


def verify_jws(token: str, root: x509.Certificate, now: datetime) -> dict[str, Any]:
    """The payload of an App Store JWS, after its chain and signature are checked."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise StoreKitError("Not a JWS.") from exc
    chain: object = header.get("x5c")
    if header.get("alg") != "ES256" or not isinstance(chain, list) or len(chain) != 3:  # pyright: ignore[reportUnknownArgumentType]
        raise StoreKitError("Not an App Store JWS (ES256 with a three-certificate x5c).")
    try:
        certs = [x509.load_der_x509_certificate(base64.b64decode(str(c), validate=True)) for c in chain]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    except (binascii.Error, ValueError) as exc:
        raise StoreKitError("The x5c certificates do not parse.") from exc
    leaf, intermediate, top = certs
    if top.public_bytes(Encoding.DER) != root.public_bytes(Encoding.DER):
        raise StoreKitError("The chain does not end in Apple's root certificate.")
    _check_cert(intermediate, root, now)
    _check_cert(leaf, intermediate, now)
    if not _is_ca(intermediate) or not _has(intermediate, INTERMEDIATE_OID):
        raise StoreKitError("The intermediate certificate is not Apple's WWDR intermediate.")
    if _is_ca(leaf) or not _has(leaf, LEAF_OID):
        raise StoreKitError("The leaf certificate is not an App Store signing certificate.")
    key = leaf.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise StoreKitError("The leaf key is not EC P-256.")
    try:
        payload: dict[str, Any] = jwt.decode(token, key, algorithms=["ES256"], options={"verify_aud": False})
    except jwt.PyJWTError as exc:
        raise StoreKitError("The JWS signature does not verify.") from exc
    return payload


def _ms(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def transaction(
    token: str, *, root: x509.Certificate, now: datetime, bundle_id: str, products: frozenset[str],
    environments: frozenset[str], require_active: bool = True, grace_until: int | None = None,
    max_age_s: int | None = None,
) -> Transaction:  # fmt: skip
    """A signed transaction that entitles this app to the relay: our bundle, one of our products, an allowed
    environment, signed within `max_age_s` (when given), and, when `require_active`, not revoked and not
    expired (an expiry inside Apple's billing grace, `grace_until`, still counts as active)."""
    data = verify_jws(token, root, now)
    if data.get("bundleId") != bundle_id or data.get("productId") not in products:
        raise StoreKitError("The transaction is not for this app's subscription.")
    if data.get("environment") not in environments:
        raise StoreKitError("The transaction's environment is not accepted.")
    expires_ms, original, signed_ms = (
        _ms(data.get("expiresDate")),
        data.get("originalTransactionId"),
        _ms(data.get("signedDate")),
    )
    if expires_ms is None or not isinstance(original, str) or not original:
        raise StoreKitError("The transaction has no expiry or original transaction id.")
    stamp = now.timestamp()
    if max_age_s is not None and (
        signed_ms is None or not stamp - max_age_s <= signed_ms / 1000 <= stamp + 300
    ):
        raise StoreKitError("The transaction was not signed recently; send a fresh one.")
    if require_active and (
        data.get("revocationDate") is not None or max(expires_ms // 1000, grace_until or 0) <= stamp
    ):
        raise StoreKitError("The subscription has expired or was revoked.")
    account_raw = data.get("appAccountToken")
    account = account_raw.lower() if isinstance(account_raw, str) and account_raw else None
    return Transaction(
        original, str(data["productId"]), expires_ms // 1000, str(data["environment"]), account
    )


def renewal(token: str, *, root: x509.Certificate, now: datetime, environments: frozenset[str]) -> Renewal:
    """Signed renewal info (`JWSRenewalInfo`)."""
    data = verify_jws(token, root, now)
    original, environment = data.get("originalTransactionId"), data.get("environment")
    if not isinstance(original, str) or not original or environment not in environments:
        raise StoreKitError("The renewal info is not for an accepted subscription.")
    grace_ms = _ms(data.get("gracePeriodExpiresDate"))
    return Renewal(original, str(environment), grace_ms // 1000 if grace_ms is not None else None)


def notification(
    signed_payload: str, *, root: x509.Certificate, now: datetime, bundle_id: str, products: frozenset[str],
    environments: frozenset[str],
) -> Notification:  # fmt: skip
    """An App Store Server Notification V2: its type, subtype and, when it has them, its (verified)
    transaction and renewal info."""
    payload = verify_jws(signed_payload, root, now)
    kind, subtype, data = payload.get("notificationType"), payload.get("subtype"), payload.get("data")
    if not isinstance(kind, str):
        raise StoreKitError("The notification has no type.")
    subtype = subtype if isinstance(subtype, str) else ""
    if not isinstance(data, dict):
        return Notification(kind, subtype, None, None)  # e.g. a summary of a mass renewal extension
    if data.get("bundleId") != bundle_id or data.get("environment") not in environments:  # pyright: ignore[reportUnknownMemberType]
        raise StoreKitError("The notification is not for this app.")
    signed = data.get("signedTransactionInfo")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(signed, str) or verify_jws(signed, root, now).get("productId") not in products:
        return Notification(kind, subtype, None, None)  # none, or another of the app's products
    tx = transaction(signed, root=root, now=now, bundle_id=bundle_id, products=products,
                     environments=environments, require_active=False)  # fmt: skip
    signed_renewal = data.get("signedRenewalInfo")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    info = None
    if isinstance(signed_renewal, str):
        info = renewal(signed_renewal, root=root, now=now, environments=environments)
        if info.original_transaction_id != tx.original_transaction_id or info.environment != tx.environment:
            raise StoreKitError("The renewal info is not for this transaction.")
    return Notification(kind, subtype, tx, info)
