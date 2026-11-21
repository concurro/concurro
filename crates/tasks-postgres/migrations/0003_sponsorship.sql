ALTER TABLE issue_controls
    ADD COLUMN consent_revision bigint NOT NULL DEFAULT 0 CHECK (consent_revision >= 0);

CREATE TABLE sponsorship_facts (
    request_id uuid NOT NULL REFERENCES requests (id),
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    provider text NOT NULL CHECK (provider IN ('openai', 'anthropic')),
    source_at timestamptz NOT NULL,
    source_id bigint NOT NULL,
    source text NOT NULL,
    source_index integer NOT NULL,
    fact jsonb NOT NULL,
    effective boolean NOT NULL DEFAULT true,
    PRIMARY KEY (request_id, provider),
    FOREIGN KEY (repository_id, issue_number)
        REFERENCES issue_controls (repository_id, issue_number)
);
CREATE INDEX sponsorship_facts_issue_order ON sponsorship_facts
    (repository_id, issue_number, source_at, source_id, source COLLATE "C", source_index)
    WHERE effective;

CREATE TABLE sponsorship_removals (
    comment_id bigint NOT NULL,
    source text NOT NULL,
    prior_request_id uuid NOT NULL REFERENCES requests (id),
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    source_at timestamptz NOT NULL,
    source_id bigint NOT NULL,
    provider text NOT NULL CHECK (provider IN ('openai', 'anthropic')),
    fact jsonb NOT NULL,
    PRIMARY KEY (comment_id, source, prior_request_id, provider),
    FOREIGN KEY (repository_id, issue_number)
        REFERENCES issue_controls (repository_id, issue_number)
);
CREATE INDEX sponsorship_removals_issue_order ON sponsorship_removals
    (repository_id, issue_number, source_at, source_id, source COLLATE "C");

CREATE TABLE sponsorship_initializations (
    task_id uuid PRIMARY KEY REFERENCES task_state (task_id),
    request_id uuid NOT NULL REFERENCES requests (id),
    consent_revision bigint NOT NULL CHECK (consent_revision >= 0),
    unresolved jsonb NOT NULL DEFAULT '[]'::jsonb,
    boundary jsonb NOT NULL,
    initial jsonb NOT NULL,
    availability jsonb NOT NULL,
    config_digest text NOT NULL CHECK (config_digest ~ '^[0-9a-f]{64}$'),
    default_funding text NOT NULL CHECK (default_funding IN ('org', 'contributed')),
    source_funding text NOT NULL CHECK (source_funding IN ('org', 'contributed', 'unknown')),
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sponsorship_transitions (
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    transition text PRIMARY KEY CHECK (transition ~ '^[0-9a-f]{64}$'),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    provider text NOT NULL CHECK (provider IN ('openai', 'anthropic')),
    expected_epoch bigint NOT NULL CHECK (expected_epoch >= -1),
    epoch bigint NOT NULL CHECK (epoch >= 0),
    body bytea NOT NULL,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded', 'sending', 'acknowledged', 'failed')),
    claim_token uuid,
    failure text,
    response jsonb,
    acknowledged_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sponsorship_transitions_task ON sponsorship_transitions
    (task_id, provider, sequence);

CREATE TABLE funding_policy_heads (
    repository_id bigint PRIMARY KEY
);

CREATE TABLE funding_policy_activations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id bigint NOT NULL REFERENCES funding_policy_heads (repository_id),
    config_sha text NOT NULL CHECK (config_sha ~ '^[0-9a-f]{40}$'),
    config_digest text NOT NULL CHECK (config_digest ~ '^[0-9a-f]{64}$'),
    funding_mode text NOT NULL CHECK (funding_mode IN ('org', 'contributed')),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX funding_policy_active ON funding_policy_activations (repository_id)
    WHERE ended_at IS NULL;
CREATE INDEX funding_policy_history ON funding_policy_activations
    (repository_id, started_at DESC);
