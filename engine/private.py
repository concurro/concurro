"""Owner-only files and directories for credentials and operator settings."""

import json
import os
import stat
from pathlib import Path

from policy import Blocked

LIMIT = 1_000_000


def outside(path, roots):
    """Refuse private material inside any of `roots` (repository checkouts)."""
    path = Path(path).expanduser()
    if any(path.resolve().is_relative_to(Path(root).resolve()) for root in roots):
        raise Blocked("private material must stay outside the repository")
    return path


def directory(path, roots=()):
    """Require an existing owner-only directory that is not a symlink."""
    path = outside(path, roots)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Blocked("private directory must be owned by you with mode 700")
    return path


def read(path, roots=(), limit=LIMIT):
    """Read a bounded owner-only regular file without following a final symlink."""
    path = outside(path, roots)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Blocked("private files must be owned by you with mode 600")
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise Blocked("private file exceeds size limit")
        return data


def write(path, data, roots=()):
    """Create a private JSON file exclusively; never replace an existing credential."""
    path = outside(path, roots)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink():
        raise Blocked("private directory must be owned by you with mode 700")
    directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
