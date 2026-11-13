"""Resolve CI-published releases without a GitHub account or live network requests."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy import releases  # noqa: E402

SERVICE = "a" * 40
WEB = "b" * 40


def run(sha, status="completed", conclusion="success"):
    """Build the relevant fields of one main push workflow response."""
    return {
        "workflow_runs": [
            {
                "head_sha": sha,
                "event": "push",
                "head_branch": "main",
                "status": status,
                "conclusion": conclusion,
                "html_url": "https://github.com/example/actions/runs/1",
            }
        ]
    }


class ReleaseTests(unittest.TestCase):
    """Keep release identities fixed while CI is pending or has not appeared yet."""

    def test_both_heads_are_pinned_before_waiting_for_ci(self):
        """New pushes during a wait cannot change either selected release."""
        responses = [
            {"sha": SERVICE},
            {"sha": WEB},
            {"workflow_runs": []},
            run(WEB, "in_progress", None),
            run(SERVICE),
            run(WEB),
        ]
        with (
            patch.object(releases, "github", side_effect=responses) as github,
            patch.object(releases.time, "sleep") as sleep,
        ):
            self.assertEqual(releases.resolve(), [SERVICE, WEB])
        paths = [call.args[0] for call in github.call_args_list]
        self.assertEqual(paths[:2], [repository + "/commits/main" for repository in releases.REPOSITORIES])
        self.assertEqual(sum("/commits/" in path for path in paths), 2)
        self.assertTrue(all("event=push&branch=main" in path for path in paths[2:]))
        self.assertIn("head_sha=" + SERVICE, paths[2])
        self.assertIn("head_sha=" + WEB, paths[3])
        sleep.assert_called_once_with(30)

    def test_explicit_releases_still_require_successful_matching_main_ci(self):
        """Explicit SHAs do not bypass CI checks or allow a workflow for a different commit."""
        for response in (run(SERVICE, conclusion="failure"), run(WEB)):
            with self.subTest(response=response), patch.object(releases, "github", return_value=response):
                with self.assertRaises(SystemExit):
                    releases.resolve([SERVICE, WEB])
        with patch.object(releases, "github", side_effect=[run(SERVICE), run(WEB)]) as github:
            self.assertEqual(releases.resolve([SERVICE, WEB]), [SERVICE, WEB])
        self.assertTrue(all("/commits/" not in call.args[0] for call in github.call_args_list))

    def test_invalid_arguments_do_not_call_github(self):
        """Reject incomplete or malformed pinned pairs before contacting any service."""
        with patch.object(releases, "github") as github:
            for arguments in ([SERVICE], [SERVICE, "main"], [SERVICE, WEB, SERVICE]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    releases.resolve(arguments)
        github.assert_not_called()

    def test_api_failure_does_not_expose_credentials(self):
        """HTTP failures report only the status, never the API response or token."""
        with (
            patch.dict(releases.os.environ, {"GH_TOKEN": "private-token"}),
            patch.object(
                releases, "urlopen", side_effect=HTTPError("https://api.github.com", 403, "private-token", {}, None)
            ),
        ):
            with self.assertRaises(SystemExit) as failed:
                releases.github("concurro/concurro/commits/main")
        self.assertIn("HTTP 403", str(failed.exception))
        self.assertNotIn("private-token", str(failed.exception))
