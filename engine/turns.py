"""One model turn, run anywhere: a workspace from a snapshot spec, the agent container, and its results.

A turn is self-contained: the agent request, the snapshot spec, and the role's retained
transcript as an archive. The service host and runner jobs run turns the same way; only the
agent container's resources differ. The container holds the gateway key and nothing else, and
works on a real Git workspace with its CLI's native tools.
"""

import base64
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid

import cleanup
from policy import IMAGE, KILLED, UNCERTAIN_EXIT
from repository import MAX_FILE, snapshot

# Inside the agent container.
WORKSPACE, SCRATCH, RETAINED = "/work", "/scratch", "/retained"
KEY_VARIABLE = "CONCURRO_GATEWAY_KEY"
MAX_RESPONSE = 300_000
MAX_RETAINED = 24 * 1024 * 1024
MAX_WORKSPACE_CHANGES = 500
# A turn's workspace name, also its mirror borrow name.
NAME = re.compile(r"turn-[0-9a-f]{32}")


@dataclass(frozen=True)
class Limits:
    """The agent container's resources."""

    memory: str = "4g"
    cpus: int = 2
    pids: int = 1024
    tmp: str = "1g"


class TurnError(RuntimeError):
    """The turn could not be run; nothing reached a provider."""


def docker_environment():
    """A Docker client environment with no provider or GitHub credentials."""
    return {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}


def git_environment(work, git_dir, index):
    """Git over the agent's work tree with the pristine repository: no configuration, hook or filter of the agent's."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(), "LC_ALL": "C",
            "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(work), "GIT_INDEX_FILE": str(index),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}


def extract(work, pristine, root):
    """The files the agent changed in its workspace, against the snapshot's head.

    Git runs with the repository copied before the agent started, so nothing the agent wrote
    under `.git` (configuration, hooks, filters) takes part; the work tree's ignore rules do.
    Returns `{"edits": [{path, content}], "removed": [path], "skipped": [path]}`.
    """
    index = Path(root) / "extract.index"
    environment = git_environment(work, pristine, index)

    def git(*args):
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                                 "-c", "core.hooksPath=" + os.devnull, *args],
                                capture_output=True, env=environment, timeout=600)
        if result.returncode:
            raise TurnError("workspace extraction failed: git " + args[0])
        return result.stdout

    def unchanged(path, data):
        result = subprocess.run(["git", "hash-object", "--no-filters", "--stdin"], input=data, capture_output=True,
                                env=environment, timeout=60)
        return not result.returncode and result.stdout.strip() == git("rev-parse", "HEAD:" + path).strip()

    git("read-tree", "HEAD")
    # Submodules are never entered: Git would read the agent's configuration inside them.
    gitlinks = {line.split("\t", 1)[1] for line in
                git("ls-tree", "-r", "-z", "--full-tree", "HEAD").decode("utf-8", errors="surrogateescape").split("\0")
                if line.startswith("160000 ")}
    listed = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--ignore-submodules=all")
    edits, removed, skipped = [], [], []
    entries = [e for e in listed.decode("utf-8", errors="surrogateescape").split("\0") if e]
    if len(entries) > MAX_WORKSPACE_CHANGES:
        raise TurnError(f"the workspace changes more than {MAX_WORKSPACE_CHANGES} files")
    for entry in entries:
        code, path = entry[:2], entry[3:]
        if path.endswith("/") or any(path == link or path.startswith(link + "/") for link in gitlinks):
            # A nested repository or submodule: not part of the candidate.
            skipped.append(path)
            continue
        # Policy is the controller's; only a path no candidate can carry is refused here.
        if not path or path.startswith("/") or ".." in path.split("/") or len(path) > 4096:
            raise TurnError("the workspace changes a path the engine cannot represent: " + json.dumps(path))
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            raise TurnError("the workspace changes a path that is not UTF-8: " + json.dumps(path)) from None
        if "D" in code:
            removed.append(path)
            continue
        file = Path(work, path)
        if file.is_symlink() or not file.is_file():
            raise TurnError("the workspace changes a path that is not a regular file: " + path)
        data = file.read_bytes()
        if len(data) > MAX_FILE:
            raise TurnError("the workspace changes a file larger than the engine accepts: " + path)
        if "?" not in code and unchanged(path, data):
            # Only its mode changed, which a candidate does not carry.
            continue
        try:
            edits.append({"path": path, "content": data.decode("utf-8")})
        except UnicodeDecodeError:
            raise TurnError("the workspace changes a file that is not UTF-8 text: " + path) from None
    return {"edits": sorted(edits, key=lambda e: e["path"]), "removed": sorted(removed), "skipped": sorted(skipped)}


def unpack(archive, directory):
    """Restore a retained transcript archive into an empty directory."""
    if archive is None:
        return
    data = base64.b64decode(archive, validate=True)
    if len(data) > MAX_RETAINED:
        raise TurnError("retained session archive is too large")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        bundle.extractall(directory, filter="data")


def pack(directory):
    """A retained transcript directory as a base64 archive."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file() and not path.is_symlink() or path.is_dir():
                bundle.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    data = buffer.getvalue()
    if len(data) > MAX_RETAINED:
        raise TurnError("retained session archive is too large")
    return base64.b64encode(data).decode()


def outcome(code, stdout):
    """`(outcome, response)` for the agent container's exit and output; only its JSON response is kept.

    A kill or the entry point's uncertainty exit may follow upstream completion, so it is
    `uncertain`; any other failure, or output that is not one bounded JSON value, is `failed`.
    """
    if code in KILLED or code == UNCERTAIN_EXIT:
        return "uncertain", None
    if code or len(stdout.encode()) > MAX_RESPONSE:
        return "failed", None
    try:
        return "ok", json.loads(stdout)
    except json.JSONDecodeError:
        return "failed", None


def command(name, image, root, mirror, retained, limits):
    """The agent container: unprivileged, the workspace and scratch space, the mirror read-only."""
    user = f"{os.getuid()}:{os.getgid()}"
    mounts = [f"type=bind,source={root / 'work'},target={WORKSPACE}",
              f"type=bind,source={root / 'scratch'},target={SCRATCH}",
              # The workspace's alternates name the mirror's own path.
              f"type=bind,source={mirror},target={mirror},readonly"]
    if retained:
        mounts.append(f"type=bind,source={root / 'retained'},target={RETAINED}")
    return ["docker", "run", "--rm", "--init", "-i", "--name", name, "--cap-drop=ALL",
            "--security-opt=no-new-privileges", f"--pids-limit={limits.pids}", f"--memory={limits.memory}",
            f"--cpus={limits.cpus}", f"--user={user}", f"--tmpfs=/tmp:rw,nosuid,nodev,size={limits.tmp},mode=1777",
            *(arg for mount in mounts for arg in ("--mount", mount)),
            "--env", KEY_VARIABLE, "--env", f"HOME={SCRATCH}/home", "--env", f"CODEX_HOME={SCRATCH}/codex",
            "--env", f"CLAUDE_CONFIG_DIR={SCRATCH}/claude", "--env", f"COPILOT_HOME={SCRATCH}/copilot",
            # Bind-mounted files may show another owner; the agent's Git must still open its workspace.
            "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory", "--env", "GIT_CONFIG_VALUE_0=*",
            "--workdir", WORKSPACE, image]


def execute(turn, image, key, mirror, root, limits=Limits()):
    """Run one turn under `root` (a fresh directory) and return its outcome.

    `turn` is `{"request": agent request, "spec": snapshot spec, "retained": archive or None,
    "timeout": seconds}`; `mirror` is a `Mirror` holding the target's history. Returns
    `{"outcome": "ok" | "failed" | "uncertain", "response", "workspace", "retained"}`;
    `workspace` (develop only) holds the extracted changes.
    """
    if not IMAGE.fullmatch(image) and not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+", image):
        raise TurnError("the agent image must be pinned")
    request, spec, timeout = turn["request"], turn["spec"], turn["timeout"]
    if type(timeout) is not int or not 60 <= timeout <= 7200:
        raise TurnError("invalid turn timeout")
    root = Path(root)
    name = root.name
    if not NAME.fullmatch(name):
        raise TurnError("invalid turn directory")
    session = request.get("session")
    for directory in ("work", "scratch", "retained"):
        (root / directory).mkdir(parents=True)
    mirror.borrow(name, spec["base_branch"], sorted(set(spec["refs"].values())))
    environment = {**docker_environment(), KEY_VARIABLE: key}
    container = "concurro-" + uuid.uuid4().hex
    try:
        snapshot({k: spec[k] for k in ("refs", "head", "candidate")}, root=root / "work", mirror=mirror.path)
        shutil.copytree(root / "work" / ".git", root / "pristine.git", symlinks=True)
        if session:
            unpack(turn.get("retained"), root / "retained")
        payload = {**request, "workspace": WORKSPACE, "timeout": timeout}
        try:
            result = subprocess.run(command(container, image, root, mirror.path, bool(session), limits),
                                    input=json.dumps(payload), capture_output=True, text=True,
                                    timeout=timeout + 60, env=environment)
            code, stdout = result.returncode, result.stdout
        except subprocess.TimeoutExpired:
            code, stdout = KILLED[0], ""
        finally:
            cleanup.remove(["docker", "rm", "-f", container], environment)
        result, response = outcome(code, stdout)
        reply = {"outcome": result, "response": response, "workspace": None,
                 "retained": pack(root / "retained") if session else None}
        if result == "ok" and request["stage"] == "develop":
            reply["workspace"] = extract(root / "work", root / "pristine.git", root)
        return reply
    finally:
        mirror.release(name)


def directory(parent):
    """A fresh turn directory under `parent`."""
    path = Path(parent) / ("turn-" + uuid.uuid4().hex)
    path.mkdir(parents=True, mode=0o700)
    return path


def remove(path):
    """Delete a turn directory, including files the agent made read-only."""
    def writable(function, target, _):
        os.chmod(target, 0o700)
        function(target)
    shutil.rmtree(path, onexc=writable)
