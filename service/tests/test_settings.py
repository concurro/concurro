"""Operator settings are private and strictly valid, or the service does not start."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

import fixtures  # noqa: F401  (puts the service and engine on the module path)
from service import settings
from service.settings import SettingsError

IMAGE = "sha256:" + "a" * 64


class SettingsTests(unittest.TestCase):
    """Each refusal names only the field."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # Canonical, as settings require: the platform's temporary directory may sit behind a symlink.
        self.root = Path(self.directory.name).resolve()
        self.root.chmod(0o700)
        (self.root / "state").mkdir(mode=0o700)
        (self.root / "subdir").mkdir(mode=0o700)
        for name in ("app.pem", "webhook", "one.json", "two.json"):
            self.private(name)
        self.data = {
            "controller_id": "service:primary", "engine": "b" * 40, "app_id": 123,
            "private_key_path": str(self.root / "app.pem"), "webhook_secret_path": str(self.root / "webhook"),
            "state_dir": str(self.root / "state"), "registry_id": "registry-1",
            "targets": [{"repository": "owner/one", "credentials_file": str(self.root / "one.json")},
                        {"repository": "owner/two", "credentials_file": str(self.root / "two.json")}],
            "agent_image": IMAGE, "execution_image": "repo/execution@" + IMAGE,
            "limits": {"global": 4, "per_repository": 2}}

    def private(self, name, content="{}"):
        path = self.root / name
        path.write_text(content)
        path.chmod(0o600)
        return path

    def load(self, data, mode=0o600):
        path = self.root / "settings.json"
        path.unlink(missing_ok=True)
        path.write_text(json.dumps(data))
        path.chmod(mode)
        return settings.load(path)

    def test_valid_settings(self):
        """A complete private file loads into frozen settings."""
        loaded = self.load(self.data)
        self.assertEqual([t.repository for t in loaded.targets], ["owner/one", "owner/two"])
        self.assertEqual((loaded.limits.global_ticks, loaded.limits.per_repository), (4, 2))
        with self.assertRaises(AttributeError):
            loaded.engine = "c" * 40

    def test_invalid_fields(self):
        """Unknown, missing or ill-typed fields, unpinned images and out-of-range limits are refused."""
        def changed(**fields):
            data = deepcopy(self.data)
            for name, value in fields.items():
                if value is KeyError:
                    del data[name]
                else:
                    data[name] = value
            return data

        cases = {
            "unknown": changed(extra=1), "missing": changed(engine=KeyError),
            "controller": changed(controller_id="actions"), "engine": changed(engine="main"),
            "boolean app": changed(app_id=True), "string app": changed(app_id="123"), "zero app": changed(app_id=0),
            "image": changed(agent_image="concurro-agent:latest"),
            "relative": changed(state_dir="state"), "no targets": changed(targets=[]),
            "bad repository": changed(targets=[{"repository": "owner", "credentials_file": self.data["targets"][0]["credentials_file"]}]),
            "repeated": changed(targets=[self.data["targets"][0],
                                         {"repository": "Owner/One", "credentials_file": str(self.root / "two.json")}]),
            "shared credentials": changed(targets=[self.data["targets"][0],
                                                   {**self.data["targets"][1], "credentials_file": str(self.root / "one.json")}]),
            "target field": changed(targets=[{**self.data["targets"][0], "token": "x"}]),
            "limits": changed(limits={"global": 1, "per_repository": 2}),
            "limit type": changed(limits={"global": 2.0, "per_repository": 1}),
            "inside checkout": changed(state_dir=str(settings.CHECKOUT / "state")),
        }
        for name, data in cases.items():
            with self.subTest(case=name), self.assertRaises(SettingsError):
                self.load(data)

    def test_private_files(self):
        """Readable or symlinked files, or a readable directory, are refused."""
        with self.subTest(case="settings readable"), self.assertRaises(SettingsError):
            self.load(self.data, mode=0o644)
        (self.root / "one.json").chmod(0o640)
        with self.subTest(case="credentials readable"), self.assertRaises(SettingsError):
            self.load(self.data)
        (self.root / "one.json").chmod(0o600)
        os.symlink(self.root / "app.pem", self.root / "link.pem")
        with self.subTest(case="symlinked key"), self.assertRaises(SettingsError):
            self.load({**self.data, "private_key_path": str(self.root / "link.pem")})
        with self.subTest(case="missing secret"), self.assertRaises(SettingsError):
            self.load({**self.data, "webhook_secret_path": str(self.root / "absent")})
        self.root.chmod(0o750)
        self.addCleanup(self.root.chmod, 0o700)
        with self.subTest(case="directory readable"), self.assertRaises(SettingsError):
            self.load(self.data)

    def test_each_path_names_one_location(self):
        """Aliases, hard links and symlinked directories cannot make two settings share a file."""
        root, one = self.root, self.data["targets"][0]
        os.symlink(root / "subdir", root / "aliased")
        os.symlink(root / "state", root / "state-link")
        self.private("subdir/three.json")
        aliases = {
            "dot-dot alias": {"targets": [one, {"repository": "owner/two", "credentials_file": str(root / "subdir/../one.json")}]},
            "dot alias": {"private_key_path": str(root) + "/./app.pem"},
            "symlinked directory": {"targets": [one, {"repository": "owner/two",
                                                      "credentials_file": str(root / "aliased/three.json")}]},
            "symlinked state": {"state_dir": str(root / "state-link")},
            "trailing slash": {"state_dir": str(root / "state") + "/"},
            "key reused": {"webhook_secret_path": str(root / "app.pem")},
            "credentials reuse key": {"targets": [{**one, "credentials_file": str(root / "app.pem")}]},
            "missing state": {"state_dir": str(root / "absent")},
            "state is a file": {"state_dir": str(root / "two.json")},
        }
        for name, change in aliases.items():
            with self.subTest(case=name), self.assertRaises(SettingsError):
                self.load({**self.data, **change})
        os.link(root / "two.json", root / "linked.json")
        with self.subTest(case="hard link"), self.assertRaises(SettingsError):
            self.load(self.data)
        (root / "linked.json").unlink()
        (root / "state").chmod(0o750)
        with self.subTest(case="readable state"), self.assertRaises(SettingsError):
            self.load(self.data)
        (root / "state").chmod(0o700)
        with self.subTest(case="settings through a symlinked ancestor"), self.assertRaises(SettingsError):
            (root / "subdir/inner").mkdir(mode=0o700)
            path = root / "subdir/inner/settings.json"
            path.write_text(json.dumps(self.data))
            path.chmod(0o600)
            settings.load(root / "aliased/inner/settings.json")
        self.private("subdir/four.json")
        loaded = self.load({**self.data, "targets": [one, {"repository": "owner/two",
                                                           "credentials_file": str(root / "subdir/four.json")}]})
        self.assertEqual(loaded.targets[1].credentials_file, root / "subdir/four.json")

    def test_refusals_name_only_the_field(self):
        """The rejected value never appears in the error."""
        with self.assertRaises(SettingsError) as raised:
            self.load({**self.data, "engine": "secret-looking-value"})
        self.assertEqual(str(raised.exception), "invalid settings field: engine")


if __name__ == "__main__":
    unittest.main()
