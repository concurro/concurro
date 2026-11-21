ALTER TABLE tasks.plan_review_seats
    ADD COLUMN session_failure_count integer NOT NULL DEFAULT 0
    CHECK (session_failure_count >= 0);
UPDATE tasks.plan_review_seats
    SET session_failure_count=failure_count WHERE failure_count > 0;

ALTER TABLE tasks.pr_review_seats
    ADD COLUMN session_failure_count integer NOT NULL DEFAULT 0
    CHECK (session_failure_count >= 0);
UPDATE tasks.pr_review_seats
    SET session_failure_count=failure_count WHERE failure_count > 0;

ALTER TABLE tasks.funding_exclusions
    ADD COLUMN cleared_at timestamptz,
    ADD COLUMN retry_source text CHECK (retry_source IN ('provider', 'local'));
