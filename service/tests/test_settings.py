"""Operator settings are private and strictly valid, or the service does not start."""

import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import fixtures  # noqa: F401  (puts the service and engine on the module path)

from service import __main__ as daemon, settings, use_gateway
from service.settings import SettingsError

IMAGE = "sha256:" + "a" * 64
# Only the host executor runs containers on the service host, so only it names images.
IMAGES = {"agent_image": IMAGE, "execution_image": "repo/execution@" + IMAGE}


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
        for name in ("app.pem", "webhook", "operator", "two.json"):
            self.private(name)
        self.data = {
            "controller_id": "service:primary",
            "engine": "b" * 40,
            "app_id": 123,
            "private_key_path": str(self.root / "app.pem"),
            "webhook_secret_path": str(self.root / "webhook"),
            "state_dir": str(self.root / "state"),
            "registry_id": "registry-1",
            "targets": [
                {"upstream": "source/one", "fork": "owner/one", "issues": "fork"},
                {"upstream": "source/two", "fork": "owner/two", "issues": "both"},
            ],
            "limits": {"global": 4, "per_repository": 2},
            "gateway": {
                "url": "https://gateway.invalid/concurro",
                "operator_token_path": str(self.root / "operator"),
                "executor": "runner",
                "workflow_ref": "concurro/concurro/.github/workflows/agent.yml@refs/heads/main",
                "models": ["claude-*", "gpt-*"],
                "budget": 1000000,
                "headers": {"codex": {"x-concurro-plan": "chatgpt"}},
            },
        }

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
        self.assertEqual([t.fork for t in loaded.targets], ["owner/one", "owner/two"])
        self.assertEqual((loaded.limits.global_ticks, loaded.limits.per_repository), (4, 2))
        self.assertEqual(
            (loaded.gateway.executor, loaded.gateway.models, loaded.gateway.operator_token_path),
            ("runner", ("claude-*", "gpt-*"), self.root / "operator"),
        )
        with self.assertRaises(settings.SettingsError):
            self.load({**self.data, "gateway": {**self.data["gateway"], "executor": "host"}})
        with self.assertRaises(AttributeError):
            loaded.engine = "c" * 40
        unpinned = deepcopy(self.data)
        del unpinned["engine"]
        self.assertIsNone(self.load(unpinned).engine, "resolved to the checkout's HEAD at startup")

    def test_optional_delegation_key(self):
        """The backoffice delegation key is an optional, distinct owner-only secret."""
        self.assertIsNone(self.load(self.data).delegation_key_path)
        key = self.private("delegation", "k" * 64)
        self.assertEqual(self.load({**self.data, "delegation_key_path": str(key)}).delegation_key_path, key)
        with self.assertRaises(settings.SettingsError):
            self.load({**self.data, "delegation_key_path": self.data["webhook_secret_path"]})
        key.chmod(0o644)
        with self.assertRaises(settings.SettingsError):
            self.load({**self.data, "delegation_key_path": str(key)})

    def test_optional_lists_token(self):
        """The backoffice's list index bearer is an optional, distinct owner-only secret."""
        self.assertIsNone(self.load(self.data).lists_token_path)
        token = self.private("lists-token", "ab" * 32)
        self.assertEqual(self.load({**self.data, "lists_token_path": str(token)}).lists_token_path, token)
        with self.assertRaises(settings.SettingsError):
            self.load({**self.data, "lists_token_path": self.data["webhook_secret_path"]})

    def test_older_settings_move_to_the_gateway(self):
        """Provider credentials and the engine pin go; the result must validate before it replaces the file."""
        path = self.root / "settings.json"
        old = {**{k: v for k, v in self.data.items() if k != "gateway"}, **IMAGES}
        old["targets"] = [{"repository": "owner/one", "credentials_file": str(self.root / "two.json")}]
        path.write_text(json.dumps(old))
        path.chmod(0o600)
        arguments = [
            "--settings",
            str(path),
            "--url",
            "https://gateway.invalid/concurro",
            "--operator-token",
            str(self.root / "operator"),
            "--upstream",
            "source/one",
            "--fork",
            "owner/one",
        ]
        self.assertEqual(use_gateway.main(arguments), 0)
        loaded = settings.load(path)
        self.assertEqual((loaded.engine, [t.fork for t in loaded.targets]), (None, ["owner/one"]))
        self.assertEqual((loaded.gateway.executor, loaded.gateway.workflow_ref), ("runner", use_gateway.WORKFLOW_REF))
        before = path.read_text()
        self.assertEqual(use_gateway.main([*arguments[:3], "http://plain.invalid", *arguments[4:]]), 1)
        self.assertEqual(path.read_text(), before, "an invalid result leaves the file alone")
        self.assertFalse(path.with_name("settings.json.new").exists())

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
            "unknown": changed(extra=1),
            "missing": changed(app_id=KeyError),
            "controller": changed(controller_id="actions"),
            "engine": changed(engine="main"),
            "boolean app": changed(app_id=True),
            "string app": changed(app_id="123"),
            "zero app": changed(app_id=0),
            "runner images": changed(**IMAGES),
            **{
                "host " + name: changed(
                    **images, gateway={**self.data["gateway"], "executor": "host", "workflow_ref": None}
                )
                for name, images in {
                    "without images": {},
                    "mutable image": {**IMAGES, "agent_image": "concurro-agent:latest"},
                    "one image": {"agent_image": IMAGE},
                }.items()
            },
            "relative": changed(state_dir="state"),
            "no targets": changed(targets=[]),
            "bad repository": changed(targets=[{"repository": "owner"}]),
            "repeated": changed(targets=[self.data["targets"][0], self.data["targets"][0]]),
            "same repository": changed(targets=[{"upstream": "Owner/One", "fork": "owner/one", "issues": "both"}]),
            "source": changed(targets=[{**self.data["targets"][0], "issues": "separate"}]),
            "ambiguous": changed(
                targets=[
                    self.data["targets"][1],
                    {"upstream": "source/two", "fork": "owner/third", "issues": "upstream"},
                ]
            ),
            "target credentials": changed(
                targets=[{**self.data["targets"][0], "credentials_file": str(self.root / "two.json")}]
            ),
            "no gateway": changed(gateway=KeyError),
            **{
                "gateway " + name: changed(gateway={**self.data["gateway"], **value})
                for name, value in {
                    "http": {"url": "http://gateway.invalid"},
                    "url path": {"url": "https://gateway.invalid/a b"},
                    "executor": {"executor": "local"},
                    "runner without workflow": {"workflow_ref": None},
                    "workflow": {"workflow_ref": "concurro/concurro/agent.yml@main"},
                    "models": {"models": []},
                    "model pattern": {"models": ["claude *"]},
                    "budget": {"budget": 0},
                    "header provider": {"headers": {"copilot": {}}},
                    "header name": {"headers": {"codex": {"x y": "z"}}},
                    "token": {"operator_token_path": str(self.root / "absent")},
                    "extra": {"key": "x"},
                }.items()
            },
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
        (self.root / "operator").chmod(0o640)
        with self.subTest(case="operator token readable"), self.assertRaises(SettingsError):
            self.load(self.data)
        (self.root / "operator").chmod(0o600)
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
        root, gateway = self.root, self.data["gateway"]
        os.symlink(root / "subdir", root / "aliased")
        os.symlink(root / "state", root / "state-link")
        self.private("subdir/three.json")
        aliases = {
            "dot-dot alias": {"gateway": {**gateway, "operator_token_path": str(root / "subdir/../operator")}},
            "dot alias": {"private_key_path": str(root) + "/./app.pem"},
            "symlinked directory": {"gateway": {**gateway, "operator_token_path": str(root / "aliased/three.json")}},
            "symlinked state": {"state_dir": str(root / "state-link")},
            "trailing slash": {"state_dir": str(root / "state") + "/"},
            "key reused": {"webhook_secret_path": str(root / "app.pem")},
            "token reuses key": {"gateway": {**gateway, "operator_token_path": str(root / "app.pem")}},
            "missing state": {"state_dir": str(root / "absent")},
            "state is a file": {"state_dir": str(root / "two.json")},
        }
        for name, change in aliases.items():
            with self.subTest(case=name), self.assertRaises(SettingsError):
                self.load({**self.data, **change})
        os.link(root / "operator", root / "linked.json")
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
        loaded = self.load({**self.data, "gateway": {**gateway, "operator_token_path": str(root / "subdir/four.json")}})
        self.assertEqual(loaded.gateway.operator_token_path, root / "subdir/four.json")

    def test_refusals_name_only_the_field(self):
        """The rejected value never appears in the error."""
        with self.assertRaises(SettingsError) as raised:
            self.load({**self.data, "engine": "secret-looking-value"})
        self.assertEqual(str(raised.exception), "invalid settings field: engine")


class HostCheckTests(unittest.TestCase):
    """Startup serves the clean checkout's HEAD, or refuses a checkout away from the configured commit."""

    def check(self, engine, head="a" * 40, dirty=""):
        replies = {"rev-parse": head + "\n", "status": dirty}

        def run(command, **kwargs):
            out = next((v for k, v in replies.items() if k in command), "")
            return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

        loaded = settings.Settings(
            "service:p", engine, 1, Path("/k"), Path("/w"), Path("/s"), "r", (), settings.Limits(1, 1), None
        )
        with patch.object(daemon.subprocess, "run", side_effect=run):
            return daemon.host_checks(loaded)

    def test_engine_resolution(self):
        self.assertEqual(self.check(None).engine, "a" * 40)
        self.assertEqual(self.check("a" * 40).engine, "a" * 40)
        with self.assertRaisesRegex(daemon.StartupError, "configured engine commit"):
            self.check("b" * 40)
        with self.assertRaisesRegex(daemon.StartupError, "clean"):
            self.check(None, dirty=" M engine/controller.py\n")


if __name__ == "__main__":
    unittest.main()
