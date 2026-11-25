"""The headless client (`pisa client ...`): the CLI as a paired device of its own, with a software device key
(CONTRACT-2 §4.3 and §15: no device token that never expires, so the CLI signs like the app does).

- `pair`: a fresh P-256 key, `POST /v1/pair` with the HMAC under the QR's token (or the typed code), the SAS
  to compare, then `GET /v1/pair/{id}` until the user confirms on the server. Over https the TLS key is
  checked against the QR's pin before anything is sent, and every later request is pinned to it.
- `token`: a nonce signed with the key, for a 1 h session token (`curl -H "Authorization: Bearer ..."`).
- `call`: one REST request with a fresh session token; `--sign OP TARGET` adds the device-op signature
  (`pisa-device`, e.g. `model_server` or `provider_key`) with the same key, named `device-<device id>`. Any
  paired client's own key signs: no Face ID key and no `--signer` is needed.
- `approve` / `reject`: an approval's REST resume (`POST /v1/approvals/{id}/resume`, CONTRACT-2 §6). The
  digest is recomputed here from `GET /v1/approvals/{id}`; an approval that needs a signature gets the
  `pisa-approval/2` challenge, which is signed only when it is byte for byte the text this client rebuilds.
- `erase`: their deletion by family (`POST /v1/data/erase`, signed device op `erase`).
- `paid-ack`: the user's OK of a paid maps or web provider and its monthly cap (`PUT
  /v1/connections/paid/{provider}`, signed device op `paid_ack`, target `<provider>:<cap_cents>`).
- `new-conversation`: a new conversation for this client (`POST /v1/conversations/new`, nothing deleted).
- `delete-conversation`: this client's own conversations (`DELETE /v1/conversations/conversation:<its device
  id>`, signed device op `conversation`).

Over plain http to a loopback address (a server in insecure loopback mode, a developer's machine) a typed code
uses that mode's fixed pin; https is pinned as above.

The key and what it was paired with live in one file (`~/.config/pisa/client.json`, or PISA_CLIENT_FILE), mode
0600. It holds no server secret: the key only signs.
"""

import asyncio
import base64
import json
import os
import ssl
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from pisa.domain import approval_sign
from pisa.domain import erase as erase_rules
from pisa.domain import pairing as rules

DEFAULT_FILE = "~/.config/pisa/client.json"
REVEAL_MISMATCH = "Pairing failed. Do not confirm on the server; start again."


class ClientError(Exception):
    """What went wrong, for the command line."""


def client_file(path: str | None = None) -> Path:
    return Path(path or os.environ.get("PISA_CLIENT_FILE") or DEFAULT_FILE).expanduser()


def _private_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


class Client:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        loaded = serialization.load_pem_private_key(data["key"].encode(), password=None)
        assert isinstance(loaded, ec.EllipticCurvePrivateKey)
        self.key = loaded

    @classmethod
    def new(cls, server: str) -> "Client":
        key = ec.generate_private_key(ec.SECP256R1())
        return cls({"server": server.rstrip("/"), "key": _private_pem(key)})

    @classmethod
    def load(cls, path: Path) -> "Client":
        try:
            return cls(json.loads(path.read_text()))
        except FileNotFoundError:
            raise ClientError(f"No client file at {path}: pair first with `pisa client pair`.") from None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(self.data, out, indent=2)

    @property
    def device_key(self) -> str:
        der = self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return rules.b64url(der)

    def sign(self, message: bytes) -> bytes:
        return self.key.sign(message, ec.ECDSA(hashes.SHA256()))

    # --- the channel
    # ------------------------------------------------------------------------------------------
    def verify(self) -> ssl.SSLContext | bool:
        """httpx's `verify`: over https, a context that trusts exactly the certificate whose key the QR
        pinned."""
        parts = urlsplit(self.data["server"])
        pins = self.data.get("spki") or []
        if parts.scheme != "https" or not pins:
            return True
        host, port = parts.hostname or "", parts.port or 443
        try:
            pem = ssl.get_server_certificate((host, port), timeout=10)
        except OSError as exc:
            raise ClientError(f"Cannot reach {host}:{port} ({exc.__class__.__name__}).") from None
        if rules.spki_pin_der(ssl.PEM_cert_to_DER_cert(pem)) not in pins:
            raise ClientError(
                "The server's TLS key is not the one in the pairing code: someone may be in between. Nothing "
                "was sent."
            )
        context = ssl.create_default_context(cadata=pem)
        context.check_hostname = False
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return context

    def http(self, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
        if transport is not None:
            return httpx.AsyncClient(base_url=self.data["server"], transport=transport, timeout=30)
        return httpx.AsyncClient(base_url=self.data["server"], verify=self.verify(), timeout=30)

    # --- pairing
    # ----------------------------------------------------------------------------------------------
    async def pair(self, http: httpx.AsyncClient, code: str, name: str) -> dict[str, Any]:
        """`POST /v1/pair/begin` (the server commits to its nonce), then `POST /v1/pair`: the pairing id, the
        device id and the SAS to compare. Raises, showing no digits, when the revealed nonce is not the one
        the server committed to, or the server's MAC does not prove it holds the same token."""
        try:
            qr = rules.parse_qr(code)
        except rules.PairingInputError as err:
            raise ClientError(str(err)) from None
        spki = qr.get("spki") or next(iter(self.data.get("spki") or []), None)
        if not spki:
            raise ClientError("A typed code needs the server over https, to pin the key it shows.")
        token = rules.token_bytes(qr["token"])
        nonce = rules.new_nonce()
        text = rules.transcript(spki, self.device_key, nonce)
        begun = _json(await http.post("/v1/pair/begin"))
        body = {"v": 1, "pairingId": begun["pairingId"], "deviceKey": self.device_key, "name": name[:60],
                "platform": "cli", "clientNonce": nonce, "mac": rules.client_mac(token, text)}  # fmt: skip
        answer = _json(await http.post("/v1/pair", json=body))
        if not rules.reveal_matches(begun["commitment"], str(answer.get("serverNonce"))):
            raise ClientError(REVEAL_MISMATCH)
        if not rules.macs_equal(answer["mac"], rules.server_mac(token, text, answer["serverNonce"])):
            raise ClientError("The server's answer does not prove it holds the pairing code: not paired.")
        self.data["deviceId"] = answer["deviceId"]
        return {
            "pairingId": answer["pairingId"],
            "deviceId": answer["deviceId"],
            "sas": rules.sas(text, answer["serverNonce"]),
        }

    async def wait_confirmed(self, http: httpx.AsyncClient, pairing_id: str, timeout_s: float = 300) -> str:
        """The pairing's final state (`confirmed`, `refused` or `expired`)."""
        deadline = time.monotonic() + timeout_s
        while True:
            state = _json(await http.get(f"/v1/pair/{pairing_id}"))["state"]
            if state != "awaiting_confirmation" or time.monotonic() > deadline:
                return state
            await asyncio.sleep(rules.POLL_S)

    # --- sessions and requests
    # --------------------------------------------------------------------------------
    async def session(self, http: httpx.AsyncClient) -> str:
        if "deviceId" not in self.data:
            raise ClientError("This client is not paired: run `pisa client pair` first.")
        nonce = _json(
            await http.get("/v1/devices/session/nonce", params={"deviceId": self.data["deviceId"]})
        )["nonce"]
        signature = rules.b64url(self.sign(rules.session_text(self.data["deviceId"], nonce)))
        answer = _json(
            await http.post(
                "/v1/devices/session",
                json={"deviceId": self.data["deviceId"], "nonce": nonce, "signature": signature},
            )
        )
        return answer["token"]

    async def call(
        self, http: httpx.AsyncClient, method: str, path: str, *, body: str | None = None,
        sign: tuple[str, str] | None = None,
    ) -> httpx.Response:  # fmt: skip
        token = await self.session(http)
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["content-type"] = "application/json"
        if sign is not None:
            op, target = sign
            challenge = _json(
                await http.post(
                    "/v1/phone/challenge",
                    json={"action": "device", "op": op, "target": target},
                    headers=headers,
                )
            )
            headers |= {
                "X-Pisa-Nonce": challenge["nonce"],
                "X-Pisa-Signature": base64.b64encode(self.sign(challenge["canonical"].encode())).decode(),
                "X-Pisa-Key-Id": self.key_id,
            }
        return await http.request(method.upper(), path, content=body, headers=headers)

    @property
    def key_id(self) -> str:
        """The name this client's device key signs under (`device-<device id>`)."""
        return rules.device_key_id(self.data["deviceId"])

    async def decide(self, http: httpx.AsyncClient, approval_id: str, decision: str) -> httpx.Response:
        """Approve or reject an approval over REST (`ApprovalResume`). The digest is this client's own, from
        the approval as the server shows it; an approval that `requires_biometric` is approved with this
        client's device key over the `pisa-approval/2` challenge (`auth: biometric` is the contract's word for
        a signed approval), after checking the server's text is exactly the one rebuilt here."""
        token = await self.session(http)
        headers = {"Authorization": f"Bearer {token}"}
        approval_id = approval_id.lower()
        shown = _json(await http.get(f"/v1/approvals/{approval_id}", headers=headers))
        digest = approval_sign.digest(shown["kind"], shown["payload"], shown["recipients"])
        body: dict[str, Any] = {"decision": decision, "auth": "tap", "digest": digest}
        if decision == "approve" and shown.get("requires_biometric"):
            interrupt = approval_sign.interrupt_id_for(approval_id)
            challenge = _json(
                await http.post(
                    "/v1/phone/challenge",
                    json={"action": "approval", "approval_id": approval_id, "interrupt_id": interrupt},
                    headers=headers,
                )
            )
            server = _json(await http.get("/v1/server"))["serverId"]
            mine = approval_sign.canonical(
                server=server, device=self.data["deviceId"], approval_id=approval_id, interrupt_id=interrupt,
                decision="approve", kind=shown["kind"], digest_hex=digest,
                recipients_count=len(approval_sign.normalise_recipients(shown["recipients"])),
                nonce=challenge["nonce"], expires_at=challenge["expires_at"],
            )  # fmt: skip
            if mine != challenge["canonical"]:
                raise ClientError(
                    "The server's approval text is not the one this client rebuilt: not signed."
                )
            body |= {
                "auth": "biometric",
                "nonce": challenge["nonce"],
                "signature": base64.b64encode(self.sign(mine.encode())).decode(),
                "keyId": self.key_id,
            }
        return await http.post(f"/v1/approvals/{approval_id}/resume", json=body, headers=headers)

    async def erase(self, http: httpx.AsyncClient, family: str, before: date) -> httpx.Response:
        """Their deletion of one family's rows older than `before`, signed with this client's key."""
        body = json.dumps({"family": family, "before": before.isoformat()})
        sign = ("erase", erase_rules.target(family, before))
        return await self.call(http, "POST", "/v1/data/erase", body=body, sign=sign)

    async def paid_ack(self, http: httpx.AsyncClient, provider: str, cap_cents: int) -> httpx.Response:
        """The user's OK of a paid provider (`google`, `tavily`) with its monthly cap in cents, signed with
        this client's key over the target `<provider>:<cap_cents>`, the same cap the body sends."""
        body = json.dumps({"cap_cents": cap_cents})
        sign = ("paid_ack", f"{provider}:{cap_cents}")
        return await self.call(http, "PUT", f"/v1/connections/paid/{provider}", body=body, sign=sign)

    @property
    def thread_id(self) -> str:
        """This client's conversation thread on the server: `conversation:<device id>`, lowercase."""
        return f"conversation:{str(self.data['deviceId']).lower()}"

    async def new_conversation(self, http: httpx.AsyncClient) -> httpx.Response:
        """A new conversation for this client (`POST /v1/conversations/new`): its next turn starts fresh for
        the model; nothing is deleted and nothing signed."""
        return await self.call(http, "POST", "/v1/conversations/new")

    async def delete_conversation(
        self, http: httpx.AsyncClient, thread_id: str | None = None
    ) -> httpx.Response:
        """A conversation deleted, signed with this client's key (device op `conversation`, target the thread
        id): this client's own by default (`thread_id`); the server answers 204, or 403 `other_device` for
        another device's thread and 404 `not_found` for one no device has."""
        thread = thread_id or self.thread_id
        return await self.call(http, "DELETE", f"/v1/conversations/{thread}", sign=("conversation", thread))


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        error = (data or {}).get("error") or {}
        raise ClientError(
            f"{response.request.method} {response.request.url.path}: HTTP {response.status_code} "
            f"{error.get('code', '')}: {error.get('message', response.text[:200])}"
        )
    return data


def observe_pin(server: str) -> str | None:
    """The pin of the key an https server presents (a typed code carries none: it is checked by the MACs);
    over plain http to loopback, the insecure loopback mode's fixed pin."""
    parts = urlsplit(server)
    if rules.is_loopback_url(server):
        return rules.INSECURE_LOOPBACK_PIN
    if parts.scheme != "https":
        return None
    try:
        pem = ssl.get_server_certificate((parts.hostname or "", parts.port or 443), timeout=10)
    except OSError as exc:
        raise ClientError(f"Cannot reach {server} ({exc.__class__.__name__}).") from None
    return rules.spki_pin_der(ssl.PEM_cert_to_DER_cert(pem))
