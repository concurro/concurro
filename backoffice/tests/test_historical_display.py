"""The migration bridge exports the retained verified projectors, never active tasks."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backoffice/tests"), str(ROOT / "service/tests")]
import test_review_tasks
import test_task_view

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from checkpoints import Checkpoints
from deploy.export_historical_display import export
from fakes import FakeAgents
from standalone_fixtures import PR


def projector_manifest():
    """Build both historical workflows through real checkpoints, detached reports and old projectors."""
    contributions = test_task_view.TaskViewTests()
    contributions.setUp()
    try:
        state = {
            "version": 2,
            "issue": 7,
            "base": "a" * 40,
            "stage": "gates",
            "plan_id": "plan-parity",
            "plan": {
                "summary": "Keep historical evidence readable",
                "steps": ["Read saved evidence"],
                "files": ["reader.rs"],
                "tests": ["Replay the display"],
                "questions": [],
            },
            "credentials": {
                "legacy-debit": {
                    "kind": "contributed",
                    "provider": "codex",
                    "account": "42",
                    "route": "openai-api",
                    "version": 2,
                    "calls": 3,
                }
            },
            "calls": 5,
            "round": 1,
            "pr": 9,
            "handoff_ready": True,
            "qc_history": [{"stage": "intake", "decision": "proceed", "risk": "low", "rationale": "Read-only display"}],
            "review_history": [{"reviewer": {"verdict": "approve", "summary": "Readable", "head": "b" * 40}}],
            "reports": [],
        }
        Checkpoints(contributions.fake, contributions.config, 7).save(state)
        contributions.wire.fixed["GET", f"repos/{KEY[0]}/issues/7"] = contributions.issue_listing()
        contributions.wire.fixed["GET", f"repos/{KEY[0]}/pulls/9"] = {
            "title": "Historical contribution",
            "head": {"ref": "concurro/issue-7-aabb", "sha": "b" * 40},
            "additions": 12,
            "deletions": 3,
        }
        contributions.wire.fixed["GET", f"repos/{KEY[0]}/commits/{'b' * 40}/check-runs?per_page=100"] = {
            "check_runs": [{"status": "completed", "conclusion": "success"}]
        }
        contribution = export(
            contributions.tasks(),
            "python-parity-fixture",
            "2026-11-21T10:00:00Z",
            [{"kind": "contribution", "repository": KEY[0], "number": 7}],
        )
    finally:
        contributions.tearDown()
        contributions.doCleanups()
    reviews = test_review_tasks.ReviewTaskTests()
    reviews.setUp()
    try:
        reviews.agents = FakeAgents(fix=True)
        reviews.request()
        reviews.drain()
        review = export(
            reviews.tasks(),
            "python-parity-fixture",
            "2026-11-21T10:00:00Z",
            [{"kind": "review", "repository": KEY[0], "number": PR}],
        )
    finally:
        reviews.tearDown()
        reviews.doCleanups()
    contribution["views"].extend(review["views"])
    return contribution


class HistoricalDisplayTests(unittest.TestCase):
    """The fixture exercises the original evidence path used before the Rust switch."""

    def test_verified_projectors_export_phase_evidence_and_complete_review_reports(self):
        manifest = projector_manifest()
        task, review = manifest["views"]
        self.assertEqual(task["view"]["task"]["plans"][-1]["id"], "plan-parity")
        self.assertEqual(task["view"]["task"]["budgets"]["modelCalls"]["used"], 5)
        self.assertFalse(task["view"]["issueWriter"])
        self.assertNotIn("sponsorships", task["view"]["task"])
        self.assertTrue(task["provenance"]["source_revision"])
        self.assertEqual(task["usage"]["status"], "available")
        self.assertEqual(task["usage"]["charged_calls"][0]["calls"], 3)
        self.assertEqual(task["usage"]["charged_calls"][0]["sponsor"], "42")
        self.assertTrue(review["reports"])
        for summary in review["view"]["task"]["reports"]:
            self.assertEqual(review["reports"][summary["id"]]["report"]["id"], summary["id"])
            self.assertIn("summary", review["reports"][summary["id"]]["report"]["content"])


if __name__ == "__main__":
    unittest.main()
