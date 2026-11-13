"""Pin registry setup, failure boundaries and shared record projections before extraction."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import fixtures  # noqa: F401 -- initializes the service and engine test paths

from controller import Outcome
from service.errors import RegistryError, RegistryFailure
from service.registry import DATABASE, MutationRecord, Registry, TaskBinding, human_input

GOLDEN = Path(__file__).with_name("goldens") / "registry_setup.json"
KEY = ("owner/repo", 7)
CONNECT = sqlite3.connect
CLOSE = os.close


class ConnectionTrace:
    """Record exact SQL and parameters, optionally failing before one statement."""

    def __init__(self, db, trace, fail=None):
        self.db, self.trace, self.failure_index = db, trace, fail
        self.closed = False

    def execute(self, sql, parameters=()):
        self.trace.append(["execute", sql, list(parameters)])
        if len(self.trace) - 1 == self.failure_index:
            raise sqlite3.OperationalError("injected")
        return self.db.execute(sql, parameters)

    def executemany(self, sql, parameters):
        values = list(parameters)
        self.trace.append(["executemany", sql, [list(value) for value in values]])
        if len(self.trace) - 1 == self.failure_index:
            raise sqlite3.OperationalError("injected")
        return self.db.executemany(sql, values)

    def close(self):
        self.trace.append(["close"])
        self.closed = True
        self.db.close()


def schema(db):
    """Read stored table text independently of the production schema constants."""
    return dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"))


def legacy(root, fresh):
    """Construct a v2 database from the pinned fixture, with representative preserved rows."""
    with closing(CONNECT(Path(root) / DATABASE)) as db, db:
        for name, sql in fresh.items():
            if name not in {
                "funding_policies",
                "sqlite_sequence",
                "bindings",
                "runner_sessions",
                "pulls",
                "review_intents",
                "webhook_recovery",
                "observations",
                "issue_sponsor_sources",
                "issue_sponsorships",
            }:
                db.execute(sql)
        db.execute(
            "CREATE TABLE pulls (repository TEXT NOT NULL, issue INTEGER NOT NULL, "
            "pull INTEGER NOT NULL, PRIMARY KEY (repository, issue), UNIQUE (repository, pull))"
        )
        db.executemany("INSERT INTO meta VALUES (?, ?)", [("registry_id", "test"), ("schema", "2")])
        db.execute("INSERT INTO pulls VALUES ('owner/repo', 7, 9)")
        db.execute("INSERT INTO deliveries VALUES ('delivery', 123)")


class SetupTests(unittest.TestCase):
    """Exact schema and SQL order, including failures before the caller receives a registry."""

    def setUp(self):
        self.golden = json.loads(GOLDEN.read_text())
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.trace, self.connections, self.closed_fds = [], [], []

    def connect(self, *args, **kwargs):
        """Wrap a real connection while pinning its autocommit and threading options."""
        self.assertEqual(kwargs, {"isolation_level": None, "check_same_thread": False})
        connection = ConnectionTrace(CONNECT(*args, **kwargs), self.trace, getattr(self, "failure_index", None))
        self.connections.append(connection)
        return connection

    def close_fd(self, fd):
        """Require database cleanup before releasing the directory lock."""
        self.assertTrue(all(connection.closed for connection in self.connections))
        self.closed_fds.append(fd)
        CLOSE(fd)

    def open(self, initialize=False, identifier="test"):
        """Run the public startup path through the SQL and cleanup recorders."""
        with patch("service.registry.sqlite3.connect", self.connect), patch("service.registry.os.close", self.close_fd):
            return Registry.open(self.root, identifier, initialize=initialize, clock=lambda: 1000)

    def test_fresh_schema_and_order(self):
        """Fresh DDL, meta values, pragmas, setup and recovery statements are byte-pinned."""
        registry = self.open(initialize=True)
        self.addCleanup(registry.close)
        self.assertEqual(self.trace, self.golden["initialize"])
        self.assertEqual(schema(registry.db.db), self.golden["fresh"])
        self.assertEqual(list(registry.db.db.execute("SELECT * FROM meta")), [("registry_id", "test"), ("schema", "8")])
        self.assertIn("\n", self.golden["fresh"]["pulls"])

    def test_migrated_schema_rows_and_order(self):
        """Migration keeps its distinct single-line pulls DDL and existing data."""
        legacy(self.root, self.golden["fresh"])
        registry = self.open()
        self.addCleanup(registry.close)
        self.assertEqual(self.trace, self.golden["migrate"])
        self.assertEqual(schema(registry.db.db), self.golden["migrated"])
        self.assertEqual(list(registry.db.db.execute("SELECT * FROM pulls")), [("owner/repo", 7, "owner/repo", 9)])
        self.assertEqual(list(registry.db.db.execute("SELECT * FROM deliveries")), [("delivery", 123)])
        self.assertEqual(list(registry.db.db.execute("SELECT * FROM meta")), [("registry_id", "test"), ("schema", "8")])
        self.assertNotIn("\n", self.golden["migrated"]["pulls"])

    def test_existing_open_order(self):
        """Opening the current schema skips initialization and migration but still performs recovery."""
        Registry.open(self.root, "test", initialize=True).close()
        registry = self.open()
        self.addCleanup(registry.close)
        self.assertEqual(self.trace, self.golden["open"])

    def test_setup_failure_at_every_statement(self):
        """Each setup failure maps once, closes DB before lock, and rolls back unfinished SQL."""
        for mode in ("initialize", "migrate", "open"):
            expected = self.golden[mode]
            # The last BEGIN starts recover(), outside the mapped setup block.
            recovery = max(i for i, entry in enumerate(expected) if entry[1] == "BEGIN IMMEDIATE")
            for index in range(recovery):
                with self.subTest(mode=mode, statement=index), tempfile.TemporaryDirectory() as directory:
                    self.root = Path(directory)
                    if mode == "migrate":
                        legacy(self.root, self.golden["fresh"])
                    elif mode == "open":
                        Registry.open(self.root, "test", initialize=True).close()
                    self.trace, self.connections, self.closed_fds = [], [], []
                    self.failure_index = index
                    with self.assertRaisesRegex(RegistryError, "^registry is unreadable$") as error:
                        self.open(initialize=mode == "initialize")
                    self.assertIsNone(error.exception.__cause__)
                    self.assertEqual(self.trace, expected[: index + 1] + [["close"]])
                    self.assertTrue(self.connections[0].closed)
                    self.assertTrue(self.closed_fds)
                    with self.assertRaises(OSError):
                        os.fstat(self.closed_fds[-1])
                    # Independent connection proves rollback and preserves the v2 fixture.
                    with closing(CONNECT(self.root / DATABASE)) as db, db:
                        tables = schema(db)
                        if mode == "migrate":
                            self.assertEqual(
                                db.execute("SELECT value FROM meta WHERE name='schema'").fetchone(), ("2",)
                            )
                            self.assertEqual(list(db.execute("SELECT * FROM pulls")), [("owner/repo", 7, 9)])
                    if not tables:
                        (self.root / DATABASE).unlink()
                    reopened = Registry.open(self.root, "test", initialize=not tables)
                    reopened.close()

    def test_connect_failure_releases_lock(self):
        """A failed connect has no connection to close, but still releases the acquired lock."""
        with patch("service.registry.sqlite3.connect", side_effect=sqlite3.OperationalError("connect")):
            with self.assertRaisesRegex(RegistryError, "unreadable"):
                Registry.open(self.root, "test", initialize=True)
        (self.root / DATABASE).unlink()
        Registry.open(self.root, "test", initialize=True).close()

    def test_identity_precedes_migration(self):
        """A wrong identity executes no migration statement and leaves v2 intact."""
        legacy(self.root, self.golden["fresh"])
        with self.assertRaisesRegex(RegistryError, "identity or schema version mismatch"):
            self.open(identifier="other")
        first_begin = next(i for i, row in enumerate(self.golden["migrate"]) if row[1] == "BEGIN IMMEDIATE")
        self.assertEqual(self.trace, self.golden["migrate"][:first_begin] + [["close"]])
        self.assertTrue(self.connections[0].closed)
        Registry.open(self.root, "test").close()

    def test_refusal_table(self):
        """Missing, empty, existing, unknown-schema and locked stores keep exact refusals."""
        for empty in (False, True):
            if empty:
                (self.root / DATABASE).touch()
            with self.assertRaisesRegex(RegistryError, "registry is missing or empty; initialize a new one explicitly"):
                Registry.open(self.root, "test")
        (self.root / DATABASE).unlink()
        registry = Registry.open(self.root, "test", initialize=True)
        with self.assertRaisesRegex(RegistryError, "state directory is in use by another process"):
            Registry.open(self.root, "test")
        registry.close()
        with self.assertRaisesRegex(RegistryError, "registry already exists; refusing to initialize over it"):
            Registry.open(self.root, "test", initialize=True)
        with closing(CONNECT(self.root / DATABASE)) as db, db:
            db.execute("UPDATE meta SET value='99' WHERE name='schema'")
        with self.assertRaisesRegex(RegistryError, "registry identity or schema version mismatch"):
            self.open()
        self.assertTrue(self.connections[0].closed)

    def test_recovery_failure_releases_store_without_remapping(self):
        """Failed recovery releases DB and lock while preserving the exact RegistryFailure."""
        Registry.open(self.root, "test", initialize=True).close()
        self.failure_index = next(
            i for i, row in enumerate(self.golden["open"]) if row[1].startswith("UPDATE mutations")
        )
        captured = []
        original = Registry.recover

        def recover(registry):
            self.addCleanup(registry.close)
            try:
                return original(registry)
            except BaseException as error:
                captured.append((registry, error))
                raise

        with patch.object(Registry, "recover", recover):
            with self.assertRaisesRegex(RegistryFailure, "registry storage failed") as caught:
                self.open()
        fail_index = self.failure_index
        del self.failure_index
        registry, error = captured[0]
        self.assertIs(caught.exception, error)
        self.assertTrue(registry.failed)
        self.assertTrue(self.connections[0].closed)
        self.assertIsNone(registry.lock_fd)
        self.assertEqual(self.trace, self.golden["open"][: fail_index + 1] + [["close"]])
        with self.assertRaises(sqlite3.ProgrammingError):
            registry.db.db.execute("SELECT 1")
        self.assertEqual(len(self.closed_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(self.closed_fds[0])
        registry.close()
        Registry.open(self.root, "test").close()

    def test_failed_recovery_rolls_back_completed_write(self):
        """Closing after a later recovery failure rolls back an already-updated mutation."""
        with closing(Registry.open(self.root, "test", initialize=True, clock=lambda: 1000)) as registry:
            generation = registry.grant(KEY, "worker", 60)
            intent = registry.admit(KEY, generation, "POST", "repos/owner/repo/issues", "digest")
        self.failure_index = next(
            i
            for i, row in enumerate(self.golden["open"])
            if row[1] == "SELECT repository, issue FROM ownership WHERE live = 1"
        )
        original = Registry.recover

        def recover(registry):
            self.addCleanup(registry.close)
            return original(registry)

        with patch.object(Registry, "recover", recover):
            with self.assertRaisesRegex(RegistryFailure, "registry storage failed"):
                self.open()
        fail_index = self.failure_index
        del self.failure_index
        self.assertEqual(self.trace, self.golden["open"][: fail_index + 1] + [["close"]])
        with closing(CONNECT(self.root / DATABASE)) as db:
            self.assertEqual(
                db.execute("SELECT status, settled FROM mutations WHERE intent=?", (intent,)).fetchone(),
                ("in-flight", None),
            )
            self.assertEqual(db.execute("SELECT live FROM ownership").fetchall(), [(1,)])
        with closing(Registry.open(self.root, "test", clock=lambda: 2000)) as registry:
            self.assertEqual(
                registry.db.execute("SELECT status, settled FROM mutations WHERE intent=?", (intent,)).fetchone(),
                ("unknown", 2000),
            )

    def test_recovery_cleanup_does_not_retry_a_closed_descriptor(self):
        """A close that releases the fd then raises preserves recovery failure and cannot be retried."""
        Registry.open(self.root, "test", initialize=True).close()
        original = RegistryFailure("recovery failed")
        captured = []

        def recover(registry):
            captured.append(registry)
            raise original

        def close_fd(fd):
            CLOSE(fd)
            raise OSError("close failed after releasing descriptor")

        with (
            patch.object(Registry, "recover", recover),
            patch("service.registry.os.close", side_effect=close_fd) as close,
        ):
            with self.assertRaises(RegistryFailure) as caught:
                Registry.open(self.root, "test")
            self.assertIs(caught.exception, original)
            close.assert_called_once()
            captured[0].close()
            close.assert_called_once()
            self.assertIsNone(captured[0].lock_fd)
        Registry.open(self.root, "test").close()

    def test_recovery_error_survives_database_close_failure(self):
        """Even BaseException during close cannot hide recovery failure or retain the lock."""
        Registry.open(self.root, "test", initialize=True).close()
        for close_error in (sqlite3.OperationalError("close failed"), KeyboardInterrupt("close interrupted")):
            with self.subTest(close_error=type(close_error).__name__):
                self.trace, self.connections, self.closed_fds = [], [], []
                original = RegistryFailure("recovery failed")
                captured = []

                def recover(registry):
                    captured.append(registry)
                    self.addCleanup(registry.db.db.close)
                    self.addCleanup(registry.close)
                    raise original

                def close_fd(fd):
                    # Database close was attempted but failed; the lock must still be released.
                    self.closed_fds.append(fd)
                    CLOSE(fd)

                with (
                    patch.object(Registry, "recover", recover),
                    patch.object(ConnectionTrace, "close", side_effect=close_error) as close_db,
                    patch.object(self, "close_fd", side_effect=close_fd),
                ):
                    with self.assertRaises(RegistryFailure) as caught:
                        self.open()
                    self.assertIs(caught.exception, original)
                    close_db.assert_called_once_with()
                    self.assertEqual(len(self.closed_fds), 1)
                    with self.assertRaises(OSError):
                        os.fstat(self.closed_fds[0])
                    self.assertIsNone(captured[0].lock_fd)
                    captured[0].close()
                    close_db.assert_called_once_with()
                # The injected close never touched SQLite; dispose of that test resource explicitly.
                captured[0].db.db.close()
                Registry.open(self.root, "test").close()


class RegistryHelperTests(unittest.TestCase):
    """Shared conversions preserve values and the distinct input projections."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.registry = Registry.open(directory.name, "test", initialize=True, clock=lambda: 1000)
        self.addCleanup(self.registry.close)

    def test_mutation_records(self):
        """List and single lookup map every column, including optional resolution JSON."""
        generation = self.registry.grant(KEY, "worker", 60)
        intent = self.registry.admit(KEY, generation, "POST", "repos/owner/repo/issues", "digest")
        expected = MutationRecord(
            intent, KEY, generation, 1, "POST", "repos/owner/repo/issues", "digest", "in-flight", 1000, None, None
        )
        self.assertEqual(self.registry.mutation(intent), expected)
        self.assertEqual(self.registry.mutations(KEY, "in-flight"), [expected])
        self.registry.settle(intent, "unknown")
        self.registry.resolve(intent, "applied", "evidence", "operator")
        record = self.registry.mutation(intent)
        self.assertEqual(self.registry.mutations(KEY, "done"), [record])
        self.assertEqual(
            record.resolution, {"disposition": "applied", "evidence": "evidence", "operator": "operator", "at": 1000}
        )
        self.assertEqual(record.settled, 1000)
        self.assertIsNone(self.registry.mutation("missing"))

    def test_binding_callers(self):
        """Both lookups keep the original pin and refuse changes to either destination."""
        self.assertIsNone(self.registry.binding(KEY))
        expected = TaskBinding("up/repo", "fork/repo", "a" * 40)
        self.assertEqual(self.registry.bind(KEY, expected.upstream, expected.fork, expected.config_sha), expected)
        self.assertEqual(self.registry.bind(KEY, expected.upstream, expected.fork, "b" * 40), expected)
        self.assertEqual(self.registry.binding(KEY), expected)
        for upstream, fork in (("other/repo", expected.fork), (expected.upstream, "other/repo")):
            with self.assertRaisesRegex(ValueError, "task target changed; reconcile before continuing"):
                self.registry.bind(KEY, upstream, fork, "c" * 40)
        self.assertEqual(self.registry.binding(KEY), expected)

    def test_stage_and_ci_projection(self):
        """Stage alone is ignored, while CI-only input wakes gates but not deferred or terminal work."""
        snapshot = {
            "issue": {"state": "open"},
            "_stage": "gates",
            "pull": {"head": "a", "checks": [1], "statuses": [2], "suites": [3], "runs": [4], "merge_head": "b"},
        }
        original = deepcopy(snapshot)
        self.assertEqual(human_input(snapshot), {"issue": {"state": "open"}, "pull": {"head": "a"}})
        self.assertIsNone(human_input(None))
        self.assertEqual(snapshot, original)
        self.registry.acknowledge(KEY, snapshot)
        self.assertFalse(self.registry.observe(KEY, {**snapshot, "_stage": "review"}))
        changed = {**snapshot, "pull": {**snapshot["pull"], "checks": [5]}}
        self.assertTrue(self.registry.observe(KEY, changed))
        self.assertEqual(self.registry.due(1000)[0].causes, ("ci",))
        self.registry.defer(KEY, snapshot)
        self.assertFalse(self.registry.observe(KEY, changed))
        self.registry.acknowledge(KEY, {**snapshot, "_stage": "review"})
        self.assertFalse(self.registry.observe(KEY, changed))
        generation = self.registry.grant(KEY, "worker", 60)
        self.registry.end(KEY, generation, Outcome("terminal"))
        self.assertFalse(self.registry.observe(KEY, changed))
