"""Trusted route ownership and credential kinds, shared with the standalone Exchange."""

ROUTES = {
    "gemini-api-key": {
        "sponsorship_provider": "google",
        "kind": "api_key",
        "url": "https://generativelanguage.googleapis.com",
    },
    "antigravity-subscription": {
        "sponsorship_provider": "google",
        "kind": "subscription",
        "url": "https://daily-cloudcode-pa.googleapis.com",
    },
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
