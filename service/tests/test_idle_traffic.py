"""Idle-task request accounting and immediate webhook wakeups."""

import unittest

from fixtures import KEY, ServiceFixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from controller import Outcome
from idle_traffic import measure
from service.webhook_recovery import Recovery
from test_webhook_recovery import DeliveryAPI


class TrafficTests(unittest.TestCase):
    """Run the measured full service path, not only a mocked scheduler."""

    def test_storage_and_models_are_not_the_idle_cost(self):
        before, after = measure(True), measure()
        self.assertGreater(before["fleet_rest"], 5000)
        self.assertEqual(after["fleet_rest"], 0)
        self.assertEqual(after["fleet_graphql"], 0)
        self.assertEqual(after["app_delivery_requests"], 12)
        self.assertEqual((before["model_calls"], after["model_calls"], before["writes"], after["writes"]), (0, 0, 0, 0))


class FleetTests(ServiceFixture, unittest.TestCase):
    """Thirteen durable idle tasks share one recovery timer."""

    def test_thirteen_idle_tasks_and_one_wakeup(self):
        self.service()
        for issue in range(7, 20):
            key = (KEY[0], issue)
            generation = self.gateway.acquire(key, "fixture")
            self.registry.acknowledge(key, {"_stage": "gates"})
            self.gateway.release(key, generation, Outcome("await-human"))
        api = DeliveryAPI()
        recovery = Recovery(api, self.registry, self.reconciler, {KEY[0]: 42}, clock=self.clock)
        self.registry.webhook_check(recovery.scope, self.clock())
        for _ in range(12):
            self.clock.now += 300
            recovery.poll()
            self.assertEqual(self.drain(), 0)
        self.assertEqual(len(api.requests), 12)
        self.registry.receive("event", [KEY], "pull_request_review")
        self.assertEqual([item.key for item in self.registry.due(self.clock())], [KEY])
