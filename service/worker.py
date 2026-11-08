"""Run one controller tick for one owned task, with that target's own configuration and bundle."""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
import json
import logging
import re
from pathlib import Path
import tempfile
import threading
import uuid
from urllib.parse import quote

from agents import Agents, RunnerExecutor
from bundle import CHARTER, Bundle, RESOURCE_LIMIT, TARGET_INSTRUCTIONS
from checkpoint_health import sync_checkpoint_health
from checkpoints import CheckpointError
from controller import Controller, validate_config
from conversation import PublicationError
from github import NotFound, Transient
from mirror import Mirror
from model_config import parse as parse_models, resolve
from policy import Blocked
import private
from relay import ExchangeError, Operator
from report_store import Cache

from . import failures
from .errors import GatewayStop
from .gateway import GatewayGitHub, LEASE_SECONDS
from .settings import CHECKOUT
from .targets import enrollment
from .target_config import read_remote

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
    config_sha: str


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
            "job_workflow_ref": workflow_ref, "job_workflow_sha": engine, "event_name": "workflow_dispatch",
            "ref": "", "actor_id": str(bot["id"])}


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


def load_target(target, reader, engine, bot_login, engine_root=CHECKOUT, config_sha=None):
    """Read JSON or YAML `.concurro/` configuration at the default-branch head, through the read token.

    The target must name the verified App. Its `engine` pin is
    optional: without one, it runs whatever engine the operator serves; with one, only that. Files are staged in a private temporary directory only while the
    bundle reads them.
    """
    repository = target.fork
    sha = config_sha or head(reader, repository)
    config = read_remote(lambda path: content(reader, repository, path, sha, CONFIG_LIMIT))
    if not isinstance(config, dict):
        raise Blocked("target configuration must be an object")
    validate_config(config)
    active = ({a.profile.harness for a in parse_models(config).profiles} if "agents" in config else
              {config["planner"], config["implementer"], config["qc"], *config["reviewers"]})
    if not active <= {"claude", "codex"}:
        raise Blocked("the MVP runner supports Claude and Codex roles only")
    if config["repository"].lower() != repository.lower():
        raise Blocked("target configuration names another repository")
    if "engine" in config and config["engine"] != engine:
        raise Blocked("target pins another engine commit")
    if config["bot_login"] != bot_login:
        raise Blocked("target bot_login differs from the controller App")
    paths = [CHARTER]
    with tempfile.TemporaryDirectory(prefix="concurro-target-") as staging:
        root = Path(staging)
        root.chmod(0o700)
        for path in paths + sorted(TARGET_INSTRUCTIONS):
            try:
                text = content(reader, repository, path, sha, RESOURCE_LIMIT)
            except NotFound:
                if path in TARGET_INSTRUCTIONS:
                    continue
                raise
            file = root / path
            file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            file.write_text(text, encoding="utf-8")
            file.chmod(0o600)
        bundle = Bundle(engine_root, target=root)
    return Target(config, bundle, sha)


def runner_termination(reader, fork, config_sha, observation):
    """Diagnose only the OIDC-bound job after exchange loss; unavailable evidence stays unknown."""
    runner = observation.get("runner", {})
    session, identity = runner.get("session", ""), runner.get("run_id", "")
    if not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}", session):
        return None
    if not isinstance(identity, str) or not re.fullmatch(r"[1-9][0-9]*/1", identity):
        return None
    run_id, attempt = identity.split("/")
    path = f"repos/{fork}/actions/runs/{run_id}/attempts/{attempt}"
    try:
        run = reader.request("GET", path)
        if (str(run.get("id")) != run_id or run.get("run_attempt") != 1
                or run.get("head_branch") != "concurro/runs/" + session
                or run.get("head_sha") != config_sha or run.get("event") != "workflow_dispatch"
                or run.get("status") != "completed"):
            return None
        jobs = reader.request("GET", path + "/jobs?per_page=100").get("jobs", [])
    except Exception:
        # Diagnostics must not turn runner loss into an automatically retried transport error.
        return None
    conclusion = run.get("conclusion")
    if any(job.get("conclusion") == "timed_out" for job in jobs):
        conclusion = "timed_out"
    labels = {"timed_out": "timed out", "cancelled": "was cancelled", "failure": "failed",
              "success": "finished without delivering the result"}
    label = labels.get(conclusion, "ended without a result")
    duration = ""
    try:
        starts = [datetime.fromisoformat(job["started_at"].replace("Z", "+00:00")) for job in jobs]
        ends = [datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00")) for job in jobs]
        seconds = int((max(ends) - min(starts)).total_seconds())
        if 0 <= seconds <= 86400:
            duration = f" after {seconds // 60}m {seconds % 60}s of job time"
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return (f"Runner {label}{duration}: https://github.com/{fork}/actions/runs/{run_id}. "
            "No result was saved; work may be incomplete. No automatic restart was attempted. "
            "Inspect the run before a writer retries.")


class DeferredExecutor:
    """Build runner authorization only when an enrolled task first asks for a model turn."""

    def __init__(self, factory):
        self.factory = factory

    @cached_property
    def executor(self):
        """The task-bound executor, reused for this tick."""
        return self.factory()

    @property
    def gateway(self):
        """The inference endpoint for a model request."""
        return self.executor.gateway

    @property
    def timing(self):
        """The last observation, without initializing an executor just for telemetry."""
        return getattr(self.__dict__.get("executor"), "timing", None)

    @property
    def task(self):
        """The current task binding used by every admitted turn."""
        return self.executor.task

    def submit(self, *args, **kwargs):
        """Admit one independently observed review turn."""
        return self.executor.submit(*args, **kwargs)

    def observe(self, handle):
        """Observe without holding up another reviewer."""
        return self.executor.observe(handle)

    def cancel(self, handle):
        """Retire only the supplied attempt's runner."""
        return self.executor.cancel(handle)

    def check_owner(self):
        """Fence result application under the same service generation."""
        return self.executor.check_owner()

    def sleep(self, seconds):
        """Yield between observation sweeps while lease renewal remains independent."""
        return self.executor.sleep(seconds)

    def run(self, *args, **kwargs):
        """Dispatch only after initialization has pinned the task's configuration."""
        return self.executor.run(*args, **kwargs)


class Worker:
    """Snapshot, load, advance and classify one task under one generation."""

    def __init__(self, settings, gateway, registry, reconciler, reader, bot_login, engine_root=CHECKOUT,
                 agents=Agents, renew_seconds=RENEW_SECONDS, executor=None):
        """`reader(repository)` returns a read transport; `agents` and `executor` are injectable."""
        self.settings, self.gateway, self.registry, self.reconciler = settings, gateway, registry, reconciler
        self.reader, self.bot_login, self.engine_root = reader, bot_login, engine_root
        self.agents, self.renew_seconds = agents, renew_seconds
        self.make_executor = executor or (lambda repo, gh: DeferredExecutor(lambda: self.executor(repo, gh)))
        self.targets = enrollment(settings.targets)
        self.guard = threading.Lock()
        self.failures = {}
        self.claims = {}
        self.report_cache = Cache()

    def executor(self, repository, github):
        """The task's role jobs, dispatched in its fork at the recorded configuration commit."""
        gateway = self.settings.gateway
        operator = Operator(gateway.url, private.read(gateway.operator_token_path, (CHECKOUT,)).decode().strip())
        request = {"url": gateway.url, "headers": gateway.headers}
        binding = self.registry.binding(github.key)
        if binding is None:
            raise Blocked("runner task has no recorded target binding")
        fork = binding.fork
        with self.guard:
            claims = self.claims.get(fork)
        if claims is None:
            claims = runner_claims(self.reader(fork), fork, gateway.workflow_ref, self.settings.engine, self.bot_login)
            with self.guard:
                self.claims[fork] = claims
        task = {"repository": github.key[0], "issue": github.key[1], "generation": github.generation,
                "upstream": binding.upstream, "fork": fork, "config_sha": binding.config_sha}
        return RunnerExecutor(operator, request, claims, list(gateway.models), gateway.budget,
                              lambda session: self.gateway.dispatch_runner(github.key, github.generation, session),
                              task=task, check_owner=lambda: self.gateway.check_owner(github.key, github.generation),
                              runner_status=lambda observation: runner_termination(
                                  self.reader(fork), fork, binding.config_sha, observation))

    def evaluate(self, repository, issue, provider):
        """An owner-requested fixture evaluation using the target's normal runner authorization."""
        if repository not in self.targets:
            raise Blocked("evaluation needs a configured target and provider")
        key = (repository, issue)
        generation = self.gateway.acquire(key, "design-evaluation", consume=False)
        if generation is None:
            raise Blocked("task is busy or has an unresolved write")
        try:
            with self.renewing(key, generation):
                target = self.load(repository, issue, pin=True)
                facade = GatewayGitHub(repository, issue, generation, self.gateway, self.reader(repository))
                executor = self.make_executor(repository, facade)
                resolve(target.config, provider, "plan_qc")
                agents = self.agents(target.config, executor, bundle=target.bundle)
                base = head(self.reader(target.config["upstream"]), target.config["upstream"])
                from design_eval import evaluate
                return evaluate(agents, target.config, provider, base,
                                self.settings.state_dir / "evaluations" / uuid.uuid4().hex)
        finally:
            self.gateway.release(key, generation, None)
            self.cleanup_runners()

    def cleanup_runners(self):
        """Revoke stale jobs and sweep only recorded session refs, preserving task queues and outcomes."""
        gateway = self.settings.gateway
        operator = Operator(gateway.url, private.read(gateway.operator_token_path, (CHECKOUT,)).decode().strip())
        for session, repository, issue, generation in self.registry.runner_sessions():
            key = (repository, issue)
            owner = self.registry.current(key)
            if owner and owner.live and owner.generation == generation and owner.lease_until > self.registry.clock():
                continue
            try:
                operator.end(session)
            except ExchangeError:
                # Do not remove the ref until the exchange durably ends the session.
                continue
            grant = self.gateway.acquire(key, "runner-cleanup", consume=False)
            if grant is None:
                continue
            try:
                target = self.registry.binding(key)
                root = f"repos/{target.fork}/git/"
                ref = "refs/heads/concurro/runs/" + session
                refs = self.reader(target.fork).request("GET", root + "matching-refs/heads/concurro/runs/" + session)
                found = [item for item in refs if item["ref"] == ref]
                if found:
                    if found[0]["object"]["sha"] != target.config_sha:
                        log.warning("session ref %s moved; left for operator reconciliation", session)
                        continue
                    self.gateway.mutate(key, grant, "DELETE", root + ref)
                self.registry.remove_runner(session)
            except (Exception, GatewayStop) as error:
                log.warning("runner cleanup failed (%s)", type(error).__name__)
            finally:
                self.gateway.release(key, grant, None)

    def load(self, repository, issue=None, pin=False):
        """Read the fork configuration pinned for this task; new tasks use its current head."""
        target = self.targets[repository]
        key = (repository, issue)
        binding = self.registry.binding(key) if issue is not None else None
        if binding and (binding.upstream, binding.fork) != (target.upstream, target.fork):
            raise Blocked("task target changed; reconcile before continuing")
        loaded = load_target(target, self.reader(target.fork), self.settings.engine,
                             self.bot_login, self.engine_root, binding.config_sha if binding else None)
        if issue is not None and pin:
            bound = self.registry.bind(key, target.upstream, target.fork, loaded.config_sha)
            if bound.config_sha != loaded.config_sha:
                loaded = load_target(target, self.reader(target.fork), self.settings.engine,
                                     self.bot_login, self.engine_root, bound.config_sha)
        config = {**loaded.config, "repository": repository, "upstream": target.upstream,
                  "fork": target.fork, "config_sha": loaded.config_sha}
        return Target(config, loaded.bundle, loaded.config_sha)

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
                target = self.load(key[0], key[1])
                facade = GatewayGitHub(key[0], key[1], generation, self.gateway, self.reader(key[0]))
                code = GatewayGitHub(target.config["fork"], key[1], generation, self.gateway,
                                     self.reader(target.config["fork"]), task_key=key)
                pulls = GatewayGitHub(target.config["upstream"], key[1], generation, self.gateway,
                                      self.reader(target.config["upstream"]), task_key=key)
                executor = self.make_executor(key[0], facade)
                agents = self.agents(target.config, executor, bundle=target.bundle,
                                     retained=self.settings.state_dir / "sessions" / key[0].replace("/", "__"))
                controller = Controller(facade, agents, target.config, bundle=target.bundle,
                                        controller_id=self.settings.controller_id, code=code, pulls=pulls,
                                        report_cache=self.report_cache,
                                        enroll=lambda: self.registry.bind(key, target.config["upstream"],
                                                                          target.config["fork"], target.config_sha))
                outcome = controller.advance(key[1])
                if controller.state:
                    sync_checkpoint_health(facade, target.config, key[1], failed=False)
                    if controller.state.get("pr"):
                        self.registry.link(key, controller.state["pr"], target.config["upstream"])
            except (Exception, GatewayStop) as error:
                return self.failed(key, generation, error, controller, snapshot)
            stop = self.gateway.stop(key, generation)
            if stop is not None:
                return stop
            self.registry.acknowledge(key, {**snapshot, "_stage": controller.state["stage"] if controller.state else None})
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
            self.registry.acknowledge(key, {**snapshot, "_stage": controller.state["stage"] if controller.state else None})
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
