-- ==========================================================================================================
-- Security review E1, re-check of M5 (M5-1): a key vouched by a device key is no Face ID key
-- ==========================================================================================================

-- A key's kind gains `device`: a key registered under the vouch of a client's own device key (D-138). It has a
-- device key's limits (it signs for its own device only; a watch's never approves), never a Face ID key's.
-- `biometric` stays for the first key (the host-paired iPhone) and for keys vouched by a Face ID key.
ALTER TABLE phone_keys DROP CONSTRAINT phone_keys_kind_check;
ALTER TABLE phone_keys ADD CONSTRAINT phone_keys_kind_check CHECK (kind IN ('biometric', 'signer', 'device'));

-- The device whose key vouched for this one (`add_key`); null for the first key and for a server-made signer.
-- Revoking that device deletes the keys it vouched for, and a key whose voucher is revoked signs nothing.
ALTER TABLE phone_keys ADD COLUMN vouched_by uuid REFERENCES devices (id) ON DELETE CASCADE;
CREATE INDEX phone_keys_vouched_by ON phone_keys (vouched_by) WHERE (vouched_by IS NOT NULL);
