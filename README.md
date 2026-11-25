# PISA

**Pretty Intelligent Silicon Assistant**: a personal assistant you host yourself. Your server, your AI keys,
your data.

> Status: **v0.1.2**. The server is usable today. The PISA app for iPhone is coming soon; until then you use
> your own client, built on the open [interface contract](protocol/v2/README.md).

## What it does

- **Conversation, in text or by voice**, with one assistant that keeps the context of your day.
- **Mail and calendar** (Google), through your own Google Cloud project: triage, drafts, and events created,
  moved and cancelled, each write after your yes.
- **Day planning**: tasks, agenda and weather turned into a plan for the day.
- **Reminders** at the time you ask for, delivered as a push when the app is closed.
- **Memoria**: standing instructions Pisa follows ("always answer briefly") and notes it keeps and finds again.
- **Places**: search and travel times, with your own maps key.
- **Voice**: speech in and out, or a live talk, with the voice provider you choose.

You choose the AI providers: a cloud API with your own key, or your own OpenAI-compatible model server (vLLM,
llama.cpp, Ollama). Spending limits cap the paid ones.

## Install

You need Docker with docker compose (macOS, Linux or Raspberry Pi OS 64-bit).

```bash
REPO=owner/name   # this repository, as in its address on GitHub
curl -fsSL "https://raw.githubusercontent.com/$REPO/v0.1.2/deploy/install.sh" | PISA_REPO=$REPO PISA_VERSION=v0.1.2 bash
```

The latest release's notes carry this line with the repository filled in: copy it from there.

The script checks every file against the release's checksums, asks a few questions (`pisa init`), and starts
the server. Read it first if you like: [`deploy/install.sh`](deploy/install.sh). The
[self-hosting guide](docs-site/self-hosting.md) covers HTTPS, pairing a client, backups and updates.

## Documentation

The documentation is in [`docs-site/`](docs-site/index.md) and is published as a website by
`.github/workflows/docs.yml`. Start with [self-hosting](docs-site/self-hosting.md),
[configuration](docs-site/configuration.md) and [build your own client](docs-site/build-a-client/index.md).

## Privacy

- Your data stays in your own server, in its own Postgres database.
- An AI or voice provider sees only what a request needs, and only after you consent to that provider.
- You can delete your data from any paired client; logs hold no user data at any level. See
  [Privacy](docs-site/privacy.md).
- You are the operator of your server: your Google project and OAuth client, your AI provider, their terms; PISA
  is provided under the AGPL-3.0 "as is", without warranty ([Operator responsibility](docs-site/privacy.md#operator-responsibility)).

## Licence and contributing

- The server, the relay and everything else: [AGPL-3.0-only](LICENSE). The protocol in `protocol/`:
  [Apache-2.0](protocol/LICENSE). Third-party works: [NOTICE](NOTICE); see also [TRADEMARKS.md](TRADEMARKS.md).
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) (DCO sign-off), [GOVERNANCE.md](GOVERNANCE.md), the
  [Code of Conduct](CODE_OF_CONDUCT.md).
- Security: report vulnerabilities privately, as in [SECURITY.md](SECURITY.md). Help: [SUPPORT.md](SUPPORT.md).
