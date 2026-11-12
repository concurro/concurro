"""Issue-addressed API and list data work before enrollment without fabricated task state."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from test_backoffice import USER, FakeOAuth, settings

from backoffice import app as app_module
from backoffice.auth import Sessions
from backoffice.commands import ServiceCommands
from backoffice.credentials import Vault
from backoffice.tasks import Tasks


class SponsorshipApiTests(unittest.TestCase):
    """Verify user-token forwarding, issue visibility, CSRF and exact provider arguments."""

    def test_issue_view_and_command_do_not_require_a_checkpoint(self):
        """Read a visible issue and post self-sponsorship through existing command transport."""
        seen = []

        def handle(request):
            """Stand in only for the internal service, retaining its exact authenticated request."""
            body = json.loads(request.content)
            seen.append((request.url.path, request.headers["authorization"], body))
            if request.url.path == "/sponsorships":
                return httpx.Response(
                    200, json={"funding": "org", "status": "confirmed", "slots": {"openai": {"sponsor_id": "42"}}}
                )
            return httpx.Response(
                201, json={"posted": "/concurro " + body["command"] + " " + body["argument"], "actor": "octo"}
            )

        service = ServiceCommands("http://service.invalid", httpx.Client(transport=httpx.MockTransport(handle)))
        sessions = Sessions()
        sid = sessions.create("user-token", USER, 60)
        reader = Mock()
        with patch.object(app_module, "Tasks", return_value=reader):
            client = TestClient(
                app_module.create_app(
                    settings(), oauth=FakeOAuth(), vault=Vault(None), sessions=sessions, commands=service
                ),
                cookies={app_module.SESSION_COOKIE: sid},
            )
            response = client.get("/api/repos/owner/project/issues/7/sponsorships")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["funding"], "org")
            reader.access.get.assert_called_with("owner/project", "issues/7")
            reader.task_view.assert_not_called()
            reader.state.assert_not_called()
            path = "/api/repos/owner/project/issues/7/commands"
            self.assertEqual(client.post(path, json={"command": "sponsor", "argument": "openai"}).status_code, 403)
            self.assertEqual(
                client.post(
                    path, headers={app_module.CSRF_HEADER: "1"}, json={"command": "sponsor", "argument": "openai"}
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    path, headers={app_module.CSRF_HEADER: "1"}, json={"command": "sponsor", "argument": "someone-else"}
                ).status_code,
                422,
            )
            reader.task_view.assert_not_called()
            reader.state.assert_not_called()
        self.assertEqual(seen[0], ("/sponsorships", "Bearer user-token", {"repository": "owner/project", "issue": 7}))
        self.assertEqual(seen[1][2]["argument"], "openai")

    def test_unenrolled_intent_is_an_issue_record_without_stage_or_budget(self):
        """Known intent issues join the target listing without inventing a Task contract."""
        issue = {
            "number": 7,
            "title": "Unenrolled",
            "body": "",
            "user": {"login": "octo"},
            "labels": [],
            "created_at": "2026-01-01Z",
            "updated_at": "2026-01-02Z",
        }
        access = Mock()
        access.task_issues.return_value = []
        access.get.return_value = issue
        view = Tasks(
            settings(),
            access,
            USER,
            lambda repository: {
                "issues": [{"issue": 7, "status": "confirmed", "slots": {"openai": {"sponsor_id": "42"}}}]
            },
        )
        target = SimpleNamespace(issue_repositories=("owner/project",), upstream="up/project")
        with patch.object(view, "pulls_by_issue", return_value={}):
            tasks = view.listed(target, {"mode": "manual"})
        self.assertEqual(tasks, [])
        self.assertEqual(len(view.issue_intents), 1)
        self.assertNotIn("stage", view.issue_intents[0])
        self.assertNotIn("budgets", view.issue_intents[0])
        self.assertEqual(view.issue_intents[0]["sponsorships"]["status"], "confirmed")
