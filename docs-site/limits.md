# Limits of the self-hosted setup

What the self-hosted packaging (the install scripts, `pisa init` and the compose profiles) does not do yet. Each
item says what works today. Nothing here has a date.

## Ways in

- **`local` mode answers this machine only until you open it.** It serves HTTPS with Pisa's own self-signed
  certificate, whose key the app pins when you pair, bound to `127.0.0.1` until you set `PISA_LAN_BIND=0.0.0.0`
  (or this machine's LAN address) for your phone. It has no public name: your phone reaches it at the address
  `pisa init` put in `PISA_ADDRESSES`, on your home network only. Away from home, use `tailscale`, or `caddy`
  with a domain.
- **Voice needs UDP, on a fixed port range.** Natural voice talks send their audio over UDP (WebRTC), one port
  per talk from `PISA_VOICE_UDP_PORTS` (default `40000-40019`: at most 20 talks at once), offered to the phone at
  `PISA_ICE_HOST`. With `tailscale`, the `voice-relay` service carries that range from the tailnet to the API,
  and `PISA_ICE_HOST` is the server's tailnet IP, which exists only after the first start: set it once (see
  your install page). With `caddy`, `pisa init` sets it to your domain and publishes the range: open it in the
  firewall. With `local`, it is this machine's LAN address. If the audio can't connect within 15 seconds
  (`PISA_VOICE_CONNECT_TIMEOUT_S`), the talk ends and the app carries on without the live voice; text always
  works. Not yet: a TURN server in the bundle (a coturn profile) for networks that block UDP; you can point
  `PISA_TALK_ICE_SERVERS` at one you run ([Self-hosting](self-hosting.md#1-install)).
- **The pin authenticates pairing; after it, the app relies on WebPKI** with `tailscale` and `caddy`, whose
  certificate is a public one (from Tailscale's or Caddy's certificate authority): as for any website, a
  certificate a public authority wrongly issued for your name would then be accepted too. With `local`, the pin
  stays the only trust. The command-line client checks the pin on every connection. A server identity apart
  from TLS comes with a later 2.x contract version ([the pin](self-hosting.md#the-pin-read-this-before-you-choose)).
- **Not yet available:** a Cloudflare Tunnel profile (the manual recipe is in
  [Self-hosting](self-hosting.md#cloudflare-tunnel-a-public-domain-no-open-port)), native installers (an app to
  download instead of Docker and a script), and packages for Umbrel, TrueNAS or Helm (Kubernetes).

## Size and memory

- **The download.** The Pisa image (`ghcr.io/<owner>/pisa`) is about 650 MB to download, compressed, and about
  2.5 GB on disk once unpacked, on both amd64 and arm64 (built from this release: 2.48 GB and 2.50 GB); Postgres
  adds about 165 MB to download (650 MB on disk), and the Tailscale or Caddy image under 100 MB. Keep 20 GB of
  disk free for the images, the database and its growth. The push relay's image, which you run only if you
  host a relay, is about 370 MB on disk.
- **Memory.** Idle, the whole `local` stack uses about 0.6 GB (measured with `docker stats` a few minutes after
  `docker compose up`, amd64: the API 275 MB, the worker 220 MB, Postgres 130 MB). A voice talk, the night's
  work and a growing database take more; plan on about 2 GB for Pisa.
- **On a laptop or a Mac**, have about 2 GB of memory free for Pisa. Docker Desktop's default memory limit is
  half of the computer's memory (Docker's documentation, Settings → Resources → Advanced), so on a computer
  with 8 GB or more the default (4 GB or more) is enough and needs no change. With less than 8 GB, raise the
  limit to 4 GB.

## One owner per server

A Pisa server belongs to one person. You can pair several devices to it, but they are all yours: there are no
separate users, accounts or permissions for a family or a team. For two people, run two servers.

## Backups

Backups are your job. Pisa keeps everything it knows in its Postgres database, on the machine it runs on: if
that machine's disk dies, or you delete its volumes, everything is gone. Nobody else has a copy.

Save the database to a file with `pg_dump`, run inside the `postgres` container. On macOS, Linux and a Raspberry
Pi (the file is created readable by you only, mode 0600):

```bash
(umask 077; docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env \
  exec -T postgres pg_dump -U postgres -d pisa -Fc > ~/pisa-backup-$(date +%Y-%m-%d).dump)
```

On Windows, in PowerShell (through `cmd`, which writes the dump's bytes unchanged; Windows PowerShell's own `>`
would not):

```powershell
cmd /c "docker compose -f %USERPROFILE%\pisa\deploy\docker-compose.yml --env-file %USERPROFILE%\pisa\deploy\.env exec -T postgres pg_dump -U postgres -d pisa -Fc > %USERPROFILE%\pisa-backup.dump"
```

Then:

- **Copy the file off the machine** (an external disk, another computer, a storage service you trust), and keep
  several dates. A backup on the same disk does not survive the disk.
- **Keep `PISA_SECRETS_KEY` too**, from `deploy/.env`, somewhere separate from the backup files. The provider
  keys in the database are encrypted with it: a backup restored without it works, but its stored keys can't be
  read and must be entered again.
- **Treat the file as private**: it holds everything Pisa knows about you.
- **Back up before every update**: updates only go forward, and going back means restoring.

To restore, and to make backups automatic, encrypted and off-site (with restic), see
[Self-hosting: Backups](self-hosting.md#4-backups) and [Restoring](self-hosting.md#restoring). Try a restore
once on another machine: a backup you never restored is a hope, not a backup.
