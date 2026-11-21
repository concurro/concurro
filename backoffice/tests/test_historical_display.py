"""The migration bridge exports the retained verified projectors, never active tasks."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backoffice/tests"), str(ROOT / "service/tests")]
import test_review_tasks
import test_task_view

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from backoffice.auth import AuthError
from checkpoints import Checkpoints
from deploy import export_historical_display as exporter
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

    def test_cli_verifies_identity_outside_the_repository_transport(self):
        """The real OAuth identity read precedes unchanged repository-scoped access checks."""
        fixture = test_task_view.TaskViewTests()
        fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token, output = root / "token", root / "manifest.json"
                token.write_text("user-token\n")
                requests = []

                def account(request):
                    """Answer only the authenticated-user read with a verified synthetic account."""
                    requests.append(request)
                    self.assertEqual((request.method, str(request.url)), ("GET", "https://api.github.com/user"))
                    self.assertEqual(request.headers["authorization"], "Bearer user-token")
                    return httpx.Response(200, json={"id": 777, "login": "writer", "name": "Verified account"})

                def project(reader, deployment, observed_at, inventory):
                    """Exercise actual repository transports after accepting GitHub's account identity."""
                    self.assertEqual(
                        (reader.user.id, reader.user.login, reader.user.name), (777, "writer", "Verified account")
                    )
                    reader.find_target(KEY[0])
                    with self.assertRaisesRegex(ValueError, "outside the transport's repository"):
                        reader.access.transport(KEY[0], as_user=True).request("GET", "user")
                    return {"format": 1, "deployment": deployment, "views": []}

                access = fixture.tasks().access
                with (
                    httpx.Client(transport=httpx.MockTransport(account)) as client,
                    patch("backoffice.auth.httpx.Client", return_value=client),
                    patch.object(exporter, "load", return_value=fixture.settings),
                    patch.object(exporter, "Identity", return_value=test_task_view.ReaderApp()),
                    patch.object(
                        exporter,
                        "Access",
                        side_effect=lambda identity, token: type(access)(identity, token, fixture.wire.connection),
                    ),
                    patch.object(exporter, "export", side_effect=project),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "export-historical",
                            "--settings",
                            "settings.json",
                            "--user-token-file",
                            str(token),
                            "--deployment",
                            "cli-fixture",
                            str(output),
                        ],
                    ),
                ):
                    exporter.main()
                self.assertEqual(len(requests), 1)
                self.assertEqual(json.loads(output.read_text())["deployment"], "cli-fixture")
                self.assertIn(("Bearer user-token", f"repos/{KEY[0]}"), fixture.wire.reads)
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_cli_rejects_an_unverified_token_before_export(self):
        """A refused identity cannot become caller-supplied authority or produce a manifest."""
        fixture = test_task_view.TaskViewTests()
        fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token, output = root / "token", root / "manifest.json"
                token.write_text("synthetic-refused-token")
                for status in (401, 502):
                    with (
                        self.subTest(status=status),
                        httpx.Client(
                            transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"message": "refused"}))
                        ) as client,
                        patch("backoffice.auth.httpx.Client", return_value=client),
                        patch.object(exporter, "load", return_value=fixture.settings),
                        patch.object(exporter, "export") as project,
                        patch.object(
                            sys,
                            "argv",
                            [
                                "export-historical",
                                "--settings",
                                "settings.json",
                                "--user-token-file",
                                str(token),
                                "--deployment",
                                "cli-fixture",
                                str(output),
                            ],
                        ),
                    ):
                        with self.assertRaises(AuthError) as error:
                            exporter.main()
                        self.assertNotIn(token.read_text(), str(error.exception))
                        project.assert_not_called()
                        self.assertFalse(output.exists())
        finally:
            fixture.tearDown()
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
