## What and why

<!-- What this changes, and the problem it solves. Link the issue: "Fixes #123". -->

## How it was tested

<!-- The tests added or changed, and what you ran. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`, the DCO in CONTRIBUTING.md).
- [ ] The title is a Conventional Commit (`fix: …`, `feat: …`, `docs: …`).
- [ ] Tests cover the change, and use invented data only (no real names, addresses, numbers or coordinates).
- [ ] No secret, key or token is in the diff.
- [ ] After an API change: `protocol/openapi.json` regenerated (`scripts/ci/gen_openapi.py`).
- [ ] A change to `protocol/` says what a client must do about it.
- [ ] Docs updated where behaviour changed.
