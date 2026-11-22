-- ==========================================================================================================
-- Contract 2 replaces v1 (B6): only session tokens authenticate
-- ==========================================================================================================

-- A device is reached only through its device key's session tokens (POST /v1/devices/session). A device paired
-- with a v1 code has no device key: it can no longer authenticate, so it is revoked (it pairs again by QR,
-- `pisa pair`), and its v1 token's hash goes.
UPDATE devices SET revoked_at = now() WHERE device_key IS NULL AND revoked_at IS NULL;
ALTER TABLE devices DROP COLUMN token_hash;

-- v1's one-time pairing codes (`pisa devices pair`, POST /v1/devices/pairing-code), which made v1 tokens.
DROP TABLE pairing_codes;
