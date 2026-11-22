CREATE TABLE grants (
    grant_id text PRIMARY KEY,
    body jsonb,
    digest text,
    status text NOT NULL CHECK (status IN ('issued','revoked')),
    revoked_at timestamptz,
    CHECK ((body IS NULL AND digest IS NULL AND status='revoked') OR
           (body IS NOT NULL AND digest IS NOT NULL))
);

CREATE TABLE sessions (
    session_id uuid PRIMARY KEY,
    staffing_group text NOT NULL,
    grant_id text NOT NULL REFERENCES grants(grant_id),
    expected_claims jsonb NOT NULL,
    binding jsonb NOT NULL,
    models jsonb NOT NULL,
    budget bigint NOT NULL,
    lease_seconds bigint NOT NULL,
    run_id text,
    created timestamptz NOT NULL,
    bound_at timestamptz,
    last_seen timestamptz,
    deadline timestamptz NOT NULL,
    ended timestamptz,
    end_reason text,
    refusal text
);
CREATE INDEX live_staffing ON sessions(staffing_group,created) WHERE ended IS NULL;

CREATE TABLE turns (
    turn_id uuid PRIMARY KEY,
    attempt_id uuid NOT NULL,
    task_id uuid NOT NULL,
    grant_id text NOT NULL REFERENCES grants(grant_id),
    digest text NOT NULL,
    body jsonb NOT NULL,
    staffing_group text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued','running','finished','lost','cancelled')),
    session_id uuid REFERENCES sessions(session_id),
    revision bigint NOT NULL CHECK(revision > 0),
    result jsonb,
    provider_failure jsonb,
    provider_requests text,
    created timestamptz NOT NULL,
    started timestamptz,
    finished timestamptz
);
CREATE INDEX queued_turns ON turns(staffing_group,created,turn_id) WHERE status='queued';

CREATE TABLE turn_sessions (
    turn_id uuid NOT NULL,
    session_id uuid NOT NULL REFERENCES sessions(session_id),
    PRIMARY KEY(turn_id,session_id)
);

CREATE TABLE turn_tombstones (
    turn_id uuid PRIMARY KEY,
    digest text,
    result jsonb,
    created timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE relay_claims (
    claim_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(session_id),
    release_status text NOT NULL CHECK (release_status IN ('pending','acknowledged'))
);
