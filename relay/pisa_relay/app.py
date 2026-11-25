# SPDX-License-Identifier: AGPL-3.0-only
"""The relay's HTTP API.

- `POST /v1/register` (the app): an APNs token, its environment and the subscription's signed transaction;
  answers a random `handle`, `secret` (for the user's server) and `app_secret` (the app keeps it). Only their
  SHA-256 is kept: they are the HMAC keys.
- `DELETE /v1/register` (the app, or the user's server): `{handle}`, signed with either secret; the handle
  ends.
- `POST /v1/register/activity` (the app): `{handle, token, kind}`, signed with the app secret: a Live Activity
  token (push-to-start, or an activity's update token) this handle may push to. No other token is reachable.
- `POST /v1/register/devices`, `POST /v1/register/devices/remove` (the app): the subscription's registrations,
  and removing one, signed with the app secret.
- `POST /v1/push` (the user's Pisa server): `relay/PROTOCOL.md`.
- `POST /v1/appstore/notifications`: App Store Server Notifications V2; a subscription's end ends its handles.
- `GET /healthz`; `GET /metrics` (counters by status, behind a bearer token).

Nothing of a request is logged or echoed: no bodies, tokens, handles or addresses. Errors carry a code only.
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

import httpx
from cryptography import x509
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pisa_relay import storekit
from pisa_relay.apns import (
    ACTIVITY_TOKEN_HEX,
    DEVICE_TOKEN_HEX,
    Apns,
    alert_payload,
    background_payload,
    live_activity_payload,
)
from pisa_relay.settings import Settings, b64url_decode
from pisa_relay.storage import (
    ENDED_KEEP_S,
    Device,
    Registration,
    Storage,
    StorageKeyError,
    TooManyDevices,
    hour,
)

REPLAY_WINDOW_S = 300
ACCEPTED_KEEP_S = 600
SWEEP_EVERY_S = 300
LIMITS = {"push": 8 * 1024, "register": 16 * 1024, "notification": 64 * 1024}
TIMESTAMP_HEADER = "X-Pisa-Relay-Timestamp"
SIGNATURE_HEADER = "X-Pisa-Relay-Signature"
_STAMP = re.compile(r"[0-9]{1,12}")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{43}")  # 32 bytes, base64url without padding
APP_CONTEXT = b"pisa-relay-app/1\n"  # what the app signs starts with this, then `<METHOD> <path>\n`
ACCOUNT_TOKEN_LABEL = b"pisa-relay/app-account-token\n"
B64URL_32 = r"^[A-Za-z0-9_-]{43}$"
DEVICE_TOKEN = rf"^[0-9a-fA-F]{{{DEVICE_TOKEN_HEX[0]},{DEVICE_TOKEN_HEX[1]}}}$"
ACTIVITY_TOKEN = rf"^[0-9a-fA-F]{{{ACTIVITY_TOKEN_HEX[0]},{ACTIVITY_TOKEN_HEX[1]}}}$"
_log = logging.getLogger("pisa_relay")

UnixTime = Annotated[int, Field(strict=True, ge=0, le=2**40)]


class Activity(BaseModel):
    """A Live Activity update in relay mode (CONTRACT-2 §9): a token, an event and the push's own
    times. Nothing of the user's day: no progress, no time of a stop, no content-state (APNs gets an empty
    one), and any other field is refused."""

    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=ACTIVITY_TOKEN)
    event: Literal["start", "update", "end"]
    priority: Literal[5, 10]
    timestamp: UnixTime
    attributesType: str | None = Field(None, max_length=64)
    dismissalDate: UnixTime | None = None

    @model_validator(mode="after")
    def _by_event(self) -> Self:
        if (self.attributesType is not None) != (self.event == "start"):
            raise ValueError("attributesType goes with start, and only with it")
        if self.dismissalDate is not None and self.event != "end":
            raise ValueError("dismissalDate goes with end only")
        return self


class PushBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=1, max_length=64)
    type: Literal["alert", "background", "liveactivity"]
    collapse_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    sealed: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]+$", max_length=2731)
    activity: Activity | None = None

    @model_validator(mode="after")
    def _by_type(self) -> Self:
        if (self.sealed is not None) != (self.type == "alert"):
            raise ValueError("sealed goes with alert, and only with it")
        if (self.activity is not None) != (self.type == "liveactivity"):
            raise ValueError("activity goes with liveactivity, and only with it")
        if self.sealed is not None and len(b64url_decode(self.sealed)) not in (512, 2048):
            raise ValueError("sealed is 512 or 2048 bytes")
        return self


class ForgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=1, max_length=64)


class ActivityTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=1, max_length=64)
    token: str = Field(pattern=ACTIVITY_TOKEN)
    kind: Literal["start", "update"]  # a push-to-start token, or a running activity's update token


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apns_token: str = Field(pattern=DEVICE_TOKEN)
    environment: Literal["production", "sandbox"]
    signed_transaction: str = Field(max_length=LIMITS["register"])
    signed_renewal_info: str | None = Field(None, max_length=LIMITS["register"])  # for Apple's billing grace
    language: str | None = Field(None, max_length=35)  # a BCP 47 tag; only en or it is kept
    account_key: str | None = Field(None, pattern=B64URL_32)  # proves the transaction's appAccountToken


class DeviceRemoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=1, max_length=64)
    device: str = Field(pattern=r"^[A-Za-z0-9_-]{16}$")


def may_remove(caller: Registration, target: Device, now: float, idle_days: int) -> bool:
    """CONTRACT-2 `relay.appApi.removalRule`: a signer that proved the account key may remove any
    registration of its subscription; any signer may remove itself, or one idle (no push, no registration) for
    `idle_days`. Nothing depends on which was registered first: a copy of the transaction can always be the
    older one."""
    idle = (target.last_used_at or target.created_at) <= hour(now - idle_days * 86_400)
    return caller.account_bound or target.handle == caller.handle or idle


def account_token(account_key: bytes) -> str:
    """The appAccountToken an app sets at purchase from its account key: the first 16 bytes of
    SHA-256(label ‖ key) as a version-8 UUID (RFC 9562), lower case."""
    raw = bytearray(hashlib.sha256(ACCOUNT_TOKEN_LABEL + account_key).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


class NotificationBody(BaseModel):
    signedPayload: str = Field(max_length=LIMITS["notification"])


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def short_hash(handle: str) -> str:
    """For an operator's eyes when one handle misbehaves: never the handle itself."""
    return hashlib.sha256(handle.encode()).hexdigest()[:8]


async def _read(request: Request, limit: int) -> bytes | None:
    declared = request.headers.get("content-length", "0")
    if not declared.isdigit() or int(declared) > limit:
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body


def verify_signature(
    key: bytes, stamp: str, given: str, body: bytes, now: float, context: bytes = b""
) -> bytes | None:
    """The signature's bytes when `given` is HMAC-SHA256(key, `<context><stamp>\\n<body>`) and `stamp` is in
    the window, else None. The server's requests (CONTRACT-2 §9) have no context; the app's start with
    `APP_CONTEXT` and the method and path, so an app request cannot be replayed to another route."""
    if not _STAMP.fullmatch(stamp) or not _SIGNATURE.fullmatch(given):
        return None
    expected = hmac.new(key, context + stamp.encode() + b"\n" + body, hashlib.sha256).digest()
    signature = b64url_decode(given)
    if not hmac.compare_digest(signature, expected) or abs(now - int(stamp)) > REPLAY_WINDOW_S:
        return None
    return signature


def app_context(method: str, path: str) -> bytes:
    return APP_CONTEXT + f"{method} {path}\n".encode()


def _signature(key: bytes, request: Request, body: bytes, now: float, context: bytes = b"") -> bytes | None:
    """Header names match case-insensitively, as HTTP requires (HTTP/2 sends them in lower case)."""
    stamp, given = request.headers.get(TIMESTAMP_HEADER, ""), request.headers.get(SIGNATURE_HEADER, "")
    return verify_signature(key, stamp, given, body, now, context)


def _is_json(request: Request) -> bool:
    """CONTRACT-2 §9: `Content-Type: application/json` (a charset parameter is allowed)."""
    return request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


def check_invariants(settings: Settings) -> None:
    """The relay refuses to start when its own windows do not fit together: an accepted signature must be kept
    for two replay windows (a request stamped at the window's far edge is still refused as a replay), and a
    refunded or revoked transaction must be remembered longer than a registration's transaction may be old
    (`transaction_max_age_s` below `ENDED_KEEP_S`), else it could register again once forgotten."""
    if ACCEPTED_KEEP_S < 2 * REPLAY_WINDOW_S:
        raise RuntimeError(
            f"ACCEPTED_KEEP_S ({ACCEPTED_KEEP_S} s) is shorter than two replay windows "
            f"({2 * REPLAY_WINDOW_S} s)"
        )
    if settings.transaction_max_age_s >= ENDED_KEEP_S:
        raise RuntimeError(
            f"RELAY_TRANSACTION_MAX_AGE_S ({settings.transaction_max_age_s} s) must be below how long an "
            f"ended transaction is remembered ({ENDED_KEEP_S} s), else a refunded transaction could register "
            "again"
        )


def create_app(
    settings: Settings, apns: Apns, *, root: x509.Certificate | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:  # fmt: skip
    check_invariants(settings)
    root = root or storekit.apple_root()
    storage = Storage(settings.database, settings.storage_key_bytes())
    counts: Counter[str] = Counter()

    def done(route: str, status: int, kind: str = "") -> Response:
        labels = f'type="{kind}",status="{status}"' if kind else f'status="{status}"'
        counts[f"{route}{{{labels}}}"] += 1
        return Response(status_code=status, headers={"cache-control": "no-store"})

    def refused(route: str, status: int, code: str) -> Response:
        counts[f'{route}{{status="{status}"}}'] += 1
        return Response(f'{{"error":"{code}"}}', status, media_type="application/json")

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        async def sweep() -> None:
            while True:  # one failed sweep never ends the sweeping (admission still checks expiry)
                try:
                    storage.sweep(clock(), settings.expiry_grace_s)
                except Exception:
                    _log.exception("the sweep failed; trying again in %d s", SWEEP_EVERY_S)
                await asyncio.sleep(SWEEP_EVERY_S)

        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        storage.close()

    app = FastAPI(title="Pisa push relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.storage = storage

    def subscription_args() -> dict[str, Any]:
        return {"root": root, "now": datetime.fromtimestamp(clock(), UTC), "bundle_id": settings.bundle_id,
                "products": settings.products(), "environments": settings.environments()}  # fmt: skip

    def admit(
        route: str, handle: str, request: Request, body: bytes, kind: str = "",
        signer: Literal["server", "app", "either"] = "server",
    ) -> Registration | Response | None:  # fmt: skip
        """The handle's registration when its request may go ahead; a refusal; or None when this exact request
        (the same signature) was already accepted: answered as accepted, never acted on twice. The rate limit
        comes before the replay set, so a flood past the limit adds nothing to it."""
        now = clock()
        try:
            registration = storage.get(handle)
        except StorageKeyError:
            _log.error("RELAY_STORAGE_KEY does not open a stored key: answering 500, never 'gone'")
            return done(route, 500, kind)
        if registration is None:
            return done(route, 410, kind)
        signature = None
        if signer in ("server", "either"):
            signature = _signature(registration.key, request, body, now)
        if signature is None and signer in ("app", "either") and registration.app_key is not None:
            context = app_context(request.method, request.url.path)
            signature = _signature(registration.app_key, request, body, now, context)
        if signature is None:
            return done(route, 401, kind)
        if registration.ends_at() + settings.expiry_grace_s < now:
            storage.delete(handle)
            return done(route, 410, kind)
        minute, day = int(now // 60), int(now // 86_400)
        if storage.count(handle, f"m{minute}", (minute + 2) * 60) > settings.push_per_minute:
            return done(route, 429, kind)
        if storage.count(handle, f"d{day}", (day + 2) * 86_400) > settings.push_per_day:
            return done(route, 429, kind)
        if not storage.accept_signature(signature, int(now) + ACCEPTED_KEEP_S):
            return None
        return registration

    @app.post("/v1/register")
    async def register(request: Request) -> Response:
        body = await _read(request, LIMITS["register"])
        if body is None:
            return refused("register", 413, "too_large")
        if not _is_json(request):
            return refused("register", 400, "malformed")
        now = clock()
        hour = int(now // 3600)
        args = subscription_args()
        # No limit across all subscriptions: anyone could hold it down, and full verification
        # costs about a millisecond. The only registration limit is per subscription, after verification; the
        # proxy limits per client (README).
        try:
            data = RegisterBody.model_validate_json(body)
            info = None
            if data.signed_renewal_info is not None:
                info = storekit.renewal(data.signed_renewal_info, root=args["root"], now=args["now"],
                                        environments=args["environments"])  # fmt: skip
            grace = info.grace_expires_at if info else None
            tx = storekit.transaction(data.signed_transaction, **args, grace_until=grace,
                                      max_age_s=settings.transaction_max_age_s)  # fmt: skip
            if info and (info.original_transaction_id, info.environment) != (
                tx.original_transaction_id, tx.environment
            ):  # fmt: skip
                raise storekit.StoreKitError("The renewal info is not for this transaction.")
        except ValidationError:
            return refused("register", 400, "malformed")
        except storekit.StoreKitError:
            return refused("register", 403, "subscription_not_verified")
        account_bound = False
        if tx.app_account_token is not None:  # the purchase is bound to the app's account key: prove it
            given = b64url_decode(data.account_key) if data.account_key else b""
            if not given or not hmac.compare_digest(account_token(given), tx.app_account_token):
                return refused("register", 403, "account_not_verified")
            account_bound = True
        ref = storage.subscription_ref(tx.environment, tx.original_transaction_id)
        if storage.ended(ref, tx.expires_at):
            return refused("register", 403, "subscription_not_verified")  # refunded or revoked since signed
        per_hour = storage.count(f"@register:{ref.hex()}", f"h{hour}", (hour + 2) * 3600)
        if per_hour > settings.register_per_subscription_hour:
            return refused("register", 429, "too_many_registrations")
        handle, secret, app_secret = (
            _b64url(secrets.token_bytes(16)),
            secrets.token_bytes(32),
            secrets.token_bytes(32),
        )
        language = "it" if (data.language or "").lower().split("-")[0] == "it" else "en"
        try:
            key = hashlib.sha256(secret).digest()
            storage.add(handle, data.apns_token.lower(), data.environment, key, ref, tx.environment,
                        tx.expires_at, grace, language, now, cap=settings.handles_per_subscription,
                        idle_s=settings.evict_idle_days * 86_400, app_key=hashlib.sha256(app_secret).digest(),
                        account_bound=account_bound)  # fmt: skip
        except TooManyDevices:
            return refused("register", 409, "too_many_devices")
        counts['register{status="201"}'] += 1
        answer = f'{{"handle":"{handle}","secret":"{_b64url(secret)}","app_secret":"{_b64url(app_secret)}"}}'
        return Response(answer, 201,
                        media_type="application/json", headers={"cache-control": "no-store"})  # fmt: skip

    @app.delete("/v1/register")
    async def forget(request: Request) -> Response:
        body = await _read(request, LIMITS["push"])
        if body is None:
            return done("forget", 413)
        if not _is_json(request):
            return done("forget", 400)
        try:
            handle = ForgetBody.model_validate_json(body).handle
        except ValidationError:
            return done("forget", 400)
        admitted = admit("forget", handle, request, body, signer="either")
        if isinstance(admitted, Response):
            return admitted
        storage.delete(handle)
        return done("forget", 204)

    @app.post("/v1/register/activity")
    async def activity_token(request: Request) -> Response:
        body = await _read(request, LIMITS["push"])
        if body is None:
            return done("activity_token", 413)
        if not _is_json(request):
            return done("activity_token", 400)
        try:
            data = ActivityTokenBody.model_validate_json(body)
        except ValidationError:
            return done("activity_token", 400)
        admitted = admit("activity_token", data.handle, request, body, signer="app")
        if isinstance(admitted, Response):
            return admitted
        if admitted is not None:
            storage.add_activity_token(data.handle, data.token, data.kind, clock())
        return done("activity_token", 204)

    @app.post("/v1/register/devices")
    async def devices(request: Request) -> Response:
        body = await _read(request, LIMITS["push"])
        if body is None:
            return done("devices", 413)
        if not _is_json(request):
            return done("devices", 400)
        try:
            handle = ForgetBody.model_validate_json(body).handle
        except ValidationError:
            return done("devices", 400)
        caller = admit("devices", handle, request, body, signer="app")
        if isinstance(caller, Response):
            return caller
        caller = caller or storage.get(handle)
        assert caller is not None
        listed = [{"id": d.id, "created": d.created_at[:10], "last_used": (d.last_used_at or "")[:10] or None,
                   "environment": d.environment, "this_device": d.handle == handle}
                  for d in storage.devices(caller.ref)]  # fmt: skip
        counts['devices{status="200"}'] += 1
        return Response(json.dumps({"devices": listed}), 200, media_type="application/json",
                        headers={"cache-control": "no-store"})  # fmt: skip

    @app.post("/v1/register/devices/remove")
    async def remove_device(request: Request) -> Response:
        body = await _read(request, LIMITS["push"])
        if body is None:
            return done("remove_device", 413)
        if not _is_json(request):
            return done("remove_device", 400)
        try:
            data = DeviceRemoveBody.model_validate_json(body)
        except ValidationError:
            return done("remove_device", 400)
        caller = admit("remove_device", data.handle, request, body, signer="app")
        if isinstance(caller, Response):
            return caller
        if caller is None:
            return done("remove_device", 204)  # the same request again: done once
        target = next((d for d in storage.devices(caller.ref) if d.id == data.device), None)
        if target is None:
            return refused("remove_device", 404, "unknown_device")
        if not may_remove(caller, target, clock(), settings.evict_idle_days):
            return refused("remove_device", 403, "not_allowed")
        storage.delete(target.handle)
        return done("remove_device", 204)

    @app.post("/v1/push")
    async def push(request: Request) -> Response:
        body = await _read(request, LIMITS["push"])
        if body is None:
            return done("push", 413)
        if not _is_json(request):
            return done("push", 400)
        try:
            data = PushBody.model_validate_json(body)
        except ValidationError:
            return done("push", 400)
        kind, activity = data.type, data.activity
        if activity and activity.attributesType not in (None, settings.live_activity_attributes_type):
            return done("push", 400, kind)
        registration = admit("push", data.handle, request, body, kind)
        if isinstance(registration, Response):
            return registration
        if registration is None:
            return done("push", 202, kind)  # the same request again: accepted once, sent once
        token = None
        if activity is not None:
            token_kind = "start" if activity.event == "start" else "update"
            if not storage.has_activity_token(data.handle, activity.token, token_kind):
                return done("push", 401, kind)  # not this handle's token: as unknown as a bad signature
            token = activity.token
        storage.used(data.handle, clock())
        if data.type == "alert":
            assert data.sealed is not None
            payload, priority = alert_payload(data.sealed, registration.language), 10
        elif activity is not None:
            payload, priority = live_activity_payload(activity.model_dump()), activity.priority
        else:
            payload, priority = background_payload(), 5
        delivery = await apns.send(registration.environment, token or registration.apns_token, kind, payload,
                                   priority=priority, collapse_id=data.collapse_id)  # fmt: skip
        if delivery.outcome == "sent":
            return done("push", 202, kind)
        if delivery.outcome == "gone" and token is not None:
            storage.drop_activity_token(data.handle, token)
            return done("push", 422, kind)  # this handle's Live Activity token is gone; the handle is not
        if delivery.outcome == "gone":
            storage.delete(data.handle)
            _log.info("handle %s ended: APNs says %s", short_hash(data.handle), delivery.reason)
            return done("push", 410, kind)
        return done("push", 502, kind)

    @app.post("/v1/appstore/notifications")
    async def appstore(request: Request) -> Response:
        body = await _read(request, LIMITS["notification"])
        if body is None:
            return done("appstore", 413)
        try:
            signed = NotificationBody.model_validate_json(body).signedPayload
            notice = storekit.notification(signed, **subscription_args())
        except (ValidationError, storekit.StoreKitError):
            return done("appstore", 400)
        kind, tx = notice.kind, notice.transaction
        if tx is not None:
            ref = storage.subscription_ref(tx.environment, tx.original_transaction_id)
            if kind in {"EXPIRED", "GRACE_PERIOD_EXPIRED", "REFUND", "REVOKE"}:
                storage.end_subscription(ref, tx.expires_at, remember=kind in {"REFUND", "REVOKE"})
            elif kind in {"SUBSCRIBED", "DID_RENEW", "DID_RECOVER", "OFFER_REDEEMED", "RENEWAL_EXTENDED"}:
                storage.renew(ref, tx.expires_at)
            elif kind == "DID_FAIL_TO_RENEW" and notice.renewal and notice.renewal.grace_expires_at:
                storage.grace(ref, notice.renewal.grace_expires_at)  # Apple's billing grace period
        counts[f'appstore{{type="{kind if re.fullmatch(r"[A-Z_]{1,40}", kind) else "other"}"}}'] += 1
        return done("appstore", 200)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        token = settings.metrics_token
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token is None or not hmac.compare_digest(given.encode(), token.get_secret_value().encode()):
            return Response(status_code=404)
        lines = [f"pisa_relay_{key} {value}" for key, value in sorted(counts.items())]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return app


def app_from_env() -> FastAPI:
    """The app as `uvicorn --factory pisa_relay.app:app_from_env` builds it: settings from RELAY_*."""
    settings = Settings()  # pyright: ignore[reportCallIssue]
    if not (settings.apns_key_path and settings.apns_key_id and settings.apns_team_id):
        raise SystemExit("Set RELAY_APNS_KEY_PATH, RELAY_APNS_KEY_ID and RELAY_APNS_TEAM_ID.")
    http = httpx.AsyncClient(http2=True)
    apns = Apns(http, settings.apns_key_path.read_bytes(), settings.apns_key_id, settings.apns_team_id,
                settings.bundle_id)  # fmt: skip
    return create_app(settings, apns)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # no access log: it would write the caller's address and the path of every request
    uvicorn.run(app_from_env(), host="0.0.0.0", port=8080, access_log=False, proxy_headers=False)  # noqa: S104
