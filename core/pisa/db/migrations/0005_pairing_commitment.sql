-- ==========================================================================================================
-- Pairing: the server commits to its nonce first (CONTRACT-2 §4.3 step 3, §15 S1), and a paired phone can offer
-- a pairing and confirm it itself (§4.3.1)
-- ==========================================================================================================

-- POST /v1/pair/begin makes the pairing row (state `begun`) with serverNonce, before any token or transcript is
-- known; POST /v1/pair fills in the rest. The transcript's fields are kept as sent: the offering phone computes
-- the SAS from them.
ALTER TABLE pairings ALTER COLUMN token_id DROP NOT NULL;
ALTER TABLE pairings ALTER COLUMN device_key DROP NOT NULL;
ALTER TABLE pairings ALTER COLUMN name DROP NOT NULL;
ALTER TABLE pairings ALTER COLUMN platform DROP NOT NULL;
ALTER TABLE pairings ALTER COLUMN mac DROP NOT NULL;
ALTER TABLE pairings ALTER COLUMN sas DROP NOT NULL;
ALTER TABLE pairings ADD COLUMN address text CHECK (char_length(address) <= 200);
ALTER TABLE pairings ADD COLUMN spki text CHECK (char_length(spki) <= 400);
ALTER TABLE pairings ADD COLUMN device_key_sent text CHECK (char_length(device_key_sent) <= 400);
ALTER TABLE pairings ADD COLUMN client_nonce text CHECK (char_length(client_nonce) <= 400);
ALTER TABLE pairings DROP CONSTRAINT pairings_state_check;
ALTER TABLE pairings ADD CONSTRAINT pairings_state_check
    CHECK (state IN ('begun', 'awaiting_confirmation', 'confirmed', 'refused', 'expired'));
ALTER TABLE pairings ADD CONSTRAINT pairings_requested_check
    CHECK ((state = 'begun') OR (token_id IS NULL AND mac IS NULL)
           OR (token_id IS NOT NULL AND device_key IS NOT NULL AND name IS NOT NULL AND platform IS NOT NULL
               AND mac IS NOT NULL AND sas IS NOT NULL));
CREATE INDEX pairings_begun ON pairings (address, expires_at) WHERE (state = 'begun');

-- A token a paired phone offered (POST /v1/pair/offers): that phone, not the server's CLI, confirms its pairing.
ALTER TABLE pair_tokens ADD COLUMN offer_id text UNIQUE CHECK (char_length(offer_id) <= 80);
ALTER TABLE pair_tokens ADD COLUMN offered_by uuid REFERENCES devices (id) ON DELETE CASCADE;
