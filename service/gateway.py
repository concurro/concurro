"""The only GitHub writer: fenced by ownership generation, one critical section per task."""

from contextlib import contextmanager
import hashlib
import json
import logging
import re
import threading

from checkpoint_health import MARKER as HEALTH_MARKER
from checkpoints import MARKER as STATUS_MARKER
from conversation import MARKER as EVENT_MARKER
from github import GitHub, NotFound, RESOLVE_THREAD, MARK_READY, Transient
from reviews import FINDING_MARKER
from policy import task_branch

from .errors import OwnershipLost, Unresolved
from .transport import throttle

log = logging.getLogger("concurro.service")

LEASE_SECONDS = 15 * 60
NUMBER = r"[1-9][0-9]*"
# Writes whose results the engine reconstructs from GitHub on its next tick; any other write is
# refused before admission. A named `issue` group must equal the task's own issue.
WRITES = (
    ("POST", rf"issues/(?P<issue>{NUMBER})/labels"),
    ("DELETE", rf"issues/(?P<issue>{NUMBER})/labels/[^/?#]+"),
    ("POST", r"labels"),
    ("POST", rf"issues/(?P<issue>{NUMBER})/comments"),
    ("PATCH", rf"issues/comments/{NUMBER}"),
    ("POST", rf"issues/(?P<issue>{NUMBER})/artifacts"),
    ("PATCH", rf"issues/(?P<issue>{NUMBER})/artifacts/{NUMBER}"),
    ("POST", r"git/refs"),
    ("POST", r"git/trees"),
    ("POST", r"git/commits"),
    ("PATCH", r"git/refs/heads/[A-Za-z0-9._/-]{1,100}"),
    ("POST", r"pulls"),
    ("POST", rf"pulls/{NUMBER}/reviews"),
    ("POST", rf"pulls/{NUMBER}/comments"),
    ("POST", rf"pulls/{NUMBER}/comments/{NUMBER}/replies"),
)
# A runner session's start: the agent event type, carrying only the session's ID.
DISPATCH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The App only ever comments on its PRs; it never approves or requests changes.
PR_BODIES = ((re.compile(rf"pulls/{NUMBER}/reviews"), {"commit_id", "event", "body", "comments"}),
             (re.compile(rf"pulls/{NUMBER}/comments"), {"commit_id", "path", "subject_type", "body"}),
             (re.compile(rf"pulls/{NUMBER}/comments/{NUMBER}/replies"), {"body"}))
ISSUE_COMMENT = re.compile(rf"issues/comments/(?P<comment>{NUMBER})")
# The App's issue comments it edits: the status comment, its health notice and Input needed requests.
EDITABLE = (STATUS_MARKER, HEALTH_MARKER, EVENT_MARKER)
PULL = re.compile(rf"pulls/(?P<pull>{NUMBER})(?:/comments/(?P<comment>{NUMBER})/replies|/.*)?")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
OID = re.compile(r"[0-9a-f]{40}")
QUERY = re.compile(r"\s*query\b")


class Disallowed(RuntimeError):
    """The engine attempted a write that has no reconstruction rule."""


class Rejected(RuntimeError):
    """GitHub rejected a write; it did not take effect."""


def allowed(key, method, path, body):
    """Whether a write's shape is on the allow-list for this task's repository and issue.

    PR writes and thread resolutions are further bound to the task's own PR and the App's own
    threads by `MutationGateway.bound`, which needs reads.
    """
    if path == "graphql":
        return method == "POST" and (resolved_thread(body) is not None or ready_pull(body) is not None)
    prefix = f"repos/{key[0]}/"
    if not path.startswith(prefix):
        return False
    relative = path[len(prefix):]
    for verb, pattern in WRITES:
        match = re.fullmatch(pattern, relative) if verb == method else None
        if match:
            if "issue" in match.groupdict() and int(match["issue"]) != key[1]:
                return False
            if relative == "git/refs":
                return (isinstance(body, dict) and set(body) == {"ref", "sha"}
                        and body["ref"] == "refs/heads/" + task_branch(*key)
                        and isinstance(body["sha"], str) and OID.fullmatch(body["sha"]) is not None)
            # Branch updates are fast-forward only.
            if relative.startswith("git/refs/"):
                return isinstance(body, dict) and set(body) == {"sha", "force"} and body["force"] is False
            for shape, fields in PR_BODIES:
                if shape.fullmatch(relative):
                    return (isinstance(body, dict) and set(body) == fields
                            and body.get("event", "COMMENT") == "COMMENT"
                            and body.get("subject_type", "file") == "file")
            return True
    return False


def destination(key, binding, path):
    """The token repository for a request; GraphQL PR mutations use upstream."""
    if path == "graphql":
        return binding.upstream if binding else key[0]
    parts = path.split("/", 3)
    return "/".join(parts[1:3]) if len(parts) == 4 and parts[0] == "repos" else None


def allowed_target(key, binding, method, path, body):
    """Admit only issue writes, fork publication, and upstream COMMENT reviews for a bound target."""
    if path == "graphql":
        return method == "POST" and (resolved_thread(body) is not None or ready_pull(body) is not None)
    repository = destination(key, binding, path)
    relative = path.split("/", 3)[-1]
    if relative.startswith(("issues/", "labels")):
        return repository == key[0] and allowed(key, method, path, body)
    branch = task_branch(*key)
    if relative.startswith("git/"):
        if repository != binding.fork:
            return False
        if method == "POST" and relative in ("git/trees", "git/commits"):
            return isinstance(body, dict)
        if method == "POST" and relative == "git/refs":
            return (isinstance(body, dict) and set(body) == {"ref", "sha"}
                    and body["ref"] == "refs/heads/" + branch
                    and isinstance(body["sha"], str) and OID.fullmatch(body["sha"]) is not None)
        return (method == "PATCH" and relative == "git/refs/heads/" + branch and isinstance(body, dict)
                and set(body) == {"sha", "force"} and body["force"] is False
                and isinstance(body["sha"], str) and OID.fullmatch(body["sha"]) is not None)
    if repository != binding.upstream:
        return False
    if method == "POST" and relative == "pulls":
        return (isinstance(body, dict) and set(body) == {"head", "head_repo", "base", "title", "body", "draft", "maintainer_can_modify"}
                and body["head"] == binding.fork.split("/")[0] + ":" + branch
                and body["head_repo"] == binding.fork.split("/")[1] and body["draft"] is True
                and body["maintainer_can_modify"] is False
                and all(isinstance(body[k], str) for k in ("base", "title", "body"))
                and BRANCH.fullmatch(body["base"]) is not None)
    return (relative.startswith("pulls/") and method == "POST"
            and allowed((repository, key[1]), method, path, body))


def runner_write(path, method, body):
    """The session ID of an exact session-ref or workflow dispatch request, or None."""
    relative = path.split("/", 3)[-1]
    if method == "POST" and relative == "git/refs" and isinstance(body, dict) and set(body) == {"ref", "sha"}:
        ref = body["ref"]
        prefix = "refs/heads/concurro/runs/"
        return ref[len(prefix):] if isinstance(ref, str) and ref.startswith(prefix) and DISPATCH.fullmatch(ref[len(prefix):]) else None
    if method == "DELETE" and relative.startswith("git/refs/heads/concurro/runs/") and body is None:
        value = relative.removeprefix("git/refs/heads/concurro/runs/")
        return value if DISPATCH.fullmatch(value) else None
    if (method == "POST" and relative == "actions/workflows/concurro-executor.yml/dispatches"
            and isinstance(body, dict) and set(body) == {"ref", "inputs"}
            and isinstance(body["inputs"], dict) and set(body["inputs"]) == {"session"}):
        session = body["inputs"]["session"]
        if isinstance(session, str) and DISPATCH.fullmatch(session) and body["ref"] == "concurro/runs/" + session:
            return session
    return None


def ready_pull(body):
    """The PR ID of the exact draft-to-ready mutation, or None."""
    if not isinstance(body, dict) or set(body) != {"query", "variables"} or body["query"] != MARK_READY:
        return None
    variables = body["variables"]
    value = variables.get("input") if isinstance(variables, dict) and set(variables) == {"input"} else None
    if not isinstance(value, dict) or set(value) != {"pullRequestId"}:
        return None
    return value["pullRequestId"] if isinstance(value["pullRequestId"], str) and value["pullRequestId"] else None


def resolved_thread(body):
    """The thread ID of an exact engine `resolveReviewThread` request, or None for anything else."""
    if not isinstance(body, dict) or set(body) != {"query", "variables"} or body["query"] != RESOLVE_THREAD:
        return None
    variables = body["variables"]
    thread = variables.get("input") if isinstance(variables, dict) and set(variables) == {"input"} else None
    if not isinstance(thread, dict) or set(thread) != {"threadId"} or not isinstance(thread["threadId"], str):
        return None
    return thread["threadId"]


def request_digest(method, path, body):
    """Audit digest of one request; it never identifies an occurrence."""
    return hashlib.sha256(json.dumps([method, path, body], sort_keys=True).encode()).hexdigest()


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
            if ready_pull(body) is not None:
                pr = (data.get("markPullRequestReadyForReview") or {}).get("pullRequest") or {}
                if pr.get("id") == ready_pull(body) and pr.get("isDraft") is False:
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
        return "failed", None, Rejected(f"GitHub rejected the write (HTTP {status}): {detail}")
    return "unknown", None, None


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
                detail = " ".join(f"{key}={value}" for key in ("resource", "field", "code", "message")
                                  if (value := clean(error.get(key))))
            else:
                detail = clean(error)
            if detail:
                parts.append(detail)
        if len(errors) > 5:
            parts.append("additional errors omitted")
    return "; ".join(parts)[:2000]


class MutationGateway:
    """Admit, send and settle every write; grant and end ownership in the same per-task section."""

    def __init__(self, registry, writer, reader, bot_login=None):
        """`writer(repository)` and `reader(repository)` return that repository's write and read transports.

        A writer's `send(method, path, body)` returns a `transport.Response`; raising means the
        outcome is unknown. The reader resolves each task's issue node ID and verifies PR and
        thread bindings; without `bot_login`, replies and thread resolutions are refused.
        """
        self.registry, self.writer, self.reader, self.bot_login = registry, writer, reader, bot_login
        self.pulls = {}
        self.guard = threading.Lock()
        self.sections = {}
        self.sending = set()
        self.stops = {}

    @contextmanager
    def section(self, key):
        """Serialize ownership changes and mutation admission for one task; yields its condition."""
        with self.guard:
            condition = self.sections.setdefault(key, threading.Condition())
        with condition:
            yield condition

    def binding(self, key):
        """The immutable destination pair recorded for this task."""
        return self.registry.binding(key)

    def owned_pull(self, key, number):
        """Verify the PR's head repository and task branch, independently of a later registry link."""
        binding = self.binding(key)
        repository = binding.upstream if binding else key[0]
        fork = binding.fork if binding else key[0]
        recorded = self.registry.pull(key)
        if recorded and (recorded.number != number or recorded.repository.lower() != repository.lower()):
            return False
        identity = (key, repository, number)
        if identity not in self.pulls:
            pr = self.reader(repository).request("GET", f"repos/{repository}/pulls/{number}")
            head = pr.get("head") or {}
            branch = head.get("ref")
            ours = binding is not None and branch == task_branch(*key)
            self.pulls[identity] = (ours and self.bot_login is not None
                and (pr.get("user") or {}).get("login") == self.bot_login
                and (head.get("repo") or {}).get("full_name", "").lower() == fork.lower())
        return self.pulls[identity]

    def ours(self, login, body):
        """Whether a comment is the App's own finding comment (GraphQL names a bot without `[bot]`)."""
        return (self.bot_login is not None and login in (self.bot_login, self.bot_login.removesuffix("[bot]"))
                and isinstance(body, str) and body.startswith(FINDING_MARKER))

    def bound(self, key, path, body):
        """PR writes only for the task's own PR; replies and resolutions only in the App's own finding threads;
        issue comment edits only on the App's own marked comments on the task's issue."""
        binding = self.binding(key)
        repository = destination(key, binding, path)
        ready = ready_pull(body) if path == "graphql" else None
        if ready is not None:
            query = "query($id:ID!) { node(id:$id) { ... on PullRequest { number repository { nameWithOwner } } } }"
            node = ((self.reader(repository).request("POST", "graphql", {"query": query, "variables": {"id": ready}})
                     or {}).get("data") or {}).get("node") or {}
            return ((node.get("repository") or {}).get("nameWithOwner", "").lower() == repository.lower()
                    and type(node.get("number")) is int and self.owned_pull(key, node["number"]))
        thread = resolved_thread(body) if path == "graphql" else None
        if thread is not None:
            query = ("query($id:ID!) { node(id:$id) { ... on PullRequestReviewThread { "
                     "pullRequest { number repository { nameWithOwner } } "
                     "comments(first:1) { nodes { author { login } body } } } } }")
            node = ((self.reader(repository).request("POST", "graphql", {"query": query, "variables": {"id": thread}})
                     or {}).get("data") or {}).get("node") or {}
            pr = node.get("pullRequest") or {}
            first = (node.get("comments") or {}).get("nodes") or [{}]
            return ((pr.get("repository") or {}).get("nameWithOwner", "").lower() == repository.lower() and isinstance(pr.get("number"), int)
                    and self.owned_pull(key, pr["number"])
                    and self.ours((first[0].get("author") or {}).get("login"), first[0].get("body")))
        prefix = f"repos/{key[0]}/"
        edit = ISSUE_COMMENT.fullmatch(path[len(prefix):]) if path.startswith(prefix) else None
        if edit:
            comment = self.reader(key[0]).request("GET", path) or {}
            return (self.bot_login is not None and (comment.get("user") or {}).get("login") == self.bot_login
                    and str(comment.get("body", "")).startswith(EDITABLE)
                    and str(comment.get("issue_url", "")).endswith(f"/issues/{key[1]}"))
        prefix = f"repos/{repository}/"
        match = PULL.fullmatch(path[len(prefix):]) if path.startswith(prefix + "pulls/") else None
        if match is None:
            return True
        if not self.owned_pull(key, int(match["pull"])):
            return False
        if match["comment"] is None:
            return True
        comment = self.reader(repository).request("GET", f"{prefix}pulls/comments/{match['comment']}")
        return (self.ours((comment.get("user") or {}).get("login"), comment.get("body"))
                and str(comment.get("pull_request_url", "")).endswith(f"/pulls/{match['pull']}"))

    def check_owner(self, key, generation):
        """Refuse model work and stale results once task ownership or mutation authority is lost."""
        with self.section(key):
            if (key, generation) in self.stops:
                self.halt(key, generation, self.stops[(key, generation)])
            owner = self.registry.current(key)
            if not owner or not owner.live or owner.generation != generation:
                self.halt(key, generation, OwnershipLost("runner task ownership changed"))
            if self.registry.unresolved(key):
                self.halt(key, generation, Unresolved("runner task has an unresolved mutation"))

    def dispatch_runner(self, key, generation, session):
        """Reserve the config-pinned fork ref, then dispatch its exact caller workflow."""
        if not isinstance(session, str) or not DISPATCH.fullmatch(session):
            raise Disallowed("invalid runner session")
        binding = self.registry.binding(key)
        if binding is None:
            raise Disallowed("runner task has no target binding")
        with self.section(key):
            owner = self.registry.current(key)
            if not owner or not owner.live or owner.generation != generation:
                self.halt(key, generation, OwnershipLost("runner task ownership changed"))
            self.registry.register_runner(key, generation, session, binding.config_sha)
        root, ref = f"repos/{binding.fork}/", "concurro/runs/" + session
        refs = self.reader(binding.fork).request("GET", root + "git/matching-refs/heads/" + ref)
        found = [item for item in refs if item["ref"] == "refs/heads/" + ref]
        if found:
            if found[0]["object"]["sha"] != binding.config_sha:
                raise Disallowed("session ref moved outside the controller")
        else:
            self.mutate(key, generation, "POST", root + "git/refs", {"ref": "refs/heads/" + ref, "sha": binding.config_sha})
        self.mutate(key, generation, "POST", root + "actions/workflows/concurro-executor.yml/dispatches",
                    {"ref": ref, "inputs": {"session": session}})

    def runner_allowed(self, key, generation, method, path, body):
        """Authorize session writes only against a prior durable record for this task."""
        session = runner_write(path, method, body)
        if session is None:
            return False
        record = self.registry.runner_session(session)
        target = self.registry.binding(key)
        if (not record or not target or (record["repository"], record["issue"]) != key or record["removed"]
                or destination(key, target, path) != target.fork):
            return False
        if method == "DELETE":
            return True
        if record["generation"] != generation:
            return False
        return body.get("sha", record["config_sha"]) == record["config_sha"]

    def acquire(self, key, worker, lease_seconds=LEASE_SECONDS, consume=True):
        """Grant a new generation only with no live owner, nothing in flight and nothing unresolved.

        A tick consumes the task's queued work; an administrative grant (`consume=False`) keeps it.
        """
        with self.section(key):
            current = self.registry.current(key)
            if current and current.live and current.lease_until > self.registry.clock():
                return None
            if self.registry.in_flight(key) or self.registry.unresolved(key):
                return None
            return self.registry.grant(key, worker, lease_seconds, consume)

    def renew(self, key, generation, lease_seconds=LEASE_SECONDS):
        """Extend a live generation's lease; liveness only, never authority over a newer owner."""
        with self.section(key):
            return self.registry.renew(key, generation, lease_seconds)

    def release(self, key, generation, outcome):
        """End that generation only; a stale generation records nothing."""
        with self.section(key):
            return self.registry.end(key, generation, outcome)

    def stop(self, key, generation):
        """The control exception already raised to this generation, if any.

        The worker classifies by this record, so a cleanup error that replaces the
        propagating exception cannot turn a stopped tick into an ordinary failure.
        """
        return self.stops.get((key, generation))

    def halt(self, key, generation, stop):
        """Remember the first stop for this generation and raise it; later calls repeat it."""
        stop = self.stops.setdefault((key, generation), stop)
        raise type(stop)(*stop.args)

    def mutate(self, key, generation, method, path, body=None):
        """Send one allow-listed write as a new occurrence under the current generation; never resend.

        A task's writes are sent one at a time: a later write waits for the earlier one to
        settle, then rechecks ownership and unresolved writes before its own admission.
        """
        binding = self.binding(key)
        admitted = (self.runner_allowed(key, generation, method, path, body)
                    or (allowed_target(key, binding, method, path, body) if binding else
                        (path.startswith(f"repos/{key[0]}/issues/") or path == f"repos/{key[0]}/labels")
                        and allowed(key, method, path, body)))
        if not admitted or not self.bound(key, path, body):
            raise Disallowed("write is not on the gateway allow-list")
        with self.section(key) as condition:
            condition.wait_for(lambda: key not in self.sending)
            if (key, generation) in self.stops:
                self.halt(key, generation, self.stops[(key, generation)])
            current = self.registry.current(key)
            if not current or not current.live or current.generation != generation:
                self.halt(key, generation, OwnershipLost(f"generation {generation} no longer owns {key[0]}#{key[1]}"))
            # An in-flight record with no sender was abandoned mid-send: its outcome is unknown.
            if self.registry.unresolved(key) or self.registry.in_flight(key):
                self.halt(key, generation, Unresolved(f"{key[0]}#{key[1]} has an unresolved write"))
            intent = self.registry.admit(key, generation, method, path, request_digest(method, path, body))
            self.sending.add(key)
        try:
            try:
                result, payload, error = settle(path, self.writer(destination(key, binding, path)).send(method, path, body),
                                                self.registry.clock(), body)
            except Exception:
                result, payload, error = "unknown", None, None
            self.registry.settle(intent, result)
        finally:
            with self.section(key) as condition:
                self.sending.discard(key)
                condition.notify_all()
        if result == "unknown":
            self.halt(key, generation, Unresolved(f"write {intent} has an unknown outcome; operator resolution required"))
        if error:
            raise error
        return payload


class GatewayGitHub(GitHub):
    """The engine's GitHub client for one owned task: direct reads, writes only through the gateway."""

    def __init__(self, repository, issue, generation, gateway, reader, task_key=None):
        """`reader` holds read access only: `request(method, path, data)` and `archive(path)`."""
        super().__init__(repository)
        self.key, self.generation = task_key or (repository, issue), generation
        self.gateway, self.reader = gateway, reader

    def api(self, path, method="GET", data=None):
        """Read directly; route every other request, GraphQL mutations included, to the gateway."""
        if method == "GET" or path == "graphql" and isinstance(data, dict) and QUERY.match(data.get("query") or ""):
            return self.reader.request(method, path, data)
        return self.gateway.mutate(self.key, self.generation, method, path, data)

    def archive(self, sha):
        """Download a pinned source archive through the read transport."""
        return self.reader.archive(self.root + "/tarball/" + sha)
