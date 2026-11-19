CREATE TABLE outbox (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id uuid NOT NULL,
    subject text NOT NULL,
    type text NOT NULL,
    version integer NOT NULL,
    message jsonb NOT NULL,
    wire_bytes bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    retry_at timestamptz,
    last_error text,
    failure_count integer NOT NULL DEFAULT 0,
    UNIQUE (id, version)
);
CREATE INDEX outbox_unpublished ON outbox (sequence) WHERE published_at IS NULL;

CREATE TABLE consumed (
    consumer text NOT NULL,
    event_id uuid NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('applied', 'parked', 'discarded')),
    PRIMARY KEY (consumer, event_id)
);

CREATE TABLE identity_aliases (
    consumer text NOT NULL,
    fallback_key text NOT NULL,
    event_id uuid NOT NULL,
    PRIMARY KEY (consumer, fallback_key)
);

CREATE TABLE attempts (
    consumer text NOT NULL,
    event_id uuid NOT NULL,
    count integer NOT NULL,
    last_error text,
    retry_at timestamptz,
    PRIMARY KEY (consumer, event_id)
);

CREATE TABLE held (
    consumer text NOT NULL,
    event_id uuid NOT NULL,
    key text NOT NULL,
    message jsonb,
    wire_bytes bytea NOT NULL,
    released boolean NOT NULL DEFAULT false,
    retry_at timestamptz,
    PRIMARY KEY (consumer, event_id)
);
CREATE INDEX held_key ON held (consumer, key) WHERE NOT released;
CREATE INDEX held_released ON held (consumer, retry_at) WHERE released;

CREATE TABLE parked (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    consumer text NOT NULL,
    event_id uuid NOT NULL,
    message jsonb,
    wire_bytes bytea NOT NULL,
    reason text NOT NULL,
    parked_at timestamptz NOT NULL DEFAULT now(),
    replayed_by text,
    replayed_at timestamptz,
    discarded_by text,
    discarded_reason text,
    discarded_at timestamptz
);
CREATE UNIQUE INDEX parked_open ON parked (consumer, event_id)
    WHERE replayed_at IS NULL AND discarded_at IS NULL;

CREATE TABLE revisions (
    consumer text NOT NULL,
    event_type text NOT NULL,
    entity text NOT NULL,
    revision bigint NOT NULL,
    PRIMARY KEY (consumer, event_type, entity)
);
