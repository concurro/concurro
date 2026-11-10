"""Answers complete an unfinished planner turn without repeating accepted intake."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from controller import Controller
from fakes import DESIGN_ISSUE
from github import Transient
from pipeline_fixtures import ConsensusFixture
from policy import approval_valid, design_roster

QUESTION = "May this change include the OpenAPI schema file?"
ANSWER = "Yes, include the OpenAPI schema in this change; the configured edit policy permits it."


class PlannerClarificationTests(ConsensusFixture, unittest.TestCase):
    """Exercise question publication, answer receipts and real subsequent model calls."""

    def load(self):
        """Load a checkpoint without advancing it."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def ask_question(self, question=QUESTION):
        """Return one valid planner response containing an outstanding question."""
        original = self.agents.ask

        def ask(provider, stage, context):
            """Keep fake provider accounting and attach a question to the plan."""
            report = original(provider, stage, context)
            if stage == "plan":
                report["questions"] = [question]
            return report

        with patch.object(self.agents, "ask", side_effect=ask):
            state = self.tick()
        self.assertEqual(state["stage"], "clarify")
        return deepcopy(state)

    def waiting(self, manual=False):
        """Reach a planner question after real accepted intake, with all revisions already spent."""
        if manual:
            self.config["autonomous"] = False
            self.gh.comment("/concurro start")
        self.assertEqual(self.tick()["stage"], "plan")
        self.ask_question()
        controller = self.load()
        controller.state["plan_revisions"] = self.config["max_plan_revisions"]
        controller.save()
        return deepcopy(controller.state)

    def test_authorized_answer_continues_plan_with_spent_revision_budget(self):
        """Manual and autonomous answers retain intake and still require actual design approval."""
        for manual in (False, True):
            with self.subTest(manual=manual):
                self.setUp()
                before = self.waiting(manual)
                self.gh.issue["user"]["login"] = "reporter"
                self.gh.comment(ANSWER, "reporter")
                if manual:
                    self.assertEqual(self.tick()["stage"], "clarify")
                    self.gh.comment("/concurro start")
                count = len(self.agents.calls)
                state = self.tick()
                self.assertEqual(state["stage"], "plan_qc")
                self.assertEqual([s for _, s, _ in self.agents.calls[count:]], ["plan"])
                self.assertEqual(state["plan_revisions"], before["plan_revisions"])
                self.assertEqual(state["calls"], before["calls"] + 1)
                self.assertEqual(state["qc"]["intake"], before["qc"]["intake"])
                self.assertEqual(state["qc_history"], before["qc_history"])
                self.assertEqual(state["base"], before["base"])
                self.assertEqual(state["design_generation"], before["design_generation"])
                self.assertIn(ANSWER, str(self.agents.calls[-1][2]["issue"]))
                self.assertNotIn("approval", state)
                state = self.reach("planned" if manual else "implement")
                if manual:
                    self.gh.comment("/concurro approve " + state["plan_id"])
                    state = self.tick()
                self.assertTrue(approval_valid(state, self.config))
                self.assertEqual(sum(s == "intake" for _, s, _ in self.agents.calls), 1)

    def test_policy_resume_then_answer_keeps_objections_and_does_not_reassess(self):
        """Reproduce the policy adoption and planner answer sequence without waiving objections."""
        before = self.waiting()
        controller = self.load()
        controller.state["objections"] = [
            {
                "id": "O1",
                "reviewer": design_roster(self.config)[0]["id"],
                "resolved": False,
                "text": "Cover database errors",
            }
        ]
        controller.save()
        objections = deepcopy(controller.state["objections"])
        new = deepcopy(self.config)
        new["paths"] = {"protected_extensions": [], "protected_prefixes": [".concurro/"]}
        controller.resume_target = lambda: (new, controller.bundle)
        self.gh.comment("/concurro resume")
        controller.commands(self.gh.comments)
        self.config = new
        self.gh.comment(ANSWER)
        state = self.tick()
        self.assertEqual(state["stage"], "plan_qc")
        self.assertEqual(state["plan_revisions"], before["plan_revisions"])
        self.assertEqual(state["objections"], objections)
        self.assertEqual(self.agents.calls[-1][1], "plan")
        self.assertEqual(self.agents.calls[-1][2]["open_objections"], objections)
        self.assertNotIn("approval", state)

    def test_unanswered_or_unauthorized_question_still_waits(self):
        """Neither a command nor an unrelated reader answers an outstanding planner question."""
        before = self.waiting()
        self.gh.comment(ANSWER, "outsider")
        self.gh.comment("/concurro resume")
        self.tick()
        state = self.tick()
        self.assertEqual(state["stage"], "clarify")
        self.assertEqual(state["calls"], before["calls"])
        self.assertEqual(state["requests"][-1]["state"], "open")

    def test_issue_or_other_discussion_changes_still_reassess(self):
        """An answer cannot hide a changed issue, edited prior feedback or an extra requirement."""
        for change in ("title", "body", "old_reply", "additional_reply"):
            with self.subTest(change=change):
                self.setUp()
                self.gh.comment("Original guidance.")
                old_reply = self.gh.comments[-1]
                self.assertEqual(self.tick()["stage"], "plan")
                before = self.ask_question()
                self.gh.comment(ANSWER)
                if change in ("title", "body"):
                    self.gh.issue[change] += " Include another feature."
                elif change == "old_reply":
                    old_reply["body"] = "Include another feature."
                else:
                    self.gh.comment("Also include another feature.")
                state = self.tick()
                self.assertEqual(self.agents.calls[-1][1], "intake")
                self.assertEqual(state["plan_revisions"], before["plan_revisions"] + 1)

    def test_lost_save_after_continuation_does_not_repeat_intake_or_spend_revision(self):
        """The answered request and resumed stage survive a lost save response before dispatch."""
        before = self.waiting()
        self.gh.comment(ANSWER)
        save = Controller.save
        lost = []

        def fail(controller, *args, **kwargs):
            """Lose the reply only after the continuation checkpoint is saved."""
            save(controller, *args, **kwargs)
            if controller.state["stage"] == "plan" and not lost:
                lost.append(True)
                raise Transient("lost continuation save")

        with patch.object(Controller, "save", fail), self.assertRaises(Transient):
            self.tick()
        state = self.tick()
        self.assertEqual(state["stage"], "plan_qc")
        self.assertEqual(state["plan_revisions"], before["plan_revisions"])
        self.assertEqual(state["calls"], before["calls"] + 1)
        self.assertEqual(sum(s == "intake" for _, s, _ in self.agents.calls), 1)
        self.assertEqual(state["requests"][-1]["state"], "answered")

    def test_followup_question_waits_for_a_new_answer(self):
        """A previous answered request cannot answer the planner's next question."""
        before = self.waiting()
        self.gh.comment(ANSWER)
        state = self.ask_question("Which validation command should I use?")
        calls = state["calls"]
        self.assertEqual(self.tick()["stage"], "clarify")
        self.assertEqual(len(self.agents.calls), calls)
        self.gh.comment("Use the repository's focused tests.")
        state = self.tick()
        self.assertEqual(state["stage"], "plan_qc")
        self.assertEqual(state["plan_revisions"], before["plan_revisions"])
        self.assertEqual([r["state"] for r in state["requests"]], ["answered", "answered"])

    def test_published_parent_is_retained_during_clarification(self):
        """Continue an already reopened design without opening another generation or revision."""
        prior = self.reach("review")
        controller = self.load()
        controller.reopen_design(DESIGN_ISSUE, "pr_review:claude")
        before = self.ask_question()
        self.gh.comment(ANSWER)
        state = self.tick()
        self.assertEqual(state["stage"], "plan_qc")
        for key in ("plan_revisions", "design_reopens", "design_generation", "findings", "base"):
            self.assertEqual(state.get(key), before.get(key), key)
        self.assertEqual(state["head"], prior["head"])
        self.assertEqual(state["design_parent"], prior["head"])
