"""Run one controller tick for one owned task, with that target's own configuration and bundle."""

import logging
import threading
import uuid
from contextlib import contextmanager
from functools import cached_property

import private
from agents import Agents, RunnerExecutor
from checkpoint_health import sync_checkpoint_health
from checkpoints import CheckpointError
from controller import Controller, config_digest
from conversation import PublicationError
from github import Transient
from model_config import resolve
from policy import Blocked
from relay import ExchangeError, Operator
from report_store import Cache

from . import failures
from .admission import RUN_BRANCH_PREFIX
from .diagnostics import Recorder
from .errors import GatewayStop
from .gateway import LEASE_SECONDS, GatewayGitHub
from .github_reads import (
    DEFAULT_BRANCH as DEFAULT_BRANCH,
    RUNNER_IDENTITY as RUNNER_IDENTITY,
    content as content,
    default_branch as default_branch,
    head as head,
    runner_claims as runner_claims,
)
from .runner_diagnostics import runner_termination as runner_termination
from .settings import CHECKOUT
from .target_loader import Target as Target, load_target as load_target
from .targets import enrollment

RENEW_SECONDS = LEASE_SECONDS // 3

log = logging.getLogger("concurro.service")


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

    def __init__(
        self,
        settings,
        gateway,
        registry,
        reconciler,
        reader,
        bot_login,
        engine_root=CHECKOUT,
        agents=Agents,
        renew_seconds=RENEW_SECONDS,
        executor=None,
        diagnostics=None,
        delegation_key=None,
    ):
        """`reader(repository)` returns a read transport; `agents` and `executor` are injectable.

        `diagnostics`, a `service.diagnostics.Store`, records every runner turn when given.
        `delegation_key` verifies backoffice commands (`engine/delegation.py`).
        """
        self.settings, self.gateway, self.registry, self.reconciler = settings, gateway, registry, reconciler
        self.reader, self.bot_login, self.engine_root = reader, bot_login, engine_root
        self.agents, self.renew_seconds = agents, renew_seconds
        self.make_executor = executor or (lambda repo, gh: DeferredExecutor(lambda: self.executor(repo, gh)))
        self.targets = enrollment(settings.targets)
        self.guard = threading.Lock()
        self.failures = {}
        self.claims = {}
        self.report_cache = Cache()
        self.diagnostics = diagnostics
        self.delegation_key = delegation_key

    def _operator(self):
        """Read the operator credential when starting runner authorization or cleanup."""
        gateway = self.settings.gateway
        return Operator(gateway.url, private.read(gateway.operator_token_path, (CHECKOUT,)).decode().strip())

    def consumed(self, snapshot, controller):
        """The pre-tick snapshot with the comments the controller actually read, when it read them.

        A comment posted between the two reads was processed, so it must not look new afterwards;
        one posted after the controller's read was not, so it still wakes the task.
        """
        comments = getattr(controller, "consumed_comments", None)
        if snapshot is None or comments is None:
            return snapshot
        return {**snapshot, "comments": self.reconciler.comment_input(comments)}

    def _acknowledge(self, key, snapshot, controller):
        """Acknowledge the pre-tick input with the controller's resulting stage."""
        self.registry.acknowledge(key, {**snapshot, "_stage": controller.state["stage"] if controller.state else None})

    def _facade(self, repository, issue, generation, task_key=None):
        """Build a read/write facade, optionally bound to another repository's task."""
        options = {"task_key": task_key} if task_key is not None else {}
        return GatewayGitHub(repository, issue, generation, self.gateway, self.reader(repository), **options)

    def executor(self, repository, github):
        """The task's role jobs, dispatched in its fork at the recorded configuration commit."""
        gateway = self.settings.gateway
        operator = self._operator()
        request = {"url": gateway.url, "headers": gateway.headers}
        binding = self.registry.binding(github.key)
        if binding is None:
            raise Blocked("runner task has no recorded target binding")
        fork = binding.fork
        with self.guard:
            claims = self.claims.get(fork)
        if claims is None:
            claims = runner_claims(self.reader(fork), fork, gateway.workflow_ref, self.bot_login)
            with self.guard:
                self.claims[fork] = claims
        task = {
            "repository": github.key[0],
            "issue": github.key[1],
            "generation": github.generation,
            "upstream": binding.upstream,
            "fork": fork,
            "config_sha": binding.config_sha,
            # The exchange hands this commit to the launcher and refuses a runner on any other.
            "engine": self.settings.engine,
        }
        return RunnerExecutor(
            operator,
            request,
            claims,
            list(gateway.models),
            gateway.budget,
            lambda session, labels: self.gateway.dispatch_runner(github.key, github.generation, session, labels),
            task=task,
            check_owner=lambda: self.gateway.check_owner(github.key, github.generation),
            runner_status=lambda observation: runner_termination(
                self.reader(fork), fork, binding.config_sha, observation
            ),
            recorder=Recorder(self.diagnostics, task) if self.diagnostics else None,
        )

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
                facade = self._facade(repository, issue, generation)
                executor = self.make_executor(repository, facade)
                resolve(target.config, provider, "plan_qc")
                agents = self.agents(target.config, executor, bundle=target.bundle)
                base = head(self.reader(target.config["upstream"]), target.config["upstream"])
                from design_eval import evaluate

                return evaluate(
                    agents, target.config, provider, base, self.settings.state_dir / "evaluations" / uuid.uuid4().hex
                )
        finally:
            self.gateway.release(key, generation, None)
            self.cleanup_runners()

    def cleanup_runners(self):
        """Revoke stale jobs and sweep only recorded session refs, preserving task queues and outcomes."""
        operator = self._operator()
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
                ref = "refs/heads/" + RUN_BRANCH_PREFIX + session
                refs = self.reader(target.fork).request(
                    "GET", root + "matching-refs/heads/" + RUN_BRANCH_PREFIX + session
                )
                found = [item for item in refs if item["ref"] == ref]
                if found:
                    recorded = self.registry.runner_session(session)
                    if found[0]["object"]["sha"] != recorded["config_sha"]:
                        log.warning("session ref %s moved; left for operator reconciliation", session)
                        continue
                    self.gateway.mutate(key, grant, "DELETE", root + ref)
                self.registry.remove_runner(session)
            except (Exception, GatewayStop) as error:
                log.warning("runner cleanup failed (%s)", type(error).__name__)
            finally:
                self.gateway.release(key, grant, None)

    def load(self, repository, issue=None, pin=False, *, latest=False, config_sha=None):
        """Read the fork configuration pinned for this task; new tasks use its current head."""
        target = self.targets[repository]
        key = (repository, issue)
        binding = self.registry.binding(key) if issue is not None else None
        if binding and (binding.upstream, binding.fork) != (target.upstream, target.fork):
            raise Blocked("task target changed; reconcile before continuing")
        loaded = load_target(
            target,
            self.reader(target.fork),
            self.settings.engine,
            self.bot_login,
            self.engine_root,
            config_sha or (binding.config_sha if binding and not latest else None),
        )
        if issue is not None and pin:
            bound = self.registry.bind(key, target.upstream, target.fork, loaded.config_sha)
            if bound.config_sha != loaded.config_sha:
                loaded = load_target(
                    target,
                    self.reader(target.fork),
                    self.settings.engine,
                    self.bot_login,
                    self.engine_root,
                    bound.config_sha,
                )
        config = {
            **loaded.config,
            "repository": repository,
            "upstream": target.upstream,
            "fork": target.fork,
            "config_sha": loaded.config_sha,
        }
        return Target(config, loaded.bundle, loaded.config_sha)

    def resume_config(self, key, generation, state):
        """Recover the execution pin from the durable checkpoint before any new runner dispatch."""
        receipt = state.get("bundle_resume", {})
        sha = receipt.get("config_sha")
        if not sha:
            return
        self.gateway.check_owner(key, generation)
        target = self.load(*key, config_sha=sha)
        if config_digest(target.config) != receipt["config_digest"] or state.get("config_sha") != sha:
            raise Blocked("saved execution configuration does not match its pinned commit")
        self.registry.adopt_config(key, generation, receipt.get("old_config_sha"), sha)

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
        durably, so input arriving during the tick is never acknowledged unseen. It is read
        afresh, and its comments are replaced by those the controller consumed: otherwise input
        the engine processed could be missing, and its late observation would wake the task again.
        """
        with self.renewing(key, generation):
            controller = snapshot = None
            try:
                snapshot = self.reconciler.snapshot(key, cached=False)
                target = self.load(key[0], key[1])
                facade = self._facade(key[0], key[1], generation)
                code = self._facade(target.config["fork"], key[1], generation, task_key=key)
                pulls = self._facade(target.config["upstream"], key[1], generation, task_key=key)
                executor = self.make_executor(key[0], facade)
                agents = self.agents(
                    target.config,
                    executor,
                    bundle=target.bundle,
                    retained=self.settings.state_dir / "sessions" / key[0].replace("/", "__"),
                )

                def latest_settings():
                    """Freeze the default-branch configuration only for an authorized resume command."""
                    latest = self.load(*key, latest=True)
                    return latest.config, latest.bundle

                controller = Controller(
                    facade,
                    agents,
                    target.config,
                    bundle=target.bundle,
                    controller_id=self.settings.controller_id,
                    code=code,
                    pulls=pulls,
                    report_cache=self.report_cache,
                    resume_target=latest_settings,
                    resume_config=lambda state: self.resume_config(key, generation, state),
                    enroll=lambda: self.registry.bind(
                        key, target.config["upstream"], target.config["fork"], target.config_sha
                    ),
                    delegation_key=self.delegation_key,
                )
                outcome = controller.advance(key[1])
                if controller.state:
                    sync_checkpoint_health(facade, target.config, key[1], failed=False)
                    if controller.state.get("pr"):
                        self.registry.link(key, controller.state["pr"], target.config["upstream"])
            except (Exception, GatewayStop) as error:
                return self.failed(key, generation, error, controller, self.consumed(snapshot, controller))
            stop = self.gateway.stop(key, generation)
            if stop is not None:
                return stop
            self._acknowledge(key, self.consumed(snapshot, controller), controller)
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
                result = self._suspend_failure(controller, key[1], error, result, attempt)
            elif action == "health":
                self._failure_health(controller, key[1], error)
        except GatewayStop as stop:
            return self.gateway.stop(key, generation) or stop
        if result.kind == "blocked":
            # Suspended durably: the pre-tick input is handled, and only new input wakes the task.
            self._acknowledge(key, snapshot, controller)
        elif result.kind == "wait-until" and snapshot is not None:
            # Polling must not run this input again before its retry is due.
            self.registry.defer(key, snapshot)
        with self.guard:
            if result.kind == "wait-until":
                self.failures[key] = attempt + 1
            else:
                self.failures.pop(key, None)
        return result

    def _suspend_failure(self, controller, issue, error, result, attempt):
        """Suspend a failed tick, reporting display failures before reclassifying them."""
        reason = str(error) if isinstance(error, Blocked) else "Transport or schema failure; inspect service health."
        try:
            # The Blocked comment is posted now; a publication failure has the health notice instead.
            controller.suspend(
                "blocked", reason, outboxes=False, publish_events=not isinstance(error, PublicationError)
            )
        except Exception as display_error:
            diagnostic = (
                display_error if isinstance(display_error, CheckpointError) else CheckpointError("failure_display")
            )
            self._failure_health(controller, issue, diagnostic)
            result = failures.classify(display_error, attempt=attempt, verified=False)
        else:
            if isinstance(error, CheckpointError):
                self._failure_health(controller, issue, error)
        return result

    def _failure_health(self, controller, issue, error):
        """Publish a checkpoint diagnostic under the caller's effect exception boundary."""
        sync_checkpoint_health(controller.gh, controller.config, issue, failed=True, error=error)


def describe(error):
    """Loggable failure text: fixed engine and gateway texts only, never raw provider output."""
    if isinstance(error, (Blocked, GatewayStop, Transient)):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__
