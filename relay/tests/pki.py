# SPDX-License-Identifier: AGPL-3.0-only
"""A test App Store PKI, generated in-test: a root, an intermediate with Apple's WWDR OID, a leaf with the App
Store receipt-signing OID, and JWS signed the way the App Store signs them (ES256, `x5c` leaf first)."""

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from pisa_relay.storekit import INTERMEDIATE_OID, LEAF_OID


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _cert(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: str,
    issuer_key: ec.EllipticCurvePrivateKey,
    now: datetime,
    *,
    ca: bool,
    oid: x509.ObjectIdentifier | None,
    days: int = 365,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if oid is not None:
        builder = builder.add_extension(x509.UnrecognizedExtension(oid, b"\x05\x00"), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


@dataclass
class Pki:
    root: x509.Certificate
    root_key: ec.EllipticCurvePrivateKey
    intermediate: x509.Certificate
    intermediate_key: ec.EllipticCurvePrivateKey
    leaf: x509.Certificate
    leaf_key: ec.EllipticCurvePrivateKey

    def chain(self) -> list[x509.Certificate]:
        return [self.leaf, self.intermediate, self.root]

    def sign(
        self,
        payload: dict[str, Any],
        chain: list[x509.Certificate] | None = None,
        key: ec.EllipticCurvePrivateKey | None = None,
    ) -> str:
        x5c = [base64.b64encode(c.public_bytes(Encoding.DER)).decode() for c in (chain or self.chain())]
        return jwt.encode(payload, key or self.leaf_key, algorithm="ES256", headers={"x5c": x5c})


def make_pki(
    now: datetime,
    *,
    root_name: str = "Test Root CA",
    intermediate_oid: bool = True,
    leaf_oid: bool = True,
    leaf_days: int = 365,
) -> Pki:
    root_key = ec.generate_private_key(ec.SECP384R1())
    root = _cert(root_name, root_key, root_name, root_key, now, ca=True, oid=None, days=3650)
    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter = _cert(
        "Test WWDR",
        inter_key,
        root_name,
        root_key,
        now,
        ca=True,
        oid=INTERMEDIATE_OID if intermediate_oid else None,
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert(
        "Test App Store",
        leaf_key,
        "Test WWDR",
        inter_key,
        now,
        ca=False,
        oid=LEAF_OID if leaf_oid else None,
        days=leaf_days,
    )
    return Pki(root, root_key, inter, inter_key, leaf, leaf_key)
