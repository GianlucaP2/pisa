-- v0.1.4, contract 2.3's `pisa.notify`: the phone schedules Pisa's alerts itself (local notifications) and
-- watches its places. Per device: the one revision counter the three frames share, the last ack, and what
-- each item was last sent with (its id, kind, moment and signature; never its title).
CREATE TABLE device_notify (
    device_id      uuid PRIMARY KEY REFERENCES devices (id) ON DELETE CASCADE,
    revision       bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    regions_sig    text CHECK (char_length(regions_sig) <= 64),
    sent_at        timestamptz,
    acked_revision bigint,
    acked_at       timestamptz,
    denied_at      timestamptz  -- the phone said its notifications are not allowed (until an ack schedules)
);

CREATE TABLE device_notify_items (
    device_id  uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    id         text NOT NULL CHECK (id ~ '^[A-Za-z0-9_-]{1,64}$'),
    kind       text NOT NULL CHECK (kind IN ('reminder', 'task', 'calendar', 'leave', 'region')),
    fire_at    timestamptz,
    sig        text NOT NULL CHECK (char_length(sig) <= 64),
    revision   bigint NOT NULL,
    state      text NOT NULL CHECK (state IN ('sent', 'scheduled', 'failed')),
    reason     text CHECK (reason IN ('denied', 'limit', 'past', 'invalid', 'unsupported', 'error')),
    acked_at   timestamptz,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (device_id, id)
);
CREATE INDEX device_notify_items_id ON device_notify_items (id) WHERE state = 'scheduled';

-- A notice for an alert the phone may hold itself carries the item's id and moment: the dispatcher skips the
-- push to a phone that acked it (`delivered_via` phone), and pushes as before otherwise.
ALTER TABLE notifications ADD COLUMN local_id text CHECK (local_id ~ '^[A-Za-z0-9_-]{1,64}$');
ALTER TABLE notifications ADD COLUMN local_at timestamptz;
ALTER TABLE notifications DROP CONSTRAINT notifications_delivered_via_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_delivered_via_check
    CHECK (delivered_via IN ('ws', 'apns', 'phone', 'held', 'none')) NOT VALID;
ALTER TABLE notifications VALIDATE CONSTRAINT notifications_delivered_via_check;

-- Any change to their reminders, place notes or plans, from any process, tells the API process, which syncs
-- the connected phones' schedules (tasks and the calendar already notify `pisa_today`, 0021). The payload
-- names the table only.
CREATE FUNCTION pisa_notify_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('pisa_notify', TG_TABLE_NAME);
    RETURN NULL;
END
$$;
CREATE TRIGGER pisa_promises_notify_changed AFTER INSERT OR UPDATE OR DELETE ON pisa_promises
    FOR EACH STATEMENT EXECUTE FUNCTION pisa_notify_changed();
CREATE TRIGGER place_notes_notify_changed AFTER INSERT OR UPDATE OR DELETE ON place_notes
    FOR EACH STATEMENT EXECUTE FUNCTION pisa_notify_changed();
CREATE TRIGGER plans_notify_changed AFTER INSERT OR UPDATE OR DELETE ON plans
    FOR EACH STATEMENT EXECUTE FUNCTION pisa_notify_changed();
