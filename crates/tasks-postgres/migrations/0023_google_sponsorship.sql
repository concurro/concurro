ALTER TABLE tasks.sponsorship_facts
    DROP CONSTRAINT sponsorship_facts_provider_check,
    ADD CONSTRAINT sponsorship_facts_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.sponsorship_removals
    DROP CONSTRAINT sponsorship_removals_provider_check,
    ADD CONSTRAINT sponsorship_removals_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.sponsorship_transitions
    DROP CONSTRAINT sponsorship_transitions_provider_check,
    ADD CONSTRAINT sponsorship_transitions_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.funding_grants
    DROP CONSTRAINT funding_grants_provider_check,
    ADD CONSTRAINT funding_grants_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.funding_exclusions
    DROP CONSTRAINT funding_exclusions_provider_check,
    ADD CONSTRAINT funding_exclusions_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.standalone_review_funding_facts
    DROP CONSTRAINT standalone_review_funding_facts_provider_check,
    ADD CONSTRAINT standalone_review_funding_facts_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.standalone_review_funding_removals
    DROP CONSTRAINT standalone_review_funding_removals_provider_check,
    ADD CONSTRAINT standalone_review_funding_removals_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.standalone_review_funding_grants
    DROP CONSTRAINT standalone_review_funding_grants_provider_check,
    ADD CONSTRAINT standalone_review_funding_grants_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));

ALTER TABLE tasks.standalone_review_applied_funding_facts
    DROP CONSTRAINT standalone_review_applied_funding_facts_provider_check,
    ADD CONSTRAINT standalone_review_applied_funding_facts_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google'));
