"""Real Git fixtures: an upstream repository, a target mirror fetched from it, and built snapshots."""

import subprocess
from pathlib import Path

import repository
from mirror import Mirror, git_environment

IDENTITY = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@invalid",
}


class Upstream:
    """A bare repository whose commits are written with plumbing, byte for byte."""

    def __init__(self, path):
        self.path = Path(path)
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(self.path)],
            env=git_environment(),
            check=True,
            capture_output=True,
        )

    def git(self, *args, data=None, extra=None):
        """Run Git in the upstream and return its trimmed output."""
        return (
            subprocess.run(
                ["git", "--git-dir", str(self.path), *args],
                input=data,
                capture_output=True,
                env={**git_environment(), **IDENTITY, **(extra or {})},
                check=True,
            )
            .stdout.decode()
            .strip()
        )

    def commit(self, files, parent=None, branch=None, message="fixture", executable=()):
        """Commit `files` (path: bytes or text; None removes) on `parent`, optionally moving `branch`."""
        index = {"GIT_INDEX_FILE": str(self.path / ("index-" + message.replace(" ", "-")))}
        if parent:
            self.git("read-tree", parent, extra=index)
        else:
            self.git("read-tree", "--empty", extra=index)
        for name, content in files.items():
            if content is None:
                self.git("update-index", "--force-remove", name, extra=index)
                continue
            data = content.encode() if isinstance(content, str) else content
            blob = self.git("hash-object", "-w", "--no-filters", "--stdin", data=data)
            mode = "100755" if name in executable else "100644"
            self.git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{name}", extra=index)
        args = ["commit-tree", self.git("write-tree", extra=index), "-m", message]
        sha = self.git(*args, *(["-p", parent] if parent else []))
        if branch:
            self.git("update-ref", "refs/heads/" + branch, sha)
        return sha


def mirror_of(upstream, root, repository_name="owner/target"):
    """A target mirror that fetches from a local upstream."""
    return Mirror(root, repository_name, url="file://" + str(upstream.path))


def build(mirror, root, refs, head, candidate=None):
    """Build a snapshot in-process, exactly as the preparation container does, borrowing the mirror's objects."""
    mirror.borrow(root.name, "main", sorted(set(refs.values())))
    root.mkdir(parents=True, exist_ok=True)
    repository.snapshot({"refs": refs, "head": head, "candidate": candidate}, root=root, mirror=mirror.path)
    return repository.Repository(root)
