"""Large candidates retain complete inventories and recoverable publication."""

import base64
import json
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import change_attention
import checkpoints
import file_inventory
import prompts
import turns
from controller import DISPATCH_ALLOWANCE, Controller
from fakes import DESIGN_ISSUE, FakeAgents, FakeGitHub
from github import Transient
from pipeline_fixtures import PipelineFixture
from policy import Blocked, approval_binding, digest
from repository_reader import RepositoryReader
from runner_fixtures import Workspace
from test_configurable_edit_policy import open_config


class InventoryGitHub(FakeGitHub):
    """Use stable blob identities for tests that compare immutable trees directly."""

    def repo(self, path, method="GET", data=None):

        if path.startswith("git/blobs/") and ":" not in path:
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.inventory_blobs[path.rsplit("/", 1)[1]].encode()).decode(),
            }
        result = super().repo(path, method, data)
        if path.startswith("git/trees/"):
            self.inventory_blobs = getattr(self, "inventory_blobs", {})
            for entry in result["tree"]:
                old = super().repo("git/blobs/" + entry["sha"])
                content = base64.b64decode(old["content"]).decode()
                entry["sha"] = digest(content)[:40]
                self.inventory_blobs[entry["sha"]] = content
        return result


class LargePublicationTests(unittest.TestCase):
    """Use real checkpoints across every tick, including lost publication responses."""

    def run_candidate(self, crash=None):
        """Publish paths which overflowed the former inline audit after PR creation."""
        files = {f"docs/{i:03d}-{'x' * 170}.txt": "new\n" for i in range(200)}
        config = {**open_config(), "autonomous": True}

        class WideAgents(FakeAgents):
            """Keep the standard approvals while widening the implementation."""

            def ask(self, provider, stage, context):
                report = super().ask(provider, stage, context)
                if stage == "plan":
                    return {**report, "scope": {"paths": ["docs/**"]}}
                if stage == "develop":
                    return {**report, "edits": [{"path": p, "content": c} for p, c in files.items()]}
                return report

        github, agents = InventoryGitHub(config), WideAgents()
        original = github.repo
        lost = False
        creations = 0

        def repo(path, method="GET", data=None):
            nonlocal lost, creations
            if path == "pulls" and method == "POST":
                creations += 1
            result = original(path, method, data)
            if (
                (
                    path == crash
                    or crash == "branch"
                    and path.startswith("git/refs/heads/")
                    or crash == "checkpoint"
                    and path.startswith("issues/7/artifacts/")
                    and data
                    and data.get("body", "").startswith(checkpoints.AUDIT_MARKER)
                    and checkpoints.legacy_state(data["body"])["state"]["stage"] == "review"
                )
                and method in ("POST", "PATCH")
                and not lost
            ):
                lost = True
                raise OSError("lost response")
            return result

        with patch.object(github, "repo", side_effect=repo):
            for _ in range(30):
                controller = Controller(github, agents, config)
                try:
                    controller.advance(7)
                except checkpoints.CheckpointError:
                    if not lost or crash != "checkpoint":
                        raise
                    continue
                except OSError:
                    continue
                self.assertLessEqual(len(controller.checkpoints.audit["body"].encode()), checkpoints.LIMIT)
                if controller.state.get("handoff_ready"):
                    break
        self.assertTrue(controller.state.get("handoff_ready"), controller.state.get("reason"))
        self.assertEqual(controller.state["changed_files"], sorted(files))
        self.assertEqual(creations, 1)
        self.assertEqual(lost, crash is not None)
        self.assertEqual(sum(stage == "develop" for _, stage, _ in agents.calls), 1)
        return controller, github

    def test_long_inventories_survive_all_checkpoints(self):
        controller, _ = self.run_candidate()
        body = controller.checkpoints.audit["body"]
        self.assertNotIn("x" * 170, body)
        payload = checkpoints.legacy_state(body)
        self.assertEqual(payload["state"]["version"], 7)
        for version in (3, 4, 9):
            payload["state"]["version"] = version
            with self.assertRaises(checkpoints.CheckpointError):
                controller.checkpoints.decode(
                    {"body": checkpoints.AUDIT_MARKER + checkpoints.STATE_PREFIX + json.dumps(payload) + "\n```\n"}
                )

    def test_reopen_retains_the_original_approval_inventory_parent(self):

        controller, github = self.run_candidate()
        old_head = controller.state["head"]
        approved = list(controller.state["changed_files"])
        new = controller._publication().build_commit(
            old_head, [{"path": "docs/extra.txt", "content": "extra\n"}], controller.tree
        )["sha"]
        controller.state.update(
            head=new,
            design_parent=old_head,
            approved_accumulated_files=approved,
            changed_files=sorted([*approved, "docs/extra.txt"]),
        )
        github.pr["head"]["sha"] = new
        controller.checkpoint()
        controller.reopen_design(DESIGN_ISSUE, "pr_review:test")
        restored = controller.checkpoints.decode(controller.checkpoints.audit)
        self.assertEqual(restored["design_parent"], new)
        self.assertEqual(restored["approved_inventory_parent"], old_head)
        self.assertEqual(restored["approved_accumulated_files"], approved)
        self.assertIn("docs/extra.txt", restored["changed_files"])

    def test_lost_completion_checkpoint_keeps_complete_inventory(self):
        self.run_candidate("checkpoint")

    def test_lost_branch_response_keeps_complete_inventory(self):
        self.run_candidate("branch")

    def test_lost_pr_response_keeps_complete_inventory(self):
        self.run_candidate("pulls")


class ExtractionBytesTests(Workspace, unittest.TestCase):
    """A count-unlimited workspace still has bounded extraction memory and wire bytes."""

    def test_aggregate_and_escaped_content_are_bounded(self):
        root = self.prepare()
        (root / "work/docs/a.txt").write_text('"' * 400)
        (root / "work/docs/b.txt").write_text('"' * 400)
        with (
            patch.object(turns, "MAX_EXTRACT_BYTES", 1000, create=True),
            self.assertRaisesRegex(turns.TurnError, "extraction.*bytes"),
        ):
            turns.extract(root / "work", root / "pristine.git", root)

    def test_large_file_is_refused_before_read_bytes(self):
        root = self.prepare()
        (root / "work/docs/a.txt").write_text("x" * 1001)
        with (
            patch.object(turns, "MAX_EXTRACT_BYTES", 1000, create=True),
            patch("pathlib.Path.read_bytes", side_effect=AssertionError("unbounded read")),
            self.assertRaisesRegex(turns.TurnError, "extraction.*bytes"),
        ):
            turns.extract(root / "work", root / "pristine.git", root)

    def test_git_output_is_bounded_and_reaped(self):
        root = self.prepare()
        with (
            patch.object(turns, "MAX_GIT_OUTPUT_BYTES", 1),
            self.assertRaisesRegex(turns.TurnError, "Git output.*bytes"),
        ):
            turns.extract(root / "work", root / "pristine.git", root)

    def test_exact_json_limit_including_unicode_and_paths(self):
        root = self.prepare()
        (root / "work/docs/new.txt").write_text("\t界\n")
        result = turns.extract(root / "work", root / "pristine.git", root)
        size = len(json.dumps(result).encode())
        with patch.object(turns, "MAX_EXTRACT_BYTES", size):
            self.assertEqual(turns.extract(root / "work", root / "pristine.git", root), result)
        with patch.object(turns, "MAX_EXTRACT_BYTES", size - 1), self.assertRaisesRegex(turns.TurnError, "JSON bytes"):
            turns.extract(root / "work", root / "pristine.git", root)


class InventoryTests(unittest.TestCase):
    """Capped REST lists cannot hide paths from the controller's policy checks."""

    def test_capped_pr_and_compare_lists_use_complete_pinned_trees(self):

        base, head = "a" * 40, "b" * 40
        paths = [f"src/{i}.rs" for i in range(3100)] + [".concurro/config.yaml"]
        calls = []

        class GitHub:
            """Expose REST caps while retaining all paths in immutable Git trees."""

            def pages(self, path):
                calls.append(path)
                return [{"filename": p} for p in paths[:3000]]

            def repo(self, path):
                calls.append(path)
                if path == "pulls/9":
                    return {"head": {"sha": head}, "base": {"sha": base}}
                if path.startswith("compare/"):
                    return {"merge_base_commit": {"sha": base}, "files": paths[:300]}
                return {
                    "truncated": False,
                    "tree": []
                    if base in path
                    else [{"path": p, "sha": "c" * 40, "type": "blob", "mode": "100644"} for p in paths],
                }

        github = GitHub()
        reader = RepositoryReader(github, github)
        state = {"base": base, "head": head}
        result = reader.pr_paths(state, 9)
        self.assertEqual(result, sorted(paths))
        self.assertIn(".concurro/config.yaml", result)
        self.assertEqual(len([p for p in calls if p.startswith("git/trees")]), 2)
        with patch.object(github, "repo", return_value={"head": {"sha": head}, "base": {"sha": base}}):
            with self.assertRaisesRegex(Blocked, "cannot prove"):
                reader.pr_paths(state, 9)

    def test_tree_comparison_preserves_rename_deletion_and_mode_change(self):

        reader = RepositoryReader(None, None)
        blob = {"sha": "c" * 40, "type": "blob", "mode": "100644"}
        reader.trees = {
            "base": {
                "old": blob,
                "deleted": blob,
                "same": blob,
                "mode": blob,
                "dir": {**blob, "type": "tree"},
                "dir/child": blob,
            },
            "head": {"new": blob, "same": blob, "mode": {**blob, "mode": "100755"}, "dir": blob},
        }
        self.assertEqual(
            reader.changed_paths({}, "base", "head"), ["deleted", "dir", "dir/child", "mode", "new", "old"]
        )

    def test_truncated_tree_is_never_treated_as_complete(self):

        github = Mock()
        github.repo.return_value = {"tree": [], "truncated": True}
        with self.assertRaisesRegex(Blocked, "tree is truncated"):
            RepositoryReader(github, github).changed_paths({"base": "base"}, "base", "head")

    def test_approval_inventory_uses_design_parent_and_preserves_digest(self):

        paths = [f"docs/{i:04d}-{'x' * 100}.txt" for i in range(100)]
        state = {
            "base": "a" * 40,
            "head": "b" * 40,
            "design_parent": "c" * 40,
            "plan_id": "p",
            "plan": {},
            "requirements": {},
            "config_digest": "config",
            "approved_accumulated_files": paths,
        }
        before = approval_binding(state, CONFIG)
        # A reopen may advance planning while the old approved list still exists.
        reopened = deepcopy(state)
        file_inventory.retain_approval_parent(reopened)
        reopened["design_parent"] = state["head"]
        _, reopened_refs = file_inventory.pack(reopened)
        self.assertEqual(reopened_refs[0]["head"], state["design_parent"])
        compact, refs = file_inventory.pack(state)
        reader = Mock()
        reader.changed_paths.return_value = paths
        file_inventory.restore(compact, refs, reader)
        reader.changed_paths.assert_called_once_with(compact, state["base"], state["design_parent"])
        self.assertEqual(compact, state)
        self.assertEqual(approval_binding(compact, CONFIG), before)
        bad = deepcopy(refs)
        bad[0]["head"] = state["head"]
        reader.reset_mock()
        with self.assertRaises(ValueError):
            file_inventory.restore(file_inventory.pack(state)[0], bad, reader)
        reader.changed_paths.assert_not_called()
        reader.changed_paths.return_value = paths[:-1]
        with self.assertRaisesRegex(ValueError, "commitment"):
            file_inventory.restore(file_inventory.pack(state)[0], refs, reader)


class AttentionTests(unittest.TestCase):
    """Editable policy and workflow paths are visible independently of model prose."""

    def test_notice_is_deterministic_bounded_and_links_complete_change(self):

        paths = [f".github/{i:03d}-{'x' * 180}.yaml" for i in range(200)] + [
            "AGENTS.md",
            "docs/custom.md",
            "src/lib.rs",
        ]
        config = {**open_config(), "policy_documents": ["docs/custom.md"]}
        selected = change_attention.paths(paths, config)
        self.assertEqual(len(selected), 202)
        result = change_attention.notice(paths, config, "a" * 40, "b" * 40)
        self.assertEqual(result, change_attention.notice(reversed(paths), config, "a" * 40, "b" * 40))
        self.assertIn("(202)", result)
        self.assertIn("more;", result)
        self.assertIn("/compare/" + "a" * 40 + "..." + "b" * 40, result)
        self.assertLess(len(result.encode()), 2500)


class PublicationCapacityTests(PipelineFixture, unittest.TestCase):
    """Publication requires durable recovery data before any ref or PR mutation."""

    def setUp(self):
        super().setUp()
        self.config["autonomous"] = True

    def test_storage_failure_precedes_branch_and_pr_writes(self):
        self.assert_capacity_refusal(self.reach("publish"))

    def test_storage_failure_precedes_existing_pr_update(self):
        controller = self.reach("gates")
        controller.state["pending"] = {
            "head": controller.state["head"],
            "parent": controller.state["candidate"]["parent"],
            "initial": False,
            "summary": "Update the existing PR",
        }
        controller.state["stage"] = "publish"
        self.assert_capacity_refusal(controller)

    def assert_capacity_refusal(self, controller):
        """Require the original intent to survive while every PR/ref mutation is refused."""
        before_pr = deepcopy(self.gh.pr)
        before_head = dict(self.gh.refs)
        controller.state["capacity_probe"] = "x" * (
            checkpoints.LIMIT - len(controller.checkpoints.audit_body(controller.state).encode()) - 2000
        )
        controller.checkpoint()
        with (
            patch.object(self.gh, "repo", wraps=self.gh.repo) as repo,
            patch.object(
                controller.checkpoints.reports, "put", side_effect=Blocked("publication checkpoint storage unavailable")
            ),
            self.assertRaisesRegex(Blocked, "publication checkpoint"),
        ):
            controller.publish()
        self.assertFalse(
            any(
                call.args[1:2] in (("POST",), ("PATCH",)) and call.args[0].startswith(("git/", "pulls"))
                for call in repo.call_args_list
            )
        )
        self.assertEqual(self.gh.pr, before_pr)
        self.assertEqual(self.gh.refs, before_head)
        loaded = self.loaded()
        self.assertEqual(loaded.state["stage"], "publish")
        self.assertEqual(loaded.state["pending"], controller.state["pending"])

    def test_publication_proceeds_with_oversized_metadata_retained(self):
        """The compact audit target is no longer a reason to reject a publishable candidate."""
        controller = self.reach("publish")
        controller.state["capacity_probe"] = "x" * 100000
        controller.checkpoint()
        controller.publish()
        self.assertIsNotNone(self.gh.pr)
        self.assertEqual(self.loaded().state["capacity_probe"], "x" * 100000)
        self.assertLessEqual(len(controller.checkpoints.audit["body"].encode()), checkpoints.LIMIT)

    def test_review_context_retains_all_identities_and_explains_omitted_previews(self):

        controller = self.reach("implement")
        before = {f"docs/{i}.txt": "old\n" for i in range(12)}
        after = {path: "new content\n" * 3000 for path in before}
        controller.config["max_context_bytes"] = 34000
        context = controller.pr_review_context("claude", before, after, "e" * 40)
        self.assertEqual([v["path"] for v in context["changes"]], sorted(after))
        self.assertTrue(all(v["truncated"] for v in context["changes"]))
        self.assertTrue(all("inspect the pinned repository" in v["diff"] for v in context["changes"]))
        self.assertLessEqual(prompts.size("review", context, controller.bundle), 34000 - DISPATCH_ALLOWANCE)
        controller.config["max_context_bytes"] = 2000
        self.assertIsNotNone(controller.oversized(None, before, after))


class InventorySaveProofTests(unittest.TestCase):
    """Read-only inventory failures must not poison the checkpoint writer."""

    def setUp(self):
        """Seed a durable checkpoint and a larger pinned candidate, with no live GitHub calls."""
        self.github = InventoryGitHub(CONFIG)
        self.head = "b" * 40
        self.paths = [f"docs/{i:03d}-{'x' * 80}.txt" for i in range(80)]
        self.github.trees[self.head] = {**self.github.trees[self.github.base], **dict.fromkeys(self.paths, "new\n")}
        self.github.commits[self.head] = {"tree": {"sha": self.head}}
        self.cp = checkpoints.Checkpoints(self.github, CONFIG, 7)
        self.initial = {"version": 2, "issue": 7, "stage": "review"}
        self.cp.save(self.initial)
        self.state = {**self.initial, "base": self.github.base, "head": self.head, "changed_files": self.paths}

    def assert_read_failure(self, failure, expected_type, message):
        """Check no write, unchanged state and successful reuse after the read evidence recovers."""
        before_state, before_artifacts = deepcopy(self.state), deepcopy(self.github.artifacts)
        before_audit = deepcopy(self.cp.audit)
        original = self.github.repo

        def repo(path, method="GET", data=None):
            if path == f"git/trees/{self.head}?recursive=1":
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return original(path, method, data)

        with (
            patch.object(self.github, "repo", side_effect=repo) as requests,
            patch.object(self.cp.reports, "put", wraps=self.cp.reports.put) as put,
            self.assertRaisesRegex(expected_type, message) as caught,
        ):
            self.cp.save(self.state)
        if isinstance(failure, Exception):
            self.assertIs(caught.exception, failure)
        self.assertTrue(requests.call_args_list)
        self.assertTrue(all(len(call.args) == 1 or call.args[1] == "GET" for call in requests.call_args_list))
        put.assert_not_called()
        self.assertFalse(self.cp.uncertain)
        self.assertEqual(self.state, before_state)
        self.assertEqual(self.cp.audit, before_audit)
        self.assertEqual(self.github.artifacts, before_artifacts)
        # Re-read corrected immutable evidence instead of retaining the injected malformed tree.
        self.cp.reader.trees.clear()
        self.cp.save(self.state)
        self.assertEqual(self.cp.decode(self.cp.audit), self.state)

    def test_transient_tree_reads_remain_transient(self):
        self.assert_read_failure(Transient("unavailable", 30), Transient, Transient.TEXT["unavailable"])

    def test_rate_limited_tree_reads_remain_transient(self):
        self.assert_read_failure(Transient("throttled", 30), Transient, Transient.TEXT["throttled"])

    def test_truncated_tree_does_not_mark_save_uncertain(self):
        self.assert_read_failure({"truncated": True, "tree": []}, Blocked, "tree is truncated")

    def test_commitment_mismatch_does_not_mark_save_uncertain(self):
        self.assert_read_failure({"truncated": False, "tree": []}, ValueError, "commitment mismatch")

    def test_lost_actual_write_still_requires_reload(self):
        original = self.github.repo
        error = Transient("response_lost")

        def repo(path, method="GET", data=None):
            result = original(path, method, data)
            if method == "PATCH" and path.startswith("issues/7/artifacts/"):
                raise error
            return result

        with patch.object(self.github, "repo", side_effect=repo), self.assertRaises(Transient) as caught:
            self.cp.save(self.state)
        self.assertIs(caught.exception, error)
        self.assertTrue(self.cp.uncertain)
        with patch.object(self.github, "repo", wraps=self.github.repo) as requests:
            with self.assertRaises(checkpoints.CheckpointError) as caught:
                self.cp.save(self.state)
            self.assertEqual(caught.exception.code, "write_uncertain")
            requests.assert_not_called()
        recovered = checkpoints.Checkpoints(self.github, CONFIG, 7).load([])
        self.assertEqual(recovered, self.state)
