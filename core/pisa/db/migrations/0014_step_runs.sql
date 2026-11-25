-- The worker's periodic steps: the last period each one ran (`services/step_runs.py`), one row a step,
-- overwritten in place. "Once per period" is the scheduler's (`domain/schedule.py`): the per-service
-- "already ran" app_settings keys move here once, so no step runs twice on the upgrade day.
CREATE TABLE step_runs (
    step text PRIMARY KEY,
    period text NOT NULL,  -- the period's key: the local day it started (ISO), sorting in time
    ran_at timestamptz NOT NULL
);

-- the night's learning: the user's day whose night learned
INSERT INTO step_runs (step, period, ran_at)
SELECT 'learn', value #>> '{}', updated_at FROM app_settings
WHERE key = 'profile_learn_ran' AND jsonb_typeof(value) = 'string';

-- the weekly check: the Sunday it was offered on
INSERT INTO step_runs (step, period, ran_at)
SELECT 'weekly_check', value #>> '{}', updated_at FROM app_settings
WHERE key = 'profile_weekly_sent' AND jsonb_typeof(value) = 'string';

-- the Sunday card: its ISO week ("2026-W46") becomes that week's Sunday
INSERT INTO step_runs (step, period, ran_at)
SELECT 'sunday_card', (to_date(value #>> '{}', 'IYYY-"W"IW') + 6)::text, updated_at FROM app_settings
WHERE key = 'ledger.sunday_sent' AND jsonb_typeof(value) = 'string'
    AND value #>> '{}' ~ '^[0-9]{4}-W[0-9]{2}$';

DELETE FROM app_settings WHERE key IN ('profile_learn_ran', 'profile_weekly_sent', 'ledger.sunday_sent');
