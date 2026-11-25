# Pisa push relay

A small service that hands sealed push signals from self-hosted Pisa servers to Apple (APNs), for a publisher of
a Pisa iOS app who chooses to run it. None is offered by the PISA project today; if one is ever offered, its
operator, the metadata it sees and its retention will be stated before it is available. Apple delivers pushes to
the App Store app only from whoever holds the app's APNs key, and a self-hosted server cannot hold it. So the
server sends the relay an opaque signal, and the relay sends Apple a generic push.

**Its one rule: it learns nothing about the user's life.** A notice is sealed on the user's server to a key that
only the phone has; the relay passes the sealed blob along and cannot open it. What it does see, and what it
keeps, is in [PRIVACY.md](PRIVACY.md). How it could fail, and what stops that, is in
[THREAT-MODEL.md](THREAT-MODEL.md). The sending API that Pisa servers use is [PROTOCOL.md](PROTOCOL.md)
(CONTRACT-2 §9 pins its request).

People who build the app themselves do not need this: their server sends to APNs directly with their own key.

## What it does

| Route | Caller | What |
|---|---|---|
| `POST /v1/register` | the app | `{apns_token, environment, signed_transaction, signed_renewal_info?, account_key?, language?}` → `201 {handle, secret, app_secret}`. The App Store signed transaction of the user's subscription is verified against Apple Root CA - G3. |
| `DELETE /v1/register` | the app, or the user's server | `{handle}`, signed with `app_secret` (or with `secret`, as a push is) → `204`. The handle ends. |
| `POST /v1/register/activity` | the app | `{handle, token, kind}`, signed with `app_secret` → `204`. A Live Activity token of this phone the handle may push to: `kind` `start` (a push-to-start token) or `update` (a running activity's token). The relay keeps the last 4 of each kind, as SHA-256 hashes. |
| `POST /v1/register/devices` | the app | `{handle}`, signed with `app_secret` → `200 {devices: [...]}`: the subscription's registrations, with opaque ids and their days of registration and last use. |
| `POST /v1/register/devices/remove` | the app | `{handle, device}`, signed with `app_secret` → `204`: ends one of them: any, for a phone that proved the account key; otherwise itself, or one idle for 30 days. |
| `POST /v1/push` | the user's Pisa server | `{handle, type, collapse_id?, sealed?, activity?}`, HMAC-signed ([PROTOCOL.md](PROTOCOL.md)) → `202`, or `400`, `401`, `410`, `413`, `422`, `429`, `500`, `502`. |
| `POST /v1/appstore/notifications` | Apple | App Store Server Notifications V2. An expiry, refund or revocation ends the subscription's handles; a renewal extends them. |
| `GET /healthz` | anyone | `{"ok": true}` |
| `GET /metrics` | the operator | Prometheus counters by route, type and status. Only with `Authorization: Bearer <RELAY_METRICS_TOKEN>`; `404` when no token is set. |

The exact wire shapes of the app's routes, the app's signature and the account key are in
[PROTOCOL.md](PROTOCOL.md), "The app's API".

Registration in detail: the app sends its APNs device token (hex), `production` or `sandbox`, the JWS of its
current subscription transaction (StoreKit 2 `Transaction.jwsRepresentation`, signed within the last 7 days,
`RELAY_TRANSACTION_MAX_AGE_S`; the app fetches the current transaction just before registering) and,
optionally, its language (a BCP 47 tag; only `en` or `it` is kept, for the generic banner). A subscriber in
Apple's billing grace period (the transaction has expired, Apple still grants access) also sends the renewal
info's JWS (`Product.SubscriptionInfo.RenewalInfo`, `signed_renewal_info`); its `gracePeriodExpiresDate`
counts as the end. When the purchase carries an `appAccountToken`, the app also sends the account key it was
derived from (`account_key`), so a copy of the transaction alone does not register.

The relay answers a random 128-bit `handle` and two random 256-bit secrets, all base64url without padding, and
keeps only their SHA-256, sealed. The app hands `handle` and `secret` to its own server over the paired
connection (`PUT /v1/devices/me/push`), and keeps `app_secret` to itself: it signs the app's own requests and
never leaves the phone.

Errors: `400 {"error": "malformed"}`; `403 {"error": "subscription_not_verified"}` (not Apple's, not this
app's, expired, stale, or refunded or revoked since it was signed); `403 {"error": "account_not_verified"}`
(the purchase is bound to an account key and the request does not prove it); `409 {"error":
"too_many_devices"}` (the subscription already has its 10 handles, all used within 30 days: the app lists them
with `POST /v1/register/devices` from a phone that is registered, and the user removes one); `413`; `429
{"error": "too_many_registrations"}` (5 an hour per subscription).

Live Activities: the app registers each Live Activity token it gets (`Activity.pushToStartTokenUpdates`,
`activity.pushTokenUpdates`) with `POST /v1/register/activity`, besides giving it to its own server. The relay
sends a Live Activity push only to a token the app registered for that handle, so neither a handle nor the
server holding it can drive Live Activities on any other phone.

## Configuration

Everything comes from environment variables. Nothing real is in this repository.

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_STORAGE_KEY` | (required) | 32 random bytes, base64url: seals the HMAC keys in the database and keys the subscription references. `python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"`. Losing it ends every registration; leaking it with the database lets someone sign pushes. |
| `RELAY_BUNDLE_ID` | (required) | the app's bundle id, e.g. `com.example.pisa`: the APNs topic, and what a transaction must name |
| `RELAY_PRODUCT_IDS` | (required) | the subscription's product ids, comma-separated |
| `RELAY_STOREKIT_ENVIRONMENTS` | `Production` | accepted transaction environments. Keep the production relay at `Production`; see "TestFlight" below |
| `RELAY_APNS_KEY_PATH` | (required to run) | the vendor's APNs auth key (`AuthKey_XXXXXXXXXX.p8`), mounted read-only |
| `RELAY_APNS_KEY_ID` | (required to run) | its key id |
| `RELAY_APNS_TEAM_ID` | (required to run) | the vendor's team id |
| `RELAY_DATABASE` | `relay.sqlite3` | the SQLite file (`/data/relay.sqlite3` in the image) |
| `RELAY_PUSH_PER_MINUTE` | `60` | per-handle limit |
| `RELAY_PUSH_PER_DAY` | `2000` | per-handle limit |
| `RELAY_HANDLES_PER_SUBSCRIPTION` | `10` | at this, a new registration replaces the subscription's least recently used handle that has been idle for `RELAY_EVICT_IDLE_DAYS`; with none idle, it is refused (409) |
| `RELAY_EVICT_IDLE_DAYS` | `30` | a handle used or registered within this many days is never evicted |
| `RELAY_REGISTER_PER_SUBSCRIPTION_HOUR` | `5` | registrations per subscription per hour |
| `RELAY_TRANSACTION_MAX_AGE_S` | `604800` | how recently a registration's transaction must have been signed (7 days) |
| `RELAY_EXPIRY_GRACE_S` | `86400` | pushes still pass this long after the subscription's known end (its expiry, or Apple's billing grace when it is in it): a late renewal notice |
| `RELAY_METRICS_TOKEN` | (unset: `/metrics` off) | bearer token for `/metrics` |
| `RELAY_LIVE_ACTIVITY_ATTRIBUTES_TYPE` | `NextStopAttributes` | the only `attributes-type` a Live Activity start may name |

In App Store Connect, set the app's **App Store Server Notifications V2** production URL to
`https://<relay host>/v1/appstore/notifications`.

### TestFlight

TestFlight builds use APNs `production` but StoreKit `Sandbox`, where anyone in the TestFlight group (and,
with a public link, anyone at all) subscribes for free and sandbox subscriptions renew every few minutes. Run a
**second relay instance** for them, with its own database, its own `RELAY_STORAGE_KEY`, and
`RELAY_STOREKIT_ENVIRONMENTS=Sandbox` (or `Production,Sandbox`), on its own host name; point the TestFlight
build at it, and the App Store Server Notifications **sandbox** URL at it. Never add `Sandbox` to the
production relay: it would make the relay free to every tester.

## Run it

Locally:

```sh
cd relay
uv sync
RELAY_STORAGE_KEY=... RELAY_BUNDLE_ID=com.example.pisa RELAY_PRODUCT_IDS=com.example.pisa.plus.monthly \
RELAY_APNS_KEY_PATH=./AuthKey_ABC123DEFG.p8 RELAY_APNS_KEY_ID=ABC123DEFG RELAY_APNS_TEAM_ID=TEAMID1234 \
uv run uvicorn --factory pisa_relay.app:app_from_env --port 8080 --no-access-log
```

With Docker:

```sh
docker build -t pisa-relay relay/
docker run -d --name pisa-relay -p 127.0.0.1:8080:8080 -v pisa-relay-data:/data \
  -v /etc/pisa-relay/AuthKey.p8:/run/secrets/apns.p8:ro --env-file /etc/pisa-relay/env \
  -e RELAY_APNS_KEY_PATH=/run/secrets/apns.p8 pisa-relay
```

**On a small VPS** (the intended home: one vCPU and 512 MB are plenty): run the container behind a TLS reverse
proxy (Caddy, nginx) that terminates HTTPS on 443. Keep the proxy's access log **off** for this host (Caddy
logs nothing unless asked; in nginx, `access_log off;`), or the relay's privacy promise is broken by the
machine in front of it. Keep `RELAY_STORAGE_KEY` in the platform's secret store, not in a file on the data
volume; back up the SQLite file and the key separately (the file alone is useless). Losing either means every
app registers again. The relay **refuses to start** when the key does not open the database (a typo, a
rotation, a restore beside another key), and answers 500, never "gone", if a stored key ever fails to open: a
wrong key must not make every Pisa server delete its registration.

### Required: a per-client limit at the proxy

The relay keeps no network addresses, so it cannot tell one client from another. Its own limits are per handle
and per subscription (5 registrations an hour, counted only once Apple's signature checks out); there is no
limit across all clients, because anyone could hold such a limit down and keep every real registration at 429.
Verifying a registration costs about a millisecond, so the relay does not need one. **The proxy in front of the
relay must limit requests per client**, on `/v1/register` at least, and must not log the addresses it limits.

Key the limit on the client's **/64 for IPv6 and /32 for IPv4**, not on the full address: one IPv6 host
usually has a whole /64 to itself and can pick a fresh address for every request, so a limit on the /128 limits
nothing; a /64 is what an ISP or a host hands one customer. Both examples below map the address to that prefix
first (the address is in its canonical text form, RFC 5952, in both nginx and Caddy). The figures are a
starting point: a phone registers a few times a year, and one Pisa server pushes for one household (raise the
push limit for a server that hosts many users behind one address).

Caddy, with the [`caddy-ratelimit`](https://github.com/mholt/caddy-ratelimit) module (build with
`xcaddy build --with github.com/mholt/caddy-ratelimit`); it keeps keys in memory for the window only, and
Caddy writes no access log unless a `log` directive asks for one:

```caddyfile
{
	order rate_limit before reverse_proxy
}

relay.example.com {
	# the client's /32 (IPv4) or /64 (IPv6), from its canonical address
	map {remote_host} {relay_client} {
		~^(\d+\.\d+\.\d+\.\d+)$ "${1}"
		~^::ffff:(\d+\.\d+\.\d+\.\d+)$ "${1}"
		~^([0-9a-f]{1,4}:[0-9a-f]{1,4}:[0-9a-f]{1,4}:[0-9a-f]{1,4}): "${1}"
		~^([0-9a-f]{1,4}):([0-9a-f]{1,4}):([0-9a-f]{1,4}):: "${1}:${2}:${3}:0"
		~^([0-9a-f]{1,4}):([0-9a-f]{1,4}):: "${1}:${2}:0:0"
		~^([0-9a-f]{1,4})::([0-9a-f]{1,4})(?::[0-9a-f]{1,4}){4}$ "${1}:0:0:${2}"
		~^([0-9a-f]{1,4}):: "${1}:0:0:0"
		~^::([0-9a-f]{1,4}):([0-9a-f]{1,4})(?::[0-9a-f]{1,4}){4}$ "0:0:${1}:${2}"
		~^::([0-9a-f]{1,4})(?::[0-9a-f]{1,4}){4}$ "0:0:0:${1}"
		default "0:0:0:0"
	}
	rate_limit {
		zone register {
			match {
				path /v1/register /v1/register/*
			}
			key {relay_client}
			events 10
			window 1m
		}
		zone push {
			match {
				path /v1/push
			}
			key {relay_client}
			events 600
			window 1m
		}
	}
	reverse_proxy 127.0.0.1:8080
}
```

nginx (`limit_req` logs each refusal with the client's address at `error` level, so the error log for this
host is kept at `crit`):

```nginx
# the client's /32 (IPv4) or /64 (IPv6), from its canonical address
map $remote_addr $relay_client {
    ~^(?<a>\d+\.\d+\.\d+\.\d+)$                                         $a;
    ~^::ffff:(?<a>\d+\.\d+\.\d+\.\d+)$                                  $a;
    ~^(?<p>[0-9a-f]{1,4}:[0-9a-f]{1,4}:[0-9a-f]{1,4}:[0-9a-f]{1,4}):     $p;
    ~^(?<a>[0-9a-f]{1,4}):(?<b>[0-9a-f]{1,4}):(?<c>[0-9a-f]{1,4})::      $a:$b:$c:0;
    ~^(?<a>[0-9a-f]{1,4}):(?<b>[0-9a-f]{1,4})::                          $a:$b:0:0;
    ~^(?<a>[0-9a-f]{1,4})::(?<d>[0-9a-f]{1,4})(?::[0-9a-f]{1,4}){4}$     $a:0:0:$d;
    ~^(?<a>[0-9a-f]{1,4})::                                              $a:0:0:0;
    ~^::(?<c>[0-9a-f]{1,4}):(?<d>[0-9a-f]{1,4})(?::[0-9a-f]{1,4}){4}$    0:0:$c:$d;
    ~^::(?<d>[0-9a-f]{1,4})(?::[0-9a-f]{1,4}){4}$                        0:0:0:$d;
    default                                                              0:0:0:0;
}
limit_req_zone $relay_client zone=relay_register:10m rate=10r/m;
limit_req_zone $relay_client zone=relay_push:10m rate=10r/s;

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name relay.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;
    access_log off;
    error_log /var/log/nginx/relay.error.log crit;
    limit_req_status 429;
    client_max_body_size 64k;

    location /v1/register {
        limit_req zone=relay_register burst=5 nodelay;
        proxy_pass http://127.0.0.1:8080;
    }
    location /v1/push {
        limit_req zone=relay_push burst=60 nodelay;
        proxy_pass http://127.0.0.1:8080;
    }
    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

The two maps are the same rules (checked against Python's `ipaddress` on 200,000 random canonical
addresses while writing them): an IPv4 address is its own key; an IPv6 address is cut to its first four
groups, with the `::` shorthand expanded where it falls inside them.

`location /v1/register` is a prefix match, so it also covers `/v1/register/activity`. Do not forward the
client's address to the relay (no `X-Forwarded-For`); it would not use it.

**Serverless** works with one condition: a single instance with a persistent disk (for example a container
platform with one always-on instance and a volume). The replay window and the rate limits live in the SQLite
file, so several instances, or a disk that is wiped between requests, would weaken both. Scaling out would mean
moving the three tables to Postgres; the queries are plain SQL.

Run one worker. The process is single-threaded and fast; SQLite has one writer.

## Develop

```sh
cd relay
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

The tests generate their own App Store certificate chain (pinned in place of Apple's root), use a fake APNs, and
run the Pisa server's own sender (`core/pisa`) against the relay as an interop test.

## Licence

AGPL-3.0-only ([LICENSE](LICENSE)). If you run a modified relay for others, offer them its source.
