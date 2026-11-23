CREATE TABLE revoked_credential_versions (
    payer_id bigint NOT NULL CHECK (payer_id > 0),
    record_id text NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    source_sequence bigint NOT NULL,
    revoked_at timestamptz NOT NULL,
    PRIMARY KEY (payer_id, record_id, version)
);
