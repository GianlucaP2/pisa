# Errors

Errors reach a client in three shapes:

| Where | Shape |
|---|---|
| a REST answer (status 400 and up) | `{"error": {"code", "message", "action", ...}}`: a stable `code`, what happened, and what fixes it. Some add fields: `provider` (`provider_key_missing`), `economy_reason` (`talk_economy`), `capability` and `model` (`model_capability_missing`) |
| a run | `RUN_ERROR {"code", "message"}` as the run's last event |
| the live socket or a talk's channel | a control frame `{"control": "error", "seq", "code", "message", "retryable"}`; for some, the socket then closes |

Show `message` to the user as it is (it is written for them, in plain words). Act on `code`.

Example: [`errors.py`](examples/errors.py) provokes the ones a client can cause, against a live server.

<!-- include: examples/pisa_example.py#error-envelope -->
```python
class PisaError(Exception):
    """Pisa's error envelope: `{"error": {"code", "message", "action"}}`, with the HTTP status."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        error = body.get("error") or {}
        self.status = status
        self.code = str(error.get("code") or f"http_{status}")
        self.message = str(error.get("message") or "")
        self.action = str(error.get("action") or "")
        self.extra = {k: v for k, v in error.items() if k not in ("code", "message", "action")}
        super().__init__(f"HTTP {status} {self.code}: {self.message}")


def checked(response: httpx.Response) -> Any:
    """The JSON body of a 2xx answer (None for 204); PisaError for anything else."""
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise PisaError(response.status_code, body if isinstance(body, dict) else {})
    return None if response.status_code == 204 or not response.content else response.json()
```

## Every code

| Code | HTTP | Also as | What the client does |
|---|---|---|---|
| `auth_failed` | 401 | control error, close 4003 | sign a new session token; if that fails too, pair again |
| `session_expired` | 401 | close 4003, reason `session_expired` | sign a new token, retry once / reconnect with `resume` |
| `device_revoked` | 401 | control error, close 4003 | forget the key and the state; pair again |
| `pairing_code_invalid` | 400 (409 on an offer's confirm) | | the code was spent, refused or expired: ask for a new one |
| `last_device` | 409 | | say this is the only device left |
| `ai_consent_required` | 409 | `RUN_ERROR`, control error | show the disclosure, ask, `POST /v1/ai/consent`, send the turn again ([Text chat](text-chat.md#consent-first)) |
| `biometric_required` | 403 (401 on an approval's resume) | `RUN_ERROR` on an approval's thread | a signed operation ([Approvals](approvals.md#signing-a-yes)) |
| `model_not_configured` | 409 | `RUN_ERROR` | no model is chosen for the feature: tell the user the server's owner must choose one |
| `provider_key_missing` | 409 | `RUN_ERROR` | a provider key is missing (`provider` names it) |
| `model_incompatible` | 400 | | the chosen model can't do this |
| `model_capability_missing` | 409 | | the model lacks `capability`: another model for the slot |
| `provider_error` | 502 | `RUN_ERROR` | the AI provider failed: retry later |
| `budget_exceeded` | 402 | `RUN_ERROR` | the AI spending cap is reached: say so |
| `rate_limited` | 429 | `RUN_ERROR`, control error | slow down, retry later |
| `internal` | 500 | `RUN_ERROR`, control error (close 1011 when the database is down) | retry later; the message carries a trace id to report |
| `validation_failed` | 400 | | fix the request: the message names the field |
| `invalid_message` | 400 | `RUN_ERROR` | the same, for a run's content (a choice no longer open...) |
| `invalid_frame` | | `RUN_ERROR`, control error (the socket stays open) | fix the frame: the message says what is wrong |
| `contract_unsupported` | | control error, close 1002 | this server speaks another contract version |
| `replaced` | | control error, close 4001 | a newer connection of this device took over: don't reconnect this one |
| `not_found` | 404 | | gone, or a wrong path |
| `approval_changed` | 409 | `RUN_ERROR` | fetch the approval again and show it again |
| `approval_expired` | 410 on resume (409 elsewhere) | `RUN_ERROR` | drop the approval |
| `approval_decided` | 409 | `RUN_ERROR` | decided elsewhere: drop it |
| `action_failed` | | `RUN_ERROR` | the approved action ran and failed: show why |
| `talk_economy` | 409 | | talk in economy ([Audio](audio.md#opening-the-talk)); `economy_reason` says why |
| `talker_unavailable` | 502 | | talk in economy |
| `voice_consent_missing`, `voice_cohort_missing` | 409 | | ask for voice consent first / try enrolment later |
| `connector_not_configured`, `connector_not_connected`, `connector_off`, `connector_auth_failed` (502), `google_permission_missing` | 409 | | a source isn't set up, connected or allowed: say which |
| `place_provider_not_configured`, `places_cap_reached` (402), `place_provider_error` (502), `location_unknown` | 409 | | places can't be searched now: say why (`location_unknown`: send a location) |
| `push_not_configured` | 409 | | no push: keep the socket open, or poll |
| `plan_changed`, `check_in_answered`, `undo_expired` | 409 | | the state moved on: refresh |
| `options_expired` | 404 | | the options are gone: ask again |

A run that fails on a REST-only code reports it over the socket as `internal`. Codes may be added: treat an
unknown one by its HTTP status (4xx: show the message; 5xx: retry later).

<!-- include: examples/errors.py#actions -->
```python
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
```

## The live socket's close codes

| Close | When |
|---|---|
| 1000 | a normal close |
| 1002 | the first frame was not a valid `hello`, or no common contract (`contract_unsupported`) |
| 1011 | the server failed (its database is unreachable) |
| 4001 | `replaced` by a newer connection of this device |
| 4003 | no or bad token, a `hello` naming another device (`auth_failed`), `device_revoked`, or the token's hour is over (reason `session_expired`, with no control frame) |
| 4008 | no `hello` within 10 seconds |

<!-- include: examples/errors.py#control -->
```python
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
```

```text
$ python errors.py
GET /v1/devices: HTTP 401 auth_failed -> sign a new session token; if that fails too, pair again
POST /v1/ai/consent: HTTP 400 validation_failed -> fix the request (the message names the field)
POST /v1/ai/consent: HTTP 409 ai_consent_required -> show the AI disclosure, ask, POST /v1/ai/consent, send the turn again
GET /v1/no-such-thing: HTTP 404 not_found -> the thing is gone, or the path is wrong
POST /v1/data/erase: HTTP 403 biometric_required -> this needs a signed operation (approvals.md); ...
/v2/live, no token: control error auth_failed, close 4003 -> ...
/v2/live, an unknown contract: control error contract_unsupported, close 1002 -> ...
/v2/live, another device's id in the hello: control error auth_failed, close 4003 -> ...
/v2/live, an unknown op: control error invalid_frame, the socket stays open
9/9 errors as documented
```
