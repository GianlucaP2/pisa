-- LOCAL-1 L1: the operator's own model servers. Per server: how long a request may wait for its next byte
-- (a CPU reads a long prompt for minutes) and, per slot, whether its calls may think. Per model: what the
-- probe learnt beside the capabilities (the context the server serves it with, whether it thinks, which
-- server it is), and the last silent cut of a prompt seen (token counts and when; never the prompt).
ALTER TABLE model_servers ADD COLUMN read_timeout_s integer NOT NULL DEFAULT 60
    CHECK (read_timeout_s BETWEEN 10 AND 900);
ALTER TABLE model_servers ADD COLUMN thinking jsonb NOT NULL DEFAULT '{}'
    CHECK (jsonb_typeof(thinking) = 'object');
ALTER TABLE model_capabilities ADD COLUMN facts jsonb NOT NULL DEFAULT '{}'
    CHECK (jsonb_typeof(facts) = 'object');
ALTER TABLE model_capabilities ADD COLUMN truncated jsonb
    CHECK (truncated IS NULL OR jsonb_typeof(truncated) = 'object');
