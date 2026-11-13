"""Recorded turns become the overview's feed and a target's run list, without reading GitHub."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_backoffice import USER, settings  # noqa: E402

from backoffice import activity  # noqa: E402
from backoffice.tasks import Tasks  # noqa: E402
from backoffice.turns import Diagnostics  # noqa: E402
from service import diagnostics  # noqa: E402
from service.targets import Target  # noqa: E402

SESSION = "12345678-1234-1234-1234-1234567890"


class ActivityTests(unittest.TestCase):
    """One diagnostics directory, seeded through the service's own recorder."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = [1.0e9]
        self.store = diagnostics.Store(self.root, clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.diagnostics = Diagnostics(self.root)

    def record(self, identifier, repository, issue, stage, *, outcome=None, run_id=None, started=True, agent="opus"):
        """Record one turn the way the worker does: submitted, observed, then finished."""
        self.clock[0] += 60
        request = {"stage": stage, "provider": "anthropic", "agent": {"profile": agent}}
        self.store.submitted(identifier, {"request": request}, {"repository": repository, "issue": issue})
        runner = {"session": SESSION, "run_id": run_id} if run_id else None
        timing = {"created": self.clock[0], "started": self.clock[0] + 5 if started else None}
        self.store.observed(identifier, "running", timing, [SESSION], runner)
        if outcome:
            self.store.finished(identifier, {"outcome": outcome, "elapsed_seconds": 999})
            self.store.db.execute("UPDATE turns SET finished = ? WHERE id = ?", (self.clock[0] + 35, identifier))

    def test_the_feed_reports_recorded_turns_newest_first(self):
        self.record("t1", "up/project", 7, "intake", outcome="ok")
        self.record("t2", "up/project", 8, "develop", outcome="failed", agent="sonnet")
        self.record("t3", "owner/project", 9, "review")
        rows = self.diagnostics.recent(["up/project", "owner/project"], activity.ACTIVITY_ITEMS)
        feed = activity.feed(rows)
        self.assertEqual([item["id"] for item in feed], ["t3", "t2", "t1"])
        self.assertEqual(
            [(item["actor"], item["text"], item["tone"]) for item in feed],
            [
                ("opus", "is running the review turn", "neutral"),
                ("sonnet", "failed the implement turn", "danger"),
                ("opus", "finished the intake turn", "success"),
            ],
        )
        self.assertEqual(feed[0]["task"], {"owner": "owner", "repo": "project", "issue": 9})
        self.assertTrue(all(item["at"] for item in feed))
        self.assertEqual(activity.feed(rows, limit=1), feed[:1])

    def test_a_queued_turn_reads_as_queued(self):
        self.record("t1", "up/project", 7, "plan", started=False)
        [item] = activity.feed(self.diagnostics.recent(["up/project"], 5))
        self.assertEqual(item["text"], "is queued the plan turn")

    def test_runs_list_recorded_assignments_with_their_turn_numbers(self):
        self.record("t1", "up/project", 7, "intake", outcome="ok", run_id="77/1")
        self.record("t2", "up/project", 7, "develop", outcome="failed", run_id="78/2")
        self.record("t3", "up/project", 7, "review")
        rows = self.diagnostics.recent(["up/project"], 10)
        runs = activity.runs(rows)
        self.assertEqual([(r["id"], r["attempt"], r["turn"]) for r in runs], [(78, 2, 2), (77, 1, 1)])
        self.assertEqual(
            [(r["title"], r["status"]) for r in runs], [("implementer turn 2", "failure"), ("triage turn 1", "success")]
        )
        self.assertEqual([r["issue"] for r in runs], [7, 7])
        self.assertEqual(runs[0]["durationS"], 30, "recorded timings win over the reported elapsed seconds")
        self.assertEqual(activity.runs(rows, limit=1), runs[:1])

    def test_turn_numbers_count_within_each_task(self):
        self.record("t1", "up/project", 7, "intake", outcome="ok", run_id="1/1")
        self.record("t2", "owner/project", 9, "intake", outcome="ok", run_id="2/1")
        self.record("t3", "up/project", 7, "plan", outcome="ok", run_id="3/1")
        rows = self.diagnostics.recent(["up/project", "owner/project"], 10)
        self.assertEqual({r["id"]: r["turn"] for r in activity.runs(rows)}, {1: 1, 2: 1, 3: 2})

    def test_views_carry_the_feed_and_runs_without_a_github_read(self):
        self.record("t1", "up/project", 7, "develop", outcome="ok", run_id="77/1")
        target = Target("up/project", "up/project", "upstream")
        access = Mock()
        access.repository.return_value = {"full_name": "up/project", "description": ""}
        access.task_issues.return_value = []
        access.pull_requests.return_value = []
        access.config.return_value = ({"repository": "up/project"}, "main")
        access.writable.return_value = True
        reader = Tasks(settings(targets=(target,)), access, USER, self.diagnostics)
        overview = reader.overview()
        self.assertEqual([item["id"] for item in overview["activity"]], ["t1"])
        view = reader.target_view("up/project")
        self.assertEqual([run["id"] for run in view["runs"]], [77])
        self.assertEqual(reader.recorded(["up/project"]), self.diagnostics.recent(["up/project"], 12))

    def test_without_a_diagnostics_directory_both_lists_are_empty(self):
        target = Target("up/project", "up/project", "upstream")
        access = Mock()
        access.repository.return_value = {"full_name": "up/project", "description": ""}
        access.task_issues.return_value = []
        access.pull_requests.return_value = []
        access.config.return_value = ({"repository": "up/project"}, "main")
        access.writable.return_value = True
        reader = Tasks(settings(targets=(target,)), access, USER)
        self.assertEqual(reader.overview()["activity"], [])
        self.assertEqual(reader.target_view("up/project")["runs"], [])
        self.assertEqual(Diagnostics(None).recent(["up/project"], 5), [])
        self.assertEqual(Diagnostics(self.root).recent([], 5), [])


if __name__ == "__main__":
    unittest.main()
