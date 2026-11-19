"""The push relay's sending API, `POST <PISA_PUSH_RELAY_URL>/v1/push` (`relay/PROTOCOL.md`).

The body is `{handle, type, collapse_id?, sealed?, activity?}`, signed with HMAC-SHA256 under the device's
relay secret, with a timestamp the relay checks against a 5-minute window (`domain.push_seal`). The relay
answers 202 when it handed the push to Apple, 410 when it no longer knows the handle (the app unregistered, or
its subscription ended: the server drops the registration), 422 to a `liveactivity` push when the activity's
token is dead (only that token is dropped; the registration stays), 401 for a bad signature or timestamp, and
429 past its rate limit.

`DELETE <PISA_PUSH_RELAY_URL>/v1/register` with `{handle}`, signed the same way, makes the relay forget a
registration: the server sends it when the device is unpaired or revoked, or leaves relay mode (204; 410 when
it was gone already).

What each status means lives once, in `push_seal.RELAY_ANSWERS`. Any other answer, a 404 included (a mistyped
relay URL, a proxy), is a failure: logged, and the registration stays.
"""

import json
import time
from collections.abc import Callable

import httpx

from pisa.domain import push_seal
from pisa.failures import external_failure
from pisa.ports.push import PushOutcome

PUSH_PATH = "/v1/push"
REGISTER_PATH = "/v1/register"


class RelayClient:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + PUSH_PATH
        self._register_url = base_url.rstrip("/") + REGISTER_PATH
        self._clock = clock

    async def forget(self, secret: bytes, handle: str) -> PushOutcome:
        """`DELETE /v1/register`: the relay drops the handle, its APNs token and its key. `sent` when it did
        (204) or had already (410); any other answer is `failed`, logged."""
        body = json.dumps({"handle": handle}, separators=(",", ":")).encode()
        headers = push_seal.relay_headers(secret, int(self._clock()), body)
        try:
            response = await self._http.request(
                "DELETE", self._register_url, content=body, headers=headers, timeout=15
            )
        except httpx.HTTPError as exc:
            external_failure("push_relay", "forget", type(exc).__name__, exc)
            return PushOutcome("failed", f"The push relay is unreachable ({exc.__class__.__name__}).")
        if push_seal.relay_meaning("forget", response.status_code) is not None:  # forgotten, or gone already
            return PushOutcome("sent")
        external_failure("push_relay", "forget", response.status_code, response.text[:200])
        return PushOutcome(
            "failed", f"The push relay refused to forget the handle (HTTP {response.status_code})."
        )

    async def send(self, secret: bytes, body: bytes, kind: str = "alert") -> PushOutcome:
        headers = push_seal.relay_headers(secret, int(self._clock()), body)
        try:
            response = await self._http.post(self._url, content=body, headers=headers, timeout=15)
        except httpx.HTTPError as exc:
            external_failure("push_relay", "push", type(exc).__name__, exc)
            return PushOutcome("failed", f"The push relay is unreachable ({exc.__class__.__name__}).")
        meaning = push_seal.relay_meaning("push", response.status_code)
        if meaning == "done":
            return PushOutcome("sent")
        if meaning == "activity_gone" and kind == "liveactivity":
            return PushOutcome("activity_invalid", "APNs says the Live Activity's token is no longer valid.")
        if meaning == "gone":
            return PushOutcome("token_invalid", "The push relay no longer knows this device's handle.")
        external_failure("push_relay", "push", response.status_code, response.text[:200])
        return PushOutcome("failed", f"The push relay refused the push (HTTP {response.status_code}).")
