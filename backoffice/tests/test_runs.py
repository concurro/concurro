"""Runner diagnostics preserve GitHub facts and distinguish unavailable evidence."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402
from test_backoffice import USER, FakeOAuth, settings  # noqa: E402

from backoffice import app as app_module  # noqa: E402
from backoffice.auth import Sessions  # noqa: E402
from backoffice.runs import Runs  # noqa: E402
from github import NotFound  # noqa: E402
from service import diagnostics  # noqa: E402

SESSION = "12345678-1234-1234-1234-123456789abc"
RUN = {
    "id": 77,
    "run_attempt": 1,
    "head_branch": "concurro/runs/" + SESSION,
    "path": ".github/workflows/concurro-executor.yml",
    "event": "workflow_dispatch",
    "status": "completed",
    "conclusion": "success",
}
JOB = {
    "id": 81,
    "name": "Agent",
    "html_url": "https://github.com/owner/project/actions/runs/77/job/81",
    "status": "completed",
    "conclusion": "success",
    "started_at": "2026-11-13T12:00:00Z",
    "completed_at": "2026-11-13T12:03:11Z",
    "runner_name": "GitHub Actions 5",
    "labels": ["ubuntu-24.04"],
}


class RunsTests(unittest.TestCase):
    """Exact assignment, historical dispatch, pagination and incomplete observations."""

    def resolve(self, *, assigned=True, run=None, jobs=None, failure=None):
        """Serve real API response shapes and retain paths for provenance assertions."""
        self.calls = []

        def request(method, path):
            """Supply run and paginated job responses without network access."""
            self.assertEqual(method, "GET")
            self.calls.append(path)
            if failure and failure in path:
                raise NotFound("not found")
            if "/jobs?" in path:
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                return {"jobs": ([JOB] if jobs is None else jobs)[(page - 1) * 100 : page * 100]}
            if "/workflows/" in path:
                return {"workflow_runs": [dict(RUN, run_attempt=3)]}
            return RUN if run is None else run

        row = {
            "runner_sessions": json.dumps([SESSION]),
            "runner": json.dumps({"session": SESSION, "run_id": "77/1"}) if assigned else None,
        }
        return Runs(Mock(request=request), "owner/project").for_turn(row)

    def test_assigned_run_loads_the_exact_attempt_and_real_job_fields(self):
        """A successful turn's recorded run is independent of later reruns."""
        result = self.resolve()
        [run] = result["runs"]
        self.assertIsNone(result["message"])
        self.assertEqual((run["id"], run["attempt"], run["association"]), (77, 1, "assigned"))
        self.assertEqual(run["jobs"][0]["durationS"], 191)
        self.assertEqual(run["jobs"][0]["runner"], "GitHub Actions 5")
        self.assertEqual(run["jobs"][0]["url"], JOB["html_url"])
        self.assertEqual(self.calls[0], "repos/owner/project/actions/runs/77/attempts/1")

    def test_old_dispatch_finds_first_attempt_without_claiming_it_executed_the_turn(self):
        """Session branch correlation survives branch cleanup and subsequent reruns."""
        [run] = self.resolve(assigned=False)["runs"]
        self.assertEqual((run["attempt"], run["association"]), (1, "dispatched"))
        self.assertEqual(parse_qs(urlsplit(self.calls[0]).query)["branch"], [RUN["head_branch"]])
        self.assertIn("/attempts/1", self.calls[1])

    def test_all_job_pages_and_each_conclusion_are_preserved(self):
        """Pagination cannot silently drop later jobs or map timeouts to successes."""
        jobs = [dict(JOB, id=n, conclusion="timed_out" if n == 100 else "success") for n in range(101)]
        [run] = self.resolve(jobs=jobs)["runs"]
        self.assertEqual(len(run["jobs"]), 101)
        self.assertEqual(run["jobs"][-1]["conclusion"], "timed_out")

    def test_missing_jobs_are_different_from_an_empty_jobs_list(self):
        """A failed jobs lookup retains the known run."""
        self.assertIsNone(self.resolve(failure="/jobs?")["runs"][0]["jobs"])
        self.assertEqual(self.resolve(jobs=[])["runs"][0]["jobs"], [])
        result = self.resolve(failure="/attempts/")
        self.assertEqual(result["runs"], [])
        self.assertIn("could not be loaded", result["message"])

    def test_running_jobs_and_mismatched_runs_do_not_invent_evidence(self):
        """An unfinished job has no final duration; another branch is not this session."""
        [run] = self.resolve(jobs=[dict(JOB, status="in_progress", conclusion=None, completed_at=None)])["runs"]
        self.assertIsNone(run["jobs"][0]["durationS"])
        self.assertIsNone(run["jobs"][0]["conclusion"])
        for change in (
            {"head_branch": "main"},
            {"id": 78},
            {"run_attempt": 2},
            {"event": "push"},
            {"path": ".github/workflows/other.yml"},
        ):
            self.assertEqual(self.resolve(run=dict(RUN, **change))["runs"], [])

    def test_missing_historical_identity_is_unavailable(self):
        """A reused runner missing from old records cannot be guessed from adjacent turns."""
        reader = Mock()
        result = Runs(reader, "owner/project").for_turn({"runner_sessions": "[]"})
        self.assertEqual(result["runs"], [])
        self.assertIn("No Actions run identity", result["message"])
        reader.request.assert_not_called()

    def test_endpoint_checks_visibility_and_scopes_turn_lookup_to_the_task(self):
        """The Reader App is used only after the signed-in user's target visibility check."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = diagnostics.Store(root)
        self.addCleanup(store.close)
        store.submitted(SESSION, {"request": {"stage": "review"}}, {"repository": "up/project", "issue": 7})
        sessions = Sessions()
        sid = sessions.create("token", USER, 60)
        access = Mock()
        access.repository.return_value = {"name": "project"}
        with patch.object(app_module, "Access", return_value=access):
            client = TestClient(
                app_module.create_app(settings(diagnostics_dir=root), oauth=FakeOAuth(), sessions=sessions),
                cookies={app_module.SESSION_COOKIE: sid},
            )
            path = f"/api/tasks/up/project/7/turns/{SESSION}/runs"
            self.assertEqual(client.get(path).status_code, 200)
            self.assertEqual(client.get(path.replace("/7/", "/8/")).status_code, 404)
            access.repository.side_effect = lambda repo: None if repo == "up/project" else {"name": "project"}
            access.transport.reset_mock()
            self.assertEqual(client.get(path).status_code, 404)
            access.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
