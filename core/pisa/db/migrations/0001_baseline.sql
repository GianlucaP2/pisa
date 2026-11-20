-- Baseline schema: every table, index, constraint, function, trigger, view and seed row of a fresh database.
-- Later changes go in new files (0002_*, 0003_*, ...); this file is never edited once applied.
--
-- The database, its roles and their grants are created by the superuser in deploy/postgres/init, not here: this
-- file runs as the migration role, which owns what it creates. The default privileges set there give the
-- application role read and write access to these tables and sequences, and the read-only role SELECT.

-- pgvector: created by deploy/postgres/init as superuser; a no-op here when it exists.
CREATE EXTENSION IF NOT EXISTS vector;


-- ==========================================================================================================
-- Server metadata, settings and secrets
-- Small key-value stores the server reads at start.
-- ==========================================================================================================

-- Server facts written by code or by deploy scripts (key, value).
CREATE TABLE app_meta (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Small JSON settings; a missing key means the default in code.
CREATE TABLE app_settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Server-wide secrets other than provider keys, sealed with the secrets key.
CREATE TABLE server_secrets (
    name       text PRIMARY KEY,
    ciphertext bytea NOT NULL,
    meta       jsonb NOT NULL DEFAULT '{}',
    set_at     timestamptz NOT NULL DEFAULT now()
);


-- ==========================================================================================================
-- Model gateway
-- The model catalog, provider keys, the slot each use is routed to, and every call's cost.
-- ==========================================================================================================

-- Models known to the gateway, refreshed from the providers.
CREATE TABLE model_catalog (
    ref                 text PRIMARY KEY,
    provider            text NOT NULL,
    model_id            text NOT NULL,
    name                text NOT NULL,
    capabilities        text[] NOT NULL DEFAULT '{}',
    context_tokens      integer,
    input_usd_per_mtok  numeric(14,6),
    output_usd_per_mtok numeric(14,6),
    price_source        text,
    refreshed_at        timestamptz NOT NULL,
    missed_refreshes    integer NOT NULL DEFAULT 0
);
CREATE INDEX model_catalog_provider ON model_catalog (provider);

-- One API key per provider, sealed with the secrets key.
CREATE TABLE provider_keys (
    provider          text PRIMARY KEY,
    ciphertext        bytea NOT NULL,
    last4             text NOT NULL,
    set_at            timestamptz NOT NULL DEFAULT now(),
    last_test_ok      boolean,
    last_test_at      timestamptz,
    last_test_message text
);

-- Which model serves each use (slot), with an optional fallback.
CREATE TABLE model_slots (
    id           text PRIMARY KEY,
    model_ref    text,
    fallback_ref text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Every model call: tokens, cost, latency and the reservation it held.
CREATE TABLE llm_calls (
    id                 uuid PRIMARY KEY,
    slot               text NOT NULL,
    model_ref          text NOT NULL,
    status             text NOT NULL CHECK (status IN ('reserved', 'settled', 'failed', 'swept')),
    reserved_eur       numeric(12,6) NOT NULL,
    input_tokens       integer,
    output_tokens      integer,
    cost_eur           numeric(12,6),
    price_known        boolean NOT NULL,
    latency_ms         integer,
    error_code         text,
    trace_id           text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    settled_at         timestamptz,
    cache_read_tokens  integer,
    cache_write_tokens integer,
    budget             text NOT NULL DEFAULT 'owner' CHECK (budget IN ('owner', 'eval', 'night')),
    grown_at           timestamptz,
    job                text CHECK (job ~ '^[a-z_]{1,40}$')
);
CREATE INDEX llm_calls_budget_created ON llm_calls (budget, created_at);
CREATE INDEX llm_calls_created ON llm_calls (created_at);
CREATE INDEX llm_calls_job_created ON llm_calls (job, created_at) WHERE job IS NOT NULL;
CREATE INDEX llm_calls_reserved ON llm_calls (created_at) WHERE status = 'reserved';
CREATE INDEX llm_calls_slot_created ON llm_calls (slot, created_at);

-- Embeddings of the skills' key text, for retrieval.
CREATE TABLE skill_vectors (
    name       text PRIMARY KEY,
    key_hash   text NOT NULL,
    model_ref  text NOT NULL,
    embedding  vector NOT NULL,
    updated_at timestamptz NOT NULL
);


-- ==========================================================================================================
-- Places
-- Place providers and their usage, Pisa's place identities, provider content and caches.
-- ==========================================================================================================

-- Place providers, their sealed keys and the last test.
CREATE TABLE place_providers (
    provider          text PRIMARY KEY CHECK (provider IN ('apple', 'google', 'tavily')),
    ciphertext        bytea,
    last4             text,
    set_at            timestamptz,
    last_test_ok      boolean,
    last_test_at      timestamptz,
    last_test_message text
);

-- Every provider call by SKU, for usage and the monthly cap.
CREATE TABLE place_calls (
    id       bigserial PRIMARY KEY,
    provider text NOT NULL,
    sku      text NOT NULL,
    units    integer NOT NULL DEFAULT 1 CHECK (units >= 0),
    at       timestamptz NOT NULL,
    ok       boolean NOT NULL,
    reserved boolean NOT NULL DEFAULT false,
    budget   text NOT NULL DEFAULT 'owner' CHECK (budget IN ('owner', 'night'))
);
CREATE INDEX place_calls_at ON place_calls (at);

-- One Pisa identity per place.
CREATE TABLE places (
    id                 uuid PRIMARY KEY,
    name               text NOT NULL,
    category           text,
    address            text,
    lat                double precision,
    lon                double precision,
    coords_source      text CHECK (coords_source IN ('apple', 'google')),
    coords_at          timestamptz,
    apple_id           text UNIQUE,
    google_place_id    text UNIQUE,
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL,
    google_no_match_at timestamptz,
    coords_retry_at    timestamptz
);
CREATE INDEX places_lat ON places (lat);

-- Provider content about a place, kept briefly.
CREATE TABLE place_cache (
    place_id   uuid REFERENCES places (id) ON DELETE CASCADE,
    provider   text,
    kind       text CHECK (kind IN ('details', 'details_reviews')),
    content    jsonb NOT NULL,
    fetched_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (place_id, provider, kind)
);
CREATE INDEX place_cache_expires ON place_cache (expires_at);

-- Pisa's research summaries about a place, with sources.
CREATE TABLE place_insights (
    id         uuid PRIMARY KEY,
    place_id   uuid NOT NULL REFERENCES places (id) ON DELETE CASCADE,
    question   text,
    summary    text NOT NULL,
    facts      jsonb NOT NULL,
    sources    jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX place_insights_place ON place_insights (place_id, created_at DESC);

-- Location text resolved to a place.
CREATE TABLE place_resolutions (
    text_key    text PRIMARY KEY,
    place_id    uuid REFERENCES places (id) ON DELETE CASCADE,
    resolved_at timestamptz NOT NULL,
    failed_at   timestamptz,
    elsewhere   text,
    here_city   text
);

-- A place's menu as Pisa read it.
CREATE TABLE place_menus (
    place_id   uuid PRIMARY KEY REFERENCES places (id) ON DELETE CASCADE,
    url        text NOT NULL,
    highlights jsonb NOT NULL DEFAULT '[]',
    read_at    timestamptz NOT NULL
);

-- Travel times per (from, to, mode, departure slot).
CREATE TABLE travel_cache (
    from_key        text,
    to_key          text,
    mode            text,
    slot            timestamptz,
    minutes         integer,
    traffic_minutes integer,
    provider        text NOT NULL,
    created_at      timestamptz NOT NULL,
    PRIMARY KEY (from_key, to_key, mode, slot)
);
CREATE INDEX travel_cache_created ON travel_cache (created_at);
CREATE INDEX travel_cache_slot ON travel_cache (slot);

-- Weather forecasts per grid cell, kept briefly.
CREATE TABLE weather_cache (
    cell       text PRIMARY KEY,
    forecast   jsonb NOT NULL,
    fetched_at timestamptz NOT NULL
);

-- The latest copy of a public data source.
CREATE TABLE public_cache (
    key        text PRIMARY KEY CHECK ((char_length(key) >= 1) AND (char_length(key) <= 120)),
    body       bytea,
    fetched_at timestamptz,
    failed_at  timestamptz,
    error      text CHECK (char_length(error) <= 300)
);

-- Errand combinations suggested, at most one a day.
CREATE TABLE errand_suggestions (
    day        date PRIMARY KEY,
    combo_key  text NOT NULL,
    text       text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX errand_suggestions_key ON errand_suggestions (combo_key, day DESC);


-- ==========================================================================================================
-- Devices, pairing and presence
-- Paired devices, their signing keys, what they report, and the user's current time zone.
-- ==========================================================================================================

-- A paired device and its token hash.
CREATE TABLE devices (
    id               uuid PRIMARY KEY,
    name             text NOT NULL,
    platform         text NOT NULL CHECK (platform IN ('ios', 'android', 'web', 'other')),
    token_hash       text NOT NULL UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz,
    revoked_at       timestamptz,
    apns_token       text,
    apns_environment text CHECK (apns_environment IN ('development', 'production')),
    push_updated_at  timestamptz,
    paired_via       text NOT NULL DEFAULT 'host' CHECK (paired_via IN ('host', 'app'))
);
CREATE UNIQUE INDEX devices_apns_token_live ON devices (apns_token) WHERE (revoked_at IS NULL)
        AND (apns_token IS NOT NULL);

-- One-use pairing codes.
CREATE TABLE pairing_codes (
    code_hash      text PRIMARY KEY,
    label          text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    attempts       integer NOT NULL DEFAULT 0,
    used_at        timestamptz,
    invalidated_at timestamptz,
    origin         text NOT NULL DEFAULT 'host' CHECK (origin IN ('host', 'app'))
);

-- Wrong pairing codes counted per source, for the lockout.
CREATE TABLE pairing_failures (
    source text NOT NULL CHECK ((length(source) >= 1) AND (length(source) <= 200)),
    at     timestamptz NOT NULL
);
CREATE INDEX pairing_failures_source_at ON pairing_failures (source, at);

-- The phone's P-256 public keys, used to verify signed approvals.
CREATE TABLE phone_keys (
    key_id     text PRIMARY KEY CHECK ((char_length(key_id) >= 1) AND (char_length(key_id) <= 128)),
    public_key bytea NOT NULL,
    label      text CHECK (char_length(label) <= 80),
    device_id  uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL
);

-- One-use challenges the phone signs; `canonical` is what it shows.
CREATE TABLE phone_challenges (
    nonce      text PRIMARY KEY,
    action     text NOT NULL CHECK (action IN ('autonomy', 'add_key', 'skill', 'approval', 'device')),
    canonical  text NOT NULL,
    subject    jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);
CREATE INDEX phone_challenges_expires ON phone_challenges (expires_at);

-- The latest presence per device (foreground, route, worn, companion).
CREATE TABLE device_presence (
    device_id  uuid PRIMARY KEY REFERENCES devices (id) ON DELETE CASCADE,
    app        text NOT NULL CHECK (app IN ('foreground', 'background')),
    glasses    text CHECK (glasses IN ('on', 'off')),
    updated_at timestamptz NOT NULL,
    companion  text CHECK (companion IN ('on', 'off')),
    earbuds    text CHECK (earbuds IN ('connected', 'disconnected')),
    state      jsonb
);

-- Location fixes uploaded by a device.
CREATE TABLE device_locations (
    id          bigserial PRIMARY KEY,
    device_id   uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    lat         double precision NOT NULL CHECK ((lat >= -90) AND (lat <= 90)),
    lon         double precision NOT NULL CHECK ((lon >= -180) AND (lon <= 180)),
    accuracy_m  double precision NOT NULL CHECK (accuracy_m >= 0),
    measured_at timestamptz NOT NULL
);
CREATE INDEX device_locations_device_measured ON device_locations (device_id, measured_at DESC);
CREATE UNIQUE INDEX device_locations_device_measured_unique ON device_locations (device_id, measured_at);
CREATE INDEX device_locations_measured ON device_locations (measured_at);

-- Visits reported by a device: one per (device, arrival).
CREATE TABLE device_visits (
    device_id   uuid REFERENCES devices (id) ON DELETE CASCADE,
    lat         double precision NOT NULL CHECK ((lat >= -90) AND (lat <= 90)),
    lon         double precision NOT NULL CHECK ((lon >= -180) AND (lon <= 180)),
    accuracy_m  double precision NOT NULL CHECK (accuracy_m >= 0),
    arrived_at  timestamptz,
    departed_at timestamptz,
    PRIMARY KEY (device_id, arrived_at)
);
CREATE INDEX device_visits_arrived ON device_visits (arrived_at);

-- Stays derived from fixes and visits, linked to a known place.
CREATE TABLE stays (
    id           uuid PRIMARY KEY,
    device_id    uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    arrived_at   timestamptz NOT NULL,
    departed_at  timestamptz,
    lat          double precision NOT NULL,
    lon          double precision NOT NULL,
    accuracy_m   double precision NOT NULL,
    last_seen_at timestamptz NOT NULL,
    place_id     uuid REFERENCES places (id) ON DELETE SET NULL
);
CREATE INDEX stays_arrived ON stays (arrived_at);
CREATE INDEX stays_device ON stays (device_id, arrived_at);
CREATE INDEX stays_place ON stays (place_id) WHERE place_id IS NOT NULL;

-- Diagnostics from the phone: payload types and call stacks only.
CREATE TABLE device_diagnostics (
    id             uuid PRIMARY KEY,
    device_id      uuid REFERENCES devices (id) ON DELETE SET NULL,
    kind           text NOT NULL CHECK (kind IN ('crash', 'hang', 'cpu', 'disk')),
    app_version    text NOT NULL,
    os_version     text NOT NULL,
    exception_type text,
    top_frames     text[] NOT NULL DEFAULT '{}',
    payload        jsonb NOT NULL,
    received_at    timestamptz NOT NULL
);
CREATE INDEX device_diagnostics_received ON device_diagnostics (received_at);

-- The phone's own daily metrics, numbers only.
CREATE TABLE device_metrics (
    device_id   uuid REFERENCES devices (id) ON DELETE CASCADE,
    day         date,
    app_version text NOT NULL,
    os_version  text NOT NULL,
    numbers     jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (device_id, day)
);

-- Allowlisted app events from the phone, no content.
CREATE TABLE device_app_events (
    id              uuid PRIMARY KEY,
    device_id       uuid REFERENCES devices (id) ON DELETE SET NULL,
    event           text NOT NULL CHECK (event IN ('companion_refused', 'companion_retry',
                                         'companion_recovered', 'companion_stopped',
                                         'notification_dismissed', 'notification_opened', 'call_started',
                                         'call_ended', 'activity', 'focus_on', 'focus_off',
                                         'microphone_mode')),
    code            text,
    site            text,
    state           text,
    background      boolean,
    at              timestamptz NOT NULL,
    received_at     timestamptz NOT NULL,
    notification_id uuid
);
CREATE INDEX device_app_events_device ON device_app_events (device_id, received_at);
CREATE INDEX device_app_events_event_at ON device_app_events (event, at);
CREATE INDEX device_app_events_received ON device_app_events (received_at);

-- Live Activity tokens per device and what was last sent.
CREATE TABLE device_live_activity (
    device_id  uuid REFERENCES devices (id) ON DELETE CASCADE,
    kind       text CHECK (kind IN ('update', 'start')),
    token      text NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (device_id, kind)
);

-- The Live Activity's current content (one row).
CREATE TABLE live_activity_state (
    device_id  uuid PRIMARY KEY REFERENCES devices (id) ON DELETE CASCADE,
    content    jsonb,
    sent_at    timestamptz,
    started_on date,
    ended_on   date
);

-- Where the user is: the phone's time zone and the one before (one row).
CREATE TABLE owner_context (
    id            boolean DEFAULT true PRIMARY KEY CHECK (id),
    tz            text NOT NULL CHECK ((length(tz) >= 1) AND (length(tz) <= 64)),
    region        text CHECK (region ~ '^[A-Z]{2}$'),
    hour_cycle    text CHECK (hour_cycle IN ('h23', 'h12')),
    device_id     uuid REFERENCES devices (id) ON DELETE SET NULL,
    updated_at    timestamptz NOT NULL,
    previous_tz   text CHECK ((length(previous_tz) >= 1) AND (length(previous_tz) <= 64)),
    tz_changed_at timestamptz
);


-- ==========================================================================================================
-- Conversations and turns
-- Conversation history, turns, talk sessions and what a turn showed.
-- ==========================================================================================================

-- A conversation and its running summary.
CREATE TABLE conversations (
    id              uuid PRIMARY KEY,
    device_id       uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    started_at      timestamptz NOT NULL DEFAULT now(),
    last_message_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_device_last ON conversations (device_id, last_message_at DESC);

-- Each message of a conversation, with the model's message JSON.
CREATE TABLE messages (
    id              bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('user', 'assistant')),
    content         text NOT NULL,
    lang            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    quoted          boolean NOT NULL DEFAULT false,
    model_messages  jsonb,
    outside_kinds   text[] NOT NULL DEFAULT '{}',
    meant           text,
    corrections     jsonb,
    origin          text NOT NULL DEFAULT 'phone' CHECK (origin IN ('phone', 'watch'))
);
CREATE INDEX messages_conversation_created ON messages (conversation_id, created_at, id);
CREATE INDEX messages_created_at ON messages (created_at);

-- A REST turn, idempotent per (device, client turn id).
CREATE TABLE turns (
    id              uuid PRIMARY KEY,
    device_id       uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    client_turn_id  uuid NOT NULL,
    origin          text NOT NULL CHECK (origin IN ('phone', 'watch')),
    lang            text NOT NULL CHECK (lang IN ('it-IT', 'en-US')),
    state           text NOT NULL CHECK (state IN ('working', 'done', 'failed')),
    say             text CHECK (char_length(say) <= 4000),
    sheet_id        uuid,
    notification_id uuid,
    error_code      text,
    created_at      timestamptz NOT NULL,
    done_at         timestamptz,
    sensitive       boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX turns_client ON turns (device_id, client_turn_id);

-- The sheet a REST turn showed, read by id.
CREATE TABLE turn_sheets (
    id         uuid PRIMARY KEY,
    turn_id    uuid NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    data       jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX turn_sheets_turn ON turn_sheets (turn_id);

-- One row per live talk session: ids, times and counts only.
CREATE TABLE talk_sessions (
    id                  uuid PRIMARY KEY,
    device_id           uuid REFERENCES devices (id) ON DELETE SET NULL,
    talker              text NOT NULL CHECK (talker IN ('xai', 'gemini')),
    voice               text NOT NULL,
    trigger             text NOT NULL CHECK (trigger IN ('press', 'wake', 'intent', 'notice', 'proactive',
                                             'glasses')),
    lang                text NOT NULL,
    started_at          timestamptz NOT NULL,
    ended_at            timestamptz,
    end_reason          text CHECK (end_reason IN ('idle', 'owner', 'budget', 'error', 'fallback', 'route')),
    seconds             numeric(10,1),
    cost_eur            numeric(12,6),
    llm_call_id         uuid,
    grounding_incidents integer NOT NULL DEFAULT 0,
    timeline            jsonb,
    closed_by           text CHECK (closed_by IN ('phone', 'replaced', 'peer', 'talker', 'idle', 'budget',
                                    'error', 'server')),
    close_detail        text,
    route               text,
    ask_id              uuid
);
CREATE INDEX talk_sessions_started ON talk_sessions (started_at DESC);

-- One row per exchange in a talk session, with its latencies.
CREATE TABLE talk_turns (
    session_id       uuid REFERENCES talk_sessions (id) ON DELETE CASCADE,
    n                integer,
    owner_end_at     timestamptz NOT NULL,
    first_sound_ms   integer,
    whisper_ms       integer,
    whisper_kind     text CHECK (whisper_kind IN ('fact', 'say', 'hold', 'correct', 'done', 'ask')),
    asked_brain      boolean NOT NULL DEFAULT false,
    first_content_ms integer,
    first_content_by text CHECK (first_content_by IN ('reflex', 'brain', 'next')),
    reflex_ms        integer,
    route            text,
    parts            jsonb,
    failed           smallint NOT NULL DEFAULT 0 CHECK (failed >= 0),
    unbacked         smallint NOT NULL DEFAULT 0 CHECK (unbacked >= 0),
    redone           boolean NOT NULL DEFAULT false,
    ready            text CHECK (ready IN ('hit', 'stale')),
    reflex_by        text CHECK (reflex_by IN ('model', 'slow', 'rules', 'final')),
    PRIMARY KEY (session_id, n)
);

-- Requests to open a talk, and what was said in it.
CREATE TABLE talk_asks (
    id              uuid PRIMARY KEY,
    device_id       uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    notification_id uuid NOT NULL UNIQUE,
    why             text NOT NULL CHECK (why IN ('leave_now', 'reply_waiting', 'approval_lapsing',
                                         'check_in')),
    asked_at        timestamptz NOT NULL,
    opened_at       timestamptz,
    said_at         timestamptz,
    pushed          boolean NOT NULL DEFAULT false
);
CREATE INDEX talk_asks_device_asked ON talk_asks (device_id, asked_at);

-- The user's corrections of what Pisa understood: heard X, meant Y.
CREATE TABLE understand_corrections (
    id         uuid PRIMARY KEY,
    heard      text NOT NULL CHECK ((length(heard) >= 1) AND (length(heard) <= 200)),
    meant      text NOT NULL CHECK ((length(meant) >= 1) AND (length(meant) <= 200)),
    lang       text NOT NULL,
    via        text CHECK (via IN ('sound', 'spelling', 'model')),
    created_at timestamptz NOT NULL
);
CREATE INDEX understand_corrections_at ON understand_corrections (created_at);

-- What a research turn found, kept with its conversation.
CREATE TABLE research_findings (
    id              uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL,
    data            jsonb NOT NULL
);
CREATE INDEX research_findings_conversation ON research_findings (conversation_id, created_at DESC);

-- Drawings shown in a conversation, kept so the last can be redrawn.
CREATE TABLE drawings (
    id              uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL,
    title           text NOT NULL CHECK (length(title) <= 120),
    summary         text NOT NULL CHECK (length(summary) <= 400),
    kind            text NOT NULL CHECK (kind IN ('flow', 'schema', 'chart', 'comparison', 'timeline',
                                         'explorer', 'other')),
    html            text NOT NULL CHECK (octet_length(html) <= 200000),
    data            jsonb NOT NULL,
    revision        integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    basis           text CHECK (basis = 'knowledge')
);
CREATE INDEX drawings_conversation ON drawings (conversation_id, created_at DESC);

-- Outside values a conversation read, and where each came from.
CREATE TABLE outside_values (
    conversation_id uuid REFERENCES conversations (id) ON DELETE CASCADE,
    what            text CHECK (what IN ('address', 'link', 'phone', 'amount', 'date')),
    value           text CHECK (char_length(value) <= 300),
    source          text NOT NULL CHECK (char_length(source) <= 120),
    noted_at        timestamptz NOT NULL,
    PRIMARY KEY (conversation_id, what, value)
);

-- Counts of hidden text left out of what Pisa read, per day and source.
CREATE TABLE pisa_hidden_text (
    day    date,
    source text CHECK (source IN ('mail', 'page')),
    runs   integer NOT NULL CHECK (runs >= 0),
    PRIMARY KEY (day, source)
);

-- Notes the user leaves for the maintainers.
CREATE TABLE owner_feedback (
    id        uuid PRIMARY KEY,
    device_id uuid REFERENCES devices (id) ON DELETE SET NULL,
    said_at   timestamptz NOT NULL,
    words     text NOT NULL
);
CREATE INDEX owner_feedback_said ON owner_feedback (said_at);


-- ==========================================================================================================
-- Approvals, decisions and autonomy
-- Actions outside Pisa and their approvals, Pisa's decision log, stops and earned autonomy.
-- ==========================================================================================================

-- An action outside Pisa with its exact effect, run only on the user's yes.
CREATE TABLE approvals (
    id                 uuid PRIMARY KEY,
    kind               text NOT NULL CHECK (kind IN ('calendar_create', 'calendar_move', 'calendar_cancel',
                                            'email_send')),
    summary            text NOT NULL,
    details            jsonb NOT NULL DEFAULT '[]',
    payload            jsonb NOT NULL,
    status             text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected',
                                                              'expired', 'done', 'failed', 'superseded')),
    result             text,
    requires_biometric boolean NOT NULL DEFAULT false,
    undo_minutes       integer,
    undo               jsonb,
    undo_until         timestamptz,
    undone_at          timestamptz,
    device_id          uuid REFERENCES devices (id) ON DELETE SET NULL,
    lang               text NOT NULL DEFAULT 'it-IT',
    created_at         timestamptz NOT NULL,
    expires_at         timestamptz NOT NULL,
    decided_at         timestamptz,
    decided_auth       text CHECK (decided_auth IN ('tap', 'voice', 'biometric', 'words', 'autonomy:tell',
                                   'autonomy:silent')),
    executed_at        timestamptz,
    origin             text NOT NULL DEFAULT 'proposal' CHECK (origin IN ('words', 'proposal', 'held')),
    lapse_noticed_at   timestamptz,
    action_hash        text,
    tier               smallint NOT NULL DEFAULT 1 CHECK (tier IN (0, 1, 2)),
    read_out_at        timestamptz,
    voice_until        timestamptz,
    verified           text CHECK (verified IN ('verified', 'not_verified')),
    decided_origin     text CHECK (decided_origin IN ('phone', 'watch'))
);
CREATE INDEX approvals_created ON approvals (created_at DESC);
CREATE INDEX approvals_device_pending ON approvals (device_id, created_at DESC) WHERE status = 'pending';
CREATE INDEX approvals_executed ON approvals (executed_at) WHERE executed_at IS NOT NULL;
CREATE INDEX approvals_pending ON approvals (expires_at) WHERE status = 'pending';
CREATE INDEX approvals_undone ON approvals (undone_at) WHERE undone_at IS NOT NULL;

-- Pisa's decision log: what Pisa did, proposed, showed or said unasked.
CREATE TABLE pisa_decisions (
    id            uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    n             bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    at            timestamptz NOT NULL,
    trace_id      text,
    device_id     uuid REFERENCES devices (id) ON DELETE SET NULL,
    move          text NOT NULL CHECK (move IN ('act', 'propose', 'show', 'ask', 'look', 'speak', 'defer',
                                       'drop', 'promise')),
    tool          text NOT NULL,
    ref           text,
    purpose       text CHECK (char_length(purpose) <= 120),
    provenance    text NOT NULL CHECK (provenance IN ('owner_words', 'pisa_proposal', 'outside_text',
                                       'pisa_initiative')),
    rail          text,
    outcome       text,
    outcome_at    timestamptz,
    present_id    uuid,
    outside_kinds text[],
    origin        text NOT NULL DEFAULT 'phone' CHECK (origin IN ('phone', 'watch'))
);
CREATE INDEX pisa_decisions_different ON pisa_decisions (n) WHERE rail = 'something different';
CREATE INDEX pisa_decisions_n ON pisa_decisions (n DESC);
CREATE INDEX pisa_decisions_places_shown ON pisa_decisions (n) WHERE (move = 'show') AND (tool = 'show_places');
CREATE INDEX pisa_decisions_present ON pisa_decisions (present_id) WHERE present_id IS NOT NULL;
CREATE INDEX pisa_decisions_ref ON pisa_decisions (ref) WHERE ref IS NOT NULL;

-- Stop-everything: the latest row without `resumed_at` is in force.
CREATE TABLE pisa_stops (
    id             uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    scope          text NOT NULL DEFAULT 'all' CHECK (scope = 'all'),
    stopped_at     timestamptz NOT NULL,
    stopped_by     text NOT NULL CHECK (stopped_by IN ('app', 'voice')),
    resumed_at     timestamptz,
    resumed_by     text CHECK (resumed_by IN ('app', 'voice')),
    stopped_origin text NOT NULL DEFAULT 'phone' CHECK (stopped_origin IN ('phone', 'watch')),
    resumed_origin text CHECK (resumed_origin IN ('phone', 'watch'))
);
CREATE UNIQUE INDEX pisa_stops_one_open ON pisa_stops ((true)) WHERE resumed_at IS NULL;

-- The autonomy level per action type, and Pisa's proposals.
CREATE TABLE autonomy_levels (
    type           text PRIMARY KEY CHECK (type IN ('calendar_create', 'calendar_move', 'calendar_cancel',
                                           'task_done', 'task_close', 'email_draft', 'email_send')),
    level          text NOT NULL DEFAULT 'ask' CHECK (level IN ('ask', 'tell', 'silent', 'ledger')),
    since          timestamptz NOT NULL,
    changed_by     text NOT NULL DEFAULT 'default' CHECK (changed_by IN ('default', 'signed', 'voice')),
    proposed_level text CHECK (proposed_level IN ('tell', 'silent', 'ledger')),
    proposed_at    timestamptz,
    CONSTRAINT autonomy_levels_check CHECK ((type <> 'email_send') OR (level = 'ask'))
);

-- Autonomy a type earned, announced once.
CREATE TABLE autonomy_unlocks (
    id             uuid PRIMARY KEY,
    type           text NOT NULL,
    from_level     text NOT NULL CHECK (from_level IN ('ask', 'tell', 'silent', 'ledger')),
    to_level       text NOT NULL CHECK (to_level IN ('tell', 'silent', 'ledger')),
    at             timestamptz NOT NULL,
    announced_week text CHECK (announced_week ~ '^[0-9]{4}-W[0-9]{2}$')
);
CREATE INDEX autonomy_unlocks_open ON autonomy_unlocks (at) WHERE announced_week IS NULL;

-- Every hold and release, with ids, kinds and reasons only.
CREATE TABLE guardian_items (
    id           uuid PRIMARY KEY,
    ref          text NOT NULL UNIQUE CHECK (char_length(ref) <= 80),
    path         text NOT NULL CHECK (char_length(path) <= 60),
    kind         text NOT NULL CHECK (char_length(kind) <= 20),
    decision     text NOT NULL CHECK (decision IN ('now', 'breakpoint', 'digest')),
    reason       text NOT NULL CHECK (char_length(reason) <= 60),
    score        jsonb NOT NULL,
    override     text CHECK (override IN ('always', 'never')),
    rule_source  text CHECK (rule_source IN ('settings', 'words')),
    counted      boolean NOT NULL DEFAULT true,
    decided_at   timestamptz NOT NULL,
    released_at  timestamptz,
    released_how text CHECK (char_length(released_how) <= 60),
    lapsed_at    timestamptz,
    outcome      text CHECK (outcome IN ('replied', 'tapped', 'dismissed', 'ignored')),
    outcome_at   timestamptz
);
CREATE INDEX guardian_items_decided ON guardian_items (decided_at);
CREATE INDEX guardian_items_kind_outcome ON guardian_items (kind, outcome_at DESC) WHERE outcome IS NOT NULL;

-- The user's deletions, run in the background: state and counts only.
CREATE TABLE erase_jobs (
    id          uuid PRIMARY KEY,
    family      text NOT NULL,
    before      date NOT NULL,
    tz          text NOT NULL,
    state       text NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed')),
    count       integer CHECK (count >= 0),
    failure     text,
    tables      jsonb NOT NULL DEFAULT '{}',
    children    jsonb NOT NULL DEFAULT '{}',
    device_id   uuid,
    created_at  timestamptz NOT NULL,
    finished_at timestamptz
);
CREATE INDEX erase_jobs_created ON erase_jobs (created_at DESC);
CREATE UNIQUE INDEX erase_jobs_one_live ON erase_jobs (family, before) WHERE state IN ('queued', 'running');


-- ==========================================================================================================
-- Mail, calendar and Google connectors
-- Google credentials and accounts, the synced mail and calendars, and Pisa's drafts.
-- ==========================================================================================================

-- OAuth clients for a connector; the web client's secret is sealed.
CREATE TABLE connector_credentials (
    connector             text PRIMARY KEY,
    web_client_id         text NOT NULL,
    web_client_secret_enc bytea NOT NULL,
    ios_client_id         text NOT NULL,
    set_at                timestamptz NOT NULL DEFAULT now()
);

-- A connected account; its tokens are sealed.
CREATE TABLE connector_accounts (
    connector          text PRIMARY KEY,
    account            text,
    scopes             text[] NOT NULL DEFAULT '{}',
    refresh_token_enc  bytea,
    access_token_enc   bytea,
    access_expires_at  timestamptz,
    status             text NOT NULL CHECK (status IN ('connected', 'error')),
    message            text,
    needs_reconnect    boolean NOT NULL DEFAULT false,
    connected_at       timestamptz NOT NULL DEFAULT now(),
    gmail_history_id   text,
    gmail_synced_at    timestamptz,
    calendar_synced_at timestamptz,
    gmail_failures     integer NOT NULL DEFAULT 0,
    calendar_failures  integer NOT NULL DEFAULT 0
);

-- Synced email: headers, snippet, labels and Pisa's triage.
CREATE TABLE email_messages (
    id              text PRIMARY KEY,
    thread_id       text NOT NULL,
    from_name       text,
    from_address    text,
    to_addresses    text[] NOT NULL DEFAULT '{}',
    subject         text NOT NULL DEFAULT '',
    sent_at         timestamptz NOT NULL,
    labels          text[] NOT NULL DEFAULT '{}',
    snippet         text NOT NULL DEFAULT '',
    body            text,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    intake_status   text NOT NULL DEFAULT 'pending' CHECK (intake_status IN ('pending', 'done', 'skipped',
                                                           'failed')),
    intake_category text,
    intake_error    text,
    intake_at       timestamptz,
    intake_reason   text,
    intake_notice   text,
    intake_priority smallint CHECK (intake_priority IN (1, 2, 3)),
    intake_gist     jsonb,
    intake_keys     jsonb,
    intake_reading  jsonb,
    trip_read_at    timestamptz,
    radar_read_at   timestamptz
);
CREATE INDEX email_messages_fetched ON email_messages (fetched_at);
CREATE INDEX email_messages_intake_at ON email_messages (intake_at);
CREATE INDEX email_messages_pending ON email_messages (sent_at) WHERE intake_status = 'pending';
CREATE INDEX email_messages_radar_unread ON email_messages (sent_at) WHERE radar_read_at IS NULL;
CREATE INDEX email_messages_sender ON email_messages (lower(from_address), sent_at DESC);
CREATE INDEX email_messages_sent ON email_messages (sent_at DESC);
CREATE INDEX email_messages_sent_by_him ON email_messages (sent_at DESC) WHERE 'SENT' = ANY (labels);
CREATE INDEX email_messages_thread ON email_messages (thread_id);

-- An email's PDF attachments, metadata only.
CREATE TABLE email_attachments (
    email_id text REFERENCES email_messages (id) ON DELETE CASCADE,
    part_id  text CHECK ((char_length(part_id) >= 1) AND (char_length(part_id) <= 40)),
    filename text NOT NULL CHECK ((char_length(filename) >= 1) AND (char_length(filename) <= 200)),
    mime     text NOT NULL CHECK (char_length(mime) <= 100),
    size     integer NOT NULL CHECK (size >= 0),
    PRIMARY KEY (email_id, part_id)
);

-- Addresses the user has written to.
CREATE TABLE email_correspondents (
    address text PRIMARY KEY,
    last_at timestamptz NOT NULL
);

-- The user's email rules by id, and the tasks a rule dismissed.
CREATE TABLE email_rule_log (
    rule_id      uuid PRIMARY KEY,
    made_at      timestamptz NOT NULL,
    dismissed    uuid[] NOT NULL DEFAULT '{}'::uuid[],
    forgotten_at timestamptz
);

-- Replies Pisa drafted in Gmail; nothing is sent without an approval.
CREATE TABLE email_drafts (
    id                uuid PRIMARY KEY,
    gmail_draft_id    text NOT NULL,
    thread_id         text,
    reply_to_id       text,
    in_reply_to       text NOT NULL,
    references_header text NOT NULL DEFAULT '',
    to_addresses      text[] NOT NULL,
    cc_addresses      text[] NOT NULL DEFAULT '{}',
    outsiders         text[] NOT NULL DEFAULT '{}',
    subject           text NOT NULL,
    body              text NOT NULL,
    version           integer NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    sent_at           timestamptz,
    approval_id       uuid REFERENCES approvals (id) ON DELETE SET NULL,
    attachments       jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(attachments) = 'array'),
    outside           jsonb NOT NULL DEFAULT '[]'
);
CREATE INDEX email_drafts_latest ON email_drafts (updated_at DESC) WHERE sent_at IS NULL;

-- Mail Pisa sent on the user's yes, which the readers skip.
CREATE TABLE pisa_sent_mail (
    gmail_message_id text PRIMARY KEY,
    draft_id         text,
    sent_at          timestamptz NOT NULL
);

-- The user's calendars and Pisa's access role in each.
CREATE TABLE calendars (
    id           text PRIMARY KEY,
    name         text NOT NULL,
    sync_token   text,
    window_start date,
    synced_at    timestamptz,
    access_role  text
);

-- Synced calendar events, with Pisa's own fields.
CREATE TABLE calendar_events (
    calendar_id        text REFERENCES calendars (id) ON DELETE CASCADE,
    id                 text,
    title              text NOT NULL,
    starts_at          timestamptz NOT NULL,
    ends_at            timestamptz NOT NULL,
    all_day            boolean NOT NULL,
    start_date         date,
    end_date           date,
    location           text,
    status             text NOT NULL,
    updated            timestamptz,
    place_id           uuid REFERENCES places (id) ON DELETE SET NULL,
    place_text         text,
    attendees          smallint,
    accepted           boolean,
    kind_owner         text CHECK (kind_owner IN ('fixed', 'window', 'soft')),
    min_stay_owner     integer CHECK ((min_stay_owner >= 0) AND (min_stay_owner <= 1440)),
    attended_at        timestamptz,
    guests             jsonb,
    recurring_event_id text,
    event_type         text,
    made_by_pisa          boolean NOT NULL DEFAULT false,
    visibility         text,
    outside            boolean NOT NULL DEFAULT false,
    ical_uid           text,
    creator_self       boolean,
    organizer_self     boolean,
    PRIMARY KEY (calendar_id, id)
);
CREATE INDEX calendar_events_place ON calendar_events (place_id) WHERE place_id IS NOT NULL;
CREATE INDEX calendar_events_starts ON calendar_events (starts_at);

CREATE FUNCTION event_outside_ledger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO event_outside (event_id) VALUES (NEW.id) ON CONFLICT DO NOTHING;
    IF NEW.recurring_event_id IS NOT NULL THEN
        INSERT INTO event_outside (event_id) VALUES (NEW.recurring_event_id) ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER event_outside_ledger AFTER INSERT OR UPDATE OF outside ON calendar_events
    FOR EACH ROW WHEN (new.outside)
    EXECUTE FUNCTION event_outside_ledger();

-- Every event or series id ever marked as written from outside text.
CREATE TABLE event_outside (
    event_id  text PRIMARY KEY,
    marked_at timestamptz NOT NULL DEFAULT now()
);


-- ==========================================================================================================
-- Tasks
-- The user's tasks, bulk changes and closures, with their undo windows.
-- ==========================================================================================================

-- A task: from the user's words, an email, or Pisa.
CREATE TABLE tasks (
    id                  uuid PRIMARY KEY,
    title               text NOT NULL,
    notes               text,
    status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dismissed',
                                                            'deleted')),
    due_at              timestamptz,
    due_kind            text NOT NULL DEFAULT 'none' CHECK (due_kind IN ('none', 'date', 'datetime')),
    duration_min        integer,
    place_text          text,
    priority            smallint NOT NULL DEFAULT 2 CHECK (priority IN (1, 2, 3)),
    source_kind         text NOT NULL CHECK (source_kind IN ('voice', 'email', 'manual', 'calendar')),
    source_ref          text,
    source_label        text,
    title_key           text NOT NULL,
    confidence          real,
    created_at          timestamptz NOT NULL,
    updated_at          timestamptz NOT NULL,
    place_id            uuid REFERENCES places (id) ON DELETE SET NULL,
    place_text_resolved text,
    place_elsewhere     text,
    repeat              jsonb,
    series_id           uuid,
    previous_id         uuid REFERENCES tasks (id) ON DELETE SET NULL,
    place_checked_in    text,
    place_by_owner      boolean NOT NULL DEFAULT false,
    priority_reason     text CHECK (length(priority_reason) <= 300),
    priority_source     text NOT NULL DEFAULT 'pisa' CHECK (priority_source IN ('pisa', 'owner')),
    deleted_at          timestamptz,
    kept_at             timestamptz,
    aging_asked_at      timestamptz,
    expected_from       timestamptz,
    expected_until      timestamptz,
    deadline_source     text CHECK (deadline_source IN ('owner', 'email', 'web', 'pisa')),
    due_date            date,
    done_at             timestamptz,
    country             text CHECK (country IN ('IT', 'US')),
    country_checked     boolean NOT NULL DEFAULT false,
    country_by_owner    boolean NOT NULL DEFAULT false,
    private             boolean NOT NULL DEFAULT false,
    CONSTRAINT tasks_check CHECK ((due_kind = 'none') = (due_at IS NULL)),
    CONSTRAINT tasks_duration_min_range CHECK ((duration_min IS NULL) OR ((duration_min >= 5)
                                               AND (duration_min <= 480))),
    CONSTRAINT tasks_expected_window CHECK (((expected_from IS NULL) AND (expected_until IS NULL))
                                            OR ((expected_from IS NOT NULL)
                                            AND (expected_until IS NOT NULL)
                                            AND (expected_until > expected_from)))
);
CREATE UNIQUE INDEX tasks_email_dedupe ON tasks (source_ref, title_key) WHERE source_kind = 'email';
CREATE UNIQUE INDEX tasks_one_next ON tasks (previous_id) WHERE previous_id IS NOT NULL;
CREATE INDEX tasks_open_due ON tasks (due_at) WHERE status = 'open';
CREATE INDEX tasks_place ON tasks (place_id) WHERE place_id IS NOT NULL;

CREATE FUNCTION tasks_done_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.status = 'done' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'done') THEN
        NEW.done_at := NEW.updated_at;
    ELSIF NEW.status <> 'done' THEN
        NEW.done_at := NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER tasks_done_at BEFORE INSERT OR UPDATE OF status ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION tasks_done_at();

CREATE FUNCTION tasks_due_date() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    now_tz  text;
    prev_tz text;
BEGIN
    IF NEW.due_kind IS DISTINCT FROM 'date' OR NEW.due_at IS NULL THEN
        NEW.due_date := NULL;
        RETURN NEW;
    END IF;
    SELECT tz, previous_tz INTO now_tz, prev_tz FROM owner_context WHERE id;
    IF now_tz IS NULL THEN
        RETURN NEW;  -- no zone from the phone yet: Pisa's default zone stamped it, and stays
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.due_at = OLD.due_at AND OLD.due_date IS NOT NULL
       AND NEW.due_date IS NOT DISTINCT FROM OLD.due_date THEN
        NEW.due_date := OLD.due_date;  -- the deadline didn't change
    ELSIF TG_OP = 'UPDATE' AND NEW.due_date IS DISTINCT FROM OLD.due_date AND NEW.due_date IS NOT NULL THEN
        NULL;  -- the day itself was written (the re-stamp): it wins
    ELSIF (NEW.due_at AT TIME ZONE now_tz)::time = '23:59' THEN
        NEW.due_date := (NEW.due_at AT TIME ZONE now_tz)::date;
    ELSIF prev_tz IS NOT NULL AND (NEW.due_at AT TIME ZONE prev_tz)::time = '23:59' THEN
        NEW.due_date := (NEW.due_at AT TIME ZONE prev_tz)::date;  -- stamped on the zone before
    ELSE
        NEW.due_date := (NEW.due_at AT TIME ZONE now_tz)::date;
    END IF;
    NEW.due_at := (NEW.due_date + time '23:59') AT TIME ZONE now_tz;
    RETURN NEW;
END $$;
CREATE TRIGGER tasks_due_date BEFORE INSERT OR UPDATE OF due_at, due_kind, due_date ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION tasks_due_date();

-- A set of tasks changed at once, with one undo.
CREATE TABLE task_bulk (
    id         uuid PRIMARY KEY,
    action     text NOT NULL CHECK (action IN ('done', 'dismiss', 'delete')),
    items      jsonb NOT NULL,
    origin     text NOT NULL CHECK (origin IN ('voice', 'app')),
    created_at timestamptz NOT NULL,
    undone_at  timestamptz
);
CREATE INDEX task_bulk_created ON task_bulk (created_at DESC);

-- Items closed by a stay or by the user's words, for the undo.
CREATE TABLE done_by_presence (
    id          uuid PRIMARY KEY,
    task_id     uuid REFERENCES tasks (id) ON DELETE CASCADE,
    calendar_id text,
    event_id    text,
    day         date NOT NULL,
    arrived_at  timestamptz,
    place_id    uuid REFERENCES places (id) ON DELETE SET NULL,
    learn       boolean NOT NULL,
    title       text NOT NULL,
    source      text NOT NULL CHECK (source IN ('stay', 'voice', 'email')),
    done_at     timestamptz NOT NULL,
    undo_until  timestamptz NOT NULL,
    undone_at   timestamptz,
    origin      text NOT NULL DEFAULT 'phone' CHECK (origin IN ('phone', 'watch')),
    CONSTRAINT done_by_presence_check CHECK ((task_id IS NULL) <> (event_id IS NULL))
);
CREATE INDEX done_by_presence_at ON done_by_presence (done_at);
CREATE UNIQUE INDEX done_by_presence_event ON done_by_presence (event_id) WHERE (event_id IS NOT NULL)
        AND (source = 'stay');
CREATE INDEX done_by_presence_place ON done_by_presence (place_id) WHERE place_id IS NOT NULL;
CREATE UNIQUE INDEX done_by_presence_task_day ON done_by_presence (task_id,
        day) WHERE (task_id IS NOT NULL) AND (source = 'stay');

-- Each change the user makes to Pisa's decisions.
CREATE TABLE feedback_events (
    id         uuid PRIMARY KEY,
    kind       text NOT NULL,
    subject    text NOT NULL,
    from_value text,
    to_value   text,
    created_at timestamptz NOT NULL
);
CREATE INDEX feedback_events_at ON feedback_events (created_at);


-- ==========================================================================================================
-- Place notes and recommendations
-- Notes and tasks that fire at a place, and what became of Pisa's recommendations.
-- ==========================================================================================================

-- A note pinned to a place, fired once per visit.
CREATE TABLE place_notes (
    id            uuid PRIMARY KEY,
    text          text NOT NULL,
    lat           double precision NOT NULL CHECK ((lat >= -90) AND (lat <= 90)),
    lon           double precision NOT NULL CHECK ((lon >= -180) AND (lon <= 180)),
    radius_m      integer NOT NULL CHECK ((radius_m >= 50) AND (radius_m <= 500)),
    place_name    text CHECK (length(place_name) <= 200),
    place_id      uuid REFERENCES places (id) ON DELETE SET NULL,
    turn_id       uuid,
    device_id     uuid REFERENCES devices (id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL,
    last_fired_at timestamptz,
    CONSTRAINT place_notes_text_check CHECK ((length(text) >= 1) AND (length(text) <= 300))
);
CREATE INDEX place_notes_created ON place_notes (created_at DESC);
CREATE INDEX place_notes_place ON place_notes (place_id) WHERE place_id IS NOT NULL;

-- When a task with a place last fired.
CREATE TABLE place_task_fires (
    task_id  uuid PRIMARY KEY REFERENCES tasks (id) ON DELETE CASCADE,
    fired_at timestamptz NOT NULL
);

-- What happened to Pisa's recommendations.
CREATE TABLE recommendation_outcomes (
    id         uuid PRIMARY KEY,
    card_id    uuid,
    place_id   uuid REFERENCES places (id) ON DELETE SET NULL,
    action     text NOT NULL CHECK (action IN ('shown', 'go', 'plan', 'call', 'website', 'details',
                                    'dismissed', 'went', 'critique')),
    shown      uuid[],
    reason     text,
    device_id  uuid REFERENCES devices (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX recommendation_outcomes_card ON recommendation_outcomes (card_id, action);
CREATE INDEX recommendation_outcomes_created ON recommendation_outcomes (created_at);
CREATE UNIQUE INDEX recommendation_outcomes_once ON recommendation_outcomes (card_id, place_id,
        action) WHERE place_id IS NOT NULL;
CREATE INDEX recommendation_outcomes_place ON recommendation_outcomes (place_id) WHERE place_id IS NOT NULL;


-- ==========================================================================================================
-- Notifications and briefings
-- Proactive messages, briefings, reminders and check-ins.
-- ==========================================================================================================

-- Every proactive message Pisa starts, and its delivery.
CREATE TABLE notifications (
    id            uuid PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('reminder', 'briefing', 'replan', 'alert', 'info',
                                       'check_in', 'done')),
    text          text NOT NULL,
    lang          text NOT NULL,
    priority      text NOT NULL CHECK (priority IN ('low', 'normal', 'urgent')),
    device_id     uuid REFERENCES devices (id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL,
    queued_at     timestamptz NOT NULL,
    delivered_via text CHECK (delivered_via IN ('ws', 'apns', 'held', 'none')),
    delivered_at  timestamptz,
    summary       text,
    check_in_id   uuid,
    choices       jsonb,
    expects_reply boolean,
    weekly        boolean,
    done          jsonb,
    urgency       text NOT NULL DEFAULT 'breakpoint' CHECK (urgency IN ('now', 'breakpoint')),
    not_before    timestamptz,
    put_off_at    timestamptz,
    event_at      timestamptz,
    spoken_at     timestamptz,
    silent        boolean,
    speak         text CHECK (speak IN ('leave_now', 'reply_waiting', 'approval_lapsing', 'check_in')),
    ledger_week   text CHECK (ledger_week ~ '^[0-9]{4}-W[0-9]{2}$'),
    ritual        text,
    sheet_id      uuid,
    ask_id        uuid,
    path          text CHECK (char_length(path) <= 60),
    person_id     uuid,
    guardian      text CHECK (guardian IN ('now', 'breakpoint', 'digest', 'released')),
    turn_id       uuid
);
CREATE INDEX notifications_created ON notifications (created_at);
CREATE INDEX notifications_held ON notifications (created_at) WHERE (delivered_via = 'held')
        AND (delivered_at IS NULL);
CREATE INDEX notifications_pending ON notifications (queued_at) WHERE delivered_via IS NULL;
CREATE INDEX notifications_spoken ON notifications (spoken_at) WHERE spoken_at IS NOT NULL;

-- Briefings composed, per day and trigger.
CREATE TABLE briefings (
    id              uuid PRIMARY KEY,
    day             date NOT NULL,
    trigger         text NOT NULL CHECK (trigger IN ('glasses_on', 'app_open', 'phrase', 'time', 'ask')),
    text            text NOT NULL,
    lang            text NOT NULL,
    created_at      timestamptz NOT NULL,
    notification_id uuid REFERENCES notifications (id) ON DELETE SET NULL
);
CREATE INDEX briefings_created ON briefings (created_at DESC);
CREATE INDEX briefings_day ON briefings (day, created_at DESC);
CREATE UNIQUE INDEX briefings_one_time_per_day ON briefings (day) WHERE trigger = 'time';

-- Each reminder fires at most once.
CREATE TABLE reminders_sent (
    kind       text CHECK (kind IN ('event', 'task', 'leave')),
    ref        text,
    fire_at    timestamptz,
    outcome    text NOT NULL CHECK (outcome IN ('sent', 'skipped')),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (kind, ref, fire_at)
);
CREATE INDEX reminders_sent_created ON reminders_sent (created_at);

-- Check-ins: asked once per item and day.
CREATE TABLE check_ins (
    id              uuid PRIMARY KEY,
    day             date NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('missed', 'confirm')),
    event_id        text,
    task_id         uuid,
    title           text NOT NULL,
    text            text NOT NULL,
    choices         jsonb NOT NULL,
    created_at      timestamptz NOT NULL,
    answered_at     timestamptz,
    answer          text,
    dropped_at      timestamptz,
    reask_at        timestamptz,
    notification_id uuid,
    purpose         text,
    answer_origin   text CHECK (answer_origin IN ('phone', 'watch')),
    CONSTRAINT check_ins_check CHECK ((event_id IS NULL) <> (task_id IS NULL))
);
CREATE INDEX check_ins_created ON check_ins (created_at);
CREATE INDEX check_ins_day ON check_ins (day, created_at);
CREATE UNIQUE INDEX check_ins_one_open ON check_ins ((true)) WHERE (answered_at IS NULL)
        AND (dropped_at IS NULL);

-- What Pisa could say unasked, the decision on it and its outcome.
CREATE TABLE attend_candidates (
    id              uuid PRIMARY KEY,
    key             text NOT NULL UNIQUE,
    source          text NOT NULL CHECK (source IN ('reminder', 'leave', 'stay', 'check_in', 'intake',
                                         'replan', 'weather', 'promise', 'knock', 'tragitto', 'intention')),
    kind            text NOT NULL,
    text            text NOT NULL,
    lang            text NOT NULL,
    urgency         text NOT NULL CHECK (urgency IN ('now', 'breakpoint')),
    priority        text NOT NULL DEFAULT 'normal',
    deadline        timestamptz,
    floor           boolean NOT NULL DEFAULT false,
    outside         boolean NOT NULL DEFAULT false,
    alone           boolean NOT NULL DEFAULT false,
    must_show       boolean NOT NULL DEFAULT false,
    delivery        jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL,
    decided_at      timestamptz,
    decision        jsonb,
    notification_id uuid,
    outcome         text,
    outcome_at      timestamptz,
    plan_change     real CHECK ((plan_change >= 0) AND (plan_change <= 1)),
    seen            boolean
);
CREATE INDEX attend_candidates_open ON attend_candidates (decided_at) WHERE outcome IS NULL;
CREATE INDEX attend_candidates_pending ON attend_candidates (created_at) WHERE decided_at IS NULL;

-- Pisa's own follow-ups, which become candidates when their condition holds.
CREATE TABLE pisa_promises (
    id           uuid PRIMARY KEY,
    what         text NOT NULL CHECK (char_length(what) <= 200),
    condition    text NOT NULL CHECK (condition IN ('time', 'arrive', 'leave', 'next_talk')),
    due_at       timestamptz,
    place_id     uuid REFERENCES places (id) ON DELETE SET NULL,
    purpose      text CHECK (char_length(purpose) <= 120),
    lang         text NOT NULL,
    provenance   text NOT NULL,
    device_id    uuid REFERENCES devices (id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL,
    fired_at     timestamptz,
    cancelled_at timestamptz
);
CREATE INDEX pisa_promises_cancelled ON pisa_promises (cancelled_at) WHERE cancelled_at IS NOT NULL;
CREATE INDEX pisa_promises_created ON pisa_promises (created_at);
CREATE INDEX pisa_promises_fired ON pisa_promises (fired_at) WHERE fired_at IS NOT NULL;
CREATE INDEX pisa_promises_open ON pisa_promises (created_at) WHERE (fired_at IS NULL) AND (cancelled_at IS NULL);
CREATE INDEX pisa_promises_place ON pisa_promises (place_id) WHERE place_id IS NOT NULL;

-- At most one morning thought a day, chosen by code.
CREATE TABLE morning_thoughts (
    id          uuid PRIMARY KEY,
    day         date NOT NULL UNIQUE,
    kind        text NOT NULL CHECK (kind IN ('welcome_back', 'stuck_task')),
    ref         uuid NOT NULL,
    ask_id      uuid,
    answer      text CHECK (answer IN ('yes', 'no')),
    answered_at timestamptz,
    created_at  timestamptz NOT NULL,
    UNIQUE (kind, ref)
);


-- ==========================================================================================================
-- Day planning
-- Plans, options and adjustments, and how the plan compared with the day.
-- ==========================================================================================================

-- Every plan made, with what it was made from.
CREATE TABLE plans (
    id           uuid PRIMARY KEY,
    day          date NOT NULL,
    generated_at timestamptz NOT NULL,
    reason       text NOT NULL,
    fingerprint  text NOT NULL,
    from_lat     double precision,
    from_lon     double precision,
    plan         jsonb NOT NULL,
    requested_by uuid REFERENCES devices (id) ON DELETE SET NULL
);
CREATE INDEX plans_day ON plans (day, generated_at DESC);
CREATE INDEX plans_generated ON plans (generated_at);
CREATE INDEX plans_requested_by ON plans (requested_by) WHERE requested_by IS NOT NULL;

-- Changes the user chose that every later plan of that day applies.
CREATE TABLE plan_adjustments (
    id           uuid PRIMARY KEY,
    day          date NOT NULL,
    action       text NOT NULL CHECK (action IN ('move', 'skip', 'replace_place', 'shorten', 'plan')),
    event_id     text,
    task_id      uuid,
    title        text NOT NULL,
    starts_at    timestamptz,
    ends_at      timestamptz,
    place_id     uuid REFERENCES places (id) ON DELETE SET NULL,
    duration_min integer CHECK ((duration_min IS NULL) OR ((duration_min >= 5) AND (duration_min <= 480))),
    source       text NOT NULL CHECK (source IN ('voice', 'app', 'check_in')),
    created_at   timestamptz NOT NULL,
    option_ref   uuid NOT NULL,
    seq          bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    approval_id  uuid REFERENCES approvals (id) ON DELETE SET NULL,
    CONSTRAINT plan_adjustments_check CHECK ((event_id IS NULL) <> (task_id IS NULL))
);
CREATE INDEX plan_adjustments_day ON plan_adjustments (day, created_at);
CREATE INDEX plan_adjustments_option ON plan_adjustments (option_ref);
CREATE INDEX plan_adjustments_place ON plan_adjustments (place_id) WHERE place_id IS NOT NULL;

-- Options offered for a short time, checked against the day.
CREATE TABLE plan_options (
    id          uuid PRIMARY KEY,
    day         date NOT NULL,
    fingerprint text NOT NULL,
    body        jsonb NOT NULL,
    created_at  timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL
);
CREATE INDEX plan_options_expires ON plan_options (expires_at);

-- Planned against actual minutes for each item of a day that ended.
CREATE TABLE plan_actuals (
    id          uuid PRIMARY KEY,
    day         date NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('task', 'event', 'travel')),
    ref         text NOT NULL CHECK ((char_length(ref) >= 1) AND (char_length(ref) <= 200)),
    planned_min integer NOT NULL CHECK (planned_min > 0),
    actual_min  integer NOT NULL CHECK (actual_min > 0),
    source      text NOT NULL CHECK (source IN ('stay', 'done')),
    scored_at   timestamptz NOT NULL,
    UNIQUE (day, kind, ref)
);
CREATE INDEX plan_actuals_kind_day ON plan_actuals (kind, day);

-- Actual over planned per kind, which the planner reads.
CREATE TABLE plan_bands (
    kind        text PRIMARY KEY CHECK (kind IN ('task', 'event', 'travel')),
    n           integer NOT NULL CHECK (n > 0),
    lo          real NOT NULL CHECK (lo > 0),
    mid         real NOT NULL,
    hi          real NOT NULL,
    computed_at timestamptz NOT NULL,
    CONSTRAINT plan_bands_check CHECK (mid >= lo),
    CONSTRAINT plan_bands_check1 CHECK (hi >= mid)
);

-- The night's rehearsal of a day that holds a conflict.
CREATE TABLE day_rehearsals (
    day          date PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('clash', 'transfer')),
    line         text NOT NULL CHECK ((char_length(line) >= 1) AND (char_length(line) <= 300)),
    refs         jsonb NOT NULL DEFAULT '[]',
    need_min     integer CHECK (need_min > 0),
    outside      boolean NOT NULL DEFAULT false,
    rehearsed_at timestamptz NOT NULL
);

-- The end-of-walk recap, at most once a day.
CREATE TABLE walk_recaps (
    day        date PRIMARY KEY,
    walk_start timestamptz NOT NULL,
    walk_end   timestamptz NOT NULL,
    items      integer NOT NULL CHECK ((items >= 1) AND (items <= 20)),
    created_at timestamptz NOT NULL
);


-- ==========================================================================================================
-- Things, people and trips
-- Orders, bookings and trips joined by hard keys; people and their dates; trips and residency.
-- ==========================================================================================================

-- A thing: an order, a booking, a trip.
CREATE TABLE things (
    id         uuid PRIMARY KEY,
    title      text NOT NULL CHECK (char_length(title) <= 200),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

-- Emails, tasks and events joined to a thing.
CREATE TABLE thing_items (
    thing_id   uuid NOT NULL REFERENCES things (id) ON DELETE CASCADE,
    item_kind  text CHECK (item_kind IN ('email', 'task', 'event')),
    item_ref   text,
    linked_by  text NOT NULL CHECK (linked_by IN ('key', 'thread', 'source', 'date_place', 'first')),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (item_kind, item_ref)
);
CREATE INDEX thing_items_thing ON thing_items (thing_id);

-- The hard keys a thing is joined by.
CREATE TABLE thing_keys (
    key      text PRIMARY KEY,
    thing_id uuid NOT NULL REFERENCES things (id) ON DELETE CASCADE,
    kind     text CHECK (kind IN ('order', 'booking', 'ticket', 'tracking', 'reference'))
);

-- A person in the user's life.
CREATE TABLE people (
    id              uuid PRIMARY KEY,
    name            text NOT NULL CHECK ((length(name) >= 1) AND (length(name) <= 80)),
    name_key        text NOT NULL,
    relation        text CHECK (length(relation) <= 40),
    tz              text CHECK (length(tz) <= 64),
    city            text CHECK (length(city) <= 80),
    emails          text[] NOT NULL DEFAULT '{}',
    source          text NOT NULL CHECK (source IN ('owner', 'email', 'calendar')),
    owner_fields    text[] NOT NULL DEFAULT '{}',
    hidden          boolean NOT NULL DEFAULT false,
    orbit           boolean NOT NULL DEFAULT true,
    last_contact_at timestamptz,
    rhythm_days     real,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL
);
CREATE INDEX people_emails ON people USING gin (emails);

-- Notes and gift ideas about a person.
CREATE TABLE person_notes (
    id         bigserial PRIMARY KEY,
    person_id  uuid NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('note', 'gift')),
    text       text NOT NULL,
    source     text NOT NULL CHECK (source IN ('owner', 'email')),
    source_ref text,
    at         timestamptz NOT NULL,
    CONSTRAINT person_notes_check CHECK ((source = 'owner') OR (kind = 'gift')),
    CONSTRAINT person_notes_text_check CHECK ((length(text) >= 1) AND (length(text) <= 300))
);
CREATE UNIQUE INDEX person_notes_email ON person_notes (person_id, source_ref) WHERE source = 'email';

-- A person's dates.
CREATE TABLE person_dates (
    id                 bigserial PRIMARY KEY,
    person_id          uuid NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    kind               text NOT NULL CHECK (kind IN ('birthday', 'anniversary', 'name_day', 'other')),
    month              smallint NOT NULL CHECK ((month >= 1) AND (month <= 12)),
    day                smallint NOT NULL CHECK ((day >= 1) AND (day <= 31)),
    year               smallint CHECK ((year >= 1900) AND (year <= 2100)),
    remind_days_before smallint[] NOT NULL DEFAULT '{7,1}'::smallint[],
    source             text NOT NULL CHECK (source IN ('owner', 'calendar')),
    source_ref         text,
    created_at         timestamptz NOT NULL,
    UNIQUE (person_id, kind)
);

-- What a person said about being somewhere or free.
CREATE TABLE person_moments (
    id         bigserial PRIMARY KEY,
    person_id  uuid NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('in_city', 'free')),
    city       text CHECK (length(city) <= 40),
    word       text CHECK (length(word) <= 12),
    first_day  date NOT NULL,
    last_day   date NOT NULL,
    source     text NOT NULL CHECK (source IN ('email', 'calendar')),
    source_ref text NOT NULL,
    at         timestamptz NOT NULL,
    UNIQUE (person_id, source_ref),
    CONSTRAINT person_moments_check CHECK (first_day <= last_day),
    CONSTRAINT person_moments_check1 CHECK ((kind = 'in_city') = (city IS NOT NULL))
);

-- What Pisa said about people unasked, so nothing is said twice.
CREATE TABLE people_nudges (
    id        bigserial PRIMARY KEY,
    kind      text NOT NULL CHECK (kind IN ('date', 'orbit', 'thanks')),
    person_id uuid REFERENCES people (id) ON DELETE CASCADE,
    ref       text NOT NULL,
    day       date NOT NULL,
    line      text NOT NULL,
    draft     text,
    at        timestamptz NOT NULL,
    UNIQUE (kind, ref)
);

-- What Pisa offers about people, once per kind and thing.
CREATE TABLE people_offers (
    id              uuid PRIMARY KEY,
    kind            text NOT NULL CHECK (kind IN ('moment', 'thanks', 'wish')),
    person_id       uuid NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    ref             text NOT NULL,
    line            text NOT NULL CHECK (length(line) <= 300),
    draft           text CHECK (length(draft) <= 1000),
    data            jsonb NOT NULL,
    due_at          timestamptz NOT NULL,
    until           timestamptz NOT NULL,
    sent_at         timestamptz,
    notification_id uuid,
    created_at      timestamptz NOT NULL,
    UNIQUE (kind, ref)
);
CREATE INDEX people_offers_due ON people_offers (due_at) WHERE sent_at IS NULL;
CREATE INDEX people_offers_person ON people_offers (person_id, kind, created_at);

-- The care circle: only what the user entered.
CREATE TABLE care_circle (
    person_id uuid PRIMARY KEY REFERENCES people (id) ON DELETE CASCADE,
    shares    text NOT NULL CHECK (shares IN ('location', 'calendar_busy', 'nothing')),
    since     timestamptz NOT NULL
);

-- Trips built from confirmation emails.
CREATE TABLE trips (
    id               uuid PRIMARY KEY,
    title            text NOT NULL CHECK ((length(title) >= 1) AND (length(title) <= 120)),
    from_city        text,
    to_city          text,
    from_country     text CHECK (from_country ~ '^[A-Z]{2}$'),
    to_country       text CHECK (to_country ~ '^[A-Z]{2}$'),
    tz_from          text,
    tz_to            text,
    depart_at        timestamptz NOT NULL,
    arrive_at        timestamptz NOT NULL,
    back_at          timestamptz,
    status           text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'cancelled')),
    doubt            text CHECK (doubt IN ('unverified_sender', 'someone_else')),
    proposals        jsonb NOT NULL DEFAULT '[]',
    landing_pending  jsonb,
    welcome_due      text,
    title_by_owner   boolean NOT NULL DEFAULT false,
    legs             jsonb NOT NULL DEFAULT '[]',
    checklist        jsonb NOT NULL DEFAULT '[]',
    source_email_ids text[] NOT NULL DEFAULT '{}',
    cancel_mentions  text[] NOT NULL DEFAULT '{}',
    landed_keys      text[] NOT NULL DEFAULT '{}',
    leaving_said_at  timestamptz,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    by_owner_at      timestamptz,
    CONSTRAINT trips_check CHECK (arrive_at >= (depart_at - '1 day'::interval))
);
CREATE INDEX trips_depart ON trips (depart_at);

-- Countries per day, for the residency counter.
CREATE TABLE residency_days (
    day           date PRIMARY KEY,
    phone_regions text[] NOT NULL DEFAULT '{}',
    phone_region  text CHECK (phone_region ~ '^[A-Z]{2}$'),
    phone_at      timestamptz,
    corrected     text[] CHECK ((corrected <@ ARRAY['IT', 'US', 'other']) AND (cardinality(corrected) >= 1)),
    corrected_at  timestamptz,
    CONSTRAINT residency_days_check CHECK ((phone_region IS NOT NULL) OR (corrected IS NOT NULL))
);

-- A handoff Pisa prepared for the user to finish in another app.
CREATE TABLE handoff_preps (
    id              uuid PRIMARY KEY,
    device_id       uuid REFERENCES devices (id) ON DELETE SET NULL,
    purpose         text NOT NULL CHECK (purpose IN ('booking', 'ride', 'message', 'call', 'shopping',
                                         'money')),
    target          text NOT NULL CHECK ((length(target) >= 1) AND (length(target) <= 200)),
    object_id       text,
    lang            text NOT NULL,
    details         jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL,
    opened_at       timestamptz,
    follow_up_at    timestamptz,
    followed_up_at  timestamptz,
    notification_id uuid,
    outcome         text CHECK (outcome IN ('worked', 'failed')),
    outcome_at      timestamptz,
    CONSTRAINT handoff_preps_check CHECK ((outcome IS NULL) = (outcome_at IS NULL))
);
CREATE INDEX handoff_preps_created ON handoff_preps (created_at);
CREATE INDEX handoff_preps_follow_up ON handoff_preps (follow_up_at) WHERE (followed_up_at IS NULL)
        AND (outcome IS NULL);

-- Each button of a handoff preparation.
CREATE TABLE handoffs (
    id        uuid PRIMARY KEY,
    prep_id   uuid NOT NULL REFERENCES handoff_preps (id) ON DELETE CASCADE,
    kind      text NOT NULL CHECK (kind IN ('open_url', 'call', 'message', 'map')),
    title     text NOT NULL,
    url       text CHECK (url ~ '^(https://|tel:|sms:|maps:|uber:|lyft:)\S*$'),
    phone     text CHECK (phone ~ '^\+[1-9][0-9]{5,14}$'),
    opened_at timestamptz
);
CREATE INDEX handoffs_prep ON handoffs (prep_id);


-- ==========================================================================================================
-- Money and life admin
-- Bills, receipts, refunds, claims, purchases, documents and obligations.
-- ==========================================================================================================

-- Documents with an expiry; never a full number.
CREATE TABLE vault_documents (
    id           uuid PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('cie', 'passport', 'driving_licence', 'health_card',
                                      'car_revision', 'car_tax', 'car_insurance', 'visa', 'green_card',
                                      'us_licence', 'other')),
    country      text NOT NULL CHECK (country IN ('IT', 'US')),
    number_last4 text CHECK (number_last4 ~ '^[A-Za-z0-9]{4}$'),
    expires_on   date NOT NULL,
    issued_on    date,
    notes        text,
    reminders    jsonb NOT NULL,
    task_id      uuid REFERENCES tasks (id) ON DELETE SET NULL,
    provenance   text NOT NULL CHECK (provenance IN ('said', 'app')),
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT vault_documents_notes_check CHECK ((char_length(notes) <= 300) AND (regexp_replace(notes,
                                                  '[[:space:]./_,:-]', '', 'g') !~ '[0-9]{5}'))
);
CREATE INDEX vault_documents_expires ON vault_documents (expires_on);

-- Bills and receipts Pisa recognised in an email.
CREATE TABLE money_seen (
    id          uuid PRIMARY KEY,
    email_id    text NOT NULL UNIQUE,
    kind        text NOT NULL CHECK (kind IN ('bill', 'receipt')),
    sender_key  text NOT NULL,
    from_name   text NOT NULL CHECK (char_length(from_name) <= 120),
    subject     text NOT NULL CHECK (char_length(subject) <= 200),
    amount      numeric(12,2),
    currency    text CHECK (currency IN ('EUR', 'USD', 'GBP')),
    due_on      date,
    paid        boolean NOT NULL DEFAULT false,
    task_id     uuid REFERENCES tasks (id) ON DELETE SET NULL,
    sent_at     timestamptz NOT NULL,
    seen_at     timestamptz NOT NULL,
    payee       text CHECK ((char_length(payee) >= 1) AND (char_length(payee) <= 120)),
    iban_last4  text CHECK (iban_last4 ~ '^[0-9A-Z]{4}$'),
    source      text NOT NULL DEFAULT 'email' CHECK (source IN ('email', 'pdf')),
    pdf_part_id text CHECK ((char_length(pdf_part_id) >= 1) AND (char_length(pdf_part_id) <= 40))
);
CREATE INDEX money_seen_bills ON money_seen (sent_at DESC) WHERE kind = 'bill';
CREATE INDEX money_seen_sender ON money_seen (sender_key, sent_at);

-- Which emails the money reader has seen.
CREATE TABLE money_reads (
    email_id text PRIMARY KEY REFERENCES email_messages (id) ON DELETE CASCADE,
    read_at  timestamptz NOT NULL,
    waiting  text CHECK (waiting IN ('refund', 'flight', 'flight|refund'))
);

-- Each bill email's PDF, read once.
CREATE TABLE bill_pdf_reads (
    email_id text PRIMARY KEY REFERENCES email_messages (id) ON DELETE CASCADE,
    outcome  text NOT NULL CHECK (outcome IN ('read', 'no_bill', 'too_big', 'too_many_pages', 'unreadable',
                                  'encrypted')),
    read_at  timestamptz NOT NULL
);

-- Bill checks for old consumption.
CREATE TABLE bill_checks (
    id              uuid PRIMARY KEY,
    source_email_id text UNIQUE,
    supplier        text NOT NULL CHECK ((char_length(supplier) >= 1) AND (char_length(supplier) <= 120)),
    utility         text NOT NULL CHECK (utility IN ('luce', 'gas', 'acqua')),
    issued_on       date NOT NULL,
    period_from     date,
    period_to       date,
    amount          numeric(12,2) CHECK (amount > (0)::numeric),
    old_amount      numeric(12,2) CHECK (old_amount > (0)::numeric),
    currency        text CHECK (currency = 'EUR'),
    reason          text NOT NULL CHECK (reason IN ('older_than_2_years', 'stated_by_supplier')),
    status          text NOT NULL CHECK (status IN ('flagged', 'contested', 'dismissed')),
    task_id         uuid REFERENCES tasks (id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL
);

-- Flight delay claims, one per flight and day.
CREATE TABLE flight_claims (
    id               uuid PRIMARY KEY,
    flight           text NOT NULL CHECK (flight ~ '^[A-Z0-9]{2}[0-9]{1,4}$'),
    day              date NOT NULL,
    origin           text CHECK (origin ~ '^[A-Z]{3}$'),
    destination      text CHECK (destination ~ '^[A-Z]{3}$'),
    scheduled_at     timestamptz NOT NULL,
    scheduled_arr    timestamptz,
    tz_from          text,
    tz_to            text,
    booking_last4    text CHECK (booking_last4 ~ '^[A-Za-z0-9]{1,4}$'),
    disruption       text NOT NULL CHECK (disruption IN ('delay', 'cancellation', 'denied_boarding')),
    delay_min        integer CHECK ((delay_min >= 0) AND (delay_min <= 4320)),
    delay_source     text CHECK (delay_source IN ('email', 'said')),
    notice_days      integer CHECK (notice_days >= 0),
    rule             text NOT NULL CHECK (rule IN ('eu261', 'us_dot', 'none')),
    status           text NOT NULL CHECK (status IN ('possible', 'eligible', 'not_eligible', 'sent', 'paid',
                                          'dismissed')),
    reason           text NOT NULL,
    distance_km      integer CHECK (distance_km > 0),
    amount           numeric(12,2) CHECK (amount = ANY (ARRAY[(250)::numeric, (400)::numeric,
                                          (600)::numeric])),
    currency         text CHECK (currency = 'EUR'),
    trip_id          uuid REFERENCES trips (id) ON DELETE SET NULL,
    source           text NOT NULL CHECK (source IN ('email', 'said')),
    source_email_ids text[] NOT NULL DEFAULT '{}',
    task_id          uuid REFERENCES tasks (id) ON DELETE SET NULL,
    tasked_at        timestamptz,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    paid_at          timestamptz,
    delay_at         timestamptz,
    delay_email_id   text,
    UNIQUE (flight, day),
    CONSTRAINT flight_claims_check CHECK ((rule = 'eu261') OR (amount IS NULL)),
    CONSTRAINT flight_claims_check1 CHECK ((amount IS NULL) = (currency IS NULL))
);

-- A refund an email promised, until it is closed.
CREATE TABLE refunds_promised (
    id                uuid PRIMARY KEY,
    sender_key        text NOT NULL,
    merchant          text NOT NULL CHECK ((char_length(merchant) >= 1) AND (char_length(merchant) <= 120)),
    amount            numeric(12,2) CHECK (amount > (0)::numeric),
    currency          text CHECK (currency IN ('EUR', 'USD', 'GBP')),
    promised_on       date NOT NULL,
    expected_by       date NOT NULL,
    status            text NOT NULL CHECK (status IN ('waiting', 'overdue', 'received', 'dismissed')),
    source_email_id   text UNIQUE,
    received_email_id text,
    task_id           uuid REFERENCES tasks (id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    received_at       timestamptz
);
CREATE INDEX refunds_promised_waiting ON refunds_promised (expected_by) WHERE status IN ('waiting',
        'overdue');

-- The user's word on a subscription.
CREATE TABLE subscription_marks (
    id                 text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{16}$'),
    used_on            date,
    kept_on            date,
    updated_at         timestamptz NOT NULL,
    cancelled_on       date,
    cancelled_name     text CHECK (char_length(cancelled_name) <= 120),
    cancelled_amount   numeric(12,2) CHECK (cancelled_amount > (0)::numeric),
    cancelled_currency text CHECK (cancelled_currency IN ('EUR', 'USD', 'GBP')),
    cancelled_period   text CHECK (cancelled_period IN ('monthly', 'yearly'))
);

-- What the user bought: warranties and supplies.
CREATE TABLE purchases (
    id              uuid PRIMARY KEY,
    item            text NOT NULL CHECK ((char_length(item) >= 2) AND (char_length(item) <= 160)),
    item_key        text NOT NULL CHECK ((char_length(item_key) >= 2) AND (char_length(item_key) <= 160)),
    seller          text CHECK (char_length(seller) <= 120),
    seller_key      text,
    ordered_on      date NOT NULL,
    delivered_on    date,
    price           numeric(12,2) CHECK (price > (0)::numeric),
    currency        text CHECK (currency IN ('EUR', 'USD', 'GBP')),
    region          text NOT NULL CHECK (region IN ('EU', 'US')),
    source          text NOT NULL CHECK (source IN ('email', 'said', 'app')),
    source_email_id text,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    UNIQUE (source_email_id, item_key)
);
CREATE INDEX purchases_item ON purchases (item_key, ordered_on);

-- The task made for one expected run-out.
CREATE TABLE supply_tasks (
    item_key   text,
    run_out_on date,
    task_id    uuid REFERENCES tasks (id) ON DELETE SET NULL,
    prep_id    uuid,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (item_key, run_out_on)
);

-- The connector registry: the user's switch, state and sealed credential.
CREATE TABLE connectors (
    id                 text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_]{1,39}$'),
    family             text NOT NULL CHECK (family IN ('health', 'sport', 'bank', 'admin', 'mail',
                                            'calendar', 'places', 'home', 'mobility')),
    provider           text NOT NULL CHECK ((char_length(provider) >= 1) AND (char_length(provider) <= 40)),
    enabled            boolean NOT NULL DEFAULT false,
    state              text NOT NULL DEFAULT 'off' CHECK (state IN ('off', 'on', 'needs_owner', 'error')),
    message            text CHECK (char_length(message) <= 300),
    settings           jsonb NOT NULL DEFAULT '{}',
    data_out_seen_at   timestamptz,
    secret_enc         bytea,
    secret_set_at      timestamptz,
    secret_expires_on  date,
    access_enc         bytea,
    access_expires_at  timestamptz,
    consent_expires_at timestamptz,
    last_sync_at       timestamptz,
    last_sync_counts   jsonb NOT NULL DEFAULT '{}',
    failures           integer NOT NULL DEFAULT 0,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT connectors_check CHECK (enabled OR (state = 'off')),
    CONSTRAINT connectors_check1 CHECK ((state <> 'off') OR (NOT enabled))
);

-- Daily health and sport numbers, derived only.
CREATE TABLE health_days (
    day          date,
    source       text CHECK (source IN ('apple_health', 'garmin')),
    sleep_min    integer CHECK ((sleep_min >= 0) AND (sleep_min <= 1440)),
    in_bed_min   integer CHECK ((in_bed_min >= 0) AND (in_bed_min <= 1440)),
    bedtime      time,
    wake_time    time,
    steps        integer CHECK ((steps >= 0) AND (steps <= 200000)),
    active_kcal  integer CHECK ((active_kcal >= 0) AND (active_kcal <= 20000)),
    exercise_min integer CHECK ((exercise_min >= 0) AND (exercise_min <= 1440)),
    resting_hr   integer CHECK ((resting_hr >= 20) AND (resting_hr <= 250)),
    hrv_sdnn_ms  integer CHECK ((hrv_sdnn_ms >= 1) AND (hrv_sdnn_ms <= 400)),
    vo2max       numeric(4,1) CHECK ((vo2max >= (5)::numeric) AND (vo2max <= (99)::numeric)),
    workouts     jsonb NOT NULL DEFAULT '[]',
    doses_taken  integer CHECK ((doses_taken >= 0) AND (doses_taken <= 100)),
    doses_missed integer CHECK ((doses_missed >= 0) AND (doses_missed <= 100)),
    received_at  timestamptz NOT NULL,
    PRIMARY KEY (day, source)
);

-- Bank accounts a consent shows; never a full IBAN.
CREATE TABLE bank_accounts (
    connector_id text REFERENCES connectors (id),
    uid          text CHECK ((char_length(uid) >= 1) AND (char_length(uid) <= 200)),
    name         text CHECK (char_length(name) <= 120),
    iban_last4   text CHECK (iban_last4 ~ '^[A-Za-z0-9]{4}$'),
    currency     text CHECK (currency ~ '^[A-Z]{3}$'),
    seen_at      timestamptz NOT NULL,
    PRIMARY KEY (connector_id, uid)
);

-- Bank movements.
CREATE TABLE bank_transactions (
    id           uuid PRIMARY KEY,
    connector_id text NOT NULL REFERENCES connectors (id),
    account_uid  text NOT NULL,
    provider_ref text NOT NULL CHECK ((char_length(provider_ref) >= 1) AND (char_length(provider_ref) <= 200)),
    booked_on    date NOT NULL,
    amount       numeric(14,2) NOT NULL CHECK (amount >= (0)::numeric),
    currency     text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    direction    text NOT NULL CHECK (direction IN ('in', 'out')),
    counterparty text CHECK (char_length(counterparty) <= 120),
    description  text CHECK (char_length(description) <= 200),
    pending      boolean NOT NULL DEFAULT false,
    seen_at      timestamptz NOT NULL,
    UNIQUE (connector_id, provider_ref)
);
CREATE INDEX bank_transactions_booked ON bank_transactions (booked_on DESC);

-- The admin radar: what is due, when and how.
CREATE TABLE obligations (
    id              uuid PRIMARY KEY,
    kind            text NOT NULL CHECK ((char_length(kind) >= 1) AND (char_length(kind) <= 40)),
    what            text NOT NULL CHECK (char_length(what) <= 200),
    subject         text CHECK (char_length(subject) <= 80),
    due_on          date,
    source          text NOT NULL CHECK (source IN ('mail', 'pec', 'vault', 'calendar', 'bank', 'ade')),
    source_ref      text NOT NULL CHECK ((char_length(source_ref) >= 1) AND (char_length(source_ref) <= 200)),
    rule_source_url text CHECK (char_length(rule_source_url) <= 300),
    penalty         text CHECK (char_length(penalty) <= 300),
    channel         text CHECK (char_length(channel) <= 200),
    amount_text     text CHECK ((char_length(amount_text) <= 40) AND (amount_text ~ '^[0-9€$., A-Z]*$')),
    state           text NOT NULL CHECK (state IN ('upcoming', 'ready', 'done', 'overdue', 'flagged')),
    flag_reason     text CHECK (char_length(flag_reason) <= 200),
    remind_on       date,
    task_id         uuid REFERENCES tasks (id) ON DELETE SET NULL,
    last_verified   date,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    UNIQUE (source, source_ref)
);
CREATE INDEX obligations_due ON obligations (due_on);


-- ==========================================================================================================
-- Intentions and promises
-- What the user means to do or is owed, and promises in the user's own sent mail.
-- ==========================================================================================================

-- An intention with its cue and next step; archived, never deleted.
CREATE TABLE intentions (
    id            uuid PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('mine', 'waiting_for')),
    state         text NOT NULL CHECK (state IN ('proposed', 'open', 'done', 'archived')),
    what          text CHECK ((char_length(what) >= 2) AND (char_length(what) <= 200)),
    why           text CHECK (char_length(why) <= 300),
    next_step     text CHECK (char_length(next_step) <= 200),
    last_step     text CHECK (char_length(last_step) <= 200),
    who           text CHECK ((char_length(who) >= 1) AND (char_length(who) <= 120)),
    cue           text NOT NULL DEFAULT 'none' CHECK (cue IN ('time', 'place', 'event', 'none')),
    cue_event     text CHECK (cue_event IN ('meeting_end', 'arrive_home', 'call_end')),
    cue_ref       text CHECK (char_length(cue_ref) <= 200),
    repeat        jsonb,
    due_on        date,
    task_id       uuid REFERENCES tasks (id) ON DELETE SET NULL,
    place_note_id uuid REFERENCES place_notes (id) ON DELETE SET NULL,
    doc_id        uuid REFERENCES vault_documents (id) ON DELETE SET NULL,
    source        text NOT NULL CHECK (source IN ('voice', 'sent_mail', 'dictation', 'received_mail')),
    source_ref    text CHECK (char_length(source_ref) <= 200),
    asked         text CHECK (asked IN ('what', 'next_step')),
    asked_at      timestamptz,
    fired_at      timestamptz,
    nudged_at     timestamptz,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    closed_at     timestamptz,
    archived_at   timestamptz,
    CONSTRAINT intentions_check CHECK ((cue = 'event') = (cue_event IS NOT NULL)),
    CONSTRAINT intentions_check1 CHECK ((asked IS NULL) = (asked_at IS NULL)),
    CONSTRAINT intentions_check2 CHECK ((source <> 'received_mail') OR ((kind = 'waiting_for')
                                        AND (task_id IS NULL) AND (cue = 'none'))),
    CONSTRAINT intentions_check3 CHECK ((source NOT IN ('sent_mail', 'dictation')) OR (kind = 'mine')),
    CONSTRAINT intentions_check4 CHECK ((state <> 'proposed') OR (source IN ('sent_mail', 'dictation'))),
    CONSTRAINT intentions_check5 CHECK ((kind = 'mine') OR ((cue = 'none') AND (task_id IS NULL)
                                        AND (place_note_id IS NULL))),
    CONSTRAINT intentions_check6 CHECK ((state = 'done') = (closed_at IS NOT NULL)),
    CONSTRAINT intentions_check7 CHECK ((state = 'archived') = (archived_at IS NOT NULL))
);
CREATE INDEX intentions_created ON intentions (created_at);
CREATE UNIQUE INDEX intentions_mail_dedupe ON intentions (source, source_ref, COALESCE(what,
        '')) WHERE source_ref IS NOT NULL;
CREATE INDEX intentions_note ON intentions (place_note_id) WHERE place_note_id IS NOT NULL;
CREATE INDEX intentions_open ON intentions (kind, created_at) WHERE state IN ('proposed', 'open');
CREATE INDEX intentions_task ON intentions (task_id) WHERE task_id IS NOT NULL;

CREATE FUNCTION intentions_never_deleted() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF coalesce(current_setting('pisa.erase', true), '') <> 'on' THEN
        RAISE EXCEPTION 'intentions are archived, never deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER intentions_never_deleted BEFORE DELETE ON intentions
    FOR EACH ROW
    EXECUTE FUNCTION intentions_never_deleted();

-- A promise in the user's own sent email.
CREATE TABLE owner_promises (
    id           uuid PRIMARY KEY,
    email_id     text NOT NULL,
    what         text NOT NULL CHECK ((char_length(what) >= 2) AND (char_length(what) <= 120)),
    to_name      text NOT NULL CHECK ((char_length(to_name) >= 1) AND (char_length(to_name) <= 120)),
    said_on      date NOT NULL,
    due_on       date,
    task_id      uuid REFERENCES tasks (id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL,
    intention_id uuid REFERENCES intentions (id) ON DELETE SET NULL,
    UNIQUE (email_id, what)
);


-- ==========================================================================================================
-- Profile and memory
-- What Pisa knows about the user, its evidence, the episodes of the user's days and Pisa's note.
-- ==========================================================================================================

-- Batches of claims Pisa retired, so they can be restored.
CREATE TABLE profile_retirements (
    id          uuid PRIMARY KEY,
    at          timestamptz NOT NULL,
    count       integer NOT NULL DEFAULT 0,
    restored_at timestamptz
);

-- A claim about the user, its scope, state and support.
CREATE TABLE profile_claims (
    id                uuid PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('like', 'dislike', 'routine', 'constraint', 'goal',
                                           'style', 'mobility', 'source', 'sensitivity')),
    statement         text NOT NULL,
    subject           jsonb NOT NULL DEFAULT '{}',
    polarity          real,
    scope             jsonb NOT NULL DEFAULT '{}',
    provenance        text NOT NULL CHECK (provenance IN ('said', 'observed', 'inferred', 'confirmed')),
    support           real NOT NULL DEFAULT 0,
    against           real NOT NULL DEFAULT 0,
    confidence        real NOT NULL DEFAULT 0,
    state             text NOT NULL DEFAULT 'active' CHECK (state IN ('candidate', 'active', 'asking',
                                                            'retired')),
    sensitive         boolean NOT NULL DEFAULT false,
    hard              boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    last_evidence_at  timestamptz,
    last_confirmed_at timestamptz,
    expires_at        timestamptz,
    subject_key       text,
    value             jsonb,
    confirmed_value   jsonb,
    retired_by        text CHECK (retired_by IN ('owner', 'expired', 'pisa')),
    owner_edited      boolean NOT NULL DEFAULT false,
    retired_reason    text CHECK (retired_reason IN ('no_subject', 'card_only')),
    retired_from      text CHECK (retired_from IN ('candidate', 'active', 'asking')),
    retired_batch     uuid REFERENCES profile_retirements (id) ON DELETE SET NULL
);
CREATE INDEX profile_claims_active ON profile_claims (state, expires_at);
CREATE INDEX profile_claims_batch ON profile_claims (retired_batch) WHERE retired_batch IS NOT NULL;
CREATE INDEX profile_claims_subject ON profile_claims (subject_key) WHERE subject_key IS NOT NULL;

-- One row per piece of evidence behind a claim.
CREATE TABLE profile_evidence (
    id          uuid PRIMARY KEY,
    claim_id    uuid REFERENCES profile_claims (id) ON DELETE CASCADE,
    signal      text NOT NULL,
    ref         text,
    weight      real NOT NULL,
    direction   smallint NOT NULL CHECK (direction = ANY (ARRAY['-1'::integer, 1])),
    scope       jsonb NOT NULL DEFAULT '{}',
    observed_at timestamptz NOT NULL,
    summary     text NOT NULL,
    kind        text,
    subject_key text,
    value       jsonb
);
CREATE INDEX profile_evidence_claim ON profile_evidence (claim_id, observed_at);
CREATE UNIQUE INDEX profile_evidence_once ON profile_evidence (signal, ref,
        subject_key) WHERE subject_key IS NOT NULL;
CREATE INDEX profile_evidence_subject ON profile_evidence (subject_key, observed_at);

-- Topics Pisa must not learn, and tombstones of forgotten claims.
CREATE TABLE profile_off_limits (
    id           uuid PRIMARY KEY,
    topic        text,
    claim_kind   text,
    subject_hash text,
    created_at   timestamptz NOT NULL,
    CONSTRAINT profile_off_limits_check CHECK ((topic IS NOT NULL) <> (subject_hash IS NOT NULL))
);

-- The weekly profile review.
CREATE TABLE profile_weekly (
    id          uuid PRIMARY KEY,
    week        date NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('confirm', 'changed', 'new', 'expiring', 'buffer')),
    claim_id    uuid REFERENCES profile_claims (id) ON DELETE CASCADE,
    statement   text NOT NULL,
    why         text NOT NULL,
    evidence    jsonb NOT NULL DEFAULT '[]',
    value       jsonb,
    created_at  timestamptz NOT NULL,
    answered_at timestamptz,
    answer      text CHECK (answer IN ('right', 'wrong', 'forget', 'yes', 'no'))
);
CREATE INDEX profile_weekly_week ON profile_weekly (week);

-- The night's extraction pass: one row per conversation read.
CREATE TABLE profile_extractions (
    conversation_id uuid PRIMARY KEY REFERENCES conversations (id) ON DELETE CASCADE,
    night           date NOT NULL,
    status          text NOT NULL CHECK (status IN ('done', 'empty', 'failed')),
    proposed        smallint NOT NULL DEFAULT 0,
    stored          smallint NOT NULL DEFAULT 0,
    reinforced      smallint NOT NULL DEFAULT 0,
    cost_eur        numeric(10,6) NOT NULL DEFAULT 0,
    at              timestamptz NOT NULL
);
CREATE INDEX profile_extractions_night ON profile_extractions (night);

-- The extraction pass per night: counts and spend.
CREATE TABLE profile_extract_nights (
    night         date PRIMARY KEY,
    conversations smallint NOT NULL DEFAULT 0,
    stored        smallint NOT NULL DEFAULT 0,
    reinforced    smallint NOT NULL DEFAULT 0,
    waiting       smallint NOT NULL DEFAULT 0,
    spent_eur     numeric(10,6) NOT NULL DEFAULT 0,
    capped        boolean NOT NULL DEFAULT false,
    at            timestamptz NOT NULL
);

-- The portrait written from the active claims (one row).
CREATE TABLE profile_portrait (
    id          smallint PRIMARY KEY,
    text        text NOT NULL,
    themes      jsonb NOT NULL DEFAULT '[]',
    built_at    timestamptz NOT NULL,
    claims_used smallint NOT NULL DEFAULT 0,
    claims_hash text NOT NULL,
    lang        text NOT NULL,
    source_ids  jsonb NOT NULL DEFAULT '[]',
    CONSTRAINT profile_portrait_text_check CHECK (length(text) <= 400)
);

-- Every version of the user's standing instructions to Pisa.
CREATE TABLE owner_note_versions (
    version       integer PRIMARY KEY CHECK (version >= 1),
    text          text NOT NULL,
    change        text NOT NULL DEFAULT '',
    source        text NOT NULL CHECK (source IN ('turn', 'app', 'restore', 'held')),
    turn_id       text,
    device_id     uuid REFERENCES devices (id) ON DELETE SET NULL,
    restored_from integer REFERENCES owner_note_versions (version),
    created_at    timestamptz NOT NULL,
    CONSTRAINT owner_note_versions_text_check CHECK (char_length(text) <= 1000)
);

-- An episode of the user's day, cut by code.
CREATE TABLE episodes (
    id                 uuid PRIMARY KEY,
    key                text NOT NULL UNIQUE CHECK (length(key) <= 200),
    kind               text NOT NULL CHECK (kind IN ('stay', 'talk', 'event', 'trip_leg', 'day')),
    day                date NOT NULL,
    started_at         timestamptz NOT NULL,
    ended_at           timestamptz NOT NULL,
    tz                 text NOT NULL CHECK (length(tz) <= 64),
    place_id           uuid REFERENCES places (id) ON DELETE SET NULL,
    home               boolean NOT NULL DEFAULT false,
    digest             text CHECK (length(digest) <= 240),
    digest_by          text CHECK (digest_by IN ('code', 'model')),
    salience           real,
    stability          real NOT NULL DEFAULT 1,
    state              text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'kept', 'faded', 'gist')),
    corrected_by_owner boolean NOT NULL DEFAULT false,
    retrieved_at       timestamptz,
    confirmed_at       timestamptz,
    gained_at          timestamptz,
    created_night      date,
    digested_night     date,
    created_at         timestamptz NOT NULL,
    CONSTRAINT episodes_check CHECK (ended_at >= started_at)
);
CREATE INDEX episodes_day ON episodes (day, started_at);
CREATE INDEX episodes_place ON episodes (place_id) WHERE place_id IS NOT NULL;

-- Pointers from an episode to rows elsewhere.
CREATE TABLE episode_links (
    episode_id   uuid REFERENCES episodes (id) ON DELETE CASCADE,
    target_kind  text CHECK (target_kind IN ('person', 'place', 'task', 'event', 'message_range', 'decision',
                             'file', 'claim', 'thing', 'trip', 'email', 'episode')),
    target_id    text CHECK (length(target_id) <= 200),
    role         text CHECK (role IN ('with', 'at', 'about', 'made', 'said', 'near')),
    source       text NOT NULL CHECK (source IN ('code', 'owner')),
    sender_class text CHECK (sender_class IN ('contact', 'own', 'other')),
    PRIMARY KEY (episode_id, target_kind, target_id, role),
    CONSTRAINT episode_links_check CHECK ((target_kind = 'email') = (sender_class IS NOT NULL))
);
CREATE INDEX episode_links_target ON episode_links (target_kind, target_id);

-- The user's explicit remember / don't-remember marks.
CREATE TABLE pisa_explicit (
    key_hash   text PRIMARY KEY CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    episode_id uuid REFERENCES episodes (id) ON DELETE SET NULL,
    explicit   smallint NOT NULL CHECK (explicit = ANY (ARRAY[1, '-1'::integer])),
    provenance text NOT NULL DEFAULT 'owner_words' CHECK (provenance = 'owner_words'),
    message_id bigint REFERENCES messages (id) ON DELETE SET NULL,
    at         timestamptz NOT NULL
);
CREATE INDEX pisa_explicit_message_id ON pisa_explicit (message_id);

-- That something was forgotten, never what.
CREATE TABLE episode_tombstones (
    id           bigserial PRIMARY KEY,
    forgotten_at timestamptz NOT NULL,
    by_owner     boolean NOT NULL DEFAULT true CHECK (by_owner)
);


-- ==========================================================================================================
-- Views, skills and evaluations
-- Saved views, skills and their uses, private evaluations and the replay's numbers.
-- ==========================================================================================================

-- A named recipe of read-only reads and sheet blocks.
CREATE TABLE pisa_views (
    id          uuid PRIMARY KEY,
    name        text NOT NULL CHECK ((length(name) >= 1) AND (length(name) <= 40)),
    name_key    text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '' CHECK (length(description) <= 200),
    recipe      jsonb NOT NULL,
    provenance  text NOT NULL CHECK (provenance = 'said'),
    turn_id     uuid,
    device_id   uuid,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL,
    shown_at    timestamptz
);

-- A skill: a view recipe built from the user's wish, with its checks.
CREATE TABLE pisa_skills (
    id           uuid PRIMARY KEY,
    name         text NOT NULL CHECK ((char_length(name) >= 1) AND (char_length(name) <= 60)),
    name_key     text NOT NULL UNIQUE,
    kind         text NOT NULL CHECK (kind IN ('view', 'tool')),
    status       text NOT NULL CHECK (status IN ('proposed', 'trial', 'on', 'withered', 'off')),
    made_from    text NOT NULL CHECK (char_length(made_from) <= 300),
    checks       jsonb NOT NULL,
    recipe       jsonb,
    job          jsonb,
    tests_passed integer NOT NULL DEFAULT 0,
    digest       text NOT NULL,
    trial        jsonb,
    schedule     jsonb,
    last_run_on  date,
    enabled_at   timestamptz,
    last_used_at timestamptz,
    turn_id      text,
    device_id    uuid REFERENCES devices (id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT pisa_skills_check CHECK ((kind = 'tool') OR (recipe IS NOT NULL)),
    CONSTRAINT pisa_skills_check1 CHECK ((status <> 'on') OR (enabled_at IS NOT NULL))
);

-- Each use of a skill, and its outcome.
CREATE TABLE pisa_skill_uses (
    skill_id uuid NOT NULL REFERENCES pisa_skills (id) ON DELETE CASCADE,
    used_at  timestamptz NOT NULL,
    how      text NOT NULL CHECK (how IN ('voice', 'schedule', 'app')),
    outcome  text CHECK (outcome IN ('kept', 'undone', 'corrected', 'failed'))
);
CREATE INDEX pisa_skill_uses_skill ON pisa_skill_uses (skill_id, used_at DESC);
CREATE INDEX pisa_skill_uses_used_at ON pisa_skill_uses (used_at);

-- A complaint kept as a private replay candidate.
CREATE TABLE personal_evals (
    id          uuid PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('correction', 'undo', 'redone')),
    source_ref  text NOT NULL UNIQUE,
    lang        text NOT NULL,
    scenario    jsonb NOT NULL,
    made_at     timestamptz NOT NULL,
    exported_at timestamptz
);

-- Anonymised scenario candidates built from private evaluations.
CREATE TABLE pisa_scenario_candidates (
    id          uuid PRIMARY KEY,
    eval_id     uuid NOT NULL UNIQUE REFERENCES personal_evals (id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('correction', 'undo')),
    scenario    jsonb NOT NULL,
    replaced    jsonb NOT NULL,
    made_at     timestamptz NOT NULL,
    exported_at timestamptz
);

-- The replay's runs as numbers.
CREATE TABLE pisa_replay_runs (
    id        uuid PRIMARY KEY,
    at        timestamptz NOT NULL,
    effort    text NOT NULL CHECK ((length(effort) >= 1) AND (length(effort) <= 20)),
    k         integer NOT NULL CHECK (k >= 1),
    scenarios integer NOT NULL CHECK (scenarios >= 0),
    passed    integer NOT NULL,
    personal  boolean NOT NULL DEFAULT false,
    CONSTRAINT pisa_replay_runs_check CHECK ((passed >= 0) AND (passed <= scenarios))
);
CREATE INDEX pisa_replay_runs_at ON pisa_replay_runs (at DESC);

-- The weekly report, made by code from production rows.
CREATE TABLE pisa_owner_reports (
    week    text PRIMARY KEY CHECK (week ~ '^[0-9]{4}-W[0-9]{2}$'),
    report  jsonb NOT NULL,
    made_at timestamptz NOT NULL
);


-- ==========================================================================================================
-- Ideas, curiosity and the desk
-- Problems, fragments and ideas with their evidence; topics and unknowns; the desk's research.
-- ==========================================================================================================

-- A problem, from the user's words or records.
CREATE TABLE pisa_idea_problems (
    id          uuid PRIMARY KEY,
    source      text NOT NULL CHECK (source IN ('his_words', 'his_record')),
    ref         text NOT NULL,
    message_id  bigint REFERENCES messages (id) ON DELETE SET NULL,
    claim_id    uuid REFERENCES profile_claims (id) ON DELETE CASCADE,
    statement   text NOT NULL,
    hard        text,
    reframes    jsonb NOT NULL DEFAULT '[]',
    state       text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resting', 'closed')),
    created_at  timestamptz NOT NULL,
    touched_at  timestamptz NOT NULL,
    prepared_on date,
    UNIQUE (source, ref)
);
CREATE INDEX pisa_idea_problems_message_id ON pisa_idea_problems (message_id);

-- An idea fragment from the user's words.
CREATE TABLE pisa_idea_fragments (
    id         uuid PRIMARY KEY,
    message_id bigint UNIQUE REFERENCES messages (id) ON DELETE SET NULL,
    problem_id uuid REFERENCES pisa_idea_problems (id) ON DELETE SET NULL,
    text       text NOT NULL,
    said_at    timestamptz NOT NULL
);

-- Something new in the world, from a night search.
CREATE TABLE pisa_idea_deltas (
    id      uuid PRIMARY KEY,
    night   date NOT NULL,
    kind    text NOT NULL CHECK (kind IN ('api', 'price', 'law', 'dataset', 'capability', 'competitor',
                                 'other')),
    title   text NOT NULL,
    url     text NOT NULL UNIQUE,
    seen_at timestamptz NOT NULL
);

-- An idea and its state.
CREATE TABLE pisa_ideas (
    id            uuid PRIMARY KEY,
    night         date NOT NULL,
    problem_id    uuid REFERENCES pisa_idea_problems (id) ON DELETE CASCADE,
    statement     text NOT NULL,
    mechanism     text NOT NULL,
    market        text NOT NULL CHECK (market IN ('it', 'us', 'both')),
    kind          text NOT NULL CHECK (kind IN ('software', 'service', 'content', 'arbitrage', 'physical')),
    cycle         smallint NOT NULL CHECK ((cycle >= 1) AND (cycle <= 8)),
    seeds         jsonb NOT NULL DEFAULT '{}',
    levers        text[] NOT NULL DEFAULT '{}',
    vec           real[],
    distance      real,
    band          text CHECK (band IN ('near', 'atypical', 'far')),
    convergence   integer NOT NULL DEFAULT 1,
    verdict       text CHECK (verdict IN ('keep', 'drop')),
    state         text NOT NULL DEFAULT 'incubating' CHECK (state IN ('incubating', 'validating', 'ready',
                                                            'question', 'shown', 'kept', 'killed', 'revived')),
    waiting       text CHECK (waiting IN ('cap', 'not_provable', 'evidence', 'lab')),
    rung          text CHECK (rung IN ('V0', 'V1', 'V2', 'V4', 'V5')),
    kill_criteria jsonb NOT NULL DEFAULT '[]',
    kills_at      timestamptz,
    p_keep        real CHECK ((p_keep > 0) AND (p_keep < 1)),
    searches      integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);
CREATE INDEX pisa_ideas_state ON pisa_ideas (state, night);

-- One evidence line and the check that verified it.
CREATE TABLE pisa_idea_evidence (
    id         uuid PRIMARY KEY,
    idea_id    uuid NOT NULL REFERENCES pisa_ideas (id) ON DELETE CASCADE,
    rung       text NOT NULL CHECK (rung IN ('V0', 'V1', 'V2', 'V4', 'V5')),
    claim      text NOT NULL,
    url        text,
    final_url  text,
    number     double precision,
    run        jsonb,
    check_kind text NOT NULL CHECK (check_kind IN ('link', 'number', 'run', 'simulated', 'conscience')),
    passed     boolean,
    simulated  boolean NOT NULL DEFAULT false,
    checked_at timestamptz NOT NULL
);
CREATE INDEX pisa_idea_evidence_idea ON pisa_idea_evidence (idea_id);

-- A killed idea, what killed it and what would revive it.
CREATE TABLE pisa_idea_cemetery (
    idea_id     uuid PRIMARY KEY REFERENCES pisa_ideas (id) ON DELETE CASCADE,
    killed_at   timestamptz NOT NULL,
    criterion   integer,
    evidence_id uuid REFERENCES pisa_idea_evidence (id) ON DELETE SET NULL,
    revival     text NOT NULL
);

-- Which idea came from which.
CREATE TABLE pisa_idea_lineage (
    parent uuid REFERENCES pisa_ideas (id) ON DELETE CASCADE,
    child  uuid REFERENCES pisa_ideas (id) ON DELETE CASCADE,
    kind   text NOT NULL CHECK (kind IN ('variation', 'recombination')),
    night  date NOT NULL,
    PRIMARY KEY (parent, child)
);

-- The stepping-stone map: each cell's best idea.
CREATE TABLE pisa_idea_map (
    market  text CHECK (market IN ('it', 'us', 'both')),
    kind    text CHECK (kind IN ('software', 'service', 'content', 'arbitrage', 'physical')),
    idea_id uuid REFERENCES pisa_ideas (id) ON DELETE SET NULL,
    grade   smallint NOT NULL DEFAULT 0,
    night   date NOT NULL,
    PRIMARY KEY (market, kind)
);

-- A topic the user named, one at a time.
CREATE TABLE pisa_topics (
    id         uuid PRIMARY KEY,
    message_id bigint UNIQUE REFERENCES messages (id) ON DELETE SET NULL,
    name       text NOT NULL,
    state      text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'active', 'monthly', 'closed')),
    graph      jsonb,
    explainer  jsonb,
    scores     jsonb NOT NULL DEFAULT '[]',
    refresh_on date,
    liked      boolean,
    opened_at  timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

-- What Pisa knows it doesn't know.
CREATE TABLE pisa_unknowns (
    id               uuid PRIMARY KEY,
    source           text NOT NULL CHECK (source IN ('his_question', 'miss', 'topic', 'horizon', 'prurito')),
    ref              text NOT NULL,
    question         text NOT NULL,
    about            text NOT NULL CHECK (about IN ('life', 'work', 'world', 'self')),
    why              text,
    answer_by        text NOT NULL DEFAULT 'web' CHECK (answer_by IN ('web', 'public_source', 'his_data',
                                                        'ask_him', 'none')),
    topic_id         uuid REFERENCES pisa_topics (id) ON DELETE CASCADE,
    message_id       bigint REFERENCES messages (id) ON DELETE SET NULL,
    deadline         date,
    instrumental     real NOT NULL DEFAULT 0,
    cognitive        real NOT NULL DEFAULT 0,
    explicit         boolean NOT NULL DEFAULT false,
    hedonic_negative boolean NOT NULL DEFAULT false,
    neutral_title    text,
    novelty          real,
    progress         real,
    tractability     real,
    noisy            boolean NOT NULL DEFAULT false,
    interest         real,
    reads            integer NOT NULL DEFAULT 0,
    adds             integer NOT NULL DEFAULT 0,
    debated          boolean NOT NULL DEFAULT false,
    state            text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'parked', 'answered', 'dropped')),
    park_reason      text CHECK (park_reason IN ('untractable', 'noisy', 'open_for_experts', 'his_data')),
    on_shelf         boolean NOT NULL DEFAULT false,
    known            text,
    blocking         text,
    next_look        date,
    answer           text,
    answer_url       text,
    shown_at         timestamptz,
    liked            boolean,
    opened_at        timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    said_at          timestamptz,
    changes          boolean,
    told_at          timestamptz,
    reason           text CHECK ((reason IS NULL) OR (reason ~ '^trip:[0-9a-f-]{36}$')),
    UNIQUE (source, ref)
);
CREATE INDEX pisa_unknowns_message_id ON pisa_unknowns (message_id);
CREATE INDEX pisa_unknowns_open ON pisa_unknowns (state, next_look);

-- One read for an unknown, and what was found in it.
CREATE TABLE pisa_unknown_reads (
    id         uuid PRIMARY KEY,
    unknown_id uuid NOT NULL REFERENCES pisa_unknowns (id) ON DELETE CASCADE,
    night      date NOT NULL,
    gait       text NOT NULL CHECK (gait IN ('hunter', 'busybody', 'dancer', 'opposing', 'lateral')),
    url        text,
    claim      text,
    quote      text,
    supported  boolean NOT NULL DEFAULT false,
    adds       boolean NOT NULL DEFAULT false,
    answers    boolean NOT NULL DEFAULT false,
    debated    boolean NOT NULL DEFAULT false,
    at         timestamptz NOT NULL
);
CREATE INDEX pisa_unknown_reads_by ON pisa_unknown_reads (unknown_id, at);

-- A state change of an unknown.
CREATE TABLE pisa_unknown_changes (
    id         bigserial PRIMARY KEY,
    unknown_id uuid NOT NULL REFERENCES pisa_unknowns (id) ON DELETE CASCADE,
    night      date NOT NULL,
    change     text NOT NULL CHECK (change IN ('answered', 'blocked', 'deadline', 'parked', 'day')),
    UNIQUE (unknown_id, night, change)
);

-- A source's host, checked once and cached.
CREATE TABLE pisa_curious_sources (
    host       text PRIMARY KEY,
    reliable   boolean NOT NULL,
    checked_on date NOT NULL
);

-- Pisa's self-test on a topic.
CREATE TABLE pisa_topic_tests (
    id          uuid PRIMARY KEY,
    topic_id    uuid NOT NULL REFERENCES pisa_topics (id) ON DELETE CASCADE,
    written_on  date NOT NULL,
    question    text NOT NULL,
    expected    text NOT NULL,
    answered_on date,
    got         text,
    correct     boolean
);

-- The horizon scan: questions, never actions.
CREATE TABLE pisa_horizon (
    id         uuid PRIMARY KEY,
    night      date NOT NULL,
    item       text NOT NULL,
    question   text NOT NULL,
    unknown_id uuid REFERENCES pisa_unknowns (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    UNIQUE (item, question)
);

-- Dated windows from official pages, checked by code.
CREATE TABLE pisa_desk_windows (
    id         uuid PRIMARY KEY,
    country    text NOT NULL CHECK (country IN ('it', 'us', 'eu')),
    kind       text NOT NULL CHECK (kind IN ('grant', 'rule', 'fair', 'cfp', 'tax', 'budget')),
    title      text NOT NULL,
    url        text NOT NULL,
    day        date NOT NULL,
    quote      text NOT NULL,
    forms      text[] NOT NULL DEFAULT '{}',
    passed     boolean,
    first_seen date NOT NULL,
    checked_on date NOT NULL,
    UNIQUE (url, kind, day, title)
);
CREATE INDEX pisa_desk_windows_day ON pisa_desk_windows (day) WHERE passed;

-- A dossier per idea ready for a morning.
CREATE TABLE pisa_desk_dossiers (
    id         uuid PRIMARY KEY,
    idea_id    uuid NOT NULL UNIQUE REFERENCES pisa_ideas (id) ON DELETE CASCADE,
    night      date NOT NULL,
    market     text NOT NULL CHECK (market IN ('it', 'us', 'both')),
    neighbours uuid[] NOT NULL DEFAULT '{}'::uuid[],
    window_id  uuid REFERENCES pisa_desk_windows (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

-- What one country has and another lacks.
CREATE TABLE pisa_desk_arbitrage (
    id         uuid PRIMARY KEY,
    night      date NOT NULL,
    dossier_id uuid REFERENCES pisa_desk_dossiers (id) ON DELETE CASCADE,
    way        text NOT NULL CHECK (way IN ('product', 'regulation', 'cost', 'entry')),
    has        text NOT NULL CHECK (has IN ('it', 'us')),
    name       text NOT NULL,
    claim      text NOT NULL,
    url        text NOT NULL,
    other_url  text,
    passed     boolean,
    checked_at timestamptz NOT NULL
);

-- Who might buy a project and what they lack.
CREATE TABLE pisa_desk_acquirers (
    id         uuid PRIMARY KEY,
    dossier_id uuid NOT NULL REFERENCES pisa_desk_dossiers (id) ON DELETE CASCADE,
    name       text NOT NULL,
    lacks      text NOT NULL,
    url        text NOT NULL,
    filing     boolean NOT NULL DEFAULT false,
    passed     boolean,
    checked_at timestamptz NOT NULL
);

-- Public reviews, each quote found on its page.
CREATE TABLE pisa_desk_voices (
    id         uuid PRIMARY KEY,
    dossier_id uuid NOT NULL REFERENCES pisa_desk_dossiers (id) ON DELETE CASCADE,
    theme      text NOT NULL CHECK (theme IN ('pain', 'praise', 'wish')),
    quote      text NOT NULL,
    url        text NOT NULL,
    passed     boolean,
    checked_at timestamptz NOT NULL
);

-- A draft capital stack.
CREATE TABLE pisa_desk_money (
    dossier_id uuid PRIMARY KEY REFERENCES pisa_desk_dossiers (id) ON DELETE CASCADE,
    stack      jsonb NOT NULL,
    state      text NOT NULL DEFAULT 'draft' CHECK (state = 'draft'),
    updated_at timestamptz NOT NULL
);

-- The decision journal, Brier-scored.
CREATE TABLE pisa_desk_decisions (
    id          uuid PRIMARY KEY,
    dossier_id  uuid REFERENCES pisa_desk_dossiers (id) ON DELETE CASCADE,
    domain      text NOT NULL CHECK (domain IN ('market', 'timing_cost', 'window', 'sparring', 'bet',
                                     'consiglio')),
    question    text NOT NULL,
    pisa_p         real NOT NULL CHECK ((pisa_p > 0) AND (pisa_p < 1)),
    his_p       real CHECK ((his_p >= 0) AND (his_p <= 1)),
    resolves_by date NOT NULL,
    outcome     boolean,
    resolved_at timestamptz,
    created_at  timestamptz NOT NULL,
    UNIQUE (dossier_id, domain)
);

-- The journal's scores per night, party and domain.
CREATE TABLE pisa_desk_scores (
    night  date,
    party  text CHECK (party IN ('pisa', 'owner')),
    domain text CHECK (domain IN ('market', 'timing_cost', 'window', 'sparring', 'bet', 'consiglio')),
    brier  real NOT NULL,
    PRIMARY KEY (night, party, domain)
);

-- Pages the desk fetched per night.
CREATE TABLE pisa_desk_nights (
    night date PRIMARY KEY,
    pages integer NOT NULL DEFAULT 0
);


-- ==========================================================================================================
-- Rituals, nights and the morning
-- Ritual sheets and questions, the night's work and the morning's sheet and dossier.
-- ==========================================================================================================

-- Sheets a ritual made, one per ritual and day.
CREATE TABLE ritual_sheets (
    id         uuid PRIMARY KEY,
    ritual     text NOT NULL CHECK (ritual IN ('ghost_run', 'closing', 'week_poster', 'life_map', 'stanotte',
                                    'dammi_tutto')),
    day        date NOT NULL,
    data       jsonb NOT NULL,
    actions    jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL,
    updated_at timestamptz
);
CREATE UNIQUE INDEX ritual_sheets_once ON ritual_sheets (ritual, day);

-- Pisa's own one-yes questions.
CREATE TABLE ritual_asks (
    id          uuid PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('ghost_fix', 'idea', 'hunch', 'thought', 'knock', 'tragitto')),
    question    text NOT NULL,
    payload     jsonb NOT NULL,
    idea_key    text,
    idea_kind   text,
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'yes', 'no', 'lapsed', 'failed')),
    device_id   uuid REFERENCES devices (id) ON DELETE SET NULL,
    sheet_id    uuid REFERENCES ritual_sheets (id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    answered_at timestamptz,
    answered_by text CHECK (answered_by IN ('voice', 'tap', 'words')),
    result      text
);
CREATE UNIQUE INDEX ritual_asks_idea_once ON ritual_asks (idea_key) WHERE idea_key IS NOT NULL;
CREATE INDEX ritual_asks_open ON ritual_asks (created_at DESC) WHERE status = 'open';

-- Ideas already offered, never offered twice.
CREATE TABLE ritual_ideas_used (
    idea_key text PRIMARY KEY,
    used_at  timestamptz NOT NULL
);

-- Pisa's guesses about the user, asked once, never acted on unconfirmed.
CREATE TABLE hunches (
    id          uuid PRIMARY KEY,
    text        text NOT NULL,
    kind        text NOT NULL DEFAULT 'like',
    key         text NOT NULL UNIQUE,
    evidence    jsonb NOT NULL,
    status      text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'asked', 'confirmed', 'wrong')),
    ask_id      uuid REFERENCES ritual_asks (id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL,
    asked_at    timestamptz,
    answered_at timestamptz,
    claim_id    uuid REFERENCES profile_claims (id) ON DELETE SET NULL,
    CONSTRAINT hunches_text_check CHECK ((length(text) >= 1) AND (length(text) <= 200))
);

-- One row a night: what ran, counts only.
CREATE TABLE pisa_nights (
    night       date PRIMARY KEY,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'stopped', 'capped',
                                                       'failed')),
    counts      jsonb NOT NULL DEFAULT '{}',
    tools       text[] NOT NULL DEFAULT '{}',
    spent_eur   numeric(12,6) NOT NULL DEFAULT 0,
    brier       real
);

-- Every web search the night made, counted.
CREATE TABLE pisa_night_searches (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    night    date NOT NULL REFERENCES pisa_nights (night) ON DELETE CASCADE,
    at       timestamptz NOT NULL,
    kind     text NOT NULL CHECK (kind ~ '^[a-z0-9_]{1,40}$'),
    stripped integer NOT NULL DEFAULT 0,
    sent     boolean NOT NULL,
    refused  text CHECK (refused IN ('cap', 'empty', 'no_provider', 'budget'))
);
CREATE INDEX pisa_night_searches_night ON pisa_night_searches (night) WHERE sent;

-- Yesterday's turns replayed against tonight's Pisa.
CREATE TABLE pisa_night_replays (
    night      date REFERENCES pisa_nights (night) ON DELETE CASCADE,
    message_id bigint,
    tools      text[] NOT NULL DEFAULT '{}',
    missing    text[] NOT NULL DEFAULT '{}',
    replied    boolean NOT NULL,
    passed     boolean NOT NULL,
    PRIMARY KEY (night, message_id)
);

-- Predictions for tomorrow, scored the next night.
CREATE TABLE pisa_night_predictions (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    night     date NOT NULL REFERENCES pisa_nights (night) ON DELETE CASCADE,
    for_day   date NOT NULL,
    kind      text NOT NULL CHECK (kind IN ('talks', 'early', 'evening', 'busy', 'task_done', 'event_kept')),
    ref       text,
    p         real NOT NULL CHECK ((p > 0) AND (p < 1)),
    outcome   boolean,
    scored_at timestamptz,
    CONSTRAINT pisa_night_predictions_for_day_kind_ref_key UNIQUE NULLS NOT DISTINCT (for_day, kind, ref)
);
CREATE INDEX pisa_night_predictions_open ON pisa_night_predictions (for_day) WHERE outcome IS NULL;

-- The morning sheet, built each night with every line's source.
CREATE TABLE pisa_sheets (
    night      date PRIMARY KEY REFERENCES pisa_nights (night) ON DELETE CASCADE,
    built_at   timestamptz NOT NULL,
    data       jsonb NOT NULL,
    everything jsonb NOT NULL,
    lines      jsonb NOT NULL DEFAULT '[]',
    shown      integer NOT NULL DEFAULT 0,
    passed     integer NOT NULL DEFAULT 0,
    idea_id    uuid REFERENCES pisa_ideas (id) ON DELETE SET NULL,
    story      text NOT NULL,
    gate       text NOT NULL DEFAULT 'shadow' CHECK (gate IN ('shadow', 'open')),
    sheet_id   uuid,
    tutto_id   uuid,
    sent_at    timestamptz
);

-- Pisa's first-person lines from its own records.
CREATE TABLE pisa_diary (
    id         uuid PRIMARY KEY,
    night      date NOT NULL REFERENCES pisa_nights (night) ON DELETE CASCADE,
    line       text NOT NULL,
    refs       jsonb NOT NULL DEFAULT '[]',
    origin     text NOT NULL DEFAULT 'pisa_self' CHECK (origin = 'pisa_self'),
    created_at timestamptz NOT NULL
);
CREATE INDEX pisa_diary_night ON pisa_diary (night);

-- The monthly open-endedness meter, counts only.
CREATE TABLE pisa_meter (
    month       date PRIMARY KEY,
    his_liked   integer NOT NULL DEFAULT 0,
    verified    integer NOT NULL DEFAULT 0,
    map_cells   integer NOT NULL DEFAULT 0,
    answered    integer NOT NULL DEFAULT 0,
    flat        boolean NOT NULL DEFAULT false,
    computed_at timestamptz NOT NULL
);

-- One item of a morning's dossier.
CREATE TABLE dossier_items (
    id           uuid PRIMARY KEY,
    day          date NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('provato', 'floor', 'radar', 'loop', 'draft', 'thought',
                                      'sheet', 'prurito', 'perche', 'consiglio', 'prova', 'quarto')),
    rank         smallint NOT NULL,
    src_table    text NOT NULL CHECK ((char_length(src_table) >= 1) AND (char_length(src_table) <= 60)),
    ref          text NOT NULL CHECK ((char_length(ref) >= 1) AND (char_length(ref) <= 200)),
    text         text NOT NULL,
    sources      jsonb NOT NULL DEFAULT '[]',
    outside      boolean NOT NULL DEFAULT false,
    current      boolean NOT NULL DEFAULT true,
    spoken       boolean NOT NULL DEFAULT false,
    said_at      timestamptz,
    thumb        text CHECK (thumb IN ('up', 'down')),
    thumbed_at   timestamptz,
    built_at     timestamptz NOT NULL,
    created_at   timestamptz NOT NULL,
    thumb_origin text CHECK (thumb_origin IN ('phone', 'watch')),
    UNIQUE (day, src_table, ref),
    CONSTRAINT dossier_items_check CHECK (spoken = (said_at IS NOT NULL)),
    CONSTRAINT dossier_items_check1 CHECK ((thumb IS NULL) = (thumbed_at IS NULL)),
    CONSTRAINT dossier_items_text_check CHECK ((char_length(text) >= 1) AND (char_length(text) <= 400))
);
CREATE INDEX dossier_items_day ON dossier_items (day, rank);

CREATE FUNCTION dossier_spoken_max() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.spoken AND (SELECT count(*) FROM dossier_items WHERE day = NEW.day AND spoken AND id <> NEW.id) >= 3 THEN
        RAISE EXCEPTION 'dossier: at most three items are spoken a morning';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER dossier_spoken_max BEFORE INSERT OR UPDATE OF spoken ON dossier_items
    FOR EACH ROW
    EXECUTE FUNCTION dossier_spoken_max();


-- ==========================================================================================================
-- Pisa's voice and character
-- Cue questions, named states, Pisa's character and opinions, and the opt-in rituals.
-- ==========================================================================================================

-- Pisa's cue questions and how they were answered.
CREATE TABLE pisa_cue_asks (
    id          uuid PRIMARY KEY,
    device_id   uuid,
    tempo       text NOT NULL CHECK (tempo IN ('up', 'down')),
    channels    text[] NOT NULL,
    reason      text NOT NULL CHECK (reason IN ('two_channels', 'missing_fact')),
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'named', 'waved',
                                                    'unanswered')),
    asked_at    timestamptz NOT NULL,
    answered_at timestamptz
);
CREATE INDEX pisa_cue_asks_at ON pisa_cue_asks (asked_at);

-- A cue kind that went quiet.
CREATE TABLE pisa_cue_quiet (
    tempo text PRIMARY KEY CHECK (tempo IN ('up', 'down')),
    since timestamptz NOT NULL,
    why   text NOT NULL CHECK (why IN ('waved', 'unanswered'))
);

-- A state the user named and asked Pisa to remember.
CREATE TABLE pisa_his_states (
    id      uuid PRIMARY KEY,
    tempo   text NOT NULL CHECK (tempo IN ('up', 'down')),
    word    text NOT NULL CHECK ((char_length(word) >= 1) AND (char_length(word) <= 40)),
    kept_at timestamptz NOT NULL
);

-- Pisa's traits, each proposed once for the user's approval.
CREATE TABLE pisa_character (
    trait       text PRIMARY KEY,
    status      text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'declined')),
    proposed_at timestamptz NOT NULL,
    decided_at  timestamptz
);

-- Pisa's opinions, each said once.
CREATE TABLE pisa_opinions (
    key     text PRIMARY KEY,
    kind    text NOT NULL CHECK (kind IN ('disagree', 'habit')),
    said_at timestamptz NOT NULL
);

-- Arguing over an idea, once per idea a day.
CREATE TABLE pisa_spars (
    id          uuid PRIMARY KEY,
    idea_id     uuid NOT NULL REFERENCES pisa_ideas (id) ON DELETE CASCADE,
    day         date NOT NULL,
    decision_id uuid REFERENCES pisa_desk_decisions (id) ON DELETE SET NULL,
    points      jsonb NOT NULL DEFAULT '[]',
    created_at  timestamptz NOT NULL,
    UNIQUE (idea_id, day)
);

-- A bet the user opened, resolved from a public source.
CREATE TABLE pisa_bets (
    id           uuid PRIMARY KEY,
    message_id   bigint REFERENCES messages (id) ON DELETE SET NULL,
    claim        text NOT NULL,
    resolver     text NOT NULL CHECK (resolver IN ('rain', 'window')),
    by_day       date NOT NULL,
    yes          boolean NOT NULL DEFAULT true,
    subject      text[] NOT NULL DEFAULT '{}',
    place        jsonb,
    his_p        real CHECK ((his_p >= 0) AND (his_p <= 1)),
    pisa_p          real NOT NULL CHECK ((pisa_p > 0) AND (pisa_p < 1)),
    decision_id  uuid REFERENCES pisa_desk_decisions (id) ON DELETE SET NULL,
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'won', 'lost', 'unresolved')),
    resolved_url text,
    resolved_at  timestamptz,
    created_at   timestamptz NOT NULL
);
CREATE INDEX pisa_bets_message_id ON pisa_bets (message_id);
CREATE INDEX pisa_bets_open ON pisa_bets (by_day) WHERE state = 'open';

-- The opt-in quarter hour.
CREATE TABLE pisa_quarti (
    id         uuid PRIMARY KEY,
    week       date NOT NULL,
    offered_at timestamptz NOT NULL,
    started_at timestamptz,
    ends_at    timestamptz,
    ended_at   timestamptz,
    state      text NOT NULL DEFAULT 'offered' CHECK (state IN ('offered', 'talking', 'ended')),
    refs       jsonb NOT NULL DEFAULT '[]'
);
CREATE INDEX pisa_quarti_week ON pisa_quarti (week);

-- The opt-in weekly council.
CREATE TABLE pisa_consigli (
    id          uuid PRIMARY KEY,
    week        date NOT NULL,
    offered_at  timestamptz NOT NULL,
    started_at  timestamptz,
    ends_at     timestamptz,
    ended_at    timestamptz,
    state       text NOT NULL DEFAULT 'offered' CHECK (state IN ('offered', 'talking', 'ended')),
    part        smallint NOT NULL DEFAULT 0,
    idea_id     uuid REFERENCES pisa_ideas (id) ON DELETE SET NULL,
    decision_id uuid REFERENCES pisa_desk_decisions (id) ON DELETE SET NULL,
    refs        jsonb NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX pisa_consigli_week ON pisa_consigli (week);

-- The opt-in rehearsal of next week's collisions.
CREATE TABLE pisa_prove (
    id         uuid PRIMARY KEY,
    week       date NOT NULL,
    offered_at timestamptz NOT NULL,
    started_at timestamptz,
    ends_at    timestamptz,
    ended_at   timestamptz,
    state      text NOT NULL DEFAULT 'offered' CHECK (state IN ('offered', 'talking', 'ended')),
    at         smallint NOT NULL DEFAULT 0,
    items      jsonb NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX pisa_prove_week ON pisa_prove (week);

-- A request held on one idea.
CREATE TABLE pisa_swarm_asks (
    id         uuid PRIMARY KEY,
    idea_id    uuid NOT NULL REFERENCES pisa_ideas (id) ON DELETE CASCADE,
    message_id bigint REFERENCES messages (id) ON DELETE SET NULL,
    state      text NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'starting', 'started', 'refused')),
    day        date,
    job_ids    text[] NOT NULL DEFAULT '{}',
    reason     text,
    created_at timestamptz NOT NULL,
    started_at timestamptz
);
CREATE UNIQUE INDEX pisa_swarm_asks_held ON pisa_swarm_asks (idea_id) WHERE state = 'held';
CREATE INDEX pisa_swarm_asks_message_id ON pisa_swarm_asks (message_id);


-- ==========================================================================================================
-- Voice profiles
-- Speaker verification: consent, prints, enrolment audio and the public cohort.
-- ==========================================================================================================

-- The user's explicit consent, with the text's version.
CREATE TABLE voice_consents (
    id         uuid PRIMARY KEY,
    version    text NOT NULL CHECK ((length(version) >= 1) AND (length(version) <= 40)),
    device_id  uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- One profile per route: sealed prints and calibration.
CREATE TABLE voice_profiles (
    route         text PRIMARY KEY CHECK (route IN ('speaker', 'receiver', 'airpods', 'headphones', 'car',
                                          'glasses')),
    model         text NOT NULL,
    model_version text NOT NULL,
    prints        bytea NOT NULL,
    calibration   jsonb NOT NULL,
    bench         jsonb NOT NULL DEFAULT '{}',
    samples_s     real NOT NULL,
    ok            boolean NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Each enrolment block's sealed audio.
CREATE TABLE voice_recordings (
    id         uuid PRIMARY KEY,
    route      text NOT NULL CHECK (route IN ('speaker', 'receiver', 'airpods', 'headphones', 'car',
                                    'glasses')),
    block      text NOT NULL CHECK (block IN ('A', 'B', 'C', 'D', 'E')),
    pcm        bytea NOT NULL,
    seconds    real NOT NULL,
    spans      jsonb NOT NULL DEFAULT '[]',
    held_out   jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX voice_recordings_route ON voice_recordings (route, created_at);

-- Public speakers' embeddings, for normalisation.
CREATE TABLE voice_cohort (
    id         bigserial PRIMARY KEY,
    model      text NOT NULL,
    lang       text NOT NULL,
    embedding  bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX voice_cohort_model ON voice_cohort (model);

-- Public babble audio for the bench.
CREATE TABLE voice_babble (
    id         bigserial PRIMARY KEY,
    langs      text NOT NULL,
    talkers    smallint NOT NULL,
    pcm        bytea NOT NULL,
    seconds    real NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);


-- ==========================================================================================================
-- Learning from sources
-- Papers, pages and files the user gave Pisa, their structure, retrieval units and answers.
-- ==========================================================================================================

-- One source the user gave Pisa.
CREATE TABLE learn_sources (
    id            uuid PRIMARY KEY,
    course        text,
    key           text NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('arxiv', 'doi', 'url', 'title', 'pdf', 'markdown')),
    ref           text NOT NULL,
    arxiv_id      text,
    doi           text,
    url           text,
    title         text,
    authors       text[] NOT NULL DEFAULT '{}',
    year          integer,
    licence       text,
    origin        text,
    parser        text,
    parse_quality real,
    citable       boolean NOT NULL DEFAULT true,
    lang          text NOT NULL DEFAULT 'en',
    state         text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'fetching', 'indexing', 'ready',
                                                        'needs_copy', 'candidates', 'failed')),
    error         text,
    candidates    jsonb,
    upload        bytea,
    content_hash  text,
    embed_model   text,
    segments      integer NOT NULL DEFAULT 0,
    chunks        integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    indexed_at    timestamptz
);
CREATE INDEX learn_sources_arxiv ON learn_sources (arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX learn_sources_doi ON learn_sources (lower(doi)) WHERE doi IS NOT NULL;
CREATE UNIQUE INDEX learn_sources_key ON learn_sources (COALESCE(course, ''), key);
CREATE INDEX learn_sources_state ON learn_sources (state) WHERE state IN ('queued', 'fetching', 'indexing');

-- A source's structure, in reading order.
CREATE TABLE learn_segments (
    source_id      uuid REFERENCES learn_sources (id) ON DELETE CASCADE,
    ord            integer,
    kind           text NOT NULL CHECK (kind IN ('paragraph', 'table', 'caption', 'equation', 'appendix')),
    section_path   text[] NOT NULL DEFAULT '{}',
    page_from      integer,
    page_to        integer,
    label          text,
    text           text NOT NULL,
    is_description boolean NOT NULL DEFAULT false,
    cites          text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, ord)
);

-- Retrieval units, with a tsvector and an optional embedding.
CREATE TABLE learn_chunks (
    id             bigserial PRIMARY KEY,
    source_id      uuid NOT NULL REFERENCES learn_sources (id) ON DELETE CASCADE,
    seg_from       integer NOT NULL,
    seg_to         integer NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('paragraph', 'table', 'caption', 'equation', 'appendix')),
    text           text NOT NULL,
    prefix_it      text NOT NULL,
    prefix_en      text NOT NULL,
    is_description boolean NOT NULL DEFAULT false,
    ts_config      regconfig NOT NULL DEFAULT 'english'::regconfig,
    tsv            tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector(ts_config, text), 'A')
        || setweight(to_tsvector(ts_config, prefix_en), 'B')
        || setweight(to_tsvector('italian'::regconfig, prefix_it), 'C')
    ) STORED,
    embedding      vector,
    embed_model    text
);
CREATE INDEX learn_chunks_source ON learn_chunks (source_id, seg_from);
CREATE INDEX learn_chunks_tsv ON learn_chunks USING gin (tsv);

-- Each answer from the sources, with its citations.
CREATE TABLE learn_answer_log (
    id           uuid PRIMARY KEY,
    course       text,
    question     text NOT NULL,
    lang         text NOT NULL,
    rationale    boolean NOT NULL DEFAULT false,
    claims       jsonb NOT NULL,
    passages     jsonb NOT NULL,
    lexical_only boolean NOT NULL,
    tiers        integer[] NOT NULL,
    latency_ms   integer NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX learn_answer_log_created ON learn_answer_log (created_at);


-- ==========================================================================================================
-- Identity and merchants
-- Pisa's own signing keys and the merchants' endpoints it learnt.
-- ==========================================================================================================

-- Pisa's Ed25519 keys; the private half is sealed.
CREATE TABLE pisa_identity_keys (
    kid             text PRIMARY KEY CHECK (kid ~ '^[A-Za-z0-9_-]{43}$'),
    public_x        text NOT NULL UNIQUE CHECK (public_x ~ '^[A-Za-z0-9_-]{43}$'),
    sealed          bytea NOT NULL,
    created_at      timestamptz NOT NULL,
    retired_at      timestamptz,
    published_until timestamptz,
    CONSTRAINT pisa_identity_keys_check CHECK ((retired_at IS NULL) = (published_until IS NULL))
);
CREATE UNIQUE INDEX pisa_identity_keys_one_active ON pisa_identity_keys ((true)) WHERE retired_at IS NULL;

-- An allowlisted merchant and its endpoint.
CREATE TABLE ucp_merchants (
    host         text PRIMARY KEY CHECK (host ~ '^[a-z0-9.-]+\.[a-z]{2,}$'),
    checked_at   timestamptz NOT NULL,
    has_ucp      boolean NOT NULL,
    version      text,
    endpoint     text CHECK (endpoint ~ '^https://\S+$'),
    capabilities text[] NOT NULL DEFAULT '{}',
    error        text,
    CONSTRAINT ucp_merchants_check CHECK (has_ucp = (endpoint IS NOT NULL))
);


-- ==========================================================================================================
-- Measures (views)
-- The competence ledger and the signals, computed from the outcomes Pisa already records.
-- ==========================================================================================================

-- Every recorded outcome, one row each: per action type and per skill. `value`: kept 1, corrected 0.5, undone 0,
-- failed 0, unrated NULL. A draft left unsent counts as undone a day after it was made. `ref`: the source row, a
-- stable tie-break for outcomes at the same moment.
CREATE VIEW pisa_outcomes AS
SELECT 'type'::text AS kind, a.kind AS key, a.executed_at AS at,
       CASE WHEN a.undone_at IS NULL THEN 'kept' ELSE 'undone' END AS outcome, a.id::text AS ref
FROM approvals a WHERE a.status = 'done' AND a.executed_at IS NOT NULL
UNION ALL
SELECT 'type', 'task_done', d.done_at, CASE WHEN d.undone_at IS NULL THEN 'kept' ELSE 'undone' END, d.id::text
FROM done_by_presence d WHERE d.task_id IS NOT NULL AND d.done_at IS NOT NULL
UNION ALL
SELECT 'type', 'task_close', d.at, CASE WHEN t.status = 'dismissed' THEN 'kept' ELSE 'undone' END, d.id::text
FROM pisa_decisions d JOIN tasks t ON t.id::text = d.ref WHERE d.move = 'act' AND d.tool = 'aging'
UNION ALL
SELECT 'type', 'email_draft', e.created_at, CASE WHEN e.sent_at IS NULL THEN 'undone' ELSE 'kept' END, e.id::text
FROM email_drafts e WHERE e.sent_at IS NOT NULL OR e.created_at < now() - interval '1 day'
UNION ALL
SELECT 'skill', u.skill_id::text, u.used_at, u.outcome, u.ctid::text FROM pisa_skill_uses u;  -- no id: its ctid

-- Each rated outcome with its rank from the newest (r = 0) within its type or skill, and its value.
CREATE VIEW pisa_outcomes_ranked AS
SELECT o.kind, o.key, o.at, o.outcome, o.ref,
       CASE o.outcome WHEN 'kept' THEN 1.0 WHEN 'corrected' THEN 0.5 ELSE 0.0 END AS value,
       row_number() OVER (PARTITION BY o.kind, o.key ORDER BY o.at DESC, o.ref) - 1 AS r
FROM pisa_outcomes o WHERE o.outcome IS NOT NULL;

-- Per type and per skill: runs, outcomes, and two trusts, exponential moving averages of the values with a
-- half-life of 5 runs (trust_fast) and 30 runs (trust_slow) from a prior of 0.5. Closed form of
-- t_k = t_(k-1) + a (x_k - t_(k-1)), t_0 = 0.5, a = 1 - 2^(-1/h):
--   t_n = 0.5 (1 - a)^n + sum over the n outcomes of a (1 - a)^r x_r, r = 0 for the newest.
CREATE VIEW pisa_competence AS
WITH runs AS (
    SELECT kind, key, count(*) AS runs FROM pisa_outcomes GROUP BY kind, key
    UNION ALL
    SELECT 'skill', s.id::text, 0 FROM pisa_skills s
    WHERE NOT EXISTS (SELECT 1 FROM pisa_skill_uses u WHERE u.skill_id = s.id)
), rated AS (
    SELECT kind, key, count(*) AS n,
           count(*) FILTER (WHERE outcome = 'kept') AS kept,
           count(*) FILTER (WHERE outcome = 'undone') AS undone,
           count(*) FILTER (WHERE outcome = 'corrected') AS corrected,
           count(*) FILTER (WHERE outcome = 'failed') AS failed,
           sum((1 - power(2, -1.0 / 5)) * power(power(2, -1.0 / 5), r) * value) AS fast,
           sum((1 - power(2, -1.0 / 30)) * power(power(2, -1.0 / 30), r) * value) AS slow
    FROM pisa_outcomes_ranked GROUP BY kind, key
)
SELECT u.kind, u.key, u.runs::int AS runs,
       coalesce(r.kept, 0)::int AS kept, coalesce(r.undone, 0)::int AS undone,
       coalesce(r.corrected, 0)::int AS corrected, coalesce(r.failed, 0)::int AS failed,
       (0.5 * power(power(2, -1.0 / 5), coalesce(r.n, 0)) + coalesce(r.fast, 0))::float8 AS trust_fast,
       (0.5 * power(power(2, -1.0 / 30), coalesce(r.n, 0)) + coalesce(r.slow, 0))::float8 AS trust_slow,
       coalesce(r.n, 0)::int AS n
FROM runs u LEFT JOIN rated r ON r.kind = u.kind AND r.key = u.key;

-- The signals, one row each (at, source, ref, signal, value):
-- - plan_change in [0, 1], from the replan candidates;
-- - outcome_error, signed: each outcome against the slow trust of its type or skill just before it;
-- - novelty 1: the first stay at a place, and the first time Pisa ran a tool on the user's words.
CREATE VIEW pisa_signals AS
SELECT c.created_at AS at, 'attend:' || c.source AS source, c.id::text AS ref, 'plan_change'::text AS signal,
       c.plan_change::float8 AS value
FROM attend_candidates c WHERE c.plan_change IS NOT NULL
UNION ALL
SELECT o.at, o.kind || ':' || o.key, NULL, 'outcome_error',
       (o.value - (0.5 * power(power(2, -1.0 / 30), coalesce(p.older, 0)) + coalesce(p.slow, 0)))::float8
FROM pisa_outcomes_ranked o
LEFT JOIN LATERAL (
    SELECT count(*) AS older,
           sum((1 - power(2, -1.0 / 30)) * power(power(2, -1.0 / 30), b.r - o.r - 1) * b.value) AS slow
    FROM pisa_outcomes_ranked b WHERE b.kind = o.kind AND b.key = o.key AND b.r > o.r
) p ON true
UNION ALL
SELECT s.arrived_at, 'stay', s.place_id::text, 'novelty', 1.0::float8
FROM (SELECT DISTINCT ON (place_id) place_id, arrived_at FROM stays WHERE place_id IS NOT NULL
      ORDER BY place_id, arrived_at) s
UNION ALL
SELECT d.at, 'decision:' || d.tool, d.ref, 'novelty', 1.0::float8
FROM (SELECT DISTINCT ON (tool) tool, at, ref FROM pisa_decisions WHERE provenance = 'owner_words'
      ORDER BY tool, at, n) d;


-- ==========================================================================================================
-- Seed rows
-- ==========================================================================================================

INSERT INTO app_meta (key, value) VALUES ('schema_baseline', '0001');

-- Model slots, routed to a model in Settings (or `pisa models set`); NULL: not set yet. Every slot starts
-- empty: Pisa has no default model.
INSERT INTO model_slots (id, model_ref) VALUES
    ('briefing.compose', NULL),
    ('chat.conversation', NULL),
    ('intake.extract', NULL),
    ('intake.triage', NULL),
    ('learn.answer', NULL),
    ('memory.embed', NULL),
    ('places.research', NULL),
    ('plan.explain', NULL),
    ('understand.confirm', NULL),
    ('vision.describe', NULL);
