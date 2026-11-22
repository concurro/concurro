ALTER TABLE funding_grants DROP CONSTRAINT funding_grants_check;
ALTER TABLE funding_grants ADD CONSTRAINT funding_grants_source_check CHECK (
    (purpose = 'task_org' AND payer_id IS NULL AND provider IS NULL
        AND slot_epoch IS NULL AND slot_grant_id IS NULL
        AND credential_record_id IS NULL AND credential_version IS NULL)
    OR (purpose = 'task_contributed' AND payer_id > 0 AND provider IS NOT NULL
        AND slot_epoch >= 0 AND slot_grant_id IS NOT NULL
        AND credential_record_id IS NOT NULL AND credential_version > 0 AND route IS NOT NULL)
);

ALTER TABLE standalone_review_funding_grants DROP CONSTRAINT standalone_review_funding_grants_check;
ALTER TABLE standalone_review_funding_grants ADD CONSTRAINT standalone_review_funding_grants_source_check CHECK (
    (purpose='org' AND payer_id IS NULL AND provider IS NULL
        AND slot_epoch IS NULL AND slot_grant_id IS NULL
        AND credential_record_id IS NULL AND credential_version IS NULL)
    OR (purpose='contributed' AND payer_id > 0 AND provider IS NOT NULL
        AND slot_epoch >= 0 AND slot_grant_id IS NOT NULL
        AND credential_record_id IS NOT NULL AND credential_version > 0 AND route IS NOT NULL)
);

CREATE TABLE funding_usage (
    request_id uuid PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    turn_id uuid NOT NULL REFERENCES attempts(id),
    grant_id text NOT NULL,
    model text NOT NULL,
    category text NOT NULL,
    input_tokens bigint,
    output_tokens bigint,
    cache_read_input_tokens bigint,
    cache_creation_input_tokens bigint,
    usage_unavailable text,
    occurred_at timestamptz NOT NULL
);

ALTER TABLE attempts ADD COLUMN rust_observed_at timestamptz;
ALTER TABLE attempts ADD COLUMN rust_exchange_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE attempts ADD COLUMN rust_provider_requests text;
ALTER TABLE attempts ADD COLUMN rust_usage jsonb;
CREATE INDEX attempts_rust_reconcile ON attempts (rust_observed_at)
    WHERE rust_exchange_revision>0 AND (status IN ('queued','running')
        OR (status IN ('finished','unknown','cancelled') AND NOT finished_event_emitted));
