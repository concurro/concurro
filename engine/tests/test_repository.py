"""Repository tools, Git snapshots, the tool container boundary and the isolated Docker transport."""

import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # unittest module fixtures
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from checks import MAX_OVERLAY_FILES
from controller import MAX_DIFF, changes, transport_size
from gitfixture import Upstream, build, mirror_of
from policy import Blocked, digest, path_allowed
import repository
from repository import Repository, TOOL_NAMES, ToolTimeout, relay, run_git, serve
from snapshot import inspection
from test_pipeline import CONFIG, Controller, FakeAgents, FakeGitHub


class RepositoryTests(unittest.TestCase):
    """Read protection is distinct from edit permission and cannot reach host secrets."""

    def test_protected_reference_is_listed_readable_and_searchable(self):
        """Protected governance text is discoverable without edit authority."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".concurro").mkdir()
            (root / "CONTRIBUTING.md").write_text("Writer enrollment\nMerge disabled\n")
            (root / ".concurro/config.json").write_text("{}")
            repo = Repository(root)
            self.assertFalse(path_allowed("CONTRIBUTING.md", CONFIG))
            self.assertIn("CONTRIBUTING.md", json.loads(repo.call("list_files", {}))["paths"])
            self.assertEqual(json.loads(repo.call("read_file", {"path": "CONTRIBUTING.md"}))["lines"][0]["text"],
                             "Writer enrollment")
            self.assertEqual(json.loads(repo.call("search", {"query": "enrollment"}))["matches"][0]["path"],
                             "CONTRIBUTING.md")
            self.assertEqual(repo.read(".concurro/config.json"), "{}")

    def test_file_tools_reject_symlink_components_and_git_metadata(self):
        """The file tools never follow links or read Git state; the git tool is the way into history."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file").write_text("safe")
            (root / ".git").mkdir()
            (root / "link").symlink_to("/etc")
            repo = Repository(root)
            self.assertEqual(repo.paths, ["file"])
            for name in ("../secret", "/etc/passwd", "link/passwd", ".git/config", "file/../file", "./file"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    repo.read(name)

    def test_read_pagination_binary_rejection_and_budget(self):
        """Large output is paginated and repeated retrieval cannot run unbounded."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lines").write_text("\n".join(str(i) for i in range(250)))
            (root / "binary").write_bytes(b"secret\x00data")
            repo = Repository(root)
            first = json.loads(repo.call("read_file", {"path": "lines"}))
            self.assertEqual(first["next_offset"], 100)
            self.assertEqual(repo.read_file("lines", offset=100)["lines"][0]["text"], "100")
            with self.assertRaises(ValueError):
                repo.read("binary")
            with self.assertRaises(ValueError):
                repo.read_file("lines", limit=True)
            repo.calls = 80
            with self.assertRaises(ValueError):
                repo.call("list_files", {})

    def test_mcp_protocol_has_only_read_tools_and_sanitized_errors(self):
        """A real stdio handshake and calls expose the fixed read surface only."""
        with tempfile.TemporaryDirectory() as directory:
            requests = [
                {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"method": "notifications/initialized"}, {"id": 2, "method": "tools/list"},
                {"id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "/secret"}}},
                {"id": 4, "method": "tools/call", "params": {"name": "shell", "arguments": {"command": "touch /tmp/pwn"}}},
                {"id": 5, "method": "tools/call", "params": {"name": "list_files", "arguments": {}}},
            ]
            output = io.StringIO()
            serve(Repository(Path(directory)), io.StringIO("\n".join(map(json.dumps, requests))), output)
            results = list(map(json.loads, output.getvalue().splitlines()))
            self.assertEqual(results[0]["result"]["protocolVersion"], "2025-03-26")
            self.assertEqual([t["name"] for t in results[1]["result"]["tools"]], list(TOOL_NAMES))
            self.assertTrue(results[2]["result"]["isError"])
            self.assertTrue(results[3]["result"]["isError"])
            self.assertNotIn("/secret", output.getvalue())
            self.assertEqual(json.loads(results[4]["result"]["content"][0]["text"])["paths"], [])

    def test_mcp_recovers_after_malformed_messages(self):
        """Bad frames receive protocol errors without losing the next valid request."""
        frames = ["not-json", "[]", '{"id":1,"method":"initialize","params":null}',
                  '{"id":2,"method":"tools/call","params":[]}',
                  '{"id":3,"method":"tools/list"}']
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            serve(Repository(Path(directory)), io.StringIO("\n".join(frames)), output)
            replies = list(map(json.loads, output.getvalue().splitlines()))
            self.assertEqual([r["error"]["code"] for r in replies[:-1]], [-32700, -32600, -32602, -32602])
            self.assertEqual([t["name"] for t in replies[-1]["result"]["tools"]], list(TOOL_NAMES))

    def test_mcp_notifications_and_pings_do_not_spend_retrieval_budget(self):
        """More than 200 protocol messages must not terminate useful repository inspection."""
        frames = [{"method": "notifications/initialized"}] * 250
        frames += [{"id": n, "method": "ping"} for n in range(250)]
        frames += [{"id": 999, "method": "tools/call", "params": {"name": "list_files"}}]
        with tempfile.TemporaryDirectory() as directory:
            repo, output = Repository(Path(directory)), io.StringIO()
            serve(repo, io.StringIO("\n".join(map(json.dumps, frames))), output)
            replies = list(map(json.loads, output.getvalue().splitlines()))
            self.assertEqual(len(replies), 251)
            self.assertEqual(replies[-1]["id"], 999)
            self.assertNotIn("error", replies[-1])
            self.assertEqual(repo.calls, 1)
            repo.calls = 80
            with self.assertRaises(ValueError):
                repo.call("list_files", {})

    def test_read_and_search_use_source_line_numbers(self):
        """Unicode separators and form feeds stay on their LF-delimited source line."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").write_bytes("first\fsecond\u2028third\r\nfourth\nfifth".encode())
            repo = Repository(root)
            lines = repo.read_file("source")["lines"]
            self.assertEqual([line["line"] for line in lines], [1, 2, 3])
            self.assertEqual(lines[0]["text"], "first\fsecond\u2028third")
            self.assertEqual(repo.search("third")["matches"][0]["line"], 1)
            self.assertEqual(repo.search("fourth")["matches"][0]["line"], 2)
            self.assertEqual(repo.read_file("source", offset=2)["lines"][0]["text"], "fifth")


class GitSnapshotTests(unittest.TestCase):
    """Snapshots hold real history under real branch names, and candidates keep exact bytes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.upstream = Upstream(self.root / "upstream.git")
        self.base = self.upstream.commit({"docs/guide.md": "Guide\n", "tool.sh": "#!/bin/sh\n",
                                          ".gitattributes": "* binary\n*.md text eol=crlf filter=secret -diff\n"},
                                         branch="main", message="base", executable=("tool.sh",))
        self.head = self.upstream.commit({"docs/guide.md": "Guide\nMore\n"}, parent=self.base,
                                         branch="concurro/issue-1", message="head")
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
        files = {"docs/guide.md": "Guide\r\nCandidate\r\n", "tool.sh": "#!/bin/sh\necho hi\n", "docs/new.md": "New\n",
                 "docs/absent.md": None}
        repo = build(self.mirror, self.root / "snap", {**self.refs, "concurro/published": self.head},
                     "concurro/issue-1", candidate={"files": files})
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
                ({**self.refs}, "concurro/issue-1", {"files": {"CONTRIBUTING.md": "x"}}),
                ({**self.refs}, "concurro/issue-1", {"files": {f"docs/{i}.md": "x" for i in range(MAX_OVERLAY_FILES + 1)}})):
            with self.subTest(refs=refs, head=head, candidate=candidate is not None), \
                    tempfile.TemporaryDirectory() as directory, self.assertRaises((ValueError, Blocked)):
                build(self.mirror, Path(directory), refs, head, candidate)

    def test_git_tool_pages_bounds_and_refuses_writes(self):
        repo = build(self.mirror, self.root / "snap", self.refs, "concurro/issue-1")
        with patch.object(repository, "GIT_CAPTURE", 50):
            repo.outputs = {}
            capped = repo.git(["log", "--format=%H %s"])
        self.assertTrue(capped["truncated"])
        self.assertIsNone(capped["exit_code"])
        page = repo.git(["log", "--format=%H"], offset=41)
        self.assertEqual(page["output"].strip(), self.base)
        for args in (["push"], ["config", "x", "y"], ["-c", "a=b", "log"], [], ["log", 1], "log"):
            with self.subTest(args=args), self.assertRaises(ValueError):
                repo.git(args)

    def test_pathspecs_are_literal(self):
        repo = build(self.mirror, self.root / "snap", self.refs, "concurro/issue-1")
        self.assertEqual(repo.git(["log", "--format=%s", "--", "docs/*.md"])["output"], "")
        self.assertIn("head", repo.git(["log", "--format=%s", "--", "docs/guide.md"])["output"])

    def test_a_hung_git_program_times_out(self):
        """Free arguments can start programs; a hung one raises ToolTimeout, which ends the tool container."""
        repo = build(self.mirror, self.root / "snap", self.refs, "concurro/issue-1")
        with self.assertRaises(ToolTimeout):
            run_git(repo.root, ["grep", "--open-files-in-pager=sleep 5;", "Guide"], timeout=1)
        requests = [{"id": 1, "method": "tools/call", "params": {"name": "git", "arguments": {
            "args": ["grep", "--open-files-in-pager=sleep 5;", "Guide"]}}}]
        output = io.StringIO()
        with patch.object(repository, "GIT_TIMEOUT", 1), self.assertRaises(ToolTimeout):
            serve(repo, io.StringIO(json.dumps(requests[0])), output)
        self.assertTrue(json.loads(output.getvalue())["result"]["isError"])

    def test_listener_and_relay_carry_mcp_over_a_socket(self):
        build(self.mirror, self.root / "snap", self.refs, "concurro/issue-1")
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: Path(directory, "s").unlink(missing_ok=True))
        path = Path(directory) / "s"
        threading.Thread(target=repository.listen, args=(path, self.root / "snap", 3600), daemon=True).start()
        requests = [{"id": 1, "method": "tools/call", "params": {"name": "git", "arguments": {"args": ["diff", "main"]}}},
                    {"id": 2, "method": "tools/list"}]
        incoming = io.BytesIO("\n".join(map(json.dumps, requests)).encode() + b"\n")
        outgoing = io.BytesIO()
        relay(path, incoming, outgoing, wait=10)
        replies = [json.loads(line) for line in outgoing.getvalue().decode().splitlines()]
        self.assertIn("+More", json.loads(replies[0]["result"]["content"][0]["text"])["output"])
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]], list(TOOL_NAMES))


class TransportTests(unittest.TestCase):
    """Snapshot preparation and the refs each stage inspects."""


    def test_inspection_requires_refs_and_candidates_only_for_candidate_stages(self):
        refs = {"refs": {"main": "a" * 40}, "head": "main"}
        self.assertIsNone(inspection(refs, None, "review", "main")["candidate"])
        self.assertEqual(inspection(refs, {"x": "y"}, "develop", "main")["candidate"], {"files": {"x": "y"}})
        for repository_refs, candidate, stage in ((None, None, "plan"), ({"refs": {"main": "main"}, "head": "main"}, None, "plan"),
                                                  ({"refs": {"main": "a" * 40}, "head": "other"}, None, "plan"),
                                                  (refs, None, "local_review"), (refs, {"x": "y"}, "review")):
            with self.subTest(stage=stage), self.assertRaises(Blocked):
                inspection(repository_refs, candidate, stage, "main")

    def test_controller_binds_refs_per_stage(self):
        """Models cannot substitute the revision; candidates are committed on the task branch."""
        agents = FakeAgents()
        controller = Controller(FakeGitHub(CONFIG), agents, CONFIG)
        controller.state = {"base": "a" * 40, "calls": 0, "issue": 7, "requirements": {}, "branch": "concurro/issue-7",
                            "bundle_id": controller.bundle.identity, "charter_id": digest(controller.bundle.charter),
                            "agent_image": "image", "config_digest": "config"}
        with patch.object(controller, "save"), patch.object(controller, "checkpoint"):
            controller.ask("claude", "plan", {"repository_commit": "f" * 40, "repository": {"refs": {}, "head": "x"}})
            self.assertEqual(agents.calls[-1][2]["repository"], {"refs": {"main": "a" * 40}, "head": "main"})
            controller.ask("codex", "develop", {"controller_candidate": {}})
            self.assertEqual(agents.calls[-1][2]["repository"],
                             {"refs": {"main": "a" * 40, "concurro/issue-7": "a" * 40}, "head": "concurro/issue-7"})
            controller.state["head"] = "b" * 40
            controller.ask("claude", "review", {})
            self.assertEqual(agents.calls[-1][2]["repository"],
                             {"refs": {"main": "a" * 40, "concurro/issue-7": "b" * 40}, "head": "concurro/issue-7"})
            controller.ask("claude", "local_review", {"controller_candidate": {}})
            self.assertEqual(agents.calls[-1][2]["repository"]["refs"]["concurro/published"], "b" * 40)
            self.assertEqual(agents.calls[-1][2]["repository_commit"], "b" * 40)


class ReviewContextTests(unittest.TestCase):
    """Reviewers get diffs and digests, not whole files; controller-only keys never reach a model."""

    def test_changes_describe_each_file_with_digests_and_counts(self):
        result = changes({"a.md": "x\n--old\n", "gone.md": "g\n", "same.md": "s\n"},
                         {"a.md": "x\n++new", "new.md": "n\n", "same.md": "s\n"})
        self.assertEqual([(c["path"], c["status"]) for c in result],
                         [("a.md", "modified"), ("gone.md", "deleted"), ("new.md", "added")])
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
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "s.md").write_text(after)
            lines = Repository(Path(directory)).read_file("s.md")["lines"]
        self.assertEqual(next(line["line"] for line in lines if line["text"] == "new"), 3)
        [unterminated] = changes({"t.md": "x\u2028y"}, {"t.md": "x\u2028z"})
        self.assertIn("@@ -1 +1 @@", unterminated["diff"])
        self.assertEqual(unterminated["diff"].count("No newline at end of file"), 2)

    def test_controller_only_keys_are_not_measured(self):
        context = {"changes": [], "controller_candidate": {"big.rs": "x" * 500000}}
        self.assertLess(transport_size(context), 100)


if __name__ == "__main__":
    unittest.main()
