"""Repository tools, Git snapshots, the tool container boundary and the isolated Docker transport."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import transport_size

from controller import MAX_DIFF, Controller, changes
from fakes import CONFIG, FakeAgents, FakeGitHub
from gitfixture import Upstream, build, mirror_of
from policy import Blocked, digest
from repository import source_lines
from snapshot import inspection


class GitSnapshotTests(unittest.TestCase):
    """Snapshots hold real history under real branch names, and candidates keep exact bytes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.upstream = Upstream(self.root / "upstream.git")
        self.base = self.upstream.commit(
            {
                "docs/guide.md": "Guide\n",
                "tool.sh": "#!/bin/sh\n",
                ".gitattributes": "* binary\n*.md text eol=crlf filter=secret -diff\n",
            },
            branch="main",
            message="base",
            executable=("tool.sh",),
        )
        self.head = self.upstream.commit(
            {"docs/guide.md": "Guide\nMore\n"}, parent=self.base, branch="concurro/issue-1", message="head"
        )
        self.mirror = mirror_of(self.upstream, self.root / "mirrors")
        self.refs = {"main": self.base, "concurro/issue-1": self.head}

    def test_real_refs_history_and_diffs_by_branch_name(self):
        repo = build(self.mirror, self.root / "snap", self.refs, "concurro/issue-1")
        self.assertEqual(repo.read("docs/guide.md"), "Guide\nMore\n")
        diff = repo.git(["diff", "main...concurro/issue-1"])
        self.assertIn("+More", diff["output"])
        self.assertEqual(diff["exit_code"], 0)
        self.assertEqual(repo.git(["log", "--format=%s", "concurro/issue-1"])["output"].split(), ["head", "base"])
        self.assertIn("head", repo.git(["blame", "--porcelain", "docs/guide.md"])["output"])
        self.assertEqual(repo.git(["rev-parse", "HEAD"])["output"].strip(), self.head)

    def test_attributes_neither_hide_nor_normalize_a_change(self):
        """In-tree attributes mark everything binary, CRLF and filtered; the snapshot's own attributes win."""
        repo = build(self.mirror, self.root / "snap", self.refs, "concurro/issue-1")
        self.assertIn("+More", repo.git(["diff", "main", "concurro/issue-1", "--", "docs/guide.md"])["output"])
        self.assertEqual((self.root / "snap/docs/guide.md").read_bytes(), b"Guide\nMore\n")
        self.assertIn("+More", repo.git(["show", "concurro/issue-1"])["output"])

    def test_candidate_commit_keeps_exact_bytes_and_modes(self):
        files = {
            "docs/guide.md": "Guide\r\nCandidate\r\n",
            "tool.sh": "#!/bin/sh\necho hi\n",
            "docs/new.md": "New\n",
            "docs/absent.md": None,
        }
        repo = build(
            self.mirror,
            self.root / "snap",
            {**self.refs, "concurro/published": self.head},
            "concurro/issue-1",
            candidate={"files": files},
        )
        self.assertEqual((self.root / "snap/docs/guide.md").read_bytes(), b"Guide\r\nCandidate\r\n")
        blob = repo.git(["cat-file", "-p", "concurro/issue-1:docs/guide.md"])["output"]
        self.assertEqual(blob, "Guide\r\nCandidate\r\n")
        self.assertIn("100755", repo.git(["ls-tree", "concurro/issue-1", "--", "tool.sh"])["output"])
        self.assertFalse((self.root / "snap/docs/absent.md").exists())
        self.assertEqual(repo.git(["rev-parse", "concurro/issue-1^"])["output"].strip(), self.head)
        self.assertIn("+Candidate", repo.git(["diff", "concurro/published", "concurro/issue-1"])["output"])

    def test_candidates_and_refs_are_validated(self):
        for refs, head, candidate in (
            ({"main": self.base}, "other", None),
            ({"main": "f" * 40}, "main", None),
            ({"bad..name": self.base}, "bad..name", None),
            ({"main": self.base, "x.lock": self.head}, "main", None),
            ({**self.refs}, "concurro/issue-1", {"files": {"../escape": "x"}}),
            ({**self.refs}, "concurro/issue-1", {"files": {".concurro/config.yaml": "x"}}),
        ):
            with (
                self.subTest(refs=refs, head=head, candidate=candidate is not None),
                tempfile.TemporaryDirectory() as directory,
                self.assertRaises((ValueError, Blocked)),
            ):
                build(self.mirror, Path(directory), refs, head, candidate)

    def test_pathspecs_are_literal(self):
        repo = build(self.mirror, self.root / "snap", self.refs, "concurro/issue-1")
        self.assertEqual(repo.git(["log", "--format=%s", "--", "docs/*.md"])["output"], "")
        self.assertIn("head", repo.git(["log", "--format=%s", "--", "docs/guide.md"])["output"])


class TransportTests(unittest.TestCase):
    """Snapshot preparation and the refs each stage inspects."""

    def test_inspection_requires_refs_and_candidates_only_for_candidate_stages(self):
        refs = {"refs": {"main": "a" * 40}, "head": "main"}
        self.assertIsNone(inspection(refs, None, "review", "main")["candidate"])
        self.assertEqual(inspection(refs, {"x": "y"}, "develop", "main")["candidate"], {"files": {"x": "y"}})
        for repository_refs, candidate, stage in (
            (None, None, "plan"),
            ({"refs": {"main": "main"}, "head": "main"}, None, "plan"),
            ({"refs": {"main": "a" * 40}, "head": "other"}, None, "plan"),
            (refs, None, "local_review"),
            (refs, {"x": "y"}, "review"),
        ):
            with self.subTest(stage=stage), self.assertRaises(Blocked):
                inspection(repository_refs, candidate, stage, "main")

    def test_controller_binds_refs_per_stage(self):
        """Models cannot substitute the revision; candidates are committed on the task branch."""
        agents = FakeAgents()
        controller = Controller(FakeGitHub(CONFIG), agents, CONFIG)
        controller.state = {
            "base": "a" * 40,
            "calls": 0,
            "issue": 7,
            "requirements": {},
            "branch": "concurro/issue-7",
            "bundle_id": controller.bundle.identity,
            "charter_id": digest(controller.bundle.charter),
            "agent_image": "image",
            "config_digest": "config",
        }
        with patch.object(controller, "save"), patch.object(controller, "checkpoint"):
            controller.ask("claude", "plan", {"repository_commit": "f" * 40, "repository": {"refs": {}, "head": "x"}})
            self.assertEqual(agents.calls[-1][2]["repository"], {"refs": {"main": "a" * 40}, "head": "main"})
            controller.ask("codex", "develop", {"controller_candidate": {}})
            self.assertEqual(
                agents.calls[-1][2]["repository"],
                {"refs": {"main": "a" * 40, "concurro/issue-7": "a" * 40}, "head": "concurro/issue-7"},
            )
            controller.state["head"] = "b" * 40
            controller.ask("claude", "review", {})
            self.assertEqual(
                agents.calls[-1][2]["repository"],
                {"refs": {"main": "a" * 40, "concurro/issue-7": "b" * 40}, "head": "concurro/issue-7"},
            )
            controller.ask("claude", "review", {"controller_candidate": {}})
            self.assertEqual(agents.calls[-1][2]["repository"]["refs"]["concurro/published"], "b" * 40)
            self.assertEqual(agents.calls[-1][2]["repository_commit"], "b" * 40)


class ReviewContextTests(unittest.TestCase):
    """Reviewers get diffs and digests, not whole files; controller-only keys never reach a model."""

    def test_changes_describe_each_file_with_digests_and_counts(self):
        result = changes(
            {"a.md": "x\n--old\n", "gone.md": "g\n", "same.md": "s\n"},
            {"a.md": "x\n++new", "new.md": "n\n", "same.md": "s\n"},
        )
        self.assertEqual(
            [(c["path"], c["status"]) for c in result],
            [("a.md", "modified"), ("gone.md", "deleted"), ("new.md", "added")],
        )
        modified = result[0]
        self.assertEqual((modified["added"], modified["removed"]), (1, 1))
        self.assertIn("+++new", modified["diff"])
        self.assertIn("\\ No newline at end of file", modified["diff"])
        self.assertEqual(modified["base_sha256"], hashlib.sha256(b"x\n--old\n").hexdigest())
        self.assertIsNone(result[2]["base_sha256"])
        self.assertIsNone(result[1]["head_sha256"])

    def test_a_large_file_with_a_small_change_stays_small(self):
        before = "".join(f"line {i}\n" for i in range(20000))
        after = before.replace("line 10000\n", "line ten thousand\n")
        self.assertGreater(len(before.encode()), 150000)
        [change] = changes({"big.rs": before}, {"big.rs": after})
        self.assertLess(len(change["diff"].encode()), 1000)
        self.assertFalse(change["truncated"])
        [huge] = changes({"big.rs": before}, {"big.rs": after.replace("line", "LINE")})
        self.assertTrue(huge["truncated"])
        self.assertLess(len(huge["diff"].encode()), MAX_DIFF + 200)
        self.assertTrue(huge["diff"].endswith("[diff truncated; inspect the pinned repository]\n"))

    def test_hunks_count_lines_as_git_and_the_reader_do(self):
        """Form feeds and Unicode separators stay inside their LF-delimited line."""
        before, after = "a\u2028b\fc\nkeep\nold\n", "a\u2028b\fc\nkeep\nnew\n"
        [change] = changes({"s.md": before}, {"s.md": after})
        self.assertIn("@@ -1,3 +1,3 @@", change["diff"])
        self.assertNotIn("No newline", change["diff"])
        self.assertEqual((change["added"], change["removed"]), (1, 1))
        self.assertEqual(source_lines(after).index("new") + 1, 3)
        [unterminated] = changes({"t.md": "x\u2028y"}, {"t.md": "x\u2028z"})
        self.assertIn("@@ -1 +1 @@", unterminated["diff"])
        self.assertEqual(unterminated["diff"].count("No newline at end of file"), 2)

    def test_controller_only_keys_are_not_measured(self):
        context = {"changes": [], "controller_candidate": {"big.rs": "x" * 500000}}
        self.assertLess(transport_size(context), 100)


if __name__ == "__main__":
    unittest.main()
