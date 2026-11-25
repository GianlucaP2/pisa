-- ==========================================================================================================
-- OpenAI-compatible servers
-- The user's own model servers, each a provider with its own id, and what each of their models can do.
-- ==========================================================================================================

-- One row per server. Its API key, when it has one, is in provider_keys under the same id.
CREATE TABLE model_servers (
    id         text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{1,31}$'),
    name       text NOT NULL,
    base_url   text NOT NULL,
    ca_bundle  text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per probed or overridden model. `probe` holds the probe's result per capability (null: never
-- probed), `overrides` only the capabilities the user set by hand. The provider is a model_servers id, or
-- the server PISA_OLLAMA_BASE_URL describes, so it has no foreign key.
CREATE TABLE model_capabilities (
    provider  text NOT NULL,
    model_id  text NOT NULL,
    probe     jsonb CHECK (probe IS NULL OR jsonb_typeof(probe) = 'object'),
    probed_at timestamptz,
    overrides jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(overrides) = 'object'),
    PRIMARY KEY (provider, model_id)
);
