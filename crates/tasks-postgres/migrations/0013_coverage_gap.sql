CREATE TABLE tasks.coverage_gaps (
    event_id uuid PRIMARY KEY,
    from_at timestamptz NOT NULL,
    to_at timestamptz NOT NULL,
    installation_id bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    recovered_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CHECK (from_at < to_at)
);
CREATE INDEX coverage_gaps_pending ON tasks.coverage_gaps (recorded_at)
    WHERE recovered_at IS NULL;
