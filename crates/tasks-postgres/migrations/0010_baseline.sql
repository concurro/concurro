CREATE TABLE tasks.baseline_snapshots (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tasks.baseline_rows (
    snapshot_id uuid NOT NULL REFERENCES tasks.baseline_snapshots (id) ON DELETE CASCADE,
    ordinal bigint NOT NULL CHECK (ordinal > 0),
    entity text NOT NULL,
    entity_id text NOT NULL,
    revision bigint NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, ordinal)
);
