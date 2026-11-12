"""Journal-backed submission, compensation and mutation boundary regressions."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import review_api
from controller import Outcome
from github import Transient
from service.errors import Unresolved
from service.gateway import Disallowed
from service.registry import DATABASE
from service.review_recovery import reconcile
from service.settlement import settle
from service.transport import Response
from test_gateway_pending_publication import PendingWrites


class RecoveryCorrections(PendingWrites):
    """Use the real durable registry and production gateway for each boundary."""

    def test_2_first_send_after_no_or_definitely_failed_intent(self):
        for failure in (None, "unsent", "401", "throttled", "disallowed"):
            with self.subTest(failure=failure):
                self.setUp()
                identifier = self.create()
                body = self.marker + "\nFrozen summary"
                request = review_api.request(
                    "submitPullRequestReview", {"pullRequestReviewId": identifier, "event": "COMMENT", "body": body}
                )
                if failure:
                    if failure == "disallowed":
                        with patch.object(self.gateway, "bound", return_value=False), self.assertRaises(Disallowed):
                            self.gateway.mutate(KEY, self.generation, "POST", "graphql", request)
                    else:
                        response = {
                            "unsent": Response(False, 0, {}, b""),
                            "401": Response(True, 401, {}, b"{}"),
                            "throttled": Response(True, 403, {"retry-after": "60"}, b"{}"),
                        }[failure]
                        with patch.object(self.backend, "send", return_value=response), self.assertRaises(Transient):
                            self.gateway.mutate(KEY, self.generation, "POST", "graphql", request)
                self.gateway.release(KEY, self.generation, Outcome("wait-until", False, 1))
                self.registry.close()
                self.registry = self.open()
                self.gateway.registry = self.registry
                self.generation = self.gateway.acquire(KEY, "recovered-worker")
                result = self.facade(self.generation).resume_review_submission(identifier, body)
                self.assertEqual(result["state"], "COMMENTED")
                self.assertEqual(len(self.fake.app_reviews), 1)

    def test_unknown_submit_never_becomes_retryable_by_manual_clear(self):
        identifier = self.create()
        request = review_api.request(
            "submitPullRequestReview",
            {"pullRequestReviewId": identifier, "event": "COMMENT", "body": self.marker + "\nSummary"},
        )
        with patch.object(self.backend, "send", side_effect=TimeoutError("may be in flight")):
            with self.assertRaises(Unresolved):
                self.gateway.mutate(KEY, self.generation, "POST", "graphql", request)
        intent = self.registry.unresolved(KEY)[0].intent
        self.gateway.release(KEY, self.generation, Outcome("wait-until", False, 1))
        self.registry.resolve(intent, "not-applied", "operator observation", "operator")
        generation = self.gateway.acquire(KEY, "next")
        with self.assertRaisesRegex(Disallowed, "same-ID retry"):
            self.facade(generation).resume_review_submission(identifier, self.marker + "\nSummary")
        self.assertFalse(self.fake.app_reviews)

    def test_10_definitely_failed_delete_can_be_retried(self):
        for response in (Response(False, 0, {}, b""), Response(True, 403, {"retry-after": "60"}, b"{}")):
            with self.subTest(response=response):
                self.setUp()
                identifier = self.create()
                intent = self.uncertain(self.thread(identifier))
                with patch.object(self.backend, "send", return_value=response), self.assertRaises(Transient):
                    reconcile(self.gateway, intent, "operator")
                first = self.registry.review_intent(intent)["recovery"]
                self.assertEqual(self.registry.mutation(first).status, "failed")
                self.registry.close()
                self.registry = self.open()
                self.gateway.registry = self.registry
                reconcile(self.gateway, intent, "operator")
                second = self.registry.review_intent(intent)["recovery"]
                self.assertNotEqual(first, second)
                self.assertEqual(self.registry.mutation(first).status, "failed")
                self.assertEqual(self.registry.mutation(second).status, "done")
                self.assertFalse(self.registry.unresolved(KEY))

    def test_inflight_compensation_stays_stopped_across_restart(self):
        identifier = self.create()
        intent = self.uncertain(self.thread(identifier))
        with patch.object(self.registry, "settle", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                reconcile(self.gateway, intent, "operator")
        child = self.registry.review_intent(intent)["recovery"]
        self.assertEqual(self.registry.mutation(child).status, "in-flight")
        self.registry.close()
        self.registry = self.open()
        self.gateway.registry = self.registry
        self.assertEqual(self.registry.mutation(child).status, "unknown")
        with self.assertRaises(ValueError):
            reconcile(self.gateway, intent, "operator")

    def test_admission_rejects_foreign_submitted_closed_or_unmarked_reviews(self):
        for mutation in ("foreign-author", "null-author", "submitted", "other-marker", "closed", "bad-submit-marker"):
            with self.subTest(mutation=mutation):
                self.setUp()
                identifier = self.create()
                node = self.fake.pending_nodes[identifier]
                body = self.thread(identifier)
                if mutation in ("foreign-author", "null-author"):
                    node["author"] = None if mutation == "null-author" else {"login": "other"}
                elif mutation == "submitted":
                    node["state"] = "COMMENTED"
                elif mutation == "other-marker":
                    node["body"] = self.marker.replace("#7:", "#8:")
                elif mutation == "closed":
                    self.fake.pr["state"] = "closed"
                else:
                    body = review_api.request(
                        "submitPullRequestReview",
                        {"pullRequestReviewId": identifier, "event": "COMMENT", "body": "unmarked"},
                    )
                writes = len(self.backend.writes)
                with self.assertRaises(Disallowed):
                    self.gateway.mutate(KEY, self.generation, "POST", "graphql", body)
                self.assertEqual(len(self.backend.writes), writes)

    def test_reply_requires_finding_on_exact_same_pr(self):
        identifier = self.create()
        self.finding_comment(body="Unmarked note")
        body = review_api.request(
            "addPullRequestReviewThreadReply",
            {"pullRequestReviewId": identifier, "pullRequestReviewThreadId": "T1", "body": "Reply"},
        )
        with self.assertRaises(Disallowed):
            self.gateway.mutate(KEY, self.generation, "POST", "graphql", body)
        self.fake.review_comments[0]["body"] = "<!-- concurro:finding:v1:F1 -->\nFinding"
        original = self.backend.thread_node

        def node(identifier):
            """Return a task-owned finding on another PR."""
            value = original(identifier)
            value["pullRequest"]["number"] = 10
            return value

        with patch.object(self.backend, "thread_node", side_effect=node), self.assertRaises(Disallowed):
            self.gateway.mutate(KEY, self.generation, "POST", "graphql", body)

    def test_settlement_validates_every_mutation_response(self):
        requests = {
            "addPullRequestReview": {"pullRequestId": "P", "commitOID": "a" * 40, "body": "marker"},
            "addPullRequestReviewThread": {
                "pullRequestReviewId": "R",
                "path": "f",
                "body": "finding",
                "subjectType": "FILE",
            },
            "addPullRequestReviewThreadReply": {
                "pullRequestReviewId": "R",
                "pullRequestReviewThreadId": "T",
                "body": "reply",
            },
            "submitPullRequestReview": {"pullRequestReviewId": "R", "event": "COMMENT", "body": "marker"},
            "deletePullRequestReview": {"pullRequestReviewId": "R"},
        }
        for name, values in requests.items():
            field = (
                "thread" if name.endswith("Thread") else "comment" if name.endswith("Reply") else "pullRequestReview"
            )
            good = {
                "id": "R",
                **(
                    {"state": "PENDING"}
                    if name == "addPullRequestReview"
                    else {"state": "COMMENTED"}
                    if name == "submitPullRequestReview"
                    else {}
                ),
            }
            payload = {"data": {name: {field: good}}}
            variants = [
                (payload, "done"),
                ({}, "unknown"),
                ({"data": {}}, "unknown"),
                ({"data": {name: {}}}, "unknown"),
                ({"data": {name: {field: {}}}}, "unknown"),
                ({**payload, "errors": []}, "unknown"),
            ]
            if name == "addPullRequestReviewThread":
                variants.append(({"data": {name: {"thread": None}}}, "done"))
            if name in ("addPullRequestReview", "submitPullRequestReview"):
                variants.append(({"data": {name: {field: {**good, "state": "DISMISSED"}}}}, "unknown"))
            if name in ("submitPullRequestReview", "deletePullRequestReview"):
                variants.append(({"data": {name: {field: {**good, "id": "foreign"}}}}, "unknown"))
            for response, expected in variants:
                with self.subTest(name=name, response=response):
                    self.assertEqual(
                        settle(
                            "graphql",
                            Response(True, 200, {}, json.dumps(response).encode()),
                            0,
                            review_api.request(name, values),
                        )[0],
                        expected,
                    )

    def test_v3_migration_preserves_unknown_write(self):
        record = self.registry.admit(KEY, self.generation, "PATCH", "repos/x/y/issues/7", "digest")
        self.registry.settle(record, "unknown")
        self.registry.close()
        with sqlite3.connect(Path(self.directory.name) / DATABASE) as db:
            db.execute("DROP TABLE review_intents")
            db.execute("DROP TABLE webhook_recovery")
            db.execute("DROP TABLE observations")
            db.execute("UPDATE meta SET value='3' WHERE name='schema'")
        self.registry = self.open()
        self.assertEqual(self.registry.mutation(record).status, "unknown")
        self.assertIsNone(self.registry.review_intent(record))
        self.assertEqual(self.registry.db.execute("SELECT value FROM meta WHERE name='schema'").fetchone(), ("5",))
