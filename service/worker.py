"""Run one controller tick for one owned task, with that target's own configuration, bundle and credentials."""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import tempfile
import threading
from urllib.parse import quote

from agents import Agents
from bundle import CHARTER, Bundle, RESOURCE_LIMIT, profile_paths
from checkpoint_health import sync_checkpoint_health
from checkpoints import CheckpointError
from controller import Controller, validate_config
from credentials import EnvironmentCredentials
from github import Transient
from policy import Blocked, SECRET_NAMES
import private

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

log = logging.getLogger("concurro.service")


@dataclass(frozen=True)
class Target:
    """One repository's validated configuration, bundle and credentials at its default-branch head."""

    config: dict
    bundle: Bundle
    credentials: EnvironmentCredentials


def head(reader, repository):
    """The default branch's head commit."""
    owner, name = repository.split("/")
    response = reader.request("POST", "graphql", {"query": DEFAULT_BRANCH, "variables": {"owner": owner, "name": name}})
    branch = ((response or {}).get("data") or {}).get("repository", {}).get("defaultBranchRef") or {}
    sha = (branch.get("target") or {}).get("oid")
    if response.get("errors") or not isinstance(sha, str) or len(sha) != 40:
        raise Blocked("cannot establish the target's default branch")
    return sha


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


def secrets(path):
    """A target's provider credentials file: only known credential names with non-empty values."""
    data = json.loads(private.read(path, (CHECKOUT,)))
    if (not isinstance(data, dict) or not set(data).issubset(SECRET_NAMES)
            or not all(isinstance(v, str) and v.strip() and "\x00" not in v for v in data.values())):
        raise Blocked("credential file has invalid or unsupported entries")
    return data


def load_target(target, reader, engine, bot_login, engine_root=CHECKOUT):
    """Read and validate `.concurro/` at the default-branch head, through the read token.

    The target must pin exactly this service's engine, name the verified App and keep
    merging disabled. Files are staged in a private temporary directory only while the
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
    if config.get("engine") != engine:
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
    return Target(config, bundle, EnvironmentCredentials(secrets(target.credentials_file)))


class Worker:
    """Snapshot, load, advance and classify one task under one generation."""

    def __init__(self, settings, gateway, registry, reconciler, reader, bot_login, engine_root=CHECKOUT,
                 agents=Agents, workspace_factory=None, renew_seconds=RENEW_SECONDS):
        """`reader(repository)` returns a read transport; `agents` and `workspace_factory` are injectable for tests."""
        self.settings, self.gateway, self.registry, self.reconciler = settings, gateway, registry, reconciler
        self.reader, self.bot_login, self.engine_root = reader, bot_login, engine_root
        self.agents, self.workspace_factory, self.renew_seconds = agents, workspace_factory, renew_seconds
        self.targets = {target.repository: target for target in settings.targets}
        self.guard = threading.Lock()
        self.failures = {}

    def load(self, repository):
        """The target's configuration, bundle and credentials, read afresh for each tick."""
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
                agents = self.agents(target.config, archive_loader=facade.archive, credentials=target.credentials,
                                     bundle=target.bundle)
                controller = Controller(facade, agents, target.config, self.workspace_factory, bundle=target.bundle,
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
                    controller.suspend("blocked", reason, publish_events=False)
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
