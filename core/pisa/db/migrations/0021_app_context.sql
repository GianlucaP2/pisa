-- Contract 2.2. Pisa changes one of its own server settings (the briefing's rules, the reminders' lead times)
-- only on the user's yes, as an approval of its own kind, with Undo for a day.
ALTER TABLE approvals DROP CONSTRAINT approvals_kind_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_kind_check CHECK (kind IN ('calendar_create', 'calendar_move',
    'calendar_cancel', 'email_send', 'setting_change')) NOT VALID;
ALTER TABLE approvals VALIDATE CONSTRAINT approvals_kind_check;

-- The riepilogo is a view of the current plan: any change to their tasks or their agenda, from any process (the
-- API, the worker's sync, the CLI), tells the API process, which tells the connected apps to read it again
-- (`pisa.app.changed`). The payload names the table only; one notification per statement, and Postgres folds
-- the same one sent twice in a transaction.
CREATE FUNCTION pisa_today_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('pisa_today', TG_TABLE_NAME);
    RETURN NULL;
END
$$;
CREATE TRIGGER tasks_today_changed AFTER INSERT OR UPDATE OR DELETE ON tasks
    FOR EACH STATEMENT EXECUTE FUNCTION pisa_today_changed();
CREATE TRIGGER calendar_events_today_changed AFTER INSERT OR UPDATE OR DELETE ON calendar_events
    FOR EACH STATEMENT EXECUTE FUNCTION pisa_today_changed();
