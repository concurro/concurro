"""Chronological decisions survive revisions, lost responses and controller restarts."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule  # unittest module fixtures
import conversation
from controller import Controller
from policy import Blocked, approval_valid
from test_autonomy import decision
from test_pipeline import CONFIG, FakeAgents, FakeGitHub


class ConversationTests(unittest.TestCase):
    """Use real controller/checkpoint paths with deterministic model responses."""

    def setUp(self):
        """Keep merging disabled and start a new enrolled issue for each scenario."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def tick(self):
        """Reload authoritative GitHub state between every provider invocation."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.advance(7)
        return controller

    def events(self):
        """Select only append-only conversation entries, excluding the editable summary."""
        return [c for c in self.gh.comments if c["body"].startswith(conversation.MARKER)]

    def test_revision_conversation_sessions_and_fresh_gate(self):
        """Two revisions keep each model's session while fresh final votes authorize work."""
        self.agents.qc_reports = [decision("proceed"), decision("approve"),
            decision("revise", concerns=["Define the error behavior."]), decision("approve"),
            decision("revise", concerns=["Test the boundary."])]
        for _ in range(15):
            controller = self.tick()
            if controller.state["stage"] == "implement":
                break
        state = controller.state
        self.assertTrue(approval_valid(state, self.config))
        self.assertEqual(state["plan_revisions"], 2)
        self.assertEqual(state["calls"], 12)
        calls = self.agents.calls
        plans = [c for _, stage, c in calls if stage == "plan"]
        self.assertEqual([c["controller_session"]["turn"] for c in plans], [0, 1, 2])
        self.assertEqual(len({c["controller_session"]["scope"] for c in plans}), 1)
        fresh = [c for _, stage, c in calls if stage == "plan_qc" and c["review_phase"] == "fresh"]
        self.assertEqual(len(fresh), 2)
        for context in fresh:
            self.assertNotIn("controller_session", context)
            self.assertNotIn("own_objections", context)
            self.assertNotIn("responses", context["plan"])
            self.assertNotIn("design_feedback", context)
        coop = [(p, c) for p, stage, c in calls if stage == "plan_qc" and c["review_phase"] == "cooperative"]
        for provider in ("claude", "codex"):
            sessions = [c["controller_session"] for p, c in coop if p == provider]
            self.assertEqual([s["turn"] for s in sessions], [0, 1, 2])
            self.assertEqual(len({s["scope"] for s in sessions}), 1)
            self.assertNotEqual(sessions[0]["scope"], plans[0]["controller_session"]["scope"])
        self.assertEqual(self.events(), [], "progress posts no issue comments")
        archives = self.gh.archives()
        self.assertIn("Define the error behavior.", "\n".join(archives))
        self.assertEqual(sum(a.split("\n")[1].startswith("### ✅ Plan QC · ") for a in archives), 6)
        summary = self.gh.summary()["body"]
        self.assertIn("| Design review | ✅ 2 of 2 approve |", summary)
        self.assertIn("| Design review | 3 fresh | [✅ Approve]", summary)
        self.assertFalse(state["conversation"]["pending"])
        before = deepcopy(self.events())
        controller.save()
        self.assertEqual(self.events(), before)
        self.assertFalse(self.gh.refs)
        self.assertEqual(self.gh.merges, 0)

    def test_lost_event_response_reconciles_before_next_model_call(self):
        """An accepted archive with a lost response is found without duplicate publication."""
        original = self.gh.repo
        failed = False

        def lose_response(path, method="GET", data=None):
            """Disconnect once after the server stores the first archive."""
            nonlocal failed
            result = original(path, method, data)
            if not failed and method == "POST" and (data or {}).get("body", "").startswith(conversation.MARKER):
                failed = True
                raise RuntimeError("lost response")
            return result

        with patch.object(self.gh, "repo", side_effect=lose_response), self.assertRaises(conversation.PublicationError):
            self.tick()
        self.assertEqual(len(self.agents.calls), 1)
        first = deepcopy(self.gh.archives())
        self.assertEqual(len(first), 1)
        self.tick()
        self.assertEqual(self.gh.archives()[:1], first, "found by marker, not posted again")
        self.assertEqual(sum(a == first[0] for a in self.gh.archives()), 1)
        self.assertEqual(len(self.agents.calls), 2, "the next tick moves on to planning")

    def test_foreign_marker_does_not_acknowledge_event_and_modified_bot_event_blocks(self):
        """Only the actual App's exact publication can acknowledge pending intent."""
        state = {"issue": 7}
        conversation.queue(state, "Review", "Details")
        body = state["conversation"]["pending"][0]["body"]
        self.gh.comment(body, "outsider")
        saved = deepcopy(state)
        conversation.publish(state, self.gh, self.config)
        self.assertEqual(len(self.gh.comments), 2)
        self.gh.comments[-1]["body"] += " edited"
        with self.assertRaises(Blocked):
            conversation.publish(saved, self.gh, self.config)

    def test_fresh_dissent_reopens_instead_of_using_cooperative_approvals(self):
        """Final independent objections invalidate the exact plan and require revision."""
        self.agents.qc_reports = [decision("proceed"), decision("approve"), decision("approve"),
                                 decision("approve"), decision("revise", concerns=["Missed edge case."])]
        for _ in range(6):
            controller = self.tick()
        self.assertEqual(controller.state["stage"], "plan")
        self.assertNotIn("approval", controller.state)
        self.assertFalse(approval_valid(controller.state, self.config))
        self.assertFalse(self.gh.refs)

    def test_planner_cannot_omit_objection_responses(self):
        """Progress stops if a revised plan silently drops an unresolved finding."""
        self.agents.qc_reports = [decision("proceed"), decision("approve"),
                                 decision("revise", concerns=["Specify the boundary."])]
        for _ in range(4):
            self.tick()
        original = self.agents.ask

        def omit(provider, stage, context):
            """Return an otherwise valid plan with the required response removed."""
            response = original(provider, stage, context)
            response["responses"] = []
            return response

        with patch.object(self.agents, "ask", side_effect=omit), self.assertRaises(Blocked):
            self.tick()
