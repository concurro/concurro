ALTER TABLE accounts.credential_entries
    DROP CONSTRAINT credential_entries_provider_check,
    ADD CONSTRAINT credential_entries_provider_check
        CHECK (provider IN ('anthropic', 'google', 'openai'));
