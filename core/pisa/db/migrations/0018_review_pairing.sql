-- App Store review mode (PISA_REVIEW_MODE=1, docs-site/app-review.md): `pisa pair --review` makes a review
-- code, valid 30 days, that pairs up to `max_uses` devices (at most 3) without the 6-digit confirmation on the
-- server. Honoured only while the server runs in review mode; `pisa pair --review --revoke` ends it (and the
-- devices paired with it). An ordinary code keeps review false, max_uses 1: nothing about it changes.
ALTER TABLE pair_tokens ADD COLUMN review boolean NOT NULL DEFAULT false;
ALTER TABLE pair_tokens ADD COLUMN max_uses smallint NOT NULL DEFAULT 1 CHECK (max_uses BETWEEN 1 AND 3);
ALTER TABLE pair_tokens ADD COLUMN uses smallint NOT NULL DEFAULT 0 CHECK (uses >= 0);
ALTER TABLE pair_tokens ADD CONSTRAINT pair_tokens_review_uses CHECK (review OR max_uses = 1);
