# Contributing to PISA

Thank you for helping. This page says how a change gets in.

## Before you start

- For a bug, open an issue with the bug form. For a new behaviour, open a feature request or a discussion first,
  so that nobody spends days on something that cannot be merged.
- Security problems never go in a public issue: see [SECURITY.md](SECURITY.md).
- Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Developer Certificate of Origin (DCO)

PISA uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/) instead of a contributor
licence agreement. By signing off a commit you certify the DCO for that commit:

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Sign off every commit with `git commit -s`. It adds a line with the name and address in your git configuration:

```text
Signed-off-by: Your Name <you@example.org>
```

A pull request whose commits are not all signed off cannot be merged (the DCO check says which). To sign off
commits you already made: `git rebase --signoff main`, then push again.

Your contribution is licensed under the licence of the files it changes: AGPL-3.0-only, or Apache-2.0 under
`protocol/`.

## Making the change

The backend is in `core/` (Python, managed with [uv](https://docs.astral.sh/uv/)); the push relay in `relay/`; the
device protocol, its schemas and the OpenAPI document in `protocol/`.

```bash
cd core
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
NODE_OPTIONS=--max-old-space-size=4096 uv run pyright
uv run lint-imports
uv run python ../scripts/ci/gen_openapi.py --check   # after an API change: run it without --check and commit
PISA_TEST_ADMIN_URL=postgresql://postgres:<password>@127.0.0.1:55432/postgres uv run pytest -n auto -q
```

The tests need a throwaway PostgreSQL with pgvector (the README shows one with Docker). `relay/` has the same
commands, without the database.

- Keep a pull request to one subject. Add or change tests with the behaviour.
- Tests and fixtures use invented data only: no real names, addresses, phone numbers, accounts or precise
  coordinates (the suite checks this: `core/tests/test_no_personal_data.py`, `test_no_precise_coordinates.py`).
- Never commit a secret, even a revoked one. Secret scanning and push protection are on.
- Write commit titles in the [Conventional Commits](https://www.conventionalcommits.org/) form (`fix: …`,
  `feat: …`, `docs: …`): the changelog and the release version are made from them.
- A change to `protocol/` is a change to a contract other people implement: say in the pull request what a client
  must do about it.

## Experimental features

Everything built is supported and on by default. Only stubs and integrations never built or never used against
the real service are **experimental**: off by default, and they may change or go away. The list, with one line
each, is in [docs-site/experimental.md](docs-site/experimental.md): `garmin`, `polar`, `withings`, `bank`,
`ade`, `radar` and `talker_xai`.

- **Turn one on**: add `PISA_EXPERIMENTAL=<id>,<id>` (e.g. `PISA_EXPERIMENTAL=garmin`) to your server's `.env`
  (`deploy/.env` with docker compose), then restart (`docker compose up -d`).
- **Report**: open an issue with the bug form and the `experimental` label. Name the id, the release, how you
  installed, the steps and what happened. Remove personal data first.
- **Test one**: use a test install and test accounts where you can, never someone else's data. Follow the
  feature's docs page from a fresh start, and note each step and what you saw. Run the tests for the code it
  touches (above).
- **Move it to supported**: open a pull request that shows it works end to end: what you ran (the release,
  the install type, the provider or the device), the steps and the result (logs or screenshots with personal
  data removed), plus any fix or test the run needed. The same pull request takes the id off the experimental
  switch and off the list in the docs, so the feature is on for everyone. A maintainer decides, as for any
  change.

## Review

A maintainer reviews and approves every pull request. CI must be green. The maintainers may ask for changes,
take over a stale pull request, or decline a change that does not fit the project's direction (GOVERNANCE.md).
