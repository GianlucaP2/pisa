# Configuration

Pisa reads its settings from environment variables, or from a `.env` file beside it (`deploy/.env` with docker
compose). [`.env.example`](../.env.example) lists them all with a comment each; this page is the reference.
Pisa refuses to start on a value it can't use and names the variable.

## Settings

Read once at start (`core/pisa/settings.py`). Change one, then `docker compose up -d` again.

| Variable | Default | What it does |
|---|---|---|
| `PISA_TIMEZONE` | none: required | Your home time zone, an IANA name (`Europe/London`). While you travel Pisa follows the time zone your client reports. |
| `PISA_LANGUAGE` | `en` | The language Pisa speaks and writes by default: `en` or `it`. A turn in the other language is answered in it; a language set from a client (`PATCH /v1/settings`) wins once made. |
| `PISA_COUNTRY` | empty | Your country, ISO 3166-1 alpha-2 (`GB`, `US`, `IT`). The default region of phone numbers written without `+`, and the switch for features made for one country (with `IT`: public transport and weather feeds, certified email, the tax calendar). Empty: none of them. |
| `PISA_ENV` | `dev` | `dev`, `test` or `prod`. In `prod`, `PISA_SECRETS_KEY` is required. `.env.example` sets `prod`. |
| `PISA_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. The commands you run and read (`pisa setup`, `pair`, `secrets`, `doctor`, `models`, `catalog`, `consent`) log to stderr, warnings and up only (everything at `debug`). At `debug`, failures are logged with their traceback, which can quote stored data: use it only to diagnose. |
| `PISA_TRUSTED_PROXIES` | empty | Comma-separated addresses or CIDRs whose `X-Forwarded-For` Pisa believes. Empty: the client is the TCP peer. See [HTTPS](self-hosting.md#3-https). |
| `PISA_DATABASE_URL` | `postgresql://pisa_app@localhost:5432/pisa` | The app role's connection (read/write, no DDL). Docker compose builds it from `PISA_APP_PASSWORD`. |
| `PISA_MIGRATE_DATABASE_URL` | empty | The migration role's connection (DDL), used by `pisa migrate` only. |
| `PISA_DBOS_DATABASE_URL` | empty | The worker's scheduling database (`pisa_dbos`), used by `pisa worker` only. |
| `PISA_SECRETS_KEY` | empty | A Fernet key (44 characters) that encrypts provider keys and tokens at rest. Without it provider keys can't be stored and pairing codes are kept in plaintext for their 10 minutes (`pisa doctor` warns). |
| `PISA_API_HOST` | `127.0.0.1` | Where the API listens. Docker compose sets it. |
| `PISA_API_PORT` | `8000` | The API's port. |
| `PISA_API_TLS_CERT` | empty | The API serves HTTPS itself with this certificate (a PEM file's path), with `PISA_API_TLS_KEY`. Docker compose's `local` mode sets both to the pair `pisa init` makes in `deploy/tls/`. Both empty: plain http, for a TLS proxy in front. |
| `PISA_API_TLS_KEY` | empty | That certificate's private key (a PEM file's path, readable by the API's user only). |
| `PISA_APNS_TEAM_ID` | empty | Your Apple Developer team id, for direct push and for the Apple Maps key. See [Notifications](notifications.md) and [Places and web search](places-and-web.md). |
| `PISA_APNS_TOPIC` | empty | Your own iOS client's bundle id, for direct push. |
| `PISA_PUSH_RELAY_URL` | empty | The push relay's https origin, for relay push. Empty: relay mode is off. |
| `PISA_EXPERIMENTAL` | empty | The experimental features to turn on, by id, comma-separated (`garmin,bank`), or `all`. Empty: only the supported features, which is everything built; the stubs and the integrations never built or never used (Garmin, Polar, Withings, the banks, the tax drawer, the admin radar, the xAI talker) are hidden (their connectors, routes and setup questions). See [Experimental features](experimental.md). The ids are in `core/pisa/domain/experimental.py`, the reasons in `docs/pisa/SUPPORT-MATRIX.md`. An unknown id is ignored with a warning in the log. Discovery (`GET /v1/server`) lists each one that is on as `experimental.<id>`. |
| `PISA_REVIEW_MODE` | `false` | App Store review mode: a demo server for Apple's reviewer, with synthetic data only. On (`1`): a few invented tasks are seeded into a database that has none (once), `pisa pair --review` makes a code valid 30 days that pairs up to 3 devices without the 6-digit check, and every connector and outbound write is off whatever `PISA_EXPERIMENTAL` says (no mail, no calendar, no push relay, no web or places provider; only your AI provider and the weather). The app can't change the AI keys or add a model server. Discovery lists the feature `review_mode`. Never on a server with anyone's real data. See [For App Store reviewers](app-review.md). |
| `PISA_ADDRESSES` | empty | How clients reach the server, shown in the pairing QR: comma-separated `https://` URLs and `relay://<host>/<handle>` addresses, at most 6. |
| `PISA_TLS_CERT` | empty | The TLS certificate clients pin, or its public key (its key must stay the same: [the pin](self-hosting.md#the-pin-read-this-before-you-choose)): a PEM file's path (docker compose: `/etc/pisa/tls/<file>`, from `deploy/tls/`) or the PEM itself (`\n` for its line breaks). Empty: QR pairing is off. |
| `PISA_TLS_PIN_FROM` | empty | Without `PISA_TLS_CERT`: the TLS proxy to read the certificate from, `host:port` (docker compose's `caddy` mode: `caddy:443`). Pisa pins the leaf certificate it presents for the first `https://` address in `PISA_ADDRESSES`, and only after WebPKI verification (the system's roots and the host name); read again every 10 minutes. |
| `PISA_TLS_NEXT_KEY` | empty | The next TLS key, the same way, announced to paired clients before a rotation. |
| `PISA_INSECURE_LOOPBACK_GATEWAY` | `false` | With insecure loopback mode in a container: also answer the container's default gateway, where the host's own loopback connections arrive under rootful Docker on Linux with the port published on `127.0.0.1` only. Not for Docker Desktop, rootless Docker or Podman, where other machines can arrive from the gateway too. Off: only loopback inside the container (`docker compose exec`). |
| `PISA_INSECURE_LOOPBACK_PAIRING` | `false` | A developer's machine: pair and talk over plain http on loopback, without a certificate. Refused unless `PISA_TLS_CERT` is empty and every `PISA_ADDRESSES` entry is `http://` on loopback; the API then answers only this machine. See [Pairing](pairing.md#a-developers-machine-no-tls). |
| `PISA_OLLAMA_BASE_URL` | empty | An Ollama server's root (`http://127.0.0.1:11434`). Empty: no Ollama provider. |
| `PISA_TALK_ICE_SERVERS` | `[]` | The WebRTC ICE servers for voice talks, a JSON list (`[{"urls": ["turn:..."], "username": "...", "credential": "..."}]`). `[]`: the client must reach the server directly (a VPN). |
| `PISA_VOICE_UDP_PORTS` | empty | The voice talks' UDP port range (`40000-40019`, or one port), one port per talk. Empty: any free port. The compose file sets `40000-40019`. |
| `PISA_ICE_HOST` | empty | The address the client reaches those ports at, offered as the talk's ICE candidate (an IP, or a name resolved at each talk): the public domain or the LAN address (with `tailscale`, leave it empty: see `PISA_ICE_HOST_FILE`). Empty: the address in `PISA_ICE_HOST_FILE`, else each interface's own address. |
| `PISA_ICE_HOST_FILE` | empty | When `PISA_ICE_HOST` is empty: a file holding that address (one IPv4 address), read at each talk. The compose file sets `/etc/pisa/ts-cert/ip`, which the `tailscale` profile's sidecar writes with the node's tailnet IP once it has logged in. Empty, missing, or not an IPv4 address: each interface's own address. |
| `PISA_VOICE_CONNECT_TIMEOUT_S` | `15` | A talk whose audio hasn't connected this many seconds after Pisa's answer ends (`fallback`: the client goes on without live voice; text works). |
| `PISA_AGENT_URL` | empty | Pisa's own public https origin, where its agent card and signing keys are published. Empty: Pisa signs nothing. |
| `PISA_VOICE_GATE` | `true` | On: once your voice is enrolled, a voice talk answers only your voice. Off: it answers anyone. |
| `PISA_VOICE_MODELS_DIR` | `/app/models/voice` | Where the voice gate's speaker models are (the image fetches them at build time). |
| `PISA_VOICE_EMBEDDER` | `resnet34` | The voice gate's speaker model: `resnet34` (the only one shipped). |
| `PISA_LEARN_CONTACT_EMAIL` | empty | The contact address Unpaywall asks callers for (finding open copies of papers). Empty: Unpaywall is skipped. |
| `PISA_MODEL_CATALOG` | empty | A JSON file laid over the shipped model catalog (`core/pisa/domain/model_catalog.json`): per-model reasoning effort and the voice connectors' default models, per provider; an empty string removes a shipped value. Empty: the shipped catalog. |
| `PISA_TALKER_MODEL_GEMINI` | empty | The Gemini Live model natural voice talks open (as Google names it). Empty: the model catalog's (`defaults.talker.gemini`). `pisa setup` offers the Live models the catalog knows; `pisa doctor --live` opens it once to check. |
| `PISA_TALKER_MODEL_XAI` | empty | The same for Grok Voice (`defaults.talker.xai`). |
| `PISA_HARD_DAILY_EUR` | `3` | The hard daily spending limit over every paid service together (AI, voice, maps, web), in euros. The caps a client sets (`PUT /v1/ai/caps`) can only be lower. |
| `PISA_HARD_MONTHLY_EUR` | `30` | The same, per calendar month. |

## Read where they are used

These are read when the feature runs, not at start.

| Variable | Default | What it does |
|---|---|---|
| `PISA_DRAWING_BOX` | empty | `on`: drawings are offered in a sealed box on the client. Turn it on only once your client (or your PISA app version) supports it. |
| `PISA_CLIENT_FILE` | `~/.config/pisa/client.json` | Where `pisa client` keeps its device key and what it paired with. |
| `PISA_HOST_NAME` | this machine's hostname | `pisa init --mode local`: the name its default `--lan-host` is made from (`<name>.local`). The install scripts pass the host's, since a container has a name of its own. |
| `PISA_HOST_TZ` | none | `pisa init`: the host's time zone (an IANA name), the default its time zone question offers. The install scripts pass the host's, since a container has a zone of its own; a value that is not a valid IANA name is ignored. |
| `PISA_VOICE_UDP_PORTS` | none: required | `pisa voice-relay` (the compose `tailscale` profile): the range it relays from the tailnet to the API. |
| `PISA_VOICE_RELAY_TO` | `api` | `pisa voice-relay`: the host it relays to, at the same ports. |

## Docker compose only

Read by `deploy/docker-compose.yml`, never by Pisa itself.

| Variable | What it does |
|---|---|
| `POSTGRES_PASSWORD` | The database superuser's password. No Pisa process gets it. |
| `PISA_MIGRATE_PASSWORD` | The migration role's password (`pisa_migrate`). |
| `PISA_APP_PASSWORD` | The app role's password (`pisa_app`). |
| `PISA_READONLY_PASSWORD` | Optional: a read-only login for your own queries. |
| `COMPOSE_PROFILES` | The way in, exactly one of `tailscale` (the default), `caddy` or `local` ([Self-hosting](self-hosting.md#the-way-in-compose-profiles)). |
| `TS_AUTHKEY`, `TS_HOSTNAME` | `tailscale`: the auth key for the sidecar's first login, and its name on the tailnet (`pisa`). |
| `PISA_DOMAIN`, `PISA_ACME_EMAIL` | `caddy`: the domain Caddy gets a certificate for, and the contact address of its ACME account (required). |
| `PISA_LAN_BIND`, `PISA_LAN_PORT` | `local`: where the API is published (`127.0.0.1`, `8443`). `0.0.0.0` opens it to your network. |
| `PISA_LAN_UID`, `PISA_LAN_GID` | `local`: who `api-lan` runs as, the owner of `deploy/tls/key.pem` (`pisa init` writes them; default `10001`, the image's user). |
| `PISA_LAN_TLS_CERT`, `PISA_LAN_TLS_KEY` | `local`: the certificate and key `api-lan` serves HTTPS with (default `/etc/pisa/tls/cert.pem` and `key.pem`). Both set and empty: plain http, for a developer's loopback setup or your own TLS proxy on the host. |
| `PISA_VOICE_UDP_BIND` | `caddy` (and `tailscale`): where the API's voice range is published (`127.0.0.1`; `pisa init` writes `0.0.0.0` for `caddy`). `local` publishes it on `PISA_LAN_BIND`. |
| `PISA_IMAGE` | The image to run. Empty: built from `core/` as `pisa:local`. A release is `ghcr.io/<owner>/pisa:<version>`. |
| `PISA_SUBNET`, `PISA_GATEWAY`, `PISA_IP_RANGE`, `PISA_PROXY_IP` | The compose network's subnet (`172.29.0.0/24`); its gateway (`.1`), where a proxy on the host comes from; the range the containers' own addresses come from (`172.29.0.128/25`); and the fixed address of the profile's proxy (`.10`: the Tailscale container or Caddy, whichever profile runs), outside that range, the only address `PISA_TRUSTED_PROXIES` names. Change them together. |

[`deploy/.env.example`](../deploy/.env.example) lists these with the settings every install needs.

The three role passwords are used when the data volume is first created (`deploy/postgres/init`).
