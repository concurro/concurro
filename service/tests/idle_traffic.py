"""Offline request measurements: unchanged handoff tasks and App-wide delivery recovery."""

import json
import unittest
from collections import Counter
from unittest.mock import patch

from fixtures import KEY, ServiceFixture

from support import setUpModule, tearDownModule

from controller import Controller, Outcome
from service.webhook_recovery import Recovery
from test_webhook_recovery import DeliveryAPI


class Fixture(ServiceFixture, unittest.TestCase):
    """Use the real scheduler, worker, controller and storage over fake GitHub."""

    def ready(self):
        """Reach handoff and warm the same two-CI-ref observation used by the pilots."""
        self.service()
        self.reconciler.poll()
        self.drain()
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        self.reconciler.poll()
        self.drain()
        self.fake.issue["updated_at"] = "2026-11-12T00:00:00Z"
        merge = "b" * 40
        self.fake.pr["merge_commit_sha"] = merge
        self.fake.commits[merge] = {
            "parents": [{"sha": self.fake.pr["base"]["sha"]}, {"sha": self.fake.pr["head"]["sha"]}]
        }
        original = self.backend.request

        def request(method, path, data=None):
            """Supply successful check runs for the synthetic test-merge SHA too."""
            result = original(method, path, data)
            if f"/commits/{merge}/check-runs" in path:
                for check in result.get("check_runs", []):
                    check["head_sha"] = merge
            return result

        self.backend.request = request
        self.registry.enqueue(KEY, "warm", self.clock())
        self.drain()
        self.reconciler.poll()
        self.drain()
        self.assertTrue(self.state()["handoff_ready"])


def measure(legacy=False, tasks=13, periods=12):
    """Measure one real task cycle; scale task reads, keeping repository listing App-wide."""
    fixture = Fixture()
    original_outcome = Controller.outcome

    def outcome(controller, progressed):
        """Reproduce the previous readiness timer without changing any read implementation."""
        if legacy and not progressed and controller.state and controller.state["stage"] == "gates":
            return Outcome("wait-until", retry_after=300)
        return original_outcome(controller, progressed)

    setUpModule()
    fixture.setUp()
    try:
        with patch.object(Controller, "outcome", outcome):
            fixture.ready()
            api = DeliveryAPI()
            recovery = Recovery(api, fixture.registry, fixture.reconciler, {KEY[0]: 42}, clock=fixture.clock)
            fixture.registry.webhook_check(recovery.scope, fixture.clock())
            counts = Counter()
            request = fixture.backend.request

            def recorded(method, path, data=None):
                """Count actual calls; never infer cost from response bytes."""
                counts[(method, path)] += 1
                return request(method, path, data)

            fixture.backend.request = recorded
            calls, writes = len(fixture.agents.calls), len(fixture.backend.writes)
            for _ in range(periods):
                fixture.clock.now += 300
                if legacy:
                    fixture.reconciler.poll()
                else:
                    recovery.poll()
                fixture.drain()
            listing = sum(n for (method, path), n in counts.items() if "/issues?" in path)
            rest = sum(n for (method, _), n in counts.items() if method == "GET")
            graphql = sum(n for (method, path), n in counts.items() if path == "graphql")
            artifacts = sum(n for (_, path), n in counts.items() if "/artifacts/" in path)
            return {
                "tasks": tasks,
                "periods": periods,
                "measured_single_task_rest": rest,
                "measured_single_task_graphql": graphql,
                "fleet_rest": (rest - listing) * tasks + listing,
                "fleet_graphql": graphql * tasks,
                "fleet_artifact_reads": artifacts * tasks,
                "app_delivery_requests": len(api.requests),
                "model_calls": len(fixture.agents.calls) - calls,
                "writes": len(fixture.backend.writes) - writes,
            }
    finally:
        fixture.doCleanups()
        tearDownModule()


if __name__ == "__main__":
    print(json.dumps({"before": measure(True), "after": measure()}, indent=2))
