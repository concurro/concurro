"""The real engine under the gateway: stops pass every wrapper, and lost writes are reconstructed."""

import subprocess
import unittest
from unittest.mock import patch

from fixtures import KEY, ServiceFixture
from support import setUpModule, tearDownModule  # unittest module fixtures
from test_pipeline import FakeWorkspace
from checkpoint_health import sync_checkpoint_health
from controller import MARKER
import cleanup
import conversation
from service.errors import OwnershipLost, Unresolved
from service.gateway import LEASE_SECONDS


class CleanupWorkspace(FakeWorkspace):
    """Removes a Docker resource on exit, as the local workbench does."""

    inside = False

    def __enter__(self):
        CleanupWorkspace.inside = True
        return self

    def __exit__(self, *_):
        CleanupWorkspace.inside = False
        cleanup.remove(["docker", "volume", "rm", "-f", "fixture"], {})


class StopTests(ServiceFixture, unittest.TestCase):
    """A takeover mid-tick stops the old generation inside each best-effort or wrapping engine path."""

    def stop_on(self, matches, workspace=FakeWorkspace, ticks=12):
        """Tick until a matching write, take over just before it, and return the escaping exception."""
        original = self.gateway.mutate
        self.after = None

        def mutate(key, generation, method, path, body=None):
            if self.after is not None:
                self.after += 1
            elif matches(method, path, body or {}):
                self.clock.now += LEASE_SECONDS + 1
                self.assertIsNotNone(self.gateway.acquire(KEY, "successor"))
                self.after = 0
            return original(key, generation, method, path, body)

        with patch.object(self.gateway, "mutate", side_effect=mutate):
            for _ in range(ticks):
                try:
                    self.tick(workspace)
                except BaseException as error:  # noqa: B036  (the type itself is under test)
                    self.assertEqual(self.after, 0, "a write was attempted after the stop")
                    return error
        self.fail("no matching write")

    def assert_lost(self, error):
        """The stop reached the caller unwrapped, and the gateway recorded it for the worker."""
        self.assertIs(type(error), OwnershipLost)
        generation = self.registry.current(KEY).generation - 1
        self.assertIsInstance(self.gateway.stop(KEY, generation), OwnershipLost)

    def test_checkpoint_save(self):
        """`Checkpoints.request` wraps every `Exception` as a checkpoint error, but not a stop."""
        self.assert_lost(self.stop_on(lambda method, path, body: method == "PATCH" and "/artifacts/" in path))

    def test_conversation_publication(self):
        """Publication wraps failures as `PublicationError`, but not a stop."""
        self.assert_lost(self.stop_on(lambda method, path, body: path.endswith("/comments")
                                      and body.get("body", "").startswith(conversation.MARKER)))

    def test_stage_label_sync(self):
        """Label sync swallows failures as display-only, but not a stop."""
        self.assert_lost(self.stop_on(lambda method, path, body: path.endswith("/issues/7/labels")))

    def test_health_reporting(self):
        """Health reporting swallows every failure, but not a stop."""
        stale = self.facade(self.gateway.acquire(KEY, "worker"))
        self.clock.now += LEASE_SECONDS + 1
        self.gateway.acquire(KEY, "successor")
        with patch.object(self.gateway, "mutate", wraps=self.gateway.mutate) as mutate:
            with self.assertRaises(OwnershipLost):
                sync_checkpoint_health(stale, self.config, 7, failed=True)
        self.assertEqual(mutate.call_count, 1)
        self.assertEqual(self.backend.writes, [])

    def test_cleanup_timeout_after_a_stop(self):
        """Workbench cleanup times out while the stop propagates; the stop still reaches the worker."""
        timeout = subprocess.TimeoutExpired("docker", 30)
        with patch("cleanup.subprocess.run", side_effect=timeout) as run:
            error = self.stop_on(lambda *_: CleanupWorkspace.inside, workspace=CleanupWorkspace)
        self.assert_lost(error)
        run.assert_called_once()


class ReconstructionTests(ServiceFixture, unittest.TestCase):
    """An applied write whose response was lost is resolved `applied`; the next tick finds its result."""

    def lose(self, method, prefix, body=""):
        """Lose the response to one write, resolve it as applied, and tick on to review."""
        self.backend.fail(method, prefix, lost=True, body=body)
        for _ in range(12):
            try:
                state, _ = self.tick()
            except Unresolved:
                [record] = self.registry.unresolved(KEY)
                self.assertTrue(record.path.endswith(prefix))
                self.registry.resolve(record.intent, "applied", "object observed on GitHub", "operator")
                continue
            if state and state["stage"] == "review":
                break
        self.assertEqual(self.backend.faults, [])
        self.assertEqual(state["stage"], "review")
        return state

    def assert_single(self, method, path):
        """The write was sent exactly once."""
        self.assertEqual(self.backend.writes.count((method, path)), 1)

    def test_audit_artifact(self):
        """The audit artifact is found by creator and name, not created twice."""
        self.lose("POST", "issues/7/artifacts")
        kinds = [item["artifact"]["type"] for item in self.fake.artifacts[7].values()]
        self.assertEqual(sorted(kinds), ["generic", "plan"])

    def test_summary_comment(self):
        """The summary comment is found by its marker and author."""
        self.lose("POST", "issues/7/comments", body=MARKER)
        self.assertEqual(len([c for c in self.fake.comments if c["body"].startswith(MARKER)]), 1)

    def test_pull_request(self):
        """The PR is found by its head branch."""
        state = self.lose("POST", "pulls")
        self.assert_single("POST", "pulls")
        self.assertEqual(state["pr"], self.fake.pr["number"])


if __name__ == "__main__":
    unittest.main()
