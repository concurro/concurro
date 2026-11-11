"""Native runner turns: isolated role transcripts, workspace extraction and bounded results."""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

import feedback
import git_env
import processes
from policy import (
    KEY_VARIABLE as KEY_VARIABLE,
    KILLED,
    MODEL_RESPONSE_LIMIT,
    SIGNALLED,
    TIMEOUT_EXIT,
    UNCERTAIN_EXIT,
    runner_bound,
)
from repository import snapshot
from vocab import TurnOutcome

MAX_RESPONSE = MODEL_RESPONSE_LIMIT
MAX_RETAINED = 24 * 1024 * 1024
# Transport/memory bounds, independent of operator file-count policy. Together with the
# base64 retained archive these leave room inside the exchange's 48 MiB result envelope.
MAX_EXTRACT_BYTES = 8 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
# Codex's regenerable state (extracted marketplaces, plugin syncs, catalogs, diagnostic logs),
# relative to the retained root; resuming needs only its transcript and thread state.
RETAINED_CACHES = (
    "codex/.tmp",
    "codex/tmp",
    "codex/plugins",
    "codex/cache",
    "codex/log",
    "codex/logs_*.sqlite*",
    "codex/models_cache*.json",
)
# A turn's workspace name, also its mirror borrow name.
NAME = re.compile(r"turn-[0-9a-f]{32}")


class TurnError(RuntimeError):
    """The turn could not be run; nothing reached a provider."""


def agent_environment(root, key, retained):
    """Keep target toolchains and HOME, excluding job credentials from the child environment.

    This is hygiene, not a sandbox: native tools can access their own ephemeral job.
    """
    excluded = ("ACTIONS_", "GITHUB_", "GIT_", "CONCURRO_", "OPENAI_", "ANTHROPIC_", "CLAUDE_", "CODEX_", "COPILOT_")
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(excluded) and k not in ("GH_TOKEN", "GH_ENTERPRISE_TOKEN")
    }
    home = root / ("retained" if retained else "scratch")
    return {
        **environment,
        KEY_VARIABLE: key,
        "CODEX_HOME": str(home / "codex"),
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "COPILOT_HOME": str(home / "copilot"),
        **git_env.GIT_ISOLATION,
        **git_env.safe_directory("*"),
    }


def git_environment(work, git_dir, index):
    """Git over the agent's work tree with the pristine repository: no configuration, hook or filter of the agent's."""
    return {
        **git_env.isolated_git_env(),
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(work),
        "GIT_INDEX_FILE": str(index),
        **git_env.safe_directory("*"),
    }


def extract(work, pristine, root, maximum=None):
    """The files the agent changed in its workspace, against the snapshot's head.

    Git runs with the repository copied before the agent started, so nothing the agent wrote
    under `.git` (configuration, hooks, filters) takes part; the work tree's ignore rules do.
    Returns `{"edits": [{path, content}], "removed": [path], "skipped": [path]}`.
    """
    if maximum is not None and (type(maximum) is not int or maximum < 1):
        raise TurnError("invalid limits.workspace_files")
    index = Path(root) / "extract.index"
    environment = git_environment(work, pristine, index)

    def git(*args):
        """Bound stdout while it is produced, and reap Git on overflow or timeout."""
        with subprocess.Popen(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                *args,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        ) as process:
            timer = threading.Timer(600, process.kill)
            timer.start()
            try:
                data = process.stdout.read(MAX_GIT_OUTPUT_BYTES + 1)
                if len(data) > MAX_GIT_OUTPUT_BYTES:
                    raise TurnError("workspace extraction Git output exceeds bounded bytes")
                if process.wait():
                    raise TurnError("workspace extraction failed: git " + args[0])
                return data
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()
                timer.join()

    def unchanged(path, data):
        result = subprocess.run(
            ["git", "hash-object", "--no-filters", "--stdin"],
            input=data,
            capture_output=True,
            env=environment,
            timeout=60,
        )
        return not result.returncode and result.stdout.strip() == git("rev-parse", "HEAD:" + path).strip()

    git("read-tree", "HEAD")
    # Submodules are never entered: Git would read the agent's configuration inside them.
    tree = {
        line.split("\t", 1)[1]: line.split(" ", 1)[0]
        for line in git("ls-tree", "-r", "-z", "--full-tree", "HEAD")
        .decode("utf-8", errors="surrogateescape")
        .split("\0")
        if line
    }
    gitlinks = {path for path, mode in tree.items() if mode == "160000"}
    listed = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--ignore-submodules=all")
    edits, removed, skipped = [], [], []
    used = len(json.dumps({"edits": [], "removed": [], "skipped": []}).encode())

    def append(target, value):
        """Charge JSON escaping, UTF-8 and separators before retaining each extracted value."""
        nonlocal used
        used += len(json.dumps(value).encode()) + (2 if target else 0)
        if used > MAX_EXTRACT_BYTES:
            raise TurnError("workspace extraction exceeds bounded JSON bytes")
        target.append(value)

    entries = [e for e in listed.decode("utf-8", errors="surrogateescape").split("\0") if e]
    if maximum is not None and len(entries) > maximum:
        raise TurnError(f"workspace exceeds limits.workspace_files ({maximum})")
    for entry in entries:
        code, path = entry[:2], entry[3:]
        if path.endswith("/") or any(path == link or path.startswith(link + "/") for link in gitlinks):
            # A nested repository or submodule: not part of the candidate.
            append(skipped, path)
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
            append(removed, path)
            continue
        file = Path(work, path)
        if file.is_symlink() or not file.is_file():
            raise TurnError("the workspace changes a path that is not a regular file: " + path)
        mode = "100755" if file.stat().st_mode & 0o111 else "100644"
        if mode != tree.get(path, "100644"):
            raise TurnError("mode changes are not supported: " + path)
        remaining = MAX_EXTRACT_BYTES - used
        if file.stat().st_size > remaining:
            raise TurnError("workspace extraction exceeds bounded content bytes")
        with file.open("rb") as source:
            data = source.read(remaining + 1)
        if len(data) > remaining:
            raise TurnError("workspace extraction exceeds bounded content bytes")
        if "?" not in code and unchanged(path, data):
            raise TurnError("mode-only changes are not supported: " + path)
        if b"\0" in data:
            raise TurnError("the workspace changes a binary file: " + path)
        try:
            append(edits, {"path": path, "content": data.decode("utf-8")})
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
    return [
        path
        for path in sorted(root.rglob("*"))
        if not caches.intersection((path, *path.parents))
        and (path.is_file() and not path.is_symlink() or path.is_dir())
    ]


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
    `uncertain`; an explicit execution timeout stays `timed_out`. Other failures, or output
    that is not one bounded JSON value, are `failed`.
    """
    if code == TIMEOUT_EXIT:
        return TurnOutcome.TIMED_OUT, None
    if code in KILLED or code == UNCERTAIN_EXIT:
        return TurnOutcome.UNCERTAIN, None
    if code or len(stdout.encode()) > MAX_RESPONSE:
        return TurnOutcome.FAILED, None
    try:
        return TurnOutcome.OK, json.loads(stdout)
    except json.JSONDecodeError:
        return TurnOutcome.FAILED, None


def command():
    """The native agent entry point; stdout is the bounded structured response."""
    return [sys.executable, str(Path(__file__).with_name("agent_entry.py"))]


def _validated_timeout(request, timeout):
    """Validate the stage's optional execution limit before preparing a workspace."""
    if timeout is not None and (type(timeout) is not int or timeout < 1):
        raise TurnError("invalid turn timeout")
    return timeout


def _run_agent(payload, root, key, stopped, timeout):
    """Run and clean up the native process group before extracting or retaining files."""
    baseline = processes.begin()
    with subprocess.Popen(
        command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=agent_environment(root, key, bool(payload.get("session"))),
        start_new_session=True,
    ) as process:
        timer = None
        if stopped is not None:

            def cancel():
                while process.poll() is None:
                    if stopped.wait(1):
                        processes.kill_group(process.pid)
                        return

            timer = threading.Thread(target=cancel, daemon=True)
            timer.start()
        try:
            stdout, _ = process.communicate(json.dumps(payload), timeout=None if timeout is None else timeout + 60)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code, stdout = TIMEOUT_EXIT, ""
        finally:
            # Stop descendants before taking the diff or archiving session state.
            processes.kill_group(process.pid)
            process.wait()
            if timer:
                timer.join(2)
            try:
                processes.finish(baseline)
            except Exception:
                if stopped is not None:
                    stopped.set()
                raise
    return code, stdout


def _termination(code, stopped):
    """Prefer runner cancellation over the timeout and signal diagnostics."""
    if stopped is not None and stopped.is_set():
        return "runner_stopped"
    if code == TIMEOUT_EXIT:
        return "turn_timeout"
    return "process_signal"


def execute(turn, key, mirror, root, stopped=None):
    """Run one native agent process group and return JSON, extracted files and its retained journal."""
    started = time.monotonic()
    request, spec, timeout = turn["request"], turn["spec"], turn["timeout"]
    timeout = _validated_timeout(request, timeout)
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
        # Evaluation alone needs the design/mirror/policy fixture dependencies.
        from design_eval import fixture_mirror

        mirror, commit = fixture_mirror(root / "evaluation", "concurro/evaluation")
        spec = {"refs": {"fixture": commit}, "head": "fixture", "base_branch": "fixture", "candidate": None}
        context = {
            **request["context"],
            "base": commit,
            "repository_commit": commit,
            "repository": {"refs": spec["refs"], "head": "fixture"},
        }
        request = {**request, "context": context}
    mirror.borrow(name, spec["base_branch"], sorted(set(spec["refs"].values())))
    try:
        snapshot(
            {
                **{k: spec[k] for k in ("refs", "head", "candidate")},
                "overlay_files_limit": spec.get("overlay_files_limit"),
            },
            root=root / "work",
            mirror=mirror.path,
        )
        shutil.copytree(root / "work" / ".git", root / "pristine.git", symlinks=True)
        if session:
            unpack(turn.get("retained"), root / "retained")
        payload = {
            **request,
            "workspace": str(root / "work"),
            "scratch": str(root / "scratch"),
            "retained_root": str(root / "retained"),
            "timeout": timeout,
        }
        if turn.get("feedback"):
            payload["feedback_directory"] = feedback.materialize(turn["feedback"], root / "feedback")
        code, stdout = _run_agent(payload, root, key, stopped, timeout)
        result, response = outcome(code, stdout)
        if runner_bound(request["stage"]) and (
            code in (*KILLED, *SIGNALLED) or (stopped is not None and stopped.is_set())
        ):
            result, response = TurnOutcome.TERMINATED, None
        reply = {
            "outcome": result,
            "response": response,
            "workspace": None,
            "retained": pack(root / "retained") if session else None,
        }
        if result in (TurnOutcome.TIMED_OUT, TurnOutcome.TERMINATED):
            reply["termination_reason"] = _termination(code, stopped)
            reply["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if result == TurnOutcome.OK and request["stage"] == "develop":
            try:
                reply["workspace"] = extract(
                    root / "work", root / "pristine.git", root, spec.get("workspace_files_limit")
                )
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
