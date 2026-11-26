-- v0.1.6 B1: the foundation of email understanding and the life graph (docs/pisa/EMAIL-UNDERSTANDING.md,
-- docs/pisa/LIFE-GRAPH.md). Additive only: new tables, and new nullable or defaulted columns, so every row
-- that exists stays valid and the old intake keeps working.
-- Mail text (a body, a cut text, a quote) is kept 30 days after it was fetched, then set to NULL by the
-- nightly purge (`services/mail_purge.py`); keys stay while a linked task is open and go 30 days after it
-- closes; everything read from Gmail goes when Google is disconnected. `source` says which kind of episode a
-- row was learned from, so a disconnect takes only what Gmail gave.

-- --------------------------------------------------------------------------------------------------------
-- The owner model: the addresses the owner writes from. An alias is learned from the From header of a SENT
-- message and counts only once the owner confirmed it (confirmed_at).
-- --------------------------------------------------------------------------------------------------------
CREATE TABLE owner_identity (
    account       text NOT NULL,
    alias         text NOT NULL CHECK (alias = lower(alias)),
    display_name  text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at  timestamptz,
    PRIMARY KEY (account, alias)
);

-- --------------------------------------------------------------------------------------------------------
-- Mail: the text without its quoted history, the quoted history apart, who wrote it.
-- --------------------------------------------------------------------------------------------------------
ALTER TABLE email_messages ADD COLUMN new_text text;
ALTER TABLE email_messages ADD COLUMN quoted text;
ALTER TABLE email_messages ADD COLUMN owner_authored boolean NOT NULL DEFAULT false;
ALTER TABLE email_messages ADD COLUMN cut_version smallint NOT NULL DEFAULT 0;

-- A thread as Pisa knows it. `truncated`: more messages exist than one sync may fetch.
CREATE TABLE email_threads (
    id            text PRIMARY KEY,
    first_at      timestamptz NOT NULL,
    last_at       timestamptz NOT NULL,
    message_count integer NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    owner_last_at timestamptz,
    truncated     boolean NOT NULL DEFAULT false,
    fetched_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_threads_last ON email_threads (last_at DESC);

-- --------------------------------------------------------------------------------------------------------
-- Things and their hard keys (an order, a parcel, a booking, an invoice, a case).
-- --------------------------------------------------------------------------------------------------------
CREATE TABLE entity (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       text NOT NULL DEFAULT 'other' CHECK (kind IN ('order', 'parcel', 'booking', 'invoice',
                                                    'appointment', 'case', 'document', 'trip', 'other')),
    issuer     text,
    source     text NOT NULL DEFAULT 'mail' CHECK (source IN ('mail', 'calendar', 'chat', 'phone', 'owner')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- A key as the issuer wrote it. `verbatim_source` is the key's own characters as found in the mail (never
-- more); `value` is its normal form. The same value under another issuer is another key.
CREATE TABLE entity_key (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       uuid NOT NULL REFERENCES entity (id) ON DELETE CASCADE,
    key_kind        text NOT NULL CHECK (key_kind IN ('order_id', 'tracking', 'booking_ref', 'invoice_no',
                                                      'other')),
    issuer          text NOT NULL DEFAULT '',
    value           text NOT NULL,
    verbatim_source text NOT NULL,
    message_id      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (entity_id, key_kind, issuer, value)
);
CREATE INDEX entity_key_lookup ON entity_key (key_kind, issuer, value);

-- Organisations: a merchant, a carrier, a bank. Never a person.
CREATE TABLE orgs (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL,
    domains    text[] NOT NULL DEFAULT '{}',
    kind       text NOT NULL DEFAULT 'other' CHECK (kind IN ('merchant', 'carrier', 'marketplace', 'bank',
                                                    'utility', 'government', 'health', 'employer', 'other')),
    source     text NOT NULL DEFAULT 'mail' CHECK (source IN ('mail', 'calendar', 'chat', 'phone', 'owner')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orgs_domains ON orgs USING gin (domains);

-- --------------------------------------------------------------------------------------------------------
-- What a message says happened. The events call (a later change) fills it. `kind` is free text on purpose:
-- the list of event kinds lives in code. `debtor`: who owes the next action.
-- --------------------------------------------------------------------------------------------------------
CREATE TABLE mail_event (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id  text NOT NULL REFERENCES email_messages (id) ON DELETE CASCADE,
    thread_id   text,
    entity_id   uuid REFERENCES entity (id) ON DELETE SET NULL,
    kind        text NOT NULL,
    actor       text NOT NULL DEFAULT 'other' CHECK (actor IN ('owner', 'other')),
    debtor      text NOT NULL DEFAULT 'none' CHECK (debtor IN ('owner', 'other', 'none')),
    occurred_at timestamptz,
    confidence  real CHECK (confidence >= 0 AND confidence <= 1),
    quote       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX mail_event_message ON mail_event (message_id);
CREATE INDEX mail_event_entity ON mail_event (entity_id) WHERE entity_id IS NOT NULL;

-- --------------------------------------------------------------------------------------------------------
-- Tasks under the state machine. NULL state: a task the machine has not seen yet (every existing one).
-- --------------------------------------------------------------------------------------------------------
ALTER TABLE tasks ADD COLUMN state text CHECK (state IN ('open', 'waiting', 'done', 'cancelled', 'superseded',
                                                         'snoozed'));
ALTER TABLE tasks ADD COLUMN waiting_until timestamptz;
ALTER TABLE tasks ADD COLUMN entity_id uuid REFERENCES entity (id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN intent text;

-- Why a task changed: the message (and its event), the decisive short quote, how sure. `undone_at`: undone.
CREATE TABLE task_evidence (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id       uuid NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    message_id    text NOT NULL,
    mail_event_id uuid REFERENCES mail_event (id) ON DELETE SET NULL,
    transition    text,
    quote         text,
    confidence    real CHECK (confidence >= 0 AND confidence <= 1),
    undone_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX task_evidence_task ON task_evidence (task_id, created_at DESC);

-- --------------------------------------------------------------------------------------------------------
-- The life graph: edges in three grades, and facts with two time axes.
-- --------------------------------------------------------------------------------------------------------
-- A soft edge only proposes. Nodes are named by (kind, id): an entity, an org, a person, a storyline.
CREATE TABLE life_edge (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    grade      text NOT NULL CHECK (grade IN ('hard', 'structural', 'soft')),
    state      text NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed', 'accepted', 'rejected')),
    src_kind   text NOT NULL,
    src_id     text NOT NULL,
    dst_kind   text NOT NULL,
    dst_id     text NOT NULL,
    relation   text NOT NULL DEFAULT 'same_thing',
    reason     text,
    confidence real CHECK (confidence >= 0 AND confidence <= 1),
    decided_by text CHECK (decided_by IN ('code', 'model', 'owner')),
    source     text NOT NULL DEFAULT 'mail' CHECK (source IN ('mail', 'calendar', 'chat', 'phone', 'owner')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (src_kind, src_id, dst_kind, dst_id, relation)
);
CREATE INDEX life_edge_src ON life_edge (src_kind, src_id);
CREATE INDEX life_edge_dst ON life_edge (dst_kind, dst_id);

-- A fact: what was true (valid_from, valid_to) and when Pisa learned it (recorded_at). A newer fact does not
-- erase the older one: `superseded_by` points at it. `provenance`: how it is known; `source`: which episode
-- it came from; `source_ref`: that episode's id (a message id).
CREATE TABLE assertion (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_kind  text NOT NULL,
    subject_id    text NOT NULL,
    predicate     text NOT NULL,
    object_kind   text,
    object_id     text,
    value         jsonb NOT NULL DEFAULT '{}',
    valid_from    timestamptz NOT NULL,
    valid_to      timestamptz,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    provenance    text NOT NULL DEFAULT 'extracted' CHECK (provenance IN ('stated', 'extracted', 'inferred',
                                                                         'playbook', 'owner')),
    confidence    real NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    source        text NOT NULL DEFAULT 'mail' CHECK (source IN ('mail', 'calendar', 'chat', 'phone', 'owner')),
    source_ref    text,
    superseded_by uuid REFERENCES assertion (id) ON DELETE SET NULL,
    quote         text,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX assertion_subject ON assertion (subject_kind, subject_id, valid_from DESC);
CREATE INDEX assertion_recorded ON assertion (recorded_at);

-- --------------------------------------------------------------------------------------------------------
-- Storylines: a sale, a refund case, a trip; their events with the evidence behind each, and an undo.
-- --------------------------------------------------------------------------------------------------------
CREATE TABLE storyline (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       text NOT NULL DEFAULT 'other',
    label      text,
    state      text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'waiting', 'done', 'cancelled')),
    next_actor text NOT NULL DEFAULT 'none' CHECK (next_actor IN ('owner', 'other', 'none')),
    entity_id  uuid REFERENCES entity (id) ON DELETE SET NULL,
    thread_id  text,
    source     text NOT NULL DEFAULT 'mail' CHECK (source IN ('mail', 'calendar', 'chat', 'phone', 'owner')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The evidence is a reference: the message and its event; the decisive quote lives on the event, where the
-- 30-day purge reaches it.
CREATE TABLE storyline_event (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    storyline_id  uuid NOT NULL REFERENCES storyline (id) ON DELETE CASCADE,
    kind          text NOT NULL,
    message_id    text,
    mail_event_id uuid REFERENCES mail_event (id) ON DELETE SET NULL,
    occurred_at   timestamptz,
    confidence    real CHECK (confidence >= 0 AND confidence <= 1),
    undone_at     timestamptz,
    undone_by     text CHECK (undone_by IN ('owner', 'code')),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX storyline_event_storyline ON storyline_event (storyline_id, created_at);
