# Pairing and sessions

A client becomes one of the user's devices by pairing once. Pairing gives it its own key; afterwards it proves
itself with a session token it signs with that key every hour. There is no long-lived secret to copy.

Example: [`pair.py`](examples/pair.py), [`session.py`](examples/session.py). The byte formats are checked
against the contract's vectors (`protocol/v2/fixtures/pairing/vectors.json`) by [`vectors.py`](examples/vectors.py).

## Discovery

`GET /v1/server` (no authentication, nothing personal) says what the server is and speaks:

```json
{
  "product": "pisa",
  "serverId": "srv_minimal0001_Hc8Wq3Ld5Rv",
  "serverVersion": "0.1.0",
  "contract": {"versions": ["2.0"]},
  "agui": ["1.0"],
  "a2ui": {"versions": ["1.0"], "catalogIds": ["urn:pisa:a2ui:catalog:core:1"]},
  "mcpApps": [],
  "features": [{"id": "stop", "version": 1}, {"id": "approvals.signed", "version": 2}],
  "readiness": {"ready": true},
  "reach": {"scheme": "https", "network": "lan"},
  "push": {"modes": ["none"]}
}
```

(`protocol/v2/examples/server-minimal.json`; `$defs/ServerDiscovery` in the schema.) Check `contract.versions`
holds `2.0` before pairing; `features` lists what this server offers (a client hides what is not listed).
`serverId` is the server's stable id: key everything you store per server by it. `/health` and `/readyz` answer
without authentication too.

## The pairing code

On the server, `pisa pair "My client"` shows a QR code and the same code as 26 characters to type, valid
10 minutes (`--json` prints it for scripts). The server must be set up for it first (its addresses and the TLS
certificate whose key the QR pins): [Pairing](../pairing.md#what-the-server-needs). A paired device can also make one for a new device: see
[Devices](devices.md#pairing-a-second-device).

The QR's text is `pisa://pair?v=1&addrs=<a1>,<a2>&spki=<pin>&token=<token>&exp=<unix seconds>`:

| Field | Format |
|---|---|
| `v` | `1` |
| `addrs` | up to 6 addresses (`https://host:port`, `relay://host/handle`), comma-separated; each one percent-encoded, so a comma inside an address is `%2C`. Split on `,` first, then decode |
| `spki` | the pin of the server's TLS key: standard base64 (with `=`) of SHA-256 over its SubjectPublicKeyInfo (DER), percent-encoded |
| `token` | 16 random bytes, base32 without padding: 26 characters `A-Z` and `2-7`. Typed by hand, any case, with spaces or dashes |
| `exp` | when it expires, Unix seconds |

<!-- include: examples/pair.py#qr -->
```python
def parse_qr(text: str) -> dict[str, Any]:
    """`pisa://pair?v=1&addrs=<a1>,<a2>&spki=<pin>&token=<token>&exp=<unix seconds>`, or a typed token alone."""
    text = text.strip()
    if not text.startswith("pisa://pair?"):
        return {"token": "".join(c for c in text.upper() if c not in " -")}  # the typed fallback
    query: dict[str, str] = {}
    for part in urlsplit(text).query.split("&"):  # split first: an address's own comma is %2C
        name, _, value = part.partition("=")
        query.setdefault(name, value)
    if query.get("v") != "1":
        raise ValueError("a pairing QR of another version")
    return {
        "v": 1,
        "addrs": [unquote(a) for a in query["addrs"].split(",") if a],
        "spki": unquote(query["spki"]),  # standard base64 with its '=' (percent-encoded in the QR)
        "token": query["token"],  # 16 random bytes, base32 without padding: 26 letters A-Z and digits 2-7
        "exp": int(query["exp"]),
    }


def token_bytes(token: str) -> bytes:
    return base64.b32decode(token.upper() + "======")
```

## The pin

Over https the client trusts exactly the certificate whose key the QR pinned: no certificate authority, no host
name check, and nothing is sent before the check. A typed code has no pin: the client takes the key the server
shows, and the MACs below prove it is the server holding the code.

<!-- include: examples/pair.py#pin -->
```python
def spki_pin(der_certificate: bytes) -> str:
    """The pin: standard base64 of SHA-256 over the certificate's SubjectPublicKeyInfo (DER)."""
    from cryptography.x509 import load_der_x509_certificate

    public = load_der_x509_certificate(der_certificate).public_key()
    spki = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def pinned_context(host: str, port: int, pins: list[str]) -> ssl.SSLContext:
    """Trust exactly the certificate whose key is pinned, before anything is sent (no CA, no host name)."""
    pem = ssl.get_server_certificate((host, port), timeout=10)
    if spki_pin(ssl.PEM_cert_to_DER_cert(pem)) not in pins:
        raise SystemExit("The server's TLS key is not the one in the pairing code: nothing was sent.")
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context
```

Plain `http` has nothing to pin: use it only for a server on the same machine (what the examples do in CI). A
server set up for that, with no certificate at all ([insecure loopback mode](../pairing.md#a-developers-machine-no-tls)),
puts a fixed pin in its QR that is no key's: base64 of SHA-256 over the bytes `pisa-insecure-loopback/1`
(`wLQ4NOolMm534yhxch2YVkRqSAMleCNoqaPfxzBUsig=`). With a typed code to such a server, use that pin in the
transcript.
Keep the pins per server. When the server announces a new key (`pisa.endpoints` on the socket, or
`GET /v1/server/endpoints`: `{revision, endpoints, spki: [current, next]}`), trust both.

## The device key

A P-256 key made on the device, never leaving it (in the platform's key store where there is one). The server
gets its public half as SubjectPublicKeyInfo DER, base64url. (An X9.63 point, 65 bytes starting `04`, is
accepted too.)

<!-- include: examples/pair.py#device-key -->
```python
def new_device_key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """A P-256 key made on the device; the server gets its public half as SubjectPublicKeyInfo DER, base64url."""
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return key, b64url(der)
```

## The exchange

1. `POST /v1/pair/begin` (no body, no auth) → `201 {pairingId, commitment, expiresAt}`. The server picks its
   nonce now and sends only a commitment to it.
2. `POST /v1/pair` with `{v: 1, pairingId, deviceKey, name, platform, clientNonce, mac}` → `201 {pairingId,
   deviceId, serverNonce, mac, state: "awaiting_confirmation"}`. `platform` is `ios`, `watchos`, `android`,
   `web`, `cli` or `other`; `clientNonce` is 32 random bytes, base64url.
3. The client checks the revealed `serverNonce` against the commitment, and the server's MAC, **before** it
   shows any digits. Either fails: abort, show nothing, start again.
4. Both sides show the SAS, 6 digits (display them as `123 456`). The user compares them and confirms on the
   server (`pisa pair --confirm 123456`, or the prompt `pisa pair` shows) or on another paired device.
5. `GET /v1/pair/{pairingId}` (no auth) until `state` is `confirmed`; `refused` and `expired` (5 minutes) end
   it.

The bytes, all UTF-8, joined by `\n`:

| Value | How |
|---|---|
| transcript | `pisa-pair/1`, the pin, the device key, the client nonce, each exactly as sent |
| the client's `mac` | HMAC-SHA256 keyed by the token's 16 bytes over the transcript, base64url |
| the server's `mac` | HMAC-SHA256, same key, over the transcript, `\n` and the server nonce |
| `commitment` | SHA-256 over `pisa-sas-commit/1`, `\n` and the server nonce, base64url |
| SAS | SHA-256 over `pisa-sas/1`, `\n`, the transcript, `\n`, the server nonce; the first 4 bytes as a big-endian number, modulo 1 000 000, as 6 digits |

<!-- include: examples/pair.py#transcript -->
```python
def transcript(spki: str, device_key: str, client_nonce: str) -> bytes:
    return "\n".join(("pisa-pair/1", spki, device_key, client_nonce)).encode()


def client_mac(token: bytes, text: bytes) -> str:
    return b64url(hmac.new(token, text, hashlib.sha256).digest())


def server_mac(token: bytes, text: bytes, server_nonce: str) -> str:
    return b64url(hmac.new(token, text + b"\n" + server_nonce.encode(), hashlib.sha256).digest())


def commitment(server_nonce: str) -> str:
    return b64url(hashlib.sha256(b"pisa-sas-commit/1\n" + server_nonce.encode()).digest())


def sas(text: bytes, server_nonce: str) -> str:
    digest = hashlib.sha256(b"pisa-sas/1\n" + text + b"\n" + server_nonce.encode()).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"
```

<!-- include: examples/pair.py#pair -->
```python
async def pair(http: httpx.AsyncClient, qr: dict[str, Any], device_key: str, name: str) -> dict[str, str]:
    token = token_bytes(qr["token"])
    nonce = b64url(secrets.token_bytes(32))
    text = transcript(qr["spki"], device_key, nonce)
    begun = checked(await http.post("/v1/pair/begin"))  # {pairingId, commitment, expiresAt}
    answer = checked(
        await http.post(
            "/v1/pair",
            json={
                "v": 1,
                "pairingId": begun["pairingId"],
                "deviceKey": device_key,
                "name": name[:60],
                "platform": "other",  # ios, watchos, android, web, cli or other
                "clientNonce": nonce,
                "mac": client_mac(token, text),
            },
        )
    )  # {pairingId, deviceId, serverNonce, mac, state}
    # Both checks come before any digits are shown. If either fails: abort, show nothing, start again.
    if not hmac.compare_digest(commitment(answer["serverNonce"]), begun["commitment"]):
        raise SystemExit("The server revealed another nonce than it committed to: not paired.")
    if not hmac.compare_digest(answer["mac"], server_mac(token, text, answer["serverNonce"])):
        raise SystemExit("The server's answer does not prove it holds the pairing code: not paired.")
    return {
        "pairingId": answer["pairingId"],
        "deviceId": answer["deviceId"],
        "sas": sas(text, answer["serverNonce"]),
    }


async def wait_confirmed(http: httpx.AsyncClient, pairing_id: str, timeout_s: float = 300) -> str:
    """`awaiting_confirmation` until the user answers, then `confirmed`, `refused` or `expired`."""
    deadline = time.monotonic() + timeout_s
    while True:
        state = checked(await http.get(f"/v1/pair/{pairing_id}"))["state"]
        if state != "awaiting_confirmation" or time.monotonic() > deadline:
            return state
        await asyncio.sleep(1)
```

Limits: a code is used once; at most 5 codes are open per server; at most 20 pairings begun and unfinished per
client address; 60 pairing requests from one address that match no code within 10 minutes pause that address
(429 `rate_limited`). A wrong attempt never spends or blocks a code.

## Session tokens

Every request about the user's data carries `Authorization: Bearer <token>`, and so does the live socket's
upgrade request. A token lasts one hour. To get one:

1. `GET /v1/devices/session/nonce?deviceId=<id>` (no auth) → `{nonce, expires_at}`: 32 bytes, base64url, single
   use, 60 seconds.
2. Sign `pisa-session/1\n<deviceId>\n<nonce>` with the device key: ECDSA P-256 with SHA-256, the signature in
   DER, base64url.
3. `POST /v1/devices/session` with `{deviceId, nonce, signature}` → `{token, expires_at}`.

<!-- include: examples/session.py#session -->
```python
def session_text(device_id: str, nonce: str) -> bytes:
    return f"pisa-session/1\n{device_id}\n{nonce}".encode()


async def new_token(http: httpx.AsyncClient, state: State) -> tuple[str, datetime]:
    """A session token and when it expires: a nonce (single use, 60 s), signed by the device key."""
    nonce = checked(await http.get("/v1/devices/session/nonce", params={"deviceId": state.device_id}))[
        "nonce"
    ]
    signature = state.key.sign(session_text(state.device_id, nonce), ec.ECDSA(hashes.SHA256()))  # DER
    answer = checked(
        await http.post(
            "/v1/devices/session",
            json={"deviceId": state.device_id, "nonce": nonce, "signature": b64url(signature)},
        )
    )
    return answer["token"], datetime.fromisoformat(answer["expires_at"])
```

### Refresh, expiry, revocation

- **Refresh** before the hour is over: sign a new nonce. Tokens are cheap; there is no refresh token.
- **Expiry**: a REST call answers 401 `session_expired`; the live socket closes with code 4003 and reason
  `session_expired`. Sign a new token and retry once (reconnect, with `resume`: [Text chat](text-chat.md#reconnect-and-resume)).
- **Revocation** (the user removed this device, from another device or with `pisa devices revoke`): a REST
  call answers 401 `device_revoked`; the live socket sends a control error `device_revoked` and closes with
  4003. Asking for a new nonce for a revoked device answers 400 `validation_failed` ("There is no such paired
  device."), the same answer as for a device that never existed. Either way: forget the key and pair again.

<!-- include: examples/session.py#refresh -->
```python
class Revoked(Exception):
    """This device was revoked (or unpaired): forget the key and pair again."""


class Session:
    """A token kept fresh: renewed 5 minutes before its hour is over, and once more on 401 session_expired."""

    def __init__(self, http: httpx.AsyncClient, state: State) -> None:
        self.http, self.state = http, state
        self.token: str | None = None
        self.expires = datetime.min.replace(tzinfo=UTC)

    async def bearer(self) -> dict[str, str]:
        if self.token is None or datetime.now(UTC) > self.expires - timedelta(minutes=5):
            try:
                self.token, self.expires = await new_token(self.http, self.state)
            except PisaError as err:
                # A revoked (or unknown) device gets 400 validation_failed already for the nonce ("There is no
                # such paired device."): the same answer for each case, so it reveals nothing to a stranger.
                if err.code == "device_revoked" or (err.status, err.code) == (400, "validation_failed"):
                    raise Revoked(err.message) from None
                raise
        return {"Authorization": f"Bearer {self.token}"}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in (1, 2):
            answer = await self.http.request(method, path, headers=await self.bearer(), **kwargs)
            try:
                return checked(answer)
            except PisaError as err:
                if err.code == "device_revoked":
                    raise Revoked(err.message) from None
                if err.code == "session_expired" and attempt == 1:
                    self.token = None  # sign a new nonce and try once more
                    continue
                raise
        raise AssertionError("unreachable")
```

`DELETE /v1/devices/me` unpairs this device itself: it is revoked, and its push registration and keys are
deleted.

## Run it

```bash
pisa pair "My client" --no-wait --json            # on the server: the QR's text as {"uri": ...}
python pair.py 'pisa://pair?v=1&addrs=...' --name "My client"
#  SAS 250822
#  Confirm on the server: pisa pair --confirm 250822
pisa pair --confirm 250822                         # on the server
#  Paired as device 4e1833ea-...; state saved in pisa-client.json
python session.py
#  token valid until 2026-11-18T22:12:12+00:00
#  no token: HTTP 401 auth_failed
#  a token that is not the server's: HTTP 401 auth_failed
#  a signature over the wrong text: HTTP 401 auth_failed
```
