# Pairing

A client (the PISA app, your own client, or the command-line client) becomes one of your devices by pairing
with the server once. Pairing gives the device its own key; afterwards it proves itself with a 1-hour session
token it signs with that key on every connection. There is no long-lived token to copy or leak.

The same key signs the operations that need a signature: deleting your data, approving an action, changing a
connector or a provider key. Any paired client can do all of these with its own key; none of them needs the
iPhone app or Face ID (see [Signed operations](#signed-operations)).

## What the server needs

- **HTTPS** (see [HTTPS](self-hosting.md#3-https)), or, on a developer's machine,
  [insecure loopback mode](#a-developers-machine-no-tls).
- `PISA_ADDRESSES`: the `https://` addresses clients use to reach the server, comma-separated (home network,
  your domain, a tailnet name), and any `relay://<host>/<handle>` addresses. At most 6.
- `PISA_TLS_CERT`: the TLS certificate clients will see (or its public key), as PEM. The QR carries its pin, so
  the client trusts that key and no other. Without it, QR pairing is off. It is either:
  - a file's path. With docker compose, put the file in `deploy/tls/` (mounted read-only at `/etc/pisa/tls`)
    and write `PISA_TLS_CERT=/etc/pisa/tls/cert.pem` (readable by the container's user: `chmod 644`, a
    certificate is public; never put the private key there); or
  - the PEM itself, in the variable, on one line with `\n` for its line breaks.
- `PISA_SECRETS_KEY` (strongly advised): without it the open pairing codes are stored in plaintext for their
  10 minutes.

With docker compose's `local` profile, `pisa init` does all of this: it makes the server's own self-signed key
and certificate in `deploy/tls/` (once; `pisa init --rotate-tls` makes new ones, and every paired client must
then pair again), serves HTTPS with them, and sets `PISA_ADDRESSES` and `PISA_TLS_CERT=/etc/pisa/tls/cert.pem`.
No certificate authority vouches for that certificate: the QR's pin is the only trust a client has in it.

The `tailscale` and `caddy` profiles are set up by `pisa init` too, with a public (WebPKI) certificate whose
pin the QR carries:

- `tailscale`: the Tailscale container exports the certificate `tailscale serve` presents (never its key) to a
  volume the Pisa containers read, at start and every 12 hours, and `pisa init` sets
  `PISA_TLS_CERT=/etc/pisa/ts-cert/cert.pem` and `PISA_ADDRESSES` from your tailnet's DNS name. HTTPS
  certificates must be enabled for the tailnet (the DNS page of the Tailscale admin console).
- `caddy`: `pisa init` sets `PISA_TLS_PIN_FROM=caddy:443`. When a QR is made, Pisa reads the certificate Caddy
  presents for your domain (the first `https://` address in `PISA_ADDRESSES`), checks it against the public
  certificate authorities and the domain's name, and pins it. A certificate that fails that check is never
  pinned: `pisa pair` stops instead.

In these two modes the pin authenticates pairing; after it, clients rely on the public certificate (WebPKI). A
later key's pin reaches paired clients in `pisa.endpoints` the next time they connect.

Otherwise, the certificate must be the one your TLS proxy presents, and its **key must stay the same**: the pin is a hash
of the public key, and a client paired with one key refuses any other. With Caddy, copy it from Caddy's data
directory (`certificates/<issuer>/<your domain>/<your domain>.crt`) and set `reuse_private_keys`, or Caddy makes
a new key at every renewal; with `tailscale serve`, `tailscale cert` writes it, and you check the pin after each
renewal; with a Cloudflare Tunnel, clients see Cloudflare's certificate, whose key you don't hold and can't
pin. Each recipe's details: [The pin](self-hosting.md#the-pin-read-this-before-you-choose). To change the key on
purpose, put the next key in `PISA_TLS_NEXT_KEY` before the switch, so paired clients are told about it ahead.
The files are read on every request, so a rotation needs no restart.

`pisa doctor` warns when no client can pair (no addresses, or no readable certificate).

## Pairing a client

```bash
pisa pair laptop
```

1. The server shows a QR code (`pisa://pair?...`) and the same code as 26 characters to type, valid 10 minutes
   (`--minutes` shortens it).
2. The client reads it: a client with a camera scans it or takes the typed code; the command-line client takes
   either as text (below).
3. Both sides compute a 6-digit code from the exchange (the SAS). The server asks whether the client shows the
   same digits: confirm only if they match. Different digits mean someone else tried to pair: the request is
   refused and nothing is paired.

Other ways to confirm: `pisa pair --confirm <6 digits>` (or `--refuse`) later, `--yes-sas <digits>` to confirm
only a request showing those digits, `--no-wait` to show the code and exit, `--json` for scripts. A paired
client can also offer a pairing code for a new device (`POST /v1/pair/offers`; in the PISA app, Settings, "Pair
a new device"). `pisa setup` walks through pairing as its last step.

## The command-line client

Any machine can be a paired device, with its key in `~/.config/pisa/client.json` (or `PISA_CLIENT_FILE`):

```bash
pisa pair laptop --no-wait                                          # on the server: shows the code
pisa client pair '<the pisa://pair... text, or the 26-character code>' --server https://pisa.example.com
pisa pair --confirm <the 6 digits the client shows>                 # on the server
pisa client token                                                   # a fresh 1-hour session token
pisa client call GET /v1/ai/slots                                   # one REST request with a fresh token
pisa client call POST /v1/ai/providers --data '<json>' --sign model_server '<target>'   # a signed one
pisa client approve <approval id>                                   # or reject
pisa client erase all --before 2026-11-18                           # or one family: messages, places, ...
```

Over https the client checks the server's TLS key against the QR's pin before it sends anything. With the typed
code it pins the key the server shows, and the two keyed MACs and the 6 digits prove both sides hold the code.

This client is a developer and testing tool, and the reference for building your own: the
[API](api.md) describes every request it makes.

## A developer's machine (no TLS)

To pair a client with a server on the same machine, at `http://127.0.0.1:8000`, without any certificate:

```bash
# deploy/.env (or core/.env)
PISA_INSECURE_LOOPBACK_PAIRING=true
PISA_ADDRESSES=http://127.0.0.1:8000
PISA_TLS_CERT=
```

Restart Pisa, then pair as above with `--server http://127.0.0.1:8000`. With docker compose, use the `local`
profile with `PISA_LAN_BIND=127.0.0.1`, `PISA_LAN_PORT=8000`, and `PISA_LAN_TLS_CERT=` and
`PISA_LAN_TLS_KEY=` (both empty: plain http), and run the client inside the API's container:
`docker compose exec api-lan pisa client pair '<code>' --server http://127.0.0.1:8000`. To
run it on the host instead (`pisa client ...` from a checkout of `core/` with `uv run`), also set
`PISA_INSECURE_LOOPBACK_GATEWAY=true`, and only with rootful Docker on Linux and `PISA_LAN_BIND=127.0.0.1`: the host's loopback connections then arrive from the compose network's gateway, which Pisa
answers. Under Docker Desktop, rootless Docker or Podman, connections from other machines can arrive from that
gateway too, so leave it off there.

What this mode does, and why it is safe only on your own machine:

- Pairing and every request after it travel as **plain http**: no encryption. The QR carries a fixed pin that
  is no key's pin, so a client never mistakes it for a pinned TLS channel.
- Pairing is still authenticated: the client and the server prove they hold the same one-time code (two
  keyed MACs) and you compare the 6 digits, so nobody can pair in your place.
- Pisa refuses to start with it unless `PISA_TLS_CERT` is empty, every address in `PISA_ADDRESSES` is `http://`
  on loopback (`127.0.0.1`, `::1`, `localhost`), and the API listens on loopback (or runs in a container).
- The API then answers **this machine only**: a connection from a loopback address, or, in a container with
  `PISA_INSECURE_LOOPBACK_GATEWAY=true`, from the container's gateway. A connection from another machine, from
  another container, or through a proxy, tunnel or CDN that says so (`Forwarded`, `X-Forwarded-For`,
  `X-Real-IP`, `Via`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `CF-Connecting-IP`, `True-Client-IP`,
  `X-Client-IP`, `Fastly-Client-IP`) gets 403.
- **A proxy or tunnel in front defeats the mode.** A raw TCP tunnel (`ssh -R`, `socat`, an editor's or
  Codespaces' port forwarding, `tailscale serve tcp`) or a proxy that adds none of those headers delivers
  other machines' connections from loopback, and Pisa cannot tell them apart. Put nothing in front of a server
  in this mode, and keep docker compose's `PISA_LAN_BIND` on `127.0.0.1`.
- Pisa logs a warning when it starts, `pisa pair` prints one, and `pisa doctor` lists it.

Never use it for a phone or any other machine: set up [HTTPS](self-hosting.md#3-https) and turn the mode off.

## Signed operations

Some requests need a signature on top of the session token, so that a copied session token alone cannot do
them: deleting data (`POST /v1/data/erase`, always), approving an action that needs a signature, and, once the
server holds a Face ID key, every account-level change (a connector, a provider key, a model server, revoking
another device, a pairing offer, an autonomy level, a skill).

A client signs with **its own device key**, the P-256 key it registered at pairing, under the key id
`device-<its device id>`:

1. `POST /v1/phone/challenge` with what is to be signed (`{"action": "device", "op": "erase", "target":
   "messages:2026-11-18"}`; for an approval, `{"action": "approval", "approval_id", "interrupt_id"}`). The answer
   holds a `nonce`, the exact `canonical` text and its `expires_at` (5 minutes).
2. Sign the canonical text (ECDSA P-256, SHA-256, DER, base64). For an approval, first rebuild the text
   yourself (CONTRACT-2 §6.2) and sign only if it is byte for byte the server's.
3. Send the request with `X-Pisa-Nonce`, `X-Pisa-Signature` and `X-Pisa-Key-Id: device-<device id>` headers;
   an approval's resume carries them in its body: `{"decision": "approve", "auth": "biometric", "digest",
   "nonce", "signature", "keyId"}` (`biometric` is the contract's word for a signed approval).

The rules are the same as for the PISA app's Face ID key: a nonce works once and for 5 minutes; the signature
covers the exact operation; a revoked device's key signs nothing. A device key signs only its own device's
requests, except that it may vouch for another device's new key (`add_key`), never its own. A key a device
key vouched for is a device key too, never a Face ID key: it signs for its own device only, and it goes when
the device that vouched for it is revoked. A device that registered a Face ID key (the PISA app) signs with
Face ID. A watch only rejects: whatever key it holds, it approves nothing, sets no autonomy level, offers no
pairing, and neither vouches for nor gets a key. `pisa pair --signer` is no longer needed and changes nothing
a client can do.

## Limits

- At most 5 pairing codes are open at once, for the whole server; a code is used once.
- A wrong attempt never spends or blocks a code. Only a flood is paused: 60 failed attempts in 10 minutes
  from one address (an IPv6 address counts by its /64).

## Managing devices

- `pisa devices list`, `pisa devices revoke <id>`: cut a device off (also from a paired client).
- `pisa phone-keys reset --keep <device id>`: remove every Face ID key and revoke every device but one.

API: `POST /v1/pair/begin`, `POST /v1/pair`, `/v1/pair/offers`, `/v1/devices`, `/v1/devices/session`,
`/v1/phone/challenge` (see [API](api.md)).
