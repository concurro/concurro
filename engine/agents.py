"""Model calls as turns: a gateway session key and native tools on a real workspace, here or on a runner."""

import base64
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import protocol
import review_timing
import run_display
from bundle import Bundle
from credentials import Credential
from github import Transient
from model_config import resolve
from policy import Blocked, RunnerTerminated, SessionFailure, UncertainOutcome, digest, runner_bound, turn_label
from relay import WAIT, ExchangeError
from runtime_defaults import DEFAULT_RETAINED_BYTES, DEFAULT_TURN_SECONDS as DEFAULT_TURN_SECONDS
from snapshot import inspection
from vocab import TurnOutcome

# Providers whose CLIs the gateway serves natively.
PROVIDERS = ("claude", "codex")
ADAPTER = "gateway-key"
# A runner turn may also wait for a runner to be dispatched, to start and finish target setup.
QUEUE_SECONDS = 1800
SUBMIT_ATTEMPTS = 4
REVIEW_POLL_SECONDS = 5
ADMIT_RETRY_SECONDS = 15
POLL_BACKOFF_SECONDS = 15
POLL_BACKOFF_CAP = 60
# Non-job-bound stages tolerate five minutes of consecutive polling failures (20 × 15s).
MAX_POLL_FAILURES = 20
OWNERSHIP_CHECK_SECONDS = 15
DEFAULT_LEASE_SECONDS = 6 * 3600
LEASE_MARGIN = 300
SCOPE = re.compile(protocol.DIGEST_PATTERN)

EXCHANGE_RETRY_SUFFIX = "Inspect the exchange and runner before a writer retries; no automatic restart was attempted."
RUNNER_RETRY_SUFFIX = "Inspect the runner before a writer retries; no automatic restart was attempted."
ADMISSION_MESSAGES = {
    TurnOutcome.ADMISSION_UNKNOWN: "{label} admission outcome is unknown; no start was confirmed.",
    TurnOutcome.QUEUE_EXPIRED: "No {lower_label} start was observed within the {queue_minutes}-minute dispatch/setup limit.",
}
TERMINATION_MESSAGES = {
    "turn_timeout": "reached Concurro's execution time limit",
    "runner_stopped": "was stopped",
    "process_signal": "was terminated by a signal",
}


class GatewayUnavailable(Transient):
    """The gateway's exchange could not be reached or refused a key; nothing ran. Retry later."""

    TEXT = {"unavailable": "Inference gateway unavailable; retry later."}


class Retained:
    """Each cooperative role's transcript archive, kept by the controller between turns."""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, session):
        if not SCOPE.fullmatch(session.get("scope", "")) or type(session.get("turn")) is not int:
            raise Blocked("invalid retained session identity")
        return self.root / (session["scope"] + ".tar.gz")

    def load(self, session):
        path = self.path(session)
        return base64.b64encode(path.read_bytes()).decode() if path.exists() else None

    def save(self, session, archive):
        path = self.path(session)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(base64.b64decode(archive, validate=True))
        os.replace(temporary, path)


class Agents:
    """Run each model call as a turn through an executor; every call holds only a gateway key."""

    def __init__(self, config, executor, bundle=None, retained=None):
        """`executor` runs turns (`RunnerExecutor`); `retained` is a directory for transcripts.

        `bundle` is the controller's own instance; by default it is built from the working directory.
        """
        self.timing = None
        self.config, self.executor = config, executor
        self.bundle = bundle or Bundle()
        self.retained = Retained(retained) if retained else None

    def credential(self, provider, stage, task):
        """Every call uses the gateway; the identity names the provider route, never a secret."""
        try:
            provider = resolve(self.config, provider, stage).profile.harness
        except ValueError:
            raise Blocked(f"{provider} has no inference gateway route") from None
        if provider not in PROVIDERS:
            raise Blocked(f"{provider} has no inference gateway route")
        return Credential("gateway:" + provider, provider, "org", ADAPTER)

    @property
    def parallel(self):
        """Whether the executor supports independent nonblocking turns."""
        return hasattr(self.executor, "submit") and hasattr(self.executor, "observe")

    @property
    def generation(self):
        """The current service ownership generation, retained only as attempt provenance."""
        return self.executor.task.get("generation") if self.executor.task else None

    def prepare(self, provider, stage, context):
        """Prepare a validated turn payload without dispatching or changing controller state."""
        self.timing = None
        context = dict(context)
        session = context.pop("controller_session", None)
        selected = context.pop("controller_credential", None)
        candidate = context.pop("controller_candidate", None)
        feedback = context.pop("controller_feedback", {})
        evaluation = context.pop("controller_evaluation", None)
        if evaluation is not None and (
            stage != "plan_qc" or session is not None or not re.fullmatch(r"[a-f0-9]{32}", evaluation)
        ):
            raise Blocked("evaluation requires a fresh design-review turn")
        agent = resolve(self.config, provider, stage, context.get("reviewer_identity"))
        model = agent.profile.model
        if not model.strip():
            raise Blocked("live inference requires an explicit model ID for every participating role")
        expected = self.credential(provider, stage, None)
        if selected is not None and Credential.parse(selected) != expected:
            raise Blocked("selected credential belongs to another provider or route")
        if context.get("bundle_id") != self.bundle.identity:
            raise Blocked("agent transport bundle differs from the approved bundle")
        if session and not self.retained:
            raise Blocked("retained sessions need a transcript store")
        request = {
            "provider": agent.profile.harness,
            "model": model,
            "effort": agent.profile.effort,
            **({"agent": agent.record()} if "agents" in self.config else {}),
            "stage": stage,
            "gateway": self.executor.gateway,
            "bundle_id": self.bundle.identity,
            "charter": self.bundle.charter_text,
            "instruction_texts": self.bundle.instruction_texts,
            "max_retained_bytes": self.config.get("max_retained_bytes", DEFAULT_RETAINED_BYTES),
            "context": context,
            "session": session,
            "local_candidate": candidate is not None,
            "evaluation": evaluation,
        }
        turn = {
            "request": request,
            "feedback": feedback,
            "spec": inspection(context.get("repository"), candidate, stage, self.config["base"], self.config),
            "retained": self.retained.load(session) if session else None,
            "timeout": None if runner_bound(stage) else self.config.get("turn_timeout_seconds", DEFAULT_TURN_SECONDS),
        }
        return turn

    def decode(self, turn, result):
        """Persist the transcript and decode one result, independently of other turns."""
        request = turn["request"]
        session, provider, stage = request["session"], request["provider"], request["stage"]
        return self.decode_result(session, provider, stage, result)

    def decode_result(self, session, provider, stage, result):
        """Share result validation between serial and batch execution."""
        if session and result.get("retained") is not None:
            self.retained.save(session, result["retained"])
        if runner_bound(stage) and result["outcome"] in (TurnOutcome.ADMISSION_UNKNOWN, TurnOutcome.QUEUE_EXPIRED):
            message = ADMISSION_MESSAGES[result["outcome"]].format(
                label=turn_label(stage), lower_label=turn_label(stage).lower(), queue_minutes=QUEUE_SECONDS // 60
            )
            raise RunnerTerminated(
                message + " Withdrawal was attempted, but admission or cancellation can race "
                "with execution. " + EXCHANGE_RETRY_SUFFIX
            )
        if runner_bound(stage) and result["outcome"] in (
            TurnOutcome.TIMED_OUT,
            TurnOutcome.TERMINATED,
            TurnOutcome.LOST,
            TurnOutcome.UNCERTAIN,
        ):
            cause = result.get("termination_reason")
            reason = (
                TERMINATION_MESSAGES["turn_timeout"]
                if result["outcome"] == TurnOutcome.TIMED_OUT
                else TERMINATION_MESSAGES.get(
                    cause if isinstance(cause, str) else None, "ended; the cause is unconfirmed"
                )
            )
            seconds = result.get("elapsed_seconds")
            duration = (
                f" after {seconds:.0f}s of turn time" if type(seconds) in (int, float) and 0 <= seconds <= 86400 else ""
            )
            raise RunnerTerminated(
                f"{turn_label(stage)} {reason}{duration}. No result was saved; execution may be incomplete. "
                + RUNNER_RETRY_SUFFIX
            )
        if result["outcome"] != TurnOutcome.OK:
            uncertain = result["outcome"] in (TurnOutcome.UNCERTAIN, TurnOutcome.LOST, TurnOutcome.TIMED_OUT)
            if result["outcome"] == TurnOutcome.TIMED_OUT:
                message = "Concurro execution time limit reached; no result was saved."
                if session:
                    raise SessionFailure(message, uncertain=True)
                raise UncertainOutcome(message)
            if session:
                raise SessionFailure(
                    "provider session failed; output suppressed to protect credentials", uncertain=uncertain
                )
            if uncertain:
                raise UncertainOutcome(f"{provider} outcome is unknown; output suppressed")
            raise Blocked(f"{provider} failed; output suppressed to protect credentials")
        response = result["response"]
        if stage == "develop" and isinstance(response, dict):
            return workspace_response(response, result.get("workspace"))
        return response

    def submit(self, turn):
        """Admit one prepared turn without waiting for completion."""
        return self.executor.submit(turn)

    def observe(self, handle):
        """Poll one live turn without a long poll."""
        return self.executor.observe(handle)

    def cancel(self, handle):
        """Cancel only this turn and its runner session."""
        return self.executor.cancel(handle)

    def check_owner(self):
        """Fence result application as well as transport operations."""
        return self.executor.check_owner()

    def idle(self):
        """Bound polling frequency without holding controller or transport locks."""
        self.executor.sleep(REVIEW_POLL_SECONDS)

    def ask(self, provider, stage, context):
        """Synchronous wrapper for single-agent stages and legacy integrations."""
        turn = self.prepare(provider, stage, context)
        try:
            result = self.executor.run(turn, f"{self.config['repository']}:{stage}")
        finally:
            self.timing = review_timing.normalize(getattr(self.executor, "timing", None))
        return self.decode(turn, result)


def workspace_response(response, workspace):
    """The implementer's report with the edits it made in its workspace; a design issue keeps none."""
    if not isinstance(workspace, dict):
        raise Blocked("implementation turn returned no workspace changes")
    if workspace.get("error") and response.get("design_issue") is None:
        return {**response, "edits": [], "workspace_error": str(workspace["error"])[:1000]}
    edits = (
        []
        if response.get("design_issue") is not None
        else [*workspace["edits"], *({"path": path, "content": None} for path in workspace["removed"])]
    )
    return {**{k: v for k, v in response.items() if k != "edits"}, "edits": edits}


@dataclass
class TurnHandle:
    """One in-process turn; exact request bytes are deliberately not crash-recoverable."""

    identifier: str
    turn: dict
    deadline: float | None
    group: str
    sessions: set = field(default_factory=set)
    result: dict | None = None
    timing: dict | None = None
    failures: int = 0
    next_poll: float = 0
    status: str = "queued"
    job_bound: bool = field(init=False)

    def __post_init__(self):
        """Pin the lifetime policy for this handle's prepared turn."""
        self.job_bound = runner_bound(self.turn["request"]["stage"])


class RunnerExecutor:
    """Turns on GitHub-hosted runners of the target, queued at the exchange and served by runner sessions."""

    def __init__(
        self,
        operator,
        gateway,
        expected,
        models,
        budget,
        dispatch,
        lease=DEFAULT_LEASE_SECONDS,
        clock=time.monotonic,
        sleep=time.sleep,
        task=None,
        check_owner=None,
        runner_status=None,
        recorder=None,
    ):
        """`expected` holds OIDC claims; `dispatch(session, labels)` starts a labeled runner.

        `recorder`, when given, observes each turn's submission, exchange observations and result.
        """
        self.operator, self.gateway, self.expected = operator, gateway, expected
        self.models, self.budget, self.dispatch, self.lease = models, budget, dispatch, lease
        self.clock, self.sleep, self.task = clock, sleep, task
        self.check_owner = check_owner or (lambda: None)
        self.runner_status = runner_status or (lambda observation: None)
        self.recorder = recorder
        self.sessions = set()
        self.groups = {}
        self.timing = None

    def binding(self, turn, identifier=None):
        """The same task/role/profile binding selects staffing and scoped cleanup."""
        request = turn.get("request", {})
        model = request.get("model", "")
        if self.task is None or not model or not any(fnmatchcase(model, allowed) for allowed in self.models):
            raise Blocked("runner turn lacks its task binding or allowed model")
        role = str(request.get("stage")) + ":" + str(request.get("provider"))
        base = request.get("context", {}).get("base") or turn["spec"]["refs"][turn["spec"]["base_branch"]]
        phase = (
            request.get("evaluation")
            or (request.get("session") or {}).get("scope")
            or request.get("context", {}).get("review_phase", "single")
        )
        if request.get("agent"):
            # Staffing includes the resolved profile, even for fresh turns on the same CLI.
            phase = digest([phase, request["agent"]])
        if runner_bound(request.get("stage")):
            # Each long-running attempt gets a fresh setup/job clock.
            phase = digest([phase, identifier])
        binding = {**self.task, "base": base, "role": role, "phase": phase}
        return binding, model

    def admit(self, identifier, turn, pin=None):
        """Queue the turn under its own ID; the reply, or None when its admission stays unknown.

        A refusal admitted nothing. A lost reply may have admitted it, so the same turn is
        submitted again, which returns the original admission rather than a second turn.
        """
        binding, model = self.binding(turn, identifier)
        # Credentials outlive dispatch/setup plus the job, but still expire and need heartbeats.
        lease = self.lease + QUEUE_SECONDS + LEASE_MARGIN if runner_bound(turn["request"]["stage"]) else self.lease
        for attempt in range(SUBMIT_ATTEMPTS):
            if attempt:
                self.sleep(ADMIT_RETRY_SECONDS)
            try:
                return self.operator.submit(identifier, self.expected, [model], self.budget, lease, turn, pin, binding)
            except ExchangeError as error:
                if 400 <= error.status < 500 and error.status not in (408, 429):
                    raise GatewayUnavailable("unavailable") from None
        return None

    def submit(self, turn, pin=None, identifier=None):
        """Admit and dispatch once; return a handle without waiting for the model."""
        self.check_owner()
        identifier = identifier or str(uuid.uuid4())
        binding, model = self.binding(turn, identifier)
        group = digest([binding, model])
        handle = TurnHandle(
            identifier,
            turn,
            self.clock() + (turn["timeout"] or 0) + QUEUE_SECONDS,
            group,
            set(self.groups.get(group, ())),
        )
        self.record("submitted", identifier, turn)
        try:
            reply = self.admit(handle.identifier, turn, pin)
            if reply is None:
                self.withdraw(handle.identifier)
                self.settle(
                    handle, self.lost(TurnOutcome.ADMISSION_UNKNOWN if handle.job_bound else TurnOutcome.UNCERTAIN)
                )
            else:
                self.receive(handle, reply)
            return handle
        except BaseException:
            self.cancel_quietly(handle)
            raise

    @staticmethod
    def lost(outcome):
        """An absent result cannot authorize work."""
        return {"outcome": outcome, "response": None, "retained": None, "workspace": None}

    def record(self, event, *args):
        """Tell the recorder, if any; it never affects the turn."""
        if self.recorder is not None:
            getattr(self.recorder, event)(*args)

    def settle(self, handle, result):
        """Set the handle's final result once and record it."""
        handle.result = result
        self.record("finished", handle.identifier, result)

    def receive(self, handle, reply):
        """Update one observation; dispatch and retain no sibling's session."""
        self.check_owner()
        observed = review_timing.normalize(reply)
        if observed and observed["turn"] == handle.identifier:
            handle.timing = observed
        handle.status = reply.get("status", handle.status)
        if (handle.job_bound or handle.turn["timeout"] is None) and handle.status == "running":
            handle.deadline = None
        session = reply.get("dispatch")
        if session and session not in handle.sessions:
            handle.sessions.add(session)
            self.groups.setdefault(handle.group, set()).add(session)
            self.start(session, handle.turn)
        self.record("observed", handle.identifier, handle.status, handle.timing, handle.sessions)
        if reply.get("refusal"):
            raise Blocked("a runner of the target was refused by the gateway: " + reply["refusal"])
        if reply.get("status") == "done" and "result" in reply:
            self.settle(handle, reply["result"])
        elif reply.get("status") in ("lost", "cancelled"):
            if handle.job_bound:
                detail = self.runner_status(reply)
                raise RunnerTerminated(
                    detail
                    or f"{turn_label(handle.turn['request']['stage'])} runner stopped reporting or was ended; no result was saved. "
                    "The cause is unconfirmed. Inspect the runner before a writer retries."
                )
            self.settle(handle, self.lost(TurnOutcome.LOST))

    def observe(self, handle, wait=0):
        """One bounded observation; default zero-wait permits round-robin polling."""
        self.check_owner()
        if handle.result is not None:
            return handle.result
        if not handle.job_bound and handle.deadline is not None and self.clock() > handle.deadline:
            self.withdraw(handle.identifier)
            self.settle(handle, self.lost(TurnOutcome.UNCERTAIN))
            return handle.result
        if self.clock() < handle.next_poll:
            return None
        try:
            reply = self.operator.poll(handle.identifier, wait=wait)
            handle.failures = 0
        except ExchangeError as error:
            # Poll refusal includes 3xx; relay.SESSION_GONE is a separate, narrower runner-session rule.
            if handle.job_bound and not (error.status in (0, 408, 429) or 500 <= error.status <= 599):
                raise RunnerTerminated(
                    f"{turn_label(handle.turn['request']['stage'])} status could not be read: exchange refused polling "
                    f"(HTTP {error.status}). Execution status is unknown. " + EXCHANGE_RETRY_SUFFIX
                ) from None
            handle.failures += 1
            delay = (
                min(POLL_BACKOFF_CAP, POLL_BACKOFF_SECONDS * 2 ** min(handle.failures - 1, 2))
                if handle.job_bound
                else POLL_BACKOFF_SECONDS
            )
            handle.next_poll = self.clock() + delay
            # Controller connectivity is not evidence of runner death. The runner may
            # still be heartbeating and delivering a result through a different path.
            if not handle.job_bound and handle.failures > MAX_POLL_FAILURES:
                self.settle(handle, self.lost(TurnOutcome.UNCERTAIN))
            return handle.result
        self.receive(handle, reply)
        # Observe first: a queued turn may have started since our last poll.
        if (
            handle.job_bound
            and handle.result is None
            and handle.status == "queued"
            and handle.deadline is not None
            and self.clock() > handle.deadline
        ):
            self.withdraw(handle.identifier)
            self.settle(handle, self.lost(TurnOutcome.QUEUE_EXPIRED))
        return handle.result

    def cancel(self, handle):
        """Withdraw this attempt and retire only its authorized staffing group."""
        error = None
        try:
            self.operator.cancel(handle.identifier)
        except ExchangeError as failed:
            error = failed
        for session in handle.sessions:
            try:
                self.operator.end(session)
                self.groups.get(handle.group, set()).discard(session)
            except ExchangeError as failed:
                error = failed
        if error:
            raise GatewayUnavailable("unavailable") from None

    def cancel_quietly(self, handle):
        """Best-effort cleanup must not mask an existing transport or ownership failure."""
        try:
            self.cancel(handle)
        except (ExchangeError, GatewayUnavailable):
            pass

    def run(self, turn, label, pin=None):
        """Synchronous wrapper, retaining legacy transport retry behavior."""
        self.timing = None
        handle = self.submit(turn, pin)
        try:
            while True:
                result = self.observe(handle, wait=WAIT)
                self.timing = handle.timing
                if result is not None:
                    if (
                        result.get("outcome") == TurnOutcome.TIMED_OUT
                        or handle.job_bound
                        and result.get("outcome")
                        in (
                            TurnOutcome.LOST,
                            TurnOutcome.UNCERTAIN,
                            TurnOutcome.TERMINATED,
                            TurnOutcome.ADMISSION_UNKNOWN,
                            TurnOutcome.QUEUE_EXPIRED,
                        )
                    ):
                        self.cancel_quietly(handle)
                    return result
                delay = max(0, handle.next_poll - self.clock())
                while delay > 0:
                    self.check_owner()
                    interval = min(OWNERSHIP_CHECK_SECONDS, delay)
                    self.sleep(interval)
                    delay -= interval
                handle.next_poll = 0
        except BaseException:
            self.cancel_quietly(handle)
            raise
        finally:
            self.timing = handle.timing

    def start(self, session, turn):
        """Dispatch the runner issued for this turn."""
        self.sessions.add(session)
        self.dispatch(session, run_display.labels(turn["request"], self.task))

    def withdraw(self, identifier):
        """Best-effort withdrawal for an uncertain serial admission."""
        try:
            self.operator.cancel(identifier)
        except ExchangeError:
            pass
