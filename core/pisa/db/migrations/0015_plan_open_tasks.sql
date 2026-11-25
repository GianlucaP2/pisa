-- The open tasks a plan was made from, each with its last change ({task id: updated_at}): a plan of today
-- read after one of them was added, closed, deleted or changed is made again first (`planner.current`).
-- NULL on the plans stored before this: taken as current.
ALTER TABLE plans ADD COLUMN open_tasks jsonb;
