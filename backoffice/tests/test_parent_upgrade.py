"""Forward-open actual parent Registry, read-cache and detached checkpoint fixtures."""

import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_task_view as fixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import report_store
from backoffice.read_cache import ReadCache
from backoffice.tasks import SECTION_FIELDS, ReadCheckpoints
from checkpoints import Checkpoints, legacy_state
from fakes import FakeGitHub
from github import Transient
from service import registry as registry_module

FIXTURES = Path(__file__).with_name("fixtures") / "main-integration"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def predecessor(parent, name):
    """Decode only hash-bound nonsecret output from an exact parent's real persistence writer."""
    entry = MANIFEST["parents"][parent]["files"][name]
    encoded = (FIXTURES / entry["file"]).read_bytes()
    assert hashlib.sha256(encoded).hexdigest() == entry["sha256"]
    raw = gzip.decompress(encoded)
    assert hashlib.sha256(raw).hexdigest() == entry["raw_sha256"]
    return raw.decode()


class ParentUpgradeTests(unittest.TestCase):
    """Read both parent layouts forward without changing saved payer, budget or source evidence."""

    def test_both_registries_preserve_existing_rows_queues_and_source_policy(self):
        """Main schema5 advances to8; existing schema8 consent/policy rows survive byte-for-byte."""
        for parent in ("main", "sponsorship"):
            with self.subTest(parent=parent), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                db = sqlite3.connect(root / registry_module.DATABASE)
                db.executescript(predecessor(parent, "registry.sql"))
                tables = [
                    v[0]
                    for v in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('meta','sqlite_sequence')"
                    )
                ]
                rows = {table: db.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
                db.close()
                registry = registry_module.Registry.open(root, "registry-test", clock=lambda: 1001)
                try:
                    self.assertEqual(dict(registry.db.execute("SELECT name,value FROM meta"))["schema"], "8")
                    for table, before in rows.items():
                        self.assertEqual(registry.db.execute(f'SELECT * FROM "{table}"').fetchall(), before, table)
                    self.assertEqual([item.key for item in registry.due(2000)], [fixture.KEY])
                    self.assertEqual(registry.due(1999), [])
                    if parent == "sponsorship":
                        self.assertEqual(registry.sponsorship(fixture.KEY)["slots"]["openai"]["sponsor_id"], "202")
                        started = registry.db.execute("SELECT started FROM funding_policies").fetchone()[0]
                        self.assertEqual(
                            registry.funding_policy(fixture.KEY[0], "d" * 64, started + 1)["funding_mode"],
                            "contributed",
                        )
                        self.assertEqual(
                            registry.funding_policy(fixture.KEY[0], "d" * 64, started - 1)["funding_mode"], "unknown"
                        )
                finally:
                    registry.close()

    def test_parent_checkpoint_formats_hydrate_full_and_section_views(self):
        """Detached v7/v8/v9 reports and v9 immutable receipt survive main's selective loader."""
        for parent in ("main", "sponsorship"):
            values = json.loads(predecessor(parent, "checkpoints.json"))
            for version, payload in values.items():
                with self.subTest(parent=parent, version=version):
                    fake = FakeGitHub(payload["config"])
                    fake.comments = payload["comments"]
                    fake.artifacts = {
                        int(i): {int(n): v for n, v in rows.items()} for i, rows in payload["artifacts"].items()
                    }
                    fake.blobs = payload["blobs"]
                    loader = Checkpoints(fake, payload["config"], 7)
                    full = loader.load(fake.comments)
                    wire = report_store.audit_envelope(
                        legacy_state(loader.audit["body"]), loader.reports, payload["config"]["repository"], 7
                    )
                    self.assertEqual(wire["state"]["version"], int(version))
                    self.assertEqual(full["version"], 2)
                    for field in ("calls", "plan_id", "sponsorships", "operation"):
                        self.assertEqual(full.get(field), payload["restored"].get(field), field)
                    self.assertEqual(full["plan"], payload["restored"]["plan"])
                    summary = ReadCheckpoints(
                        fake, payload["config"], 7, fields=frozenset(SECTION_FIELDS["summary"])
                    ).load(fake.comments)
                    self.assertEqual(summary["calls"], 5)
                    self.assertEqual(summary["version"], 2)
                    self.assertNotIn("plan", summary)
                    plan = ReadCheckpoints(
                        fake, payload["config"], 7, fields=frozenset(SECTION_FIELDS["summary"] | SECTION_FIELDS["plan"])
                    ).load(fake.comments)
                    self.assertEqual(plan["plan"], full["plan"])
                    if version == "9":
                        self.assertEqual(summary["operation"]["admission"], full["operation"]["admission"])

    def test_main_persistent_cache_retains_snapshot_command_epoch_and_retry_cooldown(self):
        """Current cache opens main's saved rows; an outage retains old evidence and its backoff."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = sqlite3.connect(root / "issue-reads.sqlite3")
            db.executescript(predecessor("main", "cache.sql"))
            epoch = db.execute("SELECT epoch FROM snapshots").fetchone()[0]
            db.close()
            cache = ReadCache(root, clock=lambda: 1001)
            failed = Mock(side_effect=Transient("synthetic outage"))
            try:
                snapshot, status = cache.read("parent-scope", fixture.KEY[0], 7, failed)
                self.assertEqual(snapshot["state"]["calls"], 5)
                self.assertTrue(status["pendingCommand"])
                self.assertEqual(cache.db.execute("SELECT epoch FROM snapshots").fetchone()[0], epoch)
            finally:
                cache.close()
            cache = ReadCache(root, clock=lambda: 1002)
            loader = Mock()
            try:
                again, status = cache.read("parent-scope", fixture.KEY[0], 7, loader)
                self.assertEqual(again, snapshot)
                self.assertTrue(status["stale"])
                self.assertTrue(status["error"])
                loader.assert_not_called()
            finally:
                cache.close()
