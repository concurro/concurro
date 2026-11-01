"""Cleanup failures never replace an exception already propagating, such as a gateway stop."""

import os
import subprocess
import unittest
from unittest.mock import patch

from support import CONFIG, EXECUTION
import cleanup
from local import LocalWorkspace

TIMEOUT = subprocess.TimeoutExpired("docker", 30)


class Stop(BaseException):
    """Stands in for a control exception that bypasses `except Exception`."""


def run(command, **_):
    """Docker succeeds except for removals, which time out."""
    if "rm" in command:
        raise TIMEOUT
    return subprocess.CompletedProcess(command, 0, b"", b"")


class CleanupTests(unittest.TestCase):
    """Removal errors surface only on a normal exit."""

    def test_removal_failure_surfaces_on_normal_exit(self):
        """Nothing else is propagating, so a leaked resource is still reported."""
        with patch("cleanup.subprocess.run", side_effect=TIMEOUT), self.assertRaises(subprocess.TimeoutExpired):
            cleanup.remove(["docker", "rm", "-f", "x"], {})

    def test_removal_failure_preserves_a_propagating_stop(self):
        """Inside a `finally` reached by a stop, the stop keeps propagating."""
        with patch("cleanup.subprocess.run", side_effect=TIMEOUT), self.assertRaises(Stop):
            try:
                raise Stop()
            finally:
                cleanup.remove(["docker", "rm", "-f", "x"], {})

    def test_workspace_exit_preserves_a_stop(self):
        """The local workbench's volume cleanup times out after a stop inside its context."""
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64}):
            workspace = LocalWorkspace(CONFIG, b"", "concurro/test", EXECUTION)
        with patch("local.subprocess.run", side_effect=run), \
                patch.object(workspace, "control", return_value={"tree": "t"}):
            with self.assertRaises(Stop):
                with workspace:
                    raise Stop()
            with self.assertRaises(subprocess.TimeoutExpired):
                with workspace:
                    pass


if __name__ == "__main__":
    unittest.main()
