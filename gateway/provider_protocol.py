"""Fixed provider routes and sanitized response parsing shared by inference and validation."""

import json
import runpy
from pathlib import Path

CATALOG = Path(__file__).with_name("credential_routes.py")
if not CATALOG.exists():
    CATALOG = Path(__file__).resolve().parents[1] / "engine" / "credential_routes.py"
CATALOG_VALUES = runpy.run_path(str(CATALOG))
ROUTES = CATALOG_VALUES["ROUTES"]
VALIDATION_SECONDS = CATALOG_VALUES["VALIDATION_SECONDS"]
UPSTREAMS = {route: value["url"] for route, value in ROUTES.items()}
MAX_EVENT = 8 * 1024 * 1024


def category(value, route):
    """Classify documented API structured codes; subscription taxonomy remains unverified."""
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if value.get("type") == "response.failed":
        response = value.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return None
    code, kind = error.get("code"), error.get("type")
    code = code if isinstance(code, str) else None
    kind = kind if isinstance(kind, str) else None
    if route == "openai-api-key":
        return {
            "invalid_api_key": "auth",
            "insufficient_quota": "quota_exhausted",
            "credit_balance_exhausted": "quota_exhausted",
            "organization_spend_limit_exceeded": "quota_exhausted",
            "project_spend_limit_exceeded": "quota_exhausted",
            "organization_usage_limit_exceeded": "quota_exhausted",
            "rate_limit_exceeded": "rate_limited",
            "slow_down": "rate_limited",
        }.get(code)
    if route == "anthropic-api-key" and kind == "authentication_error":
        return "auth"
    # Anthropic rate_limit_error also covers spending limits; HTTP429 alone is ambiguous.
    return None


def tokens(value):
    """Keep nonnegative integral token counts without converting missing usage to zero."""
    if not isinstance(value, dict):
        return None
    fields = ("input_tokens", "output_tokens")
    return {k: value[k] for k in fields} if all(type(value.get(k)) is int and value[k] >= 0 for k in fields) else None


class Observation:
    """Parse bounded structured results without retaining arbitrary provider error text."""

    def __init__(self, route, status, delay=None):
        """Bind a parser to its trusted route and numeric provider retry hint."""
        self.route = route
        self.result = {"status": status, "category": None, "usage": None, "completed": False, "retry_after": None}
        self.delay = int(delay) if delay and delay.isascii() and delay.isdecimal() and len(delay) <= 9 else None
        self.input_tokens = None
        self.ambiguous = False

    def accept(self, value):
        """Inspect one JSON value and identify events whose detail must be suppressed."""
        if not isinstance(value, dict):
            self.ambiguous = True
            return False
        kind = value.get("type")
        if kind in ("error", "response.failed") or value.get("error") is not None:
            self.result["category"] = category(value, self.route) or "unknown"
            if self.result["category"] == "rate_limited":
                self.result["retry_after"] = self.delay
            return True
        response = value.get("response", value) if kind == "response.completed" else value
        if not isinstance(response, dict):
            self.ambiguous = True
            return False
        if kind == "response.completed" or value.get("object") == "response" and value.get("status") == "completed":
            self.result.update(completed=True, usage=tokens(response.get("usage")))
        if kind == "message" and value.get("stop_reason"):
            self.result.update(completed=True, usage=tokens(value.get("usage")))
        if kind == "message_start":
            message = value.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            self.input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if kind == "message_delta":
            self.result["usage"] = tokens(
                {
                    "input_tokens": self.input_tokens,
                    "output_tokens": value["usage"].get("output_tokens")
                    if isinstance(value.get("usage"), dict)
                    else None,
                }
            )
        if kind == "message_stop":
            self.result["completed"] = True
        return False

    def finish(self):
        """Leave truncated, unsupported and incomplete usage unknown."""
        if self.ambiguous or not self.result["completed"]:
            self.result["category"] = self.result["category"] or "unknown"
            self.result["usage"] = None
        return self.result


def event_value(event):
    """Decode one bounded SSE frame, preserving explicit error labels and terminal markers."""
    payload = b"\n".join(line[5:].strip() for line in event.splitlines() if line.startswith(b"data:"))
    value = {} if payload in (b"", b"[DONE]") else json.loads(payload)
    error = any(line.startswith(b"event:") and line[6:].strip() == b"error" for line in event.splitlines())
    return value, error


def probe_request(route, model, secret, account_id=None):
    """Build a fixed, minimal generation probe with only this candidate's credential."""
    if route == "chatgpt-subscription":
        return (
            UPSTREAMS[route] + "/responses",
            {
                "Authorization": "Bearer " + secret,
                "ChatGPT-Account-Id": account_id,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
            },
            {
                "model": model,
                "instructions": "Reply with OK.",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "OK"}]}],
                "stream": True,
                "store": False,
            },
        )
    if route == "claude-subscription":
        return (
            UPSTREAMS[route] + "/messages",
            {
                "Authorization": "Bearer " + secret,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
            },
            {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "Reply OK."}], "stream": True},
        )
    raise ValueError("not a subscription route")
