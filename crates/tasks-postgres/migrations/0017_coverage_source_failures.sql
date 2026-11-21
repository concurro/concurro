ALTER TABLE tasks.coverage_gaps ADD COLUMN retry_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE tasks.coverage_source_failures (
    gap_id uuid NOT NULL REFERENCES tasks.coverage_gaps (event_id),
    repository_id bigint NOT NULL,
    source_kind text NOT NULL,
    source_id text NOT NULL,
    reason text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    PRIMARY KEY (gap_id, repository_id, source_kind, source_id)
);
