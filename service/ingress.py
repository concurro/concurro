"""Webhook ingress: verify, filter, de-duplicate and enqueue; never read task state or call the engine."""

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import time

ENROLLMENT = "concurro"
EVENTS = {"issues", "issue_comment", "pull_request", "pull_request_review", "check_suite", "check_run"}
# The Actions workflow's triggers and filters, kept for parity.
ISSUE_ACTIONS = {"opened", "labeled", "unlabeled", "edited", "reopened", "closed"}
DISPLAY_LABELS = ("concurro:stage:", "concurro:attention")
COMMENT_ACTIONS = {"created", "edited"}
WRITER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# GitHub caps webhook payloads at 25 MB.
BODY_LIMIT = 25_000_000

log = logging.getLogger("concurro.service")


class Ingress:
    """Map verified deliveries from enabled repositories and the App's own installations to tasks."""

    def __init__(self, secret, registry, installations, identities, clock=time.time):
        """`installations` maps each enabled repository to the App's installation ID on it."""
        self.secret, self.registry, self.clock = secret, registry, clock
        self.installations = dict(installations)
        self.repositories = {repository.lower(): repository for repository in installations}
        self.identities = frozenset(identities)

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
        if self.registry.seen(delivery):
            return 200, "duplicate"
        for key in keys:
            self.registry.enqueue(key, "webhook:" + event, self.clock())
        return 202, "accepted"

    def ours(self, login):
        """Whether a login is a Concurro identity."""
        return login in self.identities

    def keys(self, repository, event, payload):
        """The tasks a delivery concerns: issue events directly, PR and check events through recorded PRs."""
        if self.ours(payload["sender"]["login"]):
            return []
        action = payload.get("action")
        if event == "issues":
            return self.issue(repository, action, payload)
        if event == "issue_comment":
            return self.comment(repository, action, payload)
        if event in ("pull_request", "pull_request_review"):
            numbers = [payload["pull_request"]["number"]]
        else:
            numbers = [pr["number"] for pr in payload[event]["pull_requests"]]
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
        if (action not in COMMENT_ACTIONS or "pull_request" in issue or user.get("type") == "Bot"
                or self.ours(user["login"])):
            return []
        if user["login"] != issue["user"]["login"] and comment.get("author_association") not in WRITER_ASSOCIATIONS:
            return []
        key = (repository, issue["number"])
        labels = {item["name"] for item in issue["labels"]}
        command = comment["body"].startswith("/concurro ")
        return [key] if command or self.tracked(key, labels) else []


class Handler(BaseHTTPRequestHandler):
    """One POST endpoint for deliveries and a health endpoint."""

    ingress = None

    def do_POST(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal():
            return self.reply(411, "length required")
        if int(length) > BODY_LIMIT:
            return self.reply(413, "payload too large")
        body = self.rfile.read(int(length))
        headers = {name: self.headers.get(name, "") for name in
                   ("X-Hub-Signature-256", "X-GitHub-Event", "X-GitHub-Delivery")}
        try:
            status, text = self.ingress.handle(headers, body)
        except Exception as error:
            log.warning("delivery failed: %s", type(error).__name__)
            status, text = 503, "unavailable"
        self.reply(status, text)

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


def server(ingress, host, port):
    """An HTTP server for one ingress; the caller runs and stops it."""
    handler = type("IngressHandler", (Handler,), {"ingress": ingress})
    return ThreadingHTTPServer((host, port), handler)
