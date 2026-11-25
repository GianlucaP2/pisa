"""Generates protocol/v2/schema/pisa-contract-2.schema.json (run tools/generate.py; never edit the JSON by hand).

PISA contract 2: the pisa.* extensions, frames and negotiation around AG-UI 1.0, A2UI 1.0 and MCP Apps 2026-01-26.
Apache-2.0. The gaps contract 2 closed are marked G1, G2, G4, G5 and G6.
"""
import json
AGUI = "https://ag-ui.com/spec/1.0/schema.json"
A2UI_CAPS = "https://a2ui.org/specification/v1_0/renderer_capabilities.json"
A2UI_R2A = "https://a2ui.org/specification/v1_0/renderer_to_agent.json"
def s(mx, mn=1): return {"type": "string", "minLength": mn, "maxLength": mx}
def obj(props, req, desc=None, open_=False):
    o = {"type": "object", "properties": props, "required": req, "additionalProperties": open_}
    if desc: o["description"] = desc
    return o
UUID = {"$ref": "#/$defs/uuid"}
LANG = {"$ref": "#/$defs/lang"}
TS = {"type": "string", "format": "date-time"}
D = {}
D["uuid"] = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
D["lang"] = {"type": "string", "pattern": "^[a-z]{2}-[A-Z]{2}$"}
D["semver"] = {"type": "string", "pattern": "^[0-9]+\\.[0-9]+$"}

# ---------- server -> client CUSTOM values ----------
EV = {}
EV["pisa.voice.audio"] = obj({"messageId": s(64), "noticeId": UUID, "seq": {"type": "integer", "minimum": 0},
    "format": {"type": "string", "enum": ["opus", "aac_adts", "mp3", "pcm16"]}, "sampleRate": {"type": "integer", "enum": [16000, 22050, 24000, 44100, 48000]},
    "data": {"type": "string", "maxLength": 400000, "contentEncoding": "base64"}, "final": {"type": "boolean"}, "sentenceEnd": {"type": "boolean"},
    "complete": {"type": "boolean"}, "spokenChars": {"type": "integer", "minimum": 0}},
    ["seq", "format", "sampleRate", "data", "final"], "The server's voice for a text message (messageId) or a notice (noticeId), in seq order. Replaces say_audio. A device that negotiated audio.binary gets the same chunk as a binary frame instead.")
EV["pisa.voice.audio"]["anyOf"] = [{"required": ["messageId"]}, {"required": ["noticeId"]}]
EV["pisa.voice.state"] = obj({"state": {"type": "string", "enum": ["listening", "thinking", "speaking", "idle"]}}, ["state"], "What the voice is doing (natural mode): drives the presence orb and the Talk Live Activity.")
EV["pisa.voice.gate"] = obj({"heard": {"type": "string", "enum": ["owner", "other", "none"]}}, ["heard"],
    "G5. During a gated talk (VOICE-ID-1): whose voice the gate hears, at most twice a second (a subtle state on the orb). Replaces talk.gate.")
EV["pisa.voice.transcript"] = obj({"text": s(4000, 0), "final": {"type": "boolean"}, "ignored": {"type": "string", "enum": ["noise", "echo", "language", "fragment"], "description": "A final the server sets aside: no reply, no end of turn; the app clears the live words and waits. noise: nothing to answer (a cough, a lone letter); echo: the server's own voice heard back; language: a language other than the user's with a weak score (VOICE-ID-1, sent with text \"\"); fragment: a plainly unfinished fragment (sent with text \"\")."}}, ["text", "final"], "The user's words as the server heard them (natural mode); text is the whole line so far. A final without ignored becomes a user message in MESSAGES_SNAPSHOT.")
EV["pisa.voice.turn_hint"] = obj({"replyTo": s(64), "pEnd": {"type": "number", "minimum": 0, "maximum": 1}, "waitMs": {"type": "integer", "minimum": 0, "maximum": 5000}}, ["replyTo", "pEnd", "waitMs"], "End-of-turn score for a partial transcript (economy mode).")
EV["pisa.voice.understood"] = obj({"replyTo": s(64), "meant": s(4000), "raw": s(4000), "corrections": {"type": "array", "maxItems": 10, "items": obj({"from": s(200), "to": s(200)}, ["from", "to"])}, "ask": {"type": ["string", "null"], "maxLength": 200}}, ["replyTo", "meant", "raw", "corrections", "ask"], "What the server understood the user meant.")
CHOICE = obj({"id": {"type": "string", "pattern": "^[a-z_]{1,32}$"}, "label": s(40)}, ["id", "label"])
EV["pisa.notice"] = obj({"noticeId": UUID, "kind": {"type": "string", "enum": ["reminder", "briefing", "replan", "alert", "info", "check_in", "done", "turn_answer", "weekly", "ledger_week"]},
    "priority": {"type": "string", "enum": ["low", "normal", "urgent"]}, "urgency": {"type": "string", "enum": ["now", "breakpoint"]}, "lang": LANG,
    "text": s(1000), "title": s(200), "wrist": s(60, 0), "voice": {"type": "boolean", "default": True}, "sensitive": {"type": "boolean", "default": False},
    "checkIn": obj({"checkInId": UUID, "choices": {"type": "array", "minItems": 1, "maxItems": 5, "items": CHOICE}, "expectsReply": {"type": "boolean"}}, ["checkInId", "choices"]),
    "undo": obj({"undoId": s(200), "until": TS}, ["undoId", "until"]), "place": s(200),
    "refs": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 1200}, "description": "Ring 0 refs to open (task:, event:, sheet:, approval:, ledger_week:...)."},
    "surfaceId": s(80),
    "privacy": {"type": "string", "enum": ["none", "health", "finance", "place", "people"], "default": "none",
                "description": "What the notice touches. The lock screen, the watch and a relay push show a generic preview for health and finance unless the user allowed full previews for that class (Ring 0 setting)."},
    "genericText": s(120, 1)}, ["noticeId", "kind", "priority", "lang", "text"],
    "A proactive notice. The same object is the plaintext of a relay-sealed push (CONTRACT-2 §9). Replaces notify.")
EV["pisa.ask_to_open"] = obj({"why": {"type": "string", "enum": ["leave_now", "reply_waiting", "approval_lapsing", "check_in"]}, "line": s(300), "lang": LANG, "noticeId": UUID}, ["why", "line", "lang"], "The server asks to open a voice talk (earbuds, glasses, car).")
EV["pisa.directions"] = obj({"directionsId": UUID, "name": s(200), "lat": {"type": "number", "minimum": -90, "maximum": 90}, "lon": {"type": "number", "minimum": -180, "maximum": 180}, "mode": {"type": "string", "enum": ["walk", "transit", "drive", "bike"]}, "placeId": UUID}, ["directionsId", "name", "lat", "lon", "mode"], "Open the maps app (foreground) or a notification that opens it.")
EV["pisa.call"] = obj({"callId": UUID, "name": s(200), "phone": {"type": "string", "pattern": "^\\+[1-9][0-9]{5,14}$"}}, ["callId", "name", "phone"], "Place a phone call after the user's tap (Ring 0 confirms).")
EV["pisa.compose"] = obj({"composeId": UUID, "app": {"type": "string", "enum": ["messages", "whatsapp"]}, "recipient": obj({"name": s(200), "phone": {"type": "string", "pattern": "^\\+[1-9][0-9]{5,14}$"}}, ["name"]), "text": s(3000)}, ["composeId", "app", "recipient", "text"], "Open a prepared message; the user sends it.")
EV["pisa.action_done"] = obj({"approvalId": UUID, "kind": s(40), "summary": s(500), "result": s(500), "undoUntil": TS}, ["approvalId", "kind", "summary", "result"], "An approved act ran.")
EV["pisa.approval.state"] = obj({"approvalId": UUID, "interruptId": s(80), "state": {"type": "string", "enum": ["approved", "rejected", "expired", "replaced", "done", "failed"]}, "by": {"type": "string", "enum": ["voice", "tap", "biometric", "watch", "notification", "server"]}}, ["approvalId", "interruptId", "state"],
    "An approval's interrupt was closed somewhere else (voice, another device, expiry): the app drops the held interrupt and must not resume it.")
PROC = obj({"slot": s(40), "provider": obj({"id": s(60), "name": s(80), "kind": {"type": "string", "enum": ["cloud", "self_hosted", "on_server"]}, "region": s(40)}, ["id", "name", "kind"]),
    "model": s(120), "dataCategories": {"type": "array", "items": {"type": "string", "enum": ["conversation", "mail", "calendar", "places", "location", "people", "health", "finance", "documents", "voice"]}},
    "features": {"type": "array", "items": s(60)}}, ["slot", "provider", "model", "dataCategories"])
D["AIDisclosure"] = obj({"revision": {"type": "string", "pattern": "^[0-9a-f]{16,64}$"}, "processors": {"type": "array", "items": PROC},
    "healthRequiresSeparateConsent": {"const": True}}, ["revision", "processors", "healthRequiresSeparateConsent"],
    "Which AI providers and models the server sends the user's data to, per slot (guideline 5.1.2(i)). revision = sha256 of the JCS of processors; it changes whenever a provider or model changes, and the app asks again.")
EV["pisa.ai.disclosure"] = {"$ref": "#/$defs/AIDisclosure"}
EV["pisa.endpoints"] = obj({"revision": s(64), "endpoints": {"type": "array", "minItems": 1, "maxItems": 8, "items": obj({
    "kind": {"type": "string", "enum": ["lan", "ipv6", "ipv4", "host", "relay"]}, "address": s(255), "port": {"type": "integer", "minimum": 1, "maximum": 65535},
    "relayHandle": s(64)}, ["kind", "address"])}, "spki": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "string", "pattern": "^[A-Za-z0-9+/=]{44}$"}}},
    ["revision", "endpoints", "spki"], "The server's reachable addresses and its SPKI pins (current and the pre-announced next key), sent after a change. Authenticated by the pinned channel; the app keeps the set per server.")
EV["pisa.surface.focus"] = obj({"surfaceId": s(80), "mode": {"type": "string", "enum": ["take", "offer", "quiet"]}, "highlight": s(80)}, ["surfaceId", "mode"], "Bring a surface already shown back in front (v1 focus).")
EV["pisa.entitlement.required"] = obj({"feature": s(80), "level": s(40)}, ["feature", "level"], "The server offers this feature but the app reported no entitlement for it: the app shows its paywall (Ring 0).")

# ---------- client -> server custom values ----------
CEV = {}
CEV["pisa.presence"] = obj({"event": {"type": "string", "enum": ["glasses_on", "glasses_off", "app_foreground", "app_background", "earbuds_connected", "earbuds_disconnected", "companion_on", "companion_off"]},
    "state": obj({"foreground": {"type": "boolean"}, "locked": {"type": "boolean"}, "route": {"type": "string", "enum": ["speaker", "receiver", "wired", "earbuds", "airpods", "headphones", "bluetooth", "car", "glasses", "airplay", "other"],
        "description": "The audio route. airpods and headphones (G2) carry v1 talk.route: on airpods, glasses and car the server speaks shorter (D-055)."}, "glassesWorn": {"type": "boolean"}, "companion": {"type": "boolean"}}, [])}, ["event"], "Where the user can be reached. Sent on change and right after welcome.")
CEV["pisa.location"] = obj({"lat": {"type": "number", "minimum": -90, "maximum": 90}, "lon": {"type": "number", "minimum": -180, "maximum": 180}, "accuracyM": {"type": "number", "minimum": 0}, "measuredAt": TS}, ["lat", "lon", "accuracyM", "measuredAt"])
CEV["pisa.place_enter"] = obj({"noteIds": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "pattern": "^(task:)?[0-9a-f-]{36}$"}}}, ["noteIds"])
CEV["pisa.device_context"] = obj({"tz": s(64), "region": {"type": "string", "pattern": "^[A-Z]{2}$"}, "hourCycle": {"type": "string", "enum": ["h12", "h23"]}}, [])
CEV["pisa.surface.ack"] = obj({"surfaceId": s(80), "status": {"type": "string", "enum": ["shown", "queued", "deferred", "unavailable", "unsupported"]}, "surface": {"type": "string", "enum": ["phone", "glasses_display", "watch", "none"]}, "reason": s(200)}, ["surfaceId", "status", "surface"], "Replaces presented. Sent at once for every a2ui-surface or mcp-apps activity.")
CEV["pisa.surface.event"] = obj({"surfaceId": s(80), "event": {"type": "string", "enum": ["seen", "expanded", "kept", "closed"]}}, ["surfaceId", "event"], "Ids only: never gaze, never content.")
CEV["pisa.voice.partial"] = obj({"text": s(4000), "lang": LANG, "turnId": s(64)}, ["text", "lang", "turnId"], "A partial transcript (at most 2 a second while listening), only after welcome granted pisa.voice.turn_hints. Never stored.")
# contract 2.1 (additive): typing into the open talk (the feature talk.text)
CEV["pisa.voice.talk.text"] = obj({"id": UUID, "text": s(4000)}, ["id", "text"],
    "Contract 2.1, the feature talk.text (pisa.voice): words the user typed into this device's open talk; id is the talk's session_id "
    "(POST /v1/talk/sessions). The server gives them to the talker as the user's text turn and Pisa answers by voice; they come back "
    "as the user's final pisa.voice.transcript on the talk's channel (their bubble, in the talk's run) and are stored as their message. "
    "Refused with a control error: invalid_frame for an id that is not this device's open talk or text out of bounds (1-4000 characters, "
    "as a typed turn), ai_consent_required without the user's AI consent. While a talk is open a typed run is answered the same way, never "
    "in text only.")

# contract 2.2 (additive): the app's own screens (the extension pisa.app, the feature app.actions): Pisa opens one of
# them and the app reports back; the server says what to read again after a change (the riepilogo, the plan)
APP_ID = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"}
APP_TEXT = {"type": "string", "minLength": 1, "maxLength": 40, "pattern": "^[^\\u0000-\\u001f\\u007f]+$"}
APP_TARGETS = ["tab", "riepilogo", "task", "ai_privacy", "riepilogo_refresh"]
APP_REASONS = ["unsupported", "not_found", "not_now", "error"]
EV["pisa.app.open"] = obj({"actionId": UUID, "target": {"type": "string", "enum": APP_TARGETS}, "id": s(64)}, ["actionId", "target"],
    "Contract 2.2, the feature app.actions (pisa.app): Pisa asks the app to open one of its own screens: a tab (id: the tab's id, "
    "pisa, oggi or impostazioni), the riepilogo, a task (id: its uuid), Settings → AI e privacy, or to read the riepilogo again "
    "(riepilogo_refresh). The app does it and answers with pisa.app.result for the same actionId at once; without an answer in a "
    "few seconds Pisa says it isn't confirmed. Never anything outside the app.")
EV["pisa.app.open"]["allOf"] = [{"if": {"properties": {"target": {"enum": ["tab", "task"]}}}, "then": {"required": ["id"]}}]
EV["pisa.app.changed"] = obj({"views": {"type": "array", "minItems": 1, "maxItems": 4, "uniqueItems": True,
    "items": {"type": "string", "enum": ["riepilogo", "plan", "tasks", "agenda"]}}, "at": TS}, ["views", "at"],
    "Contract 2.2, the feature briefing.view (pisa.app): the user's tasks or agenda changed (on any device, by Pisa or by them), so "
    "the views named are out of date: the app reads them again (GET /v1/briefings/latest, GET /v1/plan...). Ids only, never content; "
    "at most about once a second.")

CEV["pisa.app.result"] = obj({"actionId": UUID, "ok": {"type": "boolean"}, "reason": {"type": "string", "enum": APP_REASONS}}, ["actionId", "ok"],
    "Contract 2.2, the feature app.actions (pisa.app): the app's answer to pisa.app.open: ok when the screen is open (or the "
    "riepilogo read again); else why not (unsupported: this app has no such screen; not_found: no such tab or task; not_now: the "
    "app can't move now, e.g. a sheet is open or the phone is locked; error).")

# contract 2.3 (additive): the phone's own maps (the extension pisa.maps, the feature maps.phone) and its local
# notifications (the extension pisa.notify, the feature notify.local). What the phone sends back (places, an ETA, an
# ack) is untrusted input to the server: every string is capped and free of control and bidi characters, every array
# is capped, and no coordinate goes into a log.
LAT = {"type": "number", "minimum": -90, "maximum": 90}
LON = {"type": "number", "minimum": -180, "maximum": 180}
# A pattern's $ also matches before a final newline in some regex engines (Python's): NO_NL refuses one everywhere.
NO_NL = {"pattern": "\\n"}
def text(mx): return {"type": "string", "minLength": 1, "maxLength": mx,
    "not": {"pattern": "[\\u0000-\\u001f\\u007f-\\u009f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069]"}}
MAPS_PLACE_ID = {"type": "string", "pattern": "^[A-Za-z0-9._:~-]{1,200}$", "not": NO_NL,
    "description": "The phone's maps service's own id for a place (opaque to the server); only ever handed back to the same phone."}
MAPS_KINDS = ["restaurant", "cafe", "pharmacy", "fuel", "supermarket", "atm", "parking", "other"]
MAPS_MODES = ["walk", "transit", "drive", "bike"]  # pisa.directions' modes: one vocabulary
MAPS_REASONS = ["denied", "unavailable", "no_results", "timeout", "unsupported"]
COORD = obj({"lat": LAT, "lon": LON}, ["lat", "lon"])
MAPS_POINT = {"oneOf": [{"const": "here"}, COORD, obj({"placeId": MAPS_PLACE_ID}, ["placeId"])],
    "description": "\"here\" (the phone's own location, which the phone reads and never sends back), a coordinate, or {placeId} "
        "from an earlier pisa.maps.result of this phone."}
D["MapsPlace"] = obj({"id": MAPS_PLACE_ID, "name": text(120), "address": text(200), "lat": LAT, "lon": LON,
    "kind": {"type": "string", "enum": MAPS_KINDS}, "distanceMeters": {"type": "integer", "minimum": 0, "maximum": 40000000},
    "openNow": {"type": "boolean"}, "phone": {"type": "string", "pattern": "^\\+[1-9][0-9]{5,14}$", "not": NO_NL},
    "url": {"type": "string", "maxLength": 500, "pattern": "^https://[^\\s]+$", "not": NO_NL}}, ["id", "name", "lat", "lon"],
    "Contract 2.3: one place the phone's own maps found (pisa.maps.result). Untrusted input to the server: its name, address "
    "and url are outside text (quarantined like a web page, never instructions), its url is shown, never fetched by the server; "
    "the coordinates are used for the answer and never logged. Not the catalog's PlaceSummary component, which the server "
    "composes from its own data.")
D["MapsEta"] = obj({"seconds": {"type": "integer", "minimum": 0, "maximum": 604800}, "distanceMeters": {"type": "integer", "minimum": 0, "maximum": 40000000},
    "mode": {"type": "string", "enum": MAPS_MODES}, "departAt": TS}, ["seconds", "distanceMeters", "mode"],
    "Contract 2.3: a travel time the phone's own maps worked out (pisa.maps.result): seconds and metres for mode, leaving at "
    "departAt (now when absent). Untrusted input to the server.")
EV["pisa.maps.search"] = obj({"requestId": UUID, "query": text(200), "near": {"oneOf": [{"const": "here"}, COORD]},
    "kind": {"type": "string", "enum": MAPS_KINDS}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["requestId", "query"],
    "Contract 2.3, the feature maps.phone (pisa.maps): Pisa asks the phone to look places up with its own maps (no server key), "
    "near \"here\" or a coordinate (absent: wherever the phone's maps search by default), of one kind, at most limit (default 5, "
    "at most 10). Sent in the turn's run; the phone answers pisa.maps.result for the same requestId at once. Without an answer "
    "in a few seconds Pisa says it couldn't look. The server never logs near's coordinates.")
EV["pisa.maps.eta"] = obj({"requestId": UUID, "from": MAPS_POINT, "to": MAPS_POINT, "mode": {"type": "string", "enum": MAPS_MODES}},
    ["requestId", "from", "to", "mode"],
    "Contract 2.3, the feature maps.phone (pisa.maps): Pisa asks the phone how long it takes from one point to another by mode "
    "(walk, transit, drive, bike, as pisa.directions) with its own maps; each point is \"here\", a coordinate or a placeId from an earlier "
    "result. The phone answers pisa.maps.result for the same requestId with eta. The server never logs the coordinates.")
CEV["pisa.maps.result"] = obj({"requestId": UUID, "ok": {"type": "boolean"},
    "places": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"$ref": "#/$defs/MapsPlace"}},
    "eta": {"$ref": "#/$defs/MapsEta"}, "reason": {"type": "string", "enum": MAPS_REASONS}}, ["requestId", "ok"],
    "Contract 2.3, the feature maps.phone (pisa.maps): the phone's answer to pisa.maps.search (places, 1-10) or pisa.maps.eta "
    "(eta), with ok true; or ok false and why (denied: the user did not allow location or maps; unavailable: the maps service "
    "failed or is offline; no_results; timeout: the maps service did not answer in time; unsupported: this client can't). "
    "Untrusted input to the server: bounded, quarantined as outside text, never instructions; coordinates are never logged.")
CEV["pisa.maps.result"]["allOf"] = [
    {"if": {"properties": {"ok": {"const": True}}}, "then": {"oneOf": [{"required": ["places"]}, {"required": ["eta"]}], "not": {"required": ["reason"]}},
     "else": {"required": ["reason"], "not": {"anyOf": [{"required": ["places"]}, {"required": ["eta"]}]}}}]

REVISION = {"type": "integer", "minimum": 1, "maximum": 9007199254740991,
    "description": "One counter per device, shared by pisa.notify.schedule, .cancel and .regions, growing by at least one per "
        "frame. The phone applies a frame only if its revision is above the last it applied (a replay is dropped, and acked again)."}
NOTIFY_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$", "not": NO_NL}
NOTIFY_KINDS = ["reminder", "task", "calendar", "leave"]
UUID_RE = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
DEEP_LINK = {"type": "string", "pattern": "^pisa://(chat|riepilogo|task/" + UUID_RE + "|reminder/" + UUID_RE + ")$", "not": NO_NL,
    "description": "Where a tap on the notification leads, one of an allow-listed set of the app's own screens: pisa://chat, "
        "pisa://riepilogo, pisa://task/<uuid>, pisa://reminder/<uuid>. Nothing else (never a web address, never pisa://pair)."}
NOTIFY_FAIL = ["denied", "limit", "past", "invalid", "unsupported", "error"]
D["NotifyItem"] = obj({"id": NOTIFY_ID, "fireAt": TS, "title": text(80), "body": text(200),
    "kind": {"type": "string", "enum": NOTIFY_KINDS}, "deepLink": DEEP_LINK}, ["id", "fireAt", "title", "kind"],
    "Contract 2.3: one local notification the phone schedules for fireAt (RFC 3339 with an offset). title and body are what "
    "the lock screen shows: the server keeps them minimal (no health or money detail, no address, no coordinate); the phone "
    "shows them as the user's preview setting allows.")
D["NotifyRegion"] = obj({"id": NOTIFY_ID, "lat": LAT, "lon": LON, "radiusMeters": {"type": "integer", "minimum": 100, "maximum": 5000},
    "title": text(80), "on": {"type": "string", "enum": ["enter", "exit"]}, "deepLink": DEEP_LINK}, ["id", "lat", "lon", "radiusMeters", "title", "on"],
    "Contract 2.3: a place the phone watches itself (geofence): it shows title when the user enters it or leaves it (on). "
    "The coordinates stay on the phone and in the server's store; neither side logs them.")
EV["pisa.notify.schedule"] = obj({"revision": REVISION, "items": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"$ref": "#/$defs/NotifyItem"}}},
    ["revision", "items"],
    "Contract 2.3, the feature notify.local (pisa.notify): schedule these local notifications on the phone (an item whose id is "
    "already scheduled replaces it; ids are unique within the frame), so they fire even with no connection. Sent on the notices "
    "thread; the phone answers pisa.notify.ack for the same revision.")
EV["pisa.notify.cancel"] = obj({"revision": REVISION, "ids": {"type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True, "items": NOTIFY_ID}},
    ["revision", "ids"],
    "Contract 2.3, the feature notify.local (pisa.notify): remove these scheduled notifications or watched places (an unknown id "
    "is no error). The phone answers pisa.notify.ack.")
EV["pisa.notify.regions"] = obj({"revision": REVISION, "regions": {"type": "array", "maxItems": 20, "items": {"$ref": "#/$defs/NotifyRegion"}}},
    ["revision", "regions"],
    "Contract 2.3, the feature notify.local (pisa.notify): the whole set of places the phone watches (at most 20, the system's "
    "limit); it replaces the previous set, and an empty one stops them all. The phone answers pisa.notify.ack.")
CEV["pisa.notify.ack"] = obj({"revision": REVISION,
    "scheduled": {"type": "array", "maxItems": 50, "uniqueItems": True, "items": NOTIFY_ID},
    "failed": {"type": "array", "maxItems": 50, "items": obj({"id": NOTIFY_ID, "reason": {"type": "string", "enum": NOTIFY_FAIL}}, ["id", "reason"])}},
    ["revision", "scheduled", "failed"],
    "Contract 2.3, the feature notify.local (pisa.notify): the phone's report on the frame with this revision: the ids it "
    "applied (scheduled, cancelled or watched) and those it could not, with why (denied: notifications or location not allowed; "
    "limit: the system's cap on pending notifications or regions; past: fireAt already gone; invalid; unsupported; error). "
    "Untrusted input to the server: ids it did not send are ignored.")

D["ServerCustomValue"] = {"description": "Values of the server's CUSTOM events, by name.", "oneOf": []}
D["ClientCustomValue"] = {"description": "Values of the client's custom frames, by name.", "oneOf": []}
for k, v in EV.items():
    key = "s_" + k.replace(".", "_"); D[key] = v
for k, v in CEV.items():
    key = "c_" + k.replace(".", "_"); D[key] = v
D["ServerCustomEvent"] = {"description": "An AG-UI CUSTOM event whose name is pisa.*: it must ALSO validate as an AG-UI 1.0 Event.",
    "type": "object", "required": ["type", "name", "value"], "properties": {"type": {"const": "CUSTOM"}, "timestamp": {"type": "integer"}, "name": {"enum": list(EV)}, "value": {"type": "object"}},
    "allOf": [{"if": {"properties": {"name": {"const": k}}}, "then": {"properties": {"value": {"$ref": "#/$defs/s_" + k.replace(".", "_")}}}} for k in EV]}
D["ClientCustom"] = {"type": "object", "required": ["op", "name", "value"], "additionalProperties": False,
    "properties": {"op": {"const": "custom"}, "name": {"enum": list(CEV)}, "value": {"type": "object"}},
    "allOf": [{"if": {"properties": {"name": {"const": k}}}, "then": {"properties": {"value": {"$ref": "#/$defs/c_" + k.replace(".", "_")}}}} for k in CEV]}
del D["ServerCustomValue"]; del D["ClientCustomValue"]

# ---------- negotiation ----------
EXT_NAMES = ["pisa.voice", "pisa.voice.turn_hints", "pisa.voice.audio.binary", "pisa.presence", "pisa.location", "pisa.notice", "pisa.directions", "pisa.call", "pisa.compose", "pisa.approval", "pisa.surface", "pisa.app", "pisa.maps", "pisa.notify"]
D["ExtensionSet"] = {"type": "object", "description": "pisa.* extension -> its major version.", "propertyNames": {"pattern": "^pisa\\.[a-z_.]{1,60}$"}, "additionalProperties": {"type": "integer", "minimum": 1}}
D["Hello"] = obj({
    "op": {"const": "hello"},
    "contract": obj({"versions": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/semver"}}}, ["versions"]),
    "agui": obj({"protocolVersions": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/semver"}}}, ["protocolVersions"]),
    "a2ui": {"$ref": A2UI_CAPS, "description": "Exactly A2UI's renderer capabilities object ({\"1.0\": {supportedCatalogIds}})."},
    "mcpApps": obj({"protocolVersions": {"type": "array", "items": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}}, "mimeTypes": {"type": "array", "items": {"const": "text/html;profile=mcp-app"}}, "bundledLibraries": {"type": "array", "maxItems": 20, "items": {"type": "string", "pattern": "^[a-z0-9-]+@[0-9.]+$"}}}, ["protocolVersions", "mimeTypes"]),
    "rings": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "integer", "enum": [0, 1, 2]}},
    "extensions": {"$ref": "#/$defs/ExtensionSet"},
    "device": obj({"deviceId": UUID, "appVersion": s(40), "platform": {"type": "string", "enum": ["ios", "watchos", "android", "web", "cli", "other"]},
        "surfaces": {"type": "array", "items": {"type": "string", "enum": ["phone", "glasses_display", "watch", "none"]}},
        "audioFormats": {"type": "array", "uniqueItems": True, "items": {"type": "string", "enum": ["opus", "aac_adts", "mp3", "pcm16"]}},
        "wearable": {"oneOf": [{"type": "null"}, obj({"kind": {"type": "string", "enum": ["meta_glasses", "other"]}, "model": s(80), "connected": {"type": "boolean"}}, ["kind", "connected"])]},
        "tz": s(64), "region": {"type": "string", "pattern": "^[A-Z]{2}$"}, "hourCycle": {"type": "string", "enum": ["h12", "h23"]}}, ["deviceId", "appVersion", "platform"]),
    "entitlements": {"type": "array", "maxItems": 8, "items": s(40), "description": "Entitlement levels the app holds (from StoreKit), e.g. pisa.pro. Advisory: used only to choose what to offer; never a security boundary."},
    "resume": obj({"sessionId": s(80), "lastSeq": {"type": "integer", "minimum": 0}}, ["sessionId", "lastSeq"])},
    ["op", "contract", "agui", "rings", "device"], "Client -> server, the first frame on /v2/live.")
D["Hello"]["$comment"] = ("G1: v1 hello.capabilities booleans (mic, speaker, display, camera, local_stt, local_tts, location, push) have no "
    "contract 2 field on purpose. No backend reads them (old J core reads only audio_formats and present, which became "
    "device.audioFormats and the A2UI catalogs; display became device.surfaces and rings). A device fact a server needs "
    "is added as a named optional field in a contract minor, never as an open set.")
D["Welcome"] = obj({"control": {"const": "welcome"}, "seq": {"type": "integer", "minimum": 0}, "sessionId": s(80), "contract": {"$ref": "#/$defs/semver"}, "agui": {"$ref": "#/$defs/semver"},
    "a2ui": {"oneOf": [{"type": "null"}, obj({"version": {"const": "1.0"}, "catalogIds": {"type": "array", "minItems": 1, "items": s(200)}}, ["version", "catalogIds"])]},
    "mcpApps": {"oneOf": [{"type": "null"}, obj({"protocolVersion": {"type": "string"}}, ["protocolVersion"])]},
    "rings": {"type": "array", "items": {"type": "integer", "enum": [0, 1, 2]}}, "extensions": {"$ref": "#/$defs/ExtensionSet"},
    "features": {"$ref": "#/$defs/FeatureList"}, "languages": {"type": "array", "items": LANG},
    "replayed": {"type": "boolean", "description": "true: the frames after resume.lastSeq follow; false: they were lost, the app reloads state over REST."}},
    ["control", "seq", "sessionId", "contract", "agui", "a2ui", "mcpApps", "rings", "extensions"], "Server -> client: what the server picked. Everything not picked is never sent.")
D["Feature"] = obj({"id": {"type": "string", "pattern": "^[a-z][a-z0-9_.]{1,60}$"}, "version": {"type": "integer", "minimum": 1},
    "entitlement": {"type": "string", "maxLength": 40, "description": "If present: the app shows the feature only while StoreKit says this level (or higher) is active."},
    "deprecated": obj({"sunset": {"type": "string", "format": "date"}, "replacement": s(60)}, ["sunset"])}, ["id", "version"])
# Safety features are ALWAYS ON (architect 1, 18 Nov): every server offers them, and they are never feature flags,
# so they can be neither paywalled nor deprecated. A minimal server lists at least these.
REQUIRED_FEATURES = ["stop", "approvals.signed"]
D["FeatureList"] = {"type": "array", "items": {"$ref": "#/$defs/Feature"},
    "description": "What the server offers. It always includes the safety features " + " and ".join(REQUIRED_FEATURES) +
        " (stop: cancelling any run and the stop/resume of autonomous work; approvals.signed: approvals as signed interrupts, "
        "CONTRACT-2 §6), which are never paywalled or deprecated. Any other feature is optional.",
    "allOf": [{"contains": {"type": "object", "properties": {"id": {"const": f}}, "required": ["id"]}} for f in REQUIRED_FEATURES] +
        [{"items": {"if": {"properties": {"id": {"enum": REQUIRED_FEATURES}}, "required": ["id"]},
                    "then": {"not": {"anyOf": [{"required": ["entitlement"]}, {"required": ["deprecated"]}]}}}}]}
D["ServerId"] = {"type": "string", "pattern": "^[A-Za-z0-9_-]{22,64}$",
    "description": "An opaque random id the server picks once, at install (at least 128 bits, base64url), and keeps for its life. "
        "Never derived from hardware, the hostname, an address or the owner. It identifies the install, not the person, so it may "
        "be served without auth. The app keys pairings by serverId plus host; approvals sign it (server=, CONTRACT-2 §6.2)."}
D["ServerDiscovery"] = obj({
    "product": {"const": "pisa"}, "serverId": {"$ref": "#/$defs/ServerId"}, "serverVersion": s(40),
    "contract": obj({"versions": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/semver"}}, "minApp": s(40, 0)}, ["versions"]),
    "agui": {"type": "array", "items": {"$ref": "#/$defs/semver"}}, "a2ui": obj({"versions": {"type": "array", "items": {"type": "string"}}, "catalogIds": {"type": "array", "items": s(200)}}, ["versions", "catalogIds"]),
    "mcpApps": {"type": "array", "items": {"type": "string"}},
    "features": {"$ref": "#/$defs/FeatureList"},
    "readiness": obj({"ready": {"type": "boolean"}, "checks": {"type": "array", "items": obj({"name": {"type": "string", "enum": ["database", "migrations", "models", "push", "clock"]}, "ok": {"type": "boolean"}}, ["name", "ok"])}}, ["ready"]),
    "reach": obj({"scheme": {"type": "string", "enum": ["https", "http"]}, "network": {"type": "string", "enum": ["public", "tailnet", "lan", "unknown"]}}, ["scheme", "network"]),
    "push": obj({"modes": {"type": "array", "items": {"type": "string", "enum": ["relay", "direct", "none"]}}, "relayConfigured": {"type": "boolean"}}, ["modes"]),
    "pairing": obj({"methods": {"type": "array", "items": {"type": "string", "enum": ["qr_v1", "typed_v1"]}}}, ["methods"]),
    "ai": obj({"disclosureRevision": {"type": "string", "pattern": "^[0-9a-f]{16,64}$"}}, ["disclosureRevision"], "Only the revision: who the providers are is told after pairing (GET /v1/ai/disclosure), so an unauthenticated caller cannot fingerprint the setup."),
    "deprecations": {"type": "array", "items": obj({"what": s(120), "sunset": {"type": "string", "format": "date"}}, ["what", "sunset"])}},
    ["product", "serverId", "serverVersion", "contract", "agui", "a2ui", "mcpApps", "features", "readiness", "reach", "push"],
    "GET /v1/server: no auth, nothing personal (D-128). The connection guide's server step reads it.")

# ---------- pairing (CONTRACT-2 §4.3) ----------
B64 = {"type": "string", "pattern": "^[A-Za-z0-9+/=_-]{8,400}$"}
D["PairingQR"] = obj({"v": {"const": 1}, "addrs": {"type": "array", "minItems": 1, "maxItems": 6, "items": s(300)}, "spki": B64, "token": {"type": "string", "pattern": "^[A-Z2-7]{26}$"},
    "exp": {"type": "integer"}}, ["v", "addrs", "spki", "token", "exp"],
    "pisa://pair?v=1&addrs=..&spki=..&token=..&exp=.. as an object. token: 128-bit one-time secret, base32 (26 characters, also the typed fallback); exp: unix seconds, at most 10 minutes ahead. No long-term secret.")
D["PairBegin"] = obj({"pairingId": s(80), "commitment": {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}, "expiresAt": TS},
    ["pairingId", "commitment", "expiresAt"],
    "POST /v1/pair/begin (no auth, no token; rate-limited per address): the server picks serverNonce now and commits to it, BEFORE "
    "it can see the app's transcript. commitment = base64url (no padding) of SHA-256('pisa-sas-commit/1\\n' || serverNonce). "
    "This ordering is what stops SAS grinding (CONTRACT-2 §15 S1), and what makes the typed fallback safe.")
D["PairRequest"] = obj({"v": {"const": 1}, "pairingId": s(80), "deviceKey": B64, "name": s(60), "platform": {"type": "string", "enum": ["ios", "watchos", "android", "web", "cli", "other"]},
    "clientNonce": B64, "mac": B64}, ["v", "pairingId", "deviceKey", "name", "platform", "clientNonce", "mac"],
    "POST /v1/pair over the pinned channel. deviceKey: the Secure Enclave P-256 device key (SPKI, base64). mac = HMAC-SHA256(token, transcript) where transcript = 'pisa-pair/1' | spki | deviceKey | clientNonce.")
D["PairResponse"] = obj({"pairingId": s(80), "deviceId": UUID, "serverNonce": B64, "mac": B64, "state": {"type": "string", "enum": ["awaiting_confirmation", "confirmed", "refused", "expired"]}},
    ["pairingId", "deviceId", "serverNonce", "mac", "state"],
    "serverNonce is the reveal of PairBegin's commitment: the app checks SHA-256('pisa-sas-commit/1\\n' || serverNonce) against it "
    "BEFORE showing any digits, and aborts pairing on a mismatch. mac = HMAC-SHA256(token, transcript \\n serverNonce). SAS = 6 "
    "decimal digits from SHA-256('pisa-sas/1' \\n transcript \\n serverNonce), shown on both sides; the server's side confirms.")
D["SessionRequest"] = obj({"deviceId": UUID, "nonce": B64, "signature": B64}, ["deviceId", "nonce", "signature"],
    "POST /v1/devices/session: the device key signs 'pisa-session/1\\n<deviceId>\\n<nonce>' (nonce from GET /v1/devices/session/nonce) and gets a short-lived bearer (1 h). Device key: Secure Enclave, .privateKeyUsage, no biometry; separate from the approval key.")

# ---------- frames ----------
D["ClientRun"] = obj({"op": {"const": "run"}, "input": {"$ref": AGUI + "#RunAgentInput"}}, ["op", "input"], "Start a run (a turn, or a resume). input is an AG-UI 1.0 RunAgentInput; forwardedProps.pisa carries the turn's device facts (TurnProps).")
D["ClientCancel"] = obj({"op": {"const": "cancel"}, "runId": s(80)}, ["op", "runId"], "Stop a run: the server ends it with the cancelled outcome and sends nothing more for it (barge-in).")
D["ClientA2UI"] = obj({"op": {"const": "a2ui"}, "message": {"$ref": A2UI_R2A}}, ["op", "message"], "An A2UI renderer->agent message (action or error), exactly as A2UI 1.0 defines it.")
D["ClientPing"] = obj({"op": {"const": "ping"}}, ["op"])
D["ClientFrame"] = {"oneOf": [{"$ref": "#/$defs/Hello"}, {"$ref": "#/$defs/ClientRun"}, {"$ref": "#/$defs/ClientCancel"}, {"$ref": "#/$defs/ClientA2UI"}, {"$ref": "#/$defs/ClientCustom"}, {"$ref": "#/$defs/ClientPing"}]}
D["ServerEventFrame"] = obj({"seq": {"type": "integer", "minimum": 1}, "threadId": s(120), "runId": s(80), "event": {"$ref": AGUI}}, ["seq", "threadId", "event"],
    "One AG-UI 1.0 event of the AG-UI stream for threadId (the socket multiplexes one AG-UI stream per thread; each obeys the AG-UI lifecycle on its own). runId is the run the event belongs to. Session-level server events (pisa.notice and the rest) travel in short server-started runs on the thread pisa-notices:<deviceId>. seq grows by one per frame in the session; resume replays after lastSeq.")
D["ServerControl"] = {"oneOf": [{"$ref": "#/$defs/Welcome"},
    obj({"control": {"const": "pong"}, "seq": {"type": "integer"}}, ["control", "seq"]),
    obj({"control": {"const": "error"}, "seq": {"type": "integer"}, "code": {"type": "string", "enum": ["auth_failed", "device_revoked", "contract_unsupported", "invalid_frame", "rate_limited", "internal", "replaced", "ai_consent_required"]}, "message": s(500), "retryable": {"type": "boolean"}}, ["control", "seq", "code", "message", "retryable"])]}
D["ServerFrame"] = {"oneOf": [{"$ref": "#/$defs/ServerEventFrame"}, {"$ref": "#/$defs/ServerControl"}]}

# ---------- run props ----------
D["AppMap"] = obj({"version": {"type": "string", "pattern": "^[0-9]{1,4}(\\.[0-9]{1,4}){1,2}$"}, "build": {"type": "integer", "minimum": 1, "maximum": 1000000},
    "tabs": {"type": "array", "minItems": 1, "maxItems": 6, "items": obj({"id": APP_ID, "title": APP_TEXT}, ["id", "title"])},
    "current": APP_ID, "cards": {"type": "array", "maxItems": 12, "uniqueItems": True, "items": APP_ID},
    "settings": {"type": "array", "maxItems": 16, "items": APP_TEXT}}, ["version", "tabs", "current"],
    "Contract 2.2: the app's screen map with each run (forwardedProps.pisa.app): its version, its tabs (id and title, in order), the "
    "tab on screen (current, one of the tabs' ids), the cards visible on it, and the Settings sections by title. app_help answers "
    "from it. The server treats its titles as untrusted data (as outside text): they never change Pisa's tools or rules. A map out "
    "of these bounds is ignored (the turn goes on with Pisa's own description of the app).")
D["TurnProps"] = obj({"turn": obj({"source": {"type": "string", "enum": ["glasses_mic", "phone_mic", "earbuds_mic", "watch_mic", "typed"]}, "wake": {"type": "string", "enum": ["wake_phrase", "stem_press", "tap", "reply", "glasses_button", "intent"]},
        "lang": LANG, "origin": {"type": "string", "enum": ["phone", "watch", "cli", "web"]}, "continues": s(80),
        "voice": obj({"pace": {"enum": ["slow", "usual", "fast"]}, "loudness": {"enum": ["soft", "usual", "loud"]}, "gaps": {"enum": ["few", "usual", "many"]}, "place": {"enum": ["whisper", "normal", "noisy_place"]}}, [])}, ["source", "lang"]),
    "consent": obj({"aiRevision": {"type": "string", "pattern": "^[0-9a-f]{16,64}$"}, "health": {"type": "boolean"}}, ["aiRevision"],
        "The AI disclosure revision the user allowed on this device, and whether health may go to AI. A run whose revision is not current ends with RUN_ERROR ai_consent_required."),
    "ask": obj({"source": {"type": "string", "enum": ["mcp-apps", "a2ui"]}, "surfaceId": s(120), "elementId": s(80), "label": s(120)}, ["source", "surfaceId", "label"],
        "'Ask about this' from a drawing (Ring 2 ui/message after the user's tap, or a native diagram's element). label is the view's text: outside text, quarantined by the server."),
    "a2uiAction": {"$ref": A2UI_R2A, "description": "HTTP binding only: the A2UI action this run answers (the WebSocket sends op a2ui instead)."},
    "app": {"$ref": "#/$defs/AppMap"}}, [],
    "RunAgentInput.forwardedProps.pisa")

# ---------- approvals ----------
D["ApprovalInterrupt"] = obj({"id": s(80), "reason": {"const": "pisa.approval"}, "message": s(500), "toolCallId": s(80), "expiresAt": TS,
    "responseSchema": {"type": "object"},
    "metadata": obj({"pisa": obj({"approvalId": UUID, "kind": {"type": "string", "enum": ["calendar_create", "calendar_move", "calendar_cancel", "email_send", "setting_change"]}, "risk": {"const": "R2"},
        "requiresBiometric": {"type": "boolean"}, "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "recipientsCount": {"type": "integer", "minimum": 0},
        "undoMinutes": {"type": ["integer", "null"]}, "detailsRef": {"type": "string", "pattern": "^approval:[0-9a-f-]{36}$"}, "server": {"$ref": "#/$defs/ServerId"}},
        ["approvalId", "kind", "risk", "requiresBiometric", "digest", "server"])}, ["pisa"])},
    ["id", "reason", "message", "expiresAt", "responseSchema", "metadata"],
    "The AG-UI Interrupt PISA raises for an approval. message is server-templated (never outside text); the content to approve is fetched by detailsRef over REST and shown as quoted data.")
D["ApprovalResume"] = obj({"decision": {"type": "string", "enum": ["approve", "reject"]}, "auth": {"type": "string", "enum": ["tap", "voice", "biometric"]},
    "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "nonce": s(200), "signature": {"type": "string", "contentEncoding": "base64", "maxLength": 200}, "keyId": s(80)},
    ["decision", "auth", "digest"], "ResumeEntry.payload for a pisa.approval interrupt; also the body of REST POST "
    "/v1/approvals/{id}/resume, in the same camelCase (keyId). A ResumeEntry with status cancelled carries no payload and decides "
    "nothing: the approval stays pending until it is decided elsewhere or expires.")
APPROVAL_ERRORS = ["approval_changed", "approval_expired", "approval_decided", "biometric_required", "action_failed"]
D["ApprovalErrorCode"] = {"type": "string", "enum": APPROVAL_ERRORS,
    "description": "RUN_ERROR.code on an approval thread (pisa-approval:<id>). approval_changed: the content changed since the user "
        "saw it, nothing ran; approval_expired; approval_decided: already decided elsewhere; biometric_required: this approval "
        "needs a valid Face ID signature; action_failed: approved, the action ran, and it failed (the approval is decided)."}
D["ApprovalRunError"] = {"type": "object", "required": ["type", "message", "code"],
    "properties": {"type": {"const": "RUN_ERROR"}, "message": {"type": "string"}, "code": {"$ref": "#/$defs/ApprovalErrorCode"}},
    "description": "A RUN_ERROR on an approval thread: it must ALSO validate as an AG-UI 1.0 RunErrorEvent. Its code is one of ApprovalErrorCode."}
D["ApprovalResume"]["allOf"] = [{"if": {"properties": {"auth": {"const": "biometric"}}}, "then": {"required": ["nonce", "signature", "keyId"]}}]

# ---------- activities ----------
EXPIRES = {"type": "string", "format": "date-time", "description": "G6 (v1 present.expires_at): when the surface leaves the screen. "
    "At this time the app removes it from view by itself, keeping it in the conversation's history, even if no deleteSurface "
    "arrives (the app may be offline). The server still sends deleteSurface when it can. Absent: it stays until deleted."}
D["A2UISurfaceActivity"] = obj({"a2ui_operations": {"type": "array", "minItems": 1, "items": {"$ref": "https://a2ui.org/specification/v1_0/agent_to_renderer.json"}},
    "pisa": obj({"purpose": s(120), "focus": {"type": "string", "enum": ["take", "offer", "quiet"]}, "replyTo": s(80), "fallbackText": s(1000), "lang": LANG, "basis": {"type": "string", "enum": ["data", "knowledge"]}, "expiresAt": EXPIRES}, ["focus", "fallbackText"])},
    ["a2ui_operations", "pisa"], "ACTIVITY_SNAPSHOT/ACTIVITY_DELTA content for activityType a2ui-surface (messageId = the surfaceId).")
D["McpAppActivity"] = obj({"resourceUri": {"type": "string", "pattern": "^ui://"}, "mimeType": {"const": "text/html;profile=mcp-app"},
    "html": s(204800), "toolInput": {"type": "object", "description": "What the view receives in ui/notifications/tool-input (v1's J_DATA)."},
    "csp": obj({"connectDomains": {"type": "array", "maxItems": 0}, "resourceDomains": {"type": "array", "maxItems": 0}, "frameDomains": {"type": "array", "maxItems": 0}, "baseUriDomains": {"type": "array", "maxItems": 0}}, []),
    "permissions": obj({}, [], "Always empty: no camera, microphone, geolocation or clipboard."),
    "pisa": obj({"title": s(120), "summary": s(400), "fallbackText": s(1000), "bundle": {"type": "array", "maxItems": 6, "items": {"type": "string", "pattern": "^(d3@7\\.9\\.0|mermaid@12\\.2\\.0|vega@6\\.4\\.0|vega-lite@6\\.5\\.0|vega-embed@7\\.3\\.0|vega-interpreter@2\\.3\\.2)$"}},
        "kind": {"type": "string", "enum": ["flow", "schema", "chart", "comparison", "timeline", "explorer", "other"]}, "basis": {"type": "string", "enum": ["data", "knowledge"]}, "focus": {"type": "string", "enum": ["take", "offer", "quiet"]}, "expiresAt": EXPIRES}, ["title", "summary", "fallbackText"])},
    ["resourceUri", "mimeType", "html", "toolInput", "pisa"], "ACTIVITY_SNAPSHOT content for activityType mcp-apps (Ring 2).")

# ---------- the sealed push (CONTRACT-2 §9) ----------
D["PushPlaintext"] = obj({"v": {"const": 2}, "notice": {"$ref": "#/$defs/s_pisa_notice"},
    "sentAt": {"type": "string", "format": "date-time", "pattern": "Z$",
               "description": "When the server sealed it, RFC 3339 in UTC (ends in Z). The Notification Service Extension drops a push "
                              "whose sentAt is more than 7 days old, and any repeat of a noticeId it has seen (security review E1 "
                              "R11: a replayed old sealed blob must not show again)."},
    "apns": obj({"category": s(64), "threadId": s(64), "interruption": {"type": "string", "enum": ["passive", "active", "time-sensitive"]},
                 "askId": UUID, "why": s(300)}, ["category", "threadId", "interruption"],
                "How to file the banner, as a direct push would: the notification category, the thread, the APNs interruption level, "
                "and for a 'may I speak?' ask its id and line.")},
    ["v", "notice", "sentAt"],
    "The plaintext of a relay-sealed push: compact UTF-8 JSON, padded with trailing spaces so that enc + ciphertext is exactly 512 "
    "or 2048 bytes, then sealed with HPKE base mode (X25519, HKDF-SHA256, ChaCha20-Poly1305), info pisa-push/2, aad the relay "
    "handle (UTF-8). notice is the same object the live socket sends. Vectors: fixtures/pairing/vectors.json.")

# ---------- G4: how a talk ended ----------
D["TalkEndedResult"] = obj({"talkEnded": {"type": "string", "enum": ["idle", "owner", "budget", "error", "fallback", "route"]}}, ["talkEnded"],
    "G4. RUN_FINISHED.result of a talk's last run, just before its data channel closes (replaces talk.ended). idle: nobody spoke; "
    "owner: the user (or a newer talk of this device) ended it; budget: the spending cap; error: the server or the peer failed; "
    "fallback: the voice model dropped, carry on in economy over /v2/live; route: a gated talk's route changed (or the voice "
    "profile was deleted), reopen it at once on the new route with no error shown.")

# ---------- G5: voice enrolment (VOICE-ID-1), outside AG-UI runs ----------
# The enrolment session (POST /v1/talk/sessions purpose "enrol") is not a conversation: no run, no thread, no welcome.
# Its data channel carries only these frames. App -> server: {"op": "custom", name, value} (EnrolClientFrame).
# Server -> app: {"seq", "event": an AG-UI 1.0 CUSTOM event} with no threadId (EnrolServerFrame).
BLOCK = {"type": "string", "enum": ["A", "B", "C", "D", "E"]}
RATE = {"type": "number", "minimum": 0, "maximum": 1}
BY_SNR = obj({"clean": {**RATE, "description": "The clean-room rate from the 60-second check; when no check ran, the +10 dB rate as a lower bound."},
    "10": RATE, "5": RATE, "0": RATE}, ["clean", "10", "5", "0"], "Accept rate of the owner's trials per condition: clean, and babble at +10, +5 and 0 dB SNR.")
ENROL_REASONS = ["missing", "consent", "no_speech", "timeout", "not_reliable", "error"]
D["EnrolReason"] = {"type": "string", "enum": ENROL_REASONS, "description":
    "Why an enrolment ended with ok false. missing: blocks still needed (listed in missing); consent: voice consent withdrawn "
    "meanwhile, nothing stored; no_speech: no usable speech in the blocks; timeout: the 15-minute limit; not_reliable: prints "
    "were made and stored, but the route's gate cannot rely on them (the report is sent); error: the server failed handling "
    "the session (v1 sent this, or an error code, and none other can reach the phone)."}
ENROL = {}
ENROL["pisa.voice.enrol.quality"] = obj({"block": BLOCK, "voicedS": {"type": "number", "minimum": 0}, "snrDb": {"type": "number"},
    "clipped": {"type": "boolean"}, "ok": {"type": "boolean"}, "hint": {"type": ["string", "null"], "enum": ["louder", "quieter", "closer", "again", None]}},
    ["block", "voicedS", "snrDb", "clipped", "ok", "hint"], "After each block: how usable the recording was, and what to do better. Replaces enrol.quality.")
ENROL["pisa.voice.enrol.test_result"] = obj({"acceptedOwner": {"type": "integer", "minimum": 0}, "ownerTotal": {"type": "integer", "minimum": 0},
    "acceptedStrangers": {"type": "integer", "minimum": 0}, "strangersTotal": {"type": "integer", "minimum": 0}, "ok": {"type": "boolean"}, "bySnr": BY_SNR},
    ["acceptedOwner", "ownerTotal", "acceptedStrangers", "strangersTotal", "ok", "bySnr"],
    "The 60-second check scored: ok is >= 80% of the owner's trials accepted within 1 s and no stranger accepted. Replaces enrol.test_result.")
ENROL["pisa.voice.enrol.done"] = obj({"route": {"type": "string", "enum": ["speaker", "receiver", "airpods", "headphones", "car", "glasses"]}, "ok": {"type": "boolean"},
    "ownerFalseReject": RATE, "strangersPerMin": {"type": "number", "minimum": 0}, "bySnr": BY_SNR,
    "reason": {"$ref": "#/$defs/EnrolReason"},
    "missing": {"type": "array", "minItems": 1, "items": BLOCK, "description": "With reason missing: the blocks still needed."}},
    ["route", "ok"], "The verdict, the last frame: the server closes the peer after it. Replaces enrol.done.")
ENROL["pisa.voice.enrol.done"]["allOf"] = [
    {"if": {"properties": {"ok": {"const": True}}}, "then": {"required": ["ownerFalseReject", "strangersPerMin", "bySnr"], "not": {"required": ["reason"]}},
     "else": {"required": ["reason"]}},
    {"if": {"properties": {"reason": {"const": "missing"}}, "required": ["reason"]}, "then": {"required": ["missing"]}, "else": {"not": {"required": ["missing"]}}},
    {"if": {"properties": {"reason": {"const": "not_reliable"}}, "required": ["reason"]}, "then": {"required": ["ownerFalseReject", "strangersPerMin", "bySnr"]}}]
CENROL = {}
CENROL["pisa.voice.enrol.block"] = obj({"block": BLOCK, "state": {"type": "string", "enum": ["start", "end"]}}, ["block", "state"], "Around the reading of a script block. Replaces enrol.block.")
CENROL["pisa.voice.enrol.test"] = obj({"state": {"type": "string", "enum": ["start", "end"]}}, ["state"], "Around the reading of the script's test (no babble). Replaces enrol.test.")
CENROL["pisa.voice.enrol.finish"] = obj({}, [], "Sent whether the test passed or not: the server stores the prints. Replaces enrol.finish.")
CENROL["pisa.voice.enrol.cancel"] = obj({}, [], "Nothing is kept. Replaces enrol.cancel.")
for k, v in ENROL.items():
    D["s_" + k.replace(".", "_")] = v
for k, v in CENROL.items():
    D["c_" + k.replace(".", "_")] = v
D["EnrolServerEvent"] = {"description": "G5. An AG-UI CUSTOM event on an enrolment data channel; it must ALSO validate as an AG-UI 1.0 Event.",
    "type": "object", "required": ["type", "name", "value"], "properties": {"type": {"const": "CUSTOM"}, "timestamp": {"type": "integer"}, "name": {"enum": list(ENROL)}, "value": {"type": "object"}},
    "allOf": [{"if": {"properties": {"name": {"const": k}}}, "then": {"properties": {"value": {"$ref": "#/$defs/s_" + k.replace(".", "_")}}}} for k in ENROL]}
D["EnrolServerFrame"] = obj({"seq": {"type": "integer", "minimum": 1}, "event": {"$ref": "#/$defs/EnrolServerEvent"}}, ["seq", "event"],
    "G5. Server -> app on the data channel of a talk session opened with purpose enrol, and only there. No threadId and no runId: "
    "enrolment is not a conversation, so it opens no AG-UI stream and breaks no AG-UI lifecycle. seq grows by one per frame.")
D["EnrolClientFrame"] = {"type": "object", "required": ["op", "name", "value"], "additionalProperties": False,
    "description": "G5. App -> server on an enrolment data channel, and only there: the ClientCustom shape with the enrolment names.",
    "properties": {"op": {"const": "custom"}, "name": {"enum": list(CENROL)}, "value": {"type": "object"}},
    "allOf": [{"if": {"properties": {"name": {"const": k}}}, "then": {"properties": {"value": {"$ref": "#/$defs/c_" + k.replace(".", "_")}}}} for k in CENROL]}

schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:pisa:contract:2:extensions",
  "title": "PISA contract 2: pisa.* extensions, frames and negotiation",
  "description": "Everything CONTRACT-2 adds around AG-UI 1.0, A2UI 1.0 and MCP Apps 2026-01-26. Apache-2.0. Field names are camelCase like AG-UI.",
  "x-pisa-contract": "2.3", "x-pisa-required-features": REQUIRED_FEATURES, "x-pisa-approval-error-codes": APPROVAL_ERRORS, "x-pisa-enrol-reasons": ENROL_REASONS,
  "x-pisa-server-events": list(EV), "x-pisa-client-events": list(CEV),
  "x-pisa-enrol-server-events": list(ENROL), "x-pisa-enrol-client-events": list(CENROL), "x-pisa-extensions": EXT_NAMES,
  "$defs": D}
SCHEMA = schema

