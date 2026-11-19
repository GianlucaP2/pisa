-- ==========================================================================================================
-- The privacy revision of contract 2 (CONTRACT-2 §4.3-§4.5)
-- QR pairing with HMAC and SAS, the device key and its short-lived session tokens, and the user's AI consent.
-- ==========================================================================================================

-- A device paired by QR has a device key (P-256, SubjectPublicKeyInfo DER) and no v1 token. The CLI pairs as a
-- device of its own (platform cli) with a software key; a watch may pair on its own (watchos).
ALTER TABLE devices ALTER COLUMN token_hash DROP NOT NULL;
ALTER TABLE devices ADD COLUMN device_key bytea CHECK ((device_key IS NULL) OR (octet_length(device_key) <= 200));
ALTER TABLE devices DROP CONSTRAINT devices_platform_check;
ALTER TABLE devices ADD CONSTRAINT devices_platform_check
    CHECK (platform IN ('ios', 'watchos', 'android', 'web', 'cli', 'other'));
-- The AI disclosure revision the user allowed on this device for REST-started work (POST /v1/ai/consent), and
-- whether health may go to AI. A run's own consent (forwardedProps.pisa.consent) wins over it.
ALTER TABLE devices ADD COLUMN ai_consent_revision text CHECK (ai_consent_revision ~ '^[0-9a-f]{16,64}$');
ALTER TABLE devices ADD COLUMN ai_consent_health boolean NOT NULL DEFAULT false;
ALTER TABLE devices ADD COLUMN ai_consent_at timestamptz;
-- The latest AI disclosure and endpoints revisions this device was told (REST or pisa.* event): a change
-- since then reaches the device as pisa.ai.disclosure or pisa.endpoints when it connects.
ALTER TABLE devices ADD COLUMN ai_told text CHECK (char_length(ai_told) <= 64);
ALTER TABLE devices ADD COLUMN endpoints_told text CHECK (char_length(endpoints_told) <= 64);

-- One-time pairing tokens (the QR's 128-bit token, also the typed fallback), made by `pisa pair` on the host.
-- The token is kept (sealed with the secrets key when the server has one) because the pairing request's HMAC is
-- checked with it; it is erased when used, refused or expired.
CREATE TABLE pair_tokens (
    id             uuid PRIMARY KEY,
    token          bytea,
    label          text NOT NULL CHECK ((char_length(label) >= 1) AND (char_length(label) <= 60)),
    origin         text NOT NULL DEFAULT 'host' CHECK (origin IN ('host', 'app')),
    -- the paired device's key also signs device ops (`pisa pair --signer`, a headless install)
    signer         boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL,
    expires_at     timestamptz NOT NULL,
    used_at        timestamptz
);

-- A pairing request (POST /v1/pair) waiting for the user's confirmation on the server side, then its outcome.
-- The device row is made only when the user confirms.
CREATE TABLE pairings (
    id           text PRIMARY KEY CHECK ((char_length(id) >= 16) AND (char_length(id) <= 80)),
    token_id     uuid NOT NULL REFERENCES pair_tokens (id) ON DELETE CASCADE,
    device_id    uuid NOT NULL UNIQUE,
    device_key   bytea NOT NULL CHECK (octet_length(device_key) <= 200),
    name         text NOT NULL CHECK ((char_length(name) >= 1) AND (char_length(name) <= 60)),
    platform     text NOT NULL CHECK (platform IN ('ios', 'watchos', 'android', 'web', 'cli', 'other')),
    server_nonce text NOT NULL,
    mac          text NOT NULL,
    sas          text NOT NULL CHECK (sas ~ '^[0-9]{6}$'),
    state        text NOT NULL DEFAULT 'awaiting_confirmation'
                 CHECK (state IN ('awaiting_confirmation', 'confirmed', 'refused', 'expired')),
    created_at   timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    decided_at   timestamptz
);
CREATE INDEX pairings_token ON pairings (token_id);

-- Session nonces (GET /v1/devices/session/nonce): single use, 60 s, at most 5 outstanding per client address.
-- Only their SHA-256 is kept.
CREATE TABLE session_nonces (
    nonce_hash text PRIMARY KEY,
    address    text NOT NULL CHECK ((char_length(address) >= 1) AND (char_length(address) <= 200)),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);
CREATE INDEX session_nonces_address ON session_nonces (address, expires_at);

-- Session tokens (POST /v1/devices/session): 1 h bearers made by the device key's signature. Only their SHA-256
-- is kept.
CREATE TABLE device_sessions (
    token_hash text PRIMARY KEY,
    device_id  uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX device_sessions_device ON device_sessions (device_id);
