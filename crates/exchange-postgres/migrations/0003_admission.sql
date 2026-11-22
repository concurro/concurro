ALTER TABLE sessions ADD COLUMN spent_tokens bigint NOT NULL DEFAULT 0;

ALTER TABLE turns ADD COLUMN finished_event_emitted boolean NOT NULL DEFAULT false;

CREATE TABLE inference_requests (
    request_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(session_id),
    turn_id uuid NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
    grant_id text NOT NULL REFERENCES grants(grant_id),
    model text NOT NULL,
    started timestamptz NOT NULL DEFAULT now(),
    finished timestamptz,
    settlement_deadline timestamptz,
    dispatched boolean,
    category text,
    usage jsonb,
    usage_unavailable text,
    retry_after double precision
);
CREATE INDEX inference_requests_open ON inference_requests(turn_id)
    WHERE finished IS NULL;
