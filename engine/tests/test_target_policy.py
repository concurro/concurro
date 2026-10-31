"""Target-repository configuration and the engine/target bundle split."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from support import CONFIG, ROOT, TARGET, setUpModule, tearDownModule  # unittest module fixtures
from agents import Agents
from bundle import CHARTER, Bundle
from controller import validate_config
from policy import Blocked, merge_path_allowed, path_allowed, path_safe


class TargetConfigTests(unittest.TestCase):
    """Each target owns its repository identity, CI names, policy documents and path rules."""

    def test_example_target_is_valid(self):
        """The shipped example stays a working configuration."""
        validate_config(deepcopy(CONFIG))

    def test_invalid_target_configuration_is_rejected(self):
        """Malformed identities, checks, documents, profiles and path rules block startup."""
        mutations = {
            "repository": ("repository", "not a repository"),
            "base": ("base", "../main"),
            "bot": ("bot_login", "concurro"),
            "no_checks": ("required_checks", []),
            "duplicate_checks": ("required_checks", ["Rust", "Rust"]),
            "absolute_document": ("policy_documents", ["/etc/passwd"]),
            "escaping_document": ("policy_documents", ["docs/../../secret.md"]),
            "profile": ("execution_profile", "python"),
            "missing_rule": ("paths", {k: v for k, v in CONFIG["paths"].items() if k != "protected_files"}),
            "no_extensions": ("paths", {**CONFIG["paths"], "editable_extensions": []}),
            "bare_extension": ("paths", {**CONFIG["paths"], "editable_extensions": ["md"]}),
            "prefix_without_slash": ("paths", {**CONFIG["paths"], "autonomous_merge_prefixes": ["docs"]}),
        }
        for name, (key, value) in mutations.items():
            with self.subTest(name=name), self.assertRaises(Blocked):
                validate_config({**deepcopy(CONFIG), key: value})


class PathPolicyTests(unittest.TestCase):
    """Engine invariants cannot be configured away; target rules only narrow further."""

    def test_engine_invariants_hold_for_any_target(self):
        """Traversal, hidden directories and agent governance files stay protected."""
        permissive = {**CONFIG, "policy_documents": [],
                      "paths": {**CONFIG["paths"], "protected_names": [], "protected_name_prefixes": [],
                                "protected_prefixes": [], "editable_extensions": [".md", ".rs"]}}
        for path in ("../x.md", ".github/x.md", "docs/.git/x.md", "AGENTS.md", "docs/CLAUDE.md", "SECURITY.md"):
            with self.subTest(path=path):
                self.assertFalse(path_safe(path))
                self.assertFalse(path_allowed(path, permissive))
        self.assertTrue(path_allowed("docs/guide.md", permissive))

    def test_target_rules_protect_configured_paths(self):
        """Configured names, prefixes, files and policy documents are not editable."""
        config = {**CONFIG, "policy_documents": ["docs/policy.md"],
                  "paths": {**CONFIG["paths"], "protected_files": ["docs/frozen.md"]}}
        self.assertTrue(path_allowed("docs/guide.md", config))
        for path in ("Cargo.toml", "crates/a/Cargo.lock", "rust-toolchain.toml", "scripts/a.md",
                     "docs/frozen.md", "docs/policy.md", "docs/image.png"):
            with self.subTest(path=path):
                self.assertFalse(path_allowed(path, config))

    def test_autonomous_merge_paths_exclude_build_checked_sources(self):
        """Only configured prefixes that need no execution-profile checks can merge autonomously."""
        self.assertTrue(merge_path_allowed("docs/guide.md", CONFIG))
        self.assertTrue(merge_path_allowed("changelog.d/x.added", CONFIG))
        self.assertFalse(merge_path_allowed("docs/example.rs", CONFIG))
        self.assertFalse(merge_path_allowed("src/lib.md", CONFIG))


class BundleSplitTests(unittest.TestCase):
    """Engine resources and the target charter bind into one identity."""

    def test_container_charter_text_reproduces_controller_identity(self):
        """An agent image without the charter derives the same identity from supplied text."""
        controller = Bundle(target=TARGET)
        container = Bundle(ROOT, charter_text=controller.charter_text)
        self.assertEqual(container.identity, controller.identity)
        self.assertEqual(container.engine_identity, controller.engine_identity)
        altered = Bundle(ROOT, charter_text=controller.charter_text + "\n")
        self.assertNotEqual(altered.identity, controller.identity)
        self.assertEqual(altered.engine_identity, controller.engine_identity)

    def test_charter_is_read_from_the_target_not_the_engine(self):
        """The working directory or explicit target supplies the charter."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with self.assertRaises(FileNotFoundError):
                Bundle(target=target)
            shutil.copytree(TARGET / ".concurro", target / ".concurro")
            self.assertEqual(Bundle(target=target).identity, Bundle().identity)
            charter = json.loads((target / CHARTER).read_text())
            charter["id"] = "another-target"
            (target / CHARTER).write_text(json.dumps(charter))
            self.assertNotEqual(Bundle(target=target).identity, Bundle().identity)

    def test_agent_payload_carries_exact_charter_text(self):
        """The controller sends the charter bytes that the container must hash."""
        configured = {**CONFIG, "models": {**CONFIG["models"], "codex": "operator-model"}}
        agents = Agents(configured, archive_loader=lambda commit: b"")
        with patch.dict("os.environ", {"CONCURRO_AGENT_IMAGE": "sha256:" + "a" * 64, "CODEX_API_KEY": "fixture"}), \
                patch("agents.inspection", return_value=("a" * 40, {})), patch("agents.Snapshot") as snapshot, \
                patch("agents.subprocess.run") as run:
            snapshot.return_value.__enter__.return_value.mount.return_value = "type=volume,source=x,target=/repo"
            run.return_value.returncode = 0
            run.return_value.stdout = "{}"
            agents.ask("codex", "review", {"bundle_id": agents.bundle.identity, "repository_commit": "a" * 40})
        payload = json.loads(run.call_args_list[0].kwargs["input"])
        self.assertEqual(payload["charter"], agents.bundle.charter_text)
        self.assertEqual(payload["bundle_id"], Bundle(ROOT, charter_text=payload["charter"]).identity)


if __name__ == "__main__":
    unittest.main()
