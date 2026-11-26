# Push relay: threat model

For an independent reviewer who will read the code next. The service is `pisa_relay/` (about 1,000 lines, a
third of them comments): `app.py` (routes, request checks), `storekit.py` (App Store JWS), `apns.py` (Apple),
`storage.py` (SQLite), `settings.py`. The server side it talks to is `core/pisa/domain/push_seal.py` and
`core/pisa/connectors/push_relay.py`; the request is pinned by CONTRACT-2 §9 and described in `PROTOCOL.md`. The
first security review (an independent one) and what came of each finding are at the end; its proofs of concept
run, inverted, in `tests/test_e1_review.py`.

## What the relay is for, in one paragraph

A self-hosted Pisa server cannot hold the App Store app's APNs key. The app registers with the relay (proving a
subscription with an App Store JWS) and gets a `handle` and a 32-byte `secret`. It gives both, plus a fresh
X25519 public key, to its own server only. The server seals each notice to that public key (HPKE, `aad` = the
handle), pads it to 512 or 2048 bytes, and sends `{handle, type, sealed}` to the relay, signed with
HMAC-SHA256 under `sha256(secret)`. The relay checks the signature, the time and the rate, and sends Apple a
generic alert with the blob; the phone's Notification Service Extension opens it.

## Assets

| Asset | Where | Why it matters |
|---|---|---|
| A1. Notice content | sealed on the user's server; opened on the phone | the user's life. The relay must never be able to read it |
| A2. The APNs auth key (.p8) | the relay's host, mounted file | lets anyone push any content to every user of the app |
| A3. Per-handle HMAC keys (`sha256(secret)`) | SQLite, AES-256-GCM-sealed under a key derived from `RELAY_STORAGE_KEY` | let someone push to that phone as if from the user's server |
| A4. `RELAY_STORAGE_KEY` | the platform's secret store | unseals A3 and keys the subscription references |
| A5. APNs device tokens, Live Activity token hashes | SQLite | address a phone (useful only with A2) |
| A6. Metadata: when and how often a handle is pushed, its type and size bucket; when a Live Activity starts, updates and ends | in transit; counters | reveals rhythms of a user's life (a burst at 3 a.m.) |
| A7. The relay's availability, and every server's registration | | if the relay is down, or tells servers a handle is gone, notifications stop |
| A8. The subscription's value | | registration is the paid feature; it should need a valid subscription |

## Attackers

- **T1. Anyone on the internet**, sending requests to the public endpoints.
- **T2. A network observer** between the server and the relay, or the relay and Apple (TLS in both legs).
- **T3. Someone with a copy of the database** (a stolen backup, a misconfigured volume) but not the key.
- **T4. A full compromise of the relay host** (code execution, the environment, the APNs key).
- **T5. The relay operator** (the app vendor), curious or compelled.
- **T6. A malicious or compromised Pisa server** (someone else's, or the user's own after a compromise).
- **T7. Someone holding a copy of a user's signed transaction** (it is not secret: it is on the phone and in
  any proxied app's traffic), or a subscriber sharing theirs.
- **T8. Apple**, which sees every push as it would for any app.
- **T9. The operator's own mistakes**: a wrong storage key, a missing file, a scaled-out deployment.

## Threats and mitigations

### Reading notices (A1)
- **T2, T3, T4, T5**: the blob is HPKE-sealed on the server to a key only the phone has; the relay never receives
  that key (it goes server-ward over the paired connection). The relay cannot open it, and the tests assert the
  relay forwards the blob byte for byte and adds no content of its own (`test_interop.py`,
  `test_an_alert_is_a_generic_banner_with_the_sealed_blob_and_nothing_else`).
- Sizes are fixed (512 or 2048 bytes; the relay refuses any other size), so the length reveals at most "short or
  long".
- **Live Activities** cannot be sealed (Apple renders them), so in relay mode they carry nothing of the user's
  day (finding R5 of the first security review, CONTRACT-2 `relay.liveActivityRelayMode`). The schema
  (`Activity` in `app.py`) admits a hex token, an event, a priority and the push's own times (`timestamp`, and
  `dismissalDate` for `end`), with `extra="forbid"` and strict types: `progress`, `time`, `staleDate` or a title
  is refused, not forwarded. `attributes-type` must equal the configured type name. APNs gets an empty
  content-state (and, for a start only, `attributes: {}`, which push-to-start needs).
- `collapse_id` is limited to `[A-Za-z0-9_.:-]{1,64}` so it cannot carry a sentence (the server uses the notice's
  UUID).

### Forging or replaying pushes (A3)
- HMAC-SHA256 over `<timestamp>\n<body bytes as received>`, compared in constant time. The timestamp must be
  digits only (no `+`, spaces or underscores that `int()` would accept), within ±300 s of the relay's clock.
- Every accepted signature is stored for 600 s (twice the window), keyed by its **decoded bytes** (the same MAC
  spelled with different unused base64 bits is the same signature). The insert is the check (primary key), so
  two concurrent copies cannot both act. A repeat is answered 202 and **not sent again**: it can only be the
  same request (the same second and body), so the answer is idempotent, a legitimate second identical wake is
  not a failure, and a replayer gains nothing.
- The rate limit is checked **before** the replay insert, so a signed flood past the limit adds no rows (at most
  `push_per_minute` × 10 rows per handle).
- `DELETE /v1/register` and `POST /v1/register/activity` are signed and checked the same way.
- Residual: whoever holds the secret (the app and the user's server) or the unsealed key can push to that one
  phone. Without the phone's public key (which only the user's server has), such a push is a generic banner
  whose blob does not open: the phone shows "Open Pisa to see it". See T6 for the server itself.

### Reaching another phone (T6, T7)
- Alerts and background pushes go only to the APNs token registered with the handle.
- **Live Activity pushes** go to a token named in the request, so the relay binds those tokens to the handle:
  the app registers each of its own Live Activity tokens (`POST /v1/register/activity`, signed with its own
  `app_secret`, `kind` `start` for push-to-start and `update` for a running activity). A push goes only to a registered token
  of the right kind; any other is answered 401, the same as a bad signature, and Apple is not asked, so the
  answer says nothing about a foreign token. When Apple says a registered token is gone, the relay forgets it
  and answers 422 (it is the handle's own token, so this is not an oracle). The relay keeps four of each kind
  per handle, as SHA-256 hashes.
- **Two secrets** (the first security review's re-check, finding N2): registration returns `secret`, which the
  app hands its server and which signs pushes, and `app_secret`, which the app keeps and never sends anywhere.
  Binding a Live Activity token needs `app_secret`, so the user's server, or whoever has taken it, cannot bind
  another phone's token to its handle; it can push only to tokens the app itself bound. The app's MAC also
  covers the method and the path (`pisa-relay-app/1`), so none of its signed requests can be replayed to another
  route, and `app_secret` never signs a push. `DELETE /v1/register` accepts either secret: the server may end
  its own handle (when the user unpairs a device), which harms no one else.
- Residual: whoever takes `app_secret` from the phone (a jailbroken device, a backup that includes the
  Keychain item) can bind tokens to that handle. The app should keep it in the Keychain as this-device-only,
  not synchronized.
  Binding tokens to the device itself would need App Attest.

### Database theft (T3)
- HMAC keys are sealed (AES-256-GCM under an HKDF-derived key, a fresh nonce, the handle as associated data so
  a sealed key cannot be moved to another row). The original transaction id is stored only as HMAC-SHA256 of
  `environment\nid` under another HKDF-derived key, so it cannot be brute-forced from the database alone
  (transaction ids are sequential numbers; a plain hash would not protect them).
- What a thief does get: handles, APNs tokens, Live Activity token hashes, environments, languages, expiry
  dates (which reveal renewal dates) and hour-granular created/last-used times. Tokens are useless without the
  APNs key (A2).

### Host compromise (T4)
- Game over for A2: the attacker can push arbitrary visible content, with any text and without
  `mutable-content` (so the extension never runs), to every phone of the app: phishing every user. That is
  inherent to holding an APNs key. Mitigations are operational: the key file read-only and owned by the service
  user, the container non-root, the key revocable in the Apple developer account (and rotated if in doubt), an
  alert on unusual APNs volume, the relay host separate from everything else.
- `sha256(secret)` at rest is as good as the secret (it *is* the HMAC key, as §9 says): with the database and
  the storage key, an attacker can sign pushes for any handle. An asymmetric design (the server signs with
  Ed25519, the relay keeps the public key) would make that leak harmless; it needs a contract change (§9 is
  frozen) and is proposed for contract 3.
- Even so, past and future notice **content** stays sealed (A1): the relay never had the phone's key.
- An attacker on the host could start logging timing and addresses (A6) from then on.

### The operator (T5)
- The code is public (AGPL); the operator can still run something else. What the user can check from outside: the
  phone only ever shows content it decrypted, so a relay that tampers with blobs only produces "Open Pisa to see
  it". The operator can always see timing (A6) and delete registrations (A7). It cannot read notices or learn the
  server's address from the protocol (the server's IP reaches the relay at the TCP level; the code does not
  record it and the README requires the proxy not to log it).
- A sealed notice carries no time, so the operator (or anyone with the `.p8` who recorded blobs) can re-deliver
  an old blob and the phone shows it as new. Actions still go to the server, which checks state. The fix is on
  the app (a seen-set of notice ids in the extension) and in a later contract (`sentAt` in the plaintext); the
  relay adds nothing a replay could be told apart by.

### A malicious Pisa server (T6)
- A server can only push to handles whose secret it holds, i.e. the phones that registered with it, and to their
  own Live Activity tokens. The rate limits (60/min, 2000/day per handle, configurable) cap the annoyance; a
  minute's refusals do not consume the day's quota.
- Its blobs are opaque to the relay, so a server could send any 512/2048-byte blob; the phone's extension opens
  only what is sealed to its key with its handle as `aad` (a blob for another handle does not open).
- It cannot reach other handles: the HMAC key is per handle and the handle is in the signed body.

### Registration abuse (T1, T7, A8)
- `POST /v1/register` needs a JWS whose chain ends in the **pinned** Apple Root CA - G3, byte for byte (the
  request's own root is never trusted), with Apple's WWDR intermediate OID (CA) and the App Store leaf OID
  (not a CA, EC P-256), each certificate directly issued by the next and valid now, signature ES256 only (no
  `alg` confusion: the header's alg must be ES256 before any key is used, and the key comes only from the
  verified leaf). Then: our bundle id, one of our product ids, an accepted environment (`Production` on the
  production relay; `Xcode` never), not revoked, not expired, and **signed within 7 days** (`signedDate`).
- A refund or a revocation is remembered per subscription reference and period, so a transaction signed before
  it (which has no `revocationDate`) does not register again; a renewal after it has a later expiry and does.
- **Rate limits**: 5 registrations an hour per subscription reference, counted only after the transaction has
  verified. There is **no limit across all clients** (the first security review's re-check, findings N1 and
  N1b): the relay cannot tell clients apart, so such a limit was an off switch for anyone (junk first, then any
  request carrying Apple's public root in its `x5c`), while full verification costs about a millisecond and
  needs no cap. Refused requests, however many, never stop a real subscriber from registering.
- Residual: a client can still make the relay do signature work as fast as it can send requests. **A per-client
  limit at the reverse proxy is a deployment requirement** (README, with Caddy and nginx examples), keyed on the
  client's /64 for IPv6 and /32 for IPv4: an IPv6 host has a whole /64 to pick addresses from.
- **No eviction of a phone in use**: a subscription holds at most 10 handles. At the cap, a new registration
  replaces the least recently used handle only if it has been idle (no push, no registration) for 30 days;
  otherwise it is refused with 409. Use is kept to the hour. Why 30 days: a Pisa server in normal use pushes a
  phone most days (notices, reminders, background wakes), so a handle quiet for a month belongs to a phone
  that is off, wiped or given away, while a fortnight's holiday with the phone off keeps its handle. Deleted
  apps do not hold slots for long either: Apple answers 410 to the next push and the handle ends.
- **The account key** (the first security review's re-check, finding N3): the app keeps one random 32-byte
  account key per Apple account in the iCloud Keychain and sets each purchase's `appAccountToken` to a UUID
  derived from it (PROTOCOL.md). Apple signs that token into every transaction of the subscription, so the relay
  requires the key behind it: a copy of the transaction no longer registers (403 `account_not_verified`), and
  cannot spend the subscription's hourly limit either (the key is checked before that counter).
- **The subscription's devices** (N3): any registered phone of the subscription can list the subscription's
  registrations (`POST /v1/register/devices`, signed with its `app_secret`; opaque ids, days only) and remove
  one (`/remove`). A phone that proved the account key may remove any; any phone may remove itself, or one idle
  for 30 days. Which was registered first decides nothing (the first security review's re-check, finding N3b):
  an order rule always lets one side win, and a copy can be the older side, so it would remove the subscriber's
  new phone.
- Residual (T7): a purchase without an `appAccountToken` (made before the app sets one) still registers from a
  copy: the copier can fill the free slots (5 an hour, so 10 in two hours), burn the hourly limit, and keep
  its copies in use, so the subscriber's new phone gets 409 until the copies go idle (30 days), a refund, or
  the subscription's end. The subscriber's phones in use cannot be removed by the copies either. Buying or
  restoring with the current app sets `appAccountToken` and ends this. **App Attest is the full
  fix** (registration bound to a genuine instance of the app, attested by Apple), planned for a later version;
  it would also bind `app_secret` to the device.
- Body limits: 16 KiB for registration, 8 KiB for push, delete and activity tokens, 64 KiB for Apple's
  notifications. The declared length is checked first and the stream is cut as soon as it passes the limit.

### App Store notifications
- Verified with the same chain rules. Bundle and environment must match; the nested transaction and renewal
  info are verified too and must name the same subscription. A notification for another of the app's products
  is acknowledged and ignored.
- Subscription references include the StoreKit environment, so a Sandbox notification can never touch a
  Production row with the same transaction id (and each environment has its own cap).
- Ending is idempotent and **monotonic**: an expiry, refund or revocation ends only rows whose paid period does
  not end later than the notification's transaction, so a stale or re-delivered notice about an earlier period
  cannot end a renewed subscription. Renewals only raise the stored expiry.
- **Billing grace**: `DID_FAIL_TO_RENEW` with a `gracePeriodExpiresDate` keeps the handles until then; a
  subscriber in grace registers with the renewal info's JWS. `GRACE_PERIOD_EXPIRED` ends them.
- Even without notifications, a row ends one day (configurable) after its known end.

### The operator's mistakes (T9, A7)
- **A wrong storage key never reads as "gone".** The database holds a sealed canary; the relay refuses to start
  when the key does not open it (or, on a database without one, its first row). If a stored key still fails to
  open at run time, the answer is 500 with an operator alert in the log, never 410, because §9 makes every
  server delete its registration on 410: one typo must not end every user's notifications.
- **The pinned root is in the repository** (`relay/pisa_relay/AppleRootCA-G3.pem`, with a narrow exception to
  the root `.gitignore`'s `*.pem`), checked by its SHA-256 fingerprint in a test, and copied into the image. It
  must never be fetched at deploy time.
- **One instance**: the replay set and the rate counters live in the SQLite file. Two instances on two disks
  would each accept a replay the other has seen. The README says to run one worker on one volume; a lock file
  on the volume that refuses a second live host is a planned hardening (not in this version).
- **TestFlight** uses StoreKit `Sandbox` with APNs `production`. Accepting `Sandbox` on the production relay
  would make it free to every TestFlight tester (a public link: anyone), and sandbox subscriptions renew every
  few minutes. The README requires a separate relay instance, with its own database and storage key, for
  TestFlight.
- **Clock**: the window trusts the host's clock. A forward jump is harmless to the replay set; a backward step of
  more than 300 s re-opens recent signatures. Run NTP; exporting the clock offset in `/metrics` is planned.

### Information leaks through the service itself
- No access log (uvicorn `--no-access-log`; the `httpx` logger, which would write each APNs URL with the device
  token, is held at WARNING). The only log lines are Apple's failure codes, the storage-key alert, and an
  8-hex-digit hash of a handle ended by Apple. That hash is pseudonymous (whoever has the database could map it
  to a row, deleted by then), so logs get the same short retention as other personal data. A test pushes,
  fails and refuses with log capture at DEBUG and checks that no handle, secret, blob, token or transaction id
  appears.
- Push refusals have an empty body (nothing of the request is echoed). Registration errors return a fixed code.
- No OpenAPI or docs routes. `/metrics` needs a bearer token and is off without one; its labels are fixed sets
  (route, push type, HTTP status, Apple's notification type when it is `[A-Z_]`).
- Times in the database are hours, not seconds; the per-minute counters hold a count and live two minutes.

### Metadata (A6, T5, T8)
- For every push, the relay and Apple see its type, its size bucket (512 or 2048: short or long), its exact
  arrival time (forwarded at once, with no delay or batching), its priority (10 for every alert) and its
  `collapse_id` (the notice's random id: a re-send is linkable to the first, nothing else). This is the cost of
  any push service; jitter or batching would trade timeliness for it and is not done.

### Availability (A7)
- One process with a SQLite file; a restart keeps registrations, the replay window and the counters.
- A flood of unauthenticated pushes costs one JSON parse and one primary-key lookup each; unknown handles answer
  410 before any HMAC or APNs work. Per-client limiting (on the /64 or /32) is the reverse proxy's, and
  required (README): the relay does not keep addresses.
- APNs failures answer 502 and keep the handle.

### Dependencies and the image
- Every Python dependency is pinned exactly in `uv.lock` and installed `--frozen`. CI runs `pip-audit` (PyPI
  and OSV advisories) on what the image ships, on every push. `cryptography` links OpenSSL statically: its
  releases follow OpenSSL advisories.
- The Dockerfile pins its base images (`python:3.13-slim`, `ghcr.io/astral-sh/uv`) by digest.
- `pyhpke` (the server's HPKE, and the relay's test dependency) is small and single-maintainer. The RFC 9180
  known-answer test in core and the contract's vectors (here, `test_vectors_relay_signature_and_push_seal`)
  guard it; moving to `cryptography`'s HPKE once it takes `aad` is the plan.

## Residual risks and open questions

1. **Timing metadata (A6) is visible to the relay and to Apple.** Inherent to push. The relay does not store
   per-push times beyond the replay set (signatures for 10 minutes, no handle attached) and per-minute counters
   (two minutes). A compromised or compelled relay could start recording.
2. **Handle existence oracle (accepted).** An unknown handle answers 410 before the signature is checked, as §9
   requires (the server deletes the registration on 410 only). Handles are 128-bit random, so this only tells
   someone who already knows a handle whether it is live.
3. **422 for a dead Live Activity token** is not in §9; the server treats it as a failure. Proposed for the
   next revision: the server drops the activity token on 422.
4. **A copied transaction** of a purchase without an `appAccountToken` can still fill free slots under the
   subscription (above); App Attest is the full fix, later.
5. **The transaction's age**: 7 days is a guess at how stale StoreKit's local copy can be; if real devices hand
   older ones, the app must refresh from the App Store before registering, or the window must grow.
6. **The storage key and the database on the same host**: sealing protects copies of the database (backups),
   not a live host compromise. Key rotation (a key id in each sealed value, old keys accepted) is not in this
   version; today a new key means every app registers again.
7. **Push-to-start alert**: a Live Activity `start` is sent without an `alert` (none of the user's text can be
   in it). Apple's documentation shows an alert in push-to-start payloads; if it proves required, the relay
   should add the same generic localized "Pisa" alert as notices, which is not personal text.
8. **Single instance and clock**: see "The operator's mistakes".

## The first security review, finding by finding

| Finding | What changed | Proven by (`tests/test_e1_review.py`) |
|---|---|---|
| R1 Live Activity tokens not bound | tokens registered per handle; others 401, Apple not asked | `test_r1_*` |
| R2 eviction by a copied JWS; no throttle | idle-30-days eviction rule, 409, hour-granular use, rate limits | `test_r2_*` |
| R3 storage-key mismatch read as "gone" | start-up canary, 500 never 410 | `test_r3_*`, `test_a_storage_key_that_does_not_open_*` |
| R4 the root not committed | `.gitignore` exception, file tracked | `test_r4_*`, `test_the_pinned_root_is_apple_root_ca_g3` |
| R5 Live Activity times | PRIVACY.md says so; coarsening is the server's | `test_r5_*` (pins what is forwarded) |
| R6 JWS before a refund | `signedDate` within 7 days; refunds remembered | `test_r6_*` |
| R7 identical requests in one second | a repeat is 202 and sent once | `test_r7_*` |
| R8 replay rows before the rate limit | the rate limit first | `test_r8_*` |
| R9 references not per environment | `HMAC(environment\nid)`, an environment column | `test_r9_*` |
| R10 billing grace | renewal info at registration; `DID_FAIL_TO_RENEW` | `test_r10_*` |
| R11 no time in a sealed notice | the app and contract 3 (`sentAt`); see T5 | `test_r11_*` (the relay adds nothing) |
| R12 handle oracle | accepted, residual 2 | `test_r12_*` |
| R13 revocation leaves the row | the server's (it signs `DELETE /v1/register` itself) | |
| R14 key storage | documented under T4 and T3; the README asks for a secret store | |
| R15 metadata | documented under "Metadata" and in PRIVACY.md | |
| R16 TestFlight | a separate relay instance (README) | |
| R17 dependencies | pip-audit in CI; base images by digest | |
| R18 code, PROTOCOL.md and §9 | PROTOCOL.md updated; 413 already sent; `sentAt` waits for the contract | |
| R19 smaller notes | push-to-start alert (residual 7); HKDF key derivation (done); single instance and clock (documented) | |
| N1, N1b (re-check) anyone holds the overall registration limit down | the overall limit removed; 5 an hour per subscription after verification; per-client limit at the proxy on the /64 or /32 | `tests/test_e1_recheck.py::test_n1_*`, `test_n1b_*` |
| N2 (re-check) the server binds foreign activity tokens | `app_secret`, kept by the app, signs the app's routes over method and path | `test_n2_*` |
| N3 (re-check) a copied transaction locks the subscriber out | the account key behind `appAccountToken`; the device list and removal; App Attest later | `test_n3_*` |
| N3b (re-check) an older copy removes the subscriber's new phone | removal only with the account key, of itself, or of an idle registration; no order rule | `test_n3b_*`, `tests/test_contract.py::test_the_removal_rule` |
| R5 in relay mode (contract) | Live Activity updates without progress, time or stale date; empty content-state | `tests/test_contract.py` |
