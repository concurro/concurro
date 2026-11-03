"""Credential-free build/test workbench with local Git checkpoints and bounded output."""

import hashlib
import json
import os
import selectors
import subprocess
import time
import uuid

import cleanup
from policy import Blocked, IMAGE
from checks import CACHE_ROOT, CONTROL_CHECKS, PLACEHOLDER, command as check_command, executable, preparing


def resource_exhausted(report):
    """Distinguish execution limits from ordinary compiler or assertion failures."""
    return report["timed_out"] or report["output_limited"] or report["exit_code"] in (137, -9)


def bounded_process(command, env, timeout):
    """Capture bounded untrusted output and stop on timeout or excessive output."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    tail, count, timed_out, limited = b"", 0, False, False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(min(remaining, 1)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                count += len(chunk)
                tail = (tail + chunk)[-12000:]
                if count > 2_000_000:
                    limited = True
                    break
        if timed_out or limited:
            process.kill()
        try:
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    return {"exit_code": code, "timed_out": timed_out, "output_limited": limited,
            "output": tail.decode("utf-8", errors="replace"), "output_sha": hashlib.sha256(tail).hexdigest()}


def cache_path(profile, cache):
    """Engine-assigned cache mount point; profiles name caches but never choose paths."""
    return f"{CACHE_ROOT}/{profile}/{cache}"


class LocalWorkspace:
    """Own temporary Docker volumes; tests never mount local Git metadata or credentials."""

    def __init__(self, config, archive, branch, execution, base_archive=None):
        """Select a pinned execution image and unique volumes for this task attempt."""
        self.config, self.archive, self.branch = config, archive, branch
        self.execution, self.base_archive = execution, base_archive
        self.image = os.environ.get("CONCURRO_EXECUTION_IMAGE", "")
        if not IMAGE.fullmatch(self.image):
            raise Blocked("CONCURRO_EXECUTION_IMAGE must be an immutable digest or local image ID")
        self.prefix = "concurro-local-" + uuid.uuid4().hex
        self.profiles = {p["id"]: p for p in execution["profiles"]}
        self.volumes = {kind: self.prefix + "-" + kind for kind in ("source", "state", "stage")}
        # Fresh per workbench: caches written by earlier candidates are never reused for preparation.
        self.caches = {(p, c): f"{self.prefix}-cache-{p}-{c}" for p in self.profiles for c in self.profiles[p]["caches"]}
        self.inputs = {p: f"{self.prefix}-input-{p}" for p, spec in self.profiles.items() if spec["prepare"]}
        self.env = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
        self.current = None
        self.baseline = None
        self.prepared = set()
        # Packages each profile's scoped checks cover; a profile absent here runs them unscoped.
        self.packages = {}
        self.staged = False

    def __enter__(self):
        """Initialize a local-only branch from the pinned source archive."""
        try:
            for volume in [*self.volumes.values(), *self.caches.values(), *self.inputs.values()]:
                subprocess.run(["docker", "volume", "create", volume], env=self.env,
                               check=True, capture_output=True, timeout=30)
            self.current = self.control("init", self.branch, *(cache_path(p, c) for p, c in self.caches), data=self.archive)
            self.baseline = self.current
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        """Attempt cleanup on normal exit and handled failures."""
        self.close()

    def close(self):
        """Clean only resources allocated by this workbench."""
        for volume in [*self.volumes.values(), *self.caches.values(), *self.inputs.values()]:
            cleanup.remove(["docker", "volume", "rm", "-f", volume], self.env)

    def expand(self, profile, value):
        """Resolve a profile's cache placeholders to its engine-assigned mount points."""
        return PLACEHOLDER.sub(lambda match: cache_path(profile, match[1]), value)

    def command(self, name, control=False, network=False, mode="check", profile=None):
        """Build a container invocation with explicit mounts and no inherited secrets."""
        mode = "control" if control else mode
        args = ["docker", "run", "--rm", "--init", "--name", name, "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=512",
                "--memory=8g", "--cpus=4", "--user=" + ("0:0" if mode == "control" else "10001:10001"),
                "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777"]
        if not network:
            args += ["--network=none"]
        mounts = []
        if mode == "control":
            mounts += [(self.volumes["source"], "/workspace", False), (self.volumes["state"], "/state", False),
                       (self.volumes["stage"], "/stage", False)]
            mounts += [(volume, "/input/" + p, False) for p, volume in self.inputs.items()]
            mounts += [(volume, cache_path(p, c), False) for (p, c), volume in self.caches.items()]
            args += ["--workdir=/workspace", "-i"]
        elif mode == "prepare":
            # Neutral working directory; inputs and the optional source view come from the approved base.
            args += ["--tmpfs=/prepare:rw,nosuid,nodev,size=64m,mode=1777", "--workdir=/prepare"]
            mounts += [(self.inputs[profile], "/prepare/input", True)]
            if self.profiles[profile]["prepare"]["source"]:
                mounts += [(self.volumes["stage"], "/workspace", True)]
            mounts += [(volume, cache_path(p, c), False) for (p, c), volume in self.caches.items() if p == profile]
        else:
            args += ["--workdir=/workspace"]
            mounts += [(self.volumes["source"], "/workspace", True)]
            mounts += [(volume, cache_path(p, c), False) for (p, c), volume in self.caches.items()]
        for volume, target, readonly in mounts:
            args += ["--mount", f"type=volume,source={volume},target={target}" + (",readonly" if readonly else "")]
        if mode != "control" and profile is not None:
            for key, value in self.profiles[profile]["env"].items():
                args += ["--env", key + "=" + self.expand(profile, value)]
        return args

    def control(self, operation, *arguments, data=None, timeout=120):
        """Run a trusted Git/file operation, without running candidate programs."""
        name = self.prefix + "-control"
        command = self.command(name, control=True) + [self.image, operation, *arguments]
        payload = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        try:
            try:
                result = subprocess.run(command, input=payload, env=self.env, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise Blocked("local Git workspace operation timed out") from None
            if result.returncode or len(result.stdout) > 500000:
                raise Blocked("local Git workspace operation failed")
            return json.loads(result.stdout)
        finally:
            cleanup.remove(["docker", "rm", "-f", name], self.env)

    def execute(self, name, command, timeout, network=False, control=False, mode="check", profile=None):
        """Run one bounded command and bind its actual exit status to the current tree."""
        container = self.prefix + "-run"
        args = self.command(container, control=control, network=network, mode=mode, profile=profile)
        args += ["--entrypoint", command[0], self.image, *command[1:]]
        try:
            report = bounded_process(args, self.env, timeout)
            return {"name": name, "tree": self.current["tree"], **report}
        finally:
            cleanup.remove(["docker", "rm", "-f", container], self.env)

    def prepare(self, selected, timeout):
        """Prepare each selected profile from the approved base, before any candidate edit."""
        if self.current != self.baseline:
            raise Blocked("dependency preparation must precede candidate edits")
        deadline = time.monotonic() + timeout

        def remaining(limit):
            """Bound each helper or step by what is left of the shared preparation budget."""
            left = deadline - time.monotonic()
            if left < 1:
                raise Blocked("dependency preparation exceeded its shared time budget")
            return min(limit, left)

        for profile in preparing(selected, self.execution):
            spec = self.profiles[profile]["prepare"]
            if not self.staged:
                if self.base_archive is None:
                    raise Blocked("dependency preparation requires the approved base snapshot")
                self.control("stage", data=self.base_archive, timeout=remaining(120))
                self.staged = True
            self.control("inputs", profile, data=spec["requires"], timeout=remaining(120))
            for index, step in enumerate(spec["steps"], 1):
                report = self.execute(f"prepare:{profile}:{index}", [self.expand(profile, v) for v in step],
                                      remaining(timeout), network=True, mode="prepare", profile=profile)
                if report["exit_code"] or report["timed_out"] or report["output_limited"]:
                    raise Blocked("dependency preparation failed; repair the execution environment")
            self.prepared.add(profile)

    def files(self, paths):
        """Read approved source files through the isolated helper."""
        return self.control("files", data=paths)

    def checkpoint(self, edits):
        """Commit one agent edit batch on the local branch before testing it."""
        self.current = self.control("checkpoint", data={"expected": self.current["local_commit"], "edits": edits})
        return self.current

    def scope(self, packages):
        """Bind each profile's scoped checks to the packages the approved plan changes (None: all)."""
        self.packages = dict(packages)

    def configure_validation(self, spec, originals):
        """Bind declarative rules to the original approved source, including later PR repairs."""
        self.control('validation', data={'spec': spec, 'originals': originals})

    def run_checks(self, required, requested, deadline):
        """Run mandatory and agent-requested checks; shell strings are never accepted."""
        known = {c["id"]: (p["id"], c) for p in self.execution["profiles"] for c in p["checks"]}
        if not isinstance(requested, list) or not all(isinstance(c, str) and (c in CONTROL_CHECKS or c in known)
                                                      for c in requested):
            raise Blocked("agent requested an unknown local check")
        names = list(dict.fromkeys([*required, *requested]))
        if any(name not in CONTROL_CHECKS and name not in known for name in names):
            raise Blocked("plan requires an unknown local check")
        if not set(names).issubset(executable(self.execution, self.prepared)):
            raise Blocked("checks needing dependencies must be prepared before candidate edits; revise the plan's checks")
        reports = []
        for name in names:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise Blocked("local execution time budget exhausted")
            timeout = min(remaining, self.config["local_check_timeout_seconds"])
            if name in CONTROL_CHECKS:
                reports.append(self.execute(name, ["python3", "-I", "/opt/concurro/workspace_entry.py", name],
                                            timeout, control=True))
            else:
                profile, check = known[name]
                argv = check_command(check, self.packages.get(profile))
                reports.append(self.execute(name, [self.expand(profile, v) for v in argv], timeout, profile=profile))
            if resource_exhausted(reports[-1]):
                break
        return reports

    def export(self):
        """Export only the net committed source changes, never Git config or build outputs."""
        return self.control("export")
