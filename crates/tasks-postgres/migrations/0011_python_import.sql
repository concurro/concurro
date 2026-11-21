CREATE TABLE tasks.python_import_sources (
    deployment text NOT NULL,
    source_path text NOT NULL,
    kind text NOT NULL,
    gzip_sha256 text,
    tar_sha256 text,
    byte_count bigint,
    status text NOT NULL,
    diagnostic text,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (deployment, source_path)
);

CREATE TABLE tasks.python_import_turns (
    deployment text NOT NULL,
    source_id text NOT NULL,
    repository text NOT NULL,
    issue_number bigint NOT NULL,
    generation bigint,
    stage text NOT NULL,
    status text NOT NULL,
    session_scope text,
    requested_turn bigint,
    completed_turn bigint,
    serving_session text,
    serving_run text,
    staffing jsonb NOT NULL,
    source_record jsonb NOT NULL,
    gzip_sha256 text,
    tar_sha256 text,
    import_status text NOT NULL,
    diagnostic text,
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (deployment, source_id)
);
CREATE INDEX python_import_turns_issue
    ON tasks.python_import_turns (repository, issue_number, imported_at);

CREATE TABLE tasks.python_import_sessions (
    deployment text NOT NULL,
    repository text NOT NULL,
    session_scope text NOT NULL,
    source_path text NOT NULL,
    gzip_sha256 text,
    completed_turn bigint,
    status text NOT NULL,
    diagnostic text,
    PRIMARY KEY (deployment, repository, session_scope)
);
