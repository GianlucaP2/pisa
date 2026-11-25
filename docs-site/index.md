# Pisa documentation

Pisa is a personal assistant you host yourself: one server you control, your data in its own Postgres
database, the AI providers you choose, and an open contract any interface can speak. Pisa has no screen of its
own: you talk to it through an interface you build ([Build your own client](build-a-client/index.md)), or through
the free PISA app for iPhone (no in-app purchases). The command-line client (`pisa client`) is a developer and
testing tool.

## Install it

- Step by step, for people who have never used a terminal: [on a Mac](install/mac.md) (start here; the
  supported way), [on Linux](install/linux.md), [on a Raspberry Pi](install/raspberry-pi.md),
  [on a VPS with your own domain](install/vps.md), and [on Windows](install/windows.md) (coming soon:
  experimental, community testing).
- [Limits](limits.md): what the self-hosted setup does not do yet, and backups, which are your job.

## Run it

- [Self-hosting](self-hosting.md): install with docker compose, first run, HTTPS (Caddy, Tailscale serve,
  Cloudflare Tunnel), backups and restore, updating and migrations.
- [Configuration](configuration.md): every environment variable.
- [The `pisa` command](cli.md): every server command.
- [Troubleshooting](troubleshooting.md): `/readyz`, `pisa doctor` and common problems.

## Set it up

- [Pairing](pairing.md): QR codes, the 6-digit check, the command-line client, signed operations, devices.
- [AI providers and models](ai-providers.md): provider keys, feature slots, spending limits.
- [Your own model server](models/index.md): OpenAI-compatible servers and the capability probe, with a page for
  [vLLM](models/vllm.md), [SGLang](models/sglang.md), [llama.cpp](models/llama-cpp.md),
  [Ollama](models/ollama.md), [LM Studio](models/lm-studio.md), [LiteLLM](models/litellm.md) and
  [TGI](models/tgi.md); [upgrading](models/upgrading.md) from before automatic probing.
- [Connectors](connectors.md): mail and calendar (and the Google Cloud setup they need), health, banks, and the
  other sources.
- [Places and web search](places-and-web.md): Apple Maps, Google Maps and Tavily: which key goes where, what each
  enables, what it costs.
- [Notifications](notifications.md): push through the relay (the PISA app), directly with your own Apple key, or
  none.
- [Privacy policy](privacy.md): what goes to which provider, retention, deleting your data.
  [For App Store reviewers](app-review.md): how to review the PISA app.
- [Experimental features](experimental.md): what is off by default, how to turn it on, how to report.
- [Memoria](memoria.md): the standing instructions and notes Pisa keeps because you asked, their limits, and
  Settings → Memoria's REST routes.

## Build on it

- [Build your own client](build-a-client/index.md): every flow a client needs, with the exact formats and a
  runnable example each: pairing and sessions, text chat, audio, the events and what to draw, screens,
  approvals, push, deleting data, more devices, every error, and testing with the conformance kit.
- [The API](api.md): `protocol/openapi.json`, contract 2 in `protocol/v2/`, authentication and errors.
