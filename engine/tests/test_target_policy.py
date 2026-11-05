"""Target-repository configuration and the engine/target bundle split."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from support import CONFIG, EXECUTION, PROFILES, ROOT, TARGET, setUpModule, tearDownModule  # unittest module fixtures
from agents import Agents
from bundle import CHARTER, Bundle
from controller import validate_config
from policy import Blocked, path_allowed, path_safe


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
            "absolute_document": ("policy_documents", ["/etc/passwd"]),
            "escaping_document": ("policy_documents", ["docs/../../secret.md"]),
            "missing_rule": ("paths", {k: v for k, v in CONFIG["paths"].items() if k != "protected_files"}),
            "no_extensions": ("paths", {**CONFIG["paths"], "editable_extensions": []}),
            "bare_extension": ("paths", {**CONFIG["paths"], "editable_extensions": ["md"]}),
            "prefix_without_slash": ("paths", {**CONFIG["paths"], "protected_prefixes": ["docs"]}),
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
                self.assertFalse(path_allowed(path, permissive, EXECUTION))
        self.assertTrue(path_allowed("docs/guide.md", permissive, EXECUTION))

    def test_target_rules_protect_configured_paths(self):
        """Configured names, prefixes, files and policy documents are not editable."""
        config = {**CONFIG, "policy_documents": ["docs/policy.md"],
                  "paths": {**CONFIG["paths"], "protected_files": ["docs/frozen.md"]}}
        self.assertTrue(path_allowed("docs/guide.md", config, EXECUTION))
        self.assertTrue(path_allowed("Cargo.toml", config, EXECUTION), "manifests are ordinary code")
        for path in ("rust-toolchain.toml", "scripts/a.md",
                     "docs/frozen.md", "docs/policy.md", "docs/image.png"):
            with self.subTest(path=path):
                self.assertFalse(path_allowed(path, config, EXECUTION))


class BundleSplitTests(unittest.TestCase):
    """Engine resources and the target charter bind into one identity."""

    def test_container_charter_text_reproduces_controller_identity(self):
        """An agent image without the charter derives the same identity from supplied text."""
        controller = Bundle(target=TARGET)
        container = Bundle(ROOT, charter_text=controller.charter_text, instruction_texts=controller.instruction_texts)
        self.assertEqual(container.identity, controller.identity)
        self.assertEqual(container.engine_identity, controller.engine_identity)
        altered = Bundle(ROOT, charter_text=controller.charter_text + "\n", instruction_texts=controller.instruction_texts)
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
        turns = []

        class Executor:
            gateway = {"url": "https://gateway.invalid", "headers": {}}

            def run(self, turn, label):
                turns.append(turn)
                return {"outcome": "ok", "response": {}}

        agents = Agents(configured, Executor())
        agents.ask("codex", "review", {"bundle_id": agents.bundle.identity, "repository_commit": "a" * 40,
                   "repository": {"refs": {"main": "a" * 40}, "head": "main"}})
        payload = turns[0]["request"]
        self.assertEqual(payload["charter"], agents.bundle.charter_text)
        self.assertEqual(payload["bundle_id"], Bundle(ROOT, charter_text=payload["charter"], instruction_texts=payload["instruction_texts"]).identity)

    def test_target_instructions_are_role_scoped_and_hash_bound(self):
        """Only approved target files travel, with shared guidance and the selected role."""
        charter = Bundle(target=TARGET).charter_text
        texts = {".concurro/instructions/shared.md": "Use target tools.",
                 ".concurro/instructions/planner.md": "Plan small changes.",
                 ".concurro/instructions/reviewer.md": "Check compatibility."}
        bundle = Bundle(ROOT, charter_text=charter, instruction_texts=texts)
        self.assertEqual(bundle.instructions("intake")["target"], {"shared": texts[".concurro/instructions/shared.md"]})
        self.assertEqual(set(bundle.instructions("plan")["target"]), {"shared", "planner"})
        self.assertEqual(set(bundle.instructions("review")["target"]), {"shared", "reviewer"})
        self.assertEqual(bundle.identity, Bundle(ROOT, charter_text=charter, instruction_texts=bundle.instruction_texts).identity)
        self.assertNotEqual(bundle.identity, Bundle(ROOT, charter_text=charter).identity)
        with self.assertRaises(Blocked):
            Bundle(ROOT, charter_text=charter, instruction_texts={"../untrusted.md": "x"})
        with self.assertRaises(Blocked):
            Bundle(ROOT, charter_text=charter, instruction_texts={".concurro/instructions/shared.md": "x" * 24001})


if __name__ == "__main__":
    unittest.main()
