"""The only GitHub writer: fenced by ownership generation, one critical section per task."""

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from copy import deepcopy

from github import GitHub
from policy import task_branch
from reviews import FINDING_MARKER

from .admission import (
    BRANCH as BRANCH,
    DISPATCH as DISPATCH,
    EDITABLE as EDITABLE,
    EXECUTOR_WORKFLOW,
    ISSUE_COMMENT as ISSUE_COMMENT,
    NUMBER as NUMBER,
    OID as OID,
    PR_BODIES as PR_BODIES,
    PULL as PULL,
    QUERY as QUERY,
    RUN_BRANCH_PREFIX,
    WRITES as WRITES,
    allowed as allowed,
    allowed_target as allowed_target,
    destination as destination,
    ready_pull as ready_pull,
    resolved_thread as resolved_thread,
    runner_write as runner_write,
    text_blob as text_blob,
)
from .errors import OwnershipLost, Unresolved
from .settlement import (
    Rejected as Rejected,
    TreeUnavailable as TreeUnavailable,
    missing_tree as missing_tree,
    rejection as rejection,
    settle as settle,
)

LEASE_SECONDS = 15 * 60
TREE_RETRY_DELAYS = (1, 2)
WRITE_INTERVAL = 1.0


class Disallowed(RuntimeError):
    """The engine attempted a write that has no reconstruction rule."""


def request_digest(method, path, body):
    """Audit digest of one request; it never identifies an occurrence."""
    return hashlib.sha256(json.dumps([method, path, body], sort_keys=True).encode()).hexdigest()


class MutationGateway:
    """Admit, send and settle every write; grant and end ownership in the same per-task section."""

    def __init__(self, registry, writer, reader, bot_login=None, *, clock=time.monotonic, sleep=time.sleep):
        """`writer(repository)` and `reader(repository)` return that repository's write and read transports.

        A writer's `send(method, path, body)` returns a `transport.Response`; raising means the
        outcome is unknown. The reader resolves each task's issue node ID and verifies PR and
        thread bindings; without `bot_login`, replies and thread resolutions are refused.
        `clock` is monotonic; `sleep` serves write pacing and commit retry backoff.
        """
        self.registry, self.writer, self.reader, self.bot_login = registry, writer, reader, bot_login
        self.write_clock, self.write_sleep = clock, sleep
        self.write_lock, self.next_write = threading.Lock(), 0.0
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
            self.pulls[identity] = (
                ours
                and self.bot_login is not None
                and (pr.get("user") or {}).get("login") == self.bot_login
                and (head.get("repo") or {}).get("full_name", "").lower() == fork.lower()
            )
        return self.pulls[identity]

    def ours(self, login, body):
        """Whether a comment is the App's own finding comment (GraphQL names a bot without `[bot]`)."""
        return (
            self.bot_login is not None
            and login in (self.bot_login, self.bot_login.removesuffix("[bot]"))
            and isinstance(body, str)
            and body.startswith(FINDING_MARKER)
        )

    def _graphql_node(self, repository, query, node_id):
        """Fetch one node using the caller's binding query."""
        return (
            (
                self.reader(repository).request("POST", "graphql", {"query": query, "variables": {"id": node_id}}) or {}
            ).get("data")
            or {}
        ).get("node") or {}

    def bound(self, key, path, body):
        """PR writes only for the task's own PR; replies and resolutions only in the App's own finding threads;
        issue comment edits only on the App's own marked comments on the task's issue."""
        binding = self.binding(key)
        repository = destination(key, binding, path)
        ready = ready_pull(body) if path == "graphql" else None
        if ready is not None:
            query = "query($id:ID!) { node(id:$id) { ... on PullRequest { number repository { nameWithOwner } } } }"
            node = self._graphql_node(repository, query, ready)
            return (
                (node.get("repository") or {}).get("nameWithOwner", "").lower() == repository.lower()
                and type(node.get("number")) is int
                and self.owned_pull(key, node["number"])
            )
        thread = resolved_thread(body) if path == "graphql" else None
        if thread is not None:
            query = (
                "query($id:ID!) { node(id:$id) { ... on PullRequestReviewThread { "
                "pullRequest { number repository { nameWithOwner } } "
                "comments(first:1) { nodes { author { login } body } } } } }"
            )
            node = self._graphql_node(repository, query, thread)
            pr = node.get("pullRequest") or {}
            first = (node.get("comments") or {}).get("nodes") or [{}]
            return (
                (pr.get("repository") or {}).get("nameWithOwner", "").lower() == repository.lower()
                and isinstance(pr.get("number"), int)
                and self.owned_pull(key, pr["number"])
                and self.ours((first[0].get("author") or {}).get("login"), first[0].get("body"))
            )
        prefix = f"repos/{key[0]}/"
        edit = ISSUE_COMMENT.fullmatch(path[len(prefix) :]) if path.startswith(prefix) else None
        if edit:
            comment = self.reader(key[0]).request("GET", path) or {}
            return (
                self.bot_login is not None
                and (comment.get("user") or {}).get("login") == self.bot_login
                and str(comment.get("body", "")).startswith(EDITABLE)
                and str(comment.get("issue_url", "")).endswith(f"/issues/{key[1]}")
            )
        prefix = f"repos/{repository}/"
        match = PULL.fullmatch(path[len(prefix) :]) if path.startswith(prefix + "pulls/") else None
        if match is None:
            return True
        if not self.owned_pull(key, int(match["pull"])):
            return False
        if match["comment"] is None:
            return True
        comment = self.reader(repository).request("GET", f"{prefix}pulls/comments/{match['comment']}")
        return self.ours((comment.get("user") or {}).get("login"), comment.get("body")) and str(
            comment.get("pull_request_url", "")
        ).endswith(f"/pulls/{match['pull']}")

    def _replay_stop(self, key, generation):
        """Repeat the first stop recorded for this ownership generation."""
        if (key, generation) in self.stops:
            self.halt(key, generation, self.stops[(key, generation)])

    def check_owner(self, key, generation):
        """Refuse model work and stale results once task ownership or mutation authority is lost."""
        with self.section(key):
            self._replay_stop(key, generation)
            owner = self.registry.current(key)
            if not owner or not owner.owns(generation):
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
            if not owner or not owner.owns(generation):
                self.halt(key, generation, OwnershipLost("runner task ownership changed"))
            self.registry.register_runner(key, generation, session, binding.config_sha)
        root, ref = f"repos/{binding.fork}/", RUN_BRANCH_PREFIX + session
        refs = self.reader(binding.fork).request("GET", root + "git/matching-refs/heads/" + ref)
        found = [item for item in refs if item["ref"] == "refs/heads/" + ref]
        if found:
            if found[0]["object"]["sha"] != binding.config_sha:
                raise Disallowed("session ref moved outside the controller")
        else:
            self.mutate(
                key, generation, "POST", root + "git/refs", {"ref": "refs/heads/" + ref, "sha": binding.config_sha}
            )
        self.mutate(
            key,
            generation,
            "POST",
            root + f"actions/workflows/{EXECUTOR_WORKFLOW}/dispatches",
            {"ref": ref, "inputs": {"session": session}},
        )

    def runner_allowed(self, key, generation, method, path, body):
        """Authorize session writes only against a prior durable record for this task."""
        session = runner_write(path, method, body)
        if session is None:
            return False
        record = self.registry.runner_session(session)
        target = self.registry.binding(key)
        if (
            not record
            or not target
            or (record["repository"], record["issue"]) != key
            or record["removed"]
            or destination(key, target, path) != target.fork
        ):
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

    def _admitted(self, key, generation, method, path, body, binding):
        """Check runner, bound-target, then unbound issue-only admission in that order."""
        if self.runner_allowed(key, generation, method, path, body):
            return True
        if binding:
            return allowed_target(key, binding, method, path, body)
        return (path.startswith(f"repos/{key[0]}/issues/") or path == f"repos/{key[0]}/labels") and allowed(
            key, method, path, body
        )

    def mutate(self, key, generation, method, path, body=None):
        """Send one allow-listed write as a new occurrence under the current generation; never resend.

        A task's writes are sent one at a time: a later write waits for the earlier one to
        settle, then rechecks ownership and unresolved writes before its own admission.
        """
        binding = self.binding(key)
        admitted = self._admitted(key, generation, method, path, body, binding)
        if not admitted or not self.bound(key, path, body):
            raise Disallowed("write is not on the gateway allow-list")
        # One pacer across tasks and repositories; waiting holds no task ownership lock.
        with self.write_lock:
            delay = self.next_write - self.write_clock()
            if delay > 0:
                self.write_sleep(delay)
            try:
                return self._send(key, generation, method, path, body, binding)
            finally:
                self.next_write = self.write_clock() + WRITE_INTERVAL

    def _send(self, key, generation, method, path, body, binding):
        """Recheck ownership, admit and settle one write while holding the shared write slot."""
        with self.section(key) as condition:
            condition.wait_for(lambda: key not in self.sending)
            self._replay_stop(key, generation)
            current = self.registry.current(key)
            if not current or not current.owns(generation):
                self.halt(key, generation, OwnershipLost(f"generation {generation} no longer owns {key[0]}#{key[1]}"))
            # An in-flight record with no sender was abandoned mid-send: its outcome is unknown.
            if self.registry.unresolved(key) or self.registry.in_flight(key):
                self.halt(key, generation, Unresolved(f"{key[0]}#{key[1]} has an unresolved write"))
            intent = self.registry.admit(key, generation, method, path, request_digest(method, path, body))
            self.sending.add(key)
        try:
            try:
                result, payload, error = settle(
                    path,
                    self.writer(destination(key, binding, path)).send(method, path, body),
                    self.registry.clock(),
                    body,
                )
            except Exception:
                # Any unclassified send failure may have applied the write; never assume rejection.
                result, payload, error = "unknown", None, None
            self.registry.settle(intent, result)
        finally:
            with self.section(key) as condition:
                self.sending.discard(key)
                condition.notify_all()
        if result == "unknown":
            self.halt(
                key, generation, Unresolved(f"write {intent} has an unknown outcome; operator resolution required")
            )
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
        return self.write(path, method, data)

    def create_tree(self, base_tree, entries, uploaded_blobs=()):
        """Retry definite 422s only for trees referencing blobs uploaded by this publication."""
        body = {"base_tree": base_tree, "tree": deepcopy(entries)}
        uploaded = {sha for sha in uploaded_blobs if isinstance(sha, str) and OID.fullmatch(sha)}
        recent = any(
            isinstance(entry, dict)
            and entry.get("type") == "blob"
            and isinstance(entry.get("sha"), str)
            and entry["sha"] in uploaded
            for entry in body["tree"]
        )
        return self.write(self.root + "/git/trees", "POST", body, retry_tree=recent)

    def write(self, path, method, data, retry_tree=False):
        """Pace and fence each attempt; only definite, eligible rejections get a short retry."""
        for attempt in range(len(TREE_RETRY_DELAYS) + 1):
            try:
                return self.gateway.mutate(self.key, self.generation, method, path, data)
            except Rejected as error:
                retry = isinstance(error, TreeUnavailable) or retry_tree and error.status == 422
                if not retry or attempt == len(TREE_RETRY_DELAYS):
                    raise
                # Each definite rejection is settled; the next occurrence rechecks ownership.
                self.gateway.write_sleep(TREE_RETRY_DELAYS[attempt])

    def archive(self, sha):
        """Download a pinned source archive through the read transport."""
        return self.reader.archive(self.root + "/tarball/" + sha)
