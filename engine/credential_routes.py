"""Trusted route ownership and credential kinds, shared with the standalone Exchange."""

ROUTES = {
    "openai-api-key": {"sponsorship_provider": "openai", "kind": "api_key", "url": "https://api.openai.com/v1"},
    "chatgpt-subscription": {
        "sponsorship_provider": "openai",
        "kind": "subscription",
        "url": "https://chatgpt.com/backend-api/codex",
    },
    "anthropic-api-key": {
        "sponsorship_provider": "anthropic",
        "kind": "api_key",
        "url": "https://api.anthropic.com/v1",
    },
    "claude-subscription": {
        "sponsorship_provider": "anthropic",
        "kind": "subscription",
        "url": "https://api.anthropic.com/v1",
    },
}

# One subscription model probe must finish within this wall-clock interval.
VALIDATION_SECONDS = 10
