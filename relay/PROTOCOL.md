# The push relay's sending API

What a Pisa server sends the push relay, and how the relay checks it. CONTRACT-2 §9 pins the request (body,
`Content-Type`, the two headers, the signature, the window and the answers); where this page and §9 ever
differ, §9 wins. The relay's own API for the app (registration, its Live Activity tokens, the subscription's
devices) is at the end of this page, "The app's API".
The server side is `core/pisa/domain/push_seal.py` and `core/pisa/connectors/push_relay.py`.

The rule: the relay learns nothing about the user's life beyond what it must. It sees an opaque handle, the
time, size bucket and type of each push, and the device's APNs token. Never text, titles, names, places, or the
server's address. A Live Activity update carries nothing of the user's day either (below).

## `POST /v1/push`

Body (JSON, UTF-8):

| Field | Type | Meaning |
|---|---|---|
| `handle` | string, 1 to 64 characters | the handle the relay gave the app at registration |
| `type` | `alert`, `background` or `liveactivity` | the APNs push type |
| `collapse_id` | string, at most 64 characters, optional | the `apns-collapse-id` (a re-send replaces the earlier banner) |
| `sealed` | string, `alert` only | the sealed notice (below), base64url without padding |
| `activity` | object, `liveactivity` only | a Live Activity update without personal text (below) |

A `background` push carries neither `sealed` nor `activity`: the app wakes and fetches from its server.

### Headers and authentication

| Header | Value |
|---|---|
| `Content-Type` | `application/json` (the relay refuses anything else with 400; a `charset` parameter is allowed) |
| `X-Pisa-Relay-Timestamp` | the server's clock, Unix seconds, decimal digits only |
| `X-Pisa-Relay-Signature` | base64url without padding of HMAC-SHA256(key, `<timestamp>` + `\n` + `<body bytes>`) |

- `key` is SHA-256 of the 32-byte `secret` the relay returned at registration. The relay stores only
  `sha256(secret)`, which is exactly this key: it must be kept as secret as a key (encrypted at rest, never
  logged), because whoever holds it can sign pushes to that handle.
- The relay computes the HMAC over the body bytes exactly as received, and compares in constant time.
- Header names are matched case-insensitively, as HTTP requires (HTTP/2 sends them in lower case).
- **Replay window:** the relay refuses a timestamp more than 300 seconds before or after its own clock. It keeps
  every accepted signature for 10 minutes (one set for all handles; keyed by the signature's decoded bytes) and
  never acts on the same signature twice. A request whose signature was already accepted is the same request
  (the same second, the same body): the relay answers it **202 and does not send it again**, so two identical
  wakes in one second are one wake, and a retry is harmless. (§9 says such a signature is "refused": it is,
  in that nothing is sent; only the answer is 202 rather than 401.)
- **Order of checks:** the body and its schema (400, 413), the handle (410), the signature and the window (401),
  the subscription (410), the rate limit (429), then the replay set: a flood past the limit adds nothing to it.
- **Live Activity tokens are the handle's own.** A `liveactivity` push goes only to a token that the app
  registered for this handle (`POST /v1/register/activity`, signed with the app's own secret, below): a
  push-to-start token for `start`, an activity's update token for `update` and `end`. Any other token is
  answered 401, the same as a bad signature, and Apple is not asked.

### Answers

| Status | When | What the server does |
|---|---|---|
| 202 | handed to APNs (or queued for it) | counts the push as sent |
| 400 | the body is not as above | logs it |
| 401 | unknown signature, a timestamp outside the window, or a Live Activity token the app did not register for this handle | logs it |
| 410 | the relay no longer knows the handle (the app unregistered, its subscription ended, or Apple said the device token is gone) | deletes the device's relay registration |
| 404 | never sent by this relay (§9 allows it for a gone handle): a mistyped relay URL or a proxy | logs it; keeps the registration |
| 413 | the body is larger than 8 KiB | logs it; no retry, keeps the registration |
| 422 | `liveactivity` only: Apple says this handle's activity token is gone; the relay forgets the token, the handle stays | drops that activity's token only and keeps the registration (§9) |
| 429 | past the handle's rate limit | logs it; the notice stays in the app's list |
| 5xx | the relay or APNs failed (500: the relay's storage key does not open its data, an operator error; 502: Apple refused or was unreachable). This relay never sends 503, but a server treats any 5xx alike. Never a sign that the handle is gone | logs it; keeps the registration |

Response bodies are not read; the relay should not echo anything of the request.

## The sealed notice (`type: alert`)

HPKE (RFC 9180), base mode: DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20-Poly1305
(`kem_id 0x0020`, `kdf_id 0x0001`, `aead_id 0x0003`).

- The recipient key is the device's X25519 public key (32 bytes), which the app gave its server only
  (`PUT /v1/devices/me/push`). The relay never has it.
- `info` = `pisa-push/2` (ASCII); `aad` = the handle (UTF-8). A blob moved to another handle does not open.
- `sealed` = `enc` (32 bytes) followed by the ciphertext (plaintext length + 16), base64url without padding.
- The plaintext is UTF-8 JSON, padded with spaces (JSON whitespace) so that `enc` + ciphertext is **exactly 512
  bytes**, or **exactly 2048 bytes** when the plaintext does not fit in 464. The server cuts a longer notice
  first (its text, then its title, then optional fields), so every blob is one of the two sizes.
- The plaintext: `$defs/PushPlaintext`, `{"v": 2, "notice": <pisa.notice>, "sentAt", "apns": {"category",
  "threadId", "interruption", "askId"?, "why"?}}`. `notice` is contract 2's `$defs/s_pisa_notice`, the same
  object the live socket sends (CONTRACT-2 §9); `sentAt` is when the server sealed it (UTC, ending in Z: the
  extension drops a push older than 7 days, dated more than 5 minutes ahead, or repeating a `noticeId`, whose
  value is the stable id of the stored notice or approval); `apns` says how to file the banner.

The relay sends APNs a generic alert (`"Pisa"`, `"New notice"`) with `mutable-content: 1`, the
`collapse_id`, and `sealed` as a top-level key of the payload. The app's Notification Service Extension opens it
with the private key in the shared keychain and shows the notice (its `genericText` for health and finance, or
when `sensitive`, unless the user allowed full previews). If it cannot open it, it shows "Open Pisa to see it".

## A Live Activity update (`type: liveactivity`)

Apple renders a Live Activity itself, so nothing can decrypt it on the phone. In relay mode the update carries
nothing of the user's day (finding R5 of the first security review): no text, no time of a stop or a departure,
no progress of the day. It only starts, refreshes and ends the activity; the app shows the next stop and its
times once it is open, from its own connection. (Direct mode, with the user's own Apple key, sends the full
content.)

| Field | Meaning |
|---|---|
| `token` | the activity's push token (hex), or the push-to-start token for `start` |
| `event` | `start`, `update` or `end` |
| `priority` | 5 or 10 (`apns-priority`) |
| `timestamp` | Unix seconds (`aps.timestamp`) |
| `attributesType` | `start` only (`aps.attributes-type`; `aps.attributes` is `{}`) |
| `dismissalDate` | `end` only (`aps.dismissal-date`): the moment of sending |

Nothing else: `progress`, `time`, `staleDate` (each a fact of the user's day) or any other field is refused
with 400 (CONTRACT-2 `relay.liveActivityRelayModeRefused`). The relay sends APNs, on the app's
`.push-type.liveactivity` topic, `aps` = `{timestamp, event, content-state: {}}`, plus, for `start` only,
`attributes-type` (from the request's `attributesType`), `attributes: {}` (push-to-start needs them; the app's
attributes type has no fields) and `input-push-token: 1`, and for `end` only `dismissal-date`
(`relay.liveActivityApsRelayMode`). Update and end never carry `attributes` or `attributes-type`. APNs
requires a content-state; the empty one leaves the widget's last values in place until the app is opened and
fills them in.

## `DELETE /v1/register` from the server

The app forgets itself with the relay's own registration API. The server also does it, as the app would, when a
device is unpaired (`DELETE /v1/devices/me`), revoked (from another device or `pisa devices revoke`), or leaves
relay mode (`PUT /v1/devices/me/push` with `none`, `direct` or another handle): body `{"handle"}`, signed with
the same three headers as a push (finding R13 of the first security review). Best effort: a failure is logged,
the server's row is deleted anyway, and the relay ends the row at its expiry.

## The app's API (the relay's own; not part of CONTRACT-2 §9)

The app talks to the relay directly, never through its server. Two secrets come out of registration: `secret`,
which the app hands its server (`PUT /v1/devices/me/push`, §9) and which signs pushes; and `app_secret`, which
**the app keeps and never sends anywhere**, not even to its own server. Only `app_secret` can bind a Live
Activity token to the handle or see and remove the subscription's devices, so a server (or whoever has taken
one) cannot reach a phone that did not ask for it.

### `POST /v1/register`

Unsigned (the App Store's signature on the transaction is the credential). `Content-Type: application/json`.

| Field | Type | Meaning |
|---|---|---|
| `apns_token` | hex, 64 to 200 characters | the phone's APNs device token |
| `environment` | `production` or `sandbox` | the APNs environment of the build |
| `signed_transaction` | string, JWS | the subscription's current transaction (`Transaction.jwsRepresentation`), signed by the App Store within the last 7 days |
| `signed_renewal_info` | string, JWS, optional | the renewal info, when the subscription is in Apple's billing grace period |
| `account_key` | base64url without padding, 32 bytes, required when the transaction carries an `appAccountToken` | the app's account key (below) |
| `language` | BCP 47 tag, optional | only `en` or `it` is kept, for the generic banner |

`201`:

```json
{"handle": "<22 characters>", "secret": "<43 characters>", "app_secret": "<43 characters>"}
```

`handle` is 16 random bytes, `secret` and `app_secret` are 32 random bytes each, all base64url without padding.
The relay keeps `SHA-256(secret)` and `SHA-256(app_secret)` (sealed), never the secrets.

Errors (`{"error": "<code>"}`): `400 malformed`; `403 subscription_not_verified`; `403 account_not_verified`
(the transaction carries an `appAccountToken` and `account_key` is missing or does not derive it);
`409 too_many_devices`; `413 too_large`; `429 too_many_registrations`.

**The account key and `appAccountToken`.** The app makes one random 32-byte *account key* per Apple account and
keeps it in the iCloud Keychain (synchronized, so every phone of the user has it). When it sells the
subscription it sets the purchase's `appAccountToken` (`Product.PurchaseOption.appAccountToken`) to:

```
d = SHA-256("pisa-relay/app-account-token\n" ‖ account_key)[0:16]
d[6] = (d[6] & 0x0F) | 0x80        # UUID version 8 (RFC 9562)
d[8] = (d[8] & 0x3F) | 0x80        # RFC 9562 variant
appAccountToken = UUID(d), compared in lower case
```

The label is the ASCII bytes shown, `\n` a single line feed. Vector: `account_key` = bytes 0x00 to 0x1f gives
`55ba834e-40eb-8f96-88a3-4ac1fd42cf59`. Apple signs the token into every transaction of that subscription, so a
registration must prove the key behind it: a copy of the transaction is no longer enough to register. A
purchase made without an `appAccountToken` (for example before the app sets one) registers without the key.

### Requests the app signs

`DELETE /v1/register`, `POST /v1/register/activity`, `POST /v1/register/devices` and
`POST /v1/register/devices/remove`. Same headers as a push (`Content-Type: application/json`,
`X-Pisa-Relay-Timestamp`, `X-Pisa-Relay-Signature`), the same window (±300 s) and replay set, and the handle's
rate limit; but the MAC also covers the method and the path, so a signed request cannot be replayed to another
route:

```
key       = SHA-256(app_secret)
message   = "pisa-relay-app/1\n" ‖ METHOD ‖ " " ‖ PATH ‖ "\n" ‖ timestamp ‖ "\n" ‖ body bytes
signature = base64url_nopad(HMAC-SHA256(key, message))
```

`METHOD` is upper case (`POST`, `DELETE`) and `PATH` the request path exactly as sent (no host, no query), for
example `POST /v1/register/activity`. No other key derivation is involved. The server's construction (§9:
`timestamp ‖ "\n" ‖ body` under `SHA-256(secret)`) is not accepted on these routes, except on
`DELETE /v1/register`, which accepts either: the server may end its own handle (when the user unpairs or
revokes a device), and ending a handle harms no one else.

| Route | Body | Signed by | Answer |
|---|---|---|---|
| `DELETE /v1/register` | `{handle}` | `app_secret`, or `secret` with §9's construction | `204`; `410` unknown handle; `401` |
| `POST /v1/register/activity` | `{handle, token, kind}`: `token` hex, 64 to 512 characters; `kind` `start` (push-to-start) or `update` (a running activity) | `app_secret` | `204` (the last 4 tokens of each kind are kept, as SHA-256) |
| `POST /v1/register/devices` | `{handle}` | `app_secret` | `200` (below) |
| `POST /v1/register/devices/remove` | `{handle, device}`: `device` an `id` from the list | `app_secret` | `204`; `404 unknown_device`; `403 not_allowed` |

`POST /v1/register/devices` answers the subscription's registrations (the subscription of the signing handle,
in the same StoreKit environment):

```json
{"devices": [{"id": "<16 characters>", "created": "2026-11-18", "last_used": "2026-11-20",
              "environment": "production", "this_device": true}]}
```

`id` is an opaque per-registration identifier (base64url of the first 12 bytes of an HMAC of the handle under a
relay key): it is not the handle and cannot be used to push. `last_used` is `null` before the first push. Dates
are UTC days.

`remove` ends the named registration if one of these holds (`relay.appApi.removalRule`): the signing
registration proved the account key; the target is the signing registration itself; or the target has had no
push (and no registration) for 30 days. Otherwise `403 not_allowed`. Which registration came first decides
nothing (finding N3b of the first security review's re-check): without the account key, a copy of the
transaction may be the older one, and must not be able to remove the subscriber's phone in use, nor the other
way round.

## Test vectors

`core/tests/test_push_relay.py` checks the HPKE suite against RFC 9180 appendix A.2.1 (base mode, the same
suite), a round trip with a test key, that a blob opens only with the matching key and handle, that a changed
byte is refused, the two sizes, the signature, and the replay window against an in-process fake relay.
