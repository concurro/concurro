"""Run one controller tick for one owned task, with that target's own configuration and bundle."""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import tempfile
import threading
from urllib.parse import quote

from agents import Agents, HostExecutor, RunnerExecutor
from bench import RemoteBench
from bundle import CHARTER, Bundle, RESOURCE_LIMIT, profile_paths
from checkpoint_health import sync_checkpoint_health
from checkpoints import CheckpointError
from controller import Controller, validate_config
from conversation import PublicationError
from github import Transient
from mirror import Mirror
from policy import Blocked
import private
from relay import Operator

from . import failures
from .errors import GatewayStop
from .gateway import GatewayGitHub, LEASE_SECONDS
from .settings import CHECKOUT

CONFIG = ".concurro/config.json"
CONFIG_LIMIT = 64_000
RENEW_SECONDS = LEASE_SECONDS // 3
DEFAULT_BRANCH = """query($owner:String!,$name:String!) {
  repository(owner:$owner,name:$name) { defaultBranchRef { name target { oid } } }
}"""
RUNNER_IDENTITY = """query($owner:String!,$name:String!) {
  repository(owner:$owner,name:$name) {
    databaseId defaultBranchRef { name }
    owner { ... on User { databaseId } ... on Organization { databaseId } }
  }
}"""

log = logging.getLogger("concurro.service")


@dataclass(frozen=True)
class Target:
    """One repository's validated configuration and bundle at its default-branch head."""

    config: dict
    bundle: Bundle


def default_branch(reader, repository):
    """The default branch's name and head commit."""
    owner, name = repository.split("/")
    response = reader.request("POST", "graphql", {"query": DEFAULT_BRANCH, "variables": {"owner": owner, "name": name}})
    branch = ((response or {}).get("data") or {}).get("repository", {}).get("defaultBranchRef") or {}
    sha = (branch.get("target") or {}).get("oid")
    if response.get("errors") or not isinstance(sha, str) or len(sha) != 40 or not isinstance(branch.get("name"), str):
        raise Blocked("cannot establish the target's default branch")
    return branch["name"], sha


def head(reader, repository):
    """The default branch's head commit."""
    return default_branch(reader, repository)[1]


def runner_claims(reader, repository, workflow_ref, engine, bot_login):
    """The OIDC claims a runner session of this target must present: the target, the pinned agent
    workflow at the served engine commit, a dispatch by the App's bot on the default branch."""
    owner, name = repository.split("/")
    response = reader.request("POST", "graphql", {"query": RUNNER_IDENTITY, "variables": {"owner": owner, "name": name}})
    found = ((response or {}).get("data") or {}).get("repository") or {}
    branch = (found.get("defaultBranchRef") or {}).get("name")
    # A bot is not a GraphQL `user`; its public profile is the one read outside the repository.
    bot = reader.request("GET", "users/" + quote(bot_login))
    if (response.get("errors") or not isinstance(found.get("databaseId"), int)
            or not isinstance((found.get("owner") or {}).get("databaseId"), int) or not isinstance(branch, str)
            or not isinstance(bot, dict) or not isinstance(bot.get("id"), int) or bot.get("login") != bot_login):
        raise Blocked("cannot establish the target's runner identity")
    return {"repository_id": str(found["databaseId"]), "repository_owner_id": str(found["owner"]["databaseId"]),
            "job_workflow_ref": workflow_ref, "job_workflow_sha": engine, "event_name": "repository_dispatch",
            "ref": "refs/heads/" + branch, "actor_id": str(bot["id"])}


def content(reader, repository, path, sha, limit):
    """A regular file's text at one commit; links, directories and oversized files are refused."""
    item = reader.request("GET", f"repos/{repository}/contents/{quote(path)}?ref={sha}")
    if (not isinstance(item, dict) or item.get("type") != "file" or item.get("encoding") != "base64"
            or item.get("path") != path or not isinstance(item.get("size"), int) or item["size"] > limit):
        raise Blocked("target configuration must be bounded regular files")
    data = base64.b64decode(item["content"])
    if len(data) != item["size"]:
        raise Blocked("target configuration must be bounded regular files")
    return data.decode("utf-8")


def load_target(target, reader, engine, bot_login, engine_root=CHECKOUT):
    """Read and validate `.concurro/` at the default-branch head, through the read token.

    The target must name the verified App and keep merging disabled. Its `engine` pin is
    optional: without one, it runs whatever engine the operator serves; with one, only that. Files are staged in a private temporary directory only while the
    bundle reads them.
    """
    repository = target.repository
    sha = head(reader, repository)
    config = json.loads(content(reader, repository, CONFIG, sha, CONFIG_LIMIT))
    if not isinstance(config, dict):
        raise Blocked("target configuration must be an object")
    validate_config(config)
    if config["repository"] != repository:
        raise Blocked("target configuration names another repository")
    if "engine" in config and config["engine"] != engine:
        raise Blocked("target pins another engine commit")
    if config["bot_login"] != bot_login:
        raise Blocked("target bot_login differs from the controller App")
    if config["merge_enabled"] is not False:
        raise Blocked("merging must stay disabled")
    paths = [CHARTER] + [path for _, path in profile_paths(config["execution_profiles"]) if path.startswith(".concurro/")]
    with tempfile.TemporaryDirectory(prefix="concurro-target-") as staging:
        root = Path(staging)
        root.chmod(0o700)
        for path in paths:
            file = root / path
            file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            file.write_text(content(reader, repository, path, sha, RESOURCE_LIMIT), encoding="utf-8")
            file.chmod(0o600)
        bundle = Bundle(engine_root, target=root, profiles=config["execution_profiles"])
    return Target(config, bundle)


class Worker:
    """Snapshot, load, advance and classify one task under one generation."""

    def __init__(self, settings, gateway, registry, reconciler, reader, bot_login, engine_root=CHECKOUT,
                 agents=Agents, workspace_factory=None, renew_seconds=RENEW_SECONDS, executor=None):
        """`reader(repository)` returns a read transport; `agents`, `workspace_factory` and `executor` are injectable."""
        self.settings, self.gateway, self.registry, self.reconciler = settings, gateway, registry, reconciler
        self.reader, self.bot_login, self.engine_root = reader, bot_login, engine_root
        self.agents, self.workspace_factory, self.renew_seconds = agents, workspace_factory, renew_seconds
        self.make_executor = executor or self.executor
        self.targets = {target.repository: target for target in settings.targets}
        self.guard = threading.Lock()
        self.failures = {}
        self.claims = {}

    def executor(self, repository, github):
        """Where this target's turns run: its runners, dispatched through the gateway, or this host."""
        gateway = self.settings.gateway
        operator = Operator(gateway.url, private.read(gateway.operator_token_path, (CHECKOUT,)).decode().strip())
        request = {"url": gateway.url, "headers": gateway.headers}
        if gateway.executor == "host":
            return HostExecutor(operator, request, self.settings.agent_image,
                                Mirror(self.settings.state_dir / "mirrors", repository),
                                self.settings.state_dir / "turns", gateway.budget)
        with self.guard:
            claims = self.claims.get(repository)
        if claims is None:
            claims = runner_claims(self.reader(repository), repository, gateway.workflow_ref, self.settings.engine,
                                   self.bot_login)
            with self.guard:
                self.claims[repository] = claims
        return RunnerExecutor(operator, request, claims, list(gateway.models), gateway.budget, github.dispatch)

    def workspaces(self, executor):
        """Where the trusted workbench runs: on the target's runners with its turns, or on this host."""
        if self.workspace_factory is not None:
            return self.workspace_factory
        return RemoteBench(executor) if isinstance(executor, RunnerExecutor) else None

    def load(self, repository):
        """The target's configuration and bundle, read afresh for each tick."""
        return load_target(self.targets[repository], self.reader(repository), self.settings.engine,
                           self.bot_login, self.engine_root)

    @contextmanager
    def renewing(self, key, generation):
        """Renew the lease while the tick runs; expiry affects liveness only."""
        done = threading.Event()

        def renew():
            while not done.wait(self.renew_seconds):
                self.gateway.renew(key, generation)

        thread = threading.Thread(target=renew, name=f"lease-{key[0]}#{key[1]}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()

    def run(self, key, generation):
        """Advance the task once; return its outcome, or the stop that ended the tick.

        The snapshot taken first is acknowledged only when the tick completed or blocked
        durably, so input arriving during the tick is never acknowledged unseen.
        """
        with self.renewing(key, generation):
            controller = snapshot = None
            try:
                snapshot = self.reconciler.snapshot(key)
                target = self.load(key[0])
                facade = GatewayGitHub(key[0], key[1], generation, self.gateway, self.reader(key[0]))
                executor = self.make_executor(key[0], facade)
                agents = self.agents(target.config, executor, bundle=target.bundle,
                                     retained=self.settings.state_dir / "sessions" / key[0].replace("/", "__"))
                controller = Controller(facade, agents, target.config, self.workspaces(executor), bundle=target.bundle,
                                        controller_id=self.settings.controller_id)
                outcome = controller.advance(key[1])
                if controller.state:
                    sync_checkpoint_health(facade, target.config, key[1], failed=False)
                    if controller.state.get("pr"):
                        self.registry.link(key, controller.state["pr"])
            except (Exception, GatewayStop) as error:
                return self.failed(key, generation, error, controller, snapshot)
            stop = self.gateway.stop(key, generation)
            if stop is not None:
                return stop
            self.registry.acknowledge(key, snapshot)
            with self.guard:
                self.failures.pop(key, None)
            return outcome

    def failed(self, key, generation, error, controller, snapshot):
        """Classify a failure, make the one diagnostic write it warrants, and acknowledge a durable block."""
        verified = bool(controller and controller.state)
        with self.guard:
            attempt = self.failures.get(key, 0)
        result = failures.classify(error, self.gateway.stop(key, generation), attempt, verified)
        log.warning("tick of %s#%s (generation %s) ended: %s", *key, generation, describe(error))
        if isinstance(result, GatewayStop):
            return result
        try:
            action = failures.effect(error, verified)
            if action == "suspend":
                reason = str(error) if isinstance(error, Blocked) else "Transport or schema failure; inspect service health."
                try:
                    # The Blocked comment is posted now; a publication failure has the health notice instead.
                    controller.suspend("blocked", reason, outboxes=False,
                                       publish_events=not isinstance(error, PublicationError))
                except Exception as display_error:
                    diagnostic = display_error if isinstance(display_error, CheckpointError) else CheckpointError("failure_display")
                    sync_checkpoint_health(controller.gh, controller.config, key[1], failed=True, error=diagnostic)
                    result = failures.classify(display_error, attempt=attempt, verified=False)
                else:
                    if isinstance(error, CheckpointError):
                        sync_checkpoint_health(controller.gh, controller.config, key[1], failed=True, error=error)
            elif action == "health":
                sync_checkpoint_health(controller.gh, controller.config, key[1], failed=True, error=error)
        except GatewayStop as stop:
            return self.gateway.stop(key, generation) or stop
        if result.kind == "blocked":
            # Suspended durably: the pre-tick input is handled, and only new input wakes the task.
            self.registry.acknowledge(key, snapshot)
        elif result.kind == "wait-until" and snapshot is not None:
            # Polling must not run this input again before its retry is due.
            self.registry.defer(key, snapshot)
        with self.guard:
            if result.kind == "wait-until":
                self.failures[key] = attempt + 1
            else:
                self.failures.pop(key, None)
        return result


def describe(error):
    """Loggable failure text: fixed engine and gateway texts only, never raw provider output."""
    if isinstance(error, (Blocked, GatewayStop, Transient)):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__
