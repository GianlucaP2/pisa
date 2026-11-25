"""Every error a client meets, and what it does about each. Live, it provokes the ones a client can cause.

    python errors.py [--state FILE]

Three places carry errors: a REST answer's envelope `{"error": {"code", "message", "action"}}`, a run's
`RUN_ERROR {code, message}`, and the socket's control frame `{"control": "error", "code", "message",
"retryable"}`, followed for some by the socket's close code.
"""

import asyncio
import json
import sys
from typing import Any

import websockets
from chat_live import hello
from pisa_example import PisaError, State, arguments, checked, http_client, ws_url
from session import Session

# [guide:actions]
# What the client does, by code. Show `message` to the user as Pisa's words; `action` says how to fix it.
WHAT_TO_DO = {
    # the device and its session
    "auth_failed": "sign a new session token; if that fails too, pair again",
    "session_expired": "sign a new session token and retry once",
    "device_revoked": "forget the key and the state file; pair again",
    "pairing_code_invalid": "the code was spent, refused or expired: ask for a new one",
    "last_device": "say so: this is the only device left",
    # what the user must decide first
    "ai_consent_required": "show the AI disclosure, ask, POST /v1/ai/consent, send the turn again",
    "biometric_required": "sign it with the device key (erase.md, approvals.md) and send it again",
    "voice_consent_missing": "ask for voice consent before enrolment",
    # the server's setup: tell the user what the owner must configure (the message names it)
    "model_not_configured": "no model is chosen for this feature: `pisa models set` on the server",
    "provider_key_missing": "a provider key is missing (`provider` names it): Settings or `pisa ai`",
    "model_incompatible": "the chosen model can't do this: pick another",
    "model_capability_missing": "the model lacks `capability`: pick another model for the slot",
    "connector_not_configured": "the owner must set this connector up first",
    "connector_not_connected": "connect the source first",
    "connector_off": "the source is switched off in Settings",
    "connector_auth_failed": "the source refused its credentials: reconnect it",
    "google_permission_missing": "reconnect Google with the missing permission",
    "place_provider_not_configured": "no place search provider is set up",
    "push_not_configured": "the server can't push yet: keep the socket open, or poll GET /v1/changes",
    "voice_cohort_missing": "the server's voice check isn't built yet: try later",
    "location_unknown": "send a location first (pisa.location)",
    # limits and transient failures: retry later (`retryable` true on the socket)
    "rate_limited": "wait and retry, slower",
    "budget_exceeded": "the AI spending cap is reached: say so; it resets on its own",
    "places_cap_reached": "the place-search cap is reached: say so",
    "provider_error": "the AI provider failed: retry later",
    "place_provider_error": "the place provider failed: retry later",
    "talker_unavailable": "talk in economy (voice_economy.py)",
    "talk_economy": "talk in economy (voice_economy.py)",
    "internal": "retry later; quote the trace id from the message when reporting it",
    # the request itself: a bug in the client
    "validation_failed": "fix the request (the message names the field)",
    "invalid_message": "fix the request (the message names the field)",
    "invalid_frame": "fix the frame (the message says what is wrong)",
    "contract_unsupported": "this server speaks another contract version: update the client",
    "not_found": "the thing is gone, or the path is wrong",
    # state that moved on: refresh and show the new state
    "plan_changed": "reload the plan and show it again",
    "check_in_answered": "already answered elsewhere: refresh",
    "options_expired": "the options are gone: ask again",
    "undo_expired": "too late to undo: say so",
    "replaced": "a newer connection of this device took over: do not reconnect this one",
    # an approval's thread (RUN_ERROR code)
    "approval_changed": "what was shown is no longer what would run: fetch the approval again",
    "approval_expired": "too late: drop the approval",
    "approval_decided": "decided elsewhere: drop it",
    "action_failed": "it ran and failed: show the message",
}
CLOSE_CODES = {  # the live socket's close codes
    1000: "closed normally",
    1002: "protocol error (a bad hello, contract_unsupported): fix the client, do not retry as is",
    1011: "the server failed (e.g. its database is unreachable): reconnect with backoff",
    4001: "replaced by a newer connection of this device: do not reconnect",
    4003: "auth: reason session_expired -> sign a new token and reconnect; otherwise (device_revoked, "
    "auth_failed) the last control error says which",
    4008: "no hello within 10 s: send hello first",
}
# [/guide:actions]


async def expect_rest(http: Any, method: str, path: str, status: int, code: str, **kwargs: Any) -> bool:
    try:
        checked(await http.request(method, path, **kwargs))
    except PisaError as err:
        print(f"{method} {path}: HTTP {err.status} {err.code} -> {WHAT_TO_DO.get(err.code)}")
        return (err.status, err.code) == (status, code)
    print(f"{method} {path}: no error")
    return False


async def expect_close(
    url: str, headers: dict[str, str], first: dict[str, Any] | None
) -> tuple[Any, int | None]:
    """The socket's last control frame and its close code."""
    async with websockets.connect(url, additional_headers=headers, subprotocols=["pisa.v2"]) as ws:
        if first is not None:
            await ws.send(json.dumps(first))
        control = None
        try:
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
                control = frame if frame.get("control") == "error" else control
        except websockets.ConnectionClosed as closed:
            return control, closed.rcvd.code if closed.rcvd else None


async def main() -> int:
    args = arguments(__doc__ or "").parse_args()
    state = State.load(args.state)
    ok: list[bool] = []
    async with http_client(state) as http:
        session = Session(http, state)
        auth = await session.bearer()
        ok.append(await expect_rest(http, "GET", "/v1/devices", 401, "auth_failed"))
        ok.append(await expect_rest(http, "POST", "/v1/ai/consent", 400, "validation_failed", headers=auth,
                                    json={"revision": "not hex", "health": False}))  # fmt: skip
        ok.append(await expect_rest(http, "POST", "/v1/ai/consent", 409, "ai_consent_required", headers=auth,
                                    json={"revision": "0" * 16, "health": False}))  # fmt: skip
        ok.append(await expect_rest(http, "GET", "/v1/no-such-thing", 404, "not_found", headers=auth))
        ok.append(await expect_rest(http, "POST", "/v1/data/erase", 403, "biometric_required", headers=auth,
                                    json={"family": "messages", "before": "2020-01-01"}))  # fmt: skip

        # [guide:control]
        url = ws_url(state.server)
        bad_hello = hello(state.device_id) | {"contract": {"versions": ["9.0"]}}
        for name, headers, first in (
            ("no token", {}, None),
            ("an unknown contract", auth, bad_hello),
            ("another device's id in the hello", auth, hello("00000000-0000-4000-8000-000000000000")),
        ):
            control, code = await expect_close(url, headers, first)
            print(
                f"/v2/live, {name}: control error {control and control['code']}, close {code} -> {CLOSE_CODES.get(code)}"
            )
            # [/guide:control]
            ok.append(code in (1002, 4003) and control is not None)
        async with websockets.connect(url, additional_headers=auth, subprotocols=["pisa.v2"]) as ws:
            await ws.send(json.dumps(hello(state.device_id)))
            await ws.recv()
            await ws.send(json.dumps({"op": "dance"}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
            print(
                f"/v2/live, an unknown op: control {frame.get('control')} {frame.get('code')}, the socket stays open"
            )
            ok.append(frame.get("code") == "invalid_frame")
    print(f"{sum(ok)}/{len(ok)} errors as documented")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
