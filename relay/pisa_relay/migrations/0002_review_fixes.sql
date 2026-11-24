-- SPDX-License-Identifier: AGPL-3.0-only
-- The security review's fixes. No relay was deployed before this migration, so the earlier rows (sealed with
-- the storage key itself, subscription references without the environment) need no conversion.

-- StoreKit's environment, part of the subscription reference since this version; Apple's billing grace.
ALTER TABLE registration ADD COLUMN storekit_environment TEXT NOT NULL DEFAULT 'Production'
    CHECK (storekit_environment IN ('Production', 'Sandbox'));
ALTER TABLE registration ADD COLUMN grace_expires_at INTEGER;  -- Unix seconds, while in billing grace
-- created_at and last_used_at now hold the hour (YYYY-MM-DDTHH), for the eviction order.

-- A sealed canary: the relay refuses to start when RELAY_STORAGE_KEY does not open it.
CREATE TABLE meta (
    name TEXT PRIMARY KEY,
    value BLOB NOT NULL
);

-- The Live Activity tokens a handle may push to: its push-to-start tokens and its activities' update tokens,
-- registered by the app (signed). Only their SHA-256 is kept.
CREATE TABLE activity_token (
    handle TEXT NOT NULL,
    token_hash BLOB NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('start', 'update')),
    added_at INTEGER NOT NULL,
    PRIMARY KEY (handle, token_hash, kind)
);

-- Subscriptions Apple said were refunded or revoked: a transaction signed before then does not register again.
CREATE TABLE ended_subscription (
    subscription_ref BLOB NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (subscription_ref, expires_at)
);
