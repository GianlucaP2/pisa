# SPDX-License-Identifier: AGPL-3.0-only
"""Apple Push Notification service: token-based auth (an ES256 JWT from the vendor's .p8 key) over HTTP/2,
and the only payloads the relay ever builds. None of them holds anything of the user's but the opaque blob."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import jwt

# The push contract, the same as the Pisa server's (`core/pisa/domain/push_contract.py`, which
# `tests/test_push_contract.py` checks this copy against): tokens in hex, 64 to 200 digits for a device
# token and 64 to 512 for a Live Activity token; the APNs host per environment; the answers that mean a
# device token is gone (410 `Unregistered` or `ExpiredToken`, 400 `BadDeviceToken`; any other answer is a
# failure); a new provider token after 50 minutes (Apple refuses one older than an hour, and a refresh
# within 20 minutes).
DEVICE_TOKEN_HEX = (64, 200)
ACTIVITY_TOKEN_HEX = (64, 512)
HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
TOKEN_GONE = {410: ("Unregistered", "ExpiredToken"), 400: ("BadDeviceToken",)}
TOKEN_TTL_S = 3000
ALERTS = {"en": ("Pisa", "New notice"), "it": ("Pisa", "Nuovo avviso")}

_log = logging.getLogger("pisa_relay.apns")
for _name in (
    "httpx",
    "httpcore",
    "hpack",
    "h2",
):  # httpx logs each request's URL, which holds the device token
    logging.getLogger(_name).setLevel(logging.WARNING)


def alert_payload(sealed: str, language: str) -> dict[str, Any]:
    """A generic banner; the Notification Service Extension opens `sealed` and shows the real notice."""
    title, body = ALERTS.get(language, ALERTS["en"])
    return {"aps": {"alert": {"title": title, "body": body}, "mutable-content": 1}, "sealed": sealed}


def background_payload() -> dict[str, Any]:
    return {"aps": {"content-available": 1}}


def live_activity_payload(activity: dict[str, Any]) -> dict[str, Any]:
    """`activity` is already checked against the schema. The content-state is empty (APNs requires one; the
    widget keeps its last values until the app is opened): CONTRACT-2's `relay.liveActivityApsRelayMode`."""
    aps: dict[str, Any] = {"timestamp": activity["timestamp"], "event": activity["event"]}
    aps["content-state"] = {}
    for field, key in (("attributesType", "attributes-type"), ("dismissalDate", "dismissal-date")):
        if activity.get(field) is not None:
            aps[key] = activity[field]
    if activity["event"] == "start":  # push-to-start needs the attributes; the app's type has none (§9)
        aps |= {"attributes": {}, "input-push-token": 1}
    return {"aps": aps}


@dataclass(frozen=True)
class Delivery:
    outcome: Literal["sent", "gone", "failed"]
    reason: str = ""  # Apple's reason code, e.g. "BadDeviceToken"; never anything of the request


class Apns:
    def __init__(self, http: httpx.AsyncClient, key_pem: bytes, key_id: str, team_id: str, topic: str,
                 *, clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        self._http, self._key, self._key_id, self._team_id = http, key_pem, key_id, team_id
        self.topic = topic
        self._clock = clock
        self._token: tuple[str, float] | None = None

    def _bearer(self) -> str:
        now = self._clock()
        if self._token is None or now - self._token[1] >= TOKEN_TTL_S:
            token = jwt.encode({"iss": self._team_id, "iat": int(now)}, self._key, algorithm="ES256",
                               headers={"kid": self._key_id})  # fmt: skip
            self._token = (token, now)
        return self._token[0]

    async def send(
        self, environment: str, device_token: str, push_type: str, payload: dict[str, Any], *,
        priority: int, collapse_id: str | None = None,
    ) -> Delivery:  # fmt: skip
        topic = self.topic + ".push-type.liveactivity" if push_type == "liveactivity" else self.topic
        headers = {
            "authorization": f"bearer {self._bearer()}",
            "apns-topic": topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id
        url = f"{HOSTS[environment]}/3/device/{device_token}"
        try:
            response = await self._http.post(url, json=payload, headers=headers, timeout=15)
        except httpx.HTTPError as exc:
            _log.warning("apns unreachable: %s", type(exc).__name__)
            return Delivery("failed", type(exc).__name__)
        if response.status_code == 200:
            return Delivery("sent")
        try:
            reason = str(response.json().get("reason", ""))[:64]
        except ValueError:
            reason = ""
        if reason in TOKEN_GONE.get(response.status_code, ()):
            return Delivery("gone", reason)
        _log.warning("apns refused: %s %s", response.status_code, reason)
        return Delivery("failed", reason)
