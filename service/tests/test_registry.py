"""The registry has one exclusive writer, never silently replaces lost state, and fails closed."""

import sqlite3
from pathlib import Path
import unittest

from fixtures import KEY, REGISTRY_ID, ServiceFixture
from support import setUpModule, tearDownModule  # unittest module fixtures
from controller import Outcome
from service.errors import RegistryError, RegistryFailure
from service.registry import DATABASE, Registry


class StartupTests(ServiceFixture, unittest.TestCase):
    """Opening the state directory."""

    def test_second_process_is_refused(self):
        """The state directory lock admits one writer; it is free again once released."""
        with self.assertRaisesRegex(RegistryError, "in use"):
            self.open()
        self.registry.close()
        self.open()

    def test_missing_empty_or_existing_store(self):
        """Lost state is never replaced by an empty registry without explicit initialization."""
        self.registry.close()
        path = Path(self.directory.name, DATABASE)
        with self.assertRaisesRegex(RegistryError, "already exists"):
            self.open(initialize=True)
        for wal in path.parent.glob(DATABASE + "-*"):
            wal.unlink()
        path.unlink()
        with self.assertRaisesRegex(RegistryError, "missing or empty"):
            self.open()
        path.touch(0o600)
        with self.assertRaisesRegex(RegistryError, "missing or empty"):
            self.open()

    def test_mismatched_identity_schema_or_corruption(self):
        """A different registry, schema version or unreadable file stops startup."""
        self.registry.close()
        with self.assertRaisesRegex(RegistryError, "mismatch"):
            Registry.open(self.directory.name, "another-registry", clock=self.clock)
        db = sqlite3.connect(Path(self.directory.name, DATABASE))
        db.execute("UPDATE meta SET value = '0' WHERE name = 'schema'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(RegistryError, "mismatch"):
            self.open()
        for path in Path(self.directory.name).glob(DATABASE + "*"):
            path.write_bytes(b"not a database" * 100)
        with self.assertRaisesRegex(RegistryError, "unreadable|integrity"):
            self.open()

    def test_shared_state_directory_is_refused(self):
        """Group- or world-accessible state is rejected before anything is read."""
        self.registry.close()
        Path(self.directory.name).chmod(0o750)
        with self.assertRaisesRegex(RegistryError, "private"):
            self.open()


class RecoveryTests(ServiceFixture, unittest.TestCase):
    """A crash between a commit and its action is recovered from the committed record."""

    def test_interrupted_write_becomes_unresolved(self):
        """A write admitted but never settled may have applied, so its task stops for the operator."""
        generation = self.registry.grant(KEY, "worker", 60)
        intent = self.registry.admit(KEY, generation, "POST", "repos/x/y/issues/7/comments", "d")
        self.registry.close()
        registry = self.open()
        self.assertEqual([r.intent for r in registry.unresolved(KEY)], [intent])
        self.assertEqual(registry.in_flight(KEY), [])

    def test_interrupted_tick_is_due_again(self):
        """A granted generation whose tick never finished ends, and its task is enqueued."""
        self.registry.enqueue(KEY, "webhook", self.clock.now + 50)
        generation = self.registry.grant(KEY, "worker", 60)
        self.assertEqual(self.registry.due(self.clock.now + 100), [])
        self.registry.close()
        registry = self.open()
        self.assertFalse(registry.current(KEY).live)
        self.assertEqual(registry.current(KEY).generation, generation)
        [item] = registry.due(self.clock.now)
        self.assertEqual((item.key, item.causes), (KEY, ("restart",)))
        self.assertEqual(registry.grant(KEY, "worker", 60), generation + 1)

    def test_storage_failure_fails_closed(self):
        """After any storage error nothing more is scheduled or admitted."""
        self.registry.db.close()
        with self.assertRaises(RegistryFailure):
            self.registry.due(self.clock.now)
        self.assertTrue(self.registry.failed)
        with self.assertRaises(RegistryFailure):
            self.registry.current(KEY)


class StateTests(ServiceFixture, unittest.TestCase):
    """Queue, deliveries, snapshots, outcomes and resolutions."""

    def test_causes_collapse_into_one_item(self):
        """One item per task at its earliest due time; due items oldest first."""
        other = (KEY[0], 8)
        self.registry.enqueue(KEY, "poll", 30)
        self.registry.enqueue(other, "webhook", 10)
        self.registry.enqueue(KEY, "webhook", 20)
        self.registry.enqueue(KEY, "webhook", 40)
        items = self.registry.due(25)
        self.assertEqual([(i.key, i.causes, i.due_at) for i in items],
                         [(other, ("webhook",), 10), (KEY, ("poll", "webhook"), 20)])

    def test_deliveries_are_recorded_once(self):
        """A redelivery is recognized."""
        self.assertFalse(self.registry.seen("delivery-1"))
        self.assertTrue(self.registry.seen("delivery-1"))
        self.assertFalse(self.open_again().seen("delivery-2"))

    def open_again(self):
        """Reopen the same registry, proving durability across processes."""
        self.registry.close()
        self.registry = self.open()
        return self.registry

    def test_snapshots_and_outcomes(self):
        """Only the current generation records an outcome; terminal tasks are not active."""
        self.assertIsNone(self.registry.acknowledged(KEY))
        self.registry.acknowledge(KEY, {"comments": {"1": "t"}})
        generation = self.registry.grant(KEY, "worker", 60)
        self.assertFalse(self.registry.end(KEY, generation - 1, Outcome("terminal")))
        self.assertTrue(self.registry.end(KEY, generation, Outcome("wait-until", retry_after=300)))
        self.assertFalse(self.registry.end(KEY, generation, Outcome("terminal")))
        registry = self.open_again()
        self.assertEqual(registry.acknowledged(KEY), {"comments": {"1": "t"}})
        self.assertEqual(registry.outcome(KEY), Outcome("wait-until", retry_after=300))
        self.assertEqual(registry.active(KEY[0]), [KEY])
        registry.end(KEY, registry.grant(KEY, "worker", 60), Outcome("terminal"))
        self.assertEqual(registry.active(KEY[0]), [])

    def test_resolution_requires_evidence_and_an_unknown_write(self):
        """Only an unknown occurrence is resolved, with a disposition, evidence and operator."""
        generation = self.registry.grant(KEY, "worker", 60)
        intent = self.registry.admit(KEY, generation, "POST", "repos/x/y/pulls", "d")
        with self.assertRaisesRegex(ValueError, "no unresolved"):
            self.registry.resolve(intent, "applied", "PR #9", "operator")
        self.registry.settle(intent, "unknown")
        for disposition, evidence, operator in (("maybe", "PR #9", "operator"), ("applied", " ", "operator"),
                                                ("applied", "PR #9", "")):
            with self.assertRaisesRegex(ValueError, "disposition"):
                self.registry.resolve(intent, disposition, evidence, operator)
        self.registry.resolve(intent, "applied", "PR #9 opened from the task branch", "operator")
        [record] = self.registry.mutations(KEY, "done")
        self.assertEqual((record.resolution["disposition"], record.resolution["operator"]), ("applied", "operator"))
        self.assertEqual(self.registry.unresolved(KEY), [])


if __name__ == "__main__":
    unittest.main()
