"""The only GitHub writer: fenced by ownership generation, one critical section per task."""

from contextlib import contextmanager
import hashlib
import json
import re
import threading

from github import GitHub, LINKED_BRANCH, NotFound, Transient

from .errors import OwnershipLost, Unresolved
from .transport import throttle

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
    ("POST", r"git/trees"),
    ("POST", r"git/commits"),
    ("PATCH", r"git/refs/heads/[A-Za-z0-9._/-]{1,100}"),
    ("POST", r"pulls"),
    ("PUT", rf"pulls/{NUMBER}/merge"),
)
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
OID = re.compile(r"[0-9a-f]{40}")
QUERY = re.compile(r"\s*query\b")


class Disallowed(RuntimeError):
    """The engine attempted a write that has no reconstruction rule."""


class Rejected(RuntimeError):
    """GitHub rejected a write; it did not take effect."""


def allowed(key, method, path, body, issue_node=None):
    """Whether a write is on the allow-list for this task's repository and issue (`issue_node` for GraphQL)."""
    if path == "graphql":
        return method == "POST" and issue_node is not None and linked_branch(body) == issue_node
    prefix = f"repos/{key[0]}/"
    if not path.startswith(prefix):
        return False
    relative = path[len(prefix):]
    for verb, pattern in WRITES:
        match = re.fullmatch(pattern, relative) if verb == method else None
        if match:
            if "issue" in match.groupdict() and int(match["issue"]) != key[1]:
                return False
            # Branch updates are fast-forward only.
            return not relative.startswith("git/refs/") or (
                isinstance(body, dict) and set(body) == {"sha", "force"} and body["force"] is False)
    return False


def linked_branch(body):
    """The issue node ID of an exact engine `createLinkedBranch` request, or None for anything else."""
    if not isinstance(body, dict) or set(body) != {"query", "variables"} or body["query"] != LINKED_BRANCH:
        return None
    variables = body["variables"]
    branch = variables.get("input") if isinstance(variables, dict) and set(variables) == {"input"} else None
    if (not isinstance(branch, dict) or set(branch) != {"issueId", "name", "oid"}
            or not all(isinstance(v, str) for v in branch.values())
            or not BRANCH.fullmatch(branch["name"]) or not OID.fullmatch(branch["oid"])):
        return None
    return branch["issueId"]


def request_digest(method, path, body):
    """Audit digest of one request; it never identifies an occurrence."""
    return hashlib.sha256(json.dumps([method, path, body], sort_keys=True).encode()).hexdigest()


def settle(path, response, now):
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
        if status == 200 and isinstance(data, dict) and data.get("createLinkedBranch") is not None:
            return "done", payload, None
        return "unknown", None, None
    if 200 <= status < 300:
        try:
            return "done", response.json(), None
        except ValueError:
            return "done", None, Transient("response_lost")
    if status == 404:
        return "failed", None, NotFound(f"GitHub rejected the write (HTTP {status})")
    if 400 <= status < 500 and status not in (408, 429):
        return "failed", None, Rejected(f"GitHub rejected the write (HTTP {status})")
    return "unknown", None, None


class MutationGateway:
    """Admit, send and settle every write; grant and end ownership in the same per-task section."""

    def __init__(self, registry, writer, reader):
        """`writer(repository)` and `reader(repository)` return that repository's write and read transports.

        A writer's `send(method, path, body)` returns a `transport.Response`; raising means the
        outcome is unknown. The reader resolves each task's issue node ID.
        """
        self.registry, self.writer, self.reader = registry, writer, reader
        self.guard = threading.Lock()
        self.sections = {}
        self.sending = set()
        self.nodes = {}
        self.stops = {}

    @contextmanager
    def section(self, key):
        """Serialize ownership changes and mutation admission for one task; yields its condition."""
        with self.guard:
            condition = self.sections.setdefault(key, threading.Condition())
        with condition:
            yield condition

    def node(self, key):
        """The task's issue node ID, which never changes."""
        if key not in self.nodes:
            self.nodes[key] = self.reader(key[0]).request("GET", f"repos/{key[0]}/issues/{key[1]}")["node_id"]
        return self.nodes[key]

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
        if not allowed(key, method, path, body, self.node(key) if path == "graphql" else None):
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
                result, payload, error = settle(path, self.writer(key[0]).send(method, path, body),
                                                self.registry.clock())
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

    def __init__(self, repository, issue, generation, gateway, reader):
        """`reader` holds read access only: `request(method, path, data)` and `archive(path)`."""
        super().__init__(repository)
        self.key, self.generation = (repository, issue), generation
        self.gateway, self.reader = gateway, reader

    def api(self, path, method="GET", data=None):
        """Read directly; route every other request, GraphQL mutations included, to the gateway."""
        if method == "GET" or path == "graphql" and isinstance(data, dict) and QUERY.match(data.get("query") or ""):
            return self.reader.request(method, path, data)
        return self.gateway.mutate(self.key, self.generation, method, path, data)

    def archive(self, sha):
        """Download a pinned source archive through the read transport."""
        return self.reader.archive(self.root + "/tarball/" + sha)
