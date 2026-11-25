# Connections

`GET /v1/connections` is the server's own list of every outside service it can reach, in six categories, for a
settings screen ("Collegamenti" in the PISA app). It is read-only: each entry names the endpoints that change it,
which already exist for its kind. What it means for the user: [Connections](../connections.md).

## The list

```http
GET /v1/connections
Authorization: Bearer <session token>
```

```json
{
  "categories": [
    {"id": "ai", "name": "AI models", "entries": [
      {"id": "ai.openai", "kind": "ai_provider", "category": "ai", "name": "OpenAI", "provider": "openai",
       "state": "on", "paid": true, "paid_ack": null, "needs_paid_ack": false, "month_spent_cents": null,
       "data_out": "Your words and the context Pisa sends with them, to OpenAI", "docs": "ai-providers.md",
       "actions": ["set_key", "delete_key", "test"], "key_last4": "9a3f",
       "slots": ["chat.conversation", "intake.triage"]}
    ]},
    {"id": "voice", "name": "Voice", "entries": []},
    {"id": "maps", "name": "Maps & places", "entries": [
      {"id": "maps.google", "kind": "place_provider", "category": "maps", "name": "Google Maps",
       "provider": "google", "state": "not_set_up", "paid": true, "paid_ack": null, "needs_paid_ack": true,
       "month_spent_cents": 0, "data_out": "Place searches, place ids and trip ends, to Google Maps",
       "docs": "places-and-web.md", "actions": ["set_key", "delete_key", "ack_paid"], "key_last4": "7788",
       "slots": []}
    ]},
    {"id": "web", "name": "Web research", "entries": []},
    {"id": "accounts", "name": "Accounts", "entries": []},
    {"id": "notifications", "name": "Notifications", "entries": []}
  ],
  "hard_limit_monthly_cents": 3000
}
```

The six categories always come, in this order (`ai`, `voice`, `maps`, `web`, `accounts`, `notifications`); an
entry of a feature off on the server is absent. An entry's `id` is unique (`<category>.<provider>`);
`provider` is the id its own endpoints take.

| Field | |
|---|---|
| `kind` | `ai_provider`, `model_server`, `voice`, `talker`, `phone_maps`, `place_provider`, `web_provider`, `account`, `phone_notify`, `push_relay`, `apns` |
| `state` | `on`, `off`, `not_set_up`, `error`, `needs_reconnect` |
| `paid` | it costs money (a metered service priced in Pisa's tables) |
| `paid_ack` | `{cap_cents, at}`: the user's OK of this paid maps or web provider; `null` otherwise |
| `needs_paid_ack` | it has a key but is never called until the OK |
| `month_spent_cents` | for a provider that takes the OK: this month's cost so far |
| `data_out` | what leaves the server and to whom: a fixed sentence, show it as is |
| `docs` | the page of the docs site about it |
| `actions` | what to offer: `set_key`, `delete_key`, `test`, `connect`, `disconnect`, `ack_paid` |
| `key_last4` | the key's last four characters, as its own endpoint shows them |
| `slots` | AI: the feature slots whose model or fallback is on this provider |

Each action is an endpoint of the entry's kind:

| Kind | `set_key` / `delete_key` | `test` | other |
|---|---|---|---|
| `ai_provider`, `model_server`, `voice`, `talker` | `PUT` / `DELETE /v1/ai/providers/{provider}/key` | `POST /v1/ai/providers/{provider}/test` | |
| `place_provider`, `web_provider` | `PUT` / `DELETE /v1/places/providers/{provider}/key` | `POST /v1/places/providers/{provider}/test` | `ack_paid`: below |
| `apns` | `PUT` / `DELETE /v1/push/key` | | |
| `account` | | | `connect`: `POST /v1/connectors/google/start`; `disconnect`: `DELETE /v1/connectors/google` |

## Giving the paid OK

Google Maps and Tavily are never called until the user gives their OK to the provider and to its own monthly
cap, in euro cents, at most `hard_limit_monthly_cents`. It is a signed device operation
([Deleting data](erase.md) shows the challenge and the headers): op `paid_ack`, target
`<provider>:<cap_cents>`. Always signed, also before the server holds a Face ID key; your client's own device key
(`device-<its device id>`) signs it.

```http
POST /v1/phone/challenge
{"action": "device", "op": "paid_ack", "target": "google:500"}
```

```http
PUT /v1/connections/paid/google
X-Pisa-Nonce: <the challenge's nonce>
X-Pisa-Signature: <the canonical text signed, base64>
X-Pisa-Key-Id: device-<your device id>

{"cap_cents": 500}
```

```json
{"cap_cents": 500, "at": "2026-11-24T10:00:00+01:00"}
```

- 403 `biometric_required`: no signature, a bad one, a spent nonce, another device's key, or a target that
  isn't exactly `<provider>:<the cap sent>`.
- 400 `validation_failed`: a cap under 1 cent, not a whole number, or over the hard limit (checked before the
  nonce is spent).
- 404 `not_found`: a provider that needs no OK (Apple Maps, OpenStreetMap…), or the places feature is off.

`DELETE /v1/connections/paid/{provider}` withdraws it (no signature: it only stops spending).

Until the OK, a request that needs the provider answers 409 `place_provider_not_configured` with
`"reason": "paid_ack_required"`, and the turn tells the user in one line. Show `message` as it is:

```json
{"error": {"code": "place_provider_not_configured",
  "message": "No provider for place search is ready: Google Maps costs money and waits for your OK of its monthly cap in Settings → Connections.",
  "action": "Acknowledge it and its monthly cap in Settings → Connections (Collegamenti)",
  "reason": "paid_ack_required"}}
```

## Deleting a key that slots use

`DELETE /v1/ai/providers/{provider}/key` answers 409 `provider_in_use` while a feature slot's model or fallback
is on that provider:

```json
{"error": {"code": "provider_in_use", "provider": "openai",
  "slots": ["chat.conversation", "intake.triage"],
  "message": "OpenAI serves chat.conversation, intake.triage: deleting its key would leave them without a model.",
  "action": "Choose a replacement provider (replace_with), or set those slots to another model first"}}
```

Offer a replacement among the providers that have a key, and send
`DELETE /v1/ai/providers/openai/key?replace_with=anthropic`: each slot moves to that provider's model for it
and the key goes, in one step (204). When the replacement has no model for a slot, nothing changes: 400
`model_incompatible` with `slots`.
