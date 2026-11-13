"""A task view reads the engine's own checkpoint through the Reader App's transport, end to end."""

import sys
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "service/tests")]

import fixtures  # noqa: E402 -- initializes the service and engine test paths

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # noqa: E402

from app_fakes import App  # noqa: E402
from backoffice import github as github_module, tasks as tasks_module  # noqa: E402
from backoffice.auth import User  # noqa: E402
from backoffice.github import Access  # noqa: E402
from backoffice.settings import Reader, Settings  # noqa: E402
from backoffice.tasks import TaskNotFound, Tasks  # noqa: E402
from service.identity import Identity  # noqa: E402
from service.targets import Target  # noqa: E402
from service.transport import StaticCredential  # noqa: E402

KEY = fixtures.KEY


class Wire(fixtures.Wire):
    """Answers some paths every time, for reads the engine's fake backend doesn't serve."""

    def __init__(self, backend):
        super().__init__(backend)
        self.fixed = {}
        self.paths = []
        self.reads: list[tuple[str, str]] = []
        self.hidden_from_user: set[str] = set()
        self.token = ""

    def connection(self, host, timeout):
        return Connection(self)

    def answer(self, method, path, data):
        self.paths.append(path)
        self.reads.append((self.token, path))
        if self.token == "Bearer user-token" and path in self.hidden_from_user:
            return fixtures.Reply(404, {"message": "Not Found"})
        if (method, path) in self.fixed:
            return fixtures.Reply(200, self.fixed[method, path])
        return super().answer(method, path, data)


class Connection(fixtures.WireConnection):
    """Records each request's token, so the wire can answer as GitHub would for it."""

    def request(self, method, target, body=None, headers=None):
        self.wire.token = (headers or {}).get("Authorization", "")
        super().request(method, target, body, headers)


class ReaderApp:
    """The Reader App's identity, with one fixed token for every repository."""

    def credential(self, repository, kind):
        assert kind == "read"
        return StaticCredential("reader-token")


class TaskViewTests(fixtures.ServiceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for cache in (tasks_module.STATES, tasks_module.CONFIGS, github_module.REPOSITORIES):
            cache.clear()
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

    def tasks(self, login="writer"):
        access = Access(ReaderApp(), "user-token", self.wire.connection)
        return Tasks(self.settings, access, User(1, login, login.title(), ""))

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
        self.assertFalse(self.tasks("reader").task_view(KEY[0], 7)["target"]["writer"], "a reader sees no commands")
        # An unchanged checkpoint comment serves the decoded state without reading artifacts again.
        self.wire.paths.clear()
        again = self.tasks().task_view(KEY[0], 7)
        self.assertEqual(again["task"]["plans"], task["plans"])
        self.assertFalse([path for path in self.wire.paths if "/artifacts" in path])

    def test_the_users_token_only_decides_visibility(self):
        """Reads use the Reader App, but a repository hidden from the user's own token hides its target."""
        reader = self.tasks()
        self.assertTrue(reader.target_record(*reader.find_target(KEY[0]))["writer"])
        users = {path for token, path in self.wire.reads if token == "Bearer user-token"}
        self.assertEqual(users, {f"repos/{KEY[0]}"})
        self.assertIn(("Bearer reader-token", f"repos/{KEY[0]}/collaborators/writer/permission"), self.wire.reads)
        github_module.REPOSITORIES.clear()
        self.wire.hidden_from_user.add(f"repos/{KEY[0]}")
        self.assertEqual(self.tasks().visible_targets(), [])
        with self.assertRaises(TaskNotFound):
            self.tasks().task_view(KEY[0], 7)

    def test_unknown_repositories_are_not_found(self):
        with self.assertRaises(TaskNotFound):
            self.tasks().task_view("someone/else", 7)

    def test_separate_installations_and_visibility_for_both_repositories(self):
        """Each organization supplies its own read token; either hidden repository hides the whole target."""
        target = Target("upstream/project", "fork/project", "both")
        settings = replace(self.settings, targets=(target,), reader=replace(self.settings.reader, targets=(target,)))
        app = App(fixtures.Clock())
        original = app.handle
        installations = {target.upstream: 11, target.fork: 22}
        tokens, reads, minted = {}, [], []
        hidden = set()
        wanted = {
            target.upstream: {
                "contents": "read",
                "pull_requests": "read",
                "checks": "read",
                "statuses": "read",
                "actions": "read",
                "issues": "read",
            },
            target.fork: {"contents": "read", "actions": "read", "issues": "read"},
        }

        def answer(method, path, headers, body):
            """Serve separate installations and reject data reads with the wrong repository's token."""
            token = headers["Authorization"].removeprefix("Bearer ")
            if path == "/installation/repositories?per_page=100":
                return 200, {"total_count": 1, "repositories": [{"full_name": tokens[token]}]}
            for repository, installation in installations.items():
                if path == f"/repos/{repository}/installation":
                    self.assertEqual(token, "test-jwt")
                    return 200, {
                        "id": installation,
                        "repository_selection": "selected",
                        "account": {"login": repository.split("/")[0]},
                    }
                if method == "POST" and path == f"/app/installations/{installation}/access_tokens":
                    self.assertEqual(token, "test-jwt")
                    status, result = original(method, "/app/installations/5/access_tokens", headers, body)
                    self.assertEqual(app.minted[-1][1:], (["project"], wanted[repository]))
                    tokens[result["token"]] = repository
                    minted.append(installation)
                    return status, result
                if path == f"/repos/{repository}" and token == "user-token":
                    reads.append((token, path))
                    return (404, {}) if repository in hidden else (200, {"full_name": repository})
                if path.startswith(f"/repos/{repository}/"):
                    self.assertEqual(tokens.get(token), repository)
                    reads.append((token, path))
                    if path.endswith("/permission"):
                        return 200, {"permission": "read"}
                    return 200, []
            return original(method, path, headers, body)

        identity = Identity(settings.reader, app.connection, app.clock)
        with (
            patch.object(app, "handle", side_effect=answer),
            patch.object(identity, "app_jwt", return_value="test-jwt"),
        ):
            access = Access(identity, "user-token", app.connection)
            reader = Tasks(settings, access, User(1, "reader", "Reader", ""))
            self.assertEqual(reader.find_target(target.upstream)[0], target)
            self.assertEqual({path for _, path in reads}, {f"/repos/{r}" for r in target.repositories})
            self.assertEqual(minted, [], "visibility alone needs no installation token")
            for repository in target.repositories:
                self.assertEqual(access.task_issues(repository), [])
            self.assertFalse(access.writable(target.fork, "reader"))
            self.assertEqual(minted, [11, 22], "the fork's writer check reuses its read token")
            for repository in target.repositories:
                with self.subTest(hidden=repository):
                    hidden.clear()
                    hidden.add(repository)
                    github_module.REPOSITORIES.clear()
                    view = Tasks(settings, Access(identity, "user-token", app.connection), reader.user)
                    self.assertEqual(view.visible_targets(), [])
                    for destination in target.repositories:
                        with self.assertRaises(TaskNotFound):
                            view.task_view(destination, 7)
            self.assertEqual(minted, [11, 22])


if __name__ == "__main__":
    unittest.main()
