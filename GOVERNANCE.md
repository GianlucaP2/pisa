# Governance

PISA is a young project with a small team. This page says who decides what, and how that will change as the
community grows.

## Roles

- **Users** run PISA and report what goes wrong or what is missing.
- **Contributors** send pull requests, review, answer questions, and improve the docs. Anyone can be one.
- **Maintainers** review and merge pull requests, triage issues, cut releases, and handle security reports. They
  are listed in `.github/CODEOWNERS` (the `<org>/<maintainers-team>` team).
- **The lead maintainer**, `<maintainer>`, sets the project's direction and decides when the maintainers cannot
  agree.

## How decisions are made

- Most decisions happen in pull requests and issues: a maintainer's approval and green CI merge a change.
- A larger change (a new subsystem, a protocol change, a new dependency with a new licence, a change to this page)
  starts as a discussion or an issue labelled `proposal`, stays open for at least a week for comments, and is
  decided by the maintainers by consensus. Without consensus, the lead maintainer decides and writes down why.
- The protocol (`protocol/`) is a contract other people implement: a breaking change needs a new version, never an
  edit of a released one.

## Becoming a maintainer

A contributor who has made sustained, good-quality contributions and reviews may be invited by the maintainers.
Maintainers who are inactive for six months may be moved to emeritus status, with thanks, and can come back.

## Conduct and security

The [Code of Conduct](CODE_OF_CONDUCT.md) applies everywhere in the project. Security reports follow
[SECURITY.md](SECURITY.md) and are handled by the maintainers in private.

## Changes to this page

By pull request, decided as a larger change (above).
