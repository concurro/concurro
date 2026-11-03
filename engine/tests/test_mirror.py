"""The host-side target mirror: fetch by full SHA, pin while borrowed, prune only when nothing is borrowed."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule  # unittest module fixtures
from gitfixture import Upstream, mirror_of
from mirror import SNAPSHOT_REFS, Mirror
from policy import Blocked


class MirrorTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.upstream = Upstream(self.root / "upstream.git")
        self.base = self.upstream.commit({"a": "a\n"}, branch="main", message="base")
        self.mirror = mirror_of(self.upstream, self.root / "mirrors")

    def pins(self):
        return self.mirror.git("for-each-ref", "--format=%(refname)", SNAPSHOT_REFS).stdout.decode().split()

    def test_fetches_a_non_tip_commit_by_full_sha(self):
        branch = self.upstream.commit({"b": "b\n"}, parent=self.base, branch="feature", message="feature")
        self.upstream.commit({"c": "c\n"}, parent=branch, branch="feature", message="later")
        self.mirror.borrow("snap", "main", [branch])
        self.assertTrue(self.mirror.present(branch))
        self.assertTrue(self.mirror.present(self.base))
        self.assertEqual(self.pins(), [SNAPSHOT_REFS + "snap/0"])
        self.assertEqual(self.mirror.git("config", "core.sharedRepository").stdout.strip(), b"0644")

    def test_an_empty_url_serves_only_present_commits(self):
        offline = Mirror(self.root / "offline", "owner/target", url="")
        with self.assertRaisesRegex(Blocked, "lacks a pinned commit"):
            offline.borrow("snap", "main", [self.base])
        for name, branch, commits in (("snap", "main", ["abc"]), ("snap", "main;rm", [self.base]),
                                      ("snap", "..", [self.base]), ("../x", "main", [self.base]), ("snap", "main", [])):
            with self.subTest(name=name, branch=branch), self.assertRaises(Blocked):
                self.mirror.borrow(name, branch, commits)

    def test_a_failed_borrow_leaves_no_pin(self):
        missing = "f" * 40
        with self.assertRaises(Blocked):
            self.mirror.borrow("snap", "main", [self.base, missing])
        self.assertEqual(self.pins(), [])
        real = Mirror.git

        def refuse_second_pin(mirror, *args, **kwargs):
            if args[:2] == ("update-ref", SNAPSHOT_REFS + "snap/1"):
                return real(mirror, "update-ref", "refs/heads/bad..ref", self.base)
            return real(mirror, *args, **kwargs)

        side = self.upstream.commit({"d": "d\n"}, parent=self.base, branch="side", message="side")
        with patch.object(Mirror, "git", refuse_second_pin), self.assertRaisesRegex(Blocked, "could not pin"):
            self.mirror.borrow("snap", "main", [self.base, side])
        self.assertEqual(self.pins(), [], "the first pin is rolled back")

    def test_maintenance_never_prunes_borrowed_objects(self):
        unreferenced = self.upstream.commit({"d": "d\n"}, parent=self.base, branch="side", message="side")
        now = time.time() + 2 * 86400
        self.mirror.borrow("snap", "main", [unreferenced])
        self.assertFalse(self.mirror.maintain(now=now))
        self.assertTrue(self.mirror.present(unreferenced))
        self.mirror.release("snap")
        self.assertTrue(self.mirror.maintain(now=now))
        self.assertFalse(self.mirror.present(unreferenced))
        self.assertTrue(self.mirror.present(self.base))
        self.assertFalse(self.mirror.maintain(now=now + 60), "at most once a day")

    def test_maintenance_cannot_run_between_fetch_and_pin(self):
        """Another snapshot's cleanup waits for the whole borrow, so a freshly fetched commit is pinned first."""
        unreferenced = self.upstream.commit({"d": "d\n"}, parent=self.base, branch="side", message="side")
        now = time.time() + 2 * 86400
        results, fetched = [], threading.Event()
        real = Mirror.fetch

        def fetch_then_race(mirror, branch, commits):
            real(mirror, branch, commits)
            fetched.set()
            time.sleep(0.5)

        cleanup = threading.Thread(target=lambda: fetched.wait(10) and results.append(self.mirror.maintain(now=now)))
        cleanup.start()
        with patch.object(Mirror, "fetch", fetch_then_race):
            self.mirror.borrow("snap", "main", [unreferenced])
        cleanup.join(10)
        self.assertEqual(results, [False])
        self.assertTrue(self.mirror.present(unreferenced))
        self.assertEqual(self.pins(), [SNAPSHOT_REFS + "snap/0"])

    def test_fetch_and_pins_share_one_lock_section(self):
        events, real_locked, real_fetch, real_git = [], Mirror.locked, Mirror.fetch, Mirror.git

        @contextmanager
        def locked(mirror):
            with real_locked(mirror):
                events.append("lock")
                yield
                events.append("unlock")

        def fetch(mirror, branch, commits):
            events.append("fetch")
            return real_fetch(mirror, branch, commits)

        def git(mirror, *args, **kwargs):
            if args[0] == "update-ref":
                events.append("pin")
            return real_git(mirror, *args, **kwargs)

        with patch.object(Mirror, "locked", locked), patch.object(Mirror, "fetch", fetch), patch.object(Mirror, "git", git):
            self.mirror.borrow("snap", "main", [self.base])
        self.assertEqual(events, ["lock", "fetch", "pin", "unlock"])


if __name__ == "__main__":
    unittest.main()
