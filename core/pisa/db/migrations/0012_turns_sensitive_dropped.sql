-- A REST turn's wrist mark: nothing writes it since the REST turn path went, and nothing reads it now. The
-- wrist rail applies where a short answer is spoken (`api/talk.py` `spoken`).
ALTER TABLE turns DROP COLUMN sensitive;
