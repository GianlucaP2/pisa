-- App Store review mode (PISA_REVIEW_MODE=1, docs-site/app-review.md): a device paired with a review code, or
-- offered by a device that was, is a review device. The mark is the device's own, so it outlives the review
-- code (pair_tokens are pruned a week after they expire, and their pairings with them). A review device offers
-- no pairing, vouches for no key and registers no phone key; `pisa pair --review --revoke` revokes every one
-- and deletes their phone keys; a server started without the flag warns while any is left.
ALTER TABLE devices ADD COLUMN review boolean NOT NULL DEFAULT false;

-- the review devices paired before this column, and the devices they offered, down the whole chain
WITH RECURSIVE tree(id) AS (
    SELECT p.device_id FROM pairings p JOIN pair_tokens t ON t.id = p.token_id
    WHERE t.review AND p.state = 'confirmed' AND p.device_id IS NOT NULL
    UNION
    SELECT p.device_id FROM pairings p JOIN pair_tokens t ON t.id = p.token_id JOIN tree ON t.offered_by = tree.id
    WHERE p.state = 'confirmed' AND p.device_id IS NOT NULL
)
UPDATE devices SET review = true WHERE id IN (SELECT id FROM tree);
