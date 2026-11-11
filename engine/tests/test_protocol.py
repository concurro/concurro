"""Literal wire-format contracts for shared URLs, markers and hexadecimal patterns."""

import ast
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import ENGINE

import actions
import agents
import controller
import conversation
import mirror
import policy
import repository


class ExistingProtocolTests(unittest.TestCase):
    """Pin public values and repository entry points before extracting their definitions."""

    def test_public_marker_and_comment_template(self):
        """Existing imports retain their exact marker and named-format template."""
        self.assertEqual(conversation.MARKER, "<!-- concurro:event:v1:")
        self.assertEqual(actions.COMMENT_URL, "https://github.com/{repository}/issues/{issue}#issuecomment-{comment}")
        self.assertEqual(
            actions.COMMENT_URL.format(repository="owner/project", issue=21, comment=42),
            "https://github.com/owner/project/issues/21#issuecomment-42",
        )

    def test_public_compiled_patterns(self):
        """Compiled exports retain exact pattern text, flags and lowercase-only matching."""
        for pattern, literal, width in (
            (agents.SCOPE, r"[a-f0-9]{64}", 64),
            (controller.COMMIT, r"[0-9a-f]{40}", 40),
            (mirror.SHA, r"[0-9a-f]{40}", 40),
            (repository.SHA, r"[0-9a-f]{40}", 40),
        ):
            with self.subTest(pattern=literal, width=width):
                self.assertEqual(pattern.pattern, literal)
                self.assertEqual(pattern.flags, re.UNICODE)
                self.assertIsNotNone(pattern.fullmatch("a" * width))
                for value in ("A" * width, "g" * width, "a" * (width - 1), "a" * (width + 1), "a" * width + "\n"):
                    self.assertIsNone(pattern.fullmatch(value))
        self.assertEqual(policy.IMAGE.pattern, r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}")
        self.assertEqual(policy.IMAGE.flags, re.UNICODE)
        self.assertEqual(
            actions.BLOCKED_HEADER.pattern, r"<!-- concurro:event:v1:[a-f0-9]{64} -->\n### (?:🛑 )?Blocked\n"
        )
        self.assertEqual(actions.BLOCKED_HEADER.flags, re.UNICODE)
        body = "<!-- concurro:event:v1:" + "a" * 64 + " -->\n### Blocked\nReason."
        self.assertIsNotNone(actions.BLOCKED_HEADER.match(body))
        self.assertIsNone(actions.BLOCKED_HEADER.fullmatch(body))
        self.assertIsNone(actions.BLOCKED_HEADER.match("prefix" + body))

    def test_repository_import_does_not_change_path(self):
        """Importing repository in-process must leave the caller's search path intact."""
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); before = list(sys.path); "
            "import repository; assert sys.path == before; assert repository.SHA.fullmatch('a' * 40)"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script, str(ENGINE)],
                capture_output=True,
                text=True,
                cwd=directory,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class SharedProtocolTests(unittest.TestCase):
    """Specify every new helper against literal output rather than another renderer."""

    def test_artifact_url(self):
        """An omitted artifact number leaves a trailing slash; zero is not omitted."""
        from protocol import artifact_url

        self.assertEqual(artifact_url("owner/project", 21), "https://github.com/owner/project/issues/21/artifacts/")
        self.assertEqual(artifact_url("owner/project", 21, 3), "https://github.com/owner/project/issues/21/artifacts/3")
        self.assertEqual(artifact_url("owner/project", 21, 0), "https://github.com/owner/project/issues/21/artifacts/0")

    def test_comment_url(self):
        """Comment links preserve the issue-local fragment and do no URL normalization."""
        from protocol import comment_url

        self.assertEqual(
            comment_url("owner/project", 21, 42), "https://github.com/owner/project/issues/21#issuecomment-42"
        )

    def test_issue_url(self):
        """Issue links use the supplied repository and identifier verbatim."""
        from protocol import issue_url

        self.assertEqual(issue_url("owner/project", 21), "https://github.com/owner/project/issues/21")

    def test_clone_url(self):
        """Clones retain the HTTPS prefix and git suffix without escaping repository text."""
        from protocol import clone_url

        self.assertEqual(clone_url("owner/project"), "https://github.com/owner/project.git")

    def test_shared_patterns_and_marker(self):
        """The leaf vocabulary keeps the exact strings and fullmatch semantics."""
        from protocol import DIGEST_PATTERN, EVENT_MARKER, SHA_PATTERN

        self.assertEqual(EVENT_MARKER, "<!-- concurro:event:v1:")
        self.assertEqual(DIGEST_PATTERN, r"[a-f0-9]{64}")
        self.assertEqual(SHA_PATTERN, r"[0-9a-f]{40}")
        for pattern, width in ((DIGEST_PATTERN, 64), (SHA_PATTERN, 40)):
            self.assertIsNotNone(re.fullmatch(pattern, "1a" * (width // 2)))
            for value in ("A" * width, "g" * width, "a" * (width - 1), "a" * (width + 1), "a" * width + "\n"):
                self.assertIsNone(re.fullmatch(pattern, value))

    def test_leaf_has_no_engine_imports(self):
        """Protocol definitions cannot acquire dependencies that recreate engine import cycles."""
        import protocol

        tree = ast.parse(Path(protocol.__file__).read_text())
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)))
