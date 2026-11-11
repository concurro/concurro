"""Actions labels and receipts describe actual task execution without exposing payloads."""

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import run_display
import runner
import turns
from controller import Controller
from relay import ExchangeError
from runner_fixtures import TASK, TURN, ExecutorFixture, FakeRunner

REVIEW = {
    **TURN["request"],
    "stage": "review",
    "model": "claude-opus-4-5",
    "effort": "high",
    "agent": {"profile": "Opus-high"},
    "context": {"pull_request": 25, "head": "c" * 40, "base": "a" * 40},
}


class DisplayTests(ExecutorFixture, unittest.TestCase):
    """Metadata follows each turn, including replacements and independent reviewers."""

    def test_dispatch_uses_pr_and_resolved_model_not_harness(self):
        """A replacement gets the same labels, while another reviewer gets its own model."""
        dispatch = Mock()
        executor, _, _ = self.runner([{"dispatch": "first"}, {"dispatch": "other"}], dispatch=dispatch)
        first = executor.submit({**TURN, "request": REVIEW})
        executor.receive(first, {"dispatch": "replacement"})
        executor.submit({**TURN, "request": {**REVIEW, "model": "claude-other", "agent": {"profile": "Other"}}})
        expected = {
            "operation-label": "PR review",
            "task-label": "up/project#25",
            "agent-label": "Opus-high (claude-opus-4-5 · high effort)",
        }
        self.assertEqual(dispatch.call_args_list[0].args, ("first", expected))
        self.assertEqual(dispatch.call_args_list[1].args, ("replacement", expected))
        self.assertEqual(dispatch.call_args_list[2].args[1]["agent-label"], "Other (claude-other · high effort)")

    def test_issue_legacy_effort_and_review_phase_labels(self):
        """Tasks without PRs retain the issue reference and the actual legacy effort default."""
        for stage, operation in run_display.OPERATIONS.items():
            labels = run_display.labels({**TURN["request"], "stage": stage}, {**TASK, "repository": "fork/project"})
            self.assertEqual(labels["task-label"], "fork/project#7")
            self.assertEqual(labels["operation-label"], operation)
            self.assertEqual(labels["agent-label"], "claude-test · medium effort")
        request = {**REVIEW, "stage": "plan_qc", "context": {"review_phase": "fresh"}}
        self.assertEqual(run_display.labels(request, TASK)["operation-label"], "Design review (fresh)")
        self.assertEqual(
            run_display.labels({**request, "evaluation": "fixture"}, TASK)["operation-label"],
            "Design review evaluation",
        )

    def test_controller_attaches_published_pr_before_preparing_turn(self):
        """The PR number comes from controller state, not the issue requirements."""
        controller = object.__new__(Controller)
        controller.state = {"pr": 25, "base": "a" * 40, "head": "c" * 40}
        controller.config = {"max_context_bytes": 10000}
        controller.require_current_bundle = Mock()
        controller.repository_refs = Mock(return_value={})
        context = controller.prepare_context("claude", "review", {"pull_request": 999})
        self.assertEqual(context["pull_request"], 25)
        self.assertEqual(context["repository_commit"], "c" * 40)
        design = controller.prepare_context("claude", "plan_qc", {})
        self.assertNotIn("pull_request", design)


class SummaryTests(unittest.TestCase):
    """Summary output distinguishes execution, delivery and model decisions."""

    def setUp(self):
        """Isolate runner receipts and the GitHub summary environment file."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "summary.md"
        environment = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.output), "JOB_STATUS": "success"})
        environment.start()
        self.addCleanup(environment.stop)

    def render(self):
        """Render the observed turn receipts once and return their Markdown."""
        run_display.summary(self.root)
        return self.output.read_text()

    def test_successful_review_links_task_and_pr_and_omits_raw_payloads(self):
        """A real runner loop records only selected metadata from a completed result."""
        task = {**TASK, "repository": "fork/project"}
        client = FakeRunner([])
        loop = runner.Loop(client, "private-key", object(), self.root, task=task)
        result = {
            "outcome": "ok",
            "response": {
                "verdict": "changes_requested",
                "summary": "private-text",
                "findings": [{"body": "private-finding"}],
            },
            "retained": "private-transcript",
        }
        with patch.object(turns, "execute", return_value=result), patch.object(runner, "log"):
            loop.run({"id": "turn", "payload": {**TURN, "request": REVIEW}})
        rendered = self.render()
        self.assertIn("https://github.com/fork/project/issues/7", rendered)
        self.assertIn("https://github.com/up/project/pull/25", rendered)
        self.assertIn("c" * 40, rendered)
        self.assertIn("| Execution | ok |", rendered)
        self.assertIn("| Result delivery | Acknowledged |", rendered)
        self.assertIn(r"changes\_requested", rendered)
        self.assertIn("| Findings | 1 |", rendered)
        self.assertNotIn("private-", rendered)
        self.assertEqual(client.completed, [("turn", result)])

    def test_failure_and_unconfirmed_delivery_do_not_appear_as_success(self):
        """Failed execution and failed acknowledgement remain distinct observations."""
        for outcome in ("failed", "timed_out", "terminated", "uncertain"):
            record = run_display.start(self.root, outcome, REVIEW, TASK)
            run_display.finish(
                self.root, outcome, record, {"outcome": outcome, "response": {"verdict": "approve"}}, 3, False
            )
        client = FakeRunner([])
        client.complete = Mock(side_effect=ExchangeError(410, "private-error"))
        loop = runner.Loop(client, "key", object(), self.root, task=TASK)
        with (
            patch.object(turns, "execute", return_value={"outcome": "ok", "response": {}}),
            patch.object(runner, "log"),
        ):
            loop.run({"id": "unconfirmed", "payload": {**TURN, "request": REVIEW}})
        rendered = self.render()
        self.assertNotIn("Acknowledged", rendered)
        self.assertNotIn("approve", rendered)
        self.assertEqual(rendered.count("| Result delivery | Not confirmed |"), 5)
        self.assertIn("| Execution | ok |", rendered)

    def test_interrupted_turn_and_no_turn_setup_failure(self):
        """Missing completion and missing execution are visible even without a grant."""
        record = run_display.start(self.root, "interrupted", REVIEW, TASK)
        self.assertEqual(record["outcome"], "No result recorded")
        self.assertIn("No result recorded", self.render())
        self.output.unlink()
        (self.root / "run-summary.json").unlink()
        with patch.dict(os.environ, {"JOB_STATUS": "failure", "RUN_TASK": "up/project#25"}):
            rendered = self.render()
        self.assertIn("Job status: **failure**", rendered)
        self.assertIn("No agent turn was recorded", rendered)

    def test_markdown_and_log_labels_escape_multiline_metadata(self):
        """A model identifier cannot create Markdown links or extra workflow commands."""
        request = deepcopy(REVIEW)
        request["model"] = "model|[link](https://invalid)\n::error::oops <script>"
        record = run_display.start(self.root, "escaped", request, TASK)
        self.assertNotIn("\n", record["agent-label"])
        rendered = self.render()
        self.assertIn(r"model\|\[link\]\(https://invalid\)", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("\n::error::", rendered)

    def test_summary_command_works_after_session_cleanup(self):
        """The final workflow step does not need the deleted grant or a live exchange."""
        root = self.root / "session"
        run_display.start(root / "turns", "t", REVIEW, TASK)
        args = ["runner", "summary", "--session", "s", "--gateway", "https://unused", "--root", str(root)]
        with patch("sys.argv", args), patch.object(runner, "load_grant", side_effect=AssertionError("grant read")):
            runner.main()
        self.assertIn("PR review", self.output.read_text())
        self.assertFalse((root / "grant.json").exists())

    def test_receipt_write_failure_does_not_prevent_delivery_or_cleanup(self):
        """Presentation failures leave the turn's existing completion semantics intact."""
        client = FakeRunner([])
        loop = runner.Loop(client, "key", object(), self.root, task=TASK)
        result = {"outcome": "ok", "response": {}}
        with (
            patch.object(turns, "execute", return_value=result),
            patch.object(turns, "remove") as remove,
            patch.object(Path, "write_text", side_effect=OSError("private-error")),
            patch.object(runner, "log"),
            patch("builtins.print"),
        ):
            loop.run({"id": "turn", "payload": {**TURN, "request": REVIEW}})
        self.assertEqual(client.completed, [("turn", result)])
        remove.assert_called_once()

    def test_damaged_receipts_do_not_fail_summary_execution_delivery_or_cleanup(self):
        """Corruption before or during a turn cannot change its execution semantics."""
        path = self.root / "run-summary.json"
        cases = (b"{", b"[]", b"null", b"42", b'{"old": {}}', b'{"old": []}', b"\xff")
        for data in cases:
            for during in (False, True):
                with self.subTest(data=data, during=during):
                    path.unlink(missing_ok=True)
                    self.output.unlink(missing_ok=True)
                    if not during:
                        path.write_bytes(data)
                    client = FakeRunner([])
                    loop = runner.Loop(client, "key", object(), self.root, task=TASK)
                    result = {"outcome": "ok", "response": {}}

                    def execute(*args):
                        """Simulate an agent damaging the receipt while completing its turn."""
                        if during:
                            path.write_bytes(data)
                        return result

                    with (
                        patch.object(turns, "execute", side_effect=execute),
                        patch.object(turns, "remove", wraps=turns.remove) as remove,
                        patch.object(runner, "log"),
                        patch("builtins.print"),
                    ):
                        loop.run({"id": "turn", "payload": {**TURN, "request": REVIEW}})
                    self.assertEqual(client.completed, [("turn", result)])
                    remove.assert_called_once()
                    rendered = self.render()
                    self.assertIn("Turn summary unavailable", rendered)
                    self.assertNotIn("| Execution | ok |", rendered)

    def test_invalid_receipt_fields_are_reported_as_unavailable(self):
        """Partially valid records cannot create a misleading result summary."""
        record = run_display.start(self.root, "turn", REVIEW, TASK)
        for fields in ({"agent-label": []}, {"acknowledged": "yes"}, {"pr": []}, {"findings": True}, {"elapsed": {}}):
            with self.subTest(fields=fields):
                self.output.unlink(missing_ok=True)
                (self.root / "run-summary.json").write_text(json.dumps({"turn": {**record, **fields}}))
                self.assertIn("Turn summary unavailable", self.render())

    def test_receipt_read_and_summary_write_errors_are_nonfatal(self):
        """Unreadable receipts get a notice; an unavailable output gets a log diagnostic."""
        with patch.object(Path, "read_text", side_effect=OSError("private-error")):
            run_display.summary(self.root)
        self.assertIn("Turn summary unavailable", self.output.read_text())
        with patch("builtins.open", side_effect=OSError("private-error")), patch("builtins.print") as diagnostic:
            run_display.summary(self.root)
        diagnostic.assert_called_once_with("Concurro result summary could not be written.", flush=True)

    def test_only_plain_github_issue_and_pr_links_are_rendered(self):
        """Links with foreign hosts, Markdown syntax or mismatched resource types are omitted."""
        record = run_display.start(self.root, "turn", REVIEW, TASK)
        invalid = (
            "https://example.test/o/r/issues/1",
            "https://github.com.evil.test/o/r/issues/1",
            "https://github.com@evil.test/o/r/issues/1",
            "javascript:alert(1)",
            "https://github.com/o/r/issues/1)\n![injected](https://example.test/image)",
            "https://github.com/o/r/issues/1?redirect=elsewhere",
            "https://github.com/o/r/issues/1#fragment",
            "https://github.com/o/r/issues/1\n",
        )
        for url in invalid:
            with self.subTest(url=url):
                self.output.unlink(missing_ok=True)
                run_display.save(self.root, "turn", {**record, "task": url, "pr": url})
                rendered = self.render()
                self.assertNotIn("| Task |", rendered)
                self.assertNotIn("| PR |", rendered)
                self.assertNotIn("injected", rendered)
        for task, pr in (
            ("https://github.com/o/.github/issues/1", "https://github.com/o/.github/pull/2"),
            ("https://github.com/o/r/pull/1", "https://github.com/o/r/issues/2"),
        ):
            self.output.unlink(missing_ok=True)
            run_display.save(self.root, "turn", {**record, "task": task, "pr": pr})
            rendered = self.render()
            self.assertEqual("| Task |" in rendered, "/issues/" in task)
            self.assertEqual("| PR |" in rendered, "/pull/" in pr)


if __name__ == "__main__":
    unittest.main()
