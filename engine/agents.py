"""Run isolated provider containers with optional role-scoped transcript volumes."""

import json
import os
import subprocess
import uuid

from policy import Blocked, SessionFailure, IMAGE, selected_model
from credentials import Credential, EnvironmentCredentials
from bundle import Bundle
from github import GitHub
from snapshot import Snapshot, inspection
from sessions import SessionVolume


class Agents:
    """Launch bounded inference without host filesystem mounts or shared role context."""

    def __init__(self, config, archive_loader=None, credentials=None):
        """Use trusted configuration, an operator-pinned image digest and a credential resolver."""
        self.config = config
        self.credentials = credentials or EnvironmentCredentials()
        self.bundle = Bundle(profiles=config["execution_profiles"])
        self.archive_loader = archive_loader or GitHub(config["repository"]).archive
        self.archives = {}

    def credential(self, provider, stage, task):
        """Select the credential identity for one model call before any secret is read."""
        return self.credentials.resolve(provider, stage, task)

    def ask(self, provider, stage, context):
        """Return one JSON response, killing the container on timeout or interruption."""
        context = dict(context)
        session = context.pop("controller_session", None)
        selected = context.pop("controller_credential", None)
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
        commit, overlays = inspection(context, stage)
        if commit not in self.archives:
            # Cache only the current immutable archive within this controller invocation.
            self.archives = {commit: self.archive_loader(commit)}
        with Snapshot(image, self.archives[commit], overlays) as snapshot:
            command[-1:-1] = ["--mount", snapshot.mount()]
            try:
                result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                        text=True, timeout=600, env=environment)
                if result.returncode or len(result.stdout.encode()) > 300000:
                    raise Blocked(f"{provider} failed; output suppressed to protect credentials")
                return json.loads(result.stdout)
            except (Blocked, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                if session:
                    raise SessionFailure("provider session failed; output suppressed to protect credentials") from None
                raise error
            finally:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                               timeout=30, env=environment)
