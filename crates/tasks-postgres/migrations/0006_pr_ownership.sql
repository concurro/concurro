CREATE TABLE tasks.pr_ownership (
    upstream text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    owner_kind text NOT NULL CHECK (owner_kind IN ('contribution', 'standalone_pr_review')),
    owner_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (upstream, pr_number),
    UNIQUE (owner_kind, owner_id)
);
