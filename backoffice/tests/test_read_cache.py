"""Persistent display reads: restart, concurrent refresh, failure deadlines and command races."""

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backoffice.read_cache import ReadCache  # noqa: E402
from checkpoints import CheckpointError  # noqa: E402
from github import NotFound, Transient  # noqa: E402


class ReadCacheTests(unittest.TestCase):
    """Drive refreshes with a fixed clock and explicit completion events."""

    def setUp(self):
        """Create an isolated on-disk store for every test."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000
        self.cache = ReadCache(Path(self.directory.name), clock=lambda: self.now)
        self.addCleanup(self.cache.close)

    def test_persistence_and_scope_isolation(self):
        """Fresh verified data survives process caches and never crosses configuration or repository scopes."""
        load = Mock(return_value={"title": "saved"})
        data, meta = self.cache.read("config-a", "o/r", 7, load)
        data["title"] = "caller mutation"
        self.cache.close()
        self.cache = ReadCache(Path(self.directory.name), clock=lambda: self.now)
        self.addCleanup(self.cache.close)
        self.assertEqual(self.cache.read("config-a", "O/R", 7, load)[0], {"title": "saved"})
        self.assertEqual(load.call_count, 1)
        self.assertFalse(meta["stale"])
        self.cache.read("config-b", "o/r", 7, load)
        self.cache.read("config-a", "other/r", 7, load)
        self.assertEqual(load.call_count, 3)

    def test_cold_requests_share_one_read(self):
        """Simultaneous cold page sections wait for the same source read."""
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def load():
            """Hold GitHub until every caller can share the pending read."""
            started.set()
            self.assertTrue(release.wait(5))
            return {"revision": "one"}

        loader = Mock(side_effect=load)
        with ThreadPoolExecutor(8) as callers:
            reads = [callers.submit(self.cache.read, "s", "o/r", 1, loader) for _ in range(8)]
            self.assertTrue(started.wait(5))
            release.set()
            for result in reads:
                self.assertEqual(result.result(5)[0], {"revision": "one"})
        self.assertEqual(loader.call_count, 1)

    def test_stale_reads_return_immediately_and_refresh_once(self):
        """A slow background read does not block any stale response or duplicate its GitHub work."""
        self.cache.read("s", "o/r", 1, lambda: {"revision": "old"})
        self.now += 61
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def load():
            """Expose a pending refresh while callers read the saved value."""
            started.set()
            self.assertTrue(release.wait(5))
            return {"revision": "new"}

        loader = Mock(side_effect=load)
        for _ in range(8):
            data, meta = self.cache.read("s", "o/r", 1, loader)
            self.assertEqual(data["revision"], "old")
            self.assertTrue(meta["stale"])
            self.assertTrue(meta["refreshing"])
        self.assertTrue(started.wait(5))
        pending = self.cache.pending["s", "o/r", "1"][0]
        release.set()
        pending.result(5)
        self.assertEqual(self.cache.read("s", "o/r", 1, loader)[0]["revision"], "new")
        self.assertEqual(loader.call_count, 1)

    def test_rate_limit_survives_restart_and_explicit_refresh(self):
        """A failed refresh retains good data and honors GitHub's entire retry deadline."""
        self.cache.read("s", "o/r", 1, lambda: {"revision": "saved"})
        self.now += 61
        loader = Mock(side_effect=Transient("throttled", retry_after=3600))
        with self.assertRaises(Transient):
            self.cache.read("s", "o/r", 1, loader, refresh=True)
        self.cache.close()
        self.cache = ReadCache(Path(self.directory.name), clock=lambda: self.now)
        self.addCleanup(self.cache.close)
        self.now += 3599
        data, meta = self.cache.read("s", "o/r", 1, loader, refresh=True)
        self.assertEqual(data["revision"], "saved")
        self.assertTrue(meta["stale"])
        self.assertFalse(meta["refreshing"])
        self.assertIn("rate limit", meta["error"])
        self.assertEqual(loader.call_count, 1)
        self.now += 1
        loader.side_effect = None
        loader.return_value = {"revision": "current"}
        self.assertEqual(self.cache.read("s", "o/r", 1, loader, refresh=True)[0]["revision"], "current")
        self.assertEqual(loader.call_count, 2)

    def test_unexpected_refresh_failure_stops_polling_until_retry_deadline(self):
        """Polling an internal failure retains the snapshot without starting another GitHub refresh."""
        saved, observed = self.cache.read("s", "o/r", 1, Mock(return_value={"revision": "saved"}))
        loader = Mock(side_effect=[RuntimeError("upstream failure"), {"revision": "recovered"}])
        with self.assertRaises(RuntimeError):
            self.cache.read("s", "o/r", 1, loader, refresh=True)
        for second in range(60):
            if second:
                self.now += 1
            for refresh in (False, True):
                data, meta = self.cache.read("s", "o/r", 1, loader, refresh=refresh)
                self.assertEqual(data, saved)
                self.assertEqual(meta["observedAt"], observed["observedAt"])
                self.assertEqual(meta["error"], "The latest issue refresh failed.")
                self.assertTrue(meta["stale"])
                self.assertFalse(meta["refreshing"])
        self.assertEqual(loader.call_count, 1)
        self.now += 1
        data, meta = self.cache.read("s", "o/r", 1, loader, refresh=True)
        self.assertEqual(data, {"revision": "recovered"})
        self.assertIsNone(meta["error"])
        self.assertFalse(meta["refreshing"])
        self.assertEqual(loader.call_count, 2)

    def test_cold_failures_do_not_retry_on_every_navigation(self):
        """A new issue under throttling returns a retryable error without repeatedly hitting GitHub."""
        loader = Mock(side_effect=Transient("throttled", retry_after=120))
        for _ in range(3):
            with self.assertRaises(Transient):
                self.cache.read("s", "o/r", 1, loader)
        self.assertEqual(loader.call_count, 1)

    def test_not_found_discards_previously_saved_content(self):
        """A confirmed missing issue cannot keep serving its old local snapshot."""
        self.cache.read("s", "o/r", 1, lambda: {"title": "old"})
        loader = Mock(side_effect=NotFound())
        with self.assertRaises(NotFound):
            self.cache.read("s", "o/r", 1, loader, refresh=True)
        with self.assertRaises(NotFound):
            self.cache.read("s", "o/r", 1, loader)
        self.assertEqual(loader.call_count, 1)

    def test_command_invalidation_discards_late_refresh(self):
        """A pre-command refresh cannot repopulate snapshots invalidated while it was in flight."""
        self.cache.read("s", "o/r", 1, lambda: {"revision": "old"})
        self.now += 61
        release = threading.Event()
        self.addCleanup(release.set)

        def old():
            """Finish an obsolete read after the command's cache invalidation."""
            self.assertTrue(release.wait(5))
            return {"revision": "obsolete"}

        self.cache.read("s", "o/r", 1, old)
        pending = self.cache.pending["s", "o/r", "1"][0]
        self.cache.invalidate("O/R", 1)
        release.set()
        pending.result(5)
        loader = Mock(return_value={"revision": "after-command"})
        self.assertEqual(self.cache.read("s", "o/r", 1, loader, refresh=True)[0]["revision"], "after-command")
        self.assertEqual(loader.call_count, 1)

    def test_checkpoint_errors_keep_their_public_diagnostic(self):
        """Cold cached failures preserve their 502 checkpoint identity and recover after a brief race delay."""
        loader = Mock(side_effect=CheckpointError("concurrent_change"))
        for _ in range(2):
            with self.assertRaises(CheckpointError) as failed:
                self.cache.read("s", "o/r", 1, loader)
            self.assertEqual(failed.exception.code, "concurrent_change")
        self.assertEqual(loader.call_count, 1)
        self.now += 2
        loader.side_effect = None
        loader.return_value = {"revision": "recovered"}
        self.assertEqual(self.cache.read("s", "o/r", 1, loader)[0], loader.return_value)

    def test_invalidation_keeps_saved_data_during_a_rate_limit(self):
        """Commands never turn a rate-limited but readable snapshot into an hour-long outage."""
        self.cache.read("s", "o/r", 1, lambda: {"revision": "saved"})
        loader = Mock(side_effect=Transient("throttled", 3600))
        with self.assertRaises(Transient):
            self.cache.read("s", "o/r", 1, loader, refresh=True)
        self.cache.invalidate("o/r", 1)
        data, meta = self.cache.read("s", "o/r", 1, loader)
        self.assertEqual(data["revision"], "saved")
        self.assertTrue(meta["stale"])
        self.assertFalse(meta["refreshing"])
        self.assertEqual(loader.call_count, 1)

    def test_post_command_snapshot_remains_pending_until_checkpoint_changes(self):
        """An immediate pre-effect read gets a short TTL rather than claiming the command is reflected."""
        before = {"state": {"_revision": "before"}, "issue": {"updated_at": "old"}}
        self.cache.read("s", "o/r", 1, lambda: before)
        self.cache.invalidate("o/r", 1)
        data, meta = self.cache.read(
            "s", "o/r", 1, lambda: {**before, "issue": {"updated_at": "comment posted"}}, refresh=True
        )
        self.assertTrue(meta["pendingCommand"])
        self.assertTrue(meta["stale"])
        self.now += 5
        changed = {"state": {"_revision": "after"}}
        data, meta = self.cache.read("s", "o/r", 1, lambda: changed, refresh=True)
        self.assertEqual(data, changed)
        self.assertFalse(meta["pendingCommand"])
        self.assertFalse(meta["stale"])

    def test_obsolete_failure_does_not_fail_new_reader(self):
        """An invalidated missing response cannot reject a reader waiting for post-command data."""
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def load():
            """Fail only the pre-command attempt."""
            calls.append(1)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(5))
                raise NotFound()
            return {"revision": "new"}

        with ThreadPoolExecutor(1) as callers:
            result = callers.submit(self.cache.read, "s", "o/r", 1, load)
            self.assertTrue(started.wait(5))
            self.cache.invalidate("o/r", 1)
            release.set()
            self.assertEqual(result.result(5)[0], {"revision": "new"})
        self.assertEqual(len(calls), 2)

    def test_late_rate_limit_survives_command_invalidation(self):
        """Discarding old results must not discard a newly received GitHub retry deadline."""
        self.cache.read("s", "o/r", 1, lambda: {"revision": "saved"})
        self.now += 61
        release = threading.Event()
        self.addCleanup(release.set)

        def load():
            """Return throttling after the command has invalidated this attempt."""
            self.assertTrue(release.wait(5))
            raise Transient("throttled", 3600)

        self.cache.read("s", "o/r", 1, load)
        future = self.cache.pending["s", "o/r", "1"][0]
        self.cache.invalidate("o/r", 1)
        release.set()
        with self.assertRaises(Transient):
            future.result(5)
        loader = Mock(return_value={"revision": "wrong"})
        self.now += 3599
        self.assertEqual(self.cache.read("s", "o/r", 1, loader)[0]["revision"], "saved")
        loader.assert_not_called()

    def test_superseded_configurations_are_retired(self):
        """A successful replacement scope does not leave old issue bodies growing on disk."""
        for scope in ("a", "b", "c"):
            self.cache.read(scope, "o/r", 1, lambda: {"state": {}})
        self.assertEqual([r[0] for r in self.cache.db.execute("SELECT scope FROM snapshots")], ["c"])

    def test_large_issue_numbers_retain_normal_missing_behavior(self):
        """GitHub handles nonexistent issue identities without a SQLite integer overflow."""
        with self.assertRaises(NotFound):
            self.cache.read("s", "o/r", 2**80, Mock(side_effect=NotFound()))

    def test_explicit_refresh_waits_for_post_invalidation_success(self):
        """An obsolete successful future cannot satisfy a caller requesting current data."""
        self.cache.read("s", "o/r", 1, lambda: {"revision": "saved"})
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def load():
            """Pause the first refresh while a command invalidates its result."""
            calls.append(1)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(5))
                return {"revision": "obsolete"}
            return {"revision": "current"}

        with ThreadPoolExecutor(1) as callers:
            result = callers.submit(self.cache.read, "s", "o/r", 1, load, refresh=True)
            self.assertTrue(started.wait(5))
            self.cache.invalidate("o/r", 1)
            release.set()
            self.assertEqual(result.result(5)[0], {"revision": "current"})
        self.assertEqual(len(calls), 2)
