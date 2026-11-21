CREATE TABLE funding_grants (
    attempt_id uuid PRIMARY KEY REFERENCES attempts (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    purpose text NOT NULL CHECK (purpose IN ('task_org', 'task_contributed')),
    status text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'revoked')),
    reservation_status text NOT NULL DEFAULT 'charged'
        CHECK (reservation_status IN ('charged', 'released')),
    payer_id bigint,
    provider text CHECK (provider IN ('openai', 'anthropic')),
    slot_epoch bigint,
    slot_grant_id text,
    credential_record_id text,
    credential_version bigint,
    route text,
    models jsonb,
    limits jsonb,
    issued_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    released_at timestamptz,
    CHECK (
        (purpose = 'task_org' AND payer_id IS NULL AND provider IS NULL
            AND slot_epoch IS NULL AND slot_grant_id IS NULL
            AND credential_record_id IS NULL AND credential_version IS NULL
            AND route IS NULL)
        OR
        (purpose = 'task_contributed' AND payer_id > 0 AND provider IS NOT NULL
            AND slot_epoch >= 0 AND slot_grant_id IS NOT NULL
            AND credential_record_id IS NOT NULL AND credential_version > 0
            AND route IS NOT NULL)
    )
);
CREATE INDEX funding_grants_task_payer ON funding_grants
    (task_id, provider, payer_id, slot_epoch) WHERE purpose = 'task_contributed';

INSERT INTO funding_grants (attempt_id, task_id, purpose, status)
SELECT id, task_id, 'task_org',
    CASE WHEN status = 'cancelled' THEN 'revoked' ELSE 'issued' END
FROM attempts;

CREATE TABLE funding_exclusions (
    attempt_id uuid PRIMARY KEY REFERENCES attempts (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    payer_id bigint NOT NULL,
    provider text NOT NULL CHECK (provider IN ('openai', 'anthropic')),
    credential_record_id text NOT NULL,
    credential_version bigint NOT NULL,
    model text,
    category text NOT NULL CHECK (category IN
        ('auth', 'model_denied', 'quota_exhausted', 'rate_limited', 'unavailable')),
    retry_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX funding_exclusions_current ON funding_exclusions
    (task_id, payer_id, provider, credential_record_id, credential_version, model);
