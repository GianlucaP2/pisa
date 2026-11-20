-- ==========================================================================================================
-- Security review E1, part 2: what a key may sign (P3), and session nonces bound to a device (P2)
-- ==========================================================================================================

-- A key's kind. `biometric`: the phone's Face ID key (`/v1/phone/keys`), which signs everything, approvals
-- included. `signer`: a device key granted on the server (`pisa pair --signer`, key id `device-<id>`), with no
-- biometry: it signs device ops only.
ALTER TABLE phone_keys ADD COLUMN kind text NOT NULL DEFAULT 'biometric'
    CHECK (kind IN ('biometric', 'signer'));
UPDATE phone_keys SET kind = 'signer' WHERE key_id LIKE 'device-%';

-- A nonce asked for with `?deviceId=` serves only that device, the newest 5 outstanding per device; one asked
-- for without it is limited by client address as before, and all of those together by a global cap.
ALTER TABLE session_nonces ADD COLUMN device_id uuid REFERENCES devices (id) ON DELETE CASCADE;
CREATE INDEX session_nonces_device ON session_nonces (device_id, expires_at) WHERE (device_id IS NOT NULL);
