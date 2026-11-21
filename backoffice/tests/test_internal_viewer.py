"""The internal viewer credential remains separate from browser command authority."""

import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backoffice.auth import Sessions, User
from backoffice.internal import create_internal_app
from backoffice.settings import Reader, Settings
from github import NotFound
from service.targets import Target


class FakeAccess:
    """A signed-in user's changing GitHub visibility for tier checks."""

    writer = True
    issue_visible = True

    def __init__(self, identity, token, credential=None):
        """Keep the session's token private to this authority."""
        assert token == "github-user-token"

    def repository(self, repository):
        """Both configured repositories are visible in the test."""
        return {"full_name": repository}

    def writable(self, repository, login, user_id):
        """Model loss of writer permission without ending the session."""
        return self.writer

    def transport(self, repository, as_user=False):
        """Resolve the issue through the user's own GitHub credential."""
        return self

    def request(self, method, path):
        """A hidden issue gives no reader tier."""
        if not self.issue_visible:
            raise NotFound("hidden")
        return {"number": 7}


class ViewerAuthorityTests(unittest.TestCase):
    """Check both credentials and every viewer tier."""

    def test_viewer_token_is_read_only_and_loses_access_on_logout(self):
        targets = (Target("up/project", "owner/project", "fork"),)
        settings = Settings(
            public_url="https://host.invalid/concurro",
            client_id="client",
            client_secret="secret",
            targets=targets,
            reader=Reader(1, Path("/unused"), targets),
        )
        sessions = Sessions()
        sid = sessions.create(
            "github-user-token",
            User(42, "octo", "Octo Cat", "https://avatars.invalid/42"),
            3600,
        )
        viewer = sessions.get(sid).viewer_token
        with patch("backoffice.internal.Access", FakeAccess):
            client = TestClient(create_internal_app(settings, sessions, object(), "read-service"))
            url = "/internal/v1/viewer?upstream=up/project&issue_repository=owner/project&issue_number=7"
            headers = {"authorization": "Bearer read-service", "x-concurro-viewer": viewer}
            self.assertEqual(client.get(url, headers=headers).json()["tier"], "writer")
            FakeAccess.writer = False
            self.assertEqual(client.get(url, headers=headers).json()["tier"], "reader")
            FakeAccess.issue_visible = False
            self.assertEqual(client.get(url, headers=headers).json()["tier"], "none")
            self.assertEqual(client.get(url, headers={**headers, "x-concurro-viewer": sid}).status_code, 401)
            self.assertEqual(client.get(url, headers={"x-concurro-viewer": viewer}).status_code, 401)
            self.assertEqual(client.post("/internal/v1/requests", headers=headers).status_code, 404)
            sessions.end(sid)
            self.assertEqual(client.get(url, headers=headers).status_code, 401)
        FakeAccess.writer = True
        FakeAccess.issue_visible = True
