CREATE TABLE tasks.cutover_targets (
    repository_id bigint PRIMARY KEY,
    activation_at timestamptz NOT NULL,
    source_incarnation text NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence >= 0),
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tasks.cutover_floors (
    repository_id bigint NOT NULL REFERENCES tasks.cutover_targets (repository_id),
    issue_number bigint NOT NULL CHECK (issue_number > 0),
    generation bigint NOT NULL CHECK (generation >= 0),
    openai_epoch bigint NOT NULL CHECK (openai_epoch >= -1),
    anthropic_epoch bigint NOT NULL CHECK (anthropic_epoch >= -1),
    PRIMARY KEY (repository_id, issue_number)
);

CREATE TABLE tasks.cutover_pr_index (
    upstream text NOT NULL,
    pr_number bigint NOT NULL CHECK (pr_number > 0),
    document jsonb NOT NULL,
    PRIMARY KEY (upstream, pr_number)
);

ALTER TABLE tasks.pr_ownership DROP CONSTRAINT pr_ownership_owner_kind_check;
ALTER TABLE tasks.pr_ownership ADD CONSTRAINT pr_ownership_owner_kind_check
    CHECK (owner_kind IN ('contribution', 'standalone_pr_review', 'python'));
