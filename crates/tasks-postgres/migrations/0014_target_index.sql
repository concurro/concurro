CREATE TABLE tasks.target_index (
    repository_id bigint NOT NULL,
    kind text NOT NULL CHECK (kind IN ('issue', 'pull', 'run')),
    item_id text NOT NULL,
    source_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    scan_id uuid,
    body jsonb NOT NULL,
    PRIMARY KEY (repository_id, kind, item_id)
);
CREATE INDEX target_index_kind ON tasks.target_index (repository_id, kind, source_at DESC);
