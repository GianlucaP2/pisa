# Troubleshooting

## First checks

```bash
cd deploy
docker compose ps                         # postgres, api (api-lan) and worker healthy, migrate exited 0
docker compose exec worker python -c "import urllib.request as u; print(u.urlopen('http://api:8000/readyz').read().decode())"
                                          # the database and the schema (with the local profile:
                                          # curl --insecure https://127.0.0.1:8443/readyz on the host)
docker compose exec worker pisa doctor    # the configuration's weak spots
docker compose logs --tail 100 api worker
```

`/readyz` answers 200 with `"status": "ok"` when the database answers and the schema is current, else 503 with
the failing check named:

- `"database": "unreachable: ..."`: Postgres is down, or `PISA_DATABASE_URL` (its password) is wrong.
- `"schema": "at N, expected M: run pisa migrate"`: a migration is missing. Run
  `docker compose up -d` again (the `migrate` container applies it) and look at `docker compose logs migrate`.

`"push"` and `"relay"` say whether direct push and the relay are set; `off` never makes Pisa not ready
([Notifications](notifications.md)).

## `pisa doctor`

Reads the settings only (no network), prints each finding with what to do, and exits 1 when it found something:

- `PISA_SECRETS_KEY` is not set: pairing codes are stored in plaintext for their 10 minutes, and provider
  keys can't be stored. Set it (see [Configuration](configuration.md)) and restart.

`pisa doctor --live` also opens a real session with natural mode's talker (Gemini Live or Grok Voice, with the
model `PISA_TALKER_MODEL_GEMINI` / `PISA_TALKER_MODEL_XAI` sets, else the catalog's), closes it, and prints OK or
the provider's exact error (its code and words, keys hidden). It spends a little; use it when natural voice
talks fall back to economy.

## `pisa consent`

Pisa sends nothing to an AI model until you allow it for your device (the app's consent screen). When the app
can't show it, or a run or a natural voice talk answers `ai_consent_required`, run `pisa consent` on the server:
it prints every provider and model and what each receives, asks, and records your consent for the paired
device (`--device <id>` when several are paired, `--health` to allow health too, `--yes` not to ask). Consent
again whenever the providers, the models, or the voice talk's mode or talker change.

## Common problems

| Symptom | Cause and fix |
|---|---|
| Pisa does not start: `PISA_TIMEZONE is required` | Set your IANA time zone in `deploy/.env`. Every invalid setting is named the same way. |
| `PISA_SECRETS_KEY is required when PISA_ENV=prod` | Generate a key (see [Self-hosting](self-hosting.md#1-install)). |
| "No model is chosen for <slot>" (the action: "Choose a model for <slot> in Settings → AI models") | No model is chosen for that feature: `pisa models set <slot> <provider:model>` ([AI providers](ai-providers.md)). |
| `pisa pair` says QR pairing needs addresses and a certificate | Run `pisa init --force` for your profile, or set `PISA_ADDRESSES` and `PISA_TLS_CERT` (with docker compose, the file goes in `deploy/tls/`) or `PISA_TLS_PIN_FROM`, or on a developer's machine use insecure loopback mode ([Pairing](pairing.md)). |
| `tailscale` profile: `pisa pair` says "Tailscale hasn't fetched its certificate yet" | Enable HTTPS certificates for the tailnet (the DNS page of the Tailscale admin console), then wait a minute: the Tailscale container retries every minute until it has one (`docker compose logs tailscale`). |
| `caddy` profile: `pisa pair` says the certificate "fails WebPKI verification" or can't be read | Caddy has no valid certificate for the domain yet: the domain's DNS must point at this machine and ports 80 and 443 must reach it (`docker compose logs caddy`); the first certificate can take a minute. `PISA_ADDRESSES` must name the same domain as `PISA_DOMAIN`. Pisa never pins a certificate that fails this check. |
| `local` profile: `api-lan` stops with "PISA_API_TLS_CERT names ..., which is not a readable file" | The server's key and certificate are not in `deploy/tls/`: run `pisa init --mode local --force` (it keeps an existing key). |
| `local` profile: the phone can't reach Pisa | `PISA_LAN_BIND` is `127.0.0.1` (this machine only): set it to `0.0.0.0` in `deploy/.env`, then `docker compose up -d`. Check that the name in `PISA_ADDRESSES` resolves on the phone's network (`.local` names need mDNS on this machine), or use its IP address (`pisa init --force --lan-host 192.168.1.20`). |
| `local` profile: a paired client says the server's key is not the one it paired with | The key in `deploy/tls/` changed (`--rotate-tls`, or the folder was lost): pair the client again. |
| A request answers 403 `auth_failed` "insecure loopback mode" | The server is in insecure loopback mode and answers only its own machine, directly: connect from it, not through a proxy ([Pairing](pairing.md#a-developers-machine-no-tls)). |
| Pairing is paused for one address | 60 failed attempts in 10 minutes from that address; it resumes by itself. Behind a proxy, check `PISA_TRUSTED_PROXIES`, or every client counts as the proxy. |
| A device can't connect after an update | A device paired with the old 6-character code has no device key and was revoked: pair it again ([Updating](self-hosting.md#5-updating)). |
| Paid features stopped | The hard limit or one of your caps is reached ([Spending](ai-providers.md#spending)). |
| A connector shows `error` | Three syncs failed; its `message` names the cause ([Connectors](connectors.md)). |
| Natural voice talks connect but no audio | WebRTC needs UDP: check `PISA_ICE_HOST` and that the UDP range `PISA_VOICE_UDP_PORTS` reaches the server ([Limits](limits.md)), or set a TURN server in `PISA_TALK_ICE_SERVERS` ([HTTPS](self-hosting.md#3-https)). |
| `409 ai_consent_required` (a run, or opening a natural voice talk) | No consent yet, or the AI disclosure changed (a model, or the voice talk's mode or talker): consent again in the PISA app, or run `pisa consent` on the server, or from your client (`POST /v1/ai/consent` with the current `revision`) ([Privacy](privacy.md)). |
| Natural voice talks fall back to economy at once | Run `pisa doctor --live`: it opens the talker and prints its exact error (the key's access, or the model: `PISA_TALKER_MODEL_GEMINI` / `PISA_TALKER_MODEL_XAI`). |
