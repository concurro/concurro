"""Cross-repository writes retain task ownership and cannot escape their destination roles."""

import json
import unittest

from fixtures import KEY, ServiceFixture
from policy import task_branch
from service.errors import OwnershipLost, Unresolved
from service.gateway import Disallowed, GatewayGitHub, MutationGateway, allowed_target
from service.transport import Response

UPSTREAM = "source/project"
FORK = "owner/project-fork"
SHA = "a" * 40


class DestinationTests(ServiceFixture, unittest.TestCase):
    """Only the fork branch, upstream PR and source issue are writable under one task generation."""

    def setUp(self):
        super().setUp()
        self.binding = self.registry.bind(KEY, UPSTREAM, FORK, SHA)
        self.sent = []
        outer = self

        class Transport:
            def __init__(self, repository):
                self.repository = repository

            def request(self, method, path, data=None):
                if path == f"repos/{UPSTREAM}/pulls/9":
                    return {"user": {"login": "concurro[bot]"}, "head": {"repo": {"full_name": FORK}, "ref": task_branch(*KEY)}}
                raise AssertionError((self.repository, method, path))

            def send(self, method, path, body):
                outer.sent.append((self.repository, method, path, body))
                return Response(True, 201, {}, json.dumps({"sha": SHA}).encode())

        self.gateway = MutationGateway(self.registry, Transport, Transport, self.config["bot_login"])
        self.generation = self.gateway.acquire(KEY, "worker")
        self.code = GatewayGitHub(FORK, KEY[1], self.generation, self.gateway, Transport(FORK), task_key=KEY)

    def test_destination_token_and_task_journal_are_independent(self):
        """A fork write uses its token but remains fenced and audited under the source issue."""
        self.code.repo("git/refs", "POST", {"ref": "refs/heads/" + task_branch(*KEY), "sha": SHA})
        self.assertEqual(self.sent[0][0], FORK)
        records = self.registry.mutations(KEY, "done")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].key, KEY)
        self.assertTrue(records[0].path.startswith(f"repos/{FORK}/"))
        self.assertEqual(self.registry.mutations((FORK, KEY[1]), "done"), [])

    def test_wrong_destination_branch_and_merge_are_refused(self):
        """Access to both tokens never grants upstream branches, a foreign issue, or merge authority."""
        branch = task_branch(*KEY)
        refused = [
            ("POST", f"repos/{UPSTREAM}/git/refs", {"ref": "refs/heads/" + branch, "sha": SHA}),
            ("POST", f"repos/{FORK}/git/refs", {"ref": "refs/heads/main", "sha": SHA}),
            ("POST", f"repos/{FORK}/git/refs", {"ref": "refs/heads/" + task_branch(UPSTREAM, KEY[1]), "sha": SHA}),
            ("POST", f"repos/{UPSTREAM}/issues/{KEY[1]}/comments", {"body": "wrong issue"}),
            ("PUT", f"repos/{UPSTREAM}/pulls/9/merge", {"sha": SHA}),
            ("PATCH", f"repos/{FORK}/git/refs/heads/" + branch, {"sha": SHA, "force": True}),
        ]
        for method, path, body in refused:
            with self.subTest(path=path), self.assertRaises(Disallowed):
                self.gateway.mutate(KEY, self.generation, method, path, body)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.registry.mutations(KEY, "done"), [])

    def test_only_draft_pr_from_the_task_fork_branch(self):
        """PR creation is bound before the registry has a PR number; reviews remain COMMENT-only."""
        body = {"head": "owner:" + task_branch(*KEY), "head_repo": "project-fork", "base": "main",
                "title": "Improve it", "body": "Issue link", "draft": True, "maintainer_can_modify": False}
        path = f"repos/{UPSTREAM}/pulls"
        self.assertTrue(allowed_target(KEY, self.binding, "POST", path, body))
        for change in ({"head": "owner:other"}, {"head_repo": "other"}, {"draft": False}):
            self.assertFalse(allowed_target(KEY, self.binding, "POST", path, {**body, **change}))
        self.gateway.mutate(KEY, self.generation, "POST", path, body)
        review = {"commit_id": SHA, "event": "COMMENT", "body": "Review", "comments": []}
        self.gateway.mutate(KEY, self.generation, "POST", path + "/9/reviews", review)
        with self.assertRaises(Disallowed):
            self.gateway.mutate(KEY, self.generation, "POST", path + "/9/reviews", {**review, "event": "APPROVE"})
        self.assertEqual([repository for repository, *_ in self.sent], [UPSTREAM, UPSTREAM])
        self.assertIsNone(self.registry.pull(KEY), "binding did not depend on an end-of-tick PR link")

    def test_pr_creation_cannot_grant_fork_collaboration(self):
        """Missing, true and merely false-like values are refused before a write is admitted."""
        body = {"head": "owner:" + task_branch(*KEY), "head_repo": "project-fork", "base": "main",
                "title": "Improve it", "body": "Issue link", "draft": True}
        invalid = [body] + [{**body, "maintainer_can_modify": value} for value in (True, None, 0, "false")]
        for value in invalid:
            with self.subTest(body=value), self.assertRaises(Disallowed):
                self.gateway.mutate(KEY, self.generation, "POST", f"repos/{UPSTREAM}/pulls", value)
        self.assertEqual(self.sent, [])
        for status in ("in-flight", "done", "failed", "unknown"):
            self.assertEqual(self.registry.mutations(KEY, status), [])
        self.gateway.mutate(KEY, self.generation, "POST", f"repos/{UPSTREAM}/pulls",
                            {**body, "maintainer_can_modify": False})
        self.assertEqual(len(self.sent), 1)
        self.assertIs(self.sent[0][3]["maintainer_can_modify"], False)

    def test_stale_generation_cannot_write_any_destination(self):
        """The original issue ownership fences fork and upstream writes alike."""
        self.gateway.release(KEY, self.generation, None)
        self.gateway.acquire(KEY, "new-worker")
        with self.assertRaises(OwnershipLost):
            self.code.repo("git/refs", "POST", {"ref": "refs/heads/" + task_branch(*KEY), "sha": SHA})
        self.assertEqual(self.sent, [])

    def test_unknown_fork_write_stops_issue_writes_too(self):
        """An uncertain write blocks the whole task, not only its destination repository."""
        class Lost:
            def send(self, *args):
                raise TimeoutError("lost response")

        self.gateway.writer = lambda _: Lost()
        with self.assertRaises(Unresolved):
            self.code.repo("git/refs", "POST", {"ref": "refs/heads/" + task_branch(*KEY), "sha": SHA})
        with self.assertRaises(Unresolved):
            self.gateway.mutate(KEY, self.generation, "POST", f"repos/{KEY[0]}/issues/{KEY[1]}/comments", {"body": "x"})
        self.assertEqual(len(self.registry.unresolved(KEY)), 1)


if __name__ == "__main__":
    unittest.main()
