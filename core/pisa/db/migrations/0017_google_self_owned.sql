-- Google is the user's own: their own Google project and web client, and the sign-in comes back to their own
-- server (`GET /oauth/google/callback`). The iPhone's Google Sign-In client (a client shared by every install)
-- is gone, and so is its column.
ALTER TABLE connector_credentials DROP COLUMN ios_client_id;

-- One sign-in under way (`POST /v1/connectors/google/start`): its state, by SHA-256 only (the state itself
-- travels through Google and the browser); the PKCE verifier sealed with PISA_SECRETS_KEY; the device that
-- started it; the redirect it was made for; the language of the page the browser comes back to. Single use
-- (`used_at`), ten minutes. A different account than the connected one waits there for the device's yes.
CREATE TABLE google_oauth_states (
    state_hash   text PRIMARY KEY CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    device_id    uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    verifier_enc bytea NOT NULL,
    redirect_uri text NOT NULL,
    lang         text NOT NULL,
    created_at   timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    used_at      timestamptz,
    -- the account Google came back with, held until the starting device confirms it (a leaked sign-in link
    -- finished with someone else's account connects nothing, and deletes nothing): sealed, 15 minutes
    pending_account     text,
    pending_scopes      text[],
    pending_refresh_enc bytea,
    pending_access_enc  bytea,
    pending_access_expires_at timestamptz,
    pending_until       timestamptz
);
CREATE INDEX google_oauth_states_expires ON google_oauth_states (expires_at);
