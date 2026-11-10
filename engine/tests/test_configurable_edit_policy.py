"""Explicit edit policy and workload limits, including legacy approval compatibility."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import candidates
import repository
import turns
from agents import Agents
from controller import Controller, config_digest, validate_config
from fakes import PLAN, FakeAgents, FakeGitHub
from policy import Blocked, path_allowed, plan_output_contract, validate_edits, validate_plan
from runner_fixtures import MODELS, FakeExecutor, Workspace
from snapshot import inspection


def open_config():
    """Use the new default-open policy without changing the test team's other settings."""
    return {**deepcopy(CONFIG), "paths": {"protected_extensions": [], "generated": []}}


class ConfigurablePolicyTests(unittest.TestCase):
    """Policy is operator data; Git and Concurro metadata remain outside candidate authority."""

    def test_default_open_and_legacy_policies_remain_distinct(self):
        config = open_config()
        validate_config(config)
        for path in (
            "docs/rpc/openapi.yaml",
            "data.json",
            "LICENSE",
            ".gitignore",
            ".github/workflows/ci.yml",
            "AGENTS.md",
            "CONTRIBUTING.md",
            "scripts/check.sh",
            "rust-toolchain.toml",
        ):
            with self.subTest(path=path):
                self.assertTrue(path_allowed(path, config))
                self.assertFalse(path_allowed(path, CONFIG))
        for path in (
            ".concurro/config.yaml",
            "nested/.concurro/charter.json",
            ".git/config",
            "x/.GIT/config",
            "../outside",
            "/outside",
            "a//b",
            "a/../b",
            "a\\b",
        ):
            with self.subTest(path=path):
                self.assertFalse(path_allowed(path, config))
        self.assertNotEqual(config_digest(config), config_digest(CONFIG))

    def test_each_optional_protection_is_enforced(self):
        for rule, value, path in (
            ("protected_extensions", [".yaml"], "docs/rpc/openapi.yaml"),
            ("protected_files", ["LICENSE"], "LICENSE"),
            ("protected_names", ["NOTICE"], "docs/NOTICE"),
            ("protected_name_prefixes", ["secret-"], "docs/secret-key"),
            ("protected_prefixes", ["tools/"], "tools/a.rs"),
            ("protected", ["**/rust-toolchain*"], "nested/rust-toolchain.toml"),
        ):
            with self.subTest(rule=rule):
                config = open_config()
                config["paths"][rule] = value
                validate_config(config)
                self.assertFalse(path_allowed(path, config))
                self.assertTrue(path_allowed("docs/other.txt", config))

    def test_invalid_policy_cannot_silently_enable_all_files(self):
        for paths in (
            {},
            {"protected_extensions": None},
            {"protected_extensions": ["yaml"]},
            {"protected_extensions": [], "editable_extensions": [".rs"]},
            {"protected_extensions": [], "protected_prefixes": ["tools"]},
            {"protected_extensions": [], "typo": []},
        ):
            with self.subTest(paths=paths), self.assertRaises(Blocked):
                validate_config({**open_config(), "paths": paths})

    def test_limit_config_rejects_invalid_and_unknown_values(self):
        for limits in (
            None,
            [],
            {"changed_files": True},
            {"changed_files": 0},
            {"changed_files": -1},
            {"changed_files": "32"},
            {"changed_file": 32},
        ):
            with self.subTest(limits=limits), self.assertRaises(Blocked):
                validate_config({**open_config(), "limits": limits})


class ConfigurableLimitTests(unittest.TestCase):
    """All count caps are opt-in; a frozen legacy plan retains its approved allowance."""

    def setUp(self):
        """Create a contribution larger than every old file-count cap."""
        self.config = open_config()
        self.files = {f"docs/file-{i}.txt": "new\n" for i in range(50)}
        self.plan = {**deepcopy(PLAN), "files": list(self.files), "scope": {"paths": list(self.files)}}

    def test_default_plan_and_candidate_have_no_file_count_cap(self):
        validate_plan(self.plan, self.config)
        edits = [{"path": path, "content": text} for path, text in self.files.items()]
        validate_edits({"summary": "Update files", "edits": edits}, self.plan, {}, self.config)
        self.assertEqual(candidates.validate({}, self.files, self.plan, self.config), sorted(self.files))
        contract = plan_output_contract(self.config)
        for key in ("primary_files", "scope_paths", "changed_files", "overlay_files"):
            self.assertIsNone(contract["limits"][key])

    def test_plan_limits_name_the_config_key(self):
        for key in ("primary_files", "scope_paths"):
            with self.subTest(key=key):
                config = {**self.config, "limits": {key: 49}}
                with self.assertRaisesRegex(Blocked, "limits." + key):
                    validate_plan(self.plan, config)
                config["limits"][key] = 50
                validate_plan(self.plan, config)

    def test_changed_file_limit_covers_proposals_and_cumulative_changes(self):
        config = {**self.config, "limits": {"changed_files": 49}}
        edits = [{"path": p, "content": c} for p, c in self.files.items()]
        with self.assertRaisesRegex(Blocked, "limits.changed_files"):
            validate_edits({"summary": "Update", "edits": edits}, self.plan, {}, config)
        with self.assertRaisesRegex(Blocked, "limits.changed_files"):
            candidates.validate({}, self.files, self.plan, config)
        config["limits"]["changed_files"] = 50
        candidates.validate({}, self.files, self.plan, config)

    def test_legacy_plan_allowance_is_not_erased(self):
        plan = {**self.plan, "files": [next(iter(self.files))], "scope": {"paths": ["docs/**"], "max_files": 3}}
        validate_plan(plan, self.config)
        with self.assertRaisesRegex(Blocked, "approved"):
            candidates.validate({}, self.files, plan, self.config)
        self.assertEqual(plan["scope"]["max_files"], 3)

    def test_scope_length_limit_is_configurable(self):
        plan = {**deepcopy(PLAN), "scope": {"paths": ["docs/**", "x" * 201]}}
        with self.assertRaisesRegex(Blocked, "limits.scope_path_characters"):
            validate_plan(plan, self.config)
        validate_plan(plan, {**self.config, "limits": {"scope_path_characters": None}})

    def test_overlay_admission_uses_the_configured_limit(self):
        refs = {"refs": {"main": "a" * 40}, "head": "main"}
        spec = inspection(refs, self.files, "develop", "main", self.config)
        self.assertIsNone(spec["overlay_files_limit"])
        with self.assertRaisesRegex(Blocked, "limits.overlay_files"):
            inspection(refs, self.files, "develop", "main", {**self.config, "limits": {"overlay_files": 49}})
        spec = inspection(refs, self.files, "develop", "main", {**self.config, "limits": {"overlay_files": 50}})
        self.assertEqual(spec["overlay_files_limit"], 50)

    def test_prepared_turn_carries_operator_limits(self):
        """Neither limit is left behind when the controller prepares a real runner request."""
        config = {**self.config, "models": MODELS, "limits": {"overlay_files": 51, "workspace_files": 52}}
        agents = Agents(config, FakeExecutor())
        turn = agents.prepare(
            "codex",
            "develop",
            {
                "bundle_id": agents.bundle.identity,
                "repository": {"refs": {"main": "a" * 40}, "head": "main"},
                "controller_candidate": self.files,
            },
        )
        self.assertEqual(turn["spec"]["overlay_files_limit"], 51)
        self.assertEqual(turn["spec"]["workspace_files_limit"], 52)

    def test_large_default_open_candidate_reaches_publication_and_review(self):
        """The complete lifecycle publishes more than 32 files, including newly editable paths."""
        config = {**self.config, "autonomous": True}
        files = {**self.files, "AGENTS.md": "Project guidance\n", ".github/workflows/check.yml": "name: Check\n"}

        class WideAgents(FakeAgents):
            """Return a broad but explicitly reviewed text contribution."""

            def ask(inner, provider, stage, context):
                """Reuse normal reports while replacing the plan and implementation fixture."""
                report = super().ask(provider, stage, context)
                if stage == "plan":
                    return {**report, "scope": {"paths": ["docs/**", "AGENTS.md", ".github/workflows/check.yml"]}}
                if stage == "develop":
                    return {**report, "edits": [{"path": p, "content": c} for p, c in files.items()]}
                return report

        github, agents = FakeGitHub(config), WideAgents()
        for _ in range(20):
            controller = Controller(github, agents, config)
            controller.advance(7)
            if controller.state.get("handoff_ready"):
                break
        self.assertTrue(controller.state.get("handoff_ready"), controller.state.get("reason"))
        self.assertEqual(set(controller.state["changed_files"]), set(files))
        for _, _, context in agents.calls:
            self.assertEqual(context["edit_policy"]["paths"]["protected_extensions"], [])
            self.assertIsNone(context["limits"]["changed_files"])
        for body in (controller.summary_body(), github.pr["body"]):
            self.assertIn("Workflow and policy changes (2)", body)
            self.assertIn("AGENTS.md", body)
            self.assertIn(".github/workflows/check.yml", body)
        for _, stage, context in agents.calls:
            if stage == "review":
                self.assertEqual(context["workflow_and_policy_changes"], [".github/workflows/check.yml", "AGENTS.md"])
        published = github.trees[github.commits[controller.state["head"]]["tree"]["sha"]]
        self.assertTrue(files.items() <= published.items())


class RunnerLimitTests(Workspace, unittest.TestCase):
    """Real Git fixtures check the former 40-file overlay and 500-file extraction ceilings."""

    def test_snapshot_has_no_implicit_count_cap(self):
        files = {f"docs/new-{i}.txt": "new\n" for i in range(50)}
        root = self.prepare(candidate={"files": files})
        self.assertEqual((root / "work/docs/new-49.txt").read_text(), "new\n")

    def test_snapshot_limit_refuses_before_git(self):
        spec = {
            "refs": {"main": "a" * 40},
            "head": "main",
            "candidate": {"files": {f"docs/{i}.txt": "new\n" for i in range(50)}},
            "overlay_files_limit": 49,
        }
        with patch.object(repository.subprocess, "run") as run, self.assertRaises(ValueError):
            repository.snapshot(spec)
        run.assert_not_called()

    def test_extraction_above_500_and_explicit_limit(self):
        root = self.prepare()
        for index in range(501):
            (root / "work/docs" / f"new-{index}.txt").write_text("new\n")
        self.assertEqual(len(turns.extract(root / "work", root / "pristine.git", root)["edits"]), 501)
        with self.assertRaisesRegex(turns.TurnError, "limits.workspace_files"):
            turns.extract(root / "work", root / "pristine.git", root, 500)
