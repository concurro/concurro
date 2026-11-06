"""Shared service fixtures: a REST view of the engine tests' in-memory GitHub, a registry, a clock."""

import base64
from copy import deepcopy
import json
import logging
import re
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "engine/tests")]

# Imported once the repository root and the engine tests are on the module path.
import service  # noqa: E402  (puts the engine on the module path)
from service.gateway import GatewayGitHub, MutationGateway  # noqa: E402
from service.transport import Response  # noqa: E402
from github import NotFound  # noqa: E402
from service.reconciler import Reconciler  # noqa: E402
from service.registry import Registry  # noqa: E402
from service.scheduler import Scheduler  # noqa: E402
from service.settings import Limits, Target  # noqa: E402
from service.worker import Worker  # noqa: E402
from support import CONFIG, TARGET  # noqa: E402
from test_pipeline import FakeAgents, FakeGitHub  # noqa: E402
from checkpoints import Checkpoints  # noqa: E402
from controller import Controller  # noqa: E402

# Expected failures are logged by the service; keep test output clean.
logging.getLogger("concurro.service").addHandler(logging.NullHandler())

SERVICE = "service:primary"
REGISTRY_ID = "registry-test"
ENGINE = "e" * 40
KEY = (CONFIG["repository"], 7)
ENROLLED = "issues?state=open&labels=concurro"


def target_files(config, charter=None):
    """The target's `.concurro/` files as the default branch holds them, pinning the test engine."""
    return {".concurro/config.json": json.dumps({**config, "engine": ENGINE}),
            ".concurro/charter.json": charter or (TARGET / ".concurro/charter.json").read_text()}


class Clock:
    """Manually advanced time."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        """Advance this clock without a wall-clock wait."""
        self.now += seconds


class RestBackend:
    """Serve the facade's reads and the gateway's writes from a `FakeGitHub`, with injectable faults."""

    def __init__(self, fake):
        """Address only the fake's repository."""
        self.fake = fake
        self.root = "repos/" + fake.config["repository"] + "/"
        self.repository_labels = set()
        self.writes = []
        self.faults = []
        self.read_faults = []
        self.files = target_files(fake.config)

    def fail(self, method, prefix, status=None, lost=False, body="", truncated=False, headers=None,
             message="fixture rejection"):
        """Fail the next matching write.

        Reject it with `status` (and `headers`), or apply it and then lose the response
        (`lost`) or truncate its body (`truncated`). The returned fault records the body it saw.
        """
        fault = {"method": method, "prefix": prefix, "status": status, "lost": lost, "body": body,
                 "truncated": truncated, "headers": headers or {}, "message": message}
        self.faults.append(fault)
        return fault

    def fail_read(self, prefix, error):
        """Raise `error` for the next read whose repository-relative path starts with `prefix`."""
        self.read_faults.append((prefix, error))

    def relative(self, path):
        """Strip and require the repository prefix."""
        if not path.startswith(self.root):
            raise AssertionError(path)
        return path[len(self.root):]

    def request(self, method, path, data=None):
        """Answer reads, including bounded pagination and GraphQL queries."""
        if path == "graphql":
            return self.query(data)
        relative = self.relative(path)
        fault = next((f for f in self.read_faults if relative.startswith(f[0])), None)
        if fault:
            self.read_faults.remove(fault)
            raise fault[1]
        if relative.startswith("contents/"):
            return self.content(relative)
        if relative.startswith("issues/comments/"):
            identifier = int(relative.rsplit("/", 1)[1])
            comment = next((c for c in self.fake.comments if c["id"] == identifier), None)
            if comment is None:
                raise NotFound("GitHub request failed: GET " + relative)
            return {**deepcopy(comment), "issue_url": f"https://api.github.com/{self.root}issues/7"}
        if relative.startswith("pulls/comments/"):
            identifier = int(relative.rsplit("/", 1)[1])
            comment = next((c for c in self.fake.review_comments if c["id"] == identifier), None)
            if comment is None:
                raise NotFound("GitHub request failed: GET " + relative)
            return {**deepcopy(comment), "pull_request_url": f"https://api.github.com/{self.root}pulls/9"}
        if match := re.fullmatch(r'(.*)[?&]per_page=(\d+)&page=(\d+)', relative):
            base, size, page = match[1], int(match[2]), int(match[3])
            key = next((key for part, key in (("/check-runs", "check_runs"), ("/check-suites", "check_suites"),
                                              ("actions/runs?", "workflow_runs")) if part in base), None)
            values = self.page(base)
            items = values[(page - 1) * size:page * size] if isinstance(values, list) else values
            return {key: items} if key else items
        if relative.startswith("issues?"):
            return []  # no prior outcomes to learn from
        if relative.startswith("collaborators/"):
            login = unquote(relative.split("/")[1])
            return {"permission": "write" if self.fake.permitted(login) else "read"}
        return self.fake.repo(relative, "GET")

    def content(self, relative):
        """A file of the default branch through the contents API."""
        path, _, ref = unquote(relative[len("contents/"):]).partition("?ref=")
        if ref != self.fake.base or path not in self.files:
            raise NotFound("GitHub request failed: GET " + relative)
        data = self.files[path].encode()
        return {"type": "file", "encoding": "base64", "path": path, "size": len(data),
                "content": base64.b64encode(data).decode()}

    def page(self, base):
        """The first and only page of a collection."""
        if base == ENROLLED:
            issue = self.fake.issue
            enrolled = issue["state"] == "open" and any(label["name"] == "concurro" for label in issue["labels"])
            return [deepcopy(issue)] if enrolled else []
        if base == "issues/7/labels":
            return deepcopy(self.fake.issue["labels"])
        if base == "labels":
            return [{"name": name} for name in sorted(self.repository_labels)]
        if base.startswith("commits/") and base.endswith("/status"):
            return {"statuses": []}
        return self.fake.pages(base)

    def query(self, data):
        """Answer the engine's two GraphQL read queries."""
        variables = data["variables"]
        if "defaultBranchRef" in data["query"]:
            ref = {"name": "main", "target": {"oid": self.fake.base}}
            return {"data": {"repository": {"defaultBranchRef": ref}}}
        if "linkedBranches" in data["query"]:
            nodes = [{"ref": {"name": b, "repository": {"nameWithOwner": self.fake.config["repository"]}}}
                     for b in sorted(self.fake.linked_branches)]
            page = {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return {"data": {"repository": {"issue": {"linkedBranches": page}}}}
        if "node(id:" in data["query"] and variables["id"] == "PR_9":
            return {"data": {"node": {"number": 9, "repository": {"nameWithOwner": self.fake.config["repository"]}}}}
        if "node(id:" in data["query"]:
            return {"data": {"node": self.thread_node(variables["id"])}}
        if "reviewThreads" in data["query"]:
            page = {"nodes": self.fake.threads(variables["number"]), "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return {"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}
        raise AssertionError(data["query"])

    def thread_node(self, thread_id):
        """A review thread as the gateway's ownership lookup reads it; GraphQL names bots without `[bot]`."""
        thread = next((t for t in self.fake.thread_list if t["id"] == thread_id), None)
        if thread is None:
            return None
        first = next(c for c in self.fake.review_comments if c["id"] == thread["comments"]["nodes"][0]["databaseId"])
        author = first["user"]["login"].removesuffix("[bot]")
        return {"pullRequest": {"number": 9, "repository": {"nameWithOwner": self.fake.config["repository"]}},
                "comments": {"nodes": [{"author": {"login": author}, "body": first["body"]}]}}

    def archive(self, path):
        """Serve the fake's source archive."""
        return self.fake.archive(path.rsplit("/", 1)[1])

    def send(self, method, path, body):
        """Apply one write and return its `Response`, honouring the next matching fault."""
        relative = "graphql" if path == "graphql" else self.relative(path)
        fault = next((f for f in self.faults if f["method"] == method and relative.startswith(f["prefix"])
                      and (body or {}).get("body", "").startswith(f["body"])), None)
        if fault:
            self.faults.remove(fault)
            fault["seen"] = deepcopy(body)
            if fault["status"]:
                return Response(True, fault["status"], fault["headers"], json.dumps({"message": fault["message"]}).encode())
        result = self.apply(method, relative, body)
        self.writes.append((method, relative))
        if fault and fault["lost"]:
            raise TimeoutError("fixture lost response")
        if fault and fault["truncated"]:
            return Response(True, 201, {}, None, "incomplete")
        return Response(True, 200, {}, json.dumps(result).encode())

    def apply(self, method, relative, body):
        """Mutate the fake's state as GitHub would."""
        issue = self.fake.issue
        if relative == "graphql" and "markPullRequestReadyForReview" in body["query"]:
            self.fake.mark_ready(body["variables"]["input"]["pullRequestId"])
            return {"data": {"markPullRequestReadyForReview": {"pullRequest": {
                "id": "PR_9", "isDraft": False, "headRefOid": self.fake.pr["head"]["sha"]}}}}
        if relative == "graphql" and "resolveReviewThread" in body["query"]:
            thread = body["variables"]["input"]["threadId"]
            self.fake.resolve_thread(thread)
            return {"data": {"resolveReviewThread": {"thread": {"id": thread, "isResolved": True}}}}
        if relative == "graphql":
            branch = body["variables"]["input"]
            self.fake.create_linked_branch(branch["issueId"], branch["name"], branch["oid"])
            ref = {"name": branch["name"], "target": {"oid": branch["oid"]}}
            return {"data": {"createLinkedBranch": {"linkedBranch": {"ref": ref}}}}
        if relative == "issues/7/labels" and method == "POST":
            names = {label["name"] for label in issue["labels"]}
            issue["labels"] += [{"name": name} for name in body["labels"] if name not in names]
            return deepcopy(issue["labels"])
        if relative.startswith("issues/7/labels/") and method == "DELETE":
            name = unquote(relative.rsplit("/", 1)[1])
            issue["labels"] = [label for label in issue["labels"] if label["name"] != name]
            return deepcopy(issue["labels"])
        if relative == "labels":
            self.repository_labels.add(body["name"])
            return {"name": body["name"]}
        return self.fake.repo(relative, method, body)

    def labels(self):
        """The issue's current label names."""
        return sorted(label["name"] for label in self.fake.issue["labels"])


class Reply:
    """The parts of `http.client.HTTPResponse` the transport reads."""

    def __init__(self, status, payload, headers=None):
        self.status = status
        self.data = b"" if payload is None else json.dumps(payload).encode()
        self.headers = {**(headers or {}), "Content-Length": str(len(self.data))}

    def getheaders(self):
        return list(self.headers.items())

    def getheader(self, name, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), default)

    def read(self, size):
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class Wire:
    """`http.client`-shaped connections serving a `RestBackend`'s reads, so a real transport fronts the engine.

    `reply(match, status, payload, headers)` answers the next request for which
    `match(method, path, data)` holds with that raw response instead.
    """

    def __init__(self, backend):
        self.backend = backend
        self.replies = []

    def reply(self, match, status, payload, headers=None):
        self.replies.append((match, Reply(status, payload, headers)))

    def connection(self, host, timeout):
        return WireConnection(self)

    def answer(self, method, path, data):
        """The raw reply for one request."""
        found = next((r for r in self.replies if r[0](method, path, data)), None)
        if found:
            self.replies.remove(found)
            return found[1]
        try:
            return Reply(200, self.backend.request(method, path, data))
        except NotFound:
            return Reply(404, {"message": "Not Found"})


class WireConnection:
    """One connection to a `Wire`."""

    def __init__(self, wire):
        self.wire = wire

    def connect(self):
        pass

    def request(self, method, target, body=None, headers=None):
        self.reply = self.wire.answer(method, target.lstrip("/"), json.loads(body) if body else None)

    def getresponse(self):
        return self.reply

    def close(self):
        pass


class Inline:
    """An executor that runs each tick at once, so tests observe every tick deterministically."""

    def submit(self, function, *args):
        function(*args)

    def shutdown(self, wait=True):
        pass


class ServiceFixture:
    """A registry in a private temporary directory, a gateway over the REST fake, and engine fixtures."""

    def setUp(self):
        """Fresh state per test; manual mode so the seeded start command creates the task."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        Path(self.directory.name).chmod(0o700)
        self.clock = Clock()
        self.write_clock = Clock()
        self.pacing = {"clock": self.write_clock, "sleep": self.write_clock.sleep}
        self.registry = self.open(initialize=True)
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.fake = FakeGitHub(self.config)
        self.backend = RestBackend(self.fake)
        self.gateway = MutationGateway(self.registry, lambda repository: self.backend, lambda repository: self.backend,
                                       self.config["bot_login"], **self.pacing)
        self.reader = self.backend
        self.agents = FakeAgents()

    def open(self, initialize=False):
        """Open the registry, closing it when the test ends."""
        registry = Registry.open(self.directory.name, REGISTRY_ID, initialize=initialize, clock=self.clock)
        self.addCleanup(registry.close)
        return registry

    def facade(self, generation, gateway=None):
        """The engine client for issue 7 under one generation."""
        return GatewayGitHub(KEY[0], KEY[1], generation, gateway or self.gateway, self.reader)

    def controller(self, generation):
        """A service-owned controller for one tick."""
        self.registry.bind(KEY, KEY[0], KEY[0], ENGINE)
        return Controller(self.facade(generation), self.agents, self.config,
                          controller_id=SERVICE)

    def tick(self):
        """Acquire, advance once and release, as the worker will; approve any plan awaiting a writer."""
        generation = self.gateway.acquire(KEY, "worker")
        self.assertIsNotNone(generation)
        controller = self.controller(generation)
        outcome = None
        try:
            outcome = controller.advance(7)
            if controller.state and controller.state["stage"] == "planned":
                self.fake.comment("/concurro approve " + controller.state["plan_id"])
            return controller.state, outcome
        finally:
            self.gateway.release(KEY, generation, outcome)

    def service(self, executor=None, limits=None):
        """The reconciler, worker and scheduler over the fakes, with the fake agents."""
        bot = self.config["bot_login"]
        self.settings = SimpleNamespace(controller_id=SERVICE, engine=ENGINE, limits=limits or Limits(4, 2),
                                        targets=(Target(KEY[0], KEY[0], "both"),),
                                        state_dir=Path(self.directory.name))
        self.reconciler = Reconciler(self.registry, lambda repository: self.backend, [KEY[0]], {bot}, clock=self.clock)
        self.worker = Worker(self.settings, self.gateway, self.registry, self.reconciler, lambda repository: self.backend,
                             bot, agents=lambda *args, **kwargs: self.agents,
                             executor=lambda repository, github: None)
        self.scheduler = Scheduler(self.registry, self.gateway, self.worker, self.settings.limits, clock=self.clock,
                                   executor=executor or Inline())
        return self.scheduler

    def drain(self, limit=20):
        """Dispatch until nothing is due now; return the number of ticks started."""
        ticks = 0
        for _ in range(limit):
            started = self.scheduler.dispatch()
            if not started:
                return ticks
            ticks += len(started)
        self.fail("work never drained")

    def stage(self):
        """The task's stage, as its progress label shows it."""
        return next((label[len("concurro:stage:"):] for label in self.backend.labels()
                     if label.startswith("concurro:stage:")), None)

    def state(self):
        """The task's verified checkpoint state, read directly from the fake."""
        return Checkpoints(self.fake, self.config, KEY[1]).load(self.fake.pages(f"issues/{KEY[1]}/comments"))
