"""Timing observations cross the lazy executor without changing its initialization."""

import unittest
from types import SimpleNamespace

from service.worker import DeferredExecutor


class DeferredTimingTests(unittest.TestCase):
    """Reading absent timing must never authorize or initialize a runner."""

    def test_observations_are_forwarded_without_initializing(self):
        calls = []
        observed = {
            "turn": "12345678-1234-1234-1234-123456789abc",
            "status": "done",
            "created": 1,
            "started": 2,
            "finished": 3,
        }

        def build():
            calls.append("build")
            return SimpleNamespace(timing=observed, run=lambda *args: {"outcome": "ok"})

        executor = DeferredExecutor(build)
        self.assertIsNone(executor.timing)
        self.assertEqual(calls, [])
        self.assertEqual(executor.run({}, "test"), {"outcome": "ok"})
        self.assertEqual(executor.timing, observed)
        self.assertEqual(calls, ["build"])
