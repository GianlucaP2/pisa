"""The wire protocol versions this server speaks, in one place: discovery (`GET /v1/server`), `/v2/live`'s
welcome, the talk data channel and the AG-UI and A2UI frames all read them here. No I/O."""

CONTRACT = "2.0"  # the device contract
AGUI = "1.0"  # AG-UI, the run events
A2UI = "1.0"  # A2UI, the surfaces
A2UI_CATALOG_ID = "urn:pisa:a2ui:catalog:core:1"  # the A2UI catalog Pisa composes in
MCP_APPS = "2026-01-26"  # MCP Apps
SUBPROTOCOL = "pisa.v2"  # contract 2's WebSocket subprotocol (`/v2/live` and the talk data channel)
