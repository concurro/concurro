"""Exact environment contracts for Git-only processes and native agents."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ENGINE

import mirror
import repository
import turns


class EnvironmentTests(unittest.TestCase):
    """Pin each builder independently, including inherited credentials and PATH edge cases."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.inherited = {
            "PATH": str(self.root / "bin"),
            "HOME": str(self.root / "home"),
            "LC_ALL": "custom-locale",
            "CARGO_HOME": str(self.root / "cargo"),
            "RUSTUP_HOME": str(self.root / "rustup"),
            "CUSTOM": "keep me",
            "GITHUB_TOKEN": "excluded",
            "GH_TOKEN": "excluded",
            "GH_ENTERPRISE_TOKEN": "excluded",
            "ACTIONS_RUNTIME_TOKEN": "excluded",
            "OPENAI_API_KEY": "excluded",
            "ANTHROPIC_API_KEY": "excluded",
            "CONCURRO_GATEWAY_KEY": "excluded",
            "CLAUDE_CONFIG_DIR": "excluded",
            "CODEX_HOME": "excluded",
            "COPILOT_HOME": "excluded",
            "GIT_CONFIG_GLOBAL": "excluded",
            "GIT_CONFIG_COUNT": "99",
            "GIT_CONFIG_KEY_1": "excluded",
            "GIT_CONFIG_VALUE_1": "excluded",
            "GIT_ASKPASS": "excluded",
        }
        self.enterContext(patch.dict(os.environ, self.inherited, clear=True))
        self.enterContext(patch.object(tempfile, "gettempdir", return_value=str(self.root)))

    def test_mirror_environment(self):
        self.assertEqual(
            mirror.git_environment(),
            {
                "PATH": str(self.root / "bin"),
                "HOME": str(self.root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "true",
                "LC_ALL": "C",
            },
        )

    def test_extraction_environment(self):
        self.assertEqual(
            turns.git_environment(self.root / "work", self.root / "pristine.git", self.root / "index"),
            {
                "PATH": str(self.root / "bin"),
                "HOME": str(self.root),
                "LC_ALL": "C",
                "GIT_DIR": str(self.root / "pristine.git"),
                "GIT_WORK_TREE": str(self.root / "work"),
                "GIT_INDEX_FILE": str(self.root / "index"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "*",
            },
        )

    def test_repository_environment(self):
        self.assertEqual(
            repository.git_environment(self.root),
            {
                "PATH": str(self.root / "bin"),
                "HOME": str(self.root),
                "LC_ALL": "C",
                "GIT_DIR": str(self.root / ".git"),
                "GIT_WORK_TREE": str(self.root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(self.root),
            },
        )

    def test_agent_environment_retains_toolchains_but_drops_credentials(self):
        for retained, directory in ((False, "scratch"), (True, "retained")):
            with self.subTest(retained=retained):
                expected = {
                    "PATH": str(self.root / "bin"),
                    "HOME": str(self.root / "home"),
                    "LC_ALL": "custom-locale",
                    "CARGO_HOME": str(self.root / "cargo"),
                    "RUSTUP_HOME": str(self.root / "rustup"),
                    "CUSTOM": "keep me",
                    "CONCURRO_GATEWAY_KEY": "turn-key",
                    "CODEX_HOME": str(self.root / directory / "codex"),
                    "CLAUDE_CONFIG_DIR": str(self.root / directory / "claude"),
                    "COPILOT_HOME": str(self.root / directory / "copilot"),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": "*",
                }
                result = turns.agent_environment(self.root, "turn-key", retained)
                self.assertEqual(result, expected)
                self.assertEqual(os.environ, self.inherited)
                result["GIT_CONFIG_NOSYSTEM"] = "mutated"
                self.assertEqual(turns.agent_environment(self.root, "turn-key", retained), expected)

    def test_path_fallback_and_empty_path(self):
        builders = (
            mirror.git_environment,
            lambda: turns.git_environment(self.root, self.root / ".git", self.root / "index"),
            lambda: repository.git_environment(self.root),
        )
        for inherited, expected in (({}, "/usr/bin:/bin"), ({"PATH": ""}, "")):
            with self.subTest(inherited=inherited), patch.dict(os.environ, inherited, clear=True):
                for builder in builders:
                    result = builder()
                    self.assertEqual(result["PATH"], expected)
                    result["GIT_CONFIG_GLOBAL"] = "mutated"
                    self.assertEqual(builder()["GIT_CONFIG_GLOBAL"], os.devnull)
                agent = turns.agent_environment(self.root, "key", False)
                self.assertEqual({key: agent[key] for key in ("PATH", "HOME", "LC_ALL") if key in agent}, inherited)


class IsolatedRepositoryTests(unittest.TestCase):
    """The repository's environment builder remains usable without site packages or engine setup."""

    def test_isolated_import_environment(self):
        script = (
            "import json, sys, tempfile; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); before = list(sys.path); import repository; "
            "assert sys.path == before; "
            "tempfile.tempdir = sys.argv[2]; print(json.dumps(repository.git_environment(Path(sys.argv[2]))))"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script, str(ENGINE), directory],
                cwd=directory,
                env={"PATH": str(root / "bin")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "PATH": str(root / "bin"),
                    "HOME": directory,
                    "LC_ALL": "C",
                    "GIT_DIR": str(root / ".git"),
                    "GIT_WORK_TREE": directory,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_LITERAL_PATHSPECS": "1",
                    "GIT_PAGER": "cat",
                    "PAGER": "cat",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": directory,
                },
            )
