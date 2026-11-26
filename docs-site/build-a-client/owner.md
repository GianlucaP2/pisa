# The addresses the user writes from

Pisa reads the user's sent mail, and a message written by the user is their own words: it never comes back to
them as a request. The connected Google account is theirs from the start. Any other address (a work alias, an
old address) Pisa learns from the `From` header of a sent message, and it counts only after the user said it is
theirs, once. A client shows the pending ones and sends the answer.

All three need a session token ([Pairing and sessions](pairing.md#session-tokens)). No signature: an alias is
a guess Pisa made, not data the user wrote.

## List

```
GET /v1/owner/aliases?state=pending
Authorization: Bearer <session token>
```

`state` is `pending` (the default: the ones to ask about), `confirmed` or `all`.

```json
{"account": "ada.demo@example.com",
 "aliases": [{"address": "ada.studio@example.com", "display_name": "Ada Demo",
              "first_seen_at": "2026-11-18T09:12:00Z", "confirmed": false}]}
```

`account` is the connected Google account (`null` when none is connected: then `aliases` is empty). `address` is
lower case, and is the `{address}` of the routes below. The oldest alias comes first.

## Confirm

```
POST /v1/owner/aliases/ada.studio@example.com/confirm
```

Always signed: a `device` challenge with op `alias_confirm` and the address in lowercase as its target, sent in
the `X-Pisa-Nonce`, `X-Pisa-Signature` and `X-Pisa-Key-Id` headers, as for every write that widens what Pisa
trusts. Without it: `403 biometric_required`.

`200` with the alias, `confirmed` true. The mail already stored from that address (the sent mail only) becomes
the user's own. `404 not_found` when Pisa knows no such address.

## Forget

```
POST /v1/owner/aliases/ada.studio@example.com/forget
```

`204`. The address is not theirs (or no longer): it is removed, and the mail it marked as theirs is no longer
theirs. `404 not_found` when Pisa knows no such address. A sent message from it, seen again, asks once more.

Added in contract 2.5 (additive, REST only: a client that never calls these routes is unaffected).
