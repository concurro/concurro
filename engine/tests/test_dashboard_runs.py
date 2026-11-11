"""Explicit invalidations reset the presentation while retaining evidence and budgets."""

import unittest
from copy import deepcopy

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import dashboard_runs
from fakes import ClassifiedAgents, FakeGitHub
from legacy_publications import legacy_events
from pipeline_fixtures import PipelineFixture
from policy import approval_binding


class DashboardRunTests(PipelineFixture, unittest.TestCase):
    """Exercise the real invalidation command, persisted boundaries and classified reports."""

    def setUp(self):
        """Start a classified autonomous task with deterministic model responses."""
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = True
        self.gh, self.agents = FakeGitHub(self.config), ClassifiedAgents()
        self.gh.comments = []

    def reach(self, stage):
        """Keep this dashboard fixture's 25-tick setup bound."""
        for _ in range(25):
            controller = self.tick()
            if controller.state["stage"] == stage:
                return controller
        self.fail("stage not reached: " + stage)

    def invalidate(self):
        """Submit the real writer command and stop before reassessment."""
        self.gh.comment("/concurro invalidate")
        return self.tick()

    def test_invalidation_hides_old_progress_and_ratings_but_keeps_evidence(self):
        """Same-bundle invalidation hides the old PR review, approvals and classification."""
        previous = deepcopy(self.reach("gates").state)
        controller = self.invalidate()
        state = controller.state
        for key in ("calls", "round", "plan_revisions", "design_reopens", "pr", "head"):
            self.assertEqual(state[key], previous[key], key)
        self.assertEqual(state["reports"], previous["reports"])
        self.assertNotIn("classification_snapshot", state)
        self.assertFalse(dashboard_runs.current_reports(state))
        summary = self.load().summary_body()
        main, history = summary.split("<details><summary>Diagnostics</summary>", 1)
        self.assertIn("**Estimate:** Not assessed yet.", main)
        self.assertIn("| Design review | Pending |", main)
        self.assertIn("| Code review | Pending |", main)
        self.assertNotIn("↳ Round", main)
        self.assertNotIn("[Implementation plan]", summary)
        self.assertIn("<summary>Previous runs</summary>", history)
        self.assertIn("Run 1 — invalidated", history)
        self.assertIn("Writer requested reassessment", history)
        for report in previous["reports"]:
            self.assertNotIn("/artifacts/" + str(report["artifact"]) + ")", main)
            self.assertIn("/artifacts/" + str(report["artifact"]) + ")", history)
        self.assertIn("previous runs", controller.summary_lines(compact=True))

    def test_new_run_starts_at_one_and_keeps_ordinary_repair_rounds(self):
        """Only explicit invalidation hides history; normal design revisions remain visible."""
        self.reach("implement")
        self.invalidate()
        controller = self.reach("implement")
        main = controller.summary_body().split("<details><summary>Diagnostics</summary>")[0]
        self.assertIn("Round 1 · iterative", main)
        self.assertIn("↳ Round 1 iterative", main)
        self.assertNotIn("↳ Round 2", main)
        self.assertNotIn("Not assessed yet", main)
        self.assertNotIn("retains earlier intake", main)
        binding = approval_binding(controller.state, self.config)
        controller.summary_body()
        self.assertEqual(approval_binding(controller.state, self.config), binding)
        controller.invalidate_design("A genuine design revision.")
        controller.state["stage"] = "plan_qc"
        summary = controller.summary_body().split("<details><summary>Diagnostics</summary>")[0]
        self.assertIn("Round 2 · iterative", summary)
        self.assertIn("↳ Round 1 iterative", summary)
        self.assertIn("↳ Round 2 iterative", summary)
        self.assertEqual(len(controller.state["invalidations"]), 1)

    @legacy_events
    def test_legacy_invalidation_recovers_boundaries_and_excludes_stale_snapshot(self):
        """The already-running pilot needs no new invalidate to fix its presentation."""
        previous = deepcopy(self.reach("implement").state)
        self.invalidate()
        controller = self.reach("implement")
        controller.state["invalidations"][0].pop("display_start")
        controller.state["classification_snapshot"] = previous["classification_snapshot"]
        expected = [r for r in controller.state["reports"] if r not in previous["reports"]]
        self.assertEqual(dashboard_runs.current_reports(controller.state), expected)
        self.assertNotIn("classification_snapshot", dashboard_runs.assessment_state(controller.state))
        controller.save()
        loaded = self.load()
        self.assertIn("display_start", loaded.state["invalidations"][0])
        self.assertEqual(dashboard_runs.current_reports(loaded.state), expected)
        loaded.state["reports"] = expected[-1:]
        self.assertEqual(dashboard_runs.current_reports(loaded.state), expected[-1:])
        main = loaded.summary_body().split("<details><summary>Diagnostics</summary>")[0]
        self.assertIn("Round 1 · iterative", main)
        self.assertNotIn("retains earlier", main)

    @legacy_events
    def test_multiple_invalidations_keep_separate_runs_and_correction_links(self):
        """Repeated invalidations don't relabel old corrections as current ones."""
        self.reach("implement")
        self.invalidate()
        controller = self.reach("implement")
        report = controller.state["reports"][-1]
        report["diagnostic"] = {"detail": "Old formatting correction.", "status": "Recovered", "attempt": 1, "limit": 4}
        controller.save()
        self.invalidate()
        summary = self.load().summary_body()
        main, diagnostics = summary.split("<details><summary>Diagnostics</summary>", 1)
        self.assertNotIn("Old formatting correction", main)
        self.assertNotIn("↳ Round", main)
        self.assertIn("Run 1 — invalidated", diagnostics)
        self.assertIn("Run 2 — invalidated", diagnostics)
        self.assertIn("Old formatting correction", diagnostics)
        self.assertEqual(diagnostics.count("/artifacts/" + str(report["artifact"]) + ")"), 1)

    def test_offsets_do_not_reset_durable_budgets(self):
        """PR review and plan counters reset only in display labels."""
        controller = self.reach("gates")
        controller.state.update(round=4, plan_revisions=3)
        controller.save()
        controller = self.invalidate()
        boundary = dashboard_runs.boundaries(controller.state)[-1]
        self.assertEqual(dashboard_runs.round_label("Code review", "5", boundary), "1")
        controller.state["stage"] = "plan"
        self.assertEqual(controller.step_row(1, "Plan", 2)[1], "✅ Version 1")
        self.assertEqual(controller.state["round"], 4)
        self.assertEqual(controller.state["plan_revisions"], 3)

    def test_unfamiliar_legacy_round_labels_do_not_block_loading_or_work(self):
        """Presentation migration tolerates old labels and preserves their archived links."""
        self.reach("implement")
        controller = self.invalidate()
        saved = deepcopy(controller.state)
        for step in ("Intake", "Design review", "Code review"):
            for label in ("", "legacy round", "²", None):
                with self.subTest(step=step, label=label):
                    controller = self.load()
                    controller.state = deepcopy(saved)
                    controller.state["invalidations"][0].pop("display_start")
                    report = controller.state["reports"][0]
                    report.update(step=step, round=label)
                    controller.save()
                    loaded = self.load()
                    self.assertEqual(loaded.state["calls"], saved["calls"])
                    self.assertIn("/artifacts/" + str(report["artifact"]) + ")", loaded.previous_runs())
                    self.assertEqual(dashboard_runs.round_label(step, label, dashboard_runs.EMPTY), str(label))
                    self.assertEqual(self.tick().state["stage"], "plan")
