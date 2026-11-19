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
| `asyncapi-live.json` | AsyncAPI 3.0 for `/v2/live`; payloads are `$ref`s into the schema | `tools/gen_asyncapi.py` |
| `openapi-v2-delta.json` | OpenAPI 3.1: the REST endpoints contract 2 adds or changes (spec §10) | `tools/gen_openapi_delta.py` |
| `examples/server.json`, `examples/server-minimal.json` | a `GET /v1/server` document, and the smallest conformant one (only the safety features) | by hand |
| `vendor/` | AG-UI 1.0 and A2UI 1.0 schemas, unmodified, pinned by sha256 (`vendor/SHA256SUMS`) | copied from upstream |
| `fixtures/` | recorded streams, every AG-UI and `pisa.*` event, the gaps' valid and invalid frames, enrolment channels, approval vectors | `conformance/src/pisa_conformance/reference/` |
| `conformance/` | `pisa-conformance`: the Python kit and CLI both backends run | |
| `swift/` | `PisaAGUI`: the Swift twin (codec, frames, negotiation, discovery, feature gate, approval canonical text, `LiveClient`, enrolment frames). Swift 6, Foundation only, builds on Linux | |

## Frozen

`FROZEN.json` holds the sha256 of the schema (contract `2.0`) and of `core:1`; a test fails if either changes.
- A **contract minor** (an optional field, a new `pisa.*` event, a feature) bumps `x-pisa-contract` in the
  generator, regenerates, and records the new digest in `FROZEN.json`, keeping the old one in `history`.
- A **catalog** never changes: a new component is a new catalog (`core:2`, a superset) beside `core:1`.
- Anything else is a new major (CONTRACT-2 §12).

| File | sha256 |
|---|---|
| `schema/pisa-contract-2.schema.json` (contract 2.0) | `9eae9434ee0855ced551fcce2337aa8d1ceb9f203f9b49a2a60d9188364c02bf` |
| `catalog/pisa-core.catalog.v1.json` (`core:1`) | `c464833c2aa3184107e042071c0ddf477b7ef809d4d53d41488fd79f39e272cc` |

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
