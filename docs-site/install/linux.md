# Install Pisa on Linux

For a Linux machine you administer (a home server, a spare PC): Docker Engine, then Pisa's install script,
reached from your phone through Tailscale. The [Mac page](mac.md) explains each Tailscale and Pisa step in more
detail; this page gives the Linux commands. For a rented server with a public domain, see [VPS](vps.md); for a
Raspberry Pi, [Raspberry Pi](raspberry-pi.md). What the self-hosted setup does not do yet:
[Limits](../limits.md).

## What you need

- A 64-bit (amd64 or arm64) Linux distribution that Docker Engine supports, with at least 4 GB of memory
  (8 GB is comfortable) and 20 GB of free disk.
- `curl` (or `wget`).
- An iPhone with iOS 16 or later, an AI provider's API key, and the release's install commands: copy them from
  the latest release on the repository's **Releases** page: its notes have an **Install** section with the exact commands for that release, the repository and the version already filled in.

## 1. Docker Engine

Install Docker Engine and its compose plugin from Docker's own package repository, following the page for your
distribution: <https://docs.docker.com/engine/install/> (for Debian or Ubuntu, the section on installing with
the `apt` repository). Prefer the repository over the convenience script: you see what is installed, and
updates come with the system's.

Then, from Docker's [post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/):

```bash
sudo systemctl enable docker.service containerd.service   # start at boot
sudo usermod -aG docker $USER                               # use docker without sudo
```

Log out and back in (or run `newgrp docker`). The `docker` group grants root-level privileges on this machine:
add only yourself. The install script runs `pisa init` as your user, so that the `.env` it writes is yours; do
not run it with `sudo`.

Check:

```bash
docker compose version     # Docker Compose version v2...
docker run --rm hello-world
```

## 2. The tailnet

Create your tailnet by signing up at <https://tailscale.com> (or by installing Tailscale on any computer and
signing in). Then, in the admin console:

- DNS page (<https://console.tailscale.com/admin/dns>): click **Enable MagicDNS** if it is off, and under
  **HTTPS Certificates** click **Enable HTTPS**. Pisa's Tailscale container uses `tailscale serve`, which needs
  HTTPS certificates; those need MagicDNS. The names of machines that get a certificate are published in the
  public Certificate Transparency logs.
- Keys page (<https://console.tailscale.com/admin/settings/keys>): **Generate auth key**, **Reusable** on,
  **Ephemeral** off, **Generate key**. Copy the `tskey-...` key.

This machine does not need Tailscale installed: Pisa runs its own Tailscale container.

## 3. Install Pisa

Copy the two **macOS or Linux** lines from the release's notes (the commands below show their form, with
`<owner>/<repo>` and `vX.Y.Z` where the release's values go); reading the script before running it is a good
idea:

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/<owner>/<repo>/vX.Y.Z/deploy/install.sh
less install.sh            # read it: it installs nothing but ~/pisa
PISA_REPO=<owner>/<repo> PISA_VERSION=vX.Y.Z bash install.sh
```

`pisa init` asks how the phone reaches Pisa (Enter keeps `tailscale`), your time zone (Enter keeps this
machine's, shown in brackets; a city such as `Rome` works too), the language (`en` or `it`), your country
(Enter keeps the time zone's; a code such as `IT` or a name such as `Italy`), the Tailscale auth key (hidden
while you paste) and the machine's name on the tailnet (`pisa`). A mistyped answer is asked again. It writes
`~/pisa/deploy/.env` (mode 0600); then the installer starts Pisa (`docker compose up -d`), waits until every
service is healthy (up to three minutes) and prints the next steps, `pisa setup` and `pisa pair`. Add
`--no-start` to only write the settings. To answer without questions,
put `TS_AUTHKEY=tskey-... ` before the release's second line and `--timezone Europe/London --yes` after it:
`TS_AUTHKEY=tskey-... PISA_REPO=<owner>/<repo> PISA_VERSION=vX.Y.Z bash install.sh --timezone Europe/London --yes`.

Keep a copy of the `PISA_SECRETS_KEY` line of `~/pisa/deploy/.env` outside this machine (a password manager):
the provider keys Pisa stores are encrypted with it.

## 4. Start, set up, pair

```bash
alias pisa-compose='docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env'
pisa-compose up -d                    # already done by the installer, unless --no-start
pisa-compose ps                       # postgres, api, worker, tailscale, voice-relay: Up (healthy)
pisa-compose exec worker pisa setup   # your AI provider and its key (stored encrypted in the database)
pisa-compose exec worker pisa pair    # the QR code the PISA app scans
```

The alias lasts for this shell; add the line to `~/.bashrc` to keep it. On the iPhone, install Tailscale, sign
in to the same tailnet, then scan the QR with the PISA app and confirm the 6 digits only if they match. On the
admin console's Machines page (<https://console.tailscale.com/admin/machines>), disable key expiry for `pisa`
(the `...` menu, **Disable Key Expiry**), or it leaves the tailnet when its key expires.

Voice talks need nothing more: the Tailscale container writes the server's tailnet address and Pisa offers it to
the phone at each talk. To check, `pisa-compose exec api cat /etc/pisa/ts-cert/ip` prints an address starting
with `100.` ([Limits](../limits.md)).

When something is wrong: `pisa-compose logs --tail 50`, `pisa-compose exec worker pisa doctor`,
`pisa-compose exec tailscale tailscale status`, and [Troubleshooting](../troubleshooting.md).

## Every day

- Stop: `pisa-compose stop`; start: `pisa-compose up -d`. With Docker enabled at boot, Pisa starts by itself.
- Update: back up ([Backups](../limits.md#backups)), run the new release's first line (from its notes on the
  **Releases** page), then its second line with `--force --yes` added:
  `PISA_REPO=<owner>/<repo> PISA_VERSION=vX.Y.Z bash install.sh --force --yes` (it keeps every setting and key,
  and switches `PISA_IMAGE` to the new version), then `pisa-compose pull && pisa-compose up -d`. Migrations run
  by themselves and only go forward.
- Another way in: rerun the script with `--force --mode caddy` (see [VPS](vps.md)) or `--mode local`. Run
  `pisa-compose down` before starting with a different profile (the data stays).

## Uninstall

```bash
pisa-compose down --volumes    # deletes the containers and Pisa's database
rm -rf ~/pisa
```

Then remove the `pisa` machine and revoke the key in the Tailscale admin console.
