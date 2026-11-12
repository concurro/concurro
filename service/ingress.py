"""Webhook ingress: verify, filter, de-duplicate and enqueue; never read task state or call the engine."""

import hashlib
import hmac
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .observations import expected as comment_expectation

ENROLLMENT = "concurro"
EVENTS = {
    "push",
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "workflow_run",
    "check_suite",
    "check_run",
    "status",
    "pull_request_review_comment",
    "pull_request_review_thread",
}
# The Actions workflow's triggers and filters, kept for parity.
ISSUE_ACTIONS = {"opened", "labeled", "unlabeled", "edited", "reopened", "closed"}
DISPLAY_LABELS = ("concurro:stage:", "concurro:attention")
COMMENT_ACTIONS = {"created", "edited", "deleted"}
CI_EVENTS = {"push", "workflow_run", "check_run", "check_suite", "status"}
WRITER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# GitHub caps webhook payloads at 25 MB.
BODY_LIMIT = 25_000_000
# A backoffice command is a few hundred bytes.
ACTION_LIMIT = 16 * 1024

log = logging.getLogger("concurro.service")


class Ingress:
    """Map verified deliveries from enabled repositories and the App's own installations to tasks."""

    def __init__(
        self,
        secret,
        registry,
        installations,
        identities,
        clock=time.time,
        issue_repositories=None,
        command_comment=None,
    ):
        """`installations` maps each enabled repository to the App's installation ID on it."""
        self.secret, self.registry, self.clock = secret, registry, clock
        self.command_comment = command_comment
        self.installations = dict(installations)
        self.repositories = {repository.lower(): repository for repository in installations}
        self.identities = frozenset(identities)
        self.issue_repositories = frozenset(installations if issue_repositories is None else issue_repositories)

    def handle(self, headers, body):
        """`(HTTP status, short text)` for one delivery; anything rejected has no side effect."""
        expected = "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("X-Hub-Signature-256", ""), expected):
            return 401, "invalid signature"
        event, delivery = headers.get("X-GitHub-Event", ""), headers.get("X-GitHub-Delivery", "")
        if event == "ping":
            return 200, "pong"
        if event not in EVENTS:
            return 202, "ignored"
        if not delivery:
            return 400, "missing delivery"
        try:
            payload = json.loads(body)
            name = payload["repository"]["full_name"]
            installation = payload["installation"]["id"]
        except (ValueError, KeyError, TypeError):
            return 400, "malformed payload"
        repository = self.repositories.get(name.lower()) if isinstance(name, str) else None
        if repository is None or installation != self.installations[repository]:
            return 403, "repository or installation not enabled"
        try:
            keys = self.keys(repository, event, payload)
        except (KeyError, TypeError, AttributeError):
            return 400, "malformed payload"
        if not keys:
            return 202, "ignored"
        observation = event == "issue_comment" and self.ours(payload["comment"]["user"]["login"])
        expectation = None
        if observation:
            previous = ((payload.get("changes") or {}).get("body") or {}).get("from")
            expectation = comment_expectation(payload["comment"], payload["action"], previous)
        if not self.registry.receive(
            delivery, keys, event, ci=event in CI_EVENTS, observation=observation, expectation=expectation
        ):
            return 200, "duplicate"
        return 202, "accepted"

    def ours(self, login):
        """Whether a login is a Concurro identity."""
        return login in self.identities

    def keys(self, repository, event, payload):
        """The tasks a delivery concerns: issue events directly, PR and check events through recorded PRs."""
        action = payload.get("action")
        if (
            event == "issue_comment"
            and self.command_comment is not None
            and self.ours(payload["comment"]["user"]["login"])
        ):
            issue, comment = payload["issue"], payload["comment"]
            key = (repository, issue["number"])
            previous = ((payload.get("changes") or {}).get("body") or {}).get("from", "")
            if (
                repository in self.issue_repositories
                and "pull_request" not in issue
                and action in COMMENT_ACTIONS
                and (
                    self.command_comment(comment.get("body") or "")
                    or self.command_comment(previous or "")
                    or self.registry.observed_comment(key, comment.get("id"))
                )
            ):
                return [key]
            return []
        if self.ours((payload.get("sender") or {}).get("login")):
            return []
        if event in ("issues", "issue_comment") and repository not in self.issue_repositories:
            return []
        if event == "issues":
            return self.issue(repository, action, payload)
        if event == "issue_comment":
            return self.comment(repository, action, payload)
        if event == "push":
            return self.registry.base_tasks(repository, payload["ref"])
        if event == "status":
            return self.registry.commit_tasks(repository, payload["sha"])
        if event in (
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "pull_request_review_thread",
        ):
            numbers = [payload["pull_request"]["number"]]
        else:
            check = payload[event]
            numbers = [pr["number"] for pr in check.get("pull_requests", [])]
            by_head = self.registry.commit_tasks(repository, check.get("head_sha")) if check.get("head_sha") else []
            return sorted(set(by_head) | {key for key in (self.registry.task(repository, n) for n in numbers) if key})
        return sorted({key for key in (self.registry.task(repository, n) for n in numbers) if key})

    def tracked(self, key, labels):
        """An enrolled issue, or a task the registry still holds active after its label was removed."""
        return ENROLLMENT in labels or key in self.registry.active(key[0])

    def issue(self, repository, action, payload):
        """Issue events the Actions workflow would act on; display-label changes never wake a task."""
        issue = payload["issue"]
        label = (payload.get("label") or {}).get("name", "")
        if action not in ISSUE_ACTIONS or "pull_request" in issue or label.startswith(DISPLAY_LABELS):
            return []
        key = (repository, issue["number"])
        labels = {item["name"] for item in issue["labels"]}
        return [key] if label == ENROLLMENT or self.tracked(key, labels) else []

    def comment(self, repository, action, payload):
        """Human comments from the issue author or a writer on an enrolled issue, or a `/concurro` command."""
        issue, comment = payload["issue"], payload["comment"]
        user = comment["user"]
        if (
            action not in COMMENT_ACTIONS
            or "pull_request" in issue
            or user.get("type") == "Bot"
            or self.ours(user["login"])
        ):
            return []
        if user["login"] != issue["user"]["login"] and comment.get("author_association") not in WRITER_ASSOCIATIONS:
            return []
        key = (repository, issue["number"])
        labels = {item["name"] for item in issue["labels"]}
        command = comment["body"].startswith("/concurro ")
        return [key] if command or self.tracked(key, labels) else []


class Handler(BaseHTTPRequestHandler):
    """Deliveries, backoffice commands at `/actions` (internal network only) and a health endpoint."""

    def do_POST(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal():
            return self.reply(411, "length required")
        if self.path == "/actions":
            return self.action(int(length))
        if int(length) > BODY_LIMIT:
            return self.reply(413, "payload too large")
        body = self.rfile.read(int(length))
        headers = {
            name: self.headers.get(name, "") for name in ("X-Hub-Signature-256", "X-GitHub-Event", "X-GitHub-Delivery")
        }
        try:
            status, text = self.server.ingress.handle(headers, body)
        except Exception as error:
            log.warning("delivery failed: %s", type(error).__name__)
            status, text = 503, "unavailable"
        self.reply(status, text)

    def action(self, length):
        """One backoffice command; refused unless the service has a delegation key."""
        actions = self.server.actions
        if actions is None:
            return self.reply_json(503, {"error": "backoffice commands are not configured"})
        if length > ACTION_LIMIT:
            return self.reply_json(413, {"error": "payload too large"})
        body = self.rfile.read(length)
        try:
            status, payload = actions.handle(self.headers.get("Authorization", ""), body)
        except Exception as error:
            log.warning("backoffice command failed: %s", type(error).__name__)
            status, payload = 503, {"error": "unavailable"}
        self.reply_json(status, payload)

    def reply_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200, "ok") if self.path == "/health" else self.reply(404, "not found")

    def reply(self, status, text):
        data = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        log.debug("ingress: " + format, *args)


def server(ingress, host, port, actions=None):
    """An HTTP server for one ingress and, optionally, backoffice commands; the caller runs and stops it."""
    instance = ThreadingHTTPServer((host, port), Handler)
    instance.ingress = ingress
    instance.actions = actions
    return instance
