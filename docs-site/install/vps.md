# Install Pisa on a VPS

A rented server (VPS) with a public address and your own domain uses the `caddy` profile: Caddy gets and
renews a public Let's Encrypt certificate for your domain and passes requests to Pisa. Your phone reaches
`https://pisa.example.com` from anywhere, with no Tailscale. What the self-hosted setup does not do yet:
[Limits](../limits.md).

## What you need

- A 64-bit Linux VPS (amd64 or arm64) with at least 4 GB of memory and 20 GB of disk, and SSH access.
- A domain (or subdomain) you control, such as `pisa.example.com`.
- An email address for Let's Encrypt: the caddy profile requires it, as the contact of Caddy's ACME account
  (certificate expiry notices).
- An AI provider's API key, and the release's install commands: copy them from the latest release on the repository's **Releases** page: its notes have an **Install** section with the exact commands for that release, the repository and the version already filled in.

## 1. DNS

At your DNS provider, point the domain at the VPS:

- an **A** record with the VPS's IPv4 address, and
- an **AAAA** record with its IPv6 address, only if the VPS really is reachable over IPv6 (a wrong AAAA record
  can make the certificate fail).

Check from your computer that the name resolves to the VPS (`dig +short pisa.example.com`, or
`nslookup pisa.example.com`) before going on: Caddy gets a certificate only once it does.

## 2. Firewall

Open ports **80** and **443** to the VPS, 443 over both TCP and UDP (the compose file publishes both; UDP is
HTTP/3). Both 80 and 443 are needed for the certificate. For voice talks, also open UDP **40000-40019**
(`PISA_VOICE_UDP_PORTS`; `pisa init` sets `PISA_ICE_HOST` to your domain and publishes the range). Keep SSH open, and nothing else. If your provider has
a firewall in its control panel, set it there. On the VPS itself, with Ubuntu's `ufw`:

```bash
sudo ufw allow 22/tcp      # SSH first, or you lock yourself out
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw allow 40000:40019/udp   # voice
sudo ufw enable
sudo ufw status
```

Ports that Docker publishes bypass `ufw`'s rules (Docker's page
[Packet filtering and firewalls](https://docs.docker.com/engine/network/packet-filtering-firewalls/)). Pisa's
compose file publishes only Caddy's 80 and 443 and the API's voice range (UDP); Postgres and the API's HTTP
port are never published. A firewall in the
provider's control panel is not affected by this.

Keep the system updated (on Debian and Ubuntu, `unattended-upgrades` installs security updates by itself) and log
in with an SSH key rather than a password.

## 3. Docker and Pisa

Install Docker Engine as on the [Linux page](linux.md#1-docker-engine). Then run the release's two **macOS or
Linux** lines, adding the caddy options to the second (below, `<owner>/<repo>` and `vX.Y.Z` stand for the
release's values):

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/<owner>/<repo>/vX.Y.Z/deploy/install.sh
PISA_REPO=<owner>/<repo> PISA_VERSION=vX.Y.Z bash install.sh --mode caddy --domain pisa.example.com --email you@example.com
```

`pisa init` asks the remaining questions (time zone, language), writes `~/pisa/deploy/.env` with
`COMPOSE_PROFILES=caddy`, `PISA_DOMAIN`, `PISA_ACME_EMAIL` and `PISA_ADDRESSES=https://pisa.example.com`, and
prints the next steps. Keep a copy of its `PISA_SECRETS_KEY` line outside the VPS.

## 4. Start, set up, pair

```bash
alias pisa-compose='docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env'
pisa-compose up -d
pisa-compose ps                       # postgres, api, worker, caddy: Up (healthy)
pisa-compose logs caddy --tail 50     # the certificate: obtained, or the reason it was not
pisa-compose exec worker pisa setup   # your AI provider and its key (stored encrypted in the database)
pisa-compose exec worker pisa pair    # the QR code the PISA app scans
```

Then open `https://pisa.example.com/health` in a browser: it answers with `"status": "ok"`. If Caddy reports
that it could not get a certificate, check the DNS records and that ports 80 and 443 are open, then
`pisa-compose restart caddy`. Let's Encrypt limits how often it issues certificates: don't restart in a loop.

To update Pisa, run the install command from the new release (its notes on the **Releases** page). Stopping
and uninstalling are as on the [Linux page](linux.md#every-day). Keep backups off the VPS:
[Backups](../limits.md#backups).
