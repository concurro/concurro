ALTER TABLE tasks.requests
    ADD COLUMN task_id uuid,
    ADD COLUMN applied_at timestamptz;

CREATE TABLE tasks.browser_actions (
    action_id uuid PRIMARY KEY,
    actor_id bigint NOT NULL,
    content jsonb NOT NULL,
    request_id uuid NOT NULL UNIQUE REFERENCES tasks.requests (id)
);
