-- ==========================================================================================================
-- Security review E1 P2: no pairing lockout of an address, and never a device id beside a client address
-- ==========================================================================================================

-- `pairing_failures` stays, as the address backstop only (CONTRACT-2 §4.3 step 5: keyed on the IPv6 /64 or the
-- IPv4 address, set much higher than 5); no failure touches a token.

-- A device id and a client address are never kept in one record: a known device id lets someone push out that
-- device's bound session nonces. A bound nonce keeps no address. A pairing keeps its address only while it is
-- begun (the per-address count of open begins), and its device id is drawn when the address goes.
UPDATE session_nonces SET address = NULL WHERE device_id IS NOT NULL;
ALTER TABLE session_nonces ALTER COLUMN address DROP NOT NULL;
ALTER TABLE session_nonces ADD CONSTRAINT session_nonces_device_or_address
    CHECK ((device_id IS NULL) <> (address IS NULL));
UPDATE pairings SET address = NULL WHERE state <> 'begun';
ALTER TABLE pairings ADD CONSTRAINT pairings_address_while_begun CHECK ((state = 'begun') OR (address IS NULL));
