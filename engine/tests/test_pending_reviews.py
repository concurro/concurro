"""Pending-review publication survives interruptions without empty timeline reviews."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import review_api
import reviews
from fakes import FakeGitHub
from format_samples import review_state
from policy import Blocked, digest


class PendingReviewTests(unittest.TestCase):
    """Exercise the new protocol independently from historical REST publication fixtures."""

    def setUp(self):
        """Seed an existing finding and a new report with inline and general findings."""
        self.state, _, _ = review_state()
        self.gh = FakeGitHub(CONFIG)
        self.gh.pr = {"node_id": "PR_9", "state": "open", "head": {"sha": "d" * 40}}
        self.report = self.state["reviews"]["claude"]
        self.report["findings"][0]["line"] = 1
        self.report["findings"][0]["start_line"] = None
        self.state["findings"][1].update(line=1, start_line=None)
        for finding in self.state["findings"]:
            finding["format"] = 4
            finding["opening_digest"] = digest(reviews.finding_body(finding))
        old = self.state["findings"][0]
        self.gh.review_comments.append(
            {"id": 101, "path": old["path"], "body": reviews.finding_body(old), "user": {"login": CONFIG["bot_login"]}}
        )
        self.gh.thread(self.gh.review_comments[0])
        reviews.queue_review(self.state, "claude", self.report)
        self.checkpoints = []

    def publish(self):
        """Publish with a diff that can host the line-level finding."""
        with patch.object(
            self.gh, "pull_files", return_value=[{"filename": "docs/guide.md", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        ):
            return reviews.publish(self.state, self.gh, CONFIG, lambda: self.checkpoints.append(deepcopy(self.state)))

    def test_one_review_contains_findings_and_replies(self):
        self.assertTrue(self.publish())
        self.assertEqual(len(self.gh.app_reviews), 1)
        self.assertTrue(self.gh.app_reviews[0]["body"])
        self.assertEqual(len(self.gh.review_comments), 3)
        self.assertTrue(self.gh.thread_list[0]["isResolved"])
        self.assertFalse(self.state["pr_outbox"])

    def test_null_line_falls_back_without_standalone_review(self):
        self.gh.null_thread = True
        self.assertTrue(self.publish())
        opening = next(c for c in self.gh.review_comments if c["body"].startswith(reviews.finding_marker("F2")))
        self.assertEqual(opening["subject_type"], "file")
        self.assertEqual(len(self.gh.app_reviews), 1)

    def test_lost_responses_and_checkpoint_interruptions(self):
        for operation in review_api.MUTATIONS:
            if operation == "deletePullRequestReview":
                continue
            with self.subTest(operation=operation):
                self.setUp()
                original, lost = self.gh.api, []

                def api(path, method="GET", data=None):
                    """Apply a mutation, then lose its response once."""
                    result = original(path, method, data)
                    if data["query"] == review_api.MUTATIONS[operation] and not lost:
                        lost.append(True)
                        raise OSError("lost response")
                    return result

                with patch.object(self.gh, "api", side_effect=api):
                    self.assertFalse(self.publish())
                self.assertTrue(self.publish())
                self.assertEqual(len(self.gh.app_reviews), 1)
                self.assertEqual(len(self.gh.review_comments), 3)
                self.assertTrue(self.gh.thread_list[0]["isResolved"])

    def test_pending_submit_is_not_resent(self):
        original = self.gh.api

        def api(path, method="GET", data=None):
            """Interrupt before GitHub receives the submission."""
            if data["query"] == review_api.MUTATIONS["submitPullRequestReview"]:
                raise OSError("connection lost")
            return original(path, method, data)

        with patch.object(self.gh, "api", side_effect=api):
            self.assertFalse(self.publish())
        with self.assertRaisesRegex(Blocked, "Same-ID retry"):
            self.publish()
        self.assertFalse(self.gh.app_reviews)
        self.assertEqual(len(self.gh.pending_nodes), 1)

    def test_unidentified_pending_review_is_never_deleted(self):
        review_api.mutate(
            self.gh, "addPullRequestReview", pullRequestId="PR_9", commitOID="d" * 40, body="other report"
        )
        with self.assertRaisesRegex(Blocked, "Unidentified"):
            self.publish()
        self.assertEqual(len(self.gh.pending_nodes), 1)

    def test_marked_review_with_conflicting_identity_blocks(self):
        marker = reviews.review_marker(self.state, CONFIG, 2)
        for field, value in (
            ("body", marker + "edited"),
            ("author", {"login": "human"}),
            ("commit", {"oid": "a" * 40}),
            ("pullRequest", {"number": 10}),
        ):
            with self.subTest(field=field):
                self.setUp()
                node = review_api.mutate(
                    self.gh, "addPullRequestReview", pullRequestId="PR_9", commitOID="d" * 40, body=marker
                )
                self.gh.pending_nodes[node["id"]][field] = value
                with self.assertRaises(Blocked):
                    self.publish()

    def test_withdrawal_is_one_nonempty_review(self):
        self.state["pr_outbox"] = []
        self.state["design_generation"] = 2
        reviews.queue_withdrawal(self.state, ["F1"])
        self.assertTrue(self.publish())
        self.assertEqual(len(self.gh.app_reviews), 1)
        self.assertIn("Design reopened", self.gh.app_reviews[0]["body"])
        self.assertFalse(self.gh.thread_list[0]["isResolved"])

    def test_crash_after_submit_finishes_receipts_without_new_review(self):
        with patch.object(reviews.Publisher, "resolve", side_effect=OSError("crash")):
            self.assertFalse(self.publish())
        self.assertEqual(self.state["pr_outbox"][0]["publication_state"]["phase"], "submitted")
        self.assertTrue(self.publish())
        self.assertEqual(len(self.gh.app_reviews), 1)
        self.assertEqual(self.state["findings"][0]["resolution"], "done")

    def test_stale_head_cleans_abandoned_build(self):
        marker = reviews.review_marker(self.state, CONFIG, 2)
        review_api.mutate(self.gh, "addPullRequestReview", pullRequestId="PR_9", commitOID="d" * 40, body=marker)
        self.gh.pr["head"]["sha"] = "e" * 40
        self.assertTrue(self.publish())
        self.assertFalse(self.gh.pending_nodes)
        self.assertFalse(self.gh.app_reviews)

    def test_duplicate_marker_stops_before_deletion(self):
        marker = reviews.review_marker(self.state, CONFIG, 2)
        node = review_api.mutate(self.gh, "addPullRequestReview", pullRequestId="PR_9", commitOID="d" * 40, body=marker)
        self.gh.pending_nodes["duplicate"] = {**node, "id": "duplicate"}
        with self.assertRaisesRegex(Blocked, "duplicate"):
            self.publish()
        self.assertEqual(len(self.gh.pending_nodes), 2)

    def test_submitted_body_is_reverified_before_finishing(self):
        with patch.object(reviews.Publisher, "resolve", side_effect=OSError("crash")):
            self.assertFalse(self.publish())
        node = next(iter(self.gh.pending_nodes.values()))
        node["body"] += "edited"
        with self.assertRaises(Blocked):
            self.publish()
        self.assertTrue(self.state["pr_outbox"])

    def test_recorded_file_only_fallback_is_honored(self):
        self.state["pr_outbox"][0]["inline"] = False
        self.assertTrue(self.publish())
        finding = next(c for c in self.gh.review_comments if c["body"].startswith(reviews.finding_marker("F2")))
        self.assertEqual(finding["subject_type"], "file")

    def test_null_file_thread_falls_back_to_body(self):
        original = self.gh.api

        def api(path, method="GET", data=None):
            """Confirm that neither inline nor file placement creates a thread."""
            if data["query"] == review_api.MUTATIONS["addPullRequestReviewThread"]:
                return {"data": {"addPullRequestReviewThread": {"thread": None}}}
            return original(path, method, data)

        with patch.object(self.gh, "api", side_effect=api):
            self.assertTrue(self.publish())
        finding = reviews.find(self.state, "F2")
        self.assertEqual(finding["thread"], "body")
        self.assertIn("F2", self.gh.app_reviews[0]["body"])

    def test_null_author_on_unrelated_review_is_ignored(self):
        self.gh.pending_nodes = {
            "deleted-author": {"id": "deleted-author", "author": None, "state": "COMMENTED", "body": "Human review"}
        }
        self.assertTrue(self.publish())

    def test_null_author_with_our_marker_is_a_conflict(self):
        marker = reviews.review_marker(self.state, CONFIG, 2)
        node = review_api.mutate(self.gh, "addPullRequestReview", pullRequestId="PR_9", commitOID="d" * 40, body=marker)
        self.gh.pending_nodes[node["id"]]["author"] = None
        with self.assertRaises(Blocked):
            self.publish()

    def test_mixed_rest_then_pending_outbox_preserves_protocol(self):
        self.state["pr_outbox"][0].pop("publication")
        self.state["design_generation"] = 2
        reviews.queue_withdrawal(self.state, ["F1"])
        self.assertTrue(self.publish())
        self.assertEqual(len(self.gh.app_reviews), 2)
        self.assertTrue(any(c["body"].startswith(reviews.WITHDRAWN_MARKER) for c in self.gh.review_comments))
        self.assertFalse(self.state["pr_outbox"])
