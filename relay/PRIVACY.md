# What the push relay sees, and what it keeps

The push relay passes notifications from a user's own Pisa server to Apple, because Apple only accepts pushes
for the App Store app from whoever holds the app's key. It is built so that it **learns nothing about the user's
life** that it does not need to deliver a push. This page says exactly what crosses it, including the one
exception (Live Activity times). The code is in this folder, so you can check every line.

## What the relay never sees

- **The text of a notice**: no titles, bodies, names, places, times of appointments, amounts, or anything the
  assistant wrote. The user's server seals each notice to a key that exists only on the user's phone (HPKE,
  RFC 9180, X25519 and ChaCha20-Poly1305). The relay holds no key that opens it.
- **The size of a notice**: every sealed notice is padded to exactly 512 or 2048 bytes, so the relay learns
  only whether it was short or long.
- **Which server sent it**, by name or address: the relay is never told the server's address, and it does not
  log the network address of any request.
- **Who the user is**: no name, email, Apple ID, phone number or account. The app registers with an App Store
  receipt that proves a valid subscription; the relay checks it and keeps only a keyed hash of its
  subscription number (below).
- **What the user does with a notification**: Approve, Undo and every other action go from the phone straight
  to the user's own server, never through the relay.
- **Live Activity text**: Apple shows Live Activities itself, so nothing can decrypt them on the phone. The
  relay only accepts updates made of numbers; it refuses anything else, so names and places cannot pass
  through it even by mistake. The numbers themselves are not nothing: see "What the relay sees".

## What the relay sees

For each **registration** (when the user turns on notifications through the relay):
- the phone's APNs device token, and whether it is a production or development build;
- the App Store signed transaction of the subscription (checked, then discarded);
- optionally the phone's language, reduced to `en` or `it`, so the generic banner is in the right language.

For each **push**:
- an opaque handle (a random number the relay chose at registration);
- the time it arrives, and its type: a notice, a background wake, or a Live Activity update;
- for a notice: the sealed blob (512 or 2048 bytes it cannot read) and a collapse id (an opaque id the server
  uses so a re-sent notice replaces the earlier banner);
- for a Live Activity update: the activity's push token, whether it starts, updates or ends, and the moment
  of sending. Nothing of the user's day: no progress, no time of a stop or a departure, no stale date (the
  relay refuses an update that carries one). Apple gets an empty content-state; the widget keeps its last
  values until the app is opened and fills them in.

To send Live Activity updates, the relay also keeps the phone's own Live Activity tokens (as SHA-256 hashes,
the last four of each kind), which the app registers, so that one phone's handle cannot reach another phone.

From **Apple** (App Store Server Notifications): that a subscription renewed, expired, was refunded or was
revoked, with its product and expiry date.

## What it sends Apple

- A notice: the words "Pisa" and "New notice" (or "Nuovo avviso"), a flag that lets the app's notification
  extension replace them with the real notice after decrypting it on the phone, the sealed blob, and the
  collapse id (the notice's random id: a re-sent notice can be linked to the first, nothing else).
- A background wake: only the flag that wakes the app. The app then asks its own server what is new.
- A Live Activity update: an empty content-state, the event and the moment of sending.

Apple learns what it learns from any push: that this device got a push at this time.

## What it keeps

One row per registered phone:

| Field | What it is |
|---|---|
| handle | the random handle |
| APNs token and environment | where to deliver |
| sealed key | `sha256(secret)`, the key that checks a push is from the user's server; encrypted with the relay's storage key |
| sealed app key | `sha256(app_secret)`, the key that checks a request is from the app itself (its Live Activity tokens, its device list); encrypted the same way |
| account proof | whether the registration proved the account key behind the purchase's `appAccountToken` (yes or no; neither the key nor the token is stored) |
| subscription reference | a keyed hash (HMAC-SHA256 under a key derived from the relay's storage key) of the StoreKit environment and the subscription's original transaction number, so Apple's "this subscription ended" notices can find the phone's row; the number itself is not stored |
| StoreKit environment | `Production` (or `Sandbox` on a test relay) |
| subscription expiry | when the paid period ends, and the end of Apple's billing grace when the user is in it |
| language | `en` or `it` |
| created, last used | the **hour** only, not the time: enough to know which of a subscription's phones is in use |

Any registered phone of a subscription can ask the relay for that subscription's list of registrations, so
the user can remove an old phone: it gets an opaque id per phone, the day each was registered and last used,
and its APNs environment, never a token or a handle.

When Apple reports a refund or a revocation, the relay also remembers the subscription's reference and that
period's expiry (for 60 days past it), so that a receipt signed before the refund cannot register again.

Short-lived bookkeeping, deleted within minutes of no longer being needed:
- the signatures of pushes accepted in the last 10 minutes (to refuse a replayed push);
- per-handle push counters for the current minute and day, and registration counters per subscription
  reference for the hour (rate limits).

**Logs:** no request bodies, no tokens, no handles, no network addresses, no access log. The relay logs only
Apple's failure codes (for example "BadDeviceToken"), an operator alert when its storage key does not open its
data, and, when Apple says a phone's token is gone, an 8-hex-digit hash of the ended handle. That hash is
pseudonymous (whoever has the database can match it to a row, which is deleted by then), so the logs get the
same short retention as any other personal data. Its metrics are counters by route, type and status, shown
only to the operator.

## When it forgets

- The app removes itself (Unpair and delete, or turning the relay off): the row is deleted at once.
- Apple says the token no longer works, or the user deleted the app: the row is deleted at the next push.
- The subscription expires, is refunded or revoked: the row is deleted when Apple's notice arrives, and in any
  case one day after the known end (the expiry, or Apple's billing grace).
- A subscription has its 10 phones and another registers: the least recently used one that has been idle for
  30 days is deleted.
- The same phone registers again: its previous row is replaced.

## What the relay could still learn

Said plainly: the relay sees **when** a user's server sends a notice, how often, which kind, and whether it
was short or long; it forwards each at once, with no delay or batching. That timing is the cost of any push
service. With Live Activities on, it sees when an activity starts, updates and ends, nothing more. The
operator also sees, like any web service, the network address a request comes from while it is being answered,
even though the relay does not record it; a reverse proxy in front of it must be configured not to log either
(see README). The full analysis is in [THREAT-MODEL.md](THREAT-MODEL.md).
