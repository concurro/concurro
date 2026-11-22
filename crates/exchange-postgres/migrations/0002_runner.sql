ALTER TABLE sessions ADD COLUMN runner_token_hash text;
ALTER TABLE sessions ADD COLUMN session_key_hash text;

CREATE TABLE oidc_jti (
    jti text PRIMARY KEY,
    expires timestamptz NOT NULL
);

CREATE TABLE blobs (
    sha256 text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('archive','workspace')),
    size bigint NOT NULL CHECK (size >= 0),
    stored_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE blob_uploads (
    session_id uuid NOT NULL REFERENCES sessions(session_id),
    sha256 text NOT NULL REFERENCES blobs(sha256),
    PRIMARY KEY (session_id,sha256)
);
