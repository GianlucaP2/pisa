-- v0.1.4, Collegamenti (Connections): the user's OK of a paid maps or web provider (Google Maps, Tavily) and
-- its own monthly cap, in cents, given with a signed `paid_ack` (`domain.connections`). A provider that bills
-- is never called without its row here. Nothing is inserted: a key set before v0.1.4 stays unused until the
-- user acknowledges its provider in Connections. Its spend is the provider's `place_calls` this month.
CREATE TABLE paid_acks (
    provider  text PRIMARY KEY CHECK (provider ~ '^[a-z][a-z0-9_]{1,39}$'),
    cap_cents integer NOT NULL CHECK (cap_cents > 0),
    acked_at  timestamptz NOT NULL,
    device_id uuid REFERENCES devices (id) ON DELETE SET NULL  -- the device that signed it
);
