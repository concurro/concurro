"""A task view reads the engine's own checkpoint through the user's transport, end to end."""

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "service/tests")]

import fixtures  # noqa: E402 -- initializes the service and engine test paths

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # noqa: E402

from backoffice import github as github_module, tasks as tasks_module  # noqa: E402
from backoffice.auth import User  # noqa: E402
from backoffice.github import UserAccess  # noqa: E402
from backoffice.settings import Settings  # noqa: E402
from backoffice.tasks import TaskNotFound, Tasks  # noqa: E402
from service.targets import Target  # noqa: E402

KEY = fixtures.KEY


class Wire(fixtures.Wire):
    """Answers some paths every time, for reads the engine's fake backend doesn't serve."""

    def __init__(self, backend):
        super().__init__(backend)
        self.fixed = {}
        self.paths = []

    def answer(self, method, path, data):
        self.paths.append(path)
        if (method, path) in self.fixed:
            return fixtures.Reply(200, self.fixed[method, path])
        return super().answer(method, path, data)


class TaskViewTests(fixtures.ServiceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for cache in (tasks_module.STATES, tasks_module.CONFIGS, github_module.REPOSITORIES):
            cache.clear()
        self.wire = Wire(self.backend)
        repository = KEY[0]
        metadata = {"full_name": repository, "description": "fixture", "permissions": {"push": True}}
        self.wire.fixed["GET", f"repos/{repository}"] = metadata
        self.settings = Settings(
            public_url="https://host.invalid/concurro",
            client_id="client",
            client_secret="secret",
            targets=(Target(repository, repository, "fork"),),
        )

    def tasks(self):
        access = UserAccess("user-token", self.wire.connection)
        return Tasks(self.settings, access, User(1, "writer", "Writer", ""))

    def issue_listing(self):
        issue = deepcopy(self.fake.issue)
        issue.setdefault("created_at", "2026-11-01T00:00:00Z")
        issue.setdefault("updated_at", "2026-11-02T00:00:00Z")
        issue.setdefault("title", "Fixture task")
        return issue

    def test_task_view_projects_the_checkpoint(self):
        state, _ = self.tick()
        for _ in range(3):
            if state and state["stage"] not in ("intake", "plan", "plan_qc", "planned"):
                break
            state, _ = self.tick()
        self.assertIsNotNone(state)
        issue = self.issue_listing()
        self.wire.fixed["GET", f"repos/{KEY[0]}/issues/7"] = issue
        reader = self.tasks()
        view = reader.task_view(KEY[0], 7)
        self.assertEqual(reader.configs[KEY[0]][0]["repository"], KEY[0], "the target configuration was read")
        task = view["task"]
        self.assertEqual(task["stage"], state["stage"])
        self.assertEqual(task["issue"], 7)
        self.assertTrue(task["plans"], "the approved plan is projected")
        self.assertEqual(task["plans"][-1]["id"], state["plan_id"])
        self.assertEqual(view["target"]["mode"], "manual")
        self.assertTrue(view["target"]["writer"])
        # An unchanged checkpoint comment serves the decoded state without reading artifacts again.
        self.wire.paths.clear()
        again = self.tasks().task_view(KEY[0], 7)
        self.assertEqual(again["task"]["plans"], task["plans"])
        self.assertFalse([path for path in self.wire.paths if "/artifacts" in path])

    def test_unknown_repositories_are_not_found(self):
        with self.assertRaises(TaskNotFound):
            self.tasks().task_view("someone/else", 7)


if __name__ == "__main__":
    unittest.main()
