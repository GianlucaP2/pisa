"""Generates protocol/v2/openapi-v2-delta.json: the REST endpoints contract 2 adds or changes (CONTRACT-2 §10).

OpenAPI 3.1. Bodies that the contract schema defines are $refs into it, so the two cannot drift. Everything else in
protocol/openapi.json (v1) keeps its path and shape. Where the specification names an endpoint but not every field
of its body, the shape here is marked "x-pisa-shape": "proposed" (to settle in packages B and C). Apache-2.0.
"""
from __future__ import annotations

from typing import Any

SCHEMA_FILE = "schema/pisa-contract-2.schema.json"
AGUI = "vendor/ag-ui-1.0/schema.json"


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"{SCHEMA_FILE}#/$defs/{name}"}


def body(schema: dict[str, Any], media: str = "application/json") -> dict[str, Any]:
    return {"required": True, "content": {media: {"schema": schema}}}


def ok(schema: dict[str, Any] | None, desc: str, code: str = "200", media: str = "application/json") -> dict[str, Any]:
    r: dict[str, Any] = {"description": desc}
    if schema is not None:
        r["content"] = {media: {"schema": schema}}
    return {code: r}


def obj(props: dict[str, Any], req: list[str], **extra: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False, **extra}


STR = {"type": "string"}
TS = {"type": "string", "format": "date-time"}
PROPOSED = {"x-pisa-shape": "proposed"}
ERR = {"$ref": "#/components/responses/Error"}
AUTH = [{"sessionToken": []}]
COLLECTED = obj({"events": {"type": "array", "items": {"$ref": f"{AGUI}"}}}, ["events"],
                description="A run collected into one document (JSON mode of the AG-UI HTTP binding): plain AG-UI 1.0 events.")
ACCEPTED = obj({"runId": STR}, ["runId"], description="The run goes on past 8 s: its events reach the device on /v2/live, and a headless "
               "client reads them from GET /v1/changes.")


def op(summary: str, tag: str, responses: dict[str, Any], *, op_id: str, auth: bool = True, request: dict[str, Any] | None = None,
       status: str, desc: str | None = None, params: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    o: dict[str, Any] = {"operationId": op_id, "summary": summary, "tags": [tag], "x-pisa-status": status}
    if desc:
        o["description"] = desc
    if params:
        o["parameters"] = params
    if request is not None:
        o["requestBody"] = request
    o["responses"] = {**responses, "default": ERR}
    o["security"] = AUTH if auth else []
    o.update(extra)
    return o


# ---------- signed device ops (v1 CORE-SEC, prefix pisa-device in contract 2) ----------
# The `device` challenge's op values. v1's list, without coder_secret (Coder is out of PISA public, D-126.3) and
# pairing_code (6-character code pairing is gone under D-122; QR/typed pairing v2 and pairing offers replace it), plus
# model_server (architect 1, 18 Nov: a model server's address decides where the user's words go), plus paid_ack
# (v0.1.4's Connections: the user's OK of a paid maps or web provider and its monthly cap, target
# '<provider>:<cap in cents>'), plus conversation (v0.1.4: a device deletes its own conversation, target its thread
# 'conversation:<deviceId>').
DEVICE_OPS = ["revoke_device", "provider_key", "google_connect", "google_disconnect", "place_key",
              "google_credentials", "messages_retention", "erase", "connector", "connector_credential", "connector_consent",
              "model_server", "pairing_offer", "pairing_confirm", "paid_ack",
              "conversation"]
PAIRING_OPS = ["pairing_offer", "pairing_confirm"]  # pairing a new device from an already-paired phone (§4.3.1)
CAPS = ["tools", "json_schema", "json_object", "tool_choice", "vision", "audio", "embeddings"]
CREF = lambda name: {"$ref": f"#/components/schemas/{name}"}  # noqa: E731
SIGNED_HEADERS = [{"$ref": f"#/components/parameters/{h}"} for h in ("XPisaNonce", "XPisaSignature", "XPisaKeyId")]
NULLABLE_STR = lambda mx, mn=1: {"type": ["string", "null"], "minLength": mn, "maxLength": mx}  # noqa: E731
MODEL_PARAM = {"name": "model", "in": "query", "required": False, "schema": {"type": "string", "minLength": 1, "maxLength": 200},
               "description": "The model the answer is about; without it, the provider's default model."}


def signed(op_name: str, target: str) -> dict[str, Any]:
    """The marks of a signed device op: what to sign, and the refusals that go with it."""
    return {"x-pisa-signed-op": {"op": op_name, "target": target},
            "x-pisa-signing-rule": "As provider_key: once the server holds a Face ID key, a request without a valid signature for "
                                   "this op and target (or a nonce used twice) gets 403 biometric_required. Before the first key "
                                   "exists, the session token alone is enough."}


def path_param(name: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": STR}


def build(schema: dict[str, Any]) -> dict[str, Any]:
    assert {"ServerDiscovery", "PairRequest", "PairResponse", "SessionRequest", "AIDisclosure", "ApprovalResume",
            "A2UISurfaceActivity", "s_pisa_endpoints"} <= set(schema["$defs"])
    paths: dict[str, Any] = {
        "/v1/server": {"get": op("Discovery: what this server is and offers (no auth, nothing personal)", "discovery",
                                 ok(ref("ServerDiscovery"), "The server's discovery document."), op_id="getServer", auth=False,
                                 status="new", desc="CONTRACT-2 §4.1. Example: examples/server.json.")},
        "/v1/server/endpoints": {"get": op("The server's addresses and SPKI pins (current and next)", "discovery",
                                           ok(ref("s_pisa_endpoints"), "The same value as the pisa.endpoints event."),
                                           op_id="getServerEndpoints", status="new", desc="CONTRACT-2 §3.4.")},
        "/v2/live": {"get": op("The live socket (WebSocket upgrade, subprotocol pisa.v2)", "live",
                               {"101": {"description": "Switching protocols. The frames are described in asyncapi-live.json."}},
                               op_id="openLive", status="new", desc="Replaces /v1/device. CONTRACT-2 §3.1.")},
        "/v2/runs": {"post": op("Run the agent (AG-UI's HTTP binding)", "live",
                                {"200": {"description": "text/event-stream: SSE data: lines of plain AG-UI 1.0 events. "
                                                        "application/json: the run collected, within 8 s.",
                                         "content": {"text/event-stream": {"schema": STR},
                                                     "application/json": {"schema": COLLECTED}}},
                                 "202": {"description": "JSON mode only: the run is still going after 8 s.",
                                         "content": {"application/json": {"schema": ACCEPTED}}}},
                                op_id="runAgent", status="new", request=body({"$ref": f"{AGUI}#RunAgentInput"}),
                                desc="Replaces POST /v1/turns. forwardedProps.pisa follows TurnProps; an A2UI action rides "
                                     "forwardedProps.pisa.a2uiAction. CONTRACT-2 §3.2.")},
        "/v1/approvals/{id}/resume": {"post": op("Decide an approval outside the socket (notification actions, the watch)", "approvals",
                                                 {**ok(COLLECTED, "The resuming run, collected: pisa.action_done, or RUN_ERROR with "
                                                       "approval_changed, approval_expired, approval_decided, biometric_required or action_failed (approved, ran, failed)."),
                                                  "202": {"description": "Still running after 8 s.", "content": {"application/json": {"schema": ACCEPTED}}}},
                                                 op_id="resumeApproval", status="new", params=[path_param("id")],
                                                 request=body(ref("ApprovalResume")),
                                                 desc="Replaces POST /v1/approvals/{id} for decisions. The body is ApprovalResume in its camelCase "
                                                      "(keyId), as in a socket resume. A cancelled answer is not sent here: cancelling "
                                                      "decides nothing, and the approval stays pending until decided elsewhere or "
                                                      "expired. Header X-Pisa-Origin: notification when sent from a notification "
                                                      "action. CONTRACT-2 §6.1.", **PROPOSED)},
        "/v1/approvals/{id}": {"get": op("An approval, with what is hashed", "approvals",
                                         ok({"type": "object", "required": ["payload", "recipients"],
                                             "properties": {"payload": {"type": "object", "description": "The action exactly as the server will run it."},
                                                            "recipients": {"type": "array", "items": STR, "description": "Exactly as hashed."}},
                                             "description": "v1's approval object plus these two fields, so the phone can recompute the digest (§6.3)."},
                                            "The approval."), op_id="getApproval", status="changed", params=[path_param("id")])},
        "/v1/phone/challenge": {"post": op("A single-use nonce, and the canonical text the phone signs after Face ID", "approvals",
                                           {**ok(CREF("Challenge"), "The challenge (5 minutes)."),
                                            "501": {"description": "unsupported: this server does not offer that device op (allowed for "
                                                                   "model_server). Nothing was issued.", "content": {"application/json": {"schema": {
                                                                       "$ref": "../openapi.json#/components/schemas/ErrorResponse"}}}}},
                                           op_id="phoneChallenge", status="changed",
                                           request=body({"oneOf": [CREF("ApprovalChallengeRequest"), CREF("DeviceChallengeRequest")]}),
                                           desc="The approval challenge gains interrupt_id and returns the pisa-approval/2 canonical text. "
                                                "The device challenge's canonical becomes pisa-device: 'pisa-device\\nop=<op>\\ntarget=<target>"
                                                "\\nnonce=<nonce>\\nexpires_at=<iso>' (v1's j-device with the prefix renamed), and its op "
                                                "enum gains model_server. The signed request carries X-Pisa-Nonce, X-Pisa-Signature (DER, "
                                                "base64) and X-Pisa-Key-Id.")},
        "/v1/ai/providers": {"post": op("Add an OpenAI-compatible model server", "ai",
                                        {**ok(CREF("Provider"), "The server as stored.", code="201"), "403": {"description": "biometric_required"},
                                         "501": {"description": "unsupported: this server does not let the user add model servers."}},
                                        op_id="createModelServer", status="new", params=SIGNED_HEADERS, request=body(CREF("ServerCreate")),
                                        desc="Signed (device op model_server, target '<id>:<SHA-256 hex of the body as sent>', id from "
                                             "the body): its address is where the user's words go. The key, when one is needed, is set "
                                             "afterwards with PUT .../key (provider_key).",
                                        **signed("model_server", "<id>:<SHA-256 hex of the request body, as sent>"))},
        "/v1/ai/providers/{id}": {
            "patch": op("Change a model server the user added", "ai",
                        {**ok(CREF("Provider"), "The server as stored."), "403": {"description": "biometric_required, or not editable "
                                                                                                   "(a server the operator set)"},
                         "501": {"description": "unsupported"}},
                        op_id="updateModelServer", status="new", params=[path_param("id"), *SIGNED_HEADERS], request=body(CREF("ServerUpdate")),
                        desc="Signed as its creation (device op model_server, target '<id>:<SHA-256 hex of the body as sent>', id "
                             "from the path). A new address forgets the old probes.",
                        **signed("model_server", "<id>:<SHA-256 hex of the request body, as sent>")),
            "delete": op("Remove a model server the user added, with its key, probes and models", "ai",
                         {"204": {"description": "Removed."}, "501": {"description": "unsupported"}},
                         op_id="deleteModelServer", status="new", params=[path_param("id")],
                         desc="Unsigned: removing a destination sends nothing anywhere."),
        },
        "/v1/ai/providers/{id}/probe": {"post": op("Probe a model: one small real request per capability", "ai",
            {**ok(CREF("ProviderCapabilities"), "Answers as GET .../capabilities."), "501": {"description": "unsupported"}},
            op_id="probeModel", status="new", params=[path_param("id"), MODEL_PARAM], desc="Unsigned. May take a minute or more on a CPU server.")},
        "/v1/ai/providers/{id}/capabilities/overrides": {"put": op("Set capabilities by hand over the probe's", "ai",
            {**ok(CREF("ProviderCapabilities"), "Answers as GET .../capabilities."), "501": {"description": "unsupported"}},
            op_id="setCapabilityOverrides", status="new", params=[path_param("id"), MODEL_PARAM], request=body(CREF("CapabilityOverrides")),
            desc="Unsigned. Replaces the model's overrides: each capability sent is set by hand; one left out follows the probe again.")},
        "/v1/ai/providers/{id}/capabilities": {"get": op("What a model can do (the guide's model step)", "ai",
            ok(CREF("ProviderCapabilities"), "The effective capabilities: the probe's result with the user's overrides applied."),
            op_id="getProviderCapabilities", status="new", params=[path_param("id"), MODEL_PARAM])},
        "/v1/ai/disclosure": {"get": op("Which AI providers and models get which data", "ai", ok(ref("AIDisclosure"), "The disclosure."),
                                        op_id="getAiDisclosure", status="new", desc="CONTRACT-2 §4.4.")},
        "/v1/ai/consent": {"post": op("Record the user's AI consent for REST-started work", "ai",
                                      {"204": {"description": "Recorded."}}, op_id="postAiConsent", status="new",
                                      request=body(obj({"revision": {"type": "string", "pattern": "^[0-9a-f]{16,64}$"}, "health": {"type": "boolean"}},
                                                       ["revision", "health"])))},
        "/v1/devices/me/push": {"put": op("How notices reach this device: the relay, direct APNs, or none", "devices",
                                          {"204": {"description": "Saved."}}, op_id="putDevicePush", status="changed",
                                          request=body({"oneOf": [
                                              obj({"mode": {"const": "relay"}, "handle": STR, "secret": STR, "public_key": STR},
                                                  ["mode", "handle", "secret", "public_key"]),
                                              obj({"mode": {"const": "direct"}, "token": STR, "environment": {"type": "string", "enum": ["production", "sandbox"]}},
                                                  ["mode", "token"]),
                                              obj({"mode": {"const": "none"}}, ["mode"])]}),
                                          desc="Relay mode added (CONTRACT-2 §9). The server deletes a relay registration when the relay answers 404 or "
                                               "410 (the handle is gone); a 422 to a liveactivity push drops only that activity's token, "
                                               "and the registration stays.", **PROPOSED)},
        "/v1/devices/me": {"delete": op("Unpair: revoke this device's keys, push and Live Activity registrations", "devices",
                                        {"204": {"description": "Revoked."}}, op_id="deleteDeviceMe", status="changed",
                                        desc="CONTRACT-2 §4.5. Afterwards this device's next connect gets device_revoked.")},
        "/v1/pair/begin": {"post": op("Begin pairing: the server commits to its nonce before seeing the app's transcript", "pairing",
                                      ok(ref("PairBegin"), "The pairing id and the commitment.", code="201"),
                                      op_id="beginPairing", auth=False, status="new",
                                      desc="No auth and no token: it only commits. Rate-limited per client address. The app keeps "
                                           "the commitment and checks the serverNonce that POST /v1/pair reveals against it. CONTRACT-2 "
                                           "§4.3, §15 S1.")},
        "/v1/pair": {"post": op("Pair with the QR token (HMAC transcript, then SAS on the server side)", "pairing",
                                ok(ref("PairResponse"), "Awaiting the user's confirmation on the server side.", code="201"),
                                op_id="pair", auth=False, status="new", request=body(ref("PairRequest")),
                                desc="Replaces POST /v1/devices/pair. 400 pairing_code_invalid when the mac matches no open token. Such a "
                                     "request names no token, so it counts only toward an address backstop (IPv6 /64 or IPv4, much "
                                     "higher than 5) and never retires a token or locks out an address (E1 P2). The token is "
                                     "protected by its 128 bits, single use, a 10-minute expiry and at most 5 open tokens. Carries the pairingId from POST /v1/pair/begin; the answer "
                                     "reveals serverNonce, which the app checks against the commitment before showing the SAS (a "
                                     "mismatch aborts pairing; the token stays spent). CONTRACT-2 §4.3.")},
        "/v1/pair/{pairingId}": {"get": op("Poll a pairing until confirmed", "pairing", ok(ref("PairResponse"), "The pairing's state."),
                                           op_id="getPairing", auth=False, status="new", params=[path_param("pairingId")])},
        "/v1/devices/session/nonce": {"get": op("A nonce for the device key to sign", "pairing",
                                                ok(obj({"nonce": {"type": "string", "description": "32 random bytes, base64url without padding."},
                                                        "expires_at": TS}, ["nonce", "expires_at"]), "The nonce (60 s)."),
                                                op_id="getSessionNonce", auth=False, status="new",
                                                params=[{"name": "deviceId", "in": "query", "required": False,
                                                         "schema": {"type": "string", "format": "uuid"},
                                                         "description": "The device the nonce is for. The app always sends it (E1 P2): a bound "
                                                                        "nonce opens a session for that device only, at most 5 outstanding "
                                                                        "per device."}],
                                                desc="Single use (60 s), consumed whether or not the session request succeeds. Bound "
                                                     "(deviceId): at most 5 outstanding per device. Unbound: at most 5 per client address "
                                                     "(the IPv6 /64 or the IPv4 address, from X-Forwarded-For only behind a trusted "
                                                     "proxy), and a server-wide cap (reference value 10 000). 429 rate_limited past "
                                                     "either.")},
        "/v1/devices/session": {"post": op("A short-lived (1 h) session token from the device key's signature", "pairing",
                                           ok(obj({"token": {"type": "string", "description": "Opaque, 32 random bytes; the server stores "
                                                                                              "only its SHA-256."}, "expires_at": TS},
                                                  ["token", "expires_at"]), "The bearer for REST and /v2/live."),
                                           op_id="createSession", auth=False, status="new", request=body(ref("SessionRequest")),
                                           desc="Replaces v1's never-expiring device token. The device key signs 'pisa-session/1\\n<deviceId>"
                                                "\\n<nonce>' (ECDSA P-256 SHA-256, DER, base64). On expiry REST answers 401 session_expired and "
                                                "/v2/live closes with 4003 and the reason session_expired (no control frame): the app signs a "
                                                "new nonce. /v2/live and /v2/runs take a session token only. CONTRACT-2 §4.3; vectors "
                                                "fixtures/pairing/vectors.json.")},
        "/v1/pair/offers": {"post": op("A paired phone offers to pair a new device: it shows the QR", "pairing",
            {**ok(CREF("PairingOffer"), "The offer, with its QR and typed fallback.", code="201"), "403": {"description": "biometric_required"},
             "400": {"description": "validation_failed: a body that does not match PairingOfferRequest, for example one carrying "
                                    "signer (E1 P1: a signer key is host-only, made with the CLI on the server)"},
             "404": {"description": "the server has no TLS pin and addresses configured"}},
            op_id="createPairingOffer", status="new", params=SIGNED_HEADERS, request=body(CREF("PairingOfferRequest")),
            desc="Replaces v1's pairing-code path (POST /v1/devices/pairing-code). Signed: device op pairing_offer, target "
                 "'<SHA-256 hex of the body as sent>'. The server mints a pairing token as for `pisa pair` and returns the "
                 "pisa://pair link; the new device scans it and uses POST /v1/pair/begin and POST /v1/pair unchanged. The token "
                 "counts against the 5 open tokens; {} is a valid body (name defaults to 'device'); offerId is 'offer_' and 16 "
                 "random bytes, base64url. The confirming side is this phone: the server's CLI neither lists nor confirms an "
                 "offered pairing. A device paired from an offer (paired_via app) never registers the first approval (Face ID) "
                 "key. No signer: a signer key is host-only (E1 P1).",
            **signed("pairing_offer", "<SHA-256 hex of the request body, as sent>"))},
        "/v1/pair/offers/{offerId}": {
            "get": op("The offering phone polls its offer", "pairing", ok(CREF("PairingOffer"), "The offer's state."),
                      op_id="getPairingOffer", status="new", params=[path_param("offerId")],
                      desc="Only the device that made the offer may read, withdraw or confirm it; for any other device it is 404. "
                           "open until a device pairs with it (refused if withdrawn first, expired past the token's 10 minutes), then "
                           "the pairing's state. At awaiting_confirmation it carries pairingId, newDevice and the transcript fields, "
                           "and expiresAt is the confirmation deadline; the phone checks the reveal and computes the SAS itself "
                           "(CONTRACT-2 §4.3), never trusting a SAS from the server. At confirmed, deviceId."),
            "delete": op("Withdraw the offer, or refuse the device that scanned it", "pairing", {"204": {"description": "Withdrawn or refused."}},
                         op_id="deletePairingOffer", status="new", params=[path_param("offerId")],
                         desc="Unsigned: refusing grants nothing. Spends the token; a pairing awaiting confirmation becomes refused. "
                              "204 also when there is nothing left to withdraw. 404 for any device but the offering one."),
        },
        "/v1/pair/offers/{offerId}/confirm": {"post": op("Confirm the new device after comparing the SAS", "pairing",
            {**ok(CREF("PairingOffer"), "confirmed, with the new device's id."), "403": {"description": "biometric_required"},
             "409": {"description": "pairing_code_invalid: the pairing is not this offer's, not awaiting confirmation, or the "
                                    "SAS is not the server's (nothing is confirmed)"}},
            op_id="confirmPairingOffer", status="new", params=[path_param("offerId"), *SIGNED_HEADERS], request=body(CREF("PairingConfirm")),
            desc="Signed: device op pairing_confirm, target '<pairingId>:<sas>', so the Face ID approval names the exact device the "
                 "user compared. The server checks that the SAS it derived for that pairing equals the one sent.",
            **signed("pairing_confirm", "<pairingId>:<the 6-digit SAS>"))},
        "/v1/sheets/{id}": {"get": op("A sheet as a Ring 1 surface", "surfaces", ok(ref("A2UISurfaceActivity"), "The same content as the activity."),
                                      op_id="getSheet", status="changed", params=[path_param("id")],
                                      desc="Returns the surface's A2UI operations and its pisa block instead of a v1 present object.")},
    }
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "PISA contract 2: REST delta",
            "version": schema["x-pisa-contract"],
            "summary": "The endpoints contract 2 adds or changes. Everything else is protocol/openapi.json, unchanged.",
            "description": "Generated from " + SCHEMA_FILE + " by tools/gen_openapi_delta.py: do not edit. x-pisa-status is new, "
                           "changed or (for a v1 endpoint it removes) replaced. Headers are X-Pisa-Nonce, X-Pisa-Signature, "
                           "X-Pisa-Key-Id and X-Pisa-Origin.",
            "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
        },
        "servers": [{"url": "https://{host}", "variables": {"host": {"default": "pisa.example"}}}],
        "tags": [{"name": t} for t in ("discovery", "live", "approvals", "ai", "devices", "pairing", "surfaces")],
        "paths": paths,
        "components": {
            "securitySchemes": {"sessionToken": {"type": "http", "scheme": "bearer",
                                                 "description": "The 1 h session token from POST /v1/devices/session."}},
            "parameters": {
                "XPisaNonce": {"name": "X-Pisa-Nonce", "in": "header", "required": False, "schema": STR,
                               "description": "A device challenge's nonce (single use, 5 minutes)."},
                "XPisaSignature": {"name": "X-Pisa-Signature", "in": "header", "required": False, "schema": STR,
                                   "description": "The Face ID key's signature over the pisa-device canonical text: DER, base64."},
                "XPisaKeyId": {"name": "X-Pisa-Key-Id", "in": "header", "required": False, "schema": STR, "description": "The signing key."},
            },
            "schemas": {
                "PairingOfferRequest": obj({"name": {"type": "string", "minLength": 1, "maxLength": 60,
                                                     "description": "A label for the new device; 'device' when absent"}}, [],
                                           description="Closed: there is no signer field. A signer key is host-only, made with the CLI "
                                                       "on the server (E1 P1); a body carrying signer is 400 validation_failed."),
                "PairingOffer": obj({"offerId": {"type": "string", "pattern": "^offer_[A-Za-z0-9_-]{22}$"}, "state": {"type": "string", "enum": ["open", "awaiting_confirmation", "confirmed", "refused", "expired"]},
                                     "qr": {"type": "string", "pattern": "^pisa://pair\\?v=1&"}, "token": {"type": "string", "pattern": "^[A-Z2-7]{26}$",
                                     "description": "The typed fallback (the same token)."}, "expiresAt": TS,
                                     "pairingId": STR, "deviceId": {"type": "string", "description": "When confirmed."},
                                     "newDevice": obj({"name": STR, "platform": STR}, ["name", "platform"]),
                                     "transcript": obj({"spki": STR, "deviceKey": STR, "clientNonce": STR, "commitment": STR, "serverNonce": STR},
                                                       ["spki", "deviceKey", "clientNonce", "commitment", "serverNonce"],
                                                       description="awaiting_confirmation only: what the phone needs to compute the SAS. "
                                                                   "The phone checks serverNonce against the commitment (as the new "
                                                                   "device does) before showing digits.")},
                                    ["offerId", "state", "expiresAt"]),
                "PairingConfirm": obj({"pairingId": STR, "sas": {"type": "string", "pattern": "^[0-9]{6}$"}}, ["pairingId", "sas"]),
                "ApprovalChallengeRequest": obj({"action": {"const": "approval"}, "approval_id": STR, "interrupt_id": STR},
                                                ["action", "approval_id", "interrupt_id"]),
                "DeviceChallengeRequest": obj({"action": {"const": "device"}, "op": {"type": "string", "enum": DEVICE_OPS,
                                                                                      "x-pisa-pairing-ops": PAIRING_OPS},
                                               "target": {"type": "string", "minLength": 1, "maxLength": 2000}},
                                              ["action", "op", "target"], description="A signed device op (v1 CORE-SEC). The server puts "
                                              "the target on one line: control characters become spaces."),
                "Challenge": obj({"action": {"type": "string", "enum": ["approval", "device"]}, "nonce": STR,
                                  "canonical": {"type": "string", "description": "pisa-approval/2 (§6.2), or pisa-device for a device op."},
                                  "expires_at": TS}, ["action", "nonce", "canonical", "expires_at"]),
                "ServerCreate": obj({"type": {"const": "openai_compatible"},
                                     "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,31}$",
                                            "description": "The provider part of its models' references (<id>:<model id>)."},
                                     "base_url": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "The address that ends in /v1."},
                                     "name": NULLABLE_STR(100), "ca_bundle": {**NULLABLE_STR(4096), "description":
                                     "An absolute path, on the PISA server, to the PEM file of a private CA that signs the model server's certificate."}},
                                    ["type", "id", "base_url"], description="An OpenAI-compatible model server."),
                "ServerUpdate": obj({"base_url": NULLABLE_STR(2000), "name": NULLABLE_STR(100), "ca_bundle": NULLABLE_STR(4096)}, [],
                                    description="Only the fields sent change; ca_bundle null removes the bundle."),
                "LastTest": obj({"ok": {"type": ["boolean", "null"]}, "at": TS, "message": STR}, ["ok", "at", "message"]),
                "Provider": {"type": "object", "required": ["id", "name", "type", "key_set", "key_last4", "last_test", "keys_url", "base_url",
                                                             "ca_bundle", "editable"],
                             "properties": {"id": STR, "name": STR, "type": {"type": "string", "enum": ["builtin", "openai_compatible"]},
                                            "key_set": {"type": "boolean"}, "key_last4": {"type": ["string", "null"]},
                                            "last_test": {"oneOf": [CREF("LastTest"), {"type": "null"}]}, "keys_url": {"type": ["string", "null"]},
                                            "base_url": {"type": ["string", "null"]}, "ca_bundle": {"type": ["string", "null"]},
                                            "editable": {"type": "boolean", "description": "false for built-in providers and for a server the "
                                                                                           "operator set in the environment"}},
                             "description": "v1's provider entry, plus the model-server fields. Further fields may appear (v1's list)."},
                "CapabilityOverrides": {"type": "object", "propertyNames": {"enum": CAPS}, "additionalProperties": {"type": "boolean"}},
                "ProviderCapabilities": obj({"model": STR, "probed_at": {"type": ["string", "null"], "format": "date-time"},
                                             **{k: {"type": "boolean"} for k in CAPS}, "overrides": CREF("CapabilityOverrides"),
                                             "features_needing_tools": {"type": "array", "items": STR, "description": "Feature ids, as in /v1/server.features."}},
                                            ["model", "probed_at", *CAPS, "overrides", "features_needing_tools"],
                                            description="Effective capabilities (the probe with the overrides applied). An unprobed model "
                                                        "reports every capability false, with probed_at null."),
            },
            "responses": {"Error": {"description": "v1's error envelope (protocol/openapi.json). 401 device_revoked: the app wipes "
                                                   "itself (§4.5); 403 ai_consent_required (§4.4).",
                                    "content": {"application/json": {"schema": {"$ref": "../openapi.json#/components/schemas/ErrorResponse"}}}}},
        },
        "x-pisa-replaced": {
            "POST /v1/turns": "POST /v2/runs",
            "GET /v1/device": "GET /v2/live",
            "POST /v1/devices/pair": "POST /v1/pair",
            "POST /v1/devices/pairing-code": "POST /v1/pair/offers",
            "POST /v1/approvals/{id}": "POST /v1/approvals/{id}/resume (decisions)",
        },
    }
