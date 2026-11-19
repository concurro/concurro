CREATE TABLE recovery_scan (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    unresolved_guids text[] NOT NULL,
    pending_guids text[] NOT NULL,
    redelivered_ids bigint[] NOT NULL
);
