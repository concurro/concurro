"""Default pending-review publication through workers, external report storage and operator recovery."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from fixtures import KEY, ServiceFixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import report_store
import review_api
import reviews
from checkpoints import Checkpoints
from fakes import FakeAgents
from service.review_recovery import reconcile
from service.transport import Response


class LongReports(FakeAgents):
    """Produce report bodies large enough to exercise detached frozen publication fields."""

    def ask(self, provider, stage, context):
        """Extend canonical review text without altering the review's decision."""
        report = super().ask(provider, stage, context)
        if stage == "review":
            report["summary"] += "\n" + "The guide was checked against its complete approved scope. " * 55
        return report


class PendingWorkerRecovery(ServiceFixture, unittest.TestCase):
    """No REST override: the production default must run and recover end to end."""

    def exercise(self, failure=None):
        """Run an approved task, inject one interruption, reconcile if necessary and finish."""
        self.assertEqual(reviews.PUBLICATION, "pending-v1")
        self.agents = LongReports(fix=True)
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        original_send, original_save = self.backend.send, Checkpoints.save
        interrupted, detached, states, writes = [], [], [], []

        def send(method, path, body):
            """Inject only at a specific GraphQL mutation; other writes use the real gateway."""
            named = review_api.identify(body) if path == "graphql" else None
            if named:
                writes.append(named[0])
            wanted = {
                "thread": "addPullRequestReviewThread",
                "reply": "addPullRequestReviewThreadReply",
                "submit-failed": "submitPullRequestReview",
            }.get(failure)
            if named and named[0] == wanted and not interrupted:
                interrupted.append(True)
                if failure == "submit-failed":
                    return Response(False, 0, {}, b"")
                original_send(method, path, body)
                raise TimeoutError("response lost after application")
            return original_send(method, path, body)

        def save(checkpoints, state):
            """Inspect real externalization and crash after durable submitting intent but before send."""
            for item in state.get("pr_outbox", []):
                progress = item.get("publication_state")
                if progress:
                    states.append(deepcopy(progress))
                    if progress["phase"] == "submitting":
                        _, parts = report_store.packed(state)
                        detached.extend(p["path"] for p in parts if p["path"][0] == "pr_outbox")
            result = original_save(checkpoints, state)
            if (
                failure in ("checkpoint", "after-submit")
                and not interrupted
                and any(
                    i.get("publication_state", {}).get("phase")
                    == ("submitting" if failure == "checkpoint" else "submitted")
                    for i in state.get("pr_outbox", [])
                )
            ):
                interrupted.append(True)
                raise OSError("crash after submitting checkpoint, before send")
            return result

        recovered = False
        with patch.object(self.backend, "send", side_effect=send), patch.object(Checkpoints, "save", save):
            for _ in range(25):
                self.reconciler.poll()
                self.drain()
                unknown = self.registry.unresolved(KEY)
                if unknown:
                    self.assertIn(failure, ("thread", "reply"))
                    self.assertEqual(len(unknown), 1)
                    self.assertFalse(recovered)
                    context = self.registry.review_intent(unknown[0].intent)
                    pending = context["review"]
                    self.assertEqual(self.fake.pending_nodes[pending]["state"], "PENDING")
                    reconcile(self.gateway, unknown[0].intent, "test-operator")
                    self.assertNotIn(pending, self.fake.pending_nodes)
                    recovered = True
                state = self.state()
                if state and state.get("handoff_ready"):
                    break
                self.clock.now += 3601
            else:
                self.fail(self.state().get("reason"))
        self.assertEqual(bool(interrupted), failure is not None)
        self.assertEqual(recovered, failure in ("thread", "reply"))
        self.assertTrue(detached, "frozen bodies must pass through report_store")
        self.assertTrue(any(p[-1] == "body" for p in detached))
        self.assertEqual({p["phase"] for p in states}, {"building", "submitting", "submitted"})
        self.assertIn("addPullRequestReviewThreadReply", writes)
        self.assertIn("submitPullRequestReview", writes)
        self.assertFalse(self.registry.unresolved(KEY))
        self.assertFalse(self.state()["pr_outbox"])
        self.assertEqual(self.state()["findings"][0]["resolution"], "done")
        self.assertTrue(all(review["body"] for review in self.fake.app_reviews))
        markers = [review["body"].split("\n", 1)[0] for review in self.fake.app_reviews]
        self.assertEqual(len(markers), len(set(markers)))
        self.assertFalse(
            any(
                r.path.endswith("/reviews") or r.path.endswith("/replies") for r in self.registry.mutations(KEY, "done")
            )
        )

    def test_default_publication_end_to_end(self):
        self.exercise()

    def test_lost_thread_response_rebuilds_after_operator_recovery(self):
        self.exercise("thread")

    def test_lost_reply_response_rebuilds_after_operator_recovery(self):
        self.exercise("reply")

    def test_definite_submit_failure_recovers_without_operator(self):
        self.exercise("submit-failed")

    def test_submitting_checkpoint_crash_recovers_without_duplicate_submit(self):
        self.exercise("checkpoint")

    def test_crash_after_submit_finishes_mapping_and_resolution(self):
        self.exercise("after-submit")
