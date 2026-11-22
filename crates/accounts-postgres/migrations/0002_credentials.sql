-- Native credential store, mirroring the Python vault's model with the same identities
-- (gateway/vault/vault.py). Timestamps are kept as the vault's ISO-8601 text so imported
-- history and as-of reads round-trip exactly; JSON values are kept as the vault's text.

CREATE TABLE accounts.credential_entries (
    entry_id text PRIMARY KEY,
    user_id bigint NOT NULL CHECK (user_id > 0),
    provider text NOT NULL CHECK (provider IN ('anthropic', 'openai')),
    route text NOT NULL,
    name text NOT NULL,
    enabled boolean NOT NULL,
    roles text NOT NULL,
    profiles text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0),
    created text NOT NULL,
    updated text NOT NULL,
    removed text
);
CREATE INDEX credential_entries_user ON accounts.credential_entries (user_id, provider);

CREATE TABLE accounts.credential_lists (
    user_id bigint NOT NULL,
    provider text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0),
    entries text NOT NULL,
    PRIMARY KEY (user_id, provider)
);

-- Each list revision's selection policy; `at` NULL means in force at every boundary,
-- as the vault's legacy revision 0 with an empty `at`.
CREATE TABLE accounts.credential_policy (
    user_id bigint NOT NULL,
    provider text NOT NULL,
    revision bigint NOT NULL,
    at text,
    document text NOT NULL,
    PRIMARY KEY (user_id, provider, revision)
);

-- One credential version. `activated` is set once a version is stored (or committed as a
-- relay version, or imported); versions that never activated are never listed.
CREATE TABLE accounts.credential_versions (
    user_id bigint NOT NULL,
    route text NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    record_id text NOT NULL UNIQUE,
    entry_id text NOT NULL,
    login text NOT NULL,
    status text NOT NULL CHECK (status IN ('valid', 'invalid', 'revoked')),
    fingerprint text NOT NULL,
    created text NOT NULL,
    updated text NOT NULL,
    kind text,
    models text,
    retired text,
    account_label text,
    verified_account text,
    status_reason text,
    validated text,
    expires_at text,
    expiry_known boolean NOT NULL DEFAULT false,
    token_revision bigint NOT NULL DEFAULT 1,
    last_used text,
    custody text NOT NULL DEFAULT 'vault' CHECK (custody IN ('vault', 'relay')),
    relay_id text,
    -- `importing` is an imported version whose material is still to be copied.
    material text NOT NULL CHECK (material IN
        ('pending', 'importing', 'stored', 'failed', 'superseded', 'destroying', 'destroyed', 'none')),
    activated boolean NOT NULL,
    -- While pending: the mutation that wrote it and either, for a replace, the entry
    -- revision it was made against, or, for an add, the new entry's provider and policy.
    mutation_id text,
    expected_revision bigint,
    pending_entry text,
    pending_since timestamptz,
    PRIMARY KEY (user_id, route, version)
);
CREATE INDEX credential_versions_entry ON accounts.credential_versions (entry_id, version);
CREATE INDEX credential_versions_pending ON accounts.credential_versions (pending_since)
    WHERE material = 'pending';

-- Owner mutation replay journal. The digest covers the request without any secret value.
-- `response` is NULL while the mutation's version is pending.
CREATE TABLE accounts.credential_mutations (
    user_id bigint NOT NULL,
    mutation_id text NOT NULL,
    digest text NOT NULL,
    response text,
    PRIMARY KEY (user_id, mutation_id)
);

-- Versions reserved for an owner's relay approval before the verifier is asked. Each row
-- of one mutation carries the operation to finish after approval, so a leftover reservation
-- can repeat the identical approval and commit or release it.
CREATE TABLE accounts.relay_intents (
    user_id bigint NOT NULL,
    route text NOT NULL,
    version bigint NOT NULL,
    mutation_id text NOT NULL,
    digest text NOT NULL,
    binding text NOT NULL,
    operation text NOT NULL,
    reserved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, route, version)
);
CREATE INDEX relay_intents_mutation ON accounts.relay_intents (user_id, mutation_id);

-- The change feed's sequence, one row per committed change fact.
CREATE TABLE accounts.credential_changes (
    sequence bigserial PRIMARY KEY,
    user_id bigint NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);

-- One vault import run, keyed by the digest of the vault database copy it read.
CREATE TABLE accounts.vault_imports (
    source_sha256 text PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    metadata_at timestamptz,
    finished_at timestamptz
);
