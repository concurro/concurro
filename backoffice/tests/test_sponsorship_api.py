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
from backoffice.commands import CommandError, ServiceCommands
from backoffice.credentials import Vault
from backoffice.tasks import Tasks
from github import STAGE_LABEL_PREFIX, Transient


class SponsorshipApiTests(unittest.TestCase):
    """Verify user-token forwarding, issue visibility, CSRF and exact provider arguments."""

    def test_discovery_service_outage_keeps_safe_http_status(self):
        """A temporary service failure is visible to the portal, not silently rendered as no consent."""
        sessions = Sessions()
        sid = sessions.create("user-token", USER, 60)
        reader = Mock()
        reader.my_sponsorships.side_effect = CommandError(503, "sponsorship service unavailable")
        with patch.object(app_module, "Tasks", return_value=reader):
            client = TestClient(
                app_module.create_app(settings(), oauth=FakeOAuth(), vault=Vault(None), sessions=sessions),
                cookies={app_module.SESSION_COOKIE: sid},
            )
            response = client.get("/api/me/sponsorships")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "sponsorship service unavailable"})

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
        reader.issue_view.side_effect = lambda repository, issue: {
            "issue": {"sponsorships": service.sponsorships("user-token", repository, issue)}
        }
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
            reader.issue_view.assert_called_with("owner/project", 7)
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
            sponsorships=lambda repository: {
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


class PortalProjectionTests(unittest.TestCase):
    """Projection contracts never infer personal payment or require task state."""

    def test_unavailable_projection_never_invents_organization_funding(self):
        """An outage cannot claim that an unknown current payer is the org pool."""
        reader = Tasks(
            settings(), Mock(), USER, sponsorships=Mock(side_effect=CommandError(503, "service unavailable"))
        )
        self.assertEqual(
            reader.sponsorship_view("owner/project", 7), {"funding": "unknown", "status": "unavailable", "slots": {}}
        )

    def test_issue_view_and_my_list_use_stable_actor_and_no_checkpoint(self):
        """Pre-task views retain pending state; matching a login is insufficient for ownership."""
        access = Mock()
        access.transport.return_value.request.return_value = access.get.return_value = {
            "number": 7,
            "title": "Intent",
            "body": "Description",
            "user": {"login": "octo"},
            "labels": [],
            "created_at": "2026-01-01Z",
            "updated_at": "2026-01-02Z",
        }
        projection = {"status": "provisional", "slots": {"openai": {"sponsor_id": str(USER.id), "login": USER.login}}}
        fetch = Mock(return_value=projection)
        reader = Tasks(settings(), access, USER, sponsorships=fetch)
        with (
            patch.object(reader, "find_target", return_value=(SimpleNamespace(upstream="owner/project"), {})),
            patch.object(reader, "target_record", return_value={"mode": "manual"}),
            patch.object(reader, "state") as checkpoint,
        ):
            result = reader.issue_view("owner/project", 7)
            checkpoint.assert_not_called()
        self.assertNotIn("stage", result["issue"])
        self.assertEqual(result["issue"]["sponsorships"]["status"], "provisional")
        fetch.assert_called_once_with("owner/project", 7)
        own = result["issue"]
        other = {**own, "issue": 8, "sponsorships": {"slots": {"openai": {"sponsor_id": "999", "login": USER.login}}}}
        history = {
            **own,
            "issue": 9,
            "sponsorships": {"slots": {}, "facts": [{"actor_id": str(USER.id), "command": "withdraw"}]},
        }
        with patch.object(reader, "overview", return_value={"tasks": [other], "issues": [own, history]}):
            self.assertEqual([i["issue"] for i in reader.my_sponsorships()], [7, 9])

    def test_issue_permission_is_independent_of_the_target_fork(self):
        """Portal sponsorship follows upstream authority independently of fork permissions."""
        access = Mock()
        access.transport.return_value.request.return_value = access.get.return_value = {
            "number": 7,
            "title": "Intent",
            "body": "",
            "user": {"login": "octo"},
            "labels": [{"name": STAGE_LABEL_PREFIX + "intake"}],
            "created_at": "2026-01-01Z",
            "updated_at": "2026-01-02Z",
        }
        access.config.return_value = ({}, "main")
        for fork_writer, issue_writer in ((False, True), (True, False)):
            access.repository.return_value = {"permissions": {"push": issue_writer}}
            access.writable.return_value = issue_writer
            reader = Tasks(settings(), access, USER)
            with (
                patch.object(
                    reader,
                    "find_target",
                    return_value=(SimpleNamespace(fork="fork/repo", upstream="upstream/repo"), {}),
                ),
                patch.object(reader, "target_record", return_value={"mode": "manual", "writer": fork_writer}),
                patch.object(reader, "state", return_value=None),
            ):
                self.assertEqual(reader.issue_view("upstream/repo", 7)["issueWriter"], issue_writer)
                self.assertEqual(reader.task_view("upstream/repo", 7)["issueWriter"], issue_writer)
            access.writable.assert_called_with("upstream/repo", USER.login, USER.id)

    def test_stale_intent_issue_statuses_do_not_hide_unrelated_issues_or_outages(self):
        """Only moved/gone issues are skipped; permission errors and transient failures stay visible."""
        issue = {
            "number": 8,
            "title": "Visible intent",
            "user": {"login": "octo"},
            "labels": [],
            "created_at": "2026-01-01Z",
            "updated_at": "2026-01-02Z",
        }
        target = SimpleNamespace(issue_repositories=("owner/project",), upstream="up/project")
        projection = lambda repository: {
            "issues": [{"issue": n, "status": "confirmed", "slots": {"openai": {"sponsor_id": "42"}}} for n in (7, 8)]
        }
        for status in (301, 410, 403):
            access = Mock()
            access.task_issues.return_value = []
            access.get.side_effect = lambda repository, path: (
                (_ for _ in ()).throw(
                    RuntimeError(f"GitHub request failed: GET repos/{repository}/{path} (HTTP {status})")
                )
                if path == "issues/7"
                else issue
            )
            reader = Tasks(settings(), access, USER, sponsorships=projection)
            with patch.object(reader, "pulls_by_issue", return_value={}):
                if status == 403:
                    with self.assertRaises(RuntimeError):
                        reader.listed(target, {"mode": "manual"})
                else:
                    self.assertEqual(reader.listed(target, {"mode": "manual"}), [])
                    self.assertEqual([i["issue"] for i in reader.issue_intents], [8])
        access.get.side_effect = Transient("unavailable")
        with patch.object(reader, "pulls_by_issue", return_value={}):
            with self.assertRaises(Transient):
                reader.listed(target, {"mode": "manual"})

    def test_empty_and_legacy_configurations_keep_target_listing(self):
        """Actual model-config/policy helpers raise caught errors or resolve legacy settings, never Blocked."""
        reader = Tasks(settings(), Mock(), USER)
        target = SimpleNamespace(fork="owner/project", upstream="up/project")
        with patch.object(reader, "config", return_value=({}, "main")):
            self.assertIsNone(reader.target_record(target, {})["credentialRequirements"])
        legacy = {
            "qc": "claude",
            "qc_model": None,
            "planner": "codex",
            "implementer": "claude",
            "reviewers": ["codex", "claude"],
            "models": {"codex": "model-o", "claude": "model-a"},
        }
        with patch.object(reader, "config", return_value=(legacy, "main")):
            requirements = reader.target_record(target, {})["credentialRequirements"]
            self.assertTrue(requirements)
            self.assertEqual({route["model"] for route in requirements}, {"model-o", "model-a"})
