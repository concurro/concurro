"""Model calls as turns: a gateway session key and native tools on a real workspace, here or on a runner."""

import base64
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from bundle import Bundle
from credentials import Credential
from github import Transient
from mirror import Mirror, default_root
from policy import Blocked, IMAGE, SessionFailure, UncertainOutcome, selected_model
from relay import ExchangeError, Operator
from snapshot import inspection
import turns

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
        """`executor` runs turns (`HostExecutor` or `RunnerExecutor`); `retained` is a directory for transcripts.

        `bundle` is the controller's own instance; by default it is built from the working directory.
        """
        self.config, self.executor = config, executor
        self.bundle = bundle or Bundle(profiles=config["execution_profiles"])
        self.retained = Retained(retained) if retained else None

    def credential(self, provider, stage, task):
        """Every call uses the gateway; the identity names the provider route, never a secret."""
        if provider not in PROVIDERS:
            raise Blocked(f"{provider} has no inference gateway route")
        return Credential("gateway:" + provider, provider, "org", ADAPTER)

    def ask(self, provider, stage, context):
        """Return one JSON response; for `develop`, its edits are the files changed in the workspace."""
        context = dict(context)
        session = context.pop("controller_session", None)
        selected = context.pop("controller_credential", None)
        candidate = context.pop("controller_candidate", None)
        model = (context["reviewer_identity"]["model"] if stage == "plan_qc"
                 else selected_model(self.config, provider, stage))
        if not model.strip():
            raise Blocked("live inference requires an explicit model ID for every participating role")
        expected = self.credential(provider, stage, None)
        if selected is not None and Credential.parse(selected) != expected:
            raise Blocked("selected credential belongs to another provider or route")
        if context.get("bundle_id") != self.bundle.identity:
            raise Blocked("agent transport bundle differs from the approved bundle")
        if session and not self.retained:
            raise Blocked("retained sessions need a transcript store")
        request = {"provider": provider, "model": model, "stage": stage, "gateway": self.executor.gateway,
                   "bundle_id": self.bundle.identity, "charter": self.bundle.charter_text,
                   "profiles": self.config["execution_profiles"], "profile_texts": self.bundle.target_profile_texts,
                   "context": context, "session": session}
        turn = {"request": request, "spec": inspection(context.get("repository"), candidate, stage, self.config["base"]),
                "retained": self.retained.load(session) if session else None,
                "timeout": TURN_SECONDS.get(stage, DEFAULT_TURN_SECONDS)}
        result = self.executor.run(turn, f"{self.config['repository']}:{stage}")
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
    if workspace["removed"]:
        raise Blocked("the implementer deleted tracked files, which candidates cannot express yet: "
                      + ", ".join(workspace["removed"][:10]))
    edits = [] if response.get("design_issue") is not None else workspace["edits"]
    return {**{k: v for k, v in response.items() if k != "edits"}, "edits": edits}


class HostExecutor:
    """Turns on this host's Docker: a key minted for each turn and deleted after it."""

    def __init__(self, operator, gateway, image, mirror, root, budget, limits=turns.Limits()):
        """`gateway` is the request's gateway block (`url`, `headers`); `root` holds turn directories."""
        self.operator, self.gateway, self.image, self.mirror = operator, gateway, image, mirror
        self.root, self.budget, self.limits = Path(root), budget, limits

    def run(self, turn, label):
        try:
            grant = self.operator.mint([turn["request"]["model"]], self.budget, turn["timeout"] + 600, label)
        except ExchangeError:
            raise GatewayUnavailable("unavailable") from None
        directory = turns.directory(self.root)
        try:
            return turns.execute(turn, self.image, grant["key"], self.mirror, directory, self.limits)
        except turns.TurnError as error:
            raise Blocked(str(error)) from None
        finally:
            try:
                self.operator.end(grant["session"])
            except ExchangeError:
                pass  # Reconciliation deletes the key once its deadline passes.
            turns.remove(directory)


class RunnerExecutor:
    """Turns on GitHub-hosted runners of the target, queued at the exchange and served by runner sessions."""

    def __init__(self, operator, gateway, expected, models, budget, dispatch, lease=6 * 3600, clock=time.monotonic,
                 sleep=time.sleep):
        """`expected` are the OIDC claims a runner of this target must present; `dispatch(session)` starts one."""
        self.operator, self.gateway, self.expected = operator, gateway, expected
        self.models, self.budget, self.dispatch, self.lease = models, budget, dispatch, lease
        self.clock, self.sleep = clock, sleep

    def admit(self, identifier, turn, pin=None):
        """Queue the turn under its own ID; the reply, or None when its admission stays unknown.

        A refusal admitted nothing. A lost reply may have admitted it, so the same turn is
        submitted again, which returns the original admission rather than a second turn.
        """
        for attempt in range(SUBMIT_ATTEMPTS):
            if attempt:
                self.sleep(15)
            try:
                return self.operator.submit(identifier, self.expected, self.models, self.budget, self.lease, turn, pin)
            except ExchangeError as error:
                if 400 <= error.status < 500 and error.status not in (408, 429):
                    raise GatewayUnavailable("unavailable") from None
        return None

    def run(self, turn, label, pin=None):
        """Queue the turn (for the runner session `pin` alone, when given) and wait for its result."""
        identifier = str(uuid.uuid4())
        submitted = self.admit(identifier, turn, pin)
        if submitted is None:
            self.withdraw(identifier)
            return {"outcome": "uncertain", "response": None, "retained": None, "workspace": None}
        deadline = self.clock() + turn["timeout"] + QUEUE_SECONDS
        reply, failures, started = submitted, 0, set()
        while True:
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
        try:
            self.dispatch(session)
        except BaseException:
            self.withdraw(identifier)
            try:
                self.operator.end(session)
            except ExchangeError:
                pass
            raise

    def withdraw(self, identifier):
        try:
            self.operator.cancel(identifier)
        except ExchangeError:
            pass


def host_agents(config, mirror=None, bundle=None, environ=None):
    """Agents running turns on this host, configured by the environment.

    For the Actions controller, the local pilot and evaluations: `CONCURRO_GATEWAY_URL`,
    `CONCURRO_GATEWAY_TOKEN` (the exchange's operator token) and `CONCURRO_AGENT_IMAGE`, with
    optional `CONCURRO_GATEWAY_HEADERS` (JSON, per provider) and `CONCURRO_GATEWAY_BUDGET`.
    """
    environ = os.environ if environ is None else environ
    url, token, image = (environ.get(k, "") for k in ("CONCURRO_GATEWAY_URL", "CONCURRO_GATEWAY_TOKEN",
                                                      "CONCURRO_AGENT_IMAGE"))
    if not url.startswith("https://") or not token.strip():
        raise Blocked("set CONCURRO_GATEWAY_URL and CONCURRO_GATEWAY_TOKEN: every model call goes through the gateway")
    if not IMAGE.fullmatch(image):
        raise Blocked("CONCURRO_AGENT_IMAGE must use an immutable digest or local image ID")
    try:
        headers = json.loads(environ.get("CONCURRO_GATEWAY_HEADERS") or "{}")
        budget = int(environ.get("CONCURRO_GATEWAY_BUDGET") or 4_000_000)
    except ValueError:
        raise Blocked("CONCURRO_GATEWAY_HEADERS must be JSON and CONCURRO_GATEWAY_BUDGET an integer") from None
    mirror = mirror or Mirror(default_root(), config["repository"])
    scratch = Path(tempfile.gettempdir()) / "concurro"
    executor = HostExecutor(Operator(url.rstrip("/"), token.strip()), {"url": url.rstrip("/"), "headers": headers},
                            image, mirror, scratch / "turns", budget)
    return Agents(config, executor, bundle=bundle, retained=scratch / "sessions" / config["repository"].replace("/", "__"))
