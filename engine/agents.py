"""Model calls as turns: a gateway session key and native tools on a real workspace, here or on a runner."""

import base64
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from bundle import Bundle
from credentials import Credential
from model_config import resolve
from github import Transient
from policy import Blocked, SessionFailure, UncertainOutcome, digest
from relay import ExchangeError, Operator
from snapshot import inspection
import turns
import review_timing

# Providers whose CLIs the gateway serves natively.
PROVIDERS = ("claude", "codex")
ADAPTER = "gateway-key"
# How long a turn may run; implementation builds and tests, the others read and reason.
TURN_SECONDS = {"develop": 1800}
DEFAULT_TURN_SECONDS = 1200
# A runner turn may also wait for a runner to be dispatched, to start and to build its image.
QUEUE_SECONDS = 1800
SUBMIT_ATTEMPTS = 4
SCOPE = re.compile(r"[a-f0-9]{64}")


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

    def ask(self, provider, stage, context):
        """Return one JSON response; for `develop`, its edits are the files changed in the workspace."""
        self.timing = None
        context = dict(context)
        session = context.pop("controller_session", None)
        selected = context.pop("controller_credential", None)
        candidate = context.pop("controller_candidate", None)
        feedback = context.pop("controller_feedback", {})
        evaluation = context.pop("controller_evaluation", None)
        if evaluation is not None and (stage != "plan_qc" or session is not None
                                       or not re.fullmatch(r"[a-f0-9]{32}", evaluation)):
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
        request = {"provider": agent.profile.harness, "model": model, "effort": agent.profile.effort,
                   **({"agent": agent.record()} if "agents" in self.config else {}), "stage": stage, "gateway": self.executor.gateway,
                   "bundle_id": self.bundle.identity, "charter": self.bundle.charter_text, "instruction_texts": self.bundle.instruction_texts,
                   "context": context, "session": session, "local_candidate": candidate is not None, "evaluation": evaluation}
        turn = {"request": request, "feedback": feedback, "spec": inspection(context.get("repository"), candidate, stage, self.config["base"]),
                "retained": self.retained.load(session) if session else None,
                "timeout": TURN_SECONDS.get(stage, DEFAULT_TURN_SECONDS)}
        try:
            result = self.executor.run(turn, f"{self.config['repository']}:{stage}")
        finally:
            self.timing = review_timing.normalize(getattr(self.executor, "timing", None))
        if session and result.get("retained") is not None:
            self.retained.save(session, result["retained"])
        if result["outcome"] != "ok":
            uncertain = result["outcome"] in ("uncertain", "lost")
            if session:
                raise SessionFailure("provider session failed; output suppressed to protect credentials", uncertain=uncertain)
            if uncertain:
                raise UncertainOutcome(f"{provider} outcome is unknown; output suppressed")
            raise Blocked(f"{provider} failed; output suppressed to protect credentials")
        response = result["response"]
        if stage == "develop" and isinstance(response, dict):
            return workspace_response(response, result.get("workspace"))
        return response


def workspace_response(response, workspace):
    """The implementer's report with the edits it made in its workspace; a design issue keeps none."""
    if not isinstance(workspace, dict):
        raise Blocked("implementation turn returned no workspace changes")
    if workspace.get("error") and response.get("design_issue") is None:
        return {**response, "edits": [], "workspace_error": str(workspace["error"])[:1000]}
    edits = [] if response.get("design_issue") is not None else [
        *workspace["edits"], *({"path": path, "content": None} for path in workspace["removed"])]
    return {**{k: v for k, v in response.items() if k != "edits"}, "edits": edits}


class RunnerExecutor:
    """Turns on GitHub-hosted runners of the target, queued at the exchange and served by runner sessions."""

    def __init__(self, operator, gateway, expected, models, budget, dispatch, lease=6 * 3600, clock=time.monotonic,
                 sleep=time.sleep, task=None, check_owner=None):
        """`expected` are the OIDC claims a runner of this target must present; `dispatch(session)` starts one."""
        self.operator, self.gateway, self.expected = operator, gateway, expected
        self.models, self.budget, self.dispatch, self.lease = models, budget, dispatch, lease
        self.clock, self.sleep, self.task = clock, sleep, task
        self.check_owner = check_owner or (lambda: None)
        self.sessions = set()
        self.timing = None

    def admit(self, identifier, turn, pin=None):
        """Queue the turn under its own ID; the reply, or None when its admission stays unknown.

        A refusal admitted nothing. A lost reply may have admitted it, so the same turn is
        submitted again, which returns the original admission rather than a second turn.
        """
        request = turn.get("request", {})
        model = request.get("model", "")
        if self.task is None or not model or not any(fnmatchcase(model, allowed) for allowed in self.models):
            raise Blocked("runner turn lacks its task binding or allowed model")
        role = str(request.get("stage")) + ":" + str(request.get("provider"))
        base = request.get("context", {}).get("base") or turn["spec"]["refs"][turn["spec"]["base_branch"]]
        phase = request.get("evaluation") or (request.get("session") or {}).get("scope") or request.get("context", {}).get("review_phase", "single")
        if request.get("agent"):
            # Staffing includes the resolved profile, even for fresh turns on the same CLI.
            phase = digest([phase, request["agent"]])
        binding = {**self.task, "base": base, "role": role, "phase": phase}
        for attempt in range(SUBMIT_ATTEMPTS):
            if attempt:
                self.sleep(15)
            try:
                return self.operator.submit(identifier, self.expected, [model], self.budget, self.lease, turn, pin, binding)
            except ExchangeError as error:
                if 400 <= error.status < 500 and error.status not in (408, 429):
                    raise GatewayUnavailable("unavailable") from None
        return None

    def run(self, turn, label, pin=None):
        """Queue the turn (for the runner session `pin` alone, when given) and wait for its result."""
        self.timing = None
        identifier = str(uuid.uuid4())
        try:
            return self.wait(identifier, turn, pin)
        except BaseException:
            self.withdraw(identifier)
            for session in self.sessions:
                try:
                    self.operator.end(session)
                except ExchangeError:
                    pass
            raise

    def wait(self, identifier, turn, pin):
        """Wait only while this generation owns the task; completed stale results are discarded."""
        self.check_owner()
        submitted = self.admit(identifier, turn, pin)
        if submitted is None:
            self.withdraw(identifier)
            return {"outcome": "uncertain", "response": None, "retained": None, "workspace": None}
        deadline = self.clock() + turn["timeout"] + QUEUE_SECONDS
        reply, failures, started = submitted, 0, set()
        while True:
            self.check_owner()
            observed = review_timing.normalize(reply)
            if observed and observed["turn"] == identifier:
                self.timing = observed
            if reply.get("dispatch") and reply["dispatch"] not in started:
                started.add(reply["dispatch"])
                self.start(reply["dispatch"], identifier)
            if reply.get("refusal"):
                self.withdraw(identifier)
                raise Blocked("a runner of the target was refused by the gateway: " + reply["refusal"])
            if reply.get("status") == "done" and "result" in reply:
                return reply["result"]
            if reply.get("status") in ("lost", "cancelled"):
                return {"outcome": "lost", "response": None, "retained": None, "workspace": None}
            if self.clock() > deadline:
                self.withdraw(identifier)
                return {"outcome": "uncertain", "response": None, "retained": None, "workspace": None}
            try:
                reply = self.operator.poll(identifier)
                failures = 0
            except ExchangeError:
                failures += 1
                reply = {}
                if failures > 20:
                    return {"outcome": "uncertain", "response": None, "retained": None, "workspace": None}
                self.sleep(15)

    def start(self, session, identifier):
        """Dispatch a runner for a new session; a failed dispatch ends the session and withdraws the turn."""
        self.sessions.add(session)
        self.dispatch(session)

    def withdraw(self, identifier):
        try:
            self.operator.cancel(identifier)
        except ExchangeError:
            pass
