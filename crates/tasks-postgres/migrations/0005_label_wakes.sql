ALTER TABLE tasks.issue_controls ADD COLUMN applied_label jsonb;

CREATE TABLE tasks.label_wakes (
    delivery_id text PRIMARY KEY,
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    action text NOT NULL CHECK (action IN ('labeled', 'unlabeled')),
    actor_id bigint NOT NULL,
    updated_at timestamptz NOT NULL,
    scan_attempts integer NOT NULL DEFAULT 0,
    retry_at timestamptz NOT NULL DEFAULT now(),
    observed_at timestamptz,
    exhausted_at timestamptz,
    last_error text
);

CREATE INDEX label_wakes_pending ON tasks.label_wakes (retry_at, repository_id, issue_number)
    WHERE observed_at IS NULL AND exhausted_at IS NULL;
