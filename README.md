# PISA

**Pretty Intelligent Silicon Assistant**: a personal assistant you host yourself.

PISA is two things:

- **a self-hosted assistant server**: one server you control, your data in its own Postgres database, and the AI
  providers you choose (a cloud API, or your own OpenAI-compatible model server such as vLLM, llama.cpp or
  Ollama);
- **an open interface contract** ([contract 2](protocol/v2/README.md)) that says exactly how any interface talks
  to that server: how to pair, how to send text or audio, and what comes back and how to draw it.

PISA has no screen of its own. You talk to it in one of two ways:

- **through your own interface**: a phone app, a desktop app, a web page, a bot, a terminal. The
  [Build your own client](docs-site/build-a-client/index.md) guide covers every flow with the exact formats and a
  runnable example each; or
- **through the PISA app for iPhone**, a separate, paid product.

The command-line client (`pisa client`) and the conformance kit are developer tools for testing a server or a
client, not an interface for daily use.

## What the server does

What follows is what the server offers to any client that implements the contract. Where a feature needs
something from the client or from a third party, it says so.

- **Conversation in text**, with one assistant that keeps the context of your day, your plans and what it
  already knows about you (`POST /v2/runs`, or the live socket `/v2/live`).
- **Voice**, when your client implements it ([Audio](docs-site/build-a-client/audio.md)): either the client
  recognises speech itself and the server answers in text, with Pisa's voice as audio if you configure a voice
  provider; or natural talk over WebRTC, which needs a live voice provider's key (xAI or Gemini).
- **Tasks, reminders, an agenda and a day plan**, with weather (Open-Meteo, no key). Travel times and place search
  need a maps provider (Apple Maps or Google Maps), and web research needs Tavily, each with your own key:
  [Places and web search](docs-site/places-and-web.md).
- **Mail and calendar** (Google): triage, drafts, and events created, moved and cancelled. This needs your own
  Google Cloud OAuth clients and a client that implements Google Sign-In: [Connectors](docs-site/connectors.md#google).
- **Other sources**: Garmin, and **read-only banking** (PSD2, through Enable Banking), each with your own
  credentials; Apple Health only through a client that reads it on the phone and sends a daily summary.
- **Approvals**: anything that writes, sends or spends on your behalf waits for your yes. Deciding an approval
  that needs a signature, and deleting data, are signed with your client's own device key.
- **Privacy controls**: per-provider AI consent, retention limits, and deleting your data on request, from any
  paired client ([Privacy](docs-site/privacy.md)).
- **Spending limits** on paid AI, voice, maps and web services.

Some features are made for one country, such as the Italian tax drawer and the certified-mail radar. They are
switched on only when `PISA_COUNTRY` says so.

## Quick start

You need Docker with docker compose.

```bash
git clone <repository URL> pisa
cd pisa
cp deploy/.env.example deploy/.env   # then set PISA_TIMEZONE, PISA_SECRETS_KEY, the three passwords and
                                     # COMPOSE_PROFILES (tailscale, caddy or local) with what it needs
cd deploy
docker compose up -d --build
docker compose ps                    # every service healthy, migrate exited 0
```

Then add an AI provider and choose your models, put HTTPS in front of the server, and pair a client: your own, or
the PISA app. To try the server before you have a client, pair the command-line client on the same machine
([Pairing](docs-site/pairing.md#a-developers-machine-no-tls)) and make requests with `pisa client call`. The
[self-hosting guide](docs-site/self-hosting.md) covers each step, plus backups and updates.

## Documentation

The full documentation is in [`docs-site/`](docs-site/index.md):

- [configuration](docs-site/configuration.md): every environment variable;
- [build your own client](docs-site/build-a-client/index.md);
- [pairing](docs-site/pairing.md);
- [AI providers and your own model server](docs-site/ai-providers.md);
- [connectors](docs-site/connectors.md);
- [places and web search](docs-site/places-and-web.md);
- [notifications](docs-site/notifications.md);
- [privacy](docs-site/privacy.md);
- [the `pisa` command](docs-site/cli.md);
- [troubleshooting](docs-site/troubleshooting.md).

## The interface contract

Every client, the PISA app included, talks to the server through the same open contract:

- **Contract 2**, in [`protocol/v2/`](protocol/v2/README.md): AG-UI over a WebSocket for live sessions,
  A2UI for screens, MCP Apps for drawings, and REST for the rest.
- **The REST API**, described in [`protocol/openapi.json`](protocol/openapi.json).
- **The guide** that explains it flow by flow: [Build your own client](docs-site/build-a-client/index.md).

For developers and testing, the repository also includes:
- **a conformance kit** in `protocol/v2/conformance`, which checks a server or a client against the contract;
- **a command-line client** (`pisa client`): pairing, a session token, one REST request at a time, approvals and
  deletes, signed with its own device key.

And an optional push relay (`relay/`), which sends notifications to the PISA app without each server needing
its own Apple key. A client of your own uses push with your own Apple key, or none ([Push](docs-site/build-a-client/push.md)).

## Repository layout

| Path | What it holds |
|---|---|
| `core/` | The server: API, assistant, connectors, background worker, database migrations, tests. |
| `protocol/` | The protocol: contract 2, schemas, fixtures, the conformance kit. |
| `relay/` | The optional push relay. |
| `deploy/` | docker compose and the Postgres image. |
| `docs-site/` | The documentation. |
| `scripts/` | CI and maintenance scripts. |

To run the tests, see [CONTRIBUTING.md](CONTRIBUTING.md). They need Python 3.13, uv and a throwaway Postgres
with pgvector, for example:

```bash
docker run -d --name pisa-test-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17
```

## Licence

- The server, the relay and everything else: [AGPL-3.0-only](LICENSE).
- The protocol in `protocol/`: [Apache-2.0](protocol/LICENSE), so clients of any licence can implement it.
- Third-party works are listed in [NOTICE](NOTICE). See also [TRADEMARKS.md](TRADEMARKS.md).

## Contributing, security and help

- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are signed off under the DCO. See also
  [GOVERNANCE.md](GOVERNANCE.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).
- Security: report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
- Help: [SUPPORT.md](SUPPORT.md).
