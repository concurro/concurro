"""Exact pending-review admission, response settlement and operator recovery."""

from copy import deepcopy
from unittest.mock import patch

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import review_api
from controller import Outcome
from service.errors import Unresolved
from service.gateway import Disallowed
from service.review_recovery import reconcile
from test_pr_reviews import PullRequestWrites


class PendingWrites(PullRequestWrites):
    """Exercise the production gateway and durable registry over simulated GitHub."""

    def create(self):
        """Create this report's pending review through the gateway."""
        self.fake.pr["node_id"] = "PR_9"
        self.marker = f"<!-- concurro:review:v1:{KEY[0]}#{KEY[1]}:1 -->"
        body = review_api.request(
            "addPullRequestReview", {"pullRequestId": "PR_9", "commitOID": "c" * 40, "body": self.marker}
        )
        result = self.gateway.mutate(KEY, self.generation, "POST", "graphql", body)
        identifier = result["data"]["addPullRequestReview"]["pullRequestReview"]["id"]
        record = self.registry.mutations(KEY, "done")[-1]
        self.assertEqual(self.registry.review_intent(record.intent)["review"], identifier)
        return identifier

    def thread(self, identifier):
        """One line-level finding request."""
        return review_api.request(
            "addPullRequestReviewThread",
            {
                "pullRequestReviewId": identifier,
                "path": "docs/guide.md",
                "body": "<!-- concurro:finding:v1:F2 -->\nFinding",
                "subjectType": "LINE",
                "line": 1,
                "side": "RIGHT",
            },
        )

    def uncertain(self, body):
        """Lose a successful response, then release the stopped worker."""
        original = self.backend.send

        def send(method, path, data):
            """Apply through the real fake transport before losing the response."""
            original(method, path, data)
            raise TimeoutError("lost response")

        with patch.object(self.backend, "send", side_effect=send):
            with self.assertRaises(Unresolved):
                self.gateway.mutate(KEY, self.generation, "POST", "graphql", body)
        self.gateway.release(KEY, self.generation, Outcome("wait-until", False, 30))
        return self.registry.unresolved(KEY)[0].intent

    def test_unknown_thread_is_discarded_only_after_journaled_delete(self):
        identifier = self.create()
        intent = self.uncertain(self.thread(identifier))
        self.assertEqual(self.registry.review_intent(intent)["review"], identifier)
        self.assertEqual(reconcile(self.gateway, intent, "operator")["disposition"], "discarded")
        self.assertFalse(self.registry.unresolved(KEY))
        child = self.registry.review_intent(intent)["recovery"]
        self.assertEqual(self.registry.mutation(child).status, "done")
        self.assertNotIn(identifier, self.fake.pending_nodes)

    def test_unknown_submit_is_verified_without_another_write(self):
        identifier = self.create()
        body = review_api.request(
            "submitPullRequestReview",
            {"pullRequestReviewId": identifier, "event": "COMMENT", "body": self.marker + "\nSummary"},
        )
        intent = self.uncertain(body)
        count = len(self.backend.writes)
        self.assertEqual(reconcile(self.gateway, intent, "operator")["disposition"], "applied")
        self.assertEqual(len(self.backend.writes), count)
        self.assertEqual(len(self.fake.app_reviews), 1)

    def test_compensation_crash_resumes_from_confirmed_child(self):
        identifier = self.create()
        intent = self.uncertain(self.thread(identifier))
        with patch.object(self.registry, "resolve", side_effect=ValueError("crash after deletion")):
            with self.assertRaises(ValueError):
                reconcile(self.gateway, intent, "operator")
        self.registry.close()
        self.registry = self.open()
        self.gateway.registry = self.registry
        writes = len(self.backend.writes)
        reconcile(self.gateway, intent, "operator")
        self.assertEqual(len(self.backend.writes), writes)

    def test_compensation_unknown_remains_stopped(self):
        identifier = self.create()
        intent = self.uncertain(self.thread(identifier))
        original = self.backend.send

        def send(method, path, body):
            """Delete successfully but lose the compensating response."""
            original(method, path, body)
            raise TimeoutError("lost delete response")

        with patch.object(self.backend, "send", side_effect=send):
            with self.assertRaises(Unresolved):
                reconcile(self.gateway, intent, "operator")
        self.assertEqual(len(self.registry.unresolved(KEY)), 2)
        with self.assertRaisesRegex(ValueError, "another write"):
            reconcile(self.gateway, intent, "operator")

    def test_wrong_review_or_event_is_refused(self):
        identifier = self.create()
        for changed in ({"event": "APPROVE"}, {"pullRequestReviewId": "foreign"}):
            values = {
                "pullRequestReviewId": identifier,
                "event": "COMMENT",
                "body": self.marker + "\nSummary",
                **changed,
            }
            with self.assertRaises(Disallowed):
                self.gateway.mutate(
                    KEY, self.generation, "POST", "graphql", review_api.request("submitPullRequestReview", values)
                )

    def test_explicit_null_is_distinct_from_malformed_response(self):
        identifier = self.create()
        body = self.thread(identifier)
        self.fake.null_thread = True
        payload = self.gateway.mutate(KEY, self.generation, "POST", "graphql", body)
        self.assertIsNone(review_api.result(*review_api.identify(body), payload))
        for payload in (
            {},
            {"data": {}},
            {"data": {"addPullRequestReviewThread": {}}},
            {"data": {"addPullRequestReviewThread": None}},
            {"data": {"addPullRequestReviewThread": {"thread": None}}, "errors": []},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                review_api.result(*review_api.identify(body), deepcopy(payload))

    def test_unknown_reply_is_compensated(self):
        self.finding_comment()
        identifier = self.create()
        request = review_api.request(
            "addPullRequestReviewThreadReply",
            {"pullRequestReviewId": identifier, "pullRequestReviewThreadId": "T1", "body": "Fixed."},
        )
        intent = self.uncertain(request)
        self.assertEqual(reconcile(self.gateway, intent, "operator")["disposition"], "discarded")

    def test_create_and_delete_stay_manual(self):
        for name in ("addPullRequestReview", "deletePullRequestReview"):
            with self.subTest(name=name):
                self.setUp()
                identifier = self.create()
                if name == "addPullRequestReview":
                    del self.fake.pending_nodes[identifier]
                    values = {"pullRequestId": "PR_9", "commitOID": "c" * 40, "body": self.marker}
                else:
                    values = {"pullRequestReviewId": identifier}
                intent = self.uncertain(review_api.request(name, values))
                with self.assertRaisesRegex(ValueError, "manual resolution"):
                    reconcile(self.gateway, intent, "operator")
                self.assertTrue(self.registry.unresolved(KEY))

    def test_unconfirmed_submit_is_not_retried(self):
        for change in ("pending", "missing", "mismatch"):
            with self.subTest(change=change):
                self.setUp()
                identifier = self.create()
                intent = self.uncertain(
                    review_api.request(
                        "submitPullRequestReview",
                        {"pullRequestReviewId": identifier, "event": "COMMENT", "body": self.marker + "\nSummary"},
                    )
                )
                node = self.fake.pending_nodes[identifier]
                if change == "pending":
                    node.update(state="PENDING", body=self.marker)
                elif change == "missing":
                    del self.fake.pending_nodes[identifier]
                else:
                    node["body"] = "human edit"
                count = len(self.backend.writes)
                with self.assertRaises(ValueError):
                    reconcile(self.gateway, intent, "operator")
                self.assertEqual(count, len(self.backend.writes))
                self.assertTrue(self.registry.unresolved(KEY))
