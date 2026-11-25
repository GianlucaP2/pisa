-- v0.1.4: places found by OpenStreetMap keep its element's id (`node/123`, `way/45`, `relation/6`) to be found
-- again by it, as Apple's and Google's ids are; and a place's coordinates may come from OpenStreetMap or from
-- the phone's own maps (contract 2.3's `pisa.maps`, whose ids are the phone's and are never kept).
ALTER TABLE places ADD COLUMN osm_id text;
ALTER TABLE places ADD CONSTRAINT places_osm_id_key UNIQUE (osm_id);
ALTER TABLE places DROP CONSTRAINT places_coords_source_check;
ALTER TABLE places ADD CONSTRAINT places_coords_source_check
    CHECK (coords_source IN ('apple', 'google', 'osm', 'phone')) NOT VALID;
ALTER TABLE places VALIDATE CONSTRAINT places_coords_source_check;
