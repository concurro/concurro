CREATE TABLE deliveries (
    guid text PRIMARY KEY,
    event text NOT NULL,
    action text,
    installation_id bigint,
    repository_id bigint,
    received_at timestamptz NOT NULL,
    payload bytea,
    outcome text NOT NULL CHECK (
        outcome IN ('published', 'unmodeled', 'unmappable', 'other_installation')
    ),
    mapping_revision integer,
    error text,
    CHECK ((outcome = 'published') = (mapping_revision IS NOT NULL))
);
CREATE INDEX deliveries_received ON deliveries (received_at);
CREATE INDEX deliveries_unmappable ON deliveries (received_at)
    WHERE outcome = 'unmappable';

CREATE TABLE coverage (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    covered_until timestamptz,
    first_started_at timestamptz NOT NULL
);
INSERT INTO coverage (id, first_started_at) VALUES (true, clock_timestamp());

