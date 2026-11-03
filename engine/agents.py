"""Run isolated provider containers with optional role-scoped transcript volumes."""

import json
import os
import subprocess
import uuid

import cleanup
from policy import Blocked, KILLED, SessionFailure, UNCERTAIN_EXIT, UncertainOutcome, IMAGE, selected_model
from credentials import Credential, EnvironmentCredentials
from bundle import Bundle
from mirror import Mirror, default_root
from snapshot import Snapshot, Tools, inspection
from sessions import SessionVolume


class Agents:
    """Launch bounded inference without host filesystem mounts or shared role context."""

    def __init__(self, config, mirror=None, credentials=None, bundle=None):
        """Use trusted configuration, an operator-pinned image digest and a credential resolver.

        `bundle` is the controller's own instance; by default it is built from the working directory.
        `mirror` is the target's host-side Git mirror.
        """
        self.config = config
        self.credentials = credentials or EnvironmentCredentials()
        self.bundle = bundle or Bundle(profiles=config["execution_profiles"])
        self.mirror = mirror or Mirror(default_root(), config["repository"])

    def credential(self, provider, stage, task):
        """Select the credential identity for one model call before any secret is read."""
        return self.credentials.resolve(provider, stage, task)

    def ask(self, provider, stage, context):
        """Return one JSON response, killing the container on timeout or interruption."""
        context = dict(context)
        session = context.pop("controller_session", None)
        selected = context.pop("controller_credential", None)
        candidate = context.pop("controller_candidate", None)
        model = (context["reviewer_identity"]["model"] if stage == "plan_qc"
                 else selected_model(self.config, provider, stage))
        if not model.strip():
            raise Blocked("live inference requires an explicit model ID for every participating role")
        image = os.environ.get("CONCURRO_AGENT_IMAGE", "")
        if not IMAGE.fullmatch(image):
            raise Blocked("CONCURRO_AGENT_IMAGE must use an immutable digest or local image ID")
        credential = (Credential.parse(selected) if selected is not None
                      else self.credential(provider, stage, None))
        if credential.provider != provider:
            raise Blocked("selected credential belongs to another provider")
        secrets = self.credentials.material(credential)
        name = "concurro-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--init", "-i", "--name", name,
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=128", "--memory=2g", "--cpus=2", "--user=10001:10001",
                   "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
                   *(arg for variable in sorted(secrets) for arg in ("--env", variable)), image]
        if context.get("bundle_id") != self.bundle.identity:
            raise Blocked("agent transport bundle differs from the approved bundle")
        payload = {"provider": provider, "model": model, "stage": stage,
                   "bundle_id": self.bundle.identity, "charter": self.bundle.charter_text,
                   "profiles": self.config["execution_profiles"], "profile_texts": self.bundle.target_profile_texts,
                   "context": context, "session": session}
        if session:
            volume = SessionVolume(image, session)
            volume.ensure()
            command[-1:-1] = ["--mount", volume.mount()]
        # Docker receives only the selected inference secret. GH_TOKEN and host paths stay out.
        environment = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT")
                       if k in os.environ}
        environment.update(secrets)
        spec = inspection(context.get("repository"), candidate, stage, self.config["base"])
        # The agent container gets only the tool socket; the repository is read in the tool container.
        with Snapshot(image, self.mirror, spec) as snapshot, Tools(snapshot) as tools:
            command[-1:-1] = ["--mount", tools.mount()]
            try:
                result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                        text=True, timeout=600, env=environment)
                if result.returncode in KILLED or result.returncode == UNCERTAIN_EXIT:
                    raise UncertainOutcome(f"{provider} outcome is unknown; output suppressed")
                if result.returncode or len(result.stdout.encode()) > 300000:
                    raise Blocked(f"{provider} failed; output suppressed to protect credentials")
                return json.loads(result.stdout)
            except (Blocked, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                # A timeout or kill may follow upstream completion; other failures were reported.
                uncertain = isinstance(error, (subprocess.TimeoutExpired, UncertainOutcome))
                if session:
                    raise SessionFailure("provider session failed; output suppressed to protect credentials",
                                         uncertain=uncertain) from None
                if uncertain:
                    raise UncertainOutcome(f"{provider} outcome is unknown; output suppressed") from None
                raise error
            finally:
                cleanup.remove(["docker", "rm", "-f", name], environment)
