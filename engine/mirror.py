"""Host-side Git mirror of a target: fetched outside any container, borrowed read-only by snapshots."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from policy import Blocked
from repository import valid_ref

SHA = re.compile(r"[0-9a-f]{40}")
SNAPSHOT_REFS = "refs/concurro/snapshots/"
# Fetched or borrowed objects are pruned at most this often, and never while a snapshot borrows them.
MAINTENANCE_SECONDS = 86400


def git_environment():
    """No system, global or interactive Git configuration, and no credentials."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true", "LC_ALL": "C"}


def default_root():
    """Where mirrors live when the caller names no directory."""
    return Path(os.environ.get("CONCURRO_MIRROR_DIR") or Path(tempfile.gettempdir()) / "concurro-mirrors")


class Mirror:
    """One bare repository per target; commits are fetched by full SHA and pinned while snapshots use them."""

    def __init__(self, root, repository, url=None, sources=()):
        """`url` defaults to the public GitHub repository; an empty URL serves only commits already present."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise Blocked("invalid mirror repository")
        self.root = Path(root)
        self.path = self.root / (repository.replace("/", "__") + ".git")
        self.url = f"https://github.com/{repository}.git" if url is None else url
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source) for source in sources):
            raise Blocked("invalid mirror source")
        self.sources = tuple(f"https://github.com/{source}.git" for source in sources)

    def git(self, *args, timeout=60):
        """Run Git against the mirror only."""
        result = subprocess.run(["git", "--git-dir", str(self.path), *args], capture_output=True,
                                env=git_environment(), timeout=timeout)
        return result

    @contextmanager
    def locked(self):
        """Serialize fetches, pins and maintenance across ticks."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / (self.path.name + ".lock"), "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def initialize(self):
        """Create the bare mirror, readable by the unprivileged tool container user."""
        if self.path.exists():
            return
        if subprocess.run(["git", "init", "--quiet", "--bare", str(self.path)], capture_output=True,
                          env=git_environment(), timeout=60).returncode:
            raise Blocked("repository mirror could not be created")
        for key, value in (("gc.auto", "0"), ("core.sharedRepository", "0644"), ("fetch.writeFetchHEAD", "false")):
            self.git("config", key, value)

    def present(self, sha):
        """Whether a commit and everything it needs are in the mirror."""
        return not self.git("cat-file", "-e", sha + "^{commit}").returncode

    def borrow(self, name, branch, commits):
        """Fetch any missing commits and pin them for a snapshot, in one critical section against maintenance.

        A failure releases any pin already made, so nothing is left blocking later maintenance.
        """
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name) or not valid_ref(branch)
                or not commits or not all(SHA.fullmatch(c) for c in commits)):
            raise Blocked("invalid mirror request")
        with self.locked():
            self.initialize()
            try:
                self.fetch(branch, commits)
                for index, sha in enumerate(commits):
                    if self.git("update-ref", f"{SNAPSHOT_REFS}{name}/{index}", sha).returncode:
                        raise Blocked("repository mirror could not pin a snapshot")
            except BaseException:
                self.drop(name)
                raise

    def release(self, name):
        """Release a snapshot's pins."""
        with self.locked():
            self.drop(name)

    def fetch(self, branch, commits):
        """Fetch missing commits with the base branch, so later fetches negotiate incrementally; the lock is held."""
        missing = sorted({c for c in commits if not self.present(c)})
        if missing and self.url:
            self.git("fetch", "--quiet", "--no-tags", self.url,
                     f"+refs/heads/{branch}:refs/heads/{branch}", timeout=1800)
            missing = [sha for sha in missing if not self.present(sha)]
        for url in dict.fromkeys((self.url, *self.sources)):
            if not missing or not url:
                continue
            # The approved base and published fork head may be reachable in different repositories.
            for sha in tuple(missing):
                self.git("fetch", "--quiet", "--no-tags", url, sha, timeout=1800)
                if self.present(sha):
                    missing.remove(sha)
        if not all(self.present(c) for c in missing):
            raise Blocked("repository mirror lacks a pinned commit")

    def drop(self, name):
        """Delete a snapshot's pins; the lock is held."""
        listed = self.git("for-each-ref", "--format=%(refname)", SNAPSHOT_REFS + name + "/")
        for ref in listed.stdout.decode().split():
            self.git("update-ref", "-d", ref)

    def maintain(self, now=None):
        """Prune at most once a day, and only while no snapshot borrows objects."""
        now = time.time() if now is None else now
        stamp = self.root / (self.path.name + ".maintained")
        with self.locked():
            if not self.path.exists() or stamp.exists() and now - stamp.stat().st_mtime < MAINTENANCE_SECONDS:
                return False
            if self.git("for-each-ref", "--count=1", SNAPSHOT_REFS).stdout.strip():
                return False
            self.git("gc", "--quiet", "--prune=now", timeout=1800)
            stamp.touch()
            os.utime(stamp, (now, now))
            return True
