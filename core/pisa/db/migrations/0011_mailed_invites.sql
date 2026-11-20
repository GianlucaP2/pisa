-- The iCalendar UIDs of the invitations that reached them by email. An event whose UID is here arrived by
-- email: its title is the sender's, whatever its other fields say (`services/foreign.py` OWN_EVENT). Kept
-- apart from the mail, which they may delete, so deleting an invitation never makes its event their own.
CREATE TABLE mailed_invites (
    uid     text PRIMARY KEY,
    seen_at timestamptz NOT NULL DEFAULT now()
);
