CREATE TABLE tasks.browser_standalone_actions (
    action_id uuid PRIMARY KEY,
    actor_id bigint NOT NULL,
    content jsonb NOT NULL,
    command_id uuid NOT NULL UNIQUE REFERENCES tasks.standalone_review_commands (id)
);
