CREATE TABLE tasks.config_targets (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL CHECK (revision > 0),
    body jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
