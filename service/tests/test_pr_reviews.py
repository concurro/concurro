"""The gateway admits only the App's own PR review writes: comment reviews, its own threads, its own PR."""

import unittest

from fixtures import KEY, ServiceFixture

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import reviews
from fakes import FakeAgents
from policy import task_branch
from service.gateway import Disallowed

BRANCH = task_branch(*KEY)


class PullRequestWrites(ServiceFixture, unittest.TestCase):
    """Writes on PR #9, which belongs to the task when its head is the engine's branch for issue 7."""

    def setUp(self):
        super().setUp()
        self.registry.bind(KEY, KEY[0], KEY[0], "a" * 40)
        self.fake.pr = {
            "user": {"login": self.config["bot_login"]},
            "number": 9,
            "state": "open",
            "head": {"sha": "c" * 40, "ref": BRANCH, "repo": {"full_name": KEY[0]}},
            "base": {"ref": "main", "sha": self.fake.base},
        }
        self.generation = self.gateway.acquire(KEY, "worker")

    def write(self, path, body):
        return self.gateway.mutate(KEY, self.generation, "POST", f"repos/{KEY[0]}/{path}", body)

    def refused(self, path, body):
        before = sum(len(self.registry.mutations(KEY, s)) for s in ("done", "failed", "unknown"))
        with self.assertRaises(Disallowed):
            self.write(path, body)
        after = sum(len(self.registry.mutations(KEY, s)) for s in ("done", "failed", "unknown"))
        self.assertEqual(after, before, "refused before admission")

    def review(self, event="COMMENT", comments=()):
        return {"commit_id": "c" * 40, "event": event, "body": "Reviewed.", "comments": list(comments)}

    def finding_comment(self, login=None, body=None):
        """Open a thread with one App finding comment, as a published review would."""
        comment = self.fake.review_comment(
            {"body": body or f"{reviews.FINDING_MARKER}F1 -->\n**F1**: Define it."}, path="docs/guide.md"
        )
        if login:
            comment["user"]["login"] = login
        self.fake.thread(comment)
        return comment

    def test_only_comment_reviews(self):
        for event in ("APPROVE", "REQUEST_CHANGES"):
            with self.subTest(event=event):
                self.refused("pulls/9/reviews", self.review(event))
        self.write("pulls/9/reviews", self.review())
        self.assertEqual([r["event"] for r in self.fake.app_reviews], ["COMMENT"])

    def test_another_tasks_pull_request_is_refused(self):
        self.fake.pr["head"]["ref"] = "concurro/issue-8-" + "a" * 12
        self.refused("pulls/9/reviews", self.review())
        self.refused(
            "pulls/9/comments", {"commit_id": "c" * 40, "path": "docs/guide.md", "subject_type": "file", "body": "x"}
        )

    def test_file_comments_only(self):
        self.refused(
            "pulls/9/comments", {"commit_id": "c" * 40, "path": "docs/guide.md", "subject_type": "line", "body": "x"}
        )
        self.refused("pulls/9/comments", {"commit_id": "c" * 40, "path": "docs/guide.md", "line": 1, "body": "x"})
        self.write(
            "pulls/9/comments", {"commit_id": "c" * 40, "path": "docs/guide.md", "subject_type": "file", "body": "x"}
        )

    def test_replies_only_in_the_apps_finding_threads(self):
        human = self.finding_comment(login="writer")
        unmarked = self.finding_comment(body="A note without a finding marker.")
        own = self.finding_comment()
        for target in (human, unmarked):
            with self.subTest(comment=target["id"]):
                self.refused(f"pulls/9/comments/{target['id']}/replies", {"body": "Fixed."})
        self.write(f"pulls/9/comments/{own['id']}/replies", {"body": "Fixed."})

    def test_resolution_only_for_the_apps_threads_and_settled_done(self):
        self.finding_comment(login="writer")
        self.finding_comment()
        facade = self.facade(self.generation)
        with self.assertRaises(Disallowed):
            facade.resolve_thread("T1")
        self.assertTrue(facade.resolve_thread("T2"))
        self.assertEqual([t["isResolved"] for t in self.fake.thread_list], [False, True])
        self.assertEqual(self.registry.unresolved(KEY), [])
        self.assertEqual([r.status for r in self.registry.mutations(KEY, "done")], ["done"])


class ServiceRun(ServiceFixture, unittest.TestCase):
    """Through the worker and gateway, a finding is threaded on the PR, answered and resolved."""

    def test_finding_lifecycle(self):
        self.agents = FakeAgents(fix=True)
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        for _ in range(6):
            self.reconciler.poll()
            self.drain()
        self.assertEqual(self.stage(), "gates")
        state = self.state()
        self.assertEqual(
            [(f["id"], f["status"], f["resolution"]) for f in state["findings"]], [("F1", "fixed", "done")]
        )
        self.assertEqual([t["isResolved"] for t in self.fake.thread_list], [True])
        self.assertEqual({r["event"] for r in self.fake.app_reviews}, {"COMMENT"})
        self.assertEqual(self.registry.unresolved(KEY), [])
        self.assertNotIn("pr_outbox", {k for k, v in state.items() if v})


if __name__ == "__main__":
    unittest.main()
