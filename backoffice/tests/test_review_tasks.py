"""Standalone review tasks in the backoffice: status before any review, rounds, and verified full reports."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "service/tests")]

from test_task_view import ReaderApp, Wire  # noqa: E402

import fixtures  # noqa: E402 -- initializes the service and engine test paths

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # noqa: E402

import report_store  # noqa: E402
from backoffice import github as github_module, review_tasks, tasks as tasks_module  # noqa: E402
from backoffice.auth import User  # noqa: E402
from backoffice.github import Access  # noqa: E402
from backoffice.settings import Reader, Settings  # noqa: E402
from backoffice.tasks import TaskNotFound, Tasks  # noqa: E402
from checkpoints import CheckpointError  # noqa: E402
from fakes import FakeAgents  # noqa: E402
from service.targets import Target  # noqa: E402
from standalone_fixtures import HEAD, PR  # noqa: E402
from test_standalone_reviews import Fixture  # noqa: E402

KEY = fixtures.KEY
NEXT = "c" * 40


class ReviewTaskTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for cache in (tasks_module.STATES, tasks_module.CONFIGS, github_module.REPOSITORIES, github_module.PERMISSIONS):
            cache.clear()
        self.enterContext(patch.object(tasks_module, "REPORTS", report_store.Cache()))
        self.wire = Wire(self.backend)
        repository = KEY[0]
        self.wire.fixed["GET", f"repos/{repository}"] = {"full_name": repository, "description": "fixture"}
        for login, permission in (("writer", "write"), ("reader", "read")):
            self.wire.fixed["GET", f"repos/{repository}/collaborators/{login}/permission"] = {"permission": permission}
        targets = (Target(repository, repository, "fork"),)
        self.settings = Settings(
            public_url="https://host.invalid/concurro",
            client_id="client",
            client_secret="secret",
            targets=targets,
            reader=Reader(1, Path("/unused"), targets),
        )
        self.fake.pr.update(created_at="2026-11-15T09:00:00Z", updated_at="2026-11-15T10:00:00Z")

    def tasks(self, login="writer"):
        access = Access(ReaderApp(), "user-token", self.wire.connection)
        return Tasks(self.settings, access, User(1, login, login.title(), ""))

    def view(self, login="writer"):
        return review_tasks.view(self.tasks(login), KEY[0], PR)

    def test_status_is_visible_before_any_review(self):
        self.request()
        self.scheduler.dispatch()
        task = self.view()["task"]
        self.assertEqual(
            (task["kind"], task["stage"], task["active"], task["round"]), ("standalone_pr_review", "queued", True, 1)
        )
        self.assertEqual(task["snapshot"]["head"], HEAD)
        self.assertEqual([r["verdict"] for r in task["rounds"][0]["reviewers"]], ["pending", "pending"])
        self.assertEqual((task["reports"], task["findings"], task["check"]["published"]), ([], [], True))
        self.assertTrue(task["revision"])

    def test_completed_rounds_link_each_reviewer_to_its_full_report(self):
        self.agents = FakeAgents(fix=True)
        self.request()
        self.drain()
        first = self.view()
        task = first["task"]
        [finished] = task["rounds"]
        self.assertEqual(
            (task["stage"], finished["outcome"], finished["requestedBy"]), ("complete", "complete", "writer")
        )
        self.assertEqual({r["verdict"] for r in finished["reviewers"]}, {"approve", "changes_requested"})
        self.assertEqual([f["status"] for f in task["findings"]], ["open"])
        self.assertFalse(task["changedSinceReview"])
        self.assertTrue(first["issueWriter"])
        requesting = next(r for r in finished["reviewers"] if r["verdict"] == "changes_requested")
        report = review_tasks.report(self.tasks(), KEY[0], PR, requesting["reportId"])["report"]
        self.assertEqual(
            (report["content"]["verdict"], report["round"], report["head"]), ("changes_requested", 1, HEAD)
        )
        self.assertEqual(report["content"]["findings"][0]["detail"], "Define the term.")
        self.assertEqual(report["id"], requesting["reportId"])
        # A later round keeps the earlier report addressable and unchanged.
        self.fake.push(NEXT, "Original guide\nA defined term.\n")
        self.assertTrue(self.view()["task"]["changedSinceReview"])
        self.request()
        self.drain()
        later = self.view()["task"]
        self.assertEqual(([r["round"] for r in later["rounds"]], later["snapshot"]["head"]), ([1, 2], NEXT))
        self.assertEqual(review_tasks.report(self.tasks(), KEY[0], PR, requesting["reportId"])["report"], report)
        self.assertEqual(len(later["reports"]), 4)

    def test_missing_report_evidence_is_an_integrity_state(self):
        self.request()
        self.drain()
        task = self.view()["task"]
        identifier = task["reports"][0]["id"]
        report = review_tasks.report(self.tasks(), KEY[0], PR, identifier)["report"]
        for number in report["artifacts"]:
            self.fake.artifacts[PR][number]["artifact"]["body"] = "replaced"
        tasks_module.REPORTS = report_store.Cache()
        with self.assertRaises((CheckpointError, report_store.ReportDataError)):
            review_tasks.report(self.tasks(), KEY[0], PR, identifier)
        with self.assertRaises(TaskNotFound):
            review_tasks.report(self.tasks(), KEY[0], PR, "0" * 64)

    def test_only_upstream_writers_and_only_review_tasks(self):
        self.request()
        self.drain()
        with self.assertRaises(TaskNotFound):
            self.view("reader")
        with self.assertRaises(TaskNotFound):
            review_tasks.report(self.tasks("reader"), KEY[0], PR, "0" * 64)
        self.fake.artifacts.pop(PR)
        with self.assertRaises(TaskNotFound):
            self.view()

    def test_reviewed_pull_requests_are_listed_from_the_services_list(self):
        rows = [
            {
                "number": 9,
                "title": "Explain",
                "open": True,
                "draft": False,
                "ref": "explain",
                "sha": HEAD,
                "runs": None,
                "updated_at": "2",
            },
            {
                "number": 12,
                "title": "Task PR",
                "open": True,
                "draft": True,
                "ref": "concurro/issue-7-abcdef012345",
                "sha": HEAD,
                "runs": None,
                "updated_at": "3",
            },
        ]
        reader = self.tasks()
        [target] = self.settings.targets
        self.assertEqual(review_tasks.listed(reader, target), [])
        with patch.object(Tasks, "holds", return_value="built"), patch.object(Tasks, "listing", return_value=rows):
            [review] = review_tasks.listed(reader, target)
        self.assertEqual((review["pr"], review["branch"], review["headSha"]), (9, "explain", HEAD[:7]))


if __name__ == "__main__":
    unittest.main()
