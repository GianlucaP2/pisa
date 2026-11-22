"""Generates protocol/v2/asyncapi-live.json: AsyncAPI 3.0 for the /v2/live WebSocket (run tools/generate.py).

Built from pisa-contract-2.schema.json, never by hand: every frame and every pisa.* event listed in the schema becomes
a message whose payload is a $ref into the schema, so the two cannot disagree. Apache-2.0.
"""
from __future__ import annotations

from typing import Any

SCHEMA_FILE = "schema/pisa-contract-2.schema.json"
FORMAT = "application/schema+json;version=draft-2020-12"


def _payload(pointer: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"$ref": f"{SCHEMA_FILE}#/$defs/{pointer}"}
    if extra:
        schema = {"allOf": [schema, extra]}
    return {"schemaFormat": FORMAT, "schema": schema}


def _key(name: str) -> str:
    return name.replace(".", "_")


def build(schema: dict[str, Any]) -> dict[str, Any]:
    d = schema["$defs"]
    messages: dict[str, Any] = {}
    sends: list[str] = []      # app -> server
    receives: list[str] = []   # server -> app

    def add(key: str, msg: dict[str, Any], direction: list[str]) -> None:
        messages[key] = {"name": key, **msg}
        direction.append(key)

    # App -> server: the client frames, in ClientFrame's order.
    client_docs = {
        "Hello": ("hello", "The first frame: what the app speaks and draws (CONTRACT-2 §4.2)."),
        "ClientRun": ("run", "Start a run: a turn, or a resume of an interrupt (an approval decision)."),
        "ClientCancel": ("cancel", "Stop a run (barge-in)."),
        "ClientA2UI": ("a2ui", "An A2UI 1.0 renderer->agent message: an action from the closed pisa.* list, or an error."),
        "ClientCustom": ("custom", "A client pisa.* event; see the custom.* messages for each name."),
        "ClientPing": ("ping", "Keep-alive, every 25 s."),
    }
    for ref in (r["$ref"].rsplit("/", 1)[1] for r in d["ClientFrame"]["oneOf"]):
        op, summary = client_docs[ref]
        add(op, {"title": op, "summary": d[ref].get("description", summary), "payload": _payload(ref)}, sends)
    for name in schema["x-pisa-client-events"]:
        add("custom." + _key(name), {"title": name, "summary": d["c_" + _key(name)].get("description", name),
            "payload": _payload("ClientCustom", {"properties": {"name": {"const": name}}})}, sends)

    # Server -> app: control frames, then event frames.
    add("welcome", {"title": "welcome", "summary": d["Welcome"]["description"], "payload": _payload("Welcome")}, receives)
    add("pong", {"title": "pong", "summary": "Answer to ping.",
                 "payload": _payload("ServerControl", {"properties": {"control": {"const": "pong"}}})}, receives)
    add("error", {"title": "error", "summary": "A control error: auth_failed, device_revoked, contract_unsupported, invalid_frame, "
                  "rate_limited, internal, replaced or ai_consent_required (CONTRACT-2 §3.1, §4.5).",
                  "payload": _payload("ServerControl", {"properties": {"control": {"const": "error"}}})}, receives)
    add("event", {"title": "AG-UI event", "summary": d["ServerEventFrame"]["description"], "payload": _payload("ServerEventFrame")}, receives)
    for name in schema["x-pisa-server-events"]:
        value = d["s_" + _key(name)]
        if "$ref" in value:  # pisa.ai.disclosure is the AIDisclosure document itself
            value = d[value["$ref"].rsplit("/", 1)[1]]
        add("event." + _key(name), {"title": name, "summary": value.get("description", name),
            "payload": _payload("ServerEventFrame", {"properties": {"event": {"allOf": [
                {"$ref": f"{SCHEMA_FILE}#/$defs/ServerCustomEvent"}, {"properties": {"name": {"const": name}}}]}}})}, receives)

    channel_messages = {k: {"$ref": f"#/components/messages/{k}"} for k in messages}
    operations: dict[str, Any] = {}
    for k in sends:
        operations["send_" + k.replace(".", "_")] = {"action": "send", "channel": {"$ref": "#/channels/live"},
                                                      "messages": [{"$ref": f"#/channels/live/messages/{k}"}]}
    for k in receives:
        operations["receive_" + k.replace(".", "_")] = {"action": "receive", "channel": {"$ref": "#/channels/live"},
                                                         "messages": [{"$ref": f"#/channels/live/messages/{k}"}]}

    return {
        "asyncapi": "3.0.0",
        "info": {
            "title": "PISA contract 2: /v2/live",
            "version": schema["x-pisa-contract"],
            "description": (
                "The live socket between a PISA server and the PISA app (or any headless client). One JSON object per text "
                "frame. Server event frames carry plain AG-UI 1.0 events (each must also validate against the official AG-UI "
                "1.0 schema), multiplexed one AG-UI stream per threadId; pisa.* server events are AG-UI CUSTOM events. "
                "Generated from " + SCHEMA_FILE + " by tools/gen_asyncapi.py: do not edit."),
            "license": {"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        },
        "defaultContentType": "application/json",
        "servers": {
            "pisa": {
                "host": "{host}",
                "protocol": "wss",
                "description": "The user's own PISA server. TLS 1.3, pinned to the SPKI from pairing (CONTRACT-2 §3.1, §4.3).",
                "variables": {"host": {"description": "host[:port] of the server, from the pairing QR or pisa.endpoints.",
                                       "default": "pisa.example"}},
                "security": [{"$ref": "#/components/securitySchemes/sessionToken"}],
            }
        },
        "channels": {
            "live": {
                "address": "/v2/live",
                "title": "The live socket",
                "description": "WebSocket, subprotocol pisa.v2. The app sends hello first; the server answers welcome with what "
                               "it picked, and never sends what was not negotiated. Close codes 1000, 1002, 1011, 4001, 4003, 4008.",
                "servers": [{"$ref": "#/servers/pisa"}],
                "messages": channel_messages,
                "bindings": {"ws": {"method": "GET", "headers": {"type": "object", "required": ["Sec-WebSocket-Protocol"],
                                     "properties": {"Sec-WebSocket-Protocol": {"type": "string", "const": "pisa.v2"}}}}},
            }
        },
        "operations": operations,
        "components": {
            "messages": messages,
            "securitySchemes": {
                "sessionToken": {"type": "http", "scheme": "bearer",
                                 "description": "Short-lived (1 h) session token from POST /v1/devices/session, signed by the "
                                                "device's Secure Enclave key (CONTRACT-2 §4.3)."}
            },
        },
    }
