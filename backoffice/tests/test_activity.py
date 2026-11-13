"""Recorded turns become the overview's feed; a target's runs come from GitHub, attributed to them."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_backoffice import USER, settings  # noqa: E402

from backoffice import activity  # noqa: E402
from backoffice.runs import Runs  # noqa: E402
from backoffice.tasks import Tasks  # noqa: E402
from backoffice.turns import Diagnostics  # noqa: E402
from github import NotFound, Transient  # noqa: E402
from service import diagnostics  # noqa: E402
from service.targets import Target  # noqa: E402

SESSION = "12345678-1234-1234-1234-123456789abc"


class ActivityTests(unittest.TestCase):
    """One diagnostics directory, seeded through the service's own recorder."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = [1.0e9]
        self.store = diagnostics.Store(self.root, clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.diagnostics = Diagnostics(self.root)

    def record(
        self,
        identifier,
        repository,
        issue,
        stage,
        *,
        outcome=None,
        run_id=None,
        started=True,
        agent="opus",
        status="running",
        session=SESSION,
    ):
        """Record one turn the way the worker does: submitted, observed, then finished."""
        self.clock[0] += 60
        request = {"stage": stage, "provider": "anthropic", "agent": {"profile": agent}}
        self.store.submitted(identifier, {"request": request}, {"repository": repository, "issue": issue})
        runner = {"session": session, "run_id": run_id} if run_id else None
        timing = {"created": self.clock[0], "started": self.clock[0] + 5 if started else None}
        self.store.observed(identifier, status, timing, [session], runner)
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

    def test_a_lost_runner_never_reads_as_still_running(self):
        """A job-bound turn whose runner vanished is recorded as lost with no outcome."""
        self.record("t1", "up/project", 7, "develop", status="lost")
        self.record("t2", "up/project", 8, "review", status="cancelled")
        feed = activity.feed(self.diagnostics.recent(["up/project"], 5))
        self.assertEqual(
            [(item["text"], item["tone"]) for item in feed],
            [("had its runner end the review turn", "warning"), ("stopped reporting on the implement turn", "danger")],
        )

    def test_the_feed_leads_with_the_newest_event_not_the_newest_submission(self):
        """A long turn that just finished outranks a turn submitted after it."""
        self.record("slow", "up/project", 7, "develop")
        self.record("quick", "up/project", 8, "review", outcome="ok")
        self.clock[0] += 600
        self.store.finished("slow", {"outcome": "ok", "elapsed_seconds": 600})
        self.store.db.execute("UPDATE turns SET finished = ? WHERE id = ?", (self.clock[0], "slow"))
        self.assertEqual(
            [item["id"] for item in activity.feed(self.diagnostics.recent(["up/project"], 5))], ["slow", "quick"]
        )
        self.assertEqual([row["id"] for row in self.diagnostics.recent(["up/project"], 1)], ["slow"])

    def runs(self, listed, repositories=("up/project",), reader=None, attempts=None):
        """`for_target` over a fake Actions listing, recording the request paths and lookups."""
        self.paths, self.asked = [], []

        def request(method, path):
            self.paths.append(path)
            if reader:
                return reader(path)
            if "/attempts/" in path:
                found = (attempts or {}).get(path)
                if found is None:
                    raise NotFound("attempt not found")
                return found
            return {"workflow_runs": listed}

        def lookup(sessions):
            self.asked.append(sorted(sessions))
            return self.diagnostics.for_sessions(repositories, sessions)

        return Runs(Mock(request=request), "owner/project").for_target(lookup)

    def listed_run(self, identifier, session=SESSION, **extra):
        return {
            "id": identifier,
            "run_attempt": 1,
            "head_branch": "concurro/runs/" + session,
            "status": "completed",
            "conclusion": "success",
            "run_started_at": "2026-11-13T12:00:00Z",
            "updated_at": "2026-11-13T12:04:00Z",
            **extra,
        }

    def test_one_run_serving_several_turns_is_listed_once(self):
        """A session runs several turns, so its run must not appear once per turn."""
        self.record("t1", "up/project", 7, "plan", outcome="ok", run_id="77/1")
        self.record("t2", "up/project", 7, "plan", outcome="ok", run_id="77/1")
        found = self.runs([self.listed_run(77)])
        self.assertEqual([run["id"] for run in found["runs"]], [77])
        [run] = found["runs"]
        self.assertEqual((run["title"], run["turn"]), ("planner turns 1–2", 1))
        self.assertEqual(run["task"], {"owner": "up", "repo": "project", "issue": 7})
        self.assertIsNone(found["message"])

    def test_status_and_duration_come_from_github_not_from_the_turn(self):
        """A finished turn on a still-running run reads as running, with no invented duration."""
        self.record("t1", "up/project", 7, "develop", outcome="ok", run_id="77/1")
        running = self.listed_run(77, status="in_progress", conclusion=None)
        [run] = self.runs([running])["runs"]
        self.assertEqual((run["status"], run["durationS"]), ("in_progress", 0))
        self.assertEqual(run["startedAt"], "2026-11-13T12:00:00Z")
        for conclusion, status in (
            ("success", "success"),
            ("failure", "failure"),
            ("cancelled", "cancelled"),
            ("timed_out", "failure"),
        ):
            with self.subTest(conclusion=conclusion):
                [run] = self.runs([self.listed_run(77, conclusion=conclusion)])["runs"]
                self.assertEqual((run["status"], run["durationS"]), (status, 240))
        [run] = self.runs([self.listed_run(77, status="queued", conclusion=None)])["runs"]
        self.assertEqual(run["status"], "queued")

    def test_a_fork_task_keeps_its_own_repository(self):
        """Issues may live on the fork; the run must link to the repository that holds them."""
        self.record("t1", "owner/project", 9, "intake", outcome="ok", run_id="78/1")
        [run] = self.runs([self.listed_run(78)], repositories=("owner/project",))["runs"]
        self.assertEqual(run["task"], {"owner": "owner", "repo": "project", "issue": 9})

    def test_a_run_with_no_recorded_turn_is_still_listed(self):
        """Dropping unmatched runs hid real runs and could make a busy fork read as empty."""
        self.record("t1", "up/project", 7, "intake", outcome="ok", run_id="77/1")
        other = "00000000-0000-0000-0000-000000000000"
        found = self.runs([self.listed_run(77), self.listed_run(99, session=other)])
        self.assertEqual([run["id"] for run in found["runs"]], [77, 99])
        unmatched = found["runs"][1]
        self.assertEqual((unmatched["task"], unmatched["turn"], unmatched["association"]), (None, None, None))
        self.assertEqual(unmatched["title"], "Executor run")
        self.assertIn("event=workflow_dispatch", self.paths[0])
        self.assertEqual(self.asked, [sorted([SESSION, other])], "attribution is asked only for the listed sessions")

    def test_attribution_does_not_depend_on_how_recent_a_turn_is(self):
        """A run GitHub still lists can be older than any window of recent turns."""
        self.record("old", "up/project", 7, "intake", outcome="ok", run_id="77/1")
        for n in range(30):
            self.record(
                f"n{n}", "up/project", 8, "review", outcome="ok", session=f"0000000{n:01d}-0000-0000-0000-000000000000"
            )
        [run] = self.runs([self.listed_run(77)])["runs"]
        self.assertEqual(run["task"], {"owner": "up", "repo": "project", "issue": 7})

    def test_failed_listings_say_so(self):
        failed = self.runs([], reader=Mock(side_effect=Transient("throttled", 30)))
        self.assertEqual(failed["runs"], [])
        self.assertIn("could not be loaded", failed["message"])

    def test_a_rerun_never_lends_its_status_to_the_turns(self):
        """Only attempt 1 can serve a session, so a failed rerun is not the turn's evidence."""
        self.record("t1", "up/project", 7, "plan", outcome="ok", run_id="77/1")
        rerun = self.listed_run(77, run_attempt=2, conclusion="failure")
        first = self.listed_run(77, updated_at="2026-11-13T12:02:00Z")
        found = self.runs([rerun], attempts={"repos/owner/project/actions/runs/77/attempts/1": first})
        [run] = found["runs"]
        self.assertEqual((run["status"], run["attempt"], run["durationS"]), ("success", 1, 120))
        self.assertIn("repos/owner/project/actions/runs/77/attempts/1", self.paths)
        unreadable = self.runs([rerun])
        self.assertEqual(unreadable["runs"], [])
        self.assertIn("reruns could not be read", unreadable["message"])
        alone = self.runs([self.listed_run(99, session="00000000-0000-0000-0000-000000000000", run_attempt=2)])
        self.assertEqual([run["attempt"] for run in alone["runs"]], [2], "an unattributed rerun needs no exact attempt")

    def test_a_dispatch_that_did_not_serve_is_marked_dispatched(self):
        """`runner_sessions` records dispatches; only the recorded assignment names the run that served."""
        self.record(
            "t1", "up/project", 7, "plan", outcome="ok", run_id="78/1", session="11111111-1111-1111-1111-111111111111"
        )
        self.record("t2", "up/project", 8, "review", outcome="ok")
        earlier = self.runs([self.listed_run(77, session="11111111-1111-1111-1111-111111111111")])
        self.assertEqual([(r["task"], r["association"]) for r in earlier["runs"]], [(None, None)])
        [dispatched] = self.runs([self.listed_run(77)])["runs"]
        self.assertEqual(dispatched["association"], "dispatched")
        self.assertEqual(dispatched["task"], {"owner": "up", "repo": "project", "issue": 8})

    def test_a_run_is_matched_by_its_session_when_no_identity_was_recorded(self):
        """Older rows record only the session; its branch still identifies the run."""
        self.record("t1", "up/project", 7, "review")
        [run] = self.runs([self.listed_run(77)])["runs"]
        self.assertEqual((run["id"], run["turn"]), (77, 1))

    def test_views_carry_the_feed_and_the_targets_runs(self):
        self.record("t1", "up/project", 7, "develop", outcome="ok", run_id="77/1")
        target = Target("up/project", "up/project", "upstream")
        access = Mock()
        access.repository.return_value = {"full_name": "up/project", "description": ""}
        access.task_issues.return_value = []
        access.pull_requests.return_value = []
        access.config.return_value = ({"repository": "up/project"}, "main")
        access.writable.return_value = True
        access.transport.return_value = Mock(request=lambda method, path: {"workflow_runs": [self.listed_run(77)]})
        reader = Tasks(settings(targets=(target,)), access, USER, self.diagnostics)
        overview = reader.overview()
        self.assertEqual([item["id"] for item in overview["activity"]], ["t1"])
        view = reader.target_view("up/project")
        self.assertEqual([run["id"] for run in view["runs"]], [77])
        self.assertIsNone(view["runsMessage"])
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
        view = reader.target_view("up/project")
        self.assertEqual((view["runs"], view["runsMessage"]), ([], None))
        access.transport.assert_not_called()
        self.assertEqual(Diagnostics(None).recent(["up/project"], 5), [])
        self.assertEqual(Diagnostics(self.root).recent([], 5), [])


if __name__ == "__main__":
    unittest.main()
