"""Publication outages and large reports preserve recoverable checkpoint authority."""

from copy import deepcopy
import io
import json
import os
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule  # unittest module fixtures
import controller as controller_module
import conversation
from checkpoint_health import MARKER as HEALTH_MARKER
from controller import Controller
from policy import approval_valid, digest
from test_pipeline import CONFIG, FakeAgents, FakeGitHub


class ConversationSizeTests(unittest.TestCase):
    """Large public reports fit checkpoints without discarding operative evidence."""

    def test_large_plan_and_reviews_reach_disabled_merge_gates(self):
        """Measure every real audit write, including the not-yet-published outbox."""
        config = deepcopy(CONFIG)
        gh, agents = FakeGitHub(config), FakeAgents()
        gh.comments = []
        original_ask, original_repo = agents.ask, gh.repo
        sizes = []
        plan_text, rationale, summary = "p" * 19000, "r" * 5500, "s" * 15000

        def large_report(provider, stage, context):
            """Use valid near-limit responses from each production role."""
            result = original_ask(provider, stage, context)
            if stage == "plan":
                result["steps"] = [plan_text]
            elif stage == 'plan_qc':
                result['rationale'] = rationale[:1200]
            elif stage == "intake":
                result["rationale"] = rationale
            elif stage == "local_review":
                result["summary"] = summary
            elif stage == "review":
                # Fill the review to the room its budget reports (twice the budget, as it has no findings).
                result["summary"] = summary[:2 * context["review_output_budget"]["max_json_bytes"] - 1500]
            return result

        def measure(path, method="GET", data=None):
            """Capture the exact artifact payload accepted by the GitHub transport."""
            if "/artifacts" in path and method in ("POST", "PATCH"):
                sizes.append(len(data["body"].encode()))
            return original_repo(path, method, data)

        with patch.object(agents, "ask", side_effect=large_report), patch.object(gh, "repo", side_effect=measure):
            for _ in range(11):
                controller = Controller(gh, agents, config)
                controller.advance(7)
        state = controller.state
        self.assertEqual(state["stage"], "gates")
        self.assertTrue(approval_valid(state, config))
        self.assertLessEqual(max(sizes), 60000)
        self.assertEqual(state["plan"]["steps"], [plan_text])
        self.assertNotIn("cooperative_votes", state)
        self.assertFalse(state["conversation"]["pending"])
        self.assertTrue(state["conversation"]["receipts"])
        self.assertTrue(all(not v["resolved"] for v in state.get("objections", [])))
        timeline = "\n".join(gh.archives())
        self.assertFalse([c for c in gh.comments if c["body"].startswith(conversation.MARKER)
                          and not any(label in c["body"] for label in ("Ready for review", "Implementation started"))], "only actionable comments are posted")
        self.assertNotIn(plan_text, timeline)
        self.assertIn(plan_text, controller.checkpoints.plan['body'])
        for full_text in (rationale,):
            self.assertIn(full_text, timeline)
        self.assertEqual(len(state["reviews"]["claude"]["summary"]), 15000)
        self.assertEqual(gh.merges, 0)

    def test_compaction_preserves_pending_and_unpublished_evidence(self):
        """Only acknowledged narrative is shortened; bindings, findings and open objections survive."""
        report = {"summary": "\u2028\\\"" * 1500, "findings": [{"path": "a", "detail": "Keep this finding"}]}
        state = {"issue": 7, "reviews": {"claude": deepcopy(report)},
                 "approval": {"binding": "exact"}, "plan": {"steps": ["unchanged"]},
                 "objections": [{"id": "open", "resolved": False}, {"id": "old", "resolved": True}],
                 "cooperative_votes": {"unused": report},
                 "conversation": {"pending": [], "receipts": {"reviews/claude": {"source_digest": digest(report)}}}}
        state["qc"] = {"intake": {"rationale": "unpublished" * 1000}}
        before = deepcopy(state)
        state["conversation"]["pending"] = [{"id": "pending"}]
        conversation.compact(state)
        self.assertEqual(state["reviews"], before["reviews"])
        state["conversation"]["pending"] = []
        conversation.compact(state)
        self.assertLess(len(json.dumps(state["reviews"]["claude"]["summary"]).encode()), 1000)
        for key in ("approval", "plan", "qc"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(state["reviews"]["claude"]["findings"], report["findings"])
        self.assertEqual(state["objections"], [{"id": "open", "resolved": False}])

    def test_reference_outbox_rejects_source_mutation(self):
        """A queued report cannot silently change between checkpointing and publication."""
        state = {"issue": 7, "reviews": {"claude": {"verdict": "approve", "summary": "original", "findings": [],
                                                    "dispositions": [], "finding_ids": [], "design_issue": None}}}
        conversation.queue(state, None, source=("reviews", "claude"), kind="review",
                           details={"head": "a" * 40, "round": 1, "pr": 9, "provider": "claude", "model": "m",
                                    "pr_url": "https://github.com/o/r/pull/9"})
        event = state["conversation"]["pending"][0]
        self.assertNotIn("body", event)
        self.assertEqual(event["content_digest"], digest(state["reviews"]["claude"]))
        conversation.event_body(state, event)
        state["reviews"]["claude"]["summary"] = "changed"
        with self.assertRaises(conversation.PublicationError):
            conversation.event_body(state, event)
