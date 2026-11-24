"""The wire protocol versions this server speaks, in one place: discovery (`GET /v1/server`), `/v2/live`'s
welcome, the talk data channel and the AG-UI and A2UI frames all read them here. No I/O."""

CONTRACT = (
    "2.2"  # the device contract: its latest minor (2.1 adds `pisa.voice.talk.text`; 2.2 `pisa.app`; additive)
)
CONTRACTS = (
    "2.0",
    "2.1",
    "2.2",
)  # every minor of it this server speaks: an app that lists any of them is served
AGUI = "1.0"  # AG-UI, the run events
A2UI = "1.0"  # A2UI, the surfaces
A2UI_CATALOG_ID = "urn:pisa:a2ui:catalog:core:1"  # the A2UI catalog Pisa composes its surfaces in
# core:2 = core:1 + WeatherSummary: only the weather surface is composed in it, for an app that listed it
A2UI_CATALOG_V2_ID = "urn:pisa:a2ui:catalog:core:2"
A2UI_CATALOG_IDS = [A2UI_CATALOG_ID, A2UI_CATALOG_V2_ID]  # every catalog Pisa composes in, in its order
MCP_APPS = "2026-01-26"  # MCP Apps
SUBPROTOCOL = "pisa.v2"  # contract 2's WebSocket subprotocol (`/v2/live` and the talk data channel)
