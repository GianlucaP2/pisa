"""Push: how notices reach a client that is not connected, and how a client opens a sealed one.

    python push.py [--state FILE]

It registers each push mode in turn and reads it back, then reads what changed with `GET /v1/changes` (what a
client without push does when it comes back). `vectors.py` checks `open_sealed` against the contract's vectors.
Needs `pip install pyhpke` for the sealed push.
"""

import asyncio
import json
import os
import sys
from typing import Any

from pisa_example import State, arguments, b64url, http_client, unb64
from session import Session


# [guide:register]
async def register_push(session: Session, mode: dict[str, Any]) -> dict[str, Any]:
    """Set how notices reach this device (a new mode replaces the old one), then read it back."""
    await session.request("PUT", "/v1/devices/me/push", json=mode)
    return await session.request("GET", "/v1/devices/me/push")  # {mode, since}; never the secret or token


def relay_mode(handle: str, secret: bytes, x25519_public: bytes) -> dict[str, Any]:
    """Relay mode: what the relay answered your registration (handle, secret), and the key notices are sealed to."""
    return {"mode": "relay", "handle": handle, "secret": b64url(secret), "public_key": b64url(x25519_public)}


def direct_mode(apns_token_hex: str, sandbox: bool) -> dict[str, Any]:
    """Direct mode: your own build's APNs device token; the server needs your own Apple key."""
    return {"mode": "direct", "token": apns_token_hex, "environment": "sandbox" if sandbox else "production"}


# [/guide:register]


# [guide:open]
def open_sealed(sealed: str, x25519_private: bytes, handle: str) -> dict[str, Any]:
    """A relay push's `sealed` field: HPKE base mode, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256,
    ChaCha20-Poly1305, info "pisa-push/2", aad the handle; 32 bytes of encapsulated key, then the ciphertext.
    The plaintext is `{"v": 2, "notice": <pisa.notice>, "sentAt", "apns"?}` padded with spaces."""
    from pyhpke import AEADId, CipherSuite, KDFId, KEMId

    suite = CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.CHACHA20_POLY1305)
    raw = unb64(sealed)
    if len(raw) not in (512, 2048):
        raise ValueError("a sealed push is exactly 512 or 2048 bytes")
    context = suite.create_recipient_context(
        raw[:32], suite.kem.deserialize_private_key(x25519_private), b"pisa-push/2"
    )
    return json.loads(context.open(raw[32:], handle.encode()))


# [/guide:open]


async def main() -> int:
    args = arguments(__doc__ or "").parse_args()
    state = State.load(args.state)
    async with http_client(state) as http:
        session = Session(http, state)
        # Synthetic values: a real relay handle and secret come from the relay's registration (App Store app
        # only), a real APNs token from iOS. The server stores what it is given; it never calls out here.
        modes = [
            relay_mode(f"h-example-{os.urandom(4).hex()}", os.urandom(32), os.urandom(32)),
            direct_mode(os.urandom(32).hex(), sandbox=True),
            {"mode": "none"},
        ]
        failures = 0
        for mode in modes:
            got = await register_push(session, mode)
            print(f"push mode {mode['mode']}: read back {got}")
            failures += got["mode"] != mode["mode"]
        # [guide:changes]
        changes = await session.request("GET", "/v1/changes")  # ?since=<RFC 3339>, default 7 days
        for change in changes["changes"]:
            print("changed:", json.dumps(change, ensure_ascii=False)[:200])
        # [/guide:changes]
        print(f"GET /v1/changes: {len(changes['changes'])} changes")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
