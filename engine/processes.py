"""Reap native turn descendants on Linux, including tools that detach with setsid."""

import ctypes
import os
from pathlib import Path
import signal
import sys
import time


def children():
    """Direct children of the runner, including orphaned descendants adopted by its subreaper."""
    result = set()
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            if int(fields[1]) == os.getpid():
                result.add(int(path.parent.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def begin():
    """Enable orphan adoption before a native turn; preserve pre-existing helper children."""
    if sys.platform != 'linux':
        return None  # Offline development tests; production runs on Linux GitHub runners.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'cannot contain native turn descendants')
    return children()


def finish(baseline, timeout=5):
    """Kill/reap adopted children until empty, or refuse further work in this job."""
    if baseline is None:
        return
    deadline = time.monotonic() + timeout
    while pending := children() - baseline:
        for pid in pending:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError('native turn descendants did not exit')
        time.sleep(0.01)
