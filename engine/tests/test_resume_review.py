"""Resume preserves judgments, actionable review ownership and finished task state."""

import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import test_bundle_lifecycle as lifecycle
from fakes import FakeAgents, FakeGitHub, named_config
from github import Transient
from policy import design_roster


class ResumeReviewTests(unittest.TestCase):
    """Exercise real commands and persisted tasks with deterministic transports."""

    setUp = lifecycle.BundleLifecycleTests.setUp
    controller = lifecycle.BundleLifecycleTests.controller
    loaded = lifecycle.BundleLifecycleTests.loaded
    tick = lifecycle.BundleLifecycleTests.tick
    reach = lifecycle.BundleLifecycleTests.reach

    def resume(self, controller):
        """Apply one writer command without the next model turn."""
        self.gh.comment("/concurro resume")
        controller.commands(self.gh.comments)

    def named(self):
        """Use distinct named reviewers so profile removal keeps a valid two-model team."""
        self.config = named_config()
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def test_judgment_resume_reassesses_edited_issue_and_spends_revision(self):
        """Resume uses the same reassessment transition as retry, after adopting settings."""
        for stage in ("rejected", "escalated"):
            with self.subTest(stage=stage):
                self.setUp()
                controller = self.reach("implement")
                controller.suspend(stage, "Judgment needs reassessment")
                before = deepcopy(controller.state)
                self.gh.issue["body"] += "\nUpdated requirements answering the judgment."
                self.gh.comment("Please include this clarification.")
                new = {**deepcopy(self.config), "max_model_calls": self.config["max_model_calls"] + 5}
                controller.resume_target = lambda: (new, controller.bundle)
                self.resume(controller)
                self.assertEqual(controller.state["stage"], "intake")
                self.assertEqual(controller.state["plan_revisions"], before["plan_revisions"] + 1)
                self.assertEqual(controller.state["calls"], before["calls"])
                self.assertEqual(controller.state["requirements"], controller.requirements(self.gh.comments))
                self.assertEqual(controller.state["issue_digest"], controller.issue_digest())
                self.assertNotIn("approval", controller.state)
                self.assertEqual(controller.config, new)
                saved = deepcopy(controller.state)
                controller.commands(self.gh.comments)
                self.assertEqual(controller.state, saved)

    def test_exhausted_judgment_keeps_waiting_revision_and_spending(self):
        """Adoption neither bypasses exhaustion nor changes an already recorded revision target."""
        for waiting in (False, True):
            with self.subTest(waiting=waiting):
                self.setUp()
                controller = self.reach("implement")
                controller.state["plan_revisions"] = self.config["max_plan_revisions"]
                controller.suspend("escalated", "Reviewers require a revision")
                if waiting:
                    controller.state.update(retry_stage="plan", revision_wait={"stage": "plan"})
                calls = controller.state["calls"]
                new = {**deepcopy(self.config), "max_model_calls": self.config["max_model_calls"] + 5}
                controller.resume_target = lambda: (new, controller.bundle)
                self.resume(controller)
                self.assertEqual(controller.state["stage"], "escalated")
                self.assertEqual(controller.state["revision_wait"]["stage"], "plan" if waiting else "intake")
                self.assertEqual(controller.state["plan_revisions"], self.config["max_plan_revisions"])
                self.assertEqual(controller.state["calls"], calls)
                self.assertFalse(any("Task resumed" in c["body"] for c in self.gh.comments))
                self.assertIn("bundle_resume", controller.state)
                self.assertEqual(controller.config, new)
                notices = [c["body"] for c in self.gh.comments if "Settings adopted" in c["body"]]
                self.assertEqual(len(notices), 1)
                self.assertIn("current configuration and instructions were adopted", notices[0])
                self.assertIn("reassessment needs more plan revisions", notices[0])
                self.assertIn("/concurro retry --plan-revisions 1", notices[0])
                self.assertFalse(any("Plan revision command not applied" in c["body"] for c in self.gh.comments))
                controller.commands(self.gh.comments)
                self.assertEqual(len([c for c in self.gh.comments if "Settings adopted" in c["body"]]), 1)

    def test_output_correction_is_not_a_new_judgment(self):
        """A schema correction keeps its stage and does not spend a design revision."""
        controller = self.reach("plan_qc")
        controller.state["review_repair"] = {"attempts": 4, "signature": "old"}
        controller.suspend("escalated", "Output needs correction")
        revisions = controller.state["plan_revisions"]
        self.resume(controller)
        self.assertEqual(controller.state["stage"], "plan_qc")
        self.assertEqual(controller.state["plan_revisions"], revisions)

    def test_removed_pr_owner_refuses_once_without_adopting(self):
        """Both open and withdrawn findings require their owner; later commands remain reachable."""
        for status in ("open", "withdrawn"):
            with self.subTest(status=status):
                self.setUp()
                self.named()
                controller = self.reach("implement")
                controller.state["findings"] = [{"id": "F1", "provider": "sol-high", "status": status}]
                controller.suspend("blocked", "Original pause")
                before = deepcopy(controller.state)
                new = deepcopy(self.config)
                new["agents"]["pr_reviewers"] = ["opus-high", "astra-high"]
                controller.resume_target = lambda: (new, controller.bundle)
                controller.resume_config = Mock()
                self.resume(controller)
                for key in ("stage", "reason", "retry_stage", "findings", "calls", "approval", "sessions"):
                    self.assertEqual(controller.state.get(key), before.get(key), key)
                self.assertNotIn("bundle_resume", controller.state)
                self.assertEqual(controller.config, self.config)
                controller.resume_config.assert_not_called()
                notices = [c for c in self.gh.comments if "Resume not applied" in c["body"]]
                self.assertEqual(len(notices), 1)
                self.assertIn("F1", notices[0]["body"])
                self.assertIn("sol-high", notices[0]["body"])
                controller.commands(self.gh.comments)
                self.assertEqual(len([c for c in self.gh.comments if "Resume not applied" in c["body"]]), 1)
                self.gh.comment("/concurro stop")
                controller.commands(self.gh.comments)
                self.assertEqual(controller.state["stage"], "stopped")

    def test_design_objection_owner_cannot_be_removed_or_changed(self):
        """Design ownership includes model and effort, even when the profile name is unchanged."""
        for change in ("remove", "model", "effort"):
            with self.subTest(change=change):
                self.setUp()
                self.named()
                controller = self.reach("plan_qc")
                member = next(m for m in design_roster(self.config) if m["provider"] == "sol-high")
                controller.state["objections"] = [{"id": "O1", "reviewer": member["id"], "resolved": False}]
                controller.suspend("blocked", "Unresolved design objection")
                new = deepcopy(self.config)
                if change == "remove":
                    new["agents"]["plan_reviewers"] = ["opus-high", "astra-high"]
                else:
                    new["models"]["sol-high"][change] = "gpt-other" if change == "model" else "medium"
                controller.resume_target = lambda: (new, controller.bundle)
                self.resume(controller)
                self.assertNotIn("bundle_resume", controller.state)
                self.assertEqual(controller.state["stage"], "blocked")
                self.assertFalse(controller.state["objections"][0]["resolved"])
                self.assertTrue(any("O1" in c["body"] and "Resume not applied" in c["body"] for c in self.gh.comments))

    def test_closed_findings_resolved_objections_and_retained_pr_names_allow_resume(self):
        """Completed ownership imposes no restriction; a PR owner can change its model or effort."""
        for change in ("remove", "model", "effort"):
            with self.subTest(change=change):
                self.setUp()
                self.named()
                controller = self.reach("plan_qc")
                member = design_roster(self.config)[0]
                controller.state["objections"] = [{"id": "O1", "reviewer": member["id"], "resolved": True}]
                controller.state["findings"] = [
                    {"id": "F1", "provider": "sol-high", "status": "closed" if change == "remove" else "open"}
                ]
                new = deepcopy(self.config)
                if change == "remove":
                    new["agents"]["pr_reviewers"] = new["agents"]["plan_reviewers"] = ["opus-high", "astra-high"]
                else:
                    new["models"]["sol-high"][change] = "gpt-other" if change == "model" else "medium"
                controller.resume_target = lambda: (new, controller.bundle)
                self.resume(controller)
                self.assertIn("bundle_resume", controller.state)
                self.assertEqual(controller.config, new)

    def test_judgment_resume_lost_save_does_not_reassess_twice(self):
        """The command and revision debit become durable together."""
        controller = self.reach("implement")
        controller.suspend("rejected", "Requires revision")
        original = controller.state["plan_revisions"]
        save = controller.save

        def lost(*args, **kwargs):
            """Lose the save reply after the authoritative state is durable."""
            save(*args, **kwargs)
            raise Transient("lost reply")

        with patch.object(controller, "save", side_effect=lost), self.assertRaises(Transient):
            self.resume(controller)
        controller = self.loaded()
        controller.commands(self.gh.comments)
        self.assertEqual(controller.state["stage"], "intake")
        self.assertEqual(controller.state["plan_revisions"], original + 1)

    def test_finished_tasks_ignore_resume_in_tick_and_direct_handler(self):
        """Finished tasks neither fetch settings nor emit a misleading resumed event."""
        for stage in ("merged", "observed_merged", "closed"):
            with self.subTest(stage=stage):
                self.setUp()
                controller = self.reach("gates")
                controller.state["stage"] = stage
                self.gh.pr.update(state="closed", merged=stage != "closed")
                controller.save()
                before = deepcopy(controller.state)
                controller.resume_target = Mock(side_effect=AssertionError("must not fetch"))
                self.resume(controller)
                self.assertEqual(controller.state, before)
                self.tick()
                self.assertFalse(any("Task resumed" in c["body"] for c in self.gh.comments))
