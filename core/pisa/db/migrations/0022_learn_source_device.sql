-- Pisa Learn (v0.1.2): the device that added a source, so its ingestion (which embeds the text) checks that
-- device's AI consent when it runs, at a restart's resume too. NULL: added before this column (any paired
-- device's consent then), or the device is gone.
ALTER TABLE learn_sources ADD COLUMN device_id uuid REFERENCES devices (id) ON DELETE SET NULL;
