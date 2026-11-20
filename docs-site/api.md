# The API

Every client talks to Pisa through one documented API: your own client, built with the guide
([Build your own client](build-a-client/index.md)), the PISA app, or the command-line client. Any client that
pairs ([Pairing](pairing.md)) can use all of it.

- **REST**: [`protocol/openapi.json`](../protocol/openapi.json), OpenAPI 3.1, generated from the server's code
  (`scripts/ci/gen_openapi.py`; CI fails when it is out of date). Outside `PISA_ENV=prod` the running server
  also serves its interactive map at `/docs`.
- **Contract 2**: [`protocol/v2/`](../protocol/v2/README.md): the live socket `/v2/live` (AG-UI 1.0 events plus
  Pisa's `pisa.*` extensions, described in AsyncAPI), the A2UI component catalog, the REST endpoints it adds or
  changes, the JSON Schema of every frame, fixtures, and `pisa-conformance`, the kit a client or a server runs
  to check itself. Contract 2 is frozen: a change is a new minor version with its own digest.
- **Discovery**: `GET /v1/server` (no authentication, nothing personal) says what a server is and offers:
  the contract versions and the features, each `{id, version}`
  ([`protocol/FEATURES.md`](../protocol/FEATURES.md) lists them).

## Authentication

A request about your data carries a session token: `Authorization: Bearer <token>` (`/health`, `/readyz`,
`GET /v1/server` and pairing need none). A paired device gets one from `POST /v1/devices/session`, signed with its own
key, valid one hour. On the command line, `pisa client token` prints one and `pisa client call` makes a request
with it.

Changes that matter (a connector switched on, a model server added) are also signed once Pisa holds a Face
ID key (the PISA app registers one), and a delete always is: by any paired client's own device key (key id
`device-<its device id>`), or by the PISA app's Face ID key. The endpoint's description names the operation and
its target; how to sign: [Pairing](pairing.md#signed-operations).

## Errors

An error answers `{"error": {"code", "message", "action"}}`: a stable `code` (`validation_failed`,
`model_not_configured`, `ai_consent_required`, `biometric_required`...), what happened, and what fixes it.

## Turns without the socket

`POST /v2/runs` runs one turn over REST, as JSON or as a server-sent event stream.
