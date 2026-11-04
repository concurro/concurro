"""Dependency changes: lockfile deltas, dependency-aware test scope, generated files and manifest assessments."""

from copy import deepcopy
import os
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from support import CONFIG, EXECUTION, setUpModule, tearDownModule  # unittest module fixtures
import checks
from controller import Controller, changes, dependency_summary
from local import LocalWorkspace
import lockfiles
from policy import Blocked, merge_path_allowed, validate_edits, validate_plan, validate_pr_review, validate_review
from test_pipeline import APPROVE, PLAN, FakeAgents, FakeGitHub

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def lock(packages):
    """A Cargo.lock: `packages` maps (name, version) to (dependencies, checksum, external)."""
    blocks = ["version = 3"]
    for (name, version), (dependencies, checksum, external) in packages.items():
        lines = ["[[package]]", f'name = "{name}"', f'version = "{version}"']
        if external:
            lines += [f'source = "{REGISTRY}"', f'checksum = "{checksum}"']
        if dependencies:
            lines.append("dependencies = [" + ", ".join(f'"{d}"' for d in dependencies) + "]")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


WORKSPACE = {("app", "0.1.0"): (["common"], None, False), ("common", "0.1.0"): (["p256", "serde"], None, False),
             ("tool", "0.1.0"): (["serde"], None, False), ("p256", "0.13.2"): (["elliptic"], "a1", True),
             ("elliptic", "0.13.0"): ([], "b1", True), ("serde", "1.0.0"): ([], "c1", True)}


def bumped(packages, old, new, checksum):
    """Move one package to another version, as `cargo update -p` would."""
    result = {}
    for key, (dependencies, check, external) in packages.items():
        result[(key[0], new) if key == old else key] = (dependencies, checksum if key == old else check, external)
    return result


class LockfileTests(unittest.TestCase):

    def test_delta_names_changes_additions_removals_and_checksums(self):
        after = bumped(WORKSPACE, ("p256", "0.13.2"), "0.14.0", "a2")
        after[("serde", "1.0.0")] = ([], "c2", True)
        del after[("elliptic", "0.13.0")]
        after[("p256", "0.14.0")] = (["crypto"], "a2", True)
        after[("crypto", "0.1.0")] = ([], "d1", True)
        delta = lockfiles.delta(lockfiles.cargo(lock(WORKSPACE)), lockfiles.cargo(lock(after)))
        self.assertEqual([(c["name"], c["change"]) for c in delta["changed"]],
                         [("p256", "0.13.2 → 0.14.0"), ("serde", "checksum")])
        self.assertEqual([a["name"] for a in delta["added"]], ["crypto"])
        self.assertEqual([r["name"] for r in delta["removed"]], ["elliptic"])
        self.assertIsNone(lockfiles.cargo("not = [toml"))

    def test_nearest_members_through_external_crates(self):
        before = lockfiles.cargo(lock(WORKSPACE))
        transitive = lockfiles.cargo(lock(bumped(WORKSPACE, ("elliptic", "0.13.0"), "0.14.0", "b2")))
        self.assertEqual(lockfiles.affected_members(before, transitive), ["common"], "app reaches it only through common")
        shared = lockfiles.cargo(lock(bumped(WORKSPACE, ("serde", "1.0.0"), "1.0.1", "c2")))
        self.assertEqual(lockfiles.affected_members(before, shared), ["common", "tool"])
        removal = deepcopy(WORKSPACE)
        removal[("common", "0.1.0")] = (["p256"], None, False)
        del removal[("serde", "1.0.0")]
        removal[("tool", "0.1.0")] = ([], None, False)
        self.assertEqual(lockfiles.affected_members(before, lockfiles.cargo(lock(removal))), ["common", "tool"])
        self.assertIsNone(lockfiles.affected_members(before, before), "nothing changed selects nothing")

    def test_bounded_keeps_exact_counts(self):
        many = {"changed": [{"name": str(i), "change": "checksum"} for i in range(50)], "added": [{"name": "x"}], "removed": []}
        kept = lockfiles.bounded(many)
        self.assertEqual(len(kept["changed"]), lockfiles.MAX_DELTA)
        self.assertEqual(kept["added"], [])
        self.assertEqual(kept["counts"], {"changed": 50, "added": 1, "removed": 0})
        self.assertIn("and 47 more", dependency_summary("Cargo.lock", kept))


class ControllerTests(unittest.TestCase):
    """Package selection, generated files and surfacing through the real controller."""

    def setUp(self):
        self.gh = FakeGitHub(deepcopy(CONFIG))
        self.gh.trees[self.gh.base].update({
            "Cargo.toml": '[workspace]\nmembers = ["app", "common", "tool"]\n', "Cargo.lock": lock(WORKSPACE),
            "app/Cargo.toml": '[package]\nname = "app"\n', "app/src/lib.rs": "",
            "common/Cargo.toml": '[package]\nname = "common"\n', "common/src/lib.rs": "",
            "tool/Cargo.toml": '[package]\nname = "tool"\n', "tool/src/lib.rs": ""})
        self.controller = Controller(self.gh, FakeAgents(), CONFIG)
        self.controller.state = {"base": self.gh.base, "issue": 7}

    def candidate(self, files):
        """A workbench reader that returns candidate texts."""
        return type("Workspace", (), {"files": lambda _, paths: {p: files.get(p) for p in paths}})()

    def test_lockfile_changes_select_the_nearest_members(self):
        transitive = lock(bumped(WORKSPACE, ("elliptic", "0.13.0"), "0.14.0", "b2"))
        workspace = self.candidate({"Cargo.lock": transitive})
        self.assertEqual(self.controller.packages(["Cargo.lock"], workspace), {"rust": ["common"]})
        self.assertEqual(self.controller.packages(["Cargo.lock", "tool/src/lib.rs"], workspace), {"rust": ["common", "tool"]})
        self.assertEqual(self.controller.packages(["Cargo.lock"], self.candidate({"Cargo.lock": lock(WORKSPACE)})),
                         {"rust": None}, "a lockfile change reaching no member tests everything")
        self.assertEqual(self.controller.packages(["Cargo.lock"], self.candidate({"Cargo.lock": "broken ["})), {"rust": None})
        self.assertEqual(self.controller.packages(["Cargo.toml"], workspace), {"rust": None}, "the workspace manifest")
        self.assertEqual(self.controller.packages(["common/Cargo.toml"], workspace), {"rust": ["common"]})

    def test_lockfile_members_need_a_library_like_owning_packages(self):
        """A bump reaching only a binary crate tests the workspace, since `--package <bin> --lib` selects nothing."""
        del self.gh.trees[self.gh.base]["tool/src/lib.rs"]
        self.gh.trees[self.gh.base]["tool/src/main.rs"] = "fn main() {}"
        controller = Controller(self.gh, FakeAgents(), CONFIG)
        controller.state = {"base": self.gh.base, "issue": 7}
        shared = self.candidate({"Cargo.lock": lock(bumped(WORKSPACE, ("serde", "1.0.0"), "1.0.1", "c2"))})
        self.assertEqual(controller.packages(["Cargo.lock"], shared), {"rust": None})
        transitive = self.candidate({"Cargo.lock": lock(bumped(WORKSPACE, ("elliptic", "0.13.0"), "0.14.0", "b2"))})
        self.assertEqual(controller.packages(["Cargo.lock"], transitive), {"rust": ["common"]})

    def test_a_workspace_manifest_with_a_root_package_selects_the_workspace(self):
        self.gh.trees[self.gh.base].update({"Cargo.toml": '[workspace]\nmembers = ["app"]\n\n[package]\nname = "root"\n',
                                            "src/lib.rs": ""})
        workspace = self.candidate({})
        self.assertEqual(self.controller.packages(["Cargo.toml"], workspace), {"rust": None})
        self.assertEqual(self.controller.packages(["src/lib.rs"], workspace), {"rust": ["root"]},
                         "the root package's own sources still scope to it")

    def test_malformed_lockfile_structures_fall_back_without_failing(self):
        for text in ('[[package]]\nname = "x"\nversion = "1"\nsource = []\n',
                     '[[package]]\nname = "x"\nversion = "1"\ndependencies = [1]\n',
                     '[[package]]\nname = "x"\nversion = "1"\nchecksum = 1\n', 'package = {}\n',
                     '[[package]]\nname = "x"\nversion = "1"\ndependencies = "x"\n'):
            with self.subTest(text=text):
                self.assertIsNone(lockfiles.cargo(text))
                self.assertEqual(self.controller.packages(["Cargo.lock"], self.candidate({"Cargo.lock": text})),
                                 {"rust": None})

    def test_generated_files_are_read_whole_and_reviewed_as_their_delta(self):
        large = lock({**WORKSPACE, **{(f"crate{i}", "1.0.0"): ([], f"{i:064x}", True) for i in range(1300)}})
        self.assertGreater(len(large.encode()), 140000)
        self.gh.trees[self.gh.base].update({"Cargo.lock": large, "big.md": "x" * 140000})
        self.assertEqual(self.controller.files(self.gh.base, ["Cargo.lock"])["Cargo.lock"], large)
        with self.assertRaises(Blocked):
            self.controller.files(self.gh.base, ["big.md"])
        after = large.replace('version = "0.13.0"', 'version = "0.14.0"')
        dependencies = self.controller.dependencies(["Cargo.lock"], lambda path: after)
        [entry] = changes({"Cargo.lock": large}, {"Cargo.lock": after}, EXECUTION, dependencies)
        self.assertTrue(entry["generated"])
        self.assertNotIn("diff", entry)
        self.assertEqual(entry["dependencies"]["changed"][0]["change"], "0.13.0 → 0.14.0")
        self.assertEqual(len(entry["head_sha256"]), 64)

    def test_dependency_delta_reaches_the_pr_body(self):
        delta = lockfiles.bounded(lockfiles.delta(lockfiles.cargo(lock(WORKSPACE)),
                                                  lockfiles.cargo(lock(bumped(WORKSPACE, ("p256", "0.13.2"), "0.14.0", "a2")))))
        self.controller.state.update(plan=deepcopy(PLAN), local_evidence={"dependencies": {"Cargo.lock": delta}})
        self.controller.checkpoints = SimpleNamespace(plan=None)
        body = self.controller.pr_body()
        self.assertIn("📦 Dependencies", body)
        self.assertIn("p256 0.13.2 → 0.14.0", body)

    def test_manifests_are_package_manifests(self):
        self.assertEqual(self.controller.manifests(["Cargo.lock", "Cargo.toml", "app/Cargo.toml", "app/src/lib.rs"]),
                         ["Cargo.toml", "app/Cargo.toml"])


class PolicyTests(unittest.TestCase):

    def test_generated_files_skip_per_file_limits_but_not_others(self):
        plan = {**deepcopy(PLAN), "scope": {"paths": ["docs/**", "Cargo.lock"], "max_files": 3, "max_changed_lines": 50}}
        large = "x" * 150000
        validate_edits({"summary": "s", "edits": [{"path": "Cargo.lock", "content": large}]}, plan, {}, CONFIG, EXECUTION)
        with self.assertRaisesRegex(Blocked, "invalid text replacement"):
            validate_edits({"summary": "s", "edits": [{"path": "docs/big.md", "content": large}]}, plan, {}, CONFIG, EXECUTION)
        with self.assertRaisesRegex(Blocked, "too large"):
            validate_edits({"summary": "s", "edits": [{"path": "Cargo.lock", "content": large * 2}]}, plan, {}, CONFIG, EXECUTION)

    def test_a_primary_file_cannot_be_generated(self):
        plan = deepcopy(PLAN)
        plan["files"] = ["Cargo.lock"]
        plan["validation"]["files"] = [{"path": "Cargo.lock", "change": "modify", "max_lines": None, "required_text": []}]
        with self.assertRaisesRegex(Blocked, "cannot be generated"):
            validate_plan(plan, CONFIG, EXECUTION)

    def test_dependency_changes_never_merge_autonomously(self):
        config = {**CONFIG, "paths": {**CONFIG["paths"], "autonomous_merge_prefixes": ["vendor/"]}}
        for path in ("vendor/Cargo.lock", "vendor/Cargo.toml"):
            with self.subTest(path=path):
                self.assertFalse(merge_path_allowed(path, config, EXECUTION))
        self.assertTrue(merge_path_allowed("vendor/notes.md", config, EXECUTION))

    def test_reviews_must_assess_every_changed_manifest(self):
        assessed = {**APPROVE, "manifests": [{"path": "app/Cargo.toml", "assessment": "Adds a feature; no test change."}]}
        validate_review(assessed, ["app/Cargo.toml"])
        validate_review(APPROVE)
        for review, manifests in ((APPROVE, ["app/Cargo.toml"]), (assessed, ["app/Cargo.toml", "Cargo.toml"]),
                                  ({**APPROVE, "manifests": [{"path": "a", "assessment": ""}]}, []),
                                  ({**APPROVE, "manifests": [{"path": "a", "assessment": "x", "extra": 1}]}, []),
                                  ({**APPROVE, "manifests": "none"}, [])):
            with self.subTest(review=review, manifests=manifests), self.assertRaises(Blocked):
                validate_review(review, manifests)
        report = {"verdict": "approve", "summary": "s", "findings": [], "dispositions": [], "design_issue": None}
        validate_pr_review({**report, "manifests": assessed["manifests"]}, [], [], ["app/Cargo.toml"])
        with self.assertRaisesRegex(Blocked, "assess every changed manifest"):
            validate_pr_review(report, [], [], ["app/Cargo.toml"])


class PreparationTests(unittest.TestCase):
    """Preparation writes fresh homes seeded only by earlier preparations; checks get fresh copies."""

    def setUp(self):
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64}):
            self.workspace = LocalWorkspace(CONFIG, b"", "concurro/test", EXECUTION, base_archive=b"base")
        self.workspace.current = self.workspace.baseline = {"local_commit": "c", "tree": "t"}
        self.digests = iter(["base", "base", "bumped", "bumped", "failed", "fixed"])
        self.seeds = []
        self.success = {"name": "x", "exit_code": 0, "timed_out": False, "output_limited": False}
        patches = [patch.object(self.workspace, "control", side_effect=self.control),
                   patch.object(self.workspace, "seed", side_effect=lambda v, s, t: self.seeds.append((v, s))),
                   patch.object(self.workspace, "execute", side_effect=self.execute),
                   patch("local.cleanup.remove")]
        self.views = []
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.failing = False

    def control(self, operation, *args, **kwargs):
        return {"digest": next(self.digests)} if operation == "inputs" else {}

    def execute(self, name, command, timeout, **kwargs):
        if kwargs.get("mode") == "prepare":
            self.views.append(kwargs["view"])
            if self.failing:
                return {**self.success, "name": name, "exit_code": 101, "output": "checksum mismatch"}
        return {**self.success, "name": name}

    def test_candidate_lockfiles_are_prepared_into_fresh_homes(self):
        workspace = self.workspace
        workspace.prepare(["check"], 600)
        first = dict(workspace.homes)
        self.assertEqual(self.seeds, [(first[("rust", "cargo")], None)])
        reports = workspace.run_checks(["check"], [], time.monotonic() + 600)
        self.assertEqual([r["name"] for r in reports][-1], "check")
        self.assertEqual(workspace.homes, first, "unchanged inputs keep the preparation")
        copy = workspace.copies[("rust", "cargo")]
        self.assertEqual(self.seeds[-1], (copy, first[("rust", "cargo")]))
        check = " ".join(workspace.command("x", profile="rust"))
        self.assertIn(f"source={copy},target=/cache/rust/cargo", check)
        self.assertNotIn(first[("rust", "cargo")] + ",", check, "checks never mount a prepared home")
        workspace.run_checks(["check"], [], time.monotonic() + 600)
        second = workspace.homes[("rust", "cargo")]
        self.assertNotEqual(second, first[("rust", "cargo")])
        self.assertIn((second, first[("rust", "cargo")]), self.seeds, "seeded from the last preparation only")
        self.assertEqual(self.views, ["stage", "source"])
        self.assertNotEqual(workspace.copies[("rust", "cargo")], copy, "every check run gets a fresh copy")

    def test_candidate_preparation_mounts_the_candidate(self):
        """The real `execute` passes the view through to the container command."""
        workspace = self.workspace
        workspace.homes = {("rust", "cargo"): "home"}
        with patch("local.bounded_process", return_value=self.success) as run:
            workspace.current = {"tree": "t"}
            LocalWorkspace.execute(workspace, "prepare:rust:1", ["cargo", "fetch"], 10, network=True, mode="prepare",
                                   profile="rust", view="source")
        self.assertIn(f"source={workspace.volumes['source']},target=/workspace,readonly", " ".join(run.call_args.args[0]))

    def test_a_failed_candidate_preparation_is_feedback_and_never_reused(self):
        """A half-built home is never used, even when the candidate returns to inputs prepared before."""
        workspace = self.workspace
        self.digests = iter(["base", "bumped", "base"])
        workspace.prepare(["check"], 600)
        self.failing = True
        [failed] = workspace.run_checks(["check"], [], time.monotonic() + 600)
        self.assertEqual((failed["name"], failed["exit_code"], failed["output"]), ("prepare:rust:1", 101, "checksum mismatch"))
        self.failing = False
        reports = workspace.run_checks(["check"], [], time.monotonic() + 600)
        self.assertEqual(reports[-1]["name"], "check")
        self.assertEqual(self.views, ["stage", "source", "source"], "the reverted inputs are prepared again")

if __name__ == "__main__":
    unittest.main()
