# Features (contract 2)

`GET /v1/server` (no authentication, nothing personal) says what a server is and what it offers, before
pairing. Its shape is contract 2's `$defs/ServerDiscovery` in
[`v2/schema/pisa-contract-2.schema.json`](v2/schema/pisa-contract-2.schema.json); the `/v2/live` welcome carries
the same feature list. Contract 1 (`/v1/device`, `/v1/devices/pair`, the `features` list of plain names) is
gone; a server answers neither.

```json
{"product": "pisa", "serverId": "Qm7Zr2XcSynthetic0Id0000aB", "serverVersion": "0.1.0",
 "contract": {"versions": ["2.0"]}, "agui": ["1.0"],
 "a2ui": {"versions": ["1.0"], "catalogIds": ["urn:pisa:a2ui:catalog:core:1"]}, "mcpApps": ["2026-01-26"],
 "features": [{"id": "stop", "version": 1}, {"id": "approvals.signed", "version": 2},
              {"id": "conversation", "version": 1}, {"id": "tasks", "version": 1}],
 "readiness": {"ready": true, "checks": [{"name": "database", "ok": true}]},
 "reach": {"scheme": "https", "network": "lan"}, "push": {"modes": ["none"], "relayConfigured": false}}
```

The full example documents are [`v2/examples/server.json`](v2/examples/server.json) and the smallest conformant
one, [`v2/examples/server-minimal.json`](v2/examples/server-minimal.json). How a client uses discovery, step by
step, is in the guide: [Build your own client](../docs-site/build-a-client/index.md).

## The rules for a client

- **`contract.versions`** lists the contract versions the server speaks. A client talks only to a server that
  lists a major it speaks, and otherwise tells the user which side to update.
- **`features`** decides what a client shows. Each entry is `{id, version}`, optionally with `entitlement` or
  `deprecated`. A client shows a screen, a settings row or an action only when its feature is listed, at a
  version it knows. An unlisted feature is hidden, not an error; an unknown id is ignored.
- **`stop` and `approvals.signed` are required.** They are the safety features: every server lists them, never
  behind an entitlement and never deprecated (the schema enforces it). A client refuses a server whose list
  lacks either, as it refuses a contract it doesn't speak.
- **`product` and `serverVersion`** are for display only. No client may branch on them.
- **`serverId`** is an opaque random id the server picks once, at install. A client keys its pairing by
  `serverId` plus host, so pairings of different servers never mix. Signed approvals bind it.
- **`pairing`** is present only when the server can pair (addresses and a TLS pin, or insecure loopback mode):
  `{"methods": ["qr_v1", "typed_v1"]}`.

A client asks on connect, after pairing, and when it comes back to the foreground.

## Always there (not features)

Every server that speaks contract 2 has these, listed or not:

- discovery, `GET /v1/server`, and the server's addresses and pins, `GET /v1/server/endpoints`;
- pairing (`/v1/pair*`) and devices (`/v1/devices*`, session tokens at `/v1/devices/session`);
- push registration (`/v1/devices/me/push`, `/v1/push/key`) and diagnostics (`/v1/devices/me/diagnostics`);
- the live socket `/v2/live`: hello and welcome, the notices thread, and errors;
- the AI disclosure and consent (`/v1/ai/disclosure`, `/v1/ai/consent`);
- settings (`GET`/`PATCH /v1/settings`).

## The features this server lists

`core/pisa/api/server.py` builds the list. Every feature is version 1 except `approvals.signed` (version 2). The
REST paths are those in [`openapi.json`](openapi.json). Live events are not gated by features but by the
extensions a client lists in its hello; they are in [`v2/asyncapi-live.json`](v2/asyncapi-live.json) and, with
what to draw for each, in the guide's [Events](../docs-site/build-a-client/events.md) page.

| Feature id | In plain words | REST |
|---|---|---|
| `stop` | Stopping Pisa's own initiative, and cancelling any run | `/v1/stop*` |
| `approvals.signed` | Approvals as signed interrupts (CONTRACT-2 §6) | `/v1/approvals*` |
| `conversation` | Talking and writing to Pisa | `POST /v2/runs`, `/v2/live` |
| `voice.natural` | Natural voice over WebRTC | `/v1/talk/sessions*` |
| `presentation` | Screens: A2UI surfaces and MCP Apps views | none (on `/v2/live`) |
| `drawings` | Drawings | none (on `/v2/live`) |
| `phone_actions` | Messages, calls and directions the device carries out | none (on `/v2/live`) |
| `today` | Today | `GET /v1/plan`, `/v1/weather`, `/v1/briefings/latest`, `/v1/day` |
| `tasks` | Tasks | `/v1/tasks*`, `/v1/agenda`, `/v1/inbox`, `/v1/rules/{id}`, `/v1/events*` |
| `planning` | Planning the day | `POST /v1/plan`, `/v1/plan/insert` |
| `follow` | Following your day | `/v1/plan/options*`, `/v1/plan/adjustments*`, `/v1/check-ins*`, `/v1/devices/me/locations`, `/v1/devices/me/live-activity` |
| `briefing` | Morning briefing | `/v1/briefings*` |
| `guardian` | When Pisa interrupts you | `/v1/guardian/*`, `/v1/ledger/summary` |
| `autopilot` | Life on autopilot | `/v1/ledger*` |
| `trust` | Autonomy, skills and signing keys | `/v1/autonomy*`, `/v1/skills*`, `/v1/phone/keys`, `/v1/phone/challenge` |
| `vault` | Documents and bills | `/v1/vault/*` |
| `people` | People | `/v1/people*` |
| `money` | Money | `/v1/money/*` |
| `trips` | Trips | `/v1/trips*`, `/v1/residency*` |
| `places` | Places and search | `/v1/places/search`, `/v1/places/{id}*`, `/v1/places/providers*`, `/v1/places/usage`, `/v1/places/outcomes` |
| `rituals` | Rituals and place notes | `/v1/rituals`, `/v1/places/notes*`, `/v1/places/enter`, `/v1/sheets*` |
| `connectors` | Google and the other connectors | `/v1/connectors*`, `/v1/obligations` |
| `health` | The daily Apple Health summary | `POST /v1/health/summary` |
| `profile` | About you | `/v1/profile*` |
| `voice` | Pisa's voices | `/v1/voice/voices`, `/v1/voice/preview` |
| `voice_id` | Voice ID (the voice gate) | `/v1/voice/profile*`, `/v1/voice/enrolment/script`, `/v1/voice/consent` |
| `ai_models` | AI providers, models and spend | `/v1/ai/providers*`, `/v1/ai/models`, `/v1/ai/slots*`, `/v1/ai/caps`, `/v1/ai/spend` |
| `views` | Pisa's views | `/v1/views*` |
| `world` | Sources and Pisa's identity | `/v1/world/*`, `/v1/public/sources` |
| `data_erase` | Deleting your data | `/v1/data/erase*`, `/v1/settings/messages_retention/preview` |
| `watch` | A watch's questions | `/v1/dossier*` |
| `companion` | Glasses and earbuds | none (on `/v2/live`) |

A listed feature says the server implements it; some still need a provider key, a connector or a client that
does its part (for example `places` needs a maps provider, and `health` a client that reads Apple Health). The
REST endpoints for your data that no feature covers (`/v1/changes`, `/v1/learn*`, `/v1/memory*`, `/v1/intentions*`
and others) work for any paired client all the same.

## Adding a feature

1. Name it here, with its paths and events, and add it to the server's list.
2. Implement all of it on the server before listing it.
3. In a client, gate every surface on it.

An old client ignores the new id; a new client hides it on a server that doesn't list it. Neither needs a new
contract major.
