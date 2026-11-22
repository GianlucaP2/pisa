-- An event whose invitation reached them by email, after that mail was erased: its UID left
-- `mailed_invites` with the mail, and this flag keeps the event foreign (`services/foreign.py` OWN_EVENT).
-- Set only by the erase of the mail family; the calendar sync never writes it.
ALTER TABLE calendar_events ADD COLUMN mailed boolean NOT NULL DEFAULT false;
