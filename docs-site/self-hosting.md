# Self-hosting Pisa

Pisa runs on one machine you control: a small VPS, a home server, a spare laptop. It needs Docker with the
compose plugin, about 2 GB of memory and a few GB of disk. Everything it knows about you stays in its own
Postgres database on that machine; it calls out only to the AI, map, voice and web providers you configure.

This page covers:

1. [Install](#1-install): Postgres and Pisa with docker compose, one `.env` file.
2. [First run](#2-first-run): pair a client, add a provider key, choose your models, consent.
3. [HTTPS](#3-https): three ways to reach Pisa over TLS: Caddy, Tailscale serve, Cloudflare Tunnel, and which
   of them keep the key clients pin.
4. [Backups](#4-backups): `pg_dump` and restic, to a bucket of your choice.
5. [Updating](#5-updating).

This page is the reference, for a checkout of the repository. To install a release step by step, with the
install script and `pisa init`, see the install guides: [Mac](install/mac.md), [Linux](install/linux.md),
[Raspberry Pi](install/raspberry-pi.md), [VPS](install/vps.md), [Windows](install/windows.md) (experimental); and what the setup
does not do yet: [Limits](limits.md). Copy the install command from the latest release on the repository's
**Releases** page: its notes carry the exact commands for that release, with the repository and the version
already filled in.

What notifications show and reveal in each mode: [Notifications](notifications.md). Every other
page: [the index](index.md); every setting: [Configuration](configuration.md).

## 1. Install

```bash
git clone <your copy of the Pisa repository> pisa
cd pisa
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env`. The settings are documented in the file itself (the optional ones are in `.env.example`
at the top of the repository: add any you want); at least:

| Setting | What to put |
|---|---|
| `PISA_TIMEZONE` | Required. Your home time zone, an IANA name: `Europe/London`, `America/Chicago`. |
| `PISA_LANGUAGE` | `en` (the default) or `it`. |
| `PISA_COUNTRY` | Optional, your ISO country code (`GB`, `US`, `IT`). It is the default region of phone numbers written without `+`, and it switches on the features made for one country (a connector made for another country stays off). |
| `PISA_SECRETS_KEY` | A Fernet key (44 characters): `openssl rand -base64 32 \| tr '+/' '-_'`. It needs only OpenSSL, not Python. |
| `POSTGRES_PASSWORD`, `PISA_MIGRATE_PASSWORD`, `PISA_APP_PASSWORD` | Three different passwords: `openssl rand -hex 24` each. |
| `COMPOSE_PROFILES` | The way in: `tailscale`, `caddy` or `local` (below). |

Keep a copy of `PISA_SECRETS_KEY` somewhere other than the server and its backups: the provider keys Pisa
stores are encrypted with it.

### The way in: compose profiles

`COMPOSE_PROFILES` in `deploy/.env` chooses how clients reach Pisa. Exactly one of:

| Profile | What runs in front of the API | Published on the host |
|---|---|---|
| `tailscale` (the default) | A Tailscale container running `tailscale serve`: HTTPS on your tailnet only, with the certificate Tailscale issues for `https://<TS_HOSTNAME>.<tailnet>.ts.net` (exported, without its key, for the pairing QR's pin: [Pairing](pairing.md#what-the-server-needs)). Set `TS_AUTHKEY` (an auth key from the Tailscale admin console, used for the first login only) and enable HTTPS certificates for the tailnet. | nothing |
| `caddy` | Caddy, with a public certificate for `PISA_DOMAIN` (and `PISA_ACME_EMAIL`, its ACME account's contact) that it gets and renews by itself (`deploy/caddy/Caddyfile`, which keeps the key across renewals: [the pin](#the-pin-read-this-before-you-choose)). | 80 and 443 |
| `local` | Nothing: the API itself, as `api-lan`, on `PISA_LAN_BIND:PISA_LAN_PORT`, serving HTTPS with Pisa's own self-signed key and certificate, which `pisa init --mode local` makes once in `deploy/tls/` (the key `0600`; `--rotate-tls` makes a new one, and every paired client must then pair again). Clients trust that key through the pin in the pairing QR, not a certificate authority. `PISA_LAN_BIND` is `127.0.0.1` (this machine only) until you set it to `0.0.0.0` for your phone. With `PISA_LAN_TLS_CERT=` and `PISA_LAN_TLS_KEY=` (both empty) it speaks plain http instead, for your own proxy on the host (`PISA_LAN_PORT=8000`, the recipes in section 3) or a developer's loopback setup. | `PISA_LAN_PORT`, on `PISA_LAN_BIND` |

With `tailscale` and `caddy` the compose file tells Pisa to trust the `X-Forwarded-For` of that profile's
proxy alone: it holds the fixed address `172.29.0.10` (`PISA_PROXY_IP`), and every other container gets its
address from `172.29.0.128/25`, so none can take it. Both proxies send `X-Forwarded-For`. With `local`,
`PISA_TRUSTED_PROXIES` in `deploy/.env` stays empty, or is `172.29.0.1/32` (the gateway) with a proxy on the
host. To change profile, run
`docker compose down` first (the data stays in its volumes): a plain `up -d` would leave the old API running.

Then:

```bash
cd deploy
docker compose up -d --build
docker compose ps        # postgres, worker and api (api-lan with `local`) healthy, migrate exited 0
docker compose exec worker python -c "import urllib.request as u; print(u.urlopen('http://api:8000/readyz').read().decode())"
# {"status":"ok","checks":{"database":"ok","schema":"ok"},"push":"off","relay":"off"}
```

(With `local`, `curl --insecure https://127.0.0.1:8443/readyz` on the host: the certificate is Pisa's own, so
curl can't vouch for it, and `pisa pair` shows the pin a client checks instead.)

The compose file runs `postgres` (with pgvector), `migrate` (applies the database migrations, then exits),
`api` (the HTTP API and the clients' live socket), `worker` (the background schedules: email intake, reminders,
the night's work) and the profile's proxy. Postgres is never published. Every container runs read-only, with no
capabilities beyond what it needs and no Docker socket.

Release images are published as `ghcr.io/<owner>/pisa:<version>` for amd64 and arm64 (a Raspberry Pi 4 or 5 on a
64-bit OS), signed with cosign; set `PISA_IMAGE` to one to skip the build (`<owner>` is the GitHub account that
publishes Pisa, `<repo>` its repository). To check that an image is the release's own, verify its signature
against the release's tag and the tag push that built it. Each release's notes on the **Releases** page carry this
command filled in for that release; copy it from there. Its form:

```bash
cosign verify ghcr.io/<owner>/pisa:1.2.3 \
  --certificate-identity 'https://github.com/<owner>/<repo>/.github/workflows/release.yml@refs/tags/v1.2.3' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-github-workflow-trigger push
```

Use the same version in the image's tag and in `refs/tags/v…`. An image signed by any other run, a manual run
from a branch included, does not verify. `.github/workflows/release.yml` is the only workflow that publishes
Pisa's images; it also attaches signed build-provenance and SBOM attestations
(`gh attestation verify oci://ghcr.io/<owner>/pisa:1.2.3 --repo <owner>/<repo>`).

Each release also carries two files, which the install scripts fetch and check for you:

- `IMAGES`: the digest of each image the release pushed and signed (`pisa ghcr.io/<owner>/pisa@sha256:…`).
  The install scripts write the server's into `PISA_IMAGE` by digest, so your install never follows a tag
  that someone moved. To verify that exact image, run the `cosign verify` above with
  `ghcr.io/<owner>/pisa@sha256:…` in place of the tag.
- `SHA256SUMS`: the SHA-256 of every file of the install bundle and of `install.sh` and `install.ps1`. The
  scripts refuse to install a file that doesn't match, and install nothing then. To check the script you
  downloaded before running it:

  ```bash
  curl -fsSLO https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/SHA256SUMS
  grep ' deploy/install.sh$' SHA256SUMS; sha256sum install.sh    # the two hashes must be the same
  ```

  (On a Mac, `shasum -a 256 install.sh`; on Windows, `Get-FileHash install.ps1`, and look for
  `deploy/install.ps1`.)

A commit id in `PISA_VERSION` has no release: set `PISA_IMAGE` to an image by digest yourself.

`"push": "off"` is normal: the phone schedules its own alerts by default, with no key. Push is Apple's push
service, for an iOS client of your own (with your own Apple Developer account: `PISA_APNS_TEAM_ID`,
`PISA_APNS_TOPIC` and `pisa secrets set-apns`), or through a relay if an app's publisher runs one (none is offered
by the PISA project today). Everything else works without it; a client without push catches up when it connects
([Notifications](notifications.md)).

Natural voice talks (for a client that implements them) use WebRTC, whose audio travels over UDP between the
client and the server. Behind a plain HTTPS proxy that UDP has no way in: give Pisa a fixed UDP range
(`PISA_VOICE_UDP_PORTS`) open to the client and the address it reaches it at (`PISA_ICE_HOST`), a TURN server
(`PISA_TALK_ICE_SERVERS`), or use the Tailscale recipe, where the client and the server share a private network
and the address is found by itself (the sidecar writes the tailnet IP to `PISA_ICE_HOST_FILE`). The compose
bundle sets the range up for each profile ([Limits](limits.md)).

## 2. First run

Every `pisa` command runs inside a Pisa container; the `worker` runs under every profile:

```bash
alias pisa='docker compose exec worker pisa'
```

1. **Pair a client.** Your own client, the PISA app, or the command-line client: `pisa pair laptop` shows a
   QR code (and a typed code), valid for 10 minutes; the client reads it, and you confirm on the server that
   both show the same 6 digits. A paired client then signs a 1-hour session token with its own device key
   whenever it connects (there is no long-lived token to copy), and signs deletes and approvals with the same
   key ([Pairing](pairing.md)). Pairing needs one of:
   - **The `local` profile**: nothing to do. `pisa init` made the key and certificate and set
     `PISA_ADDRESSES` (`https://<this machine>.local:8443`) and `PISA_TLS_CERT=/etc/pisa/tls/cert.pem`; the QR
     carries that certificate's pin.
   - **HTTPS** (section 3): the QR carries the pin of the server's TLS key, so choose a recipe whose key stays
     the same ([the pin](#the-pin-read-this-before-you-choose)). Put the certificate your proxy presents (or
     its public key) in `deploy/tls/cert.pem` (the compose file mounts `deploy/tls` read-only at `/etc/pisa/tls`),
     then in `deploy/.env`:

     ```bash
     PISA_ADDRESSES=https://pisa.example.com
     PISA_TLS_CERT=/etc/pisa/tls/cert.pem
     ```

     (`PISA_TLS_CERT` also takes the PEM itself, on one line with `\n` for its line breaks.)
   - **Your own machine, no TLS**, for development only: the server and the client on the same machine, over
     plain http on loopback ([Pairing](pairing.md#a-developers-machine-no-tls)):

     ```bash
     PISA_INSECURE_LOOPBACK_PAIRING=true
     PISA_ADDRESSES=http://127.0.0.1:8000
     ```

     Use the `local` profile with `PISA_LAN_BIND=127.0.0.1`, `PISA_LAN_PORT=8000`, `PISA_LAN_TLS_CERT=`,
     `PISA_LAN_TLS_KEY=` and `PISA_TLS_CERT=`, and put no proxy or tunnel in front of it (section 3 does not
     apply): anything in front lets other machines in over plain http.

   Then `docker compose up -d`, and:

   ```bash
   pisa pair laptop --no-wait            # shows the code
   pisa client pair '<the code>' --server http://127.0.0.1:8000    # on this machine, as a client
   pisa pair --confirm <the 6 digits>    # in another terminal, when the client shows them
   pisa client call GET /v1/ai/disclosure
   ```

   (With the alias above, `pisa client` runs inside the `worker` container and keeps its key there until the
   container restarts. In insecure loopback mode the client must be inside the API's own container: use the
   `local` profile and `docker compose exec api-lan pisa client ...`. A checkout of `core/` with `uv run pisa client ...` keeps it on the host; in
   insecure loopback mode that also needs `PISA_INSECURE_LOOPBACK_GATEWAY=true`, with rootful Docker on Linux
   only: [Pairing](pairing.md#a-developers-machine-no-tls).)
2. **Add a provider key.** On the server: `pisa secrets set openai` (the key is read from a hidden prompt, never
   from the command line), or from a client (`PUT /v1/ai/providers/{id}/key`; in the PISA app, Settings > AI
   providers). Then `pisa catalog refresh`.
3. **Choose your models.** Pisa has no default model: each feature (a *slot*) uses the model you choose, and a
   feature whose slot is empty fails with "No model is chosen for ..." until you choose one.

   ```bash
   pisa models list                        # every slot and its model, "(not set)" until you choose
   pisa models available --slot chat.conversation
   pisa models set chat.conversation openai:<a model from the list>
   ```

   `chat.conversation` (the conversation) needs a model that can call tools; the others say what they need in
   [AI providers](ai-providers.md#slots-which-model-does-what). A client can do the same over the API
   (`/v1/ai/slots`).
4. **Consent.** Before the first turn, Pisa needs your consent to send data to the providers you chose: a client
   shows `GET /v1/ai/disclosure` and records your yes with `POST /v1/ai/consent`
   ([Privacy](privacy.md)). Until then every turn ends with `ai_consent_required`.

### Pairing codes and their limits

- **At most 5 pairing codes are open at once, for the whole server.** A code made by `pisa pair` and an offer
  made from a paired client (`POST /v1/pair/offers`) count alike. A sixth waits until one is used, withdrawn or
  10 minutes old. A paired client can therefore keep `pisa pair` busy for up to 10 minutes; if that happens,
  revoke it with `pisa devices revoke <id>`.
- **Failed pairing attempts never spend or block your code**, and pairing is never locked for everyone behind
  one address (a home network, an office, a mobile carrier). A code is 128 random bits, used once, and valid
  for 10 minutes: a wrong attempt can't guess it. The only limit is a backstop against floods: 60 failed
  attempts in 10 minutes from one address (an IPv6 address counts by its /64) pause pairing from that address
  for a while.
- **Run `pisa doctor`** after setting up. It warns when `PISA_SECRETS_KEY` is not set (the open pairing codes
  are then stored in plaintext in the database for their 10 minutes), when no client can pair (no addresses,
  or no readable certificate), and while insecure loopback mode is on.

## 3. HTTPS

Clients talk to Pisa over HTTPS and a WebSocket. The `tailscale` and `caddy` profiles (section 1) set up the
first two recipes below in containers; the recipes here run the proxy on the host instead, in front of the
`local` profile with `PISA_LAN_BIND=127.0.0.1`, `PISA_LAN_PORT=8000` and plain http (`PISA_LAN_TLS_CERT=`,
`PISA_LAN_TLS_KEY=`). Either way, give Pisa the
certificate the proxy presents (`PISA_TLS_CERT`, section 2). Each puts a TLS proxy in front of
`127.0.0.1:8000`; then tell Pisa to trust that proxy's `X-Forwarded-For` header, so a pairing attempt is
counted against the real client and not against the proxy:

```bash
# deploy/.env: a proxy on the host reaches the compose network from its gateway
PISA_TRUSTED_PROXIES=172.29.0.1/32
```

and `docker compose up -d` again. Leave `PISA_TRUSTED_PROXIES` empty if nothing sits in front of Pisa: a
client could otherwise choose the address its guesses are counted under.

### The pin: read this before you choose

**The pin authenticates pairing.** The pairing QR carries the pin (a hash of the public key) of the TLS key
the server presents, and a client pairs only with a server that presents that key. Where the pin comes from:

- the `tailscale` profile: the current leaf certificate `tailscale serve` presents, exported by the sidecar;
- the `caddy` profile: the leaf certificate Caddy presents, read when the QR is made, and only after it passes
  WebPKI verification (the public certificate authorities and the domain's name);
- the `local` profile: Pisa's own key;
- a proxy of your own (the recipes below): the certificate or public key you put in `PISA_TLS_CERT`.

After pairing, the app checks the server the usual way, WebPKI, wherever the certificate is a public one
(`tailscale`, `caddy` and the recipes below): as for any website, a certificate a public authority wrongly
issued for your name would then be accepted too. A later key's pin reaches paired apps in `pisa.endpoints` the
next time they connect. In `local` mode no authority vouches for the key, so the pin stays the only trust. The
command-line client (`pisa client`) still checks the pin on every connection. A server identity apart from TLS
comes with a later contract 2.x version.

So the key matters most for pairing and for the command-line client. A renewed certificate with the **same key**
changes nothing; with a **new key**, a QR made from an old `PISA_TLS_CERT` no longer pairs (it fails closed),
and a command-line client that was offline when the key changed must pair again. Pisa can announce a key before
it is used (`PISA_TLS_NEXT_KEY`: paired clients then trust both), which needs the next key before the proxy
switches to it.

So the recipe must give you a key that stays the same, or one you know in advance:

| Recipe | Does the key Pisa pins stay the same? | What to do |
|---|---|---|
| Caddy | Not by default: Caddy makes a new key at every renewal (about every 60 days). | Tell Caddy to keep it (`reuse_private_keys`, below). |
| Tailscale serve | Tailscale makes and renews the certificate itself (about every 90 days); its key is Tailscale's to keep or change. | Check the pin after each renewal (below), or put a TLS proxy whose key you hold on the tailnet address. |
| Cloudflare Tunnel | No: clients see Cloudflare's edge certificate, whose key Cloudflare holds and rotates on its own schedule. | Not suitable for pairing clients that pin, as it stands (below). |

Since only the key matters, `PISA_TLS_CERT` can hold just the public key, which never needs updating while the
key stays the same. Name it `pubkey.pem`: `deploy/tls/key.pem` is the `local` profile's *private* key, which
this must never overwrite.

```bash
openssl x509 -in <the certificate file> -pubkey -noout > deploy/tls/pubkey.pem    # PISA_TLS_CERT=/etc/pisa/tls/pubkey.pem
```

To see the pin a server presents right now (compare it before and after a renewal):

```bash
openssl s_client -connect pisa.example.com:443 -servername pisa.example.com </dev/null 2>/dev/null \
  | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64
```

### Caddy (a public domain, Let's Encrypt)

You need a domain name whose DNS points at the server, and ports 80 and 443 open.

```caddyfile
# /etc/caddy/Caddyfile
pisa.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl reload caddy
curl -s https://pisa.example.com/readyz
```

Caddy gets and renews the certificate by itself, and proxies WebSockets without more configuration. The server
address clients use is `https://pisa.example.com` (`PISA_ADDRESSES`).

**The pin.** By default Caddy makes a new private key for every certificate it gets, so the pin would change at
each renewal. Keep the key with Caddy's `reuse_private_keys` option in the site's `tls` block (it needs a
recent Caddy; check its documentation for your version):

```caddyfile
pisa.example.com {
    tls {
        reuse_private_keys
    }
    reverse_proxy 127.0.0.1:8000
}
```

The certificate is in Caddy's data directory (with the Debian package,
`/var/lib/caddy/.local/share/caddy/certificates/<issuer>/pisa.example.com/pisa.example.com.crt`); put its public
key in `deploy/tls/` as above. Set the option before you pair any client: the first certificate after you set it
may still come with a new key, so check the pin once it is issued.

### Tailscale serve (private: only your devices)

Nothing is public: Pisa is reachable only from devices in your tailnet, with a certificate Tailscale issues for
the machine's tailnet name. Install Tailscale on the server and on each device your clients run on, sign them
into the same tailnet and enable HTTPS certificates in the tailnet's admin console. Then:

```bash
sudo tailscale serve --bg http://127.0.0.1:8000
tailscale serve status    # shows https://<machine>.<tailnet>.ts.net
```

The server address clients use is the `https://<machine>.<tailnet>.ts.net` that `tailscale serve status`
shows. Tailscale's proxy connects from the host, so the compose gateway is the trusted proxy as above; Pisa also uses
the `Tailscale-User-Login` header it sets (believed only from that trusted proxy). `tailscale serve` sets
`X-Forwarded-For` to the tailnet address of the device that connected, replacing any value the client sent
(Tailscale's `ipn/ipnlocal/serve.go`, `addProxyForwardedHeaders`, in the version the compose file pins), so
each device is counted under its own address; the `tailscale` profile relies on the same.

Inside the tailnet a client can reach the server directly, so natural voice talks work without a TURN server
once the API's UDP is reachable: run the `api` on the host's network with this `deploy/docker-compose.override.yml`
(docker compose reads it beside the main file):

```yaml
services:
  postgres:
    ports: ["127.0.0.1:5432:5432"]   # the api, on the host's network, reaches it here
  api:
    network_mode: host
    ports: !reset []
    environment:
      PISA_API_HOST: 127.0.0.1
      PISA_DATABASE_URL: postgresql://pisa_app:${PISA_APP_PASSWORD}@127.0.0.1:5432/pisa
      PISA_DBOS_DATABASE_URL: postgresql://pisa_app:${PISA_APP_PASSWORD}@127.0.0.1:5432/pisa_dbos
```

On the host's network the proxy's connections come from loopback: then `PISA_TRUSTED_PROXIES=127.0.0.1/32,::1/128`.
Allow inbound traffic on the `tailscale0` interface only (for example `ufw allow in on tailscale0`), so
nothing new is public.

**The pin.** `tailscale cert <machine>.<tailnet>.ts.net` writes the certificate (and its key) that
`tailscale serve` presents; put its public key in `deploy/tls/` as above. Tailscale renews it on its own, and
whether the key stays the same is up to Tailscale, not Pisa. Check the pin after each renewal (the command
above); if it changed, update `deploy/tls/` before the next pairing, and a command-line client must pair again
([the pin](#the-pin-read-this-before-you-choose)). To avoid that, terminate TLS yourself on the
tailnet address with a key you hold (for example Caddy with your own certificate and key, `tls <cert> <key>`)
instead of `tailscale serve`. A client that trusts the pin alone, as the guide's
[pairing](build-a-client/pairing.md#the-pin) does, needs no public certificate authority for that.

### Cloudflare Tunnel (a public domain, no open port)

The server opens an outbound tunnel to Cloudflare; no port is opened on the server. You need a domain on
Cloudflare.

```bash
cloudflared tunnel login
cloudflared tunnel create pisa
cloudflared tunnel route dns pisa pisa.example.com
```

```yaml
# ~/.cloudflared/config.yml
tunnel: pisa
credentials-file: /home/<you>/.cloudflared/<tunnel id>.json
ingress:
  - hostname: pisa.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

```bash
sudo cloudflared service install
curl -s https://pisa.example.com/readyz
```

Cloudflare terminates TLS and sets `X-Forwarded-For` to the client's address; `cloudflared` connects from the
host, so the trusted proxy is the compose gateway, as above. WebSockets pass through tunnels by default.

**The pin.** Clients see Cloudflare's edge certificate, not one you hold. Its key is Cloudflare's, and
Cloudflare replaces it on its own schedule, without notice to you: a pin on it breaks with no warning, and the
next key can't be announced ahead. As it stands, a Cloudflare Tunnel is fine for checking `/readyz` from
outside, but not for pairing clients that pin. Use Caddy or Tailscale for that. (Cloudflare's paid custom
certificates, where you upload a certificate and key you hold, would give a stable key; that is outside this
guide.)

## 4. Backups

Back up the database, not the container: everything Pisa knows is in Postgres (`pg_dump`). The recipe below
dumps it every night and sends the dump, encrypted, to a bucket with [restic](https://restic.net), which keeps
a history and only uploads what changed. Any restic backend works: an S3-compatible bucket (AWS S3, Backblaze
B2, Wasabi, MinIO...), SFTP, or a local disk.

Install restic on the host, then create the repository once:

```bash
sudo install -d -m 700 /etc/pisa-backup
sudo tee /etc/pisa-backup/env >/dev/null <<'EOF'
RESTIC_REPOSITORY=s3:https://s3.example.com/my-pisa-backups
RESTIC_PASSWORD=<a long random password, kept also outside this server>
AWS_ACCESS_KEY_ID=<the bucket's key id>
AWS_SECRET_ACCESS_KEY=<the bucket's secret>
EOF
sudo chmod 600 /etc/pisa-backup/env
sudo bash -c 'set -a; . /etc/pisa-backup/env; restic init'
```

The backup script (adjust `PISA_DIR` to where you cloned Pisa):

```bash
#!/bin/bash
# /usr/local/bin/pisa-backup: dump Pisa's database and send it to the restic repository.
set -euo pipefail
PISA_DIR=/opt/pisa/deploy
set -a; . /etc/pisa-backup/env; set +a
cd "$PISA_DIR"

# a consistent dump in Postgres's custom format, streamed into restic (no copy left on the disk)
docker compose exec -T postgres pg_dump -U postgres -d pisa -Fc \
  | restic backup --stdin --stdin-filename pisa.dump --tag pisa

restic forget --tag pisa --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune

# tell Pisa when its last off-site copy was made (it shows it, and reminds you when it gets old)
docker compose exec -T postgres psql -U postgres -d pisa -v ON_ERROR_STOP=1 -c \
  "INSERT INTO app_meta (key, value) VALUES ('offsite_backup_at', to_json(now())::text)
   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
```

```bash
sudo install -m 700 pisa-backup /usr/local/bin/pisa-backup
sudo /usr/local/bin/pisa-backup          # once by hand, to see it work
echo '30 3 * * * root /usr/local/bin/pisa-backup' | sudo tee /etc/cron.d/pisa-backup
```

The worker's own maintenance runs in the small hours as well; any time works for the backup.

### Restoring

On a fresh install (section 1, with the same `PISA_SECRETS_KEY`; the roles exist once the database has started),
before pairing:

```bash
cd deploy
docker compose stop api worker
sudo bash -c 'set -a; . /etc/pisa-backup/env; restic dump --tag pisa latest pisa.dump' \
  | docker compose exec -T postgres pg_restore -U postgres -d pisa --clean --if-exists
docker compose up -d
docker compose exec api pisa notify reset-epoch   # `api-lan` in local mode
```

The last command gives the phones' notification count a new epoch: the restored count is older than theirs.
The dump keeps each table's owner and grants (the `pisa_migrate` and `pisa_app` roles), so the restored
database works as before. The worker's `pisa_dbos` database holds only its schedules' bookkeeping: it is
rebuilt by itself and needs no backup.

Check a restore now and then on another machine: a backup you never restored is a hope, not a backup.

## 5. Updating

```bash
git pull
cd deploy
docker compose up -d --build
```

The `migrate` container applies any new database migration before the API and the worker start. Migrations
only go forward: to go back to an older version, restore the backup taken before the update.

**From a version that still paired with a 6-character code:** Pisa now speaks only contract 2 (the live socket
`/v2/live`, the talk's channel with the subprotocol `pisa.v2`, `GET /v1/server` as contract 2's discovery), and
only a device's 1-hour session tokens authenticate. A device paired with the old code has no device key: the
update revokes it, and it pairs again with `pisa pair <name>` (section 2) from a client that speaks contract 2.
Contract 1's REST shapes are gone too: a turn without the socket is `POST /v2/runs` (not `POST /v1/turns`), an
approval is decided with `POST /v1/approvals/{id}/resume` (not `POST /v1/approvals/{id}`), and
`PUT /v1/devices/me/push` takes `{"mode": "direct", "token": ...}` (not `{"apns_token": ...}`). A script that
used them moves to the new shapes.
