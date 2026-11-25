-- The processors behind each AI disclosure revision a user consented to (`services/disclosure.record`): a
-- consent covers a later disclosure that sends the same or less (`domain/disclosure.covers`), so turning a
-- processor off (natural → economy drops the talker) never asks for consent again; adding or changing one
-- does. Names of slots, providers, models and data categories only: never the user's data.
CREATE TABLE ai_disclosures (
    revision text PRIMARY KEY CHECK (revision ~ '^[0-9a-f]{16,64}$'),
    processors jsonb NOT NULL,
    recorded_at timestamptz NOT NULL
);
