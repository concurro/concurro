"""Settle GitHub write outcomes and retain bounded operator diagnostics."""

import logging
import re

from github import NotFound, Transient

from .admission import OID, ready_pull, resolved_thread
from .transport import throttle

log = logging.getLogger("concurro.service")


class Rejected(RuntimeError):
    """GitHub rejected a write; it did not take effect."""

    def __init__(self, message, status=None):
        """Keep the HTTP status separate from operator-facing diagnostic text."""
        super().__init__(message)
        self.status = status


class TreeUnavailable(Rejected):
    """Commit creation was rejected because its tree is not yet visible to GitHub."""


def settle(path, response, now, body=None):
    """`(recorded status, payload, error for the engine)` for one write's response.

    A REST 2xx establishes that the write applied even when its body is lost; a GraphQL
    response is `done` only with a complete result and no errors, since execution can
    report errors after its resolver ran.
    """
    if not response.sent:
        return "failed", None, Transient("unavailable")
    status = response.status
    if status == 401:
        return "failed", None, Transient("unavailable")
    delay = throttle(response, now) if status == 403 else None
    if delay is not None:
        return "failed", None, Transient("throttled", delay)
    if path == "graphql":
        try:
            payload = response.json()
        except ValueError:
            return "unknown", None, None
        data = payload.get("data") if isinstance(payload, dict) and "errors" not in payload else None
        if status == 200 and isinstance(data, dict):
            ready = ready_pull(body)
            if ready is not None:
                pr = (data.get("markPullRequestReadyForReview") or {}).get("pullRequest") or {}
                if pr.get("id") == ready and pr.get("isDraft") is False:
                    return "done", payload, None
            elif resolved_thread(body) is not None:
                thread = (data.get("resolveReviewThread") or {}).get("thread") or {}
                if thread.get("isResolved") is True:
                    return "done", payload, None
        return "unknown", None, None
    if status == 204:
        return "done", None, None
    if 200 <= status < 300:
        try:
            return "done", response.json(), None
        except ValueError:
            return "done", None, Transient("response_lost")
    if status == 404:
        return "failed", None, NotFound(f"GitHub rejected the write (HTTP {status})")
    if 400 <= status < 500 and status not in (408, 429):
        # GitHub's own validation message, for the operator; the engine publishes only fixed diagnostics.
        detail = rejection(response)
        log.warning("GitHub rejected a write to %s (HTTP %s): %s", path, status, detail)
        error_type = Rejected
        if (
            status == 422
            and re.fullmatch(r"repos/[^/]+/[^/]+/git/commits", path)
            and isinstance(body, dict)
            and isinstance(body.get("tree"), str)
            and OID.fullmatch(body["tree"])
        ):
            if missing_tree(response):
                error_type = TreeUnavailable
        return "failed", None, error_type(f"GitHub rejected the write (HTTP {status}): {detail}", status=status)
    return "unknown", None, None


def missing_tree(response):
    """Recognize a missing tree by structured validation fields, then normalized legacy text."""
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    errors = payload.get("errors")
    if errors:
        return isinstance(errors, list) and all(
            isinstance(error, dict) and error.get("field") == "tree" and error.get("code") == "missing"
            for error in errors
        )
    message = payload.get("message")
    return isinstance(message, str) and " ".join(message.split()).rstrip(".").casefold() == "tree sha does not exist"


def rejection(response):
    """Bounded, single-line GitHub validation details for operator logs, excluding arbitrary payload fields."""

    def clean(value):
        """Keep string diagnostics printable and bounded, including terminal control characters."""
        if not isinstance(value, str):
            return ""
        return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:300]

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return "no message"
    parts = [clean(payload.get("message")) or "no message"]
    errors = payload.get("errors")
    if isinstance(errors, list):
        for error in errors[:5]:
            if isinstance(error, dict):
                detail = " ".join(
                    f"{key}={value}"
                    for key in ("resource", "field", "code", "message")
                    if (value := clean(error.get(key)))
                )
            else:
                detail = clean(error)
            if detail:
                parts.append(detail)
        if len(errors) > 5:
            parts.append("additional errors omitted")
    return "; ".join(parts)[:2000]
