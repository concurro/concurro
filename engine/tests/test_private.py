"""Owner-only secret files remain protected independently of any execution adapter."""

import json
import tempfile
import unittest
from pathlib import Path

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import private
from policy import Blocked


class PrivateFileTests(unittest.TestCase):
    """No overwrite, symlink following or repository-local secret storage."""

    def test_private_create_read_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "private" / "secret.json"
            private.write(file, {"token": "fixture"})
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(private.read(file)), {"token": "fixture"})
            with self.assertRaises(FileExistsError):
                private.write(file, {})
            file.chmod(0o644)
            with self.assertRaises(Blocked):
                private.read(file)

    def test_links_and_repository_storage_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private.write(root / "secret", {})
            (root / "link").symlink_to(root / "secret")
            with self.assertRaises(OSError):
                private.read(root / "link")
            with self.assertRaises(Blocked):
                private.read(root / "secret", (root,))
            with self.assertRaises(Blocked):
                private.write(root / "new", {}, (root,))
