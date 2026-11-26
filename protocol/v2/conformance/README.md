# pisa-conformance

The conformance kit for PISA contract 2 (`protocol/v2`). Both backends (the public backend and the new
core) run it in CI; the app's Swift twin is `protocol/v2/swift`. Apache-2.0 (`protocol/LICENSE`, `protocol/NOTICE`).

It checks:
- every server frame against the **official** AG-UI 1.0 schema, A2UI 1.0 with the PISA catalog `core:1`, and
  `pisa-contract-2.schema.json` (all vendored or frozen in `protocol/v2`, bundled in the wheel);
- the AG-UI lifecycle on every thread, and `seq` growing by one per frame;
- the enrolment data channel (G5), app frames, and `GET /v1/server`;
- negotiation, in `live` mode: the server never sends what the app's hello did not list (CONTRACT-2 §4.2).

It carries the recorded fixture streams, the approval vectors (`fixtures/approval_vectors.json`: JCS, digest,
canonical text, the phone's refusals and the server's resume checks, as data for any language), the gap fixtures,
and a reference emitter (`pisa_conformance.reference`) that produces them.

## Install

Python 3.11 or later. Pick one:

```sh
# From a checkout (a backend repo that vendors or submodules this one):
pip install ./protocol/v2/conformance                 # add [live] for live mode, [test] for the kit's own tests

# From git, pinned to a commit (what a backend's CI should do):
pip install "pisa-conformance[live] @ git+https://github.com/<owner>/<repo>@<sha>#subdirectory=protocol/v2/conformance"
```

Both build the wheel from the source tree: the contract files are copied in from `protocol/v2` at build time, so an
installed kit needs no checkout. (Building an sdist on its own is not supported.) `PISA_PROTOCOL_V2=<dir>` points
the kit at another copy of `protocol/v2`.

## Use

```sh
pisa-conformance selftest                                   # every bundled fixture
pisa-conformance stream recorded.jsonl                      # a recorded /v2/live session, one frame per line
pisa-conformance stream --client app.jsonl                  # app frames
pisa-conformance stream --enrol enrol.jsonl                 # an enrolment data channel
pisa-conformance discovery https://pisa.example/v1/server   # or a file
PISA_TOKEN=... pisa-conformance live wss://pisa.example/v2/live --token-env PISA_TOKEN \
    --send run.jsonl --seconds 10 --record got.jsonl
```

`live` sends a conservative hello (Rings 0 and 1, catalog `core:1`, a synthetic device id) unless `--hello` gives
one, then any `--send` frames after `welcome`, records what arrives for `--seconds`, and checks it. The token is read
from the named environment variable, never from the command line. Binary audio frames are skipped.

Exit status: 0 clean, 1 a check failed, 2 usage or connection error.

## In a backend's CI

```yaml
- run: pip install "pisa-conformance[live] @ git+https://github.com/<owner>/<repo>@<sha>#subdirectory=protocol/v2/conformance"
- run: pisa-conformance selftest
- run: python -m your_backend.record_fixture_streams out/      # replay the scenarios through your own emitter
- run: pisa-conformance stream out/*.jsonl
- run: pisa-conformance live ws://127.0.0.1:8000/v2/live --token-env PISA_TOKEN --send protocol-run.jsonl
```

For Schemathesis (install `[openapi]`), `pisa_conformance.openapi.bundled(operations)` gives the OpenAPI delta as one
self-contained document, its references to the contract schema, the vendored AG-UI and A2UI schemas and v1's
`openapi.json` made local: `schemathesis.openapi.from_dict(bundled({"getApproval", ...}))`. The new core runs it on
its own endpoints in `core/tests/api/test_schemathesis.py`.

and port `fixtures/approval_vectors.json` into the backend's own test suite (each verifier case runs on a fresh
verifier; `expect` lists the result of each submission in turn).

## The kit's own tests

```sh
cd protocol/v2/conformance && pip install -e '.[test]' && python -m pytest -q
```

Set `ASYNCAPI_3_SCHEMA` to `@asyncapi/specs` `schemas/3.0.0.json` to also validate `asyncapi-live.json` (CI does).
Regenerate after changing a generator: `python3 protocol/v2/tools/generate.py` (schema, catalog, AsyncAPI, OpenAPI
delta), `python -m pisa_conformance.reference.scenarios` (streams), `.all_events`, `.enrol`. The schema and the
catalog are frozen (`protocol/v2/FROZEN.json`).
