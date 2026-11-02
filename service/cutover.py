"""Move tasks between the Actions controller and this service by rewriting their ownership marker."""

import http.client
import re
import time

from controller import ACTIONS, Controller, Outcome
from github import NotFound

from . import transport
from .gateway import GatewayGitHub
from .reconciler import ENROLLMENT, pages
from .worker import content, head

# The engine commit that made the Actions controller honour ownership markers.
MARKER_AWARE = "b2aaafbcad761e294634789b0de580dbc0451207"
WORKFLOW = ".github/workflows/concurro.yml"
WORKFLOW_LIMIT = 64_000
PIN = re.compile(r"uses:\s*([\w.-]+/concurro)@([0-9a-f]{40})\b")
ACTIVE_RUNS = ("queued", "in_progress", "waiting", "pending", "requested")
COMPARE_LIMIT = 4_000_000


class CutoverError(RuntimeError):
    """A cutover precondition failed; nothing was written."""


class Token:
    """A fixed token in the transport's credential shape."""

    def __init__(self, token):
        self.token = token

    def get(self):
        return self.token

    def drop(self, token):
        pass


def compare(repository, base, head, connection=transport.connect):
    """`status` of GitHub's unauthenticated compare of two engine commits (`identical`, `ahead`, …)."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": transport.API_VERSION,
               "User-Agent": transport.USER_AGENT}
    try:
        response = transport.exchange(connection, transport.API, "GET", f"/repos/{repository}/compare/{base}...{head}",
                                      headers, None, COMPARE_LIMIT, transport.TIMEOUT)
        status = response.json().get("status") if response.sent and response.status == 200 else None
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        status = None
    if not isinstance(status, str):
        raise CutoverError("cannot compare the Actions engine pin; the engine repository must be public")
    return status


class Cutover:
    """`adopt` and `release`, run by the daemon for an operator."""

    def __init__(self, settings, identity, gateway, registry, reader, loader, clock=time.time,
                 compare=compare, transport_factory=transport.Transport):
        """`loader(repository)` returns the worker's validated `Target`; `reader(repository)` a read transport."""
        self.settings, self.identity, self.gateway, self.registry = settings, identity, gateway, registry
        self.reader, self.loader, self.clock, self.compare = reader, loader, clock, compare
        self.transport_factory = transport_factory

    def check(self, repository):
        """Require a marker-aware Actions controller that is disabled and idle.

        The pin is read with the read token and compared anonymously; variables and runs
        need the command-scoped cutover token, so without its permissions this refuses.
        """
        reader = self.reader(repository)
        workflow = content(reader, repository, WORKFLOW, head(reader, repository), WORKFLOW_LIMIT)
        pins = set(PIN.findall(workflow))
        if len(pins) != 1:
            raise CutoverError("the Actions workflow must pin exactly one engine commit")
        [(engine, pin)] = pins
        if self.compare(engine, MARKER_AWARE, pin) not in ("identical", "ahead"):
            raise CutoverError("the Actions engine pin predates ownership markers")
        with self.identity.cutover(repository) as token:
            actions = self.transport_factory(repository, Token(token), self.clock)
            for path in ("actions/variables", f"environments/{ENROLLMENT}/variables"):
                try:
                    variables = pages(actions, f"repos/{repository}/{path}", "variables")
                except NotFound:
                    variables = []
                enabled = next((v["value"] for v in variables if v["name"] == "CONCURRO_ENABLED"), "false")
                if enabled.lower() != "false":
                    raise CutoverError("CONCURRO_ENABLED must be false before cutover")
            for status in ACTIVE_RUNS:
                runs = actions.request("GET", f"repos/{repository}/actions/runs?status={status}&per_page=100")
                if runs.get("total_count", 0) > len(runs["workflow_runs"]):
                    raise CutoverError("too many active runs to establish an idle Actions controller")
                if any(run.get("path") == WORKFLOW for run in runs["workflow_runs"]):
                    raise CutoverError("an Actions controller run is pending or active")

    def adopt(self, repository, issues=()):
        """Move tasks (every enrolled open issue by default) from Actions to this service and enqueue them.

        Returns the adopted issues and, for each refused one, why.
        """
        self.check(repository)
        if not issues:
            enrolled = pages(self.reader(repository), f"repos/{repository}/issues?state=open&labels={ENROLLMENT}")
            issues = [issue["number"] for issue in enrolled if "pull_request" not in issue]
        adopted, refused = [], {}
        for number in issues:
            key = (repository, number)
            try:
                self.transfer(key, ACTIONS, self.settings.controller_id)
            except CutoverError as error:
                refused[number] = str(error)
                continue
            self.registry.enqueue(key, "adopt", self.clock())
            adopted.append(number)
        return {"adopted": adopted, "refused": refused}

    def release(self, repository, issue):
        """Hand an idle task back to Actions; the service then treats it as terminal."""
        key = (repository, issue)
        if self.registry.unresolved(key):
            raise CutoverError("resolve the task's unknown writes before releasing it")
        self.transfer(key, self.settings.controller_id, ACTIONS, Outcome("terminal"))

    def transfer(self, key, source, destination, outcome=None):
        """Rewrite one task's marker from `source` to `destination` under a fresh generation.

        The generation fences the write without consuming queued work, so a refusal loses nothing.
        """
        generation = self.gateway.acquire(key, "cutover", consume=False)
        if generation is None:
            raise CutoverError("task is ticking, or has a write in flight or unresolved")
        done = None
        try:
            target = self.loader(key[0])
            facade = GatewayGitHub(key[0], key[1], generation, self.gateway, self.reader(key[0]))
            controller = Controller(facade, None, target.config, bundle=target.bundle, controller_id=destination)
            controller.issue = facade.repo(f"issues/{key[1]}")
            controller.load(facade.pages(f"issues/{key[1]}/comments"))
            if not controller.state or controller.state.get("controller", ACTIONS) != source:
                raise CutoverError(f"task is not owned by {source}")
            controller.state["controller"] = destination
            controller.checkpoint()
            done = outcome
        finally:
            self.gateway.release(key, generation, done)
