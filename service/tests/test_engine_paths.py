"""The real engine under the gateway: stops pass every wrapper, and lost writes are reconstructed."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import KEY, ServiceFixture, Wire

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import conversation
from checkpoint_health import sync_checkpoint_health
from checkpoints import CheckpointError
from controller import MARKER, Outcome
from github import Transient
from policy import Blocked, UncertainOutcome
from report_store import MARKER as REPORT_DATA_MARKER
from service import failures
from service.errors import OwnershipLost, Unresolved
from service.gateway import LEASE_SECONDS
from service.transport import Transport


class Token:
    """A fixed read token."""

    def get(self):
        return "read-token"

    def drop(self, token):
        pass


class StopTests(ServiceFixture, unittest.TestCase):
    """A takeover mid-tick stops the old generation inside each best-effort or wrapping engine path."""

    def stop_on(self, matches, ticks=12):
        """Tick until a matching write, take over just before it, and return the escaping exception."""
        original = self.gateway.mutate
        self.after = None

        def mutate(key, generation, method, path, body=None):
            if self.after is not None:
                self.after += 1
            elif matches(method, path, body or {}):
                self.clock.now += LEASE_SECONDS + 1
                self.assertIsNotNone(self.gateway.acquire(KEY, "successor"))
                self.after = 0
            return original(key, generation, method, path, body)

        with patch.object(self.gateway, "mutate", side_effect=mutate):
            for _ in range(ticks):
                try:
                    self.tick()
                except BaseException as error:  # noqa: B036  (the type itself is under test)
                    self.assertEqual(self.after, 0, "a write was attempted after the stop")
                    return error
        self.fail("no matching write")

    def assert_lost(self, error):
        """The stop reached the caller unwrapped, and the gateway recorded it for the worker."""
        self.assertIs(type(error), OwnershipLost)
        generation = self.registry.current(KEY).generation - 1
        self.assertIsInstance(self.gateway.stop(KEY, generation), OwnershipLost)

    def test_checkpoint_save(self):
        """`Checkpoints.request` wraps every `Exception` as a checkpoint error, but not a stop."""
        self.assert_lost(self.stop_on(lambda method, path, body: method == "PATCH" and "/artifacts/" in path))

    def test_conversation_publication(self):
        """Publication wraps failures as `PublicationError`, but not a stop."""
        self.assert_lost(
            self.stop_on(
                lambda method, path, body: (
                    path.endswith("/comments") and body.get("body", "").startswith(conversation.MARKER)
                )
            )
        )

    def test_stage_label_sync(self):
        """Label sync swallows failures as display-only, but not a stop."""
        self.assert_lost(self.stop_on(lambda method, path, body: path.endswith("/issues/7/labels")))

    def test_health_reporting(self):
        """Health reporting swallows every failure, but not a stop."""
        stale = self.facade(self.gateway.acquire(KEY, "worker"))
        self.clock.now += LEASE_SECONDS + 1
        self.gateway.acquire(KEY, "successor")
        with patch.object(self.gateway, "mutate", wraps=self.gateway.mutate) as mutate:
            with self.assertRaises(OwnershipLost):
                sync_checkpoint_health(stale, self.config, 7, failed=True)
        self.assertEqual(mutate.call_count, 1)
        self.assertEqual(self.backend.writes, [])


class Recovery(ServiceFixture):
    """Tick to review as the worker will: resolve unknown writes, wait out transient failures."""

    def to_review(self, prefix=None):
        """Return the final state and every `Transient` that ended a tick, each classified `wait-until`."""
        transients, state = [], None
        for _ in range(12):
            try:
                state, _ = self.tick()
            except Unresolved:
                [record] = self.registry.unresolved(KEY)
                self.assertTrue(record.path.endswith(prefix))
                self.registry.resolve(record.intent, "applied", "object observed on GitHub", "operator")
                continue
            except Transient as error:
                outcome = failures.classify(error, self.gateway.stop(KEY, self.registry.current(KEY).generation))
                self.assertEqual((outcome.kind, outcome.progressed), ("wait-until", False))
                self.assertEqual(outcome.retry_after, error.retry_after or failures.backoff(0))
                transients.append(error)
                continue
            if state and state["stage"] == "review":
                break
        self.assertEqual((self.backend.faults, self.backend.read_faults), ([], []))
        self.assertEqual(state["stage"], "review")
        self.assertEqual(self.registry.unresolved(KEY), [])
        return state, transients

    def assert_single(self, method, path):
        """The write was sent exactly once."""
        self.assertEqual(self.backend.writes.count((method, path)), 1)


class ReconstructionTests(Recovery, unittest.TestCase):
    """An applied write whose response was lost is reconstructed by the next tick, never resent."""

    def lose(self, method, prefix, body=""):
        """Lose the response to one write, resolve it as applied, and tick on to review."""
        self.backend.fail(method, prefix, lost=True, body=body)
        return self.to_review(prefix)[0]

    def truncate(self, method, prefix, body=""):
        """Apply one write but truncate its 2xx body; it settles `done` and the next tick finds it."""
        self.backend.fail(method, prefix, truncated=True, body=body)
        state, transients = self.to_review()
        self.assertEqual([error.code for error in transients], ["response_lost"])
        return state

    def test_audit_artifact(self):
        """The audit artifact is found by creator and name, not created twice."""
        self.lose("POST", "issues/7/artifacts")
        items = [item["artifact"] for item in self.fake.artifacts[7].values()]
        archives = [i["name"] for i in items if i["name"].startswith(("Concurro report ", "Concurro review data "))]
        self.assertEqual(sorted(i["type"] for i in items if i["name"] not in archives), ["generic", "plan"])
        self.assertEqual(len(archives), len(set(archives)), "no report archive is created twice")

    def check_report_data_recovery(self, truncated):
        """Reconstruct canonical report parts before any dependent approval is saved."""
        if truncated:
            self.truncate("POST", "issues/7/artifacts", body=REPORT_DATA_MARKER)
        else:
            self.lose("POST", "issues/7/artifacts", body=REPORT_DATA_MARKER)
        parts = [
            item["artifact"]["body"].split("\n", 1)[0]
            for item in self.fake.artifacts[7].values()
            if item["artifact"]["body"].startswith(REPORT_DATA_MARKER)
        ]
        self.assertTrue(parts)
        self.assertEqual(len(parts), len(set(parts)))

    def test_report_data_recovers_a_lost_write_response(self):
        """A successful part write with a lost response is discovered, never duplicated."""
        self.check_report_data_recovery(False)

    def test_report_data_recovers_a_truncated_write_response(self):
        """A truncated successful response is reconstructed through the real service gateway."""
        self.check_report_data_recovery(True)

    def test_summary_comment(self):
        """The summary comment is found by its marker and author."""
        self.lose("POST", "issues/7/comments", body=MARKER)
        self.assertEqual(len([c for c in self.fake.comments if c["body"].startswith(MARKER)]), 1)

    def test_pull_request(self):
        """The PR is found by its head branch."""
        state = self.lose("POST", "pulls")
        self.assert_single("POST", "pulls")
        self.assertEqual(state["pr"], self.fake.pr["number"])

    def test_summary_comment_with_a_truncated_response(self):
        """A created summary comment whose body was lost is found by marker; no duplicate, no retry command."""
        self.truncate("POST", "issues/7/comments", body=MARKER)
        self.assertEqual(len([c for c in self.fake.comments if c["body"].startswith(MARKER)]), 1)

    def test_pull_request_with_a_truncated_response(self):
        """A created PR whose body was lost is found by its head branch."""
        state = self.truncate("POST", "pulls")
        self.assert_single("POST", "pulls")
        self.assertEqual(state["pr"], self.fake.pr["number"])


class TransientTests(Recovery, unittest.TestCase):
    """A transient GitHub failure passes every engine wrapper typed, and the task resumes on its own."""

    def assert_resumes(self, code, retry_after):
        """Tick to review; exactly one tick ended with this transient failure."""
        state, transients = self.to_review()
        self.assertEqual([(e.code, e.retry_after) for e in transients], [(code, retry_after)])
        self.assertFalse(any(c["body"].startswith("/concurro retry") for c in self.fake.comments))
        return state

    def test_checkpoint_request(self):
        """`Checkpoints.request` wraps `Exception` as a checkpoint error, but not `Transient`."""
        self.backend.fail_read("issues/7/artifacts/", Transient("unavailable"))
        self.assert_resumes("unavailable", None)

    def test_checkpoint_write(self):
        """A throttled artifact write is `failed`, typed, and the next tick saves again."""
        self.backend.fail("PATCH", "issues/7/artifacts/", status=403, headers={"retry-after": "120"})
        self.assert_resumes("throttled", 120)

    def test_artifact_collection(self):
        """`Checkpoints.collection` wraps `Exception` too, but not `Transient`."""
        self.backend.fail_read("issues/7/artifacts?", Transient("throttled", 30))
        self.assert_resumes("throttled", 30)

    def test_conversation_publication(self):
        """Pending events survive a throttled comment and are published once by the next tick."""
        fault = self.backend.fail(
            "POST", "issues/7/comments", status=403, body=conversation.MARKER, headers={"retry-after": "30"}
        )
        self.assert_resumes("throttled", 30)
        published = [c["body"] for c in self.fake.comments if c["body"].startswith(conversation.MARKER)]
        self.assertEqual(published.count(fault["seen"]["body"]), 1)
        self.assertEqual(len(published), len(set(published)))

    def test_graphql_query_throttled_with_http_200(self):
        """A throttled PR-thread read propagates unchanged through the actual transport and controller."""
        self.to_review()
        wire = Wire(self.backend)
        reader = Transport(KEY[0], Token(), self.clock, wire.connection)
        self.reader = SimpleNamespace(request=reader.request, archive=self.backend.archive)
        wire.reply(
            lambda method, path, data: path == "graphql" and "reviewThreads" in data["query"],
            200,
            {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(self.clock()) + 90)},
        )
        with self.assertRaises(Transient) as raised:
            self.tick()
        self.assertEqual((raised.exception.code, raised.exception.retry_after), ("throttled", 90))
        self.tick()
        self.assertEqual(wire.replies, [])

    def test_outcome_lessons_do_not_skip_a_transient_read(self):
        """A lesson is skipped only for an integrity error; a transient read of its checkpoint propagates."""
        controller = self.controller(self.gateway.acquire(KEY, "worker"))
        controller.state = {"issue": 7, "requirements": {"title": "Improve fixture handling"}}
        self.backend.fail_read("issues/12/artifacts", Transient("unavailable"))
        with patch.object(controller.gh, "outcome_candidates", return_value=[(12, [])]):
            with self.assertRaises(Transient):
                controller.retrieve_outcomes()

    def test_classifier(self):
        """Stops win over errors; a transient failure waits for GitHub's delay or local backoff."""
        stop = OwnershipLost("stale")
        self.assertIs(failures.classify(Transient("throttled", 7200), stop), stop)
        self.assertIs(failures.classify(stop), stop)
        self.assertEqual(failures.classify(Transient("throttled", 7200)), Outcome("wait-until", retry_after=7200))
        self.assertEqual(
            failures.classify(Transient("unavailable"), attempt=10),
            Outcome("wait-until", progressed=False, retry_after=failures.BACKOFF_LIMIT),
        )
        self.assertEqual(failures.classify(Transient("unavailable")).retry_after, failures.BACKOFF_SECONDS)

    def test_remaining_classes(self):
        """Uncertain calls, checkpoint failures and unverified state keep the stage; the rest block."""
        wait = Outcome("wait-until", retry_after=failures.BACKOFF_SECONDS)
        cases = [
            (UncertainOutcome("unknown"), True, wait, None),
            (CheckpointError("unknown"), True, wait, "health"),
            (conversation.PublicationError("conversation_conflict"), True, Outcome("blocked"), "suspend"),
            (Blocked("policy"), True, Outcome("blocked"), "suspend"),
            (RuntimeError("other"), True, Outcome("blocked"), "suspend"),
            (Blocked("policy"), False, wait, None),
            (Transient("unavailable"), True, wait, None),
        ]
        for error, verified, outcome, effect in cases:
            with self.subTest(error=type(error).__name__, verified=verified):
                self.assertEqual(failures.classify(error, verified=verified), outcome)
                self.assertEqual(failures.effect(error, verified), effect)
        self.assertIsNone(failures.effect(OwnershipLost("stale"), True))


if __name__ == "__main__":
    unittest.main()
