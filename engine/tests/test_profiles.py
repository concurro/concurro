"""Execution profiles: schema, composition, bundle binding and workbench isolation, all offline."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from support import CONFIG, EXECUTION, PROFILES, ROOT, TARGET, setUpModule, tearDownModule  # unittest module fixtures
import checks
from bundle import Bundle
from local import LocalWorkspace
from policy import Blocked, merge_path_allowed, path_allowed
import workspace_entry

RUST = json.loads((ROOT / "bundle/profiles/rust.json").read_text())
DOCS = {"version": 1, "id": "docs", "triggers": ["docs/**/*.md"], "protected": ["docs/.lint.json"],
        "caches": ["lint"], "env": {"LINT_HOME": "{cache:lint}"},
        "prepare": {"requires": ["docs/.lint.json"], "source": False,
                    "steps": [["lint-setup", "--config", "/prepare/input/docs/.lint.json", "--home", "{cache:lint}"]]},
        "checks": [{"id": "markdown", "purpose": "Lint changed documentation offline.",
                    "command": ["lint", "--home", "{cache:lint}", "docs"], "prepared": True, "mandatory": True},
                   {"id": "spelling", "purpose": "Check documentation spelling.",
                    "command": ["spell", "docs"], "prepared": False, "mandatory": False}]}


def load(profile, expected=None):
    """Validate a profile document as the bundle loader does."""
    return checks.load_profile(json.dumps(profile), expected or profile["id"])


def composed(*profiles):
    """Compose validated profiles in order."""
    return checks.compose([load(p) for p in profiles])


class ProfileSchemaTests(unittest.TestCase):
    """Profiles are strict data; anything ambiguous fails closed at load."""

    def test_built_in_rust_reproduces_previous_behaviour(self):
        """Rust paths require Cargo checks; other paths require only control checks."""
        self.assertEqual(checks.required(["docs/a.md"], EXECUTION), ["diff", "scope", "text"])
        self.assertEqual(checks.required(["crates/a/src/lib.rs"], EXECUTION),
                         ["diff", "scope", "text", "format", "check", "nextest"])
        self.assertEqual(checks.preparing(["format"], EXECUTION), [])
        self.assertEqual(checks.preparing(["nextest"], EXECUTION), ["rust"])

    def test_invalid_profiles_are_rejected(self):
        """Selection, environment, placeholders, preparation and check IDs are all validated."""
        mutations = {
            "id_mismatch": lambda p: p.update(id="other"),
            "extra_field": lambda p: p.update(extra=True),
            "reserved_env": lambda p: p["env"].update(PATH="/evil"),
            "engine_env": lambda p: p["env"].update(CONCURRO_ISSUE="1"),
            "loader_env": lambda p: p["env"].update(LD_PRELOAD="/evil.so"),
            "unknown_cache": lambda p: p["env"].update(X="{cache:missing}"),
            "stray_brace": lambda p: p["checks"][0]["command"].append("{oops}"),
            "absolute_trigger": lambda p: p["triggers"].append("/etc/*"),
            "traversal_protected": lambda p: p["protected"].append("../x"),
            "unprotected_input": lambda p: p["prepare"]["requires"].append("README.md"),
            "glob_input": lambda p: p["prepare"]["requires"].append("docs/*.json"),
            "control_collision": lambda p: p["checks"][0].update(id="diff"),
            "duplicate_check": lambda p: p["checks"].append(deepcopy(p["checks"][0])),
            "empty_argv": lambda p: p["checks"][0].update(command=[]),
            "bad_cache_name": lambda p: p.update(caches=["../escape"]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                profile = deepcopy(DOCS)
                mutate(profile)
                with self.assertRaises(checks.ProfileError):
                    load(profile, "docs")
        unprepared = deepcopy(DOCS)
        unprepared["prepare"] = None
        unprepared["caches"], unprepared["env"] = [], {}
        unprepared["checks"][0]["command"] = ["lint", "docs"]
        with self.assertRaises(checks.ProfileError):
            load(unprepared)

    def test_composition_rejects_collisions_and_keeps_order(self):
        """Check IDs never merge; each profile's triggers select only its own mandatory checks."""
        execution = composed(RUST, DOCS)
        self.assertEqual(checks.required(["docs/guide/a.md"], execution), ["diff", "scope", "text", "markdown"])
        self.assertEqual(checks.required(["src/a.rs", "docs/a.md"], execution),
                         ["diff", "scope", "text", "format", "check", "nextest", "markdown"])
        self.assertEqual(checks.preparing(["markdown", "check"], execution), ["rust", "docs"])
        clash = deepcopy(DOCS)
        clash["checks"][1]["id"] = "format"
        with self.assertRaises(checks.ProfileError):
            composed(RUST, clash)
        with self.assertRaises(checks.ProfileError):
            checks.compose([load(RUST), load(RUST)])

    def test_optional_prepared_check_is_selectable_without_a_trigger(self):
        """A docs-only plan may still choose a check that needs preparation."""
        plan = {"files": ["docs/a.md"], "validation": {"checks": ["diff", "scope", "text", "check"], "files": [
            {"path": "docs/a.md", "change": "modify", "max_lines": None, "required_text": []}]}}
        self.assertEqual(checks.contract_errors(plan, EXECUTION), [])
        self.assertEqual(checks.preparing(plan["validation"]["checks"], EXECUTION), ["rust"])
        catalog = checks.catalog(plan["files"], EXECUTION)
        self.assertIn({"id": "check", "profile": "rust", "purpose": RUST["checks"][1]["purpose"], "prepared": True},
                      catalog["checks"])

    def test_nested_profile_protections_and_merge_risk(self):
        """Profile globs protect nested manifests; triggered paths never merge autonomously."""
        for path in ("Cargo.toml", "crates/a/Cargo.toml", "crates/a/build.rs", "nested/Cargo.lock",
                     "rust-toolchain.toml"):
            with self.subTest(path=path):
                self.assertFalse(path_allowed(path, CONFIG, EXECUTION))
        self.assertTrue(path_allowed("crates/a/src/lib.rs", CONFIG, EXECUTION))
        execution = composed(RUST, DOCS)
        self.assertFalse(merge_path_allowed("docs/guide/a.md", CONFIG, execution))
        self.assertTrue(merge_path_allowed("docs/guide/a.md", CONFIG, EXECUTION))


class ProfileBundleTests(unittest.TestCase):
    """Selected profiles bind into the bundle identity and travel to agent containers."""

    def target(self, directory, profile=DOCS):
        """Create a target checkout with the example charter and one target profile."""
        root = Path(directory)
        shutil.copytree(TARGET / ".concurro", root / ".concurro")
        (root / ".concurro/profiles").mkdir()
        (root / ".concurro/profiles/docs.json").write_text(json.dumps(profile))
        return root

    def test_target_profile_identity_and_container_transport(self):
        """A container reproduces the identity only from the exact selection and profile text."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.target(directory)
            selection = ["rust", "target:docs"]
            host = Bundle(target=root, profiles=selection)
            self.assertEqual([p["id"] for p in host.execution["profiles"]], ["rust", "docs"])
            texts = host.target_profile_texts
            self.assertEqual(set(texts), {".concurro/profiles/docs.json"})
            container = Bundle(ROOT, charter_text=host.charter_text, profiles=selection, profile_texts=texts)
            self.assertEqual(container.identity, host.identity)
            self.assertNotEqual(Bundle(target=root, profiles=["rust"]).identity, host.identity)
            self.assertNotEqual(Bundle(target=root, profiles=["target:docs", "rust"]).identity, host.identity)
            changed = {path: text + "\n" for path, text in texts.items()}
            self.assertNotEqual(Bundle(ROOT, charter_text=host.charter_text, profiles=selection,
                                       profile_texts=changed).identity, host.identity)
            for bad in ({}, {**texts, ".concurro/profiles/other.json": "{}"}):
                with self.subTest(texts=sorted(bad)), self.assertRaises(Blocked):
                    Bundle(ROOT, charter_text=host.charter_text, profiles=selection, profile_texts=bad)

    def test_invalid_selection_and_missing_files_fail_closed(self):
        """Unknown built-ins, malformed entries and absent target files never fall back silently."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.target(directory)
            for selection in (["python3"], ["target:Docs"], ["rust", "rust"], [], ["target:missing"],
                              ["a", "b", "c", "d", "e"]):
                with self.subTest(selection=selection), self.assertRaises((Blocked, FileNotFoundError)):
                    Bundle(target=root, profiles=selection)
            (root / ".concurro/profiles/docs.json").unlink()
            (root / ".concurro/profiles/docs.json").symlink_to(TARGET / ".concurro/charter.json")
            with self.assertRaises(Blocked):
                Bundle(target=root, profiles=["target:docs"])


class PreparationIsolationTests(unittest.TestCase):
    """Preparation is networked, so its container sees only approved inputs and its own caches."""

    def workspace(self, *profiles):
        """Construct an unopened workbench for command inspection."""
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64}):
            return LocalWorkspace(CONFIG, b"", "concurro/test", composed(*profiles), base_archive=b"base")

    def test_prepare_container_is_neutral_and_reads_only_the_approved_base(self):
        """No candidate source, Git state or other profile caches; source view only when declared."""
        workspace = self.workspace(RUST, DOCS)
        rust = " ".join(workspace.command("x", network=True, mode="prepare", profile="rust"))
        docs = " ".join(workspace.command("x", network=True, mode="prepare", profile="docs"))
        for command in (rust, docs):
            self.assertIn("--workdir=/prepare", command)
            self.assertIn("target=/prepare/input,readonly", command)
            self.assertNotIn(workspace.volumes["source"], command)
            self.assertNotIn(workspace.volumes["state"], command)
            self.assertNotIn("--network=none", command)
        self.assertIn(f"source={workspace.volumes['stage']},target=/workspace,readonly", rust)
        self.assertNotIn("target=/workspace", docs)
        self.assertIn("target=/cache/rust/cargo", rust)
        self.assertNotIn("/cache/docs/", rust)
        self.assertNotIn("/cache/rust/", docs)
        self.assertIn("--env CARGO_HOME=/cache/rust/cargo", rust)
        self.assertNotIn("LINT_HOME", rust)

    def test_profile_environment_never_reaches_the_control_helper(self):
        """The trusted helper sees every volume but no profile variables."""
        workspace = self.workspace(RUST, DOCS)
        control = " ".join(workspace.command("x", control=True))
        self.assertNotIn("--env", control)
        check = " ".join(workspace.command("x", profile="docs"))
        self.assertIn("--env LINT_HOME=/cache/docs/lint", check)
        self.assertIn("target=/workspace,readonly", check)
        self.assertIn("--network=none", check)

    def test_shared_budget_order_and_preparation_before_edits(self):
        """All profiles share one budget, run in order, and cannot start after a checkpoint."""
        workspace = self.workspace(RUST, DOCS)
        workspace.current = workspace.baseline = {"local_commit": "base", "tree": "t"}
        success = {"exit_code": 0, "timed_out": False, "output_limited": False}
        clock = iter([0, 1, 2, 3, 60])
        with patch.object(workspace, "control", return_value={}) as control, \
                patch.object(workspace, "execute", return_value=success) as execute, \
                patch("local.time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(Blocked, "shared time budget"):
                workspace.prepare(["check", "markdown"], 30)
        self.assertEqual([c.args[0] for c in execute.call_args_list], ["prepare:rust:1"])
        self.assertEqual(execute.call_args.args[2], 27)
        self.assertEqual([c.args[:2] for c in control.call_args_list], [("stage",), ("inputs", "rust")])
        self.assertEqual([c.kwargs["timeout"] for c in control.call_args_list], [29, 28])
        workspace.current = {"local_commit": "edited", "tree": "u"}
        with self.assertRaisesRegex(Blocked, "precede candidate edits"):
            workspace.prepare(["check"], 30)

    def test_helper_operations_stop_at_the_shared_deadline(self):
        """A slow staging helper consumes the budget instead of granting each helper its own."""
        workspace = self.workspace(RUST)
        workspace.current = workspace.baseline = {"local_commit": "base", "tree": "t"}
        clock = iter([0, 1, 21])
        with patch.object(workspace, "control", return_value={}) as control, \
                patch.object(workspace, "execute") as execute, \
                patch("local.time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(Blocked, "shared time budget"):
                workspace.prepare(["check"], 5)
        self.assertEqual([(c.args[0], c.kwargs["timeout"]) for c in control.call_args_list], [("stage", 4)])
        execute.assert_not_called()
        with patch("local.subprocess.run", side_effect=[subprocess.TimeoutExpired("docker", 4), None]) as run:
            with self.assertRaisesRegex(Blocked, "timed out"):
                workspace.control("stage", data=b"base", timeout=4)
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 4)
        self.assertEqual(run.call_args_list[1].args[0][:3], ["docker", "rm", "-f"])

    def test_checks_resolve_profile_argv_and_require_preparation(self):
        """Models select IDs; argv and placeholders come from the profile, and prepared checks wait."""
        workspace = self.workspace(RUST, DOCS)
        workspace.current = {"local_commit": "base", "tree": "t"}
        success = {"exit_code": 0, "timed_out": False, "output_limited": False}
        with patch.object(workspace, "execute", return_value=success) as execute:
            with self.assertRaisesRegex(Blocked, "prepared before candidate edits"):
                workspace.run_checks(["diff"], ["markdown"], 10 ** 9)
            workspace.prepared.add("docs")
            workspace.run_checks(["diff"], ["markdown", "spelling"], 10 ** 9)
        commands = {c.args[0]: c.args[1] for c in execute.call_args_list}
        self.assertEqual(commands["markdown"], ["lint", "--home", "/cache/docs/lint", "docs"])
        self.assertEqual(execute.call_args_list[1].kwargs["profile"], "docs")
        self.assertTrue(execute.call_args_list[0].kwargs["control"])


class HelperPreparationTests(unittest.TestCase):
    """The trusted helper stages the approved base and copies only declared inputs."""

    def archive(self, files):
        """Build a single-root GitHub-style archive."""
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as handle:
            for name, text in files.items():
                data = text.encode()
                item = tarfile.TarInfo("repo/" + name)
                item.size = len(data)
                handle.addfile(item, io.BytesIO(data))
        return output.getvalue()

    def test_inputs_come_from_the_staged_base_only(self):
        """Declared files are copied read-only; missing, linked or escaping inputs are rejected."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, inputs = root / "stage", root / "input"
            stage.mkdir()
            inputs.mkdir()
            stream = io.TextIOWrapper(io.BytesIO(self.archive({"Cargo.lock": "lock\n", "src/lib.rs": "fn x() {}\n"})))
            with patch.multiple(workspace_entry, STAGE=stage, INPUT=inputs), patch.object(sys, "stdin", stream):
                workspace_entry.stage()
                workspace_entry.inputs("rust", ["Cargo.lock"])
                copied = inputs / "rust/Cargo.lock"
                self.assertEqual(copied.read_text(), "lock\n")
                self.assertEqual(copied.stat().st_mode & 0o777, 0o444)
                self.assertFalse((inputs / "rust/src").exists())
                (stage / "link.lock").symlink_to(stage / "Cargo.lock")
                for bad in (["missing.lock"], ["../Cargo.lock"], ["/etc/passwd"], ["link.lock"], [".git/config"]):
                    with self.subTest(bad=bad), self.assertRaises(ValueError):
                        workspace_entry.inputs("rust", bad)
                with self.assertRaises(ValueError):
                    workspace_entry.inputs("../rust", ["Cargo.lock"])

    def test_cache_mounts_must_be_engine_assigned(self):
        """Initialization accepts only /cache/<profile>/<name> mount points."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("source", "state", "cache/rust/cargo"):
                (root / name).mkdir(parents=True)
            stream = io.TextIOWrapper(io.BytesIO(self.archive({"a.md": "x\n"})))
            with patch.multiple(workspace_entry, ROOT=root / "source", STATE=root / "state", CACHE=root / "cache"), \
                    patch.object(sys, "stdin", stream):
                with self.assertRaises(ValueError):
                    workspace_entry.initialize("concurro/test", str(root / "cache/rust/cargo/../../escape"))


if __name__ == "__main__":
    unittest.main()
