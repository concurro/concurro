"""Prepare disposable Git snapshots and serve them from a credential-free tool container."""

import json
import os
import re
import subprocess
import uuid

import cleanup
from policy import Blocked, IMAGE

# The tool container's user; the agent container runs as the same user to reach the socket.
TOOL_USER = "10001:10001"
SOCKET_DIRECTORY = "/run/concurro-tools"
# Tool containers outlive the longest agent call, then stop on their own.
TOOL_LIFETIME = 660


def docker_environment():
    """A Docker client environment with no provider or GitHub credentials."""
    return {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}


def hardened(name, user, cpus, memory, pids):
    """The runtime policy every Concurro helper container gets."""
    return ["docker", "run", "--rm", "--name", name, "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", f"--pids-limit={pids}", f"--memory={memory}", f"--cpus={cpus}",
            f"--user={user}", "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777"]


class Snapshot:
    """Own one Git snapshot volume, writable during preparation and read-only while served."""

    def __init__(self, image, mirror, spec):
        """`spec` names the refs, the checked-out head, and an optional candidate (see repository.snapshot)."""
        if not IMAGE.fullmatch(image):
            raise Blocked("snapshot requires an immutable agent image")
        self.image, self.mirror, self.spec = image, mirror, spec
        self.volume = "concurro-source-" + uuid.uuid4().hex
        self.env = docker_environment()

    def mount(self, readonly=True):
        """Return only this allocated named volume."""
        return f"type=volume,source={self.volume},target=/repository" + (",readonly" if readonly else "")

    def mirror_mount(self):
        """The target's mirror, read-only; it holds only the target's public history."""
        return f"type=bind,source={self.mirror.path},target=/mirror,readonly"

    def __enter__(self):
        """Borrow the commits from the mirror, then build the snapshot offline before any model starts."""
        name = self.volume + "-prepare"
        try:
            self.mirror.borrow(self.volume, self.spec["base_branch"], sorted(set(self.spec["refs"].values())))
            subprocess.run(["docker", "volume", "create", self.volume], check=True,
                           capture_output=True, env=self.env, timeout=30)
            command = hardened(name, "0:0", 2, "2g", 128) + [
                "-i", "--mount", self.mount(False), "--mount", self.mirror_mount(),
                "--entrypoint", "python3", self.image, "-I", "/opt/concurro/engine/repository.py", "snapshot"]
            payload = {k: self.spec[k] for k in ("refs", "head", "candidate")}
            try:
                result = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True,
                                        env=self.env, timeout=600)
            finally:
                cleanup.remove(["docker", "rm", "-f", name], self.env)
            if result.returncode:
                raise Blocked("repository snapshot preparation failed")
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        """Remove this snapshot's volume and release its pins."""
        cleanup.remove(["docker", "volume", "rm", "-f", self.volume], self.env)
        self.mirror.release(self.volume)
        self.mirror.maintain()


class Tools:
    """One session's tool container and the dedicated volume that carries only its socket."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.name = "concurro-tools-" + uuid.uuid4().hex
        self.volume = self.name + "-socket"
        self.env = docker_environment()

    def mount(self):
        """The socket volume, for the agent container; it holds nothing else."""
        return f"type=volume,source={self.volume},target={SOCKET_DIRECTORY}"

    def __enter__(self):
        """Start the tool container: no credentials, no network, read-only source, bounded resources."""
        try:
            subprocess.run(["docker", "volume", "create", self.volume], check=True,
                           capture_output=True, env=self.env, timeout=30)
            owner = self.name + "-owner"
            try:
                chown = subprocess.run(hardened(owner, "0:0", 1, "64m", 16) + [
                    "--cap-add=CHOWN", "--mount", self.mount(), "--entrypoint", "chown", self.snapshot.image,
                    TOOL_USER, SOCKET_DIRECTORY], capture_output=True, env=self.env, timeout=60)
            finally:
                cleanup.remove(["docker", "rm", "-f", owner], self.env)
            if chown.returncode:
                raise Blocked("tool socket preparation failed")
            command = hardened(self.name, TOOL_USER, 1, "1g", 64) + [
                "--detach", "--mount", self.snapshot.mount(), "--mount", self.snapshot.mirror_mount(),
                "--mount", self.mount(), "--entrypoint", "python3", self.snapshot.image, "-I",
                "/opt/concurro/engine/repository.py", "listen", str(TOOL_LIFETIME)]
            started = subprocess.run(command, capture_output=True, env=self.env, timeout=60)
            if started.returncode:
                raise Blocked("repository tool container failed to start")
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        """Remove the container, then its socket volume."""
        cleanup.remove(["docker", "rm", "-f", self.name], self.env)
        cleanup.remove(["docker", "volume", "rm", "-f", self.volume], self.env)


def inspection(repository, candidate, stage, base_branch):
    """The snapshot a call reads: controller-supplied refs, and the candidate for develop and local review."""
    if not isinstance(repository, dict) or set(repository) != {"refs", "head"}:
        raise Blocked("repository inspection requires pinned refs")
    refs, head = repository["refs"], repository["head"]
    if (not isinstance(refs, dict) or head not in refs
            or not all(isinstance(c, str) and re.fullmatch(r"[a-f0-9]{40}", c) for c in refs.values())):
        raise Blocked("repository inspection requires pinned refs")
    if (candidate is None) == (stage in ("develop", "local_review")):
        raise Blocked("only develop and local review inspect a candidate")
    return {"refs": refs, "head": head, "base_branch": base_branch,
            "candidate": None if candidate is None else {"files": candidate}}
