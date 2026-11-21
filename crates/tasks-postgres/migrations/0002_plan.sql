ALTER TABLE attempts ADD COLUMN retained_scope text;
ALTER TABLE attempts ADD COLUMN retained_turn bigint CHECK (retained_turn >= 0);
ALTER TABLE attempts ADD COLUMN retained_request text;
CREATE UNIQUE INDEX attempts_retained_identity ON attempts (task_id,retained_scope,retained_turn)
    WHERE retained_scope IS NOT NULL;
ALTER TABLE initialization_inputs ADD COLUMN spent_revisions bigint NOT NULL DEFAULT 0 CHECK (spent_revisions >= 0);
ALTER TABLE initialization_inputs ADD COLUMN extra_plan_revisions bigint NOT NULL DEFAULT 0 CHECK (extra_plan_revisions >= 0);
ALTER TABLE initialization_inputs ADD COLUMN spent_design_reopens bigint NOT NULL DEFAULT 0 CHECK (spent_design_reopens >= 0);
ALTER TABLE initialization_inputs ADD COLUMN planner_session_epoch integer NOT NULL DEFAULT 0 CHECK (planner_session_epoch >= 0);
ALTER TABLE initialization_inputs ADD COLUMN planner_failure_count integer NOT NULL DEFAULT 0 CHECK (planner_failure_count >= 0);
ALTER TABLE initialization_inputs ADD COLUMN implementation_session_epoch integer NOT NULL DEFAULT 0 CHECK (implementation_session_epoch >= 0);
ALTER TABLE initialization_inputs ADD COLUMN implementation_failure_count integer NOT NULL DEFAULT 0 CHECK (implementation_failure_count >= 0);

CREATE TABLE task_invalidations (
    request_id uuid PRIMARY KEY REFERENCES requests (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    actor jsonb NOT NULL,
    prior_base_sha text,
    prior_bundle_id text,
    prior_plan_id text,
    prior_head text,
    next_base_sha text NOT NULL,
    next_bundle_id text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE task_resumes (
    request_id uuid PRIMARY KEY REFERENCES requests (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    actor jsonb NOT NULL,
    description_only boolean NOT NULL,
    prior_snapshot jsonb NOT NULL,
    adopted_snapshot jsonb NOT NULL,
    prior_stage text NOT NULL,
    next_stage text NOT NULL,
    approval_binding text,
    kind text NOT NULL DEFAULT 'resume'
        CHECK (kind IN ('resume','implementer_skill_retry')),
    old_skill_sha text,
    new_skill_sha text,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE report_artifact_writes (
    report_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    name text NOT NULL,
    body text NOT NULL,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded','sent','confirmed','failed')),
    artifact_number bigint,
    error text,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id,name)
);

CREATE TABLE plan_reports (
    attempt_id uuid PRIMARY KEY REFERENCES attempts (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    initialization_revision bigint NOT NULL CHECK (initialization_revision > 0),
    raw_report jsonb NOT NULL,
    raw_report_bytes bytea NOT NULL,
    accepted boolean NOT NULL,
    current boolean NOT NULL DEFAULT true,
    validation_error text,
    plan_id text,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((accepted AND validation_error IS NULL AND plan_id IS NOT NULL)
        OR (NOT accepted AND validation_error IS NOT NULL AND plan_id IS NULL))
);
CREATE INDEX plan_reports_task ON plan_reports (task_id, recorded_at DESC);

CREATE TABLE plan_review_batches (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    plan_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 0),
    phase text NOT NULL CHECK (phase IN ('cooperative','fresh')),
    binding text NOT NULL,
    roster jsonb NOT NULL,
    snapshot jsonb,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded','admitted','settled','superseded')),
    UNIQUE (task_id,generation,phase)
);

CREATE TABLE plan_review_sessions (
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    reviewer_id text NOT NULL,
    session_epoch integer NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    PRIMARY KEY (task_id,reviewer_id)
);

CREATE TABLE plan_review_seats (
    batch_id uuid NOT NULL REFERENCES plan_review_batches (id),
    reviewer_id text NOT NULL,
    identity jsonb NOT NULL,
    attempt_id uuid UNIQUE REFERENCES attempts (id),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','admitted','completed','accepted','correcting','retrying','failed')),
    first_pass_eligible boolean NOT NULL DEFAULT false,
    report jsonb,
    report_raw bytea,
    validation_error text,
    correction_count integer NOT NULL DEFAULT 0 CHECK (correction_count >= 0),
    failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    session_epoch integer NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    repair_signature text,
    PRIMARY KEY (batch_id,reviewer_id)
);

CREATE TABLE plan_objections (
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    id text NOT NULL,
    reviewer_id text NOT NULL,
    detail text NOT NULL,
    counterexample text NOT NULL,
    closure text NOT NULL,
    evidence text NOT NULL,
    resolved boolean NOT NULL,
    plan_id text NOT NULL,
    phase text NOT NULL,
    PRIMARY KEY (task_id,id),
    UNIQUE (task_id,sequence)
);
CREATE INDEX plan_objections_reviewer ON plan_objections (task_id,reviewer_id,sequence);

CREATE TABLE plan_approvals (
    request_id uuid PRIMARY KEY REFERENCES requests (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    plan_id text NOT NULL,
    binding text NOT NULL,
    actor jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE implementation_reports (
    attempt_id uuid PRIMARY KEY REFERENCES attempts (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    raw_result bytea NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected','superseded','reopened')),
    validation_error text,
    candidate jsonb,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status='pending' AND validation_error IS NULL AND candidate IS NULL)
        OR (status='accepted' AND validation_error IS NULL AND candidate IS NOT NULL)
        OR (status='reopened' AND validation_error IS NULL AND candidate IS NOT NULL)
        OR (status IN ('rejected','superseded') AND validation_error IS NOT NULL AND candidate IS NULL))
);

CREATE TABLE implementation_reopens (
    turn_id uuid PRIMARY KEY REFERENCES attempts (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    plan_id text NOT NULL,
    binding text NOT NULL,
    prior_snapshot jsonb NOT NULL,
    prior_head text,
    design_issue jsonb NOT NULL,
    findings_snapshot jsonb NOT NULL,
    recovered_request uuid REFERENCES requests (id),
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE git_publications (
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    attempt_id uuid NOT NULL UNIQUE REFERENCES attempts (id),
    revision bigint NOT NULL DEFAULT 0,
    intent jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id,attempt_id)
);

CREATE TABLE pr_review_batches (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    publication_attempt uuid NOT NULL REFERENCES attempts (id),
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    head text NOT NULL CHECK (head ~ '^[0-9a-f]{40}$'),
    branch text NOT NULL,
    plan_id text NOT NULL,
    round bigint NOT NULL CHECK (round >= 0),
    fix_round bigint NOT NULL DEFAULT 0 CHECK (fix_round >= 0),
    changed_paths jsonb NOT NULL,
    roster jsonb NOT NULL,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded','admitted','settled','superseded')),
    UNIQUE (task_id,round)
);

CREATE TABLE pr_review_sessions (
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    reviewer_id text NOT NULL,
    session_epoch integer NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    PRIMARY KEY (task_id,reviewer_id)
);

CREATE TABLE pr_review_seats (
    batch_id uuid NOT NULL REFERENCES pr_review_batches (id),
    reviewer_id text NOT NULL,
    identity jsonb NOT NULL,
    binding text NOT NULL,
    attempt_id uuid UNIQUE REFERENCES attempts (id),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','admitted','completed','accepted','correcting','retrying','failed')),
    report jsonb,
    report_raw bytea,
    validation_error text,
    correction_count integer NOT NULL DEFAULT 0 CHECK (correction_count >= 0),
    repair_signature text,
    failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    session_epoch integer NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    PRIMARY KEY (batch_id,reviewer_id)
);

CREATE TABLE pr_review_findings (
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    id text NOT NULL,
    reviewer_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('open','fixed','withdrawn','obsolete')),
    head text NOT NULL,
    finding jsonb NOT NULL,
    disposition jsonb,
    thread_node_id text,
    PRIMARY KEY (task_id,sequence),
    UNIQUE (task_id,id)
);

CREATE INDEX pr_review_findings_owner ON pr_review_findings (task_id,reviewer_id,status,sequence);

CREATE TABLE pr_review_publications (
    turn_id uuid PRIMARY KEY REFERENCES attempts (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    batch_id uuid NOT NULL REFERENCES pr_review_batches (id),
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    head text NOT NULL CHECK (head ~ '^[0-9a-f]{40}$'),
    marker text NOT NULL,
    body text NOT NULL,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded','sent','confirmed','failed')),
    bot_user_id bigint,
    review_id bigint,
    review_node_id text,
    error text,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pr_review_actions (
    turn_id uuid NOT NULL REFERENCES pr_review_publications (turn_id),
    sequence integer NOT NULL CHECK (sequence >= 0),
    kind text NOT NULL CHECK (kind IN ('create','thread','reply','submit','resolve')),
    finding_id text,
    path text,
    start_line bigint,
    line bigint,
    placement text CHECK (placement IN ('inline','file','body')),
    marker text NOT NULL,
    body text NOT NULL,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded','sent','confirmed','failed')),
    result_node_id text,
    error text,
    PRIMARY KEY (turn_id,sequence)
);

CREATE TABLE pr_ready_intents (
    task_id uuid PRIMARY KEY REFERENCES task_state (task_id),
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    head text NOT NULL CHECK (head ~ '^[0-9a-f]{40}$'),
    base_sha text NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    merge_sha text,
    status text NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded','sent','confirmed','failed')),
    error text,
    ready_seen boolean NOT NULL DEFAULT false,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pr_terminal_observations (
    task_id uuid PRIMARY KEY REFERENCES task_state (task_id),
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    published_head text NOT NULL CHECK (published_head ~ '^[0-9a-f]{40}$'),
    observed_head text NOT NULL,
    merged boolean NOT NULL,
    merge_commit_sha text,
    gates_verified boolean NOT NULL DEFAULT false,
    pr jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
