-- Display-only snapshots; deliberately unrelated to execution ownership or task state.
CREATE TABLE tasks.historical_display (
    deployment text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('contribution','review')),
    repository text NOT NULL,
    number bigint NOT NULL CHECK (number > 0),
    observed_at timestamptz NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (deployment,kind,repository,number)
);

-- Failed scans keep serving the previous list with an explicit freshness warning.
CREATE TABLE tasks.target_index_status (
    repository text NOT NULL,
    kind text NOT NULL,
    verified_at timestamptz,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    failed boolean NOT NULL,
    PRIMARY KEY (repository,kind)
);
