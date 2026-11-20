"""Apple Push Notification service: token authentication with an ES256 JWT, HTTP/2.

- Hosts: api.sandbox.push.apple.com for `development` device tokens, api.push.apple.com for `production`.
- The JWT (kid = key id, iss = team id, iat) is cached and renewed every 50 minutes (Apple accepts 20 to 60).
- 410 `Unregistered`/`ExpiredToken` and 400 `BadDeviceToken` mean the token is dead: the caller drops it.
  These rules are the push contract's (`domain/push_contract`), which the relay keeps too.

Library: httpx with HTTP/2 (h2) + PyJWT (ES256 via cryptography). aioapns was considered: its last release is
from April 2025 and its connection pool can't take an injected transport, which the tests need.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import jwt

from pisa.domain.push_contract import APNS_ENVIRONMENTS, APNS_HOSTS, APNS_JWT_RENEW_S, apns_token_gone
from pisa.failures import external_failure
from pisa.ports.push import PushOutcome

# by the environment stored on the device (`development` is the wire's `sandbox`)
HOSTS = {APNS_ENVIRONMENTS[wire]: host for wire, host in APNS_HOSTS.items()}
JWT_RENEW_S = APNS_JWT_RENEW_S


@dataclass(frozen=True)
class ApnsKey:
    key_id: str
    team_id: str
    private_key_pem: str = field(repr=False)  # the .p8 content (secret)


class ApnsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key: ApnsKey,
        *,
        topic: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._key = key
        self._topic = topic
        self._clock = clock
        self._jwt: str | None = None
        self._jwt_at = 0.0

    def provider_token(self, *, renew: bool = False) -> str:
        now = self._clock()
        if renew or self._jwt is None or now - self._jwt_at >= JWT_RENEW_S:
            self._jwt = jwt.encode(
                {"iss": self._key.team_id, "iat": int(now)},
                self._key.private_key_pem,
                algorithm="ES256",
                headers={"kid": self._key.key_id},
            )
            self._jwt_at = now
        return self._jwt

    async def send(
        self,
        token: str,
        environment: str,
        *,
        title: str,
        body: str,
        thread_id: str,
        data: dict[str, object],
        push_id: str,
        category: str | None = None,
        interruption: str | None = None,
    ) -> PushOutcome:
        host = HOSTS.get(environment)
        if host is None:
            return PushOutcome("failed", f"Unknown APNs environment {environment!r}.")
        aps: dict[str, object] = {
            "alert": {"title": title, "body": body},
            "thread-id": thread_id,
            "sound": "default",
        }
        if category is not None:  # the app's notification category: its actions are the check-in choices
            aps["category"] = category
        if interruption is not None:  # "time-sensitive" for Pisa speaking first
            aps["interruption-level"] = interruption
        if interruption == "passive":  # shown without a sound (their "never" over a floor)
            aps.pop("sound")
        payload: dict[str, object] = {"aps": aps, **data}
        outcome = await self._post(host, token, payload, push_id, renew=False)
        if outcome.status == "failed" and outcome.message and "ExpiredProviderToken" in outcome.message:
            outcome = await self._post(host, token, payload, push_id, renew=True)
        return outcome

    async def send_live_activity(
        self, token: str, environment: str, *, aps: dict[str, object], priority: int, push_id: str
    ) -> PushOutcome:
        """A Live Activity push: the activity's update token (or the push-to-start token), the
        `liveactivity` push type on the app's `.push-type.liveactivity` topic."""
        host = HOSTS.get(environment)
        if host is None:
            return PushOutcome("failed", f"Unknown APNs environment {environment!r}.")
        payload: dict[str, object] = {"aps": aps}
        extra = {"apns-push-type": "liveactivity", "apns-topic": f"{self._topic}.push-type.liveactivity",
                 "apns-priority": str(priority)}  # fmt: skip
        outcome = await self._post(host, token, payload, push_id, renew=False, extra=extra)
        if outcome.status == "failed" and outcome.message and "ExpiredProviderToken" in outcome.message:
            outcome = await self._post(host, token, payload, push_id, renew=True, extra=extra)
        return outcome

    async def _post(
        self,
        host: str,
        token: str,
        payload: dict[str, object],
        push_id: str,
        *,
        renew: bool,
        extra: dict[str, str] | None = None,
    ) -> PushOutcome:
        headers = {
            "authorization": f"bearer {self.provider_token(renew=renew)}",
            "apns-topic": self._topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-id": push_id,
            "apns-collapse-id": push_id,  # a repeat of the same message replaces it on the phone
        } | (extra or {})
        try:
            response = await self._http.post(
                f"{host}/3/device/{token}", content=json.dumps(payload).encode(), headers=headers, timeout=15
            )
        except httpx.HTTPError as exc:
            external_failure("apns", "push", type(exc).__name__, exc)
            return PushOutcome("failed", f"APNs is unreachable ({exc.__class__.__name__}).")
        if response.status_code == 200:
            return PushOutcome("sent")
        try:
            reason = str(response.json().get("reason", ""))
        except ValueError:
            reason = ""
        if apns_token_gone(response.status_code, reason):
            return PushOutcome("token_invalid", f"APNs says the device token is no longer valid ({reason}).")
        detail = f": {reason}" if reason else ""
        external_failure("apns", "push", response.status_code, reason or response.text)
        return PushOutcome("failed", f"APNs refused the push (HTTP {response.status_code}{detail}).")
