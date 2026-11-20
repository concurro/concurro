CREATE TABLE issue_controls (
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    applied_stop jsonb,
    applied_lifecycle jsonb,
    PRIMARY KEY (repository_id, issue_number)
);

CREATE TABLE requests (
    id uuid PRIMARY KEY,
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    channel text NOT NULL,
    comment_id bigint,
    source text NOT NULL,
    source_at timestamptz NOT NULL,
    source_id bigint NOT NULL,
    source_index integer NOT NULL,
    request_class smallint NOT NULL,
    request jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'refused', 'superseded')),
    reason text,
    UNIQUE (channel, source, source_index),
    FOREIGN KEY (repository_id, issue_number)
        REFERENCES issue_controls (repository_id, issue_number)
);
CREATE INDEX requests_pending_issue ON requests
    (repository_id, issue_number, request_class, source_at, source_id, source COLLATE "C", source_index)
    WHERE status = 'pending';
CREATE INDEX requests_pending_comment ON requests (comment_id) WHERE channel='comment' AND status='pending';

CREATE TABLE comment_revisions (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    comment_id bigint NOT NULL,
    source text NOT NULL,
    source_at timestamptz NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    UNIQUE (comment_id, source),
    FOREIGN KEY (repository_id, issue_number)
        REFERENCES issue_controls (repository_id, issue_number)
);
CREATE INDEX comment_revisions_latest ON comment_revisions (comment_id, source_at DESC, source COLLATE "C");

CREATE TABLE task_state (
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    task_id uuid NOT NULL UNIQUE,
    revision bigint NOT NULL CHECK (revision > 0),
    state jsonb NOT NULL,
    PRIMARY KEY (repository_id, issue_number),
    FOREIGN KEY (repository_id, issue_number)
        REFERENCES issue_controls (repository_id, issue_number)
);

CREATE TABLE start_pins (
    task_id uuid PRIMARY KEY REFERENCES task_state (task_id),
    request_id uuid NOT NULL REFERENCES requests (id),
    task_revision bigint NOT NULL CHECK (task_revision > 0),
    config_sha text NOT NULL CHECK (config_sha ~ '^[0-9a-f]{40}$')
);

CREATE TABLE initialization_inputs (
    task_id uuid PRIMARY KEY REFERENCES task_state (task_id),
    task_revision bigint NOT NULL CHECK (task_revision > 0),
    snapshot jsonb NOT NULL
);

CREATE TABLE attempts (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    operation text NOT NULL,
    expected_revision bigint NOT NULL,
    staffing_group text NOT NULL,
    fork text NOT NULL,
    config_sha text NOT NULL CHECK (config_sha ~ '^[0-9a-f]{40}$'),
    submit_body bytea NOT NULL,
    prompt_body bytea,
    initial_deadline timestamptz NOT NULL,
    deadline timestamptz,
    unbounded_running boolean NOT NULL,
    status text NOT NULL CHECK (status IN ('ready', 'sending', 'queued', 'running', 'finished', 'unknown', 'cancelled')),
    outcome_reason text,
    admitted_at timestamptz NOT NULL DEFAULT now(),
    poll_generation bigint NOT NULL DEFAULT 0,
    poll_failures integer NOT NULL DEFAULT 0,
    status_revision bigint NOT NULL DEFAULT 0,
    finished_event_emitted boolean NOT NULL DEFAULT false,
    observation jsonb,
    observation_raw bytea,
    result jsonb,
    result_raw bytea
);
CREATE INDEX attempts_open_task ON attempts (task_id) WHERE status IN ('ready', 'sending', 'queued', 'running', 'unknown');

CREATE TABLE intake_reports (
    attempt_id uuid PRIMARY KEY REFERENCES attempts (id),
    task_id uuid NOT NULL REFERENCES task_state (task_id),
    initialization_revision bigint NOT NULL,
    raw_report jsonb NOT NULL,
    raw_report_bytes bytea NOT NULL,
    accepted boolean NOT NULL,
    validation_error text,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE staffing_groups (
    staffing_group text PRIMARY KEY,
    turn_id uuid NOT NULL UNIQUE
);

CREATE TABLE effects (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    turn_id uuid NOT NULL REFERENCES attempts (id),
    kind text NOT NULL CHECK (kind IN ('submit', 'retire')),
    status text NOT NULL CHECK (status IN ('recorded', 'sending', 'settled')),
    response jsonb,
    response_raw bytea,
    claim_token uuid,
    submit_attempts integer NOT NULL DEFAULT 0 CHECK (submit_attempts >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (turn_id, kind)
);
CREATE INDEX effects_ready ON effects (id) WHERE status <> 'settled';

CREATE TABLE attempt_sessions (
    turn_id uuid NOT NULL REFERENCES attempts (id),
    session_id uuid NOT NULL,
    retired_at timestamptz,
    PRIMARY KEY (turn_id, session_id)
);

CREATE TABLE turn_artifacts (
    sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    body bytea NOT NULL,
    media_type text NOT NULL
);

CREATE TABLE turn_artifact_links (
    turn_id uuid NOT NULL REFERENCES attempts (id),
    sha256 text NOT NULL REFERENCES turn_artifacts (sha256),
    PRIMARY KEY (turn_id, sha256)
);

CREATE TABLE runner_sessions (
    session_id uuid PRIMARY KEY,
    turn_id uuid NOT NULL REFERENCES attempts (id),
    repository_id bigint NOT NULL,
    fork text NOT NULL,
    config_sha text NOT NULL CHECK (config_sha ~ '^[0-9a-f]{40}$'),
    desired_present boolean NOT NULL DEFAULT true,
    ref_revision bigint NOT NULL DEFAULT 1,
    ref_watch boolean NOT NULL DEFAULT true,
    ref_present_observed boolean NOT NULL DEFAULT false,
    ref_retry_at timestamptz,
    ref_error text,
    dispatch_status text NOT NULL DEFAULT 'recorded'
        CHECK (dispatch_status IN ('recorded','sending','obsolete_uncertain','sent','observed','failed','skipped')),
    dispatch_error text,
    run_id bigint,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX runner_sessions_turn ON runner_sessions (turn_id);
CREATE INDEX runner_sessions_watch ON runner_sessions (updated_at,session_id)
    WHERE ref_watch OR dispatch_status IN ('recorded','sending','obsolete_uncertain','sent');

CREATE TABLE runner_ref_writes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES runner_sessions (session_id),
    revision bigint NOT NULL,
    action text NOT NULL CHECK (action IN ('create','delete')),
    status text NOT NULL CHECK (status IN ('sending','obsolete_uncertain','written','failed')),
    failure text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX runner_ref_writes_uncertain ON runner_ref_writes (session_id)
    WHERE status IN ('sending','obsolete_uncertain');

CREATE TABLE operator_actions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    outcome text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE publication_comments (
    id uuid PRIMARY KEY,
    request_id uuid UNIQUE REFERENCES requests (id),
    task_id uuid REFERENCES task_state (task_id),
    kind text NOT NULL DEFAULT 'reply' CHECK (kind IN ('reply','notice','dashboard')),
    source_revision bigint,
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    marker text NOT NULL,
    body text NOT NULL,
    status text NOT NULL CHECK (status IN ('recorded', 'sending', 'written', 'failed')),
    claim_token uuid,
    comment_id bigint,
    comment_url text,
    failure text,
    retry_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repository_id, issue_number, marker)
);
CREATE INDEX publication_comments_pending ON publication_comments (updated_at, id)
    WHERE status IN ('recorded', 'sending');

CREATE TABLE dashboard_surfaces (
    task_id uuid PRIMARY KEY REFERENCES task_state (task_id),
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    revision bigint NOT NULL,
    desired_body text NOT NULL,
    create_id uuid NOT NULL REFERENCES publication_comments (id),
    watch boolean NOT NULL DEFAULT true,
    last_error text,
    retry_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dashboard_writes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES dashboard_surfaces (task_id),
    revision bigint NOT NULL,
    body text NOT NULL,
    status text NOT NULL CHECK (status IN ('sending','obsolete_uncertain','written','failed')),
    failure text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX dashboard_writes_uncertain ON dashboard_writes (task_id)
    WHERE status IN ('sending','obsolete_uncertain');

CREATE TABLE stage_label_surfaces (
    task_id uuid PRIMARY KEY REFERENCES task_state (task_id),
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    revision bigint NOT NULL,
    desired text,
    watch boolean NOT NULL DEFAULT true,
    last_error text,
    retry_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE stage_label_writes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES stage_label_surfaces (task_id),
    revision bigint NOT NULL,
    action text NOT NULL CHECK (action IN ('create', 'add', 'remove')),
    label text NOT NULL,
    status text NOT NULL CHECK (status IN ('sending', 'obsolete_uncertain', 'written', 'failed')),
    failure text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX stage_label_writes_uncertain ON stage_label_writes (task_id)
    WHERE status IN ('sending', 'obsolete_uncertain');

CREATE TABLE archive_uploads (
    gzip_sha256 text PRIMARY KEY CHECK (gzip_sha256 ~ '^[0-9a-f]{64}$'),
    gzip_bytes bytea,
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    status text NOT NULL CHECK (status IN ('pending','retrying','exhausted','verified')),
    upload_attempts integer NOT NULL DEFAULT 0,
    retry_generation integer NOT NULL DEFAULT 0,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE turn_archives (
    turn_id uuid PRIMARY KEY REFERENCES attempts (id),
    repository_id bigint NOT NULL,
    issue_number bigint NOT NULL,
    gzip_sha256 text REFERENCES archive_uploads (gzip_sha256),
    tar_sha256 text,
    retained_file_bytes bigint CHECK (retained_file_bytes >= 0),
    session_scope text,
    session_turn bigint CHECK (session_turn >= 0),
    archive_error text,
    journal jsonb,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE transcript_events (
    tar_sha256 text NOT NULL,
    member_ordinal integer NOT NULL,
    line_number integer NOT NULL,
    member_path text NOT NULL,
    member_mtime bigint NOT NULL,
    harness text NOT NULL,
    event_kind text,
    event_json jsonb,
    fallback_bytes bytea,
    diagnostic text,
    PRIMARY KEY (tar_sha256, member_ordinal, line_number)
);
CREATE INDEX transcript_display ON transcript_events
    (tar_sha256, member_mtime, member_path COLLATE "C", member_ordinal, line_number);

CREATE TABLE transcript_text_parts (
    tar_sha256 text NOT NULL,
    member_ordinal integer NOT NULL,
    line_number integer NOT NULL,
    part_index integer NOT NULL,
    search_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    PRIMARY KEY (tar_sha256, member_ordinal, line_number, part_index),
    FOREIGN KEY (tar_sha256, member_ordinal, line_number)
        REFERENCES transcript_events (tar_sha256, member_ordinal, line_number)
);
CREATE INDEX transcript_text_search ON transcript_text_parts USING gin (search_vector);

-- Prefer LZ4 for large retained values; keep the database's PGLZ fallback usable.
DO $$
DECLARE
    method text;
    column_target text;
BEGIN
    SELECT CASE WHEN 'lz4' = ANY(enumvals) THEN 'lz4' ELSE 'pglz' END
    INTO method FROM pg_settings WHERE name = 'default_toast_compression';
    method := COALESCE(method, 'pglz');
    FOREACH column_target IN ARRAY ARRAY[
        'archive_uploads.gzip_bytes',
        'task_state.state', 'initialization_inputs.snapshot',
        'attempts.submit_body', 'attempts.observation', 'attempts.result',
        'intake_reports.raw_report', 'turn_archives.journal',
        'transcript_events.event_json', 'transcript_events.fallback_bytes',
        'transcript_text_parts.search_text'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN %I SET COMPRESSION %s',
            split_part(column_target, '.', 1), split_part(column_target, '.', 2), method
        );
    END LOOP;
END $$;
