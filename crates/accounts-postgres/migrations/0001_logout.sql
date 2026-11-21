CREATE TABLE accounts.logout_receipts (
    id uuid PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('pending', 'revoked', 'unknown')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE accounts.logout_jobs (
    id uuid PRIMARY KEY,
    receipt_id uuid NOT NULL UNIQUE REFERENCES accounts.logout_receipts(id),
    nonce bytea,
    sealed bytea,
    state text NOT NULL CHECK (state IN ('pending', 'done', 'unknown')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'pending' AND nonce IS NOT NULL AND sealed IS NOT NULL)
        OR (state <> 'pending' AND nonce IS NULL AND sealed IS NULL))
);
