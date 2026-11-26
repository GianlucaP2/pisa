-- Memoria (v0.1.2): their standing instructions, one a row, and their free notes.
--
-- owner_instructions: what Pisa keeps doing because they asked ("d'ora in poi, ogni volta che…"), in their
-- own words (`turn`), in Pisa's words after their one yes (`held`), from Settings → Memoria (`app`), or copied
-- from the v0.1 note (`note`). Every turn reads them as their preferences; no rail reads them. The caps (at most
-- 20 rows and 2,000 characters in all) are the service's; a row's own length is checked here.
CREATE TABLE owner_instructions (
    id         bigserial PRIMARY KEY,
    text       text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 280),
    source     text NOT NULL CHECK (source IN ('turn', 'held', 'app', 'note')),
    turn_id    text,
    device_id  uuid REFERENCES devices (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- owner_notes: their notes ("annota: …"), found again by their words, and by meaning when the `memory.embed`
-- slot has a model (`embedding`, made by that model, `model_ref`; NULL without one).
CREATE TABLE owner_notes (
    id         bigserial PRIMARY KEY,
    title      text CHECK (title IS NULL OR char_length(title) BETWEEN 1 AND 120),
    text       text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 4000),
    source     text NOT NULL CHECK (source IN ('turn', 'app')),
    device_id  uuid REFERENCES devices (id) ON DELETE SET NULL,
    model_ref  text,
    embedding  vector,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT owner_notes_embedding_model CHECK ((embedding IS NULL) = (model_ref IS NULL))
);

CREATE INDEX owner_notes_updated ON owner_notes (updated_at DESC);

-- The v0.1 note's lines become instructions (its newest version, at most 20 lines, each cut to 280
-- characters; the note held at most 1,000). The note's versions stay, read-only.
INSERT INTO owner_instructions (text, source, created_at, updated_at)
SELECT left(btrim(regexp_replace(l.line, '\s+', ' ', 'g')), 280), 'note', v.created_at, v.created_at
FROM (SELECT text, created_at FROM owner_note_versions ORDER BY version DESC LIMIT 1) v,
     LATERAL unnest(string_to_array(v.text, E'\n')) WITH ORDINALITY AS l (line, n)
WHERE btrim(l.line) <> ''
ORDER BY l.n
LIMIT 20;
