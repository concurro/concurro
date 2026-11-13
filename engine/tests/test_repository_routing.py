"""Issue artifacts, upstream source/reviews and fork candidates use separate clients."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, TARGET, setUpModule as setUpModule, tearDownModule as tearDownModule

from controller import Controller
from fakes import FakeAgents, FakeGitHub


class RepositoryRoutingTests(unittest.TestCase):
    """A routing mistake cannot silently read another repository's same-numbered object."""

    def test_source_reads_and_pr_links(self):
        """Base files come from upstream; candidate files and the issue's links stay independently bound."""
        config = {**deepcopy(CONFIG), "upstream": "source/project", "fork": "owner/project"}
        issue, upstream, fork = (FakeGitHub(config) for _ in range(3))
        upstream.trees[upstream.base]["docs/guide.md"] = "upstream base"
        fork.trees["c" * 40] = {"docs/guide.md": "fork candidate"}
        controller = Controller(issue, FakeAgents(), config, target_root=TARGET, code=fork, pulls=upstream)
        controller.state = {"issue": 7, "base": upstream.base}
        self.assertEqual(controller.files(upstream.base, ["docs/guide.md"]), {"docs/guide.md": "upstream base"})
        self.assertEqual(controller.files("c" * 40, ["docs/guide.md"]), {"docs/guide.md": "fork candidate"})
        self.assertEqual(controller.pr_url(9), "https://github.com/source/project/pull/9")
        self.assertIs(controller.gh, issue)

    def test_writer_checks_use_upstream_and_recheck_each_invocation(self):
        """Fork-only writers cannot enroll or command tasks; upstream writers need no fork permission."""
        config = {**deepcopy(CONFIG), "upstream": "source/project", "fork": "owner/project"}
        issue, upstream, fork = (FakeGitHub(config) for _ in range(3))
        for permitted in (False, True, False):
            upstream.authorized = permitted
            issue.authorized = fork.authorized = not permitted
            controller = Controller(issue, FakeAgents(), config, target_root=TARGET, code=fork, pulls=upstream)
            controller.issue = issue.issue
            with patch.object(upstream, "permitted", wraps=upstream.permitted) as check:
                self.assertEqual(controller.permitted("writer"), permitted)
                self.assertEqual(bool(controller.enrollment()), permitted)
                check.assert_called_once_with("writer")

    def test_fork_draft_pr_recovers_then_becomes_ready_upstream(self):
        """Separate issue artifacts, fork objects and upstream PR writes survive a lost PR response."""
        config = {**deepcopy(CONFIG), "upstream": "source/project", "fork": "owner/project", "autonomous": False}
        issue, upstream, fork = (FakeGitHub(config) for _ in range(3))
        upstream.publication_source = fork
        issue.authorized = fork.authorized = False
        agents = FakeAgents()

        def tick():
            controller = Controller(issue, agents, config, target_root=TARGET, code=fork, pulls=upstream)
            controller.advance(7)
            return controller

        for _ in range(30):
            controller = tick()
            if controller.state["stage"] == "planned":
                issue.comment("/concurro approve " + controller.state["plan_id"])
            if controller.state["stage"] == "publish":
                break
        else:
            self.fail("implementation never completed")
        original = upstream.repo
        posted = []

        def lose(path, method="GET", data=None):
            result = original(path, method, data)
            if path == "pulls" and method == "POST":
                posted.append(deepcopy(data))
                raise RuntimeError("lost PR response")
            return result

        with patch.object(upstream, "repo", side_effect=lose), self.assertRaises(RuntimeError):
            tick()
        self.assertTrue(upstream.pr["draft"])
        self.assertEqual(posted[0]["head_repo"], "project")
        self.assertTrue(posted[0]["head"].startswith("owner:concurro/issue-7-"))
        for _ in range(5):
            controller = tick()
        self.assertEqual(controller.state["stage"], "gates")
        self.assertTrue(controller.state["handoff_ready"])
        self.assertEqual(upstream.ready_calls, 1)
        self.assertEqual(len(upstream.app_reviews), len(config["reviewers"]))
        self.assertIsNone(issue.pr)
        self.assertIsNone(fork.pr)
        self.assertFalse(upstream.refs)
        self.assertFalse(issue.refs)
        self.assertEqual(len(fork.refs), 1)
        self.assertFalse(fork.linked_branches)
        self.assertIn("Closes https://github.com/" + config["repository"] + "/issues/7", upstream.pr["body"])
        self.assertEqual(sum("Work branch:" in c["body"] for c in issue.comments), 1)
        self.assertEqual(upstream.merges + fork.merges + issue.merges, 0)


if __name__ == "__main__":
    unittest.main()
