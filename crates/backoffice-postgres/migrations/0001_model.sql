CREATE SCHEMA IF NOT EXISTS backoffice;

CREATE TABLE backoffice.model_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    ready boolean NOT NULL DEFAULT false,
    baseline_id uuid,
    revision bigint NOT NULL DEFAULT 0
);
INSERT INTO backoffice.model_state (singleton) VALUES (true);

CREATE TABLE backoffice.model_items (
    entity text NOT NULL,
    entity_id text NOT NULL,
    revision bigint NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (entity, entity_id)
);
CREATE INDEX model_tasks ON backoffice.model_items (entity, (body->>'repository_id'), (body->>'issue_number'))
    WHERE entity IN ('contribution_task','standalone_review');

CREATE TABLE backoffice.baseline_stage (
    snapshot_id uuid NOT NULL,
    ordinal bigint NOT NULL,
    entity text NOT NULL,
    entity_id text NOT NULL,
    revision bigint NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, ordinal)
);

CREATE TABLE backoffice.model_events (
    id uuid PRIMARY KEY,
    received_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    event_type text NOT NULL,
    entity text,
    revision bigint,
    data jsonb NOT NULL,
    applied boolean NOT NULL DEFAULT false,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_events_unapplied ON backoffice.model_events (received_order) WHERE NOT applied;
