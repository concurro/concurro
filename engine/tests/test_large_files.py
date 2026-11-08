"""Large source stays in Git while metadata and bounded previews guide independent reviews."""

from copy import deepcopy
import json
import unittest

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule
from controller import DISPATCH_ALLOWANCE, transport_size, sha256, changes, diff_preview, DIFF_TRUNCATED, MAX_DIFF
from policy import Blocked, validate_edits
from repository import Repository
from review_fixtures import Harness
from fakes import FakeAgents, PLAN
from runner_fixtures import Workspace
import turns

# Above the former extraction/snapshot limit as well as both response-size limits.
SOURCE = "// large source fixture\n" + "// unchanged body\n" * 200_000


class LargeAgents(FakeAgents):
    """Make one small edit in a large file, then repair it after a real review request."""

    def __init__(self):
        super().__init__(fix=True)
        self.developments = 0

    def ask(self, provider, stage, context):
        """Reuse the lifecycle fixture's review ledger with native-sized workspace results."""
        if stage == "develop":
            self.calls.append((provider, stage, deepcopy(context)))
            self.developments += 1
            return {
                "summary": "Correct the guide.",
                "validation": "Checked the changed line.",
                "design_issue": None,
                "edits": [{"path": "docs/guide.md", "content": SOURCE + f"Correction {self.developments}\n"}],
            }
        return super().ask(provider, stage, context)


class LifecycleTests(Harness, unittest.TestCase):
    """Exercise large base files, exact publication bytes, repair and readiness."""

    def test_large_source_reaches_ready_through_a_review_and_repair(self):
        self.gh.trees[self.gh.base]["docs/guide.md"] = SOURCE
        agents = LargeAgents()
        controller = self.run_to(agents, "gates")
        controller.advance(7)
        self.assertTrue(controller.state["handoff_ready"])
        self.assertEqual(agents.developments, 2)
        head = self.gh.pr["head"]["sha"]
        tree = self.gh.commits[head]["tree"]["sha"]
        self.assertEqual(self.gh.trees[tree]["docs/guide.md"], SOURCE + "Correction 2\n")
        self.assertEqual(len(self.gh.blobs), 2)
        for _, stage, context in agents.calls:
            with self.subTest(stage=stage):
                self.assertLess(transport_size(context), self.config["max_context_bytes"])
                self.assertNotIn(SOURCE, json.dumps(context))
                if stage == "plan_qc":
                    self.assertEqual(context["files"]["docs/guide.md"]["size"], len(SOURCE))
                    self.assertIn("sha", context["files"]["docs/guide.md"])
                if stage == "review":
                    self.assertEqual([v["path"] for v in context["changes"]], ["docs/guide.md"])
                    self.assertEqual(
                        context["changes"][0]["head_sha256"],
                        sha256(self.gh.trees[self.gh.commits[context["head"]]["tree"]["sha"]]["docs/guide.md"]),
                    )
        self.assertTrue(all(v["status"] == "fixed" for v in controller.state["findings"]))

    def test_large_diffs_shrink_previews_without_omitting_files_or_findings(self):
        controller = self.run_to(FakeAgents(), "gates")
        controller.config["max_context_bytes"] = 14000
        before = {f"docs/{i}.md": "old\n" for i in range(4)}
        after = {p: "".join(f"new line {j}\n" for j in range(6000)) for p in before}
        context = controller.pr_review_context("claude", before, after, "e" * 40)
        self.assertLessEqual(transport_size(context), 14000 - DISPATCH_ALLOWANCE)
        self.assertEqual([v["path"] for v in context["changes"]], sorted(after))
        self.assertTrue(all(v["truncated"] for v in context["changes"]))
        self.assertTrue(all(v["diff"].count("[diff truncated") == 1 for v in context["changes"]))
        self.assertEqual([v["head_sha256"] for v in context["changes"]], [sha256(after[p]) for p in sorted(after)])
        self.assertIsNone(controller.oversized(None, before, after))
        # Real review evidence must not be dropped to fit a preview.
        controller.state.setdefault("findings", []).append(
            {
                "id": "F99",
                "provider": "claude",
                "path": None,
                "line": None,
                "start_line": None,
                "detail": "Evidence " * 3000,
                "status": "open",
                "head": "e" * 40,
                "review": 1,
                "text_digest": "x",
            }
        )
        self.assertIsNone(controller.oversized(None, before, after))
        from feedback import restore

        context = controller.pr_review_context("claude", before, after, "e" * 40)
        self.assertEqual(restore(context)["own_findings"][-1]["detail"], "Evidence " * 3000)
        self.assertIn("controller_feedback", context)

    def test_truncation_marker_is_not_duplicated_when_an_existing_preview_shrinks(self):
        """Both per-file and aggregate shortening use one replaceable marker."""
        [change] = changes({"docs/guide.md": "old\n"}, {"docs/guide.md": "".join(f"new {i}\n" for i in range(12000))})
        preview = change["diff"]
        self.assertTrue(change["truncated"])
        self.assertEqual(preview.count(DIFF_TRUNCATED), 1)
        # A limit landing inside the previous marker must not preserve a partial marker either.
        preview = diff_preview(preview, MAX_DIFF + 10)
        self.assertEqual(preview.count("[diff truncated"), 1)
        for limit in (16000, 1000, 0):
            preview = diff_preview(preview, limit)
            self.assertEqual(preview.count("[diff truncated"), 1)
            self.assertTrue(preview.endswith(DIFF_TRUNCATED))

    def test_reopened_review_manifest_identifies_both_base_and_published_files(self):
        agents = LargeAgents()
        self.gh.trees[self.gh.base]["docs/guide.md"] = SOURCE
        controller = self.run_to(agents, "gates")
        controller.state["stage"] = "plan_qc"
        controller.state["design_votes"] = {}
        controller.quality_control("plan_qc")
        context = agents.calls[-1][2]
        self.assertEqual(context["files"]["docs/guide.md"]["size"], len(SOURCE))
        self.assertEqual(context["published_files"]["docs/guide.md"]["size"], len(SOURCE + "Correction 2\n"))
        self.assertNotEqual(
            context["files"]["docs/guide.md"]["sha"], context["published_files"]["docs/guide.md"]["sha"]
        )
        self.assertEqual(context["repository"]["refs"][controller.state["branch"]], controller.state["head"])
        self.assertNotIn(SOURCE, json.dumps(context))
        self.assertIsNone(controller.file_manifest(controller.state["base"], ["docs/new.md"])["docs/new.md"])


class WorkspaceTests(Workspace, unittest.TestCase):
    """Real Git snapshots and extraction retain large text byte-for-byte."""

    def test_large_snapshot_extraction_paging_and_deletion(self):
        root = self.prepare(candidate={"files": {"docs/guide.md": SOURCE}})
        work = root / "work"
        repository = Repository(work)
        page = repository.read_file("docs/guide.md", offset=199999, limit=2)
        self.assertEqual([v["line"] for v in page["lines"]], [200000, 200001])
        self.assertIsNone(page["next_offset"])
        (work / "docs/guide.md").write_text(SOURCE + "changed\n")
        extracted = turns.extract(work, root / "pristine.git", root)
        self.assertEqual(extracted["edits"], [{"path": "docs/guide.md", "content": SOURCE + "changed\n"}])
        proposal = {"summary": "Change one line.", "edits": extracted["edits"]}
        self.assertEqual(validate_edits(proposal, PLAN, {"docs/guide.md": SOURCE}, CONFIG), extracted["edits"])
        (work / "docs/guide.md").unlink()
        self.assertEqual(turns.extract(work, root / "pristine.git", root)["removed"], ["docs/guide.md"])
        proposal["edits"] = [{"path": "docs/guide.md", "content": None}]
        validate_edits(proposal, PLAN, {"docs/guide.md": SOURCE}, CONFIG)
        proposal["edits"][0]["content"] = SOURCE + "\x00binary"
        with self.assertRaisesRegex(Blocked, "invalid text replacement"):
            validate_edits(proposal, PLAN, {"docs/guide.md": SOURCE}, CONFIG)


if __name__ == "__main__":
    unittest.main()
