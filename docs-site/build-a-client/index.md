# Build your own client

Pisa is a server and a contract. The server does the work (your data, the AI calls, the tools); a client is
whatever you build to talk to it: a phone app, a desktop app, a web page, a bot, a terminal. This guide is
everything a client needs, flow by flow, with the exact bytes on the wire and a runnable example for each.

Pisa's own iPhone app is one such client. Nothing in this guide depends on it, except where a page says so
plainly.

## What a client does

1. **Pair once** ([Pairing and sessions](pairing.md)): read the server's pairing code, make a device key, prove
   the code with an HMAC, show the 6-digit check, wait for the user's yes.
2. **Sign a session** every hour ([Pairing and sessions](pairing.md#session-tokens)): the device key signs a
   nonce for a bearer token.
3. **Ask for consent** ([Text chat](text-chat.md#consent-first)): show which AI providers get which data,
   record the user's yes.
4. **Talk**: in text ([Text chat](text-chat.md)), over `POST /v2/runs` or the live socket `/v2/live`; by voice
   ([Audio](audio.md)), with the device's own speech recognition, or over WebRTC.
5. **Draw what comes back** ([Events](events.md)): AG-UI 1.0 events, Pisa's `pisa.*` extensions, and screens
   ([Screens](screens.md)): A2UI surfaces in Pisa's component catalog, and MCP Apps views.
6. **Handle the rest**: [approvals](approvals.md), [push](push.md), [deleting data](erase.md),
   [more devices](devices.md), [every error](errors.md), [the user's connections](connections.md) (every
   outside service, its keys and the OK of a paid one), and, from contract 2.3, the phone's own
   [maps](maps.md) and [local notifications](notify.md).
7. **Test it** ([Testing your client](testing.md)) with the conformance kit, against the contract's fixtures and
   your own recorded traffic.

## Where the formats come from

The contract is machine-readable, in `protocol/v2/` of the source tree. This guide explains it; when the two
disagree, the contract wins.

| What | File |
|---|---|
| Every frame, event and REST body Pisa adds (JSON Schema 2020-12) | `protocol/v2/schema/pisa-contract-2.schema.json` |
| The live socket (AsyncAPI 3.0) | `protocol/v2/asyncapi-live.json` |
| The REST endpoints contract 2 adds or changes (OpenAPI 3.1) | `protocol/v2/openapi-v2-delta.json` |
| Every REST endpoint (OpenAPI 3.1) | `protocol/openapi.json` |
| The A2UI component catalog `urn:pisa:a2ui:catalog:core:1` | `protocol/v2/catalog/pisa-core.catalog.v1.json` |
| AG-UI 1.0 and A2UI 1.0, unmodified | `protocol/v2/vendor/` |
| Recorded streams, every event once, test vectors | `protocol/v2/fixtures/` |
| The conformance kit (validators, a reference emitter) | `protocol/v2/conformance/` |

## The examples

Every code block in this guide is cut from a file in [`examples/`](examples/), and a check fails the build when a
block and its file differ. The files are small Python programs (3.11 or later); each runs on its own:

```bash
pip install httpx websockets cryptography   # and aiortc for WebRTC, pyhpke for push
python pair.py 'pisa://pair?v=1&addrs=...' --name "My client"    # writes pisa-client.json
python chat_live.py "What's on today?"
```

| File | What it shows |
|---|---|
| [`pair.py`](examples/pair.py) | pairing from the QR's text: the device key, the HMAC, the commitment, the SAS |
| [`session.py`](examples/session.py) | session tokens: sign, refresh, expiry, revocation |
| [`consent.py`](examples/consent.py) | the AI disclosure and the user's consent |
| [`chat_runs.py`](examples/chat_runs.py), [`chat_runs.sh`](examples/chat_runs.sh) | a turn over `POST /v2/runs` (SSE and JSON), in Python and with curl |
| [`chat_live.py`](examples/chat_live.py) | the live socket: hello, a turn, cancel, resume |
| [`render.py`](examples/render.py) | what to draw for every event |
| [`screens.py`](examples/screens.py) | A2UI surfaces as widgets, taps and acknowledgments; MCP Apps views |
| [`voice_economy.py`](examples/voice_economy.py) | voice with on-device recognition: partials, turn hints, Pisa's voice as audio chunks |
| [`talk_webrtc.py`](examples/talk_webrtc.py) | natural voice over WebRTC with aiortc, and the fallback |
| [`approvals.py`](examples/approvals.py) | approvals: the digest, the canonical text, the decision |
| [`push.py`](examples/push.py) | push registration, opening a sealed push, catching up without push |
| [`erase.py`](examples/erase.py) | deleting data |
| [`devices.py`](examples/devices.py) | pairing a second device from the first, revoking, unpairing |
| [`errors.py`](examples/errors.py) | every error code and what to do |
| [`vectors.py`](examples/vectors.py) | the byte formats checked against the contract's test vectors, offline |
| [`pisa_example.py`](examples/pisa_example.py) | what they share: the state file, base64, the error envelope |

Every example runs in CI against a real server started from the same commit, with no model chosen, so each turn
ends with `RUN_ERROR model_not_configured`. The pages show what a real answer looks like from the contract's
recorded streams.

## Not covered here

- Running the server: [Self-hosting](../self-hosting.md). Choosing models: [AI providers](../ai-providers.md).
- The REST endpoints for your data (tasks, plans, places, settings, Memoria...): they follow the same
  authentication and error envelope; `protocol/openapi.json` lists them all, and a running server outside
  production serves `/docs`. Settings → Memoria (standing instructions and notes, `/v1/memoria`) has its own
  page: [Memoria](../memoria.md).
