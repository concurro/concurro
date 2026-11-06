"""Native runner turns: isolated role transcripts, workspace extraction and bounded results."""

import base64
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
import signal
import sys

import processes

from policy import KILLED, UNCERTAIN_EXIT, MODEL_RESPONSE_LIMIT
from repository import snapshot
import feedback

KEY_VARIABLE = "CONCURRO_GATEWAY_KEY"
MAX_RESPONSE = MODEL_RESPONSE_LIMIT
MAX_RETAINED = 24 * 1024 * 1024
# Codex's regenerable state (extracted marketplaces, plugin syncs, catalogs, diagnostic logs),
# relative to the retained root; resuming needs only its transcript and thread state.
RETAINED_CACHES = ("codex/.tmp", "codex/tmp", "codex/plugins", "codex/cache", "codex/log", "codex/logs_*.sqlite*",
                   "codex/models_cache*.json")
MAX_WORKSPACE_CHANGES = 500
# A turn's workspace name, also its mirror borrow name.
NAME = re.compile(r"turn-[0-9a-f]{32}")


class TurnError(RuntimeError):
    """The turn could not be run; nothing reached a provider."""


def agent_environment(root, key, retained):
    """Keep target toolchains and HOME, excluding job credentials from the child environment.

    This is hygiene, not a sandbox: native tools can access their own ephemeral job.
    """
    excluded = ("ACTIONS_", "GITHUB_", "GIT_", "CONCURRO_", "OPENAI_", "ANTHROPIC_", "CLAUDE_", "CODEX_", "COPILOT_")
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(excluded) and k not in ("GH_TOKEN", "GH_ENTERPRISE_TOKEN")}
    home = root / ("retained" if retained else "scratch")
    return {**environment, KEY_VARIABLE: key, "CODEX_HOME": str(home / "codex"),
            "CLAUDE_CONFIG_DIR": str(home / "claude"), "COPILOT_HOME": str(home / "copilot"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}


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
    tree = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in
            git("ls-tree", "-r", "-z", "--full-tree", "HEAD").decode("utf-8", errors="surrogateescape").split("\0") if line}
    gitlinks = {path for path, mode in tree.items() if mode == "160000"}
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
            if tree.get(path) not in ("100644", "100755"):
                raise TurnError("deletion requires an existing regular file: " + path)
            removed.append(path)
            continue
        file = Path(work, path)
        if file.is_symlink() or not file.is_file():
            raise TurnError("the workspace changes a path that is not a regular file: " + path)
        mode = "100755" if file.stat().st_mode & 0o111 else "100644"
        if mode != tree.get(path, "100644"):
            raise TurnError("mode changes are not supported: " + path)
        data = file.read_bytes()
        if "?" not in code and unchanged(path, data):
            raise TurnError("mode-only changes are not supported: " + path)
        if b"\0" in data:
            raise TurnError("the workspace changes a binary file: " + path)
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


def retained_entries(directory):
    """A retained directory's archived entries, sorted: everything but the CLIs' regenerable caches."""
    root = Path(directory)
    caches = {path for pattern in RETAINED_CACHES for path in root.glob(pattern)}
    return [path for path in sorted(root.rglob("*"))
            if not caches.intersection((path, *path.parents))
            and (path.is_file() and not path.is_symlink() or path.is_dir())]


def pack(directory):
    """A retained transcript directory as a base64 archive."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for path in retained_entries(directory):
            bundle.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    data = buffer.getvalue()
    if len(data) > MAX_RETAINED:
        raise TurnError("retained session archive is too large; largest (uncompressed): " + largest(directory))
    return base64.b64encode(data).decode()


def largest(directory, count=5):
    """The biggest archived entries two levels down, by escaped path and size, to diagnose an oversized archive."""
    root = Path(directory)
    sizes = {}
    for path in retained_entries(root):
        if path.is_file():
            entry = Path(*path.relative_to(root).parts[:2])
            sizes[entry] = sizes.get(entry, 0) + path.stat().st_size
    ranked = sorted(sizes.items(), key=lambda item: -item[1])[:count]
    return ", ".join(f"{json.dumps(str(entry))} {size // 1024} KiB" for entry, size in ranked)


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


def command(root):
    """The native agent entry point; stdout is the bounded structured response."""
    return [sys.executable, str(Path(__file__).with_name("agent_entry.py"))]


def execute(turn, key, mirror, root, stopped=None):
    """Run one native agent process group and return JSON, extracted files and its retained journal."""
    request, spec, timeout = turn["request"], turn["spec"], turn["timeout"]
    if type(timeout) is not int or not 60 <= timeout <= 7200:
        raise TurnError("invalid turn timeout")
    root = Path(root).resolve()
    name = root.name
    if not NAME.fullmatch(name):
        raise TurnError("invalid turn directory")
    session = request.get("session")
    for directory in ("work", "scratch", "retained"):
        (root / directory).mkdir(parents=True)
    if request.get("evaluation"):
        if request["stage"] != "plan_qc" or session is not None:
            raise TurnError("evaluation is limited to fresh design review")
        from design_eval import fixture_mirror
        mirror, commit = fixture_mirror(root / "evaluation", "concurro/evaluation")
        spec = {"refs": {"fixture": commit}, "head": "fixture", "base_branch": "fixture", "candidate": None}
        context = {**request["context"], "base": commit, "repository_commit": commit,
                   "repository": {"refs": spec["refs"], "head": "fixture"}}
        request = {**request, "context": context}
    mirror.borrow(name, spec["base_branch"], sorted(set(spec["refs"].values())))
    try:
        snapshot({k: spec[k] for k in ("refs", "head", "candidate")}, root=root / "work", mirror=mirror.path)
        shutil.copytree(root / "work" / ".git", root / "pristine.git", symlinks=True)
        if session:
            unpack(turn.get("retained"), root / "retained")
        payload = {**request, "workspace": str(root / "work"), "scratch": str(root / "scratch"),
                   "retained_root": str(root / "retained"), "timeout": timeout}
        if turn.get("feedback"):
            payload["feedback_directory"] = feedback.materialize(turn["feedback"], root / "feedback")
        baseline = processes.begin()
        with subprocess.Popen(command(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, env=agent_environment(root, key, bool(session)), start_new_session=True) as process:
            timer = None
            if stopped is not None:
                import threading
                def cancel():
                    while process.poll() is None:
                        if stopped.wait(1):
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            return
                timer = threading.Thread(target=cancel, daemon=True)
                timer.start()
            try:
                stdout, _ = process.communicate(json.dumps(payload), timeout=timeout + 60)
                code = process.returncode
            except subprocess.TimeoutExpired:
                code, stdout = KILLED[0], ""
            finally:
                # Stop descendants before taking the diff or archiving session state.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if timer:
                    timer.join(2)
                try:
                    processes.finish(baseline)
                except Exception:
                    if stopped is not None:
                        stopped.set()
                    raise
        result, response = outcome(code, stdout)
        reply = {"outcome": result, "response": response, "workspace": None,
                 "retained": pack(root / "retained") if session else None}
        if result == "ok" and request["stage"] == "develop":
            try:
                reply["workspace"] = extract(root / "work", root / "pristine.git", root)
            except TurnError as error:
                reply["workspace"] = {"error": str(error)}
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
