"""Pinned Git snapshots and source-line identities used by native runner turns."""

import os
import re
import subprocess
import tempfile
from pathlib import Path

import git_env
import protocol

ROOT = Path("/repository")
MIRROR = Path("/mirror")
ATTRIBUTES = "* diff -text -filter -ident -working-tree-encoding\n"
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
SHA = re.compile(protocol.SHA_PATTERN)
CANDIDATE_IDENTITY = {
    "GIT_AUTHOR_NAME": "Concurro",
    "GIT_AUTHOR_EMAIL": "concurro@invalid",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "Concurro",
    "GIT_COMMITTER_EMAIL": "concurro@invalid",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
}


def git_environment(root):
    """Bind Git to the snapshot, with no system, global or repository-supplied programs."""
    return {
        **git_env.isolated_git_env(),
        "GIT_DIR": str(root / ".git"),
        "GIT_WORK_TREE": str(root),
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
        # Preparation runs as root and serving as the tool user; this snapshot is trusted either way.
        **git_env.safe_directory(str(root)),
    }


def source_lines(text):
    """Count LF-delimited source lines, preserving other Unicode separators as content."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def valid_ref(name):
    """A branch name Git accepts, without traversal or lock-file shapes."""
    return (
        isinstance(name, str)
        and REF.fullmatch(name)
        and ".." not in name
        and "//" not in name
        and not name.endswith((".", "/", ".lock"))
        and "/." not in name
    )


def snapshot(spec, root=ROOT, mirror=MIRROR):
    """Build the model-visible repository: real refs borrowed from the mirror, plus an optional candidate commit.

    Runs during credential-free, offline preparation. `spec` is `{"refs": {name: sha}, "head": name,
    "candidate": {"files": {path: text or None}} or None}`; a candidate is committed on the head ref.
    """
    from policy import path_safe

    refs, head, candidate = spec["refs"], spec["head"], spec.get("candidate")
    if (
        not isinstance(refs, dict)
        or not 1 <= len(refs) <= 8
        or head not in refs
        or not all(valid_ref(n) and isinstance(c, str) and SHA.fullmatch(c) for n, c in refs.items())
    ):
        raise ValueError("invalid snapshot refs")
    maximum = spec.get("overlay_files_limit")
    if maximum is not None and (type(maximum) is not int or maximum < 1):
        raise ValueError("invalid limits.overlay_files")
    if candidate is not None:
        files = candidate.get("files") if isinstance(candidate, dict) else None
        if (
            set(candidate) != {"files"}
            or not isinstance(files, dict)
            or not files
            or maximum is not None
            and len(files) > maximum
            or not all(
                path_safe(path) and (text is None or isinstance(text, str) and "\x00" not in text)
                for path, text in files.items()
            )
        ):
            raise ValueError("invalid snapshot candidate")
    environment = git_environment(root)

    def git(*args, data=None, extra=None):
        result = subprocess.run(
            ["git", *args], input=data, capture_output=True, timeout=600, env={**environment, **(extra or {})}
        )
        if result.returncode:
            raise ValueError("snapshot preparation failed: git " + args[0])
        return result.stdout.decode().strip()

    git("init", "--quiet")
    git_dir = root / ".git"
    (git_dir / "objects" / "info" / "alternates").write_text(str(mirror / "objects") + "\n")
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "attributes").write_text(ATTRIBUTES)
    for key, value in (
        ("core.symlinks", "false"),
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
        ("core.autocrlf", "false"),
        ("gc.auto", "0"),
        ("advice.detachedHead", "false"),
    ):
        git("config", key, value)
    for name, sha in refs.items():
        git("check-ref-format", "refs/heads/" + name)
        git("update-ref", "refs/heads/" + name, git("rev-parse", "--verify", sha + "^{commit}"))
    if candidate is not None:
        parent = refs[head]
        index = {"GIT_INDEX_FILE": str(Path(tempfile.mkdtemp()) / "candidate.index")}
        git("read-tree", parent, extra=index)
        for path, text in sorted(candidate["files"].items()):
            listed = git("ls-tree", parent, "--", path)
            if text is None:
                if listed:
                    if listed.split()[0] not in ("100644", "100755"):
                        raise ValueError("a candidate can only remove regular files")
                    git("update-index", "--force-remove", "--", path, extra=index)
                continue
            mode = listed.split()[0] if listed else "100644"
            if mode not in ("100644", "100755"):
                raise ValueError("a candidate can only replace regular files")
            blob = git("hash-object", "-w", "--no-filters", "--stdin", data=text.encode())
            git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", extra=index)
        tree = git("write-tree", extra=index)
        commit = git("commit-tree", tree, "-p", parent, "-m", "Concurro candidate", extra=CANDIDATE_IDENTITY)
        git("update-ref", "refs/heads/" + head, commit)
    git("symbolic-ref", "HEAD", "refs/heads/" + head)
    git("read-tree", "--reset", "-u", "HEAD")
