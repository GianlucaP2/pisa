# Repository settings checklist

Settings that live in GitHub, not in files. **A person applies them by hand, once, when the public repository is
created**, after the lead maintainer's explicit yes to creating it. Nothing here is applied by a script.

Placeholders: `<org>` (the owning organisation), `<repo>` (the repository's name), `<maintainers-team>` (the team
that reviews), `<maintainer>` (the lead maintainer), `<security contact>`, `<conduct contact>`,
`<trademark holder>`, `<trademark contact>`.

## Before the first push

- [ ] The repository is created **empty** (no README, licence or .gitignore from GitHub's form), in `<org>`.
- [ ] Visibility stays **private** until every box below the first push is ticked; make it public last.
- [ ] Replace the placeholders in `.github/CODEOWNERS`, `.github/ISSUE_TEMPLATE/config.yml`, `GOVERNANCE.md`,
      `CODE_OF_CONDUCT.md`, `TRADEMARKS.md`, in a commit after the first.
- [ ] The team `<org>/<maintainers-team>` exists and has **Write** (or Maintain) access; nobody else does.

## Security (Settings → Code security)

- [ ] **Private vulnerability reporting**: on (SECURITY.md sends reporters there).
- [ ] **Dependency graph**: on. **Dependabot alerts**: on. **Dependabot security updates**: on.
- [ ] **Grouped security updates**: on (dependabot.yml groups the version updates).
- [ ] **Secret scanning**: on. **Push protection**: on. **Validity checks** and **non-provider patterns**: on where
      offered.
- [ ] **Code scanning**: set to the advanced setup (the `codeql` workflow), not the default setup, so the two do
      not run twice.
- [ ] Settings → Actions → General: **Allow select actions**, limited to the ones pinned in `.github/workflows/`
      (actions/*, github/codeql-action, ossf/scorecard-action, googleapis/release-please-action,
      anchore/sbom-action, docker/*, sigstore/cosign-installer, astral-sh/setup-uv); **Require actions to be pinned to a full-length commit SHA**:
      on.
- [ ] Workflow permissions: **Read repository contents** by default; **Allow GitHub Actions to create and approve
      pull requests**: on only if release-please uses the default token (off with RELEASE_PLEASE_TOKEN).
- [ ] Fork pull request workflows: **Require approval for first-time contributors**.

## Rulesets (Settings → Rules → Rulesets)

A ruleset `main` on the default branch, enforcement **Active**, with no bypass except `<org>/<maintainers-team>`
for emergencies (pull requests only, never direct pushes):
- [ ] Restrict deletions; block force pushes.
- [ ] Require linear history.
- [ ] Require a pull request before merging: 1 approval, dismiss stale approvals on new commits, require review
      from Code Owners, require approval of the most recent push, require conversation resolution.
- [ ] Require status checks to pass, with the branch up to date: `core`, `relay`, `dco` (ci), `analyze (python)`,
      `analyze (actions)` (codeql).
- [ ] Require code scanning results: CodeQL, alerts of severity **High or higher** block.
- [ ] Require signed commits: optional (the DCO sign-off is required either way).

A ruleset `tags` on `refs/tags/v*`, **before the first release tag**:
- [ ] Restrict creations, updates and deletions to the release-please token's identity (and the maintainers).
      The install scripts fetch the bundle by tag and check it against the release's `SHA256SUMS`: a release
      tag must never move.

## Features (Settings → General)

- [ ] **Discussions**: on, with the categories Announcements (maintainers only), Q&A, Ideas, Show and tell,
      Trademark.
- [ ] **Issues**: on. **Wiki**: off. **Projects**: as wanted. **Sponsorships**: as the lead maintainer decides.
- [ ] Pull requests: allow **squash merging** only (title = the pull request's Conventional Commit title);
      **Always suggest updating pull request branches**: on; **Automatically delete head branches**: on.
- [ ] Labels: create those in `.github/labels.yml` (and delete GitHub's defaults that aren't listed).
- [ ] Topics and description: set; the website field points to the docs once published.

## Releases and packages

- [ ] The secret `RELEASE_PLEASE_TOKEN`: a GitHub App installation token or a fine-grained token, limited to this
      repository, with contents and pull-requests write (release-please.yml). Without it, the tag release-please
      makes starts nothing, and a release.yml run started by hand signs its images with the `workflow_dispatch`
      trigger, which the documented `cosign verify` (the push trigger) refuses: set the token before the first
      release.
- [ ] After the first release, the packages `ghcr.io/<org>/pisa` and `ghcr.io/<org>/pisa-relay`: link them to the
      repository, set them **public**, and give the repository's Actions **write** access in the package settings.
- [ ] Check an attestation and the signature once: `gh attestation verify oci://ghcr.io/<org>/pisa:<version> --repo
      <org>/<repo>`, and the `cosign verify` in release.yml's header; and that the release has `IMAGES` and
      `SHA256SUMS` (an install of a release without them refuses).

## Last

- [ ] The release gates are met, and the lead maintainer said yes to going public.
- [ ] Settings → General → Danger zone → **Change visibility** to public.
- [ ] Turn on the OpenSSF Scorecard badge in the README once the first `scorecard` run has published.
