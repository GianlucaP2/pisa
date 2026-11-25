-- SPDX-License-Identifier: AGPL-3.0-only
-- One row per registered device. Nothing about the user's life: an opaque handle, where to deliver, the key
-- that signs pushes to it (sealed), which subscription pays for it (a keyed hash), and when that ends.
CREATE TABLE registration (
    handle TEXT PRIMARY KEY,
    apns_token TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('production', 'sandbox')),
    secret_hash BLOB NOT NULL,                 -- sha256(secret), the HMAC key, sealed with AES-256-GCM
    subscription_ref BLOB NOT NULL,            -- HMAC-SHA256(storage key, originalTransactionId)
    subscription_expires_at INTEGER NOT NULL,  -- Unix seconds
    language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'it')),
    created_at TEXT NOT NULL,                  -- the day only
    last_used_at TEXT                          -- the day only
);
CREATE INDEX registration_subscription ON registration (subscription_ref);

-- Short-lived bookkeeping, swept every few minutes: accepted signatures (the replay window) and rate counters.
CREATE TABLE accepted_signature (
    signature BLOB PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
CREATE TABLE usage (
    handle TEXT NOT NULL,
    bucket TEXT NOT NULL,
    count INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (handle, bucket)
);
