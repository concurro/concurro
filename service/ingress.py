"""Webhook ingress: verify, filter, de-duplicate and enqueue; never read task state or call the engine."""

import hashlib
import hmac
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import delegation
from commands import CommandKind, parse as parse_command
from identities import Actor
from sponsorship import hint as sponsor_hint
from vocab import STANDALONE_REVIEW

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
REVIEW_REQUESTS = {"review_requested": "requested", "review_request_removed": "removed"}
# The only PR lifecycle changes a standalone review acts on; pushes, retargets and edits change no assessment.
REVIEW_LIFECYCLE = {"closed", "reopened"}
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
        list_index=None,
        reviewer=None,
        review_targets=None,
    ):
        """`installations` maps each enabled repository to the App's installation ID on it.

        `identities` are Concurro's accounts, as actor descriptors or bare logins. With a
        `reviewer` account, a request for its review on a PR of a `review_targets` upstream
        enrolls that PR; the mapping gives each upstream's target.
        """
        self.secret, self.registry, self.clock = secret, registry, clock
        self.command_comment = command_comment
        self.list_index = list_index
        self.installations = dict(installations)
        self.repositories = {repository.lower(): repository for repository in installations}
        self.reviewer = Actor.parse(reviewer) if reviewer else None
        self.review_targets = dict(review_targets or {})
        self.identities = tuple(Actor.parse(identity) for identity in identities)
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
            # Recorded first, so the list index already finds the PR linked to its task.
            requested = self.review_request(delivery, repository, event, payload)
        except (KeyError, TypeError, AttributeError):
            return 400, "malformed payload"
        if self.list_index is not None:
            # Before the task filter, so Concurro's own label changes count; never part of the delivery's outcome.
            try:
                self.list_index.offer(delivery, event, repository, payload)
            except Exception as error:
                log.warning("list index offer failed: %s", type(error).__name__)
        if requested is not None:
            return requested
        try:
            keys = self.keys(repository, event, payload)
        except (KeyError, TypeError, AttributeError):
            return 400, "malformed payload"
        if not keys:
            return 202, "ignored"
        source = None
        if event == "issue_comment":
            previous = ((payload.get("changes") or {}).get("body") or {}).get("from")
            comment = payload["comment"]
            command = parse_command(comment.get("body") or "")
            prior = parse_command(previous or "")
            known = any(h["comment_id"] == comment.get("id") for h in self.registry.sponsor_sources(keys[0]))
            if (
                command
                and command.kind in (CommandKind.SPONSOR, CommandKind.FUNDING, CommandKind.WITHDRAW)
                or prior
                and prior.kind in (CommandKind.SPONSOR, CommandKind.FUNDING, CommandKind.WITHDRAW)
                or known
                or payload["action"] == "edited"
                and (command and command.kind == CommandKind.START or prior and prior.kind == CommandKind.START)
                or self.ours(comment["user"])
                and delegation.sponsorship_hint(comment.get("body") or "")
            ):
                source = sponsor_hint(comment, payload["action"], previous, (payload.get("sender") or {}).get("id"))
        observation = event == "issue_comment" and (source is not None or self.ours(payload["comment"]["user"]))
        expectation = None
        if observation:
            previous = ((payload.get("changes") or {}).get("body") or {}).get("from")
            expectation = comment_expectation(payload["comment"], payload["action"], previous)
        if not self.registry.receive(
            delivery,
            keys,
            event,
            ci=event in CI_EVENTS,
            observation=observation,
            expectation=expectation,
            sponsorship_hint=source,
        ):
            return 200, "duplicate"
        return 202, "accepted"

    def ours(self, user):
        """Whether a webhook `user` object, or a bare login, is a Concurro account."""
        user = {"login": user} if isinstance(user, str) else user
        return any(identity.rest(user) for identity in self.identities)

    def review_request(self, delivery, repository, event, payload):
        """Record a request for, or removal of, the reviewer account on a PR; None for any other delivery.

        Matched by the account's numeric ID. A request enrolls the PR as its own task unless a
        contribution task already owns it; receipt, task, evidence and wakeup commit together.
        """
        action = REVIEW_REQUESTS.get(payload.get("action")) if event == "pull_request" else None
        target = self.review_targets.get(repository)
        requested = payload.get("requested_reviewer") if action else None
        if (
            self.reviewer is None
            or self.reviewer.id is None
            or target is None
            or not isinstance(requested, dict)
            or requested.get("id") != self.reviewer.id
        ):
            return None
        pull, sender = payload["pull_request"], payload["sender"]
        request = {
            "action": action,
            "actor": {"login": sender["login"], "id": sender["id"]},
            "at": pull["updated_at"],
            "head": pull["head"]["sha"],
        }
        outcome = self.registry.review_event(
            delivery,
            (repository, pull["number"]),
            request,
            (target.upstream, target.fork) if action == "requested" else None,
        )
        if outcome == "duplicate":
            return 200, "duplicate"
        return 202, "accepted" if outcome == "accepted" else "ignored"

    def standalone(self, key):
        """Whether a task is a standalone PR review."""
        return key is not None and self.registry.kind(key) == STANDALONE_REVIEW

    def review_comment(self, key, action, payload):
        """A PR conversation comment that concerns a standalone review: only its commands.

        Delegated commands arrive as marked App comments, writer commands as `/concurro`
        comments; edits and deletions count when either revision was one.
        """
        comment = payload["comment"]
        body = comment.get("body") or ""
        previous = ((payload.get("changes") or {}).get("body") or {}).get("from") or ""
        if action not in COMMENT_ACTIONS:
            return []
        if self.ours(comment["user"]):
            marked = self.command_comment is not None and (
                self.command_comment(body)
                or self.command_comment(previous)
                or self.registry.observed_comment(key, comment.get("id"))
            )
            return [key] if marked else []
        if comment["user"].get("type") == "Bot":
            return []
        known = any(h["comment_id"] == comment.get("id") for h in self.registry.sponsor_sources(key))
        command = body.startswith("/concurro") or previous.startswith("/concurro")
        return [key] if command or known else []

    def keys(self, repository, event, payload):
        """The tasks a delivery concerns: issue events directly, PR and check events through recorded PRs.

        A standalone review wakes for its PR's commands, closure and reopening only: CI
        results, pushes, retargets and review activity are no input to it.
        """
        action = payload.get("action")
        if event == "issue_comment" and "pull_request" in payload["issue"]:
            key = (repository, payload["issue"]["number"])
            return self.review_comment(key, action, payload) if self.standalone(key) else []
        if event == "issue_comment" and self.command_comment is not None and self.ours(payload["comment"]["user"]):
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
        if self.ours(payload.get("sender") or {}):
            return []
        if event in ("issues", "issue_comment") and repository not in self.issue_repositories:
            return []
        if event == "issues":
            return self.issue(repository, action, payload)
        if event == "issue_comment":
            return self.comment(repository, action, payload)
        if event == "push":
            return self.contributions(self.registry.base_tasks(repository, payload["ref"]))
        if event == "status":
            return self.contributions(self.registry.commit_tasks(repository, payload["sha"]))
        if event in (
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "pull_request_review_thread",
        ):
            key = self.registry.task(repository, payload["pull_request"]["number"])
            if self.standalone(key):
                return [key] if event == "pull_request" and action in REVIEW_LIFECYCLE else []
            return [key] if key else []
        check = payload[event]
        numbers = [pr["number"] for pr in check.get("pull_requests", [])]
        by_head = self.registry.commit_tasks(repository, check.get("head_sha")) if check.get("head_sha") else []
        return self.contributions(
            set(by_head) | {key for key in (self.registry.task(repository, n) for n in numbers) if key}
        )

    def contributions(self, keys):
        """Only contribution tasks; a standalone review's verdict takes no CI or base-branch input."""
        return sorted(key for key in keys if not self.standalone(key))

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
        if action not in COMMENT_ACTIONS or "pull_request" in issue or user.get("type") == "Bot" or self.ours(user):
            return []
        command = parse_command(comment.get("body") or "")
        previous = ((payload.get("changes") or {}).get("body") or {}).get("from", "")
        prior = parse_command(previous or "")
        known = any(
            h["comment_id"] == comment.get("id") for h in self.registry.sponsor_sources((repository, issue["number"]))
        )
        sponsorship = (
            command
            and command.kind in (CommandKind.SPONSOR, CommandKind.FUNDING, CommandKind.WITHDRAW)
            or prior
            and prior.kind in (CommandKind.SPONSOR, CommandKind.FUNDING, CommandKind.WITHDRAW)
            or known
        )
        if (
            not sponsorship
            and not comment["body"].startswith("/concurro ")
            and user["login"] != issue["user"]["login"]
            and comment.get("author_association") not in WRITER_ASSOCIATIONS
        ):
            return []
        key = (repository, issue["number"])
        labels = {item["name"] for item in issue["labels"]}
        command = comment["body"].startswith("/concurro ")
        return [key] if command or sponsorship or self.tracked(key, labels) else []


class Handler(BaseHTTPRequestHandler):
    """Deliveries, backoffice commands at `/actions` (internal network only) and a health endpoint."""

    def do_POST(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal():
            return self.reply(411, "length required")
        if self.path in ("/actions", "/sponsorships"):
            return self.action(int(length))
        if self.path == "/lists" or self.path.startswith("/lists/"):
            return self.lists(int(length))
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
            method = actions.sponsorships if self.path == "/sponsorships" else actions.handle
            status, payload = method(self.headers.get("Authorization", ""), body)
        except Exception as error:
            log.warning("backoffice command failed: %s", type(error).__name__)
            status, payload = 503, {"error": "unavailable"}
        self.reply_json(status, payload)

    def lists(self, length):
        """One list index request from the backoffice; independent of command delegation."""
        lists = self.server.lists
        if lists is None:
            return self.reply_json(503, {"error": "the list index is not configured"})
        if length > ACTION_LIMIT:
            return self.reply_json(413, {"error": "payload too large"})
        body = self.rfile.read(length)
        try:
            status, payload = lists.handle(self.path, self.headers.get("Authorization", ""), body)
        except Exception as error:
            log.warning("list index request failed: %s", type(error).__name__)
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


def server(ingress, host, port, actions=None, lists=None):
    """An HTTP server for one ingress and, optionally, backoffice commands and list index requests."""
    instance = ThreadingHTTPServer((host, port), Handler)
    instance.ingress = ingress
    instance.actions = actions
    instance.lists = lists
    return instance
