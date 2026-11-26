# Push

While a client has `/v2/live` open, everything Pisa says unasked arrives there (`pisa.notice` on the notices
thread). Push is for when it is closed. A device registers one of three modes with `PUT /v1/devices/me/push`; a
new mode replaces the old one, and `GET /v1/devices/me/push` reads back `{mode, since}` (never the secret or the
token). Both answer 204 / 200 whenever the request is valid, even while the server has no push configured.

| Mode | For | What the device gives the server | What the server needs |
|---|---|---|---|
| `relay` | an app whose publisher runs a relay (none is offered by the PISA project today) | the relay's `handle` (up to 64 characters) and `secret` (32 bytes), and the device's X25519 `public_key` (32 bytes raw), both base64url | `PISA_PUSH_RELAY_URL` |
| `direct` | your own iOS build | its APNs device `token` (hex) and `environment` (`production` or `sandbox`) | your own Apple key: `PISA_APNS_TEAM_ID`, `PISA_APNS_TOPIC`, `pisa secrets set-apns` ([Notifications](../notifications.md)) |
| `none` | everything else | nothing | nothing |

**Both push modes are Apple Push Notification service.** A relay registers only the app its publisher runs it
for (it verifies that app's App Store subscription), and none is offered by the PISA project today, so a client
of your own uses `direct` with its own Apple key, or `none`. There is no Android (FCM), web push or UnifiedPush mode today. A client in `none` mode
keeps the socket open while it runs and catches up when it comes back:
`GET /v1/changes?since=<RFC 3339>` (Pisa's own changes, each with its reason and its Undo), `GET /v1/approvals`,
`GET /v1/check-ins`.

Example: [`push.py`](examples/push.py).

<!-- include: examples/push.py#register -->
```python
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
```

<!-- include: examples/push.py#changes -->
```python
changes = await session.request("GET", "/v1/changes")  # ?since=<RFC 3339>, default 7 days
for change in changes["changes"]:
    print("changed:", json.dumps(change, ensure_ascii=False)[:200])
```

## What a relay push carries

The server never sends readable content through the relay. It seals the notice to the device's X25519 key:
HPKE (RFC 9180) base mode, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20-Poly1305, `info` `pisa-push/2`,
`aad` the relay handle. `sealed` is the 32-byte encapsulated key followed by the ciphertext, base64url, exactly
512 or 2048 bytes before encoding. The plaintext (`$defs/PushPlaintext`) is
`{"v": 2, "notice": <the same object as pisa.notice>, "sentAt": "<RFC 3339, UTC>", "apns"?: {category,
threadId, interruption}}`, padded with spaces. Drop a push older than 7 days, dated more than 5 minutes ahead, or
repeating a `noticeId` already seen.

<!-- include: examples/push.py#open -->
```python
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
```

`vectors.py` opens both of the contract's sealed vectors (`pairing/vectors.json`, `push`). The relay's own API
(registration, the server's signed requests) is in `relay/PROTOCOL.md`.

Buttons on a notice (Undo, a check-in's answer, an approval's decision) go straight to the server over REST:
`POST /v1/check-ins/{checkInId}/answer {choice}`, the undo route the notice names, and
`POST /v1/approvals/{id}/resume` with the header `X-Pisa-Origin: notification`.

## Run it

```text
$ python push.py
push mode relay: read back {'mode': 'relay', 'since': '2026-11-18T21:12:22.903693+00:00'}
push mode direct: read back {'mode': 'direct', 'since': '2026-11-18T21:12:22.913641+00:00'}
push mode none: read back {'mode': 'none', 'since': '2026-11-18T21:12:22.922721+00:00'}
GET /v1/changes: 0 changes
```

(The handle, secret and token there are random: the server stores them and sends nothing on its own.)
