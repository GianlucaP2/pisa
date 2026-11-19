"""Session tokens: the device key signs a fresh nonce for a 1-hour bearer token.

    python session.py [--state FILE] [--print-token]

It gets a token, uses it, refreshes it, and shows the answers a client must handle: no token, a token that is not
the server's, and a signature that does not hold (all 401 `auth_failed`).
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pisa_example import PisaError, State, arguments, b64url, checked, http_client


# [guide:session]
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


# [/guide:session]


# [guide:refresh]
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


# [/guide:refresh]


async def main() -> int:
    parser = arguments(__doc__ or "")
    parser.add_argument("--print-token", action="store_true", help="print one token and exit (for curl)")
    args = parser.parse_args()
    state = State.load(args.state)
    async with http_client(state) as http:
        if args.print_token:
            print((await new_token(http, state))[0])
            return 0
        session = Session(http, state)
        devices = await session.request("GET", "/v1/devices")
        for device in devices["devices"]:
            mark = "  <- this client" if device["this_device"] else ""
            print(f"device {device['name']} ({device['platform']}) revoked={device['revoked_at']}{mark}")
        print(f"token valid until {session.expires.isoformat()}")

        # What a client must expect, and what it does (all 401 auth_failed: get a token, or pair again).
        failures = 0
        for name, headers in (
            ("no token", {}),
            ("a token that is not the server's", {"Authorization": "Bearer not-a-token"}),
        ):
            answer = await http.get("/v1/devices", headers=headers)
            code = answer.json()["error"]["code"]
            print(f"{name}: HTTP {answer.status_code} {code}")
            failures += (answer.status_code, code) != (401, "auth_failed")
        nonce = checked(await http.get("/v1/devices/session/nonce", params={"deviceId": state.device_id}))
        forged = b64url(state.key.sign(b"something else", ec.ECDSA(hashes.SHA256())))
        answer = await http.post(
            "/v1/devices/session",
            json={"deviceId": state.device_id, "nonce": nonce["nonce"], "signature": forged},
        )
        print(f"a signature over the wrong text: HTTP {answer.status_code} {answer.json()['error']['code']}")
        failures += answer.status_code != 401
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
