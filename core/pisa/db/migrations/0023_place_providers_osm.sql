-- v0.1.4: the OpenStreetMap fallback. `openrouteservice` keeps the user's own OpenRouteService key (sealed like
-- the Google and Tavily keys) for travel times on OSM's roads; `osm` (no key) keeps only its last test.
ALTER TABLE place_providers DROP CONSTRAINT place_providers_provider_check;
ALTER TABLE place_providers ADD CONSTRAINT place_providers_provider_check
    CHECK (provider IN ('apple', 'google', 'tavily', 'osm', 'openrouteservice')) NOT VALID;
ALTER TABLE place_providers VALIDATE CONSTRAINT place_providers_provider_check;
