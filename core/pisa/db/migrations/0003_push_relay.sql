-- ==========================================================================================================
-- Push through the relay
-- A device's registration with the push relay (contract 2, PUT /v1/devices/me/push mode "relay"). A device has
-- at most one push mode: this row (relay), devices.apns_token (direct), or neither (none).
-- ==========================================================================================================

-- The relay's opaque handle for the device, the secret the relay shares with it (sealed with the secrets key),
-- and the device's X25519 public key the notices are sealed to. Never returned by the API.
CREATE TABLE device_push_relay (
    device_id  uuid PRIMARY KEY REFERENCES devices (id) ON DELETE CASCADE,
    handle     text NOT NULL CHECK ((char_length(handle) >= 1) AND (char_length(handle) <= 64)),
    secret     bytea NOT NULL,
    public_key bytea NOT NULL CHECK (octet_length(public_key) = 32),
    created_at timestamptz NOT NULL
);
