"""PR reviewers keep private conversations without carrying approvals across heads."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from fakes import CONFIG, FakeGitHub
from pipeline_fixtures import PipelineFixture
from policy import SessionFailure
from test_session_recovery import JournalAgents


class PRReviewSessionTests(PipelineFixture, unittest.TestCase):
    """Run repairs and recovery through real checkpoints and private on-disk journals."""

    def setUp(self):
        """Give each reviewer an isolated temporary home and an enrolled task."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = deepcopy(CONFIG)
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.agents = JournalAgents(self.directory.name)

    def test_repair_round_resumes_each_reviewer_with_full_diff_and_own_findings(self):
        """Repair reviews append updated inputs while preserving peer isolation and head binding."""
        self.agents.fix = True
        controller = self.reach("gates")
        reviews = [(p, c) for p, stage, c in self.agents.calls if stage == "review"]
        self.assertEqual(len(reviews), 4)
        scopes = set()
        for provider in ("claude", "codex"):
            first, second = [c for p, c in reviews if p == provider]
            a, b = first["controller_session"], second["controller_session"]
            self.assertEqual([a["turn"], b["turn"]], [0, 1])
            self.assertEqual(a["scope"], b["scope"])
            scopes.add(a["scope"])
            self.assertNotEqual(first["head"], second["head"])
            self.assertEqual(second["head"], controller.state["head"])
            self.assertTrue(second["changes"])
            self.assertEqual([x["path"] for x in second["changes"]], controller.state["changed_files"])
            for context in (first, second):
                self.assertNotIn("prior_reviews", context)
                self.assertNotIn("reviews", context)
            prompts = [prompt for scope, prompt in self.agents.prompts if scope == a["scope"]]
            self.assertEqual(len(prompts), 2)
            self.assertIn("untrusted_context", prompts[0])
            self.assertNotIn("untrusted_context", prompts[1])
            self.assertEqual(prompts[1]["context_update"]["head"], second["head"])
            self.assertEqual(prompts[1]["context_update"]["changes"], second["changes"])
            if provider == "claude":
                self.assertEqual(len(second["own_findings"]), 1)
            else:
                self.assertEqual(second["own_findings"], [])
        self.assertEqual(len(scopes), 2)
        self.assertTrue(all(r["head"] == controller.state["head"] for r in controller.state["reviews"].values()))
        self.assertTrue(all(f["status"] == "fixed" for f in controller.state["findings"]))

    def test_completed_review_replays_without_another_call_or_session_turn(self):
        """A crash after durable completion consumes the saved answer exactly once."""
        self.reach("review")
        controller = self.loaded()
        with patch.object(controller, "record_pr_review", side_effect=RuntimeError("after completion")):
            with self.assertRaisesRegex(RuntimeError, "after completion"):
                controller.review()
        saved = self.loaded().state
        role = next(k for k in saved["sessions"] if k.startswith("review:"))
        self.assertEqual(saved["sessions"][role]["turn"], 1)
        recovered = self.tick().state
        self.assertEqual(recovered["calls"], saved["calls"])
        self.assertEqual(recovered["sessions"][role], saved["sessions"][role])
        self.assertEqual(len(recovered["reviews"]), 1)
        self.assertEqual(sum(stage == "review" for _, stage, _ in self.agents.calls), 1)

    def test_failed_review_preserves_transcript_and_waits_for_writer_retry(self):
        """Retention adds no automatic call; a writer retry gets a clean replacement session."""
        self.reach("review")
        controller = self.loaded()
        before = deepcopy(controller.state)
        self.agents.failures = 1
        with self.assertRaisesRegex(SessionFailure, "provider session failed"):
            controller.review()
        controller.suspend("blocked", "provider session failed", publish_events=False)
        state = self.loaded().state
        self.assertEqual(state["calls"], before["calls"] + 1)
        self.assertEqual(state["approval"], before["approval"])
        role = next(k for k in state["sessions"] if k.startswith("review:"))
        current = state["sessions"][role]
        self.assertEqual((current["turn"], current["restarts"]), (0, 1))
        old_scope = self.agents.calls[-1][2]["controller_session"]["scope"]
        pending = Path(self.directory.name) / old_scope / "turn.json"
        self.assertEqual(json.loads(pending.read_text())["status"], "pending")
        for name, session in before["sessions"].items():
            self.assertEqual(state["sessions"][name], session)
        self.gh.comment("/concurro retry")
        retried = self.tick().state
        self.assertEqual(retried["calls"], before["calls"] + 2)
        self.assertEqual(retried["sessions"][role]["scope"], current["scope"])
        self.assertEqual(retried["sessions"][role]["turn"], 1)
        self.assertNotEqual(current["scope"], old_scope)
        self.assertEqual(json.loads(pending.read_text())["status"], "pending")

    def test_legacy_completed_review_needs_no_transcript_or_duplicate_call(self):
        """An in-flight answer from before retention was enabled still applies unchanged."""
        self.reach("review")
        controller = self.loaded()
        with (
            patch("sessions.prepare", return_value=None),
            patch.object(controller, "record_pr_review", side_effect=RuntimeError("legacy completion")),
        ):
            with self.assertRaisesRegex(RuntimeError, "legacy completion"):
                controller.review()
        saved = self.loaded().state
        self.assertFalse(any(k.startswith("review:") for k in saved["sessions"]))
        recovered = self.tick().state
        self.assertEqual(recovered["calls"], saved["calls"])
        self.assertEqual(recovered["sessions"], saved["sessions"])
        self.assertEqual(len(recovered["reviews"]), 1)
        self.tick()
        self.assertEqual(self.agents.calls[-1][2]["controller_session"]["turn"], 0)


if __name__ == "__main__":
    unittest.main()
