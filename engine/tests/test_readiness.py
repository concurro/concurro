"""Draft-to-ready is a bounded observation, never merge or CI execution."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule, tearDownModule
from test_pipeline import FakeAgents, FakeGitHub
from controller import Controller
from readiness import ci_reasons


class CIEvidenceTests(unittest.TestCase):
    """Latest results across all contexts and apps must pass, with at least one success."""

    def test_nonpassing_and_empty_results(self):
        for conclusion in (None, "failure", "cancelled", "timed_out", "action_required", "stale"):
            check = {"id": 1, "name": "test", "head_sha": "a", "status": "completed", "conclusion": conclusion}
            self.assertTrue(ci_reasons([("a", [check], [])]))
        self.assertTrue(ci_reasons([]))
        for conclusion in ("neutral", "skipped"):
            self.assertTrue(ci_reasons([("a", [{"name": "test", "head_sha": "a", "status": "completed",
                                                  "conclusion": conclusion}], [])]))

    def test_latest_results_and_status_only(self):
        failed = {"id": 1, "name": "test", "head_sha": "a", "status": "completed", "conclusion": "failure"}
        success = {**failed, "id": 2, "conclusion": "success"}
        skipped = {**success, "name": "optional", "conclusion": "skipped"}
        self.assertEqual(ci_reasons([("a", [success, failed, skipped], [])]), [])
        statuses = [{"id": 1, "context": "external", "state": "failure"},
                    {"id": 2, "context": "external", "state": "success"}]
        self.assertEqual(ci_reasons([("a", [], statuses)]), [])
        self.assertTrue(ci_reasons([("b", [success], [])]))
        self.assertTrue(ci_reasons([("a", [success], []), ("b", [failed], [])]))

    def test_empty_external_suite_does_not_block_or_supply_success(self):
        suite = {"id": 10, "app": {"slug": "external-checks"}, "head_sha": "a", "status": "queued",
                 "conclusion": None, "latest_check_runs_count": 0}
        passing = {"id": 1, "context": "external", "state": "success"}
        self.assertEqual(ci_reasons([("a", [], [passing], [suite], [])]), [])
        self.assertEqual(ci_reasons([("a", [], [], [suite], [])]), ["CI has no successful result yet"])

    def test_actions_nonempty_failed_and_unknown_suites_still_block(self):
        suite = {"id": 10, "app": {"slug": "external-checks"}, "head_sha": "a", "status": "queued",
                 "conclusion": None, "latest_check_runs_count": 0}
        passing = [{"id": 1, "context": "external", "state": "success"}]
        variants = [{**suite, "app": app} for app in (None, {}, {"slug": ""}, {"slug": "github-actions"})]
        variants += [{**suite, "latest_check_runs_count": count} for count in (None, False, 0.0, 1)]
        variants += [{k: v for k, v in suite.items() if k != missing}
                     for missing in ("latest_check_runs_count", "conclusion", "id", "head_sha")]
        variants += [{**suite, "status": "in_progress"}, {**suite, "head_sha": "old"},
                     {**suite, "conclusion": "failure"},
                     {**suite, "status": "completed", "conclusion": "action_required"}]
        for variant in variants:
            with self.subTest(suite=variant):
                self.assertTrue(any(reason.startswith("CI suite ")
                                    for reason in ci_reasons([("a", [], passing, [variant], [])])))

    def test_fetched_run_prevents_empty_external_suite_exception(self):
        suite = {"id": 10, "app": {"slug": "external-checks"}, "head_sha": "a", "status": "queued",
                 "conclusion": None, "latest_check_runs_count": 0}
        passing = [{"id": 1, "context": "external", "state": "success"}]
        for status, conclusion in (("in_progress", None), ("completed", "failure"), ("completed", "success")):
            check = {"id": 20, "check_suite": {"id": 10}, "name": "test", "head_sha": "a",
                     "status": status, "conclusion": conclusion}
            with self.subTest(status=status, conclusion=conclusion):
                reasons = ci_reasons([("a", [check], passing, [suite], [])])
                self.assertIn("CI suite pending: 10", reasons)
                if conclusion == "failure":
                    self.assertIn("CI unsuccessful: test", reasons)
                elif status == "in_progress":
                    self.assertIn("CI pending: test", reasons)


class ReadinessPipelineTests(unittest.TestCase):
    """Real controller lifecycle with deterministic agent/provider substitutes."""

    def setUp(self):
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()

    def tick(self):
        controller = Controller(self.gh, self.agents, self.config)
        controller.advance(7)
        return controller

    def reviewed(self):
        for _ in range(30):
            controller = self.tick()
            if controller.state["stage"] == "planned":
                self.gh.comment("/concurro approve " + controller.state["plan_id"])
            if controller.state["stage"] == "gates":
                return controller
        self.fail("review never completed")

    def test_ready_once_then_ci_regression_and_manual_draft(self):
        self.reviewed()
        calls = len(self.agents.calls)
        self.assertTrue(self.gh.pr["draft"])
        self.tick()
        self.assertFalse(self.gh.pr["draft"])
        self.assertEqual(self.gh.ready_calls, 1)
        self.gh.checks_green = False
        state = self.tick().state
        self.assertFalse(state["handoff_ready"])
        self.assertFalse(self.gh.pr["draft"])
        self.gh.checks_green = True
        self.tick()
        self.gh.pr["draft"] = True
        state = self.tick().state
        self.assertIn("human must mark", state["reason"])
        self.assertTrue(self.gh.pr["draft"])
        self.assertEqual(self.gh.ready_calls, 1)
        self.assertEqual(self.gh.merges, 0)
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(sum("### Ready for review" in c["body"] for c in self.gh.comments), 1)

    def test_waiting_workflow_blocks_a_passing_external_status(self):
        controller = self.reviewed()
        original = self.gh.pages
        def pages(path, key=None):
            if '/check-runs' in path:
                return []
            if path.endswith('/statuses'):
                return [{"id": 1, "context": "CLA", "state": "success"}]
            if path.startswith('actions/runs?'):
                return [{"id": 10, "workflow_id": 1, "head_sha": controller.state["head"],
                         "name": "CI", "status": "waiting", "conclusion": None}]
            return original(path, key)
        with patch.object(self.gh, 'pages', side_effect=pages):
            controller.gates()
        self.assertTrue(self.gh.pr['draft'])
        self.assertIn('CI workflow pending', controller.state['reason'])

    def test_empty_external_suite_allows_ready_but_empty_actions_suite_does_not(self):
        controller = self.reviewed()
        suite = {"id": 10, "app": {"slug": "github-actions"}, "head_sha": controller.state["head"],
                 "status": "queued", "conclusion": None, "latest_check_runs_count": 0}
        original = self.gh.pages
        def pages(path, key=None):
            return [deepcopy(suite)] if path.endswith('/check-suites') else original(path, key)
        with patch.object(self.gh, 'pages', side_effect=pages):
            controller.gates()
            self.assertTrue(self.gh.pr['draft'])
            suite['app']['slug'] = 'external-checks'
            controller.gates()
        self.assertFalse(self.gh.pr['draft'])
        self.assertEqual(self.gh.ready_calls, 1)

    def test_lost_ready_response_recovers_without_another_write(self):
        self.reviewed()
        self.gh.fail_after_ready = True
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertTrue(self.tick().state["handoff_ready"])
        self.assertEqual(self.gh.ready_calls, 1)

    def test_new_or_incomplete_reviews_and_head_changes_keep_draft(self):
        controller = self.reviewed()
        controller.state["reviews"][self.config["reviewers"][0]]["input_revision"] = -1
        controller.save()
        self.assertFalse(self.tick().state["handoff_ready"])
        self.assertTrue(self.gh.pr["draft"])
        self.assertFalse(hasattr(self.gh, "ready_calls"))

    def test_ci_recheck_detects_merge_commit_change(self):
        controller = self.reviewed()
        original = self.gh.pages
        def pages(path, key=None):
            result = original(path, key)
            if "/check-runs" in path:
                self.gh.pr["merge_commit_sha"] = "b" * 40
            return result
        with patch.object(self.gh, "pages", side_effect=pages):
            controller.gates()
        self.assertTrue(self.gh.pr["draft"])
        self.assertIn("changed while observing", controller.state["reason"])

    def test_stale_test_merge_cannot_supply_green_ci(self):
        """An old test-merge SHA is insufficient even if GitHub temporarily returns it on the PR."""
        controller = self.reviewed()
        self.gh.pr["merge_commit_sha"] = "b" * 40
        self.gh.commits["b" * 40] = {"parents": [{"sha": self.gh.base}, {"sha": "c" * 40}]}
        controller.gates()
        self.assertTrue(self.gh.pr["draft"])
        self.assertIn("test-merge commit is not current", controller.state["reason"])
        self.assertFalse(hasattr(self.gh, "ready_calls"))

    def test_writer_feedback_and_retry_starts_one_bounded_repair(self):
        self.reviewed()
        self.gh.checks_green = False
        before = len(self.agents.calls)
        self.gh.comment("CI failed: fix the missing example.", "outsider")
        self.gh.comment("/concurro retry", "outsider")
        self.tick()
        self.assertEqual(len(self.agents.calls), before)
        self.gh.comment("/concurro retry")
        state = self.tick().state
        self.assertEqual((state["round"], state["stage"]), (0, "gates"))
        self.assertEqual(len(self.agents.calls), before)
        self.gh.comment("CI failed: add the missing example, within the existing scope.")
        self.gh.comment("/concurro retry")
        state = self.tick().state
        self.assertEqual((state["round"], state["stage"]), (1, "publish"))
        context = [c for _, stage, c in self.agents.calls if stage == "develop"][-1]
        self.assertIn("add the missing example", context["implementation_feedback"])
        self.assertNotIn("outsider", context["implementation_feedback"])
        for _ in range(5):
            state = self.tick().state
        self.assertEqual((state["round"], state["stage"]), (1, "gates"))
        self.assertTrue(self.gh.pr["draft"])
        calls = len(self.agents.calls)
        self.tick()
        self.assertEqual(len(self.agents.calls), calls)
        self.config["max_fix_rounds"] = state["round"]
        # Exercise the budget at the command boundary without changing durable config identity.
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        self.gh.comment("Another bounded change please.")
        command = self.gh.comment("/concurro retry")
        from policy import Blocked
        with self.assertRaisesRegex(Blocked, "allowance exhausted"):
            controller.commands(self.gh.comments)

    def test_closed_pr_stops_observation(self):
        self.reviewed()
        self.gh.pr["state"] = "closed"
        controller = self.tick()
        self.assertEqual(controller.outcome(False).kind, "terminal")
        self.assertFalse(hasattr(self.gh, "ready_calls"))
