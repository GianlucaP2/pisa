-- ==========================================================================================================
-- Any country (audit M1): no pair of countries is the user's
-- ==========================================================================================================

-- A task bound to a country, a document's country and a corrected residency day take any ISO 3166-1 alpha-2
-- code, not only Italy and the US. A document may have no country (not said, and no default for its kind: a
-- visa, or a passport on an install with no PISA_COUNTRY).
ALTER TABLE tasks DROP CONSTRAINT tasks_country_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_country_check CHECK (country ~ '^[A-Z]{2}$');

ALTER TABLE vault_documents DROP CONSTRAINT vault_documents_country_check;
ALTER TABLE vault_documents ALTER COLUMN country DROP NOT NULL;
ALTER TABLE vault_documents ADD CONSTRAINT vault_documents_country_check CHECK (country ~ '^[A-Z]{2}$');

ALTER TABLE residency_days DROP CONSTRAINT residency_days_corrected_check;
ALTER TABLE residency_days ADD CONSTRAINT residency_days_corrected_check CHECK (
    (array_to_string(corrected, ',') ~ '^([A-Z]{2}|other)(,([A-Z]{2}|other))*$') AND (cardinality(corrected) >= 1)
);
