"""Trusted route ownership and credential kinds, shared with the standalone Exchange."""

ROUTES = {
    "openai-api-key": {"sponsorship_provider": "openai", "kind": "api_key"},
    "chatgpt-subscription": {"sponsorship_provider": "openai", "kind": "subscription"},
    "anthropic-api-key": {"sponsorship_provider": "anthropic", "kind": "api_key"},
    "claude-subscription": {"sponsorship_provider": "anthropic", "kind": "subscription"},
}
