CREATE TABLE stream_generations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stream_created text NOT NULL UNIQUE,
    observed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE outbox
    ADD COLUMN stream_generation bigint REFERENCES stream_generations(id),
    ADD COLUMN stream_sequence bigint;
ALTER TABLE outbox
    ADD CONSTRAINT outbox_position_pair CHECK (
        (stream_generation IS NULL) = (stream_sequence IS NULL)
    );
CREATE INDEX outbox_position ON outbox (stream_generation, stream_sequence)
    WHERE stream_generation IS NOT NULL;
UPDATE outbox SET published_at = NULL WHERE published_at IS NOT NULL;

CREATE TABLE journal_state (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    pruned_generation bigint REFERENCES stream_generations(id),
    pruned_sequence bigint,
    CHECK ((pruned_generation IS NULL) = (pruned_sequence IS NULL))
);
INSERT INTO journal_state (id) VALUES (true);

CREATE TABLE publication_barrier (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    covered_until timestamptz,
    through_sequence bigint
);
INSERT INTO publication_barrier (id) VALUES (true);

CREATE TABLE replay_requests (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    consumer text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    floor_created text NOT NULL,
    floor_sequence bigint NOT NULL,
    target_created text NOT NULL,
    replayed_through bigint,
    unrecoverable boolean NOT NULL,
    upper_sequence bigint NOT NULL DEFAULT 0,
    cursor_sequence bigint NOT NULL DEFAULT 0,
    replayed_created text,
    replayed_stream_sequence bigint,
    completed_at timestamptz
);
CREATE UNIQUE INDEX replay_in_progress ON replay_requests (consumer, floor_created, floor_sequence, target_created)
    WHERE completed_at IS NULL;

CREATE TABLE consumer_positions (
    consumer text PRIMARY KEY,
    stream_created text NOT NULL,
    floor_sequence bigint NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE consumer_losses (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    consumer text NOT NULL,
    floor_created text NOT NULL,
    floor_sequence bigint NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);
CREATE UNIQUE INDEX consumer_open_loss ON consumer_losses (consumer)
    WHERE resolved_at IS NULL;
ALTER TABLE events.outbox ALTER COLUMN message DROP NOT NULL;

