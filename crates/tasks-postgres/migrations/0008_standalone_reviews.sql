CREATE TABLE tasks.standalone_review_wakes (
    delivery_id text PRIMARY KEY,
    repository_id bigint NOT NULL,
    upstream text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    action text NOT NULL CHECK (action IN ('review_requested', 'review_request_removed')),
    actor_id bigint NOT NULL,
    reviewer_id bigint NOT NULL,
    updated_at timestamptz NOT NULL,
    scan_attempts integer NOT NULL DEFAULT 0,
    retry_at timestamptz NOT NULL DEFAULT now(),
    observed_at timestamptz,
    exhausted_at timestamptz,
    last_error text
);
CREATE INDEX standalone_review_wakes_pending ON tasks.standalone_review_wakes (retry_at)
    WHERE observed_at IS NULL AND exhausted_at IS NULL;

CREATE TABLE tasks.standalone_review_requests (
    id uuid PRIMARY KEY,
    repository_id bigint NOT NULL,
    upstream text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    source_id bigint NOT NULL,
    source_at timestamptz NOT NULL,
    actor jsonb NOT NULL,
    action text NOT NULL CHECK (action IN ('review_requested', 'review_request_removed')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'refused', 'superseded')),
    reason text,
    round integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    UNIQUE (upstream, pr_number, source_id)
);

CREATE TABLE tasks.standalone_review_commands (
    id uuid PRIMARY KEY,
    repository_id bigint NOT NULL,
    upstream text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    comment_id bigint NOT NULL,
    source text NOT NULL,
    source_at timestamptz NOT NULL,
    actor jsonb NOT NULL,
    comment_author_id bigint NOT NULL,
    kind jsonb,
    body text NOT NULL,
    deleted boolean NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','applied','noop','refused')),
    reason text,
    decided_at timestamptz,
    UNIQUE (upstream,pr_number,source)
);
CREATE INDEX standalone_review_commands_pending ON tasks.standalone_review_commands
    (upstream,pr_number,source_at,comment_id) WHERE status='pending';

CREATE TABLE tasks.standalone_review_funding_facts (
    command_id uuid NOT NULL REFERENCES tasks.standalone_review_commands (id),
    repository_id bigint NOT NULL,
    pr_number bigint NOT NULL,
    comment_id bigint NOT NULL,
    provider text NOT NULL CHECK (provider IN ('openai','anthropic')),
    fact jsonb NOT NULL,
    effective boolean NOT NULL DEFAULT true,
    PRIMARY KEY (command_id,provider)
);
CREATE INDEX standalone_review_funding_source ON tasks.standalone_review_funding_facts
    (repository_id,pr_number,provider) WHERE effective;

CREATE TABLE tasks.standalone_review_funding_removals (
    command_id uuid NOT NULL REFERENCES tasks.standalone_review_commands (id),
    prior_command_id uuid NOT NULL REFERENCES tasks.standalone_review_commands (id),
    provider text NOT NULL CHECK (provider IN ('openai','anthropic')),
    fact jsonb NOT NULL,
    PRIMARY KEY (command_id,prior_command_id,provider)
);

ALTER TABLE tasks.publication_comments ADD COLUMN standalone_command_id uuid
    UNIQUE REFERENCES tasks.standalone_review_commands (id);
CREATE INDEX standalone_review_requests_pending ON tasks.standalone_review_requests
    (upstream, pr_number, source_at, source_id) WHERE status='pending';

CREATE TABLE tasks.standalone_review_controls (
    upstream text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    last_source_at timestamptz,
    last_source_id bigint,
    PRIMARY KEY (upstream, pr_number),
    CHECK ((last_source_at IS NULL) = (last_source_id IS NULL))
);

CREATE TABLE tasks.standalone_review_tasks (
    id uuid PRIMARY KEY,
    repository_id bigint NOT NULL,
    runner_repository_id bigint NOT NULL,
    upstream text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    reviewer_id bigint NOT NULL,
    stage text NOT NULL CHECK (stage IN
        ('queued', 'review', 'publishing', 'complete', 'closed', 'cancelled',
         'blocked', 'stopped', 'awaiting_credentials', 'budget_exhausted')),
    round integer NOT NULL DEFAULT 0 CHECK (round >= 0),
    used_model_calls bigint NOT NULL DEFAULT 0 CHECK (used_model_calls >= 0),
    closed_from_stage text,
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (upstream, pr_number)
);

ALTER TABLE tasks.report_artifact_writes ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE tasks.report_artifact_writes ADD COLUMN standalone_task_id uuid
    REFERENCES tasks.standalone_review_tasks (id);
ALTER TABLE tasks.report_artifact_writes ADD CONSTRAINT report_artifact_one_owner
    CHECK (num_nonnulls(task_id, standalone_task_id) = 1);

ALTER TABLE tasks.attempts ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE tasks.attempts ADD COLUMN standalone_task_id uuid
    REFERENCES tasks.standalone_review_tasks (id);
ALTER TABLE tasks.attempts ADD CONSTRAINT attempts_exactly_one_owner
    CHECK (num_nonnulls(task_id, standalone_task_id) = 1);
CREATE INDEX attempts_open_standalone ON tasks.attempts (standalone_task_id)
    WHERE standalone_task_id IS NOT NULL
    AND status IN ('ready', 'sending', 'queued', 'running', 'unknown');

CREATE TABLE tasks.standalone_review_rounds (
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    number integer NOT NULL CHECK (number > 0),
    request_id uuid NOT NULL UNIQUE REFERENCES tasks.standalone_review_requests (id),
    snapshot jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued', 'review', 'publishing', 'complete', 'cancelled', 'closed',
         'blocked', 'stopped', 'awaiting_credentials', 'budget_exhausted')),
    cancellation_request_id uuid REFERENCES tasks.standalone_review_requests (id),
    cancelled_at timestamptz,
    cancelled_by jsonb,
    opened_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (task_id, number)
);

CREATE TABLE tasks.standalone_review_adoptions (
    command_id uuid PRIMARY KEY REFERENCES tasks.standalone_review_commands (id),
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    round integer NOT NULL,
    previous_snapshot jsonb NOT NULL,
    adopted_snapshot jsonb NOT NULL,
    adopted_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id,round) REFERENCES tasks.standalone_review_rounds (task_id,number)
);

CREATE TABLE tasks.standalone_review_attempts (
    attempt_id uuid PRIMARY KEY REFERENCES tasks.attempts (id),
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    round integer NOT NULL,
    reviewer_id text NOT NULL,
    attempt_number integer NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
    status text NOT NULL DEFAULT 'admitted'
        CHECK (status IN ('admitted', 'completed', 'correcting', 'retrying', 'validated', 'published', 'failed')),
    report jsonb,
    report_raw bytea,
    validated_report jsonb,
    failure_reason text,
    repair_signature text,
    FOREIGN KEY (task_id, round) REFERENCES tasks.standalone_review_rounds (task_id, number),
    UNIQUE (task_id, round, reviewer_id, attempt_number)
);

CREATE TABLE tasks.standalone_review_funding_grants (
    attempt_id uuid PRIMARY KEY REFERENCES tasks.attempts (id),
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    purpose text NOT NULL CHECK (purpose IN ('org', 'contributed')),
    status text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'revoked')),
    payer_id bigint,
    provider text CHECK (provider IN ('openai', 'anthropic')),
    slot_epoch bigint,
    slot_grant_id text,
    credential_record_id text,
    credential_version bigint,
    route text,
    issued_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((purpose='org' AND payer_id IS NULL AND provider IS NULL
            AND slot_epoch IS NULL AND slot_grant_id IS NULL
            AND credential_record_id IS NULL AND credential_version IS NULL
            AND route IS NULL)
        OR (purpose='contributed' AND payer_id > 0 AND provider IS NOT NULL
            AND slot_epoch >= 0 AND slot_grant_id IS NOT NULL
            AND credential_record_id IS NOT NULL AND credential_version > 0
            AND route IS NOT NULL))
);

ALTER TABLE tasks.funding_exclusions ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE tasks.funding_exclusions ADD COLUMN standalone_task_id uuid
    REFERENCES tasks.standalone_review_tasks (id);
ALTER TABLE tasks.funding_exclusions ADD CONSTRAINT funding_exclusion_one_owner
    CHECK (num_nonnulls(task_id, standalone_task_id) = 1);
CREATE INDEX funding_exclusions_standalone_current ON tasks.funding_exclusions
    (standalone_task_id, payer_id, provider, credential_record_id, credential_version, model)
    WHERE standalone_task_id IS NOT NULL;

CREATE TABLE tasks.standalone_review_sponsorship_initializations (
    task_id uuid PRIMARY KEY REFERENCES tasks.standalone_review_tasks (id),
    request_id uuid NOT NULL REFERENCES tasks.standalone_review_requests (id),
    boundary jsonb NOT NULL,
    initial jsonb NOT NULL,
    availability jsonb NOT NULL,
    config_digest text NOT NULL CHECK (config_digest ~ '^[0-9a-f]{64}$'),
    default_funding text NOT NULL CHECK (default_funding IN ('org','contributed')),
    source_funding text NOT NULL CHECK (source_funding IN ('org','contributed','unknown')),
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tasks.standalone_review_applied_funding_facts (
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    provider text NOT NULL CHECK (provider IN ('openai','anthropic')),
    grant_id text NOT NULL,
    PRIMARY KEY (task_id,provider,grant_id)
);

ALTER TABLE tasks.sponsorship_transitions ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE tasks.sponsorship_transitions ADD COLUMN standalone_task_id uuid
    REFERENCES tasks.standalone_review_tasks (id);
ALTER TABLE tasks.sponsorship_transitions ADD CONSTRAINT sponsorship_transition_one_owner
    CHECK (num_nonnulls(task_id,standalone_task_id)=1);

CREATE TABLE tasks.standalone_review_findings (
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    id text NOT NULL,
    reviewer_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('open', 'fixed', 'withdrawn', 'obsolete')),
    head text NOT NULL CHECK (head ~ '^[0-9a-f]{40}$'),
    finding jsonb NOT NULL,
    disposition jsonb,
    thread_node_id text,
    root_comment_id bigint,
    PRIMARY KEY (task_id, sequence),
    UNIQUE (task_id, id)
);

CREATE TABLE tasks.standalone_review_dispositions (
    turn_id uuid NOT NULL REFERENCES tasks.attempts (id),
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    finding_id text NOT NULL,
    marker text NOT NULL,
    body text NOT NULL,
    reply_status text NOT NULL DEFAULT 'recorded'
        CHECK (reply_status IN ('recorded','sent','confirmed','failed')),
    reply_id bigint,
    resolve_status text NOT NULL DEFAULT 'none'
        CHECK (resolve_status IN ('none','recorded','intended','confirmed','manual','failed')),
    error text,
    PRIMARY KEY (turn_id, finding_id),
    FOREIGN KEY (task_id, finding_id)
        REFERENCES tasks.standalone_review_findings (task_id, id)
);
CREATE INDEX standalone_review_findings_owner ON tasks.standalone_review_findings
    (task_id, reviewer_id, status, sequence);

CREATE TABLE tasks.standalone_review_publications (
    turn_id uuid PRIMARY KEY REFERENCES tasks.attempts (id),
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    round integer NOT NULL,
    reviewer_id text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    head text NOT NULL CHECK (head ~ '^[0-9a-f]{40}$'),
    marker text NOT NULL,
    body text NOT NULL,
    comments jsonb NOT NULL DEFAULT '[]'::jsonb,
    unplaced_comments jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded', 'sent', 'confirmed', 'failed')),
    reviewer_user_id bigint NOT NULL,
    review_id bigint,
    error text,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id, round) REFERENCES tasks.standalone_review_rounds (task_id, number)
);

CREATE TABLE tasks.standalone_review_checks (
    task_id uuid NOT NULL REFERENCES tasks.standalone_review_tasks (id),
    round integer NOT NULL,
    attempt integer NOT NULL CHECK (attempt > 0),
    head text NOT NULL CHECK (head ~ '^[0-9a-f]{40}$'),
    external_id text NOT NULL,
    run_id bigint,
    create_state text NOT NULL DEFAULT 'recorded'
        CHECK (create_state IN ('recorded','sent','confirmed')),
    create_misses integer NOT NULL DEFAULT 0,
    recheck_until timestamptz,
    sent_digest text,
    completed boolean NOT NULL DEFAULT false,
    error text,
    PRIMARY KEY (task_id, round, attempt),
    FOREIGN KEY (task_id, round)
        REFERENCES tasks.standalone_review_rounds (task_id, number)
);
