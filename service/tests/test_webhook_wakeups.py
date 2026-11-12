"""Webhook wakeups survive crashes and replace idle readiness timers."""

import unittest
from unittest.mock import patch

from fixtures import KEY, ServiceFixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import test_ingress
from controller import Outcome
from service import registry
from service.ingress import Ingress
from test_ingress import BOT, INSTALLATION, SECRET, comment, payload


class AtomicWakeupTests(ServiceFixture, unittest.TestCase):
    """Retain the ingress contract while testing its durable boundary."""

    deliver = test_ingress.IngressTests.deliver
    queued = test_ingress.IngressTests.queued

    def setUp(self):
        super().setUp()
        self.ingress = Ingress(SECRET, self.registry, {KEY[0]: INSTALLATION}, {BOT}, clock=self.clock)
        self.deliveries = 0

    def test_queue_failure_rolls_back_receipt_and_all_tasks(self):
        """A failure after the first queued task leaves the delivery redeliverable."""
        other = (KEY[0], 8)
        self.registry.link(KEY, 9)
        self.registry.link(other, 10)
        for key in (KEY, other):
            self.registry.acknowledge(key, {"_stage": "gates"})
        data = payload(workflow_run={"pull_requests": [{"number": 9}, {"number": 10}]})
        original = registry.queue
        calls = []

        def interrupted(db, key, cause, due):
            """Fail after the first actual queue write."""
            original(db, key, cause, due)
            calls.append(key)
            raise RuntimeError("crash")

        with patch("service.registry.queue", interrupted), self.assertRaisesRegex(RuntimeError, "crash"):
            self.deliver("workflow_run", data, "atomic")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.deliver("workflow_run", data, "atomic"), 202)
        self.assertEqual(self.queued(), [KEY, other])
        self.assertEqual(self.deliver("workflow_run", data, "atomic"), 200)

    def test_workflow_events_without_sender_use_head_and_merge_sha(self):
        """Internal CI events can lack a sender and PR list, including test-merge runs."""
        self.registry.link(KEY, 9)
        self.registry.acknowledge(KEY, {"_stage": "gates", "pull": {"head": "a" * 40, "merge_head": "b" * 40}})
        for event in ("workflow_run", "check_run", "check_suite"):
            for sha in ("a" * 40, "b" * 40):
                data = payload(**{event: {"head_sha": sha, "pull_requests": []}})
                data.pop("sender")
                self.assertEqual(self.deliver(event, data), 202)
                self.assertEqual(self.queued(), [KEY])
                generation = self.gateway.acquire(KEY, "test")
                self.gateway.release(KEY, generation, Outcome("await-human"))

    def test_ci_does_not_wake_deferred_or_model_work(self):
        """New workflow coverage respects the existing CI-only admission predicate."""
        self.registry.link(KEY, 9)
        for stage in ("implement", "blocked", "plan_qc"):
            self.registry.acknowledge(KEY, {"_stage": stage})
            self.deliver("workflow_run", payload(workflow_run={"pull_requests": [{"number": 9}]}))
            self.assertEqual(self.queued(), [])

    def test_deleted_issue_comment_wakes_task(self):
        """Deletion used to be observed by polling and must now arrive through ingress."""
        data = comment()
        data["action"] = "deleted"
        self.assertEqual(self.deliver("issue_comment", data), 202)
        self.assertEqual(self.queued(), [KEY])

    def test_base_push_wakes_only_affected_pulls(self):
        """Base changes may have no PR synchronize event; unrelated branches do not wake work."""
        self.registry.link(KEY, 9)
        self.registry.acknowledge(KEY, {"_stage": "gates", "pull": {"base_ref": "main"}})
        self.deliver("push", payload(ref="refs/heads/other"))
        self.assertEqual(self.queued(), [])
        self.deliver("push", payload(ref="refs/heads/main"))
        self.assertEqual(self.queued(), [KEY])

    def test_atomic_receipt_survives_restart(self):
        """A committed enqueue is durable and redelivery after reopening is a no-op."""
        self.deliver("issue_comment", comment(), "restart")
        self.registry.close()
        self.registry = self.open()
        self.ingress.registry = self.registry
        self.assertEqual(self.deliver("issue_comment", comment(), "restart"), 200)
        self.assertEqual(self.queued(), [KEY])


class IdleWakeupTests(ServiceFixture, unittest.TestCase):
    """No recurring controller ticks once readiness is waiting for external events."""

    def test_handoff_sleeps_until_changed_input(self):
        """An idle hour performs no engine reads or model calls; a comment still wakes it."""
        self.service()
        self.reconciler.poll()
        self.drain()
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        self.reconciler.poll()
        self.drain()
        self.assertTrue(self.state()["handoff_ready"])
        self.assertEqual(self.registry.outcome(KEY).kind, "await-human")
        with patch.object(self.backend, "request", wraps=self.backend.request) as request:
            for _ in range(12):
                self.clock.now += 300
                self.assertEqual(self.drain(), 0)
            self.assertEqual(request.call_count, 0)
        self.registry.enqueue(KEY, "webhook:issue_comment", self.clock())
        self.assertEqual(self.drain(), 1)
