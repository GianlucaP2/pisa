-- SPDX-License-Identifier: AGPL-3.0-only
-- The app's own key: sha256(app_secret), sealed like secret_hash, for the requests only the
-- app may sign (its Live Activity tokens, the subscription's device list). NULL only for rows registered
-- before this version.
ALTER TABLE registration ADD COLUMN app_key BLOB;
-- 1 when the registration proved the key behind the transaction's appAccountToken.
ALTER TABLE registration ADD COLUMN account_bound INTEGER NOT NULL DEFAULT 0;
