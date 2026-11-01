"""Removal of local Docker resources that never masks an exception already propagating."""

import subprocess
import sys


def remove(command, env, timeout=30):
    """Run one removal; its own failure surfaces only when no other exception is in flight."""
    # Inside a `finally`, `except` or `__exit__` reached by an exception, that exception is current.
    pending = sys.exc_info()[1] is not None
    try:
        subprocess.run(command, env=env, capture_output=True, timeout=timeout)
    except Exception:
        if not pending:
            raise
