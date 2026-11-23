CREATE TABLE tasks.withdrawal_steps (
    source_id uuid PRIMARY KEY,
    request_id uuid UNIQUE REFERENCES tasks.requests(id),
    standalone_command_id uuid UNIQUE REFERENCES tasks.standalone_review_commands(id),
    request jsonb NOT NULL,
    task_id uuid,
    transitions text[] NOT NULL DEFAULT '{}',
    blocked_at timestamptz NOT NULL DEFAULT now(),
    stopped_at timestamptz,
    CONSTRAINT withdrawal_step_source CHECK ((request_id IS NULL) <> (standalone_command_id IS NULL))
);
CREATE INDEX withdrawal_steps_pending ON tasks.withdrawal_steps (blocked_at, source_id)
    WHERE stopped_at IS NULL;

ALTER TABLE tasks.publication_comments ADD COLUMN standalone_step_command_id uuid
    REFERENCES tasks.standalone_review_commands(id);
