# PISA contract 2 (`protocol/v2`)

The contract every PISA server speaks to the PISA app and to headless clients (D-128, D-129): AG-UI 1.0 for the live
conversation, A2UI 1.0 for Ring 1 surfaces, MCP Apps 2026-01-26 for Ring 2 views, the `pisa.*` extensions, and REST.
The machine-readable contract is `schema/`, `catalog/`, the generated OpenAPI and AsyncAPI documents beside them,
and the conformance kit in `conformance/`. v1 stays beside it in `protocol/` until
D-122 retires it. Apache-2.0: `protocol/LICENSE`, third-party notices in `protocol/NOTICE`.

| Path | What | How it is made |
|---|---|---|
| `schema/pisa-contract-2.schema.json` | frames, hello/welcome, `GET /v1/server`, pairing, every `pisa.*` event, approvals, activity contents, the talk's end and voice enrolment (JSON Schema 2020-12) | **frozen**; `tools/gen_schema.py` |
| `catalog/pisa-core.catalog.v1.json` | the A2UI catalog `urn:pisa:a2ui:catalog:core:1` (24 components) | **frozen**; `tools/gen_catalog.py` |
| `catalog/pisa-core.catalog.v2.json` | the A2UI catalog `urn:pisa:a2ui:catalog:core:2`: core:1 unchanged plus `WeatherSummary` (25 components) | **frozen**; `tools/gen_catalog_v2.py` |
| `asyncapi-live.json` | AsyncAPI 3.0 for `/v2/live`; payloads are `$ref`s into the schema | `tools/gen_asyncapi.py` |
| `openapi-v2-delta.json` | OpenAPI 3.1: the REST endpoints contract 2 adds or changes (spec §10) | `tools/gen_openapi_delta.py` |
| `examples/server.json`, `examples/server-minimal.json` | a `GET /v1/server` document, and the smallest conformant one (only the safety features) | by hand |
| `vendor/` | AG-UI 1.0 and A2UI 1.0 schemas, unmodified, pinned by sha256 (`vendor/SHA256SUMS`) | copied from upstream |
| `fixtures/` | recorded streams, every AG-UI and `pisa.*` event, the gaps' valid and invalid frames, enrolment channels, approval vectors | `conformance/src/pisa_conformance/reference/` |
| `conformance/` | `pisa-conformance`: the Python kit and CLI both backends run | |
| `swift/` | `PisaAGUI`: the Swift twin (codec, frames, negotiation, discovery, feature gate, approval canonical text, `LiveClient`, enrolment frames). Swift 6, Foundation only, builds on Linux | |

## Frozen

`FROZEN.json` holds the sha256 of the schema (contract `2.3`), of `core:1` and of `core:2`; a test fails if one
changes.
- A **contract minor** (an optional field, a new `pisa.*` event, a feature) bumps `x-pisa-contract` in the
  generator, regenerates, and records the new digest in `FROZEN.json`, keeping the old one in `history`.
- A **catalog** never changes: a new component is a new catalog (`core:2`, a superset) beside `core:1`.
- Anything else is a new major (CONTRACT-2 §12).

| File | sha256 |
|---|---|
| `schema/pisa-contract-2.schema.json` (contract 2.3) | `a5ace8b93272643bd8a1658becceea57e5c1413bf50df7e6d03dec77ed6f5133` |
| `catalog/pisa-core.catalog.v1.json` (`core:1`) | `c464833c2aa3184107e042071c0ddf477b7ef809d4d53d41488fd79f39e272cc` |
| `catalog/pisa-core.catalog.v2.json` (`core:2`) | `04e29e9cd681b0187de5a13ce83bf212d3a6a3cca94f01ea0b516f1e215fb465` |

## Catalog core:2: WeatherSummary

`urn:pisa:a2ui:catalog:core:2` is `core:1` with every component unchanged (`PlaceSummary` included) plus one,
`WeatherSummary`: the forecast the turn's `get_weather` read, for one place and one day. A server composes a surface
in `core:2` only when the app's hello listed it in `a2ui."1.0".supportedCatalogIds` (and the welcome picked it);
every other surface stays in `core:1`. An app that lists only `core:1` gets the forecast in Pisa's words, as before.
Discovery (`GET /v1/server`) lists both: `"catalogIds": ["urn:pisa:a2ui:catalog:core:1",
"urn:pisa:a2ui:catalog:core:2"]`. Over `POST /v2/runs` (no hello) surfaces stay in `core:1`.

| Property | Type | |
|---|---|---|
| `place` | string, 1 to 120 | the place's or city's name ("here" resolves to the city) |
| `date` | `YYYY-MM-DD` | the forecast's day |
| `condition` | string, 1 to 60 | the day's condition in words, in the surface's language ("Light rain", "Pioggia debole") |
| `now` | `{tempC, condition}`, optional | the current hour's forecast; absent for another day |
| `minC`, `maxC` | number, °C | the day's low and high |
| `rainChance` | integer 0 to 100 | the day's chance of precipitation, percent |
| `hourly` | up to 24 `{time, tempC, rainChance, condition}` | in time order: today the next 24 hours from the current one, another day its hours (a 25-hour day keeps the first 24); `time` is RFC 3339 with the place's offset |
| `fallback` | string, 1 to 400 | the text where it can't be drawn: `"Testville, 2026-11-22: Light rain, 8.5 to 15 °C, rain chance 60%"` |

The forecast is Open-Meteo's, in degrees Celsius. No other property is allowed on `now` or an hour. The kit
(`pisa_conformance.validate.surface_errors`, and `server_frame_errors` for a frame) judges a surface's components
by the catalog its `createSurface` names, so a `WeatherSummary` in a `core:1` surface fails.

## Contract 2.2: the app's own screens (`pisa.app`)

Additive over 2.1 (a client of 2.0 or 2.1 is served its own; discovery says `["2.0", "2.1", "2.2"]`):

| Addition | Where | |
|---|---|---|
| extension `pisa.app` (major 1) | `x-pisa-extensions`, the hello | gates the two server events below |
| `pisa.app.open` `{actionId, target, id?}` | server CUSTOM, in the turn's run | open one of the app's own screens: `target` `tab` (`id` `pisa`, `oggi`, `impostazioni`), `riepilogo`, `task` (`id`: its uuid), `ai_privacy`, `riepilogo_refresh`; `id` required for `tab` and `task` |
| `pisa.app.result` `{actionId, ok, reason?}` | client custom | the app's report, at once; `reason` `unsupported`, `not_found`, `not_now`, `error`. No report within about 3 s: Pisa says "not confirmed" |
| `pisa.app.changed` `{views, at}` | server CUSTOM, on the notices thread | the tasks or the agenda changed: read again `riepilogo` (`GET /v1/briefings/latest`), `plan`, `tasks`, `agenda`; ids only, at most about once a second |
| `$defs/AppMap`, `TurnProps.app` | `forwardedProps.pisa.app` | the app's screen map with each run: `version`, `build?`, `tabs` (1-6 `{id, title}`), `current`, `cards` (0-12 ids), `settings` (0-16 titles); ids `^[a-z][a-z0-9_]{0,31}$`, titles 1-40 characters without control characters. Untrusted data for the server: it never changes tools or rules |
| approval kind `setting_change` | `ApprovalInterrupt.metadata.pisa.kind` | one of Pisa's server settings (the briefing's rules, the reminders' lead times), made only on the user's yes, with Undo for 24 hours |

Features in discovery: `app.actions` and `briefing.view` (version 1). Fixtures: `fixtures/gaps/{valid,invalid}/app_*`;
the kit's `test_app_actions_and_the_screen_map_are_additive_in_contract_2_2`.

## Contract 2.3: the phone's own maps and local notifications (`pisa.maps`, `pisa.notify`)

Additive over 2.2 (a client of 2.0, 2.1 or 2.2 is served its own). Client guide: `docs-site/build-a-client/maps.md`
and `notify.md`.

| Addition | Where | |
|---|---|---|
| extensions `pisa.maps`, `pisa.notify` (major 1) | `x-pisa-extensions`, the hello | the client lists them to get the events below; the welcome grants them |
| `pisa.maps.search` `{requestId, query, near?, kind?, limit?}` | server CUSTOM, in the turn's run | look places up with the phone's maps (no server key): `query` 1-200, `near` `"here"` or `{lat, lon}`, `kind` `restaurant`, `cafe`, `pharmacy`, `fuel`, `supermarket`, `atm`, `parking`, `other`; `limit` 1-10 |
| `pisa.maps.eta` `{requestId, from, to, mode}` | server CUSTOM, in the turn's run | a travel time; `from`/`to` `"here"`, `{lat, lon}` or `{placeId}` from an earlier result; `mode` `walk`, `transit`, `drive`, `bike` (as `pisa.directions`) |
| `pisa.maps.result` `{requestId, ok, places? \| eta?, reason?}` | client custom | ok with `places` (1-10 `$defs/MapsPlace`) or `eta` (`$defs/MapsEta`), else `reason` `denied`, `unavailable`, `no_results`, `timeout`, `unsupported` |
| `$defs/MapsPlace` | `pisa.maps.result.places[]` | `{id, name, lat, lon, address?, kind?, distanceMeters?, openNow?, phone?, url?}`: `id` 1-200 of `[A-Za-z0-9._:~-]`, `name` 1-120, `address` 1-200 (no control or bidi characters), `phone` E.164, `url` `https://` only, 500 at most. New, not the catalog's `PlaceSummary` component (which the server composes, with a server `placeId` and a `fallback`) |
| `pisa.notify.schedule` `{revision, items}` | server CUSTOM, on the notices thread | 1-50 `$defs/NotifyItem` `{id, fireAt, title, body?, kind, deepLink?}`: `kind` `reminder`, `task`, `calendar`, `leave`; `title` 1-80, `body` 1-200 (lock-screen text, kept minimal) |
| `pisa.notify.cancel` `{revision, ids}` | server CUSTOM, on the notices thread | 1-50 ids |
| `pisa.notify.regions` `{revision, regions}` | server CUSTOM, on the notices thread | the whole set, 0-20 `$defs/NotifyRegion` `{id, lat, lon, radiusMeters, title, on, deepLink?}`: `radiusMeters` 100-5000, `on` `enter` or `exit` |
| `pisa.notify.ack` `{revision, scheduled, failed}` | client custom | ids applied (0-50) and `{id, reason}` failures (0-50): `denied`, `limit`, `past`, `invalid`, `unsupported`, `error` |

`revision` is one counter per device shared by the three `pisa.notify.*` frames; the phone applies a frame only
above the last revision it applied (contract 2.5: unless the frame's optional `epoch` changed, which resets it). Ids are 1-64 of `[A-Za-z0-9_-]`. `deepLink` is one of `pisa://chat`,
`pisa://riepilogo`, `pisa://task/<uuid>`, `pisa://reminder/<uuid>` and nothing else. What the phone sends back
(places, an ETA, an ack) is untrusted input to the server: bounded, closed (no other property), quarantined as
outside text; no coordinate goes into a log on either side. Patterns that end in `$` also refuse a trailing
newline (`not: {pattern: "\\n"}`), since some regex engines let `$` match before one.

Features in discovery: `maps.phone` and `notify.local` (version 1), listed by a server that serves them. Fixtures:
`fixtures/gaps/{valid,invalid}/maps_*` and `notify_*`, the server events in `fixtures/events/pisa_server_events.jsonl`;
the kit's `test_phone_maps_and_local_notifications_are_additive_in_contract_2_3`.

## Contract 2.4: a presence with no event, a talk's MCP Apps

Additive over 2.3 (a client of 2.0 to 2.3 is served its own; every 2.3 frame stays valid).

| Addition | Where | |
|---|---|---|
| `pisa.presence`'s `event` is optional | client custom (`$defs/c_pisa_presence`) | a change with no event of its own (a route change: CarPlay, AirPods out; a lock) sends `state` alone. The server keeps the state; the app, glasses, earbuds and companion fields change only with an event, and only an event starts a briefing. Still closed: no other property, `event` from its set |
| `mcpApps` on `POST /v1/talk/sessions` | REST (`protocol/openapi.json`, `TalkMcpApps`) | `{protocolVersions, mimeTypes, bundledLibraries?}`, the live hello's shape: the MCP Apps the phone renders in that talk. A talk's channel takes no hello, so this is where the phone declares them. Fail closed: without it (or without Pisa's version and `text/html;profile=mcp-app`), a drawing in the talk comes as a ring-1 note card (its title and summary), never as an `mcp-apps` view |

Fixture: `fixtures/gaps/valid/presence_state_only.json`; the kit's
`test_a_presence_with_no_event_is_valid_in_contract_2_4`.

## Contract 2.5: an epoch on the notify frames

Additive over 2.4 (a client of 2.0 to 2.4 is served its own; every 2.3 and 2.4 frame stays valid).

| Addition | Where | |
|---|---|---|
| `epoch` (optional) on `pisa.notify.schedule`, `.cancel` and `.regions` | server CUSTOM (`$defs/s_pisa_notify_*`) | an opaque string of 1-64 of `[A-Za-z0-9_-]`, the same on every frame until the server's notify state is restored or reset. When it differs from the epoch the phone last saw, the phone resets its last applied revision (to 0) and applies the frame. A frame with no epoch leaves the phone as it was |

Fixtures: `fixtures/gaps/valid/notify_cancel_epoch.json`, `fixtures/gaps/invalid/notify_epoch_free_text.json`; the
kit's `test_the_notify_frames_may_carry_an_epoch_in_contract_2_5`.

2.5 also adds three REST routes (`protocol/openapi.json`, tag `owner`, a session token and no signature; no
frame, schema or catalog changes, so `FROZEN.json` is as it was): `GET /v1/owner/aliases` (`state`: `pending`,
the default, `confirmed` or `all`) lists the addresses Pisa saw the user write from, `POST
/v1/owner/aliases/{address}/confirm` says one is theirs, `POST /v1/owner/aliases/{address}/forget` says it is
not. Bodies: `OwnerAliases` and `OwnerAlias`. Guide: `docs-site/build-a-client/owner.md`.

## Regenerate and check

```sh
python3 protocol/v2/tools/generate.py           # schema, catalog, AsyncAPI, OpenAPI delta
python3 protocol/v2/tools/generate.py --check   # what CI runs: fails on drift
cd protocol/v2/conformance && pip install -e '.[test]' && python -m pytest -q
swift test --package-path protocol/v2/swift
```

CI: `.github/workflows/protocol-v2.yml` (GitHub-hosted `ubuntu-latest`), on every pull request or push that touches
`protocol/`.

## For a backend

Install the kit from a path or from git (pinned by sha) and run it: `conformance/README.md`.

For the app: SwiftPM resolves a git package only at a repository's root, so until `protocol/` has its own repository
(D-128 §2) the app depends on `PisaAGUI` by local path, `.package(path: "<checkout>/protocol/v2/swift")`, as
`prototypes/contract-2/renderer-swift` does.
