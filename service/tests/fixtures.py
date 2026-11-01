"""Shared service fixtures: a REST view of the engine tests' in-memory GitHub, a registry, a clock."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "engine/tests")]

# Imported once the repository root and the engine tests are on the module path.
import service  # noqa: E402  (puts the engine on the module path)
from service.gateway import GatewayGitHub, MutationGateway  # noqa: E402
from service.registry import Registry  # noqa: E402
from support import CONFIG  # noqa: E402
from test_pipeline import FakeAgents, FakeGitHub, FakeWorkspace  # noqa: E402
from controller import Controller  # noqa: E402

SERVICE = "service:primary"
REGISTRY_ID = "registry-test"
KEY = (CONFIG["repository"], 7)
PAGE = "per_page=100&page="


class Clock:
    """Manually advanced time."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class RestBackend:
    """Serve the facade's reads and the gateway's writes from a `FakeGitHub`, with injectable faults."""

    def __init__(self, fake):
        """Address only the fake's repository."""
        self.fake = fake
        self.root = "repos/" + fake.config["repository"] + "/"
        self.repository_labels = set()
        self.writes = []
        self.faults = []

    def fail(self, method, prefix, status=None, lost=False, body=""):
        """Fail the next matching write: reject it with `status`, or apply it and lose the response."""
        self.faults.append({"method": method, "prefix": prefix, "status": status, "lost": lost, "body": body})

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
        if PAGE in relative:
            base, page = relative.split(PAGE)
            base = base[:-1]
            key = "check_runs" if "/check-runs" in base else None
            items = self.page(base) if page == "1" else []
            return {key: items} if key else items
        if relative.startswith("issues?"):
            return []  # no prior outcomes to learn from
        if relative.startswith("collaborators/"):
            login = unquote(relative.split("/")[1])
            return {"permission": "write" if self.fake.permitted(login) else "read"}
        return self.fake.repo(relative, "GET")

    def page(self, base):
        """The first and only page of a collection."""
        if base == "issues/7/labels":
            return deepcopy(self.fake.issue["labels"])
        if base == "labels":
            return [{"name": name} for name in sorted(self.repository_labels)]
        return self.fake.pages(base)

    def query(self, data):
        """Answer the engine's two GraphQL read queries."""
        variables = data["variables"]
        if "linkedBranches" in data["query"]:
            nodes = [{"ref": {"name": b, "repository": {"nameWithOwner": self.fake.config["repository"]}}}
                     for b in sorted(self.fake.linked_branches)]
            page = {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return {"data": {"repository": {"issue": {"linkedBranches": page}}}}
        if "reviewThreads" in data["query"]:
            page = {"nodes": self.fake.threads(variables["number"]), "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return {"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}
        raise AssertionError(data["query"])

    def archive(self, path):
        """Serve the fake's source archive."""
        return self.fake.archive(path.rsplit("/", 1)[1])

    def send(self, method, path, body):
        """Apply one write and return `(status, payload)`, honouring the next matching fault."""
        relative = "graphql" if path == "graphql" else self.relative(path)
        fault = next((f for f in self.faults if f["method"] == method and relative.startswith(f["prefix"])
                      and (body or {}).get("body", "").startswith(f["body"])), None)
        if fault:
            self.faults.remove(fault)
            if fault["status"]:
                return fault["status"], {"message": "fixture rejection"}
        result = self.apply(method, relative, body)
        self.writes.append((method, relative))
        if fault and fault["lost"]:
            raise TimeoutError("fixture lost response")
        return 200, result

    def apply(self, method, relative, body):
        """Mutate the fake's state as GitHub would."""
        issue = self.fake.issue
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


class ServiceFixture:
    """A registry in a private temporary directory, a gateway over the REST fake, and engine fixtures."""

    def setUp(self):
        """Fresh state per test; manual mode so the seeded start command creates the task."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        Path(self.directory.name).chmod(0o700)
        self.clock = Clock()
        self.registry = self.open(initialize=True)
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.fake = FakeGitHub(self.config)
        self.backend = RestBackend(self.fake)
        self.gateway = MutationGateway(self.registry, self.backend.send, lambda path: self.backend.request("GET", path))
        self.agents = FakeAgents()

    def open(self, initialize=False):
        """Open the registry, closing it when the test ends."""
        registry = Registry.open(self.directory.name, REGISTRY_ID, initialize=initialize, clock=self.clock)
        self.addCleanup(registry.close)
        return registry

    def facade(self, generation, gateway=None):
        """The engine client for issue 7 under one generation."""
        return GatewayGitHub(KEY[0], KEY[1], generation, gateway or self.gateway, self.backend)

    def controller(self, generation, workspace=FakeWorkspace):
        """A service-owned controller for one tick."""
        return Controller(self.facade(generation), self.agents, self.config, workspace_factory=workspace,
                          controller_id=SERVICE)

    def tick(self, workspace=FakeWorkspace):
        """Acquire, advance once and release, as the worker will; approve any plan awaiting a writer."""
        generation = self.gateway.acquire(KEY, "worker")
        self.assertIsNotNone(generation)
        controller = self.controller(generation, workspace)
        outcome = None
        try:
            outcome = controller.advance(7)
            if controller.state and controller.state["stage"] == "planned":
                self.fake.comment("/concurro approve " + controller.state["plan_id"])
            return controller.state, outcome
        finally:
            self.gateway.release(KEY, generation, outcome)
