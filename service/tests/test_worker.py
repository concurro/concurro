"""The worker and scheduler drive the real controller: end to end, failures, waits and isolation."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
import unittest

from fixtures import ENGINE, KEY, Inline, RestBackend, ServiceFixture, target_files
from support import setUpModule, tearDownModule  # unittest module fixtures
from test_pipeline import FakeAgents, FakeGitHub, FakeWorkspace
from controller import Controller, Outcome, config_digest
from github import Transient
import reviews
from policy import Blocked, UncertainOutcome
from service.errors import OwnershipLost, Unresolved
from service.gateway import LEASE_SECONDS, MutationGateway
from service.reconciler import Reconciler
from service.scheduler import Scheduler, fair
from service.registry import WorkItem
from service.settings import Limits, Target
from service.worker import Worker


class FlakyAgents(FakeAgents):
    """Fail the first call of one stage with a chosen error, then answer normally."""

    def __init__(self, stage, error):
        super().__init__()
        self.failing = (stage, error)

    def ask(self, provider, stage, context):
        if self.failing and stage == self.failing[0]:
            error, self.failing = self.failing[1], None
            raise error
        return super().ask(provider, stage, context)


class EndToEndTests(ServiceFixture, unittest.TestCase):
    """A fake-provider task advances from intake to `gates` through the service, as through Actions."""

    def actions_path(self):
        """The same task through the Actions controller: its model calls and final stage."""
        fake, agents = FakeGitHub(deepcopy(self.config)), FakeAgents()
        for _ in range(12):
            controller = Controller(fake, agents, fake.config, workspace_factory=FakeWorkspace)
            controller.advance(7)
            if controller.state["stage"] == "planned":
                fake.comment("/concurro approve " + controller.state["plan_id"])
            if controller.state["stage"] == "gates":
                break
        return [(provider, stage) for provider, stage, _ in agents.calls], controller.state["stage"]

    def test_intake_to_gates(self):
        """Webhook-free: the reconciler finds the task, the scheduler ticks it, approval wakes it."""
        self.service()
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.drain()
        self.assertEqual(self.stage(), "planned")
        self.assertEqual(self.drain(), 0, "a task awaiting approval is not dispatched")
        calls = len(self.agents.calls)
        self.assertEqual(self.reconciler.poll(), [])
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        self.assertEqual(self.reconciler.poll(), [KEY])
        for _ in range(4):
            self.drain()
            self.reconciler.poll()
        self.assertEqual(self.stage(), "gates")
        self.assertGreater(len(self.agents.calls), calls)
        state = self.state()
        self.assertEqual(state["controller"], "service:primary")
        self.assertEqual(state["config_digest"], config_digest(self.config))
        self.assertEqual(self.registry.pull(KEY), state["pr"])
        expected = self.actions_path()
        self.assertEqual(([(p, s) for p, s, _ in self.agents.calls], self.stage()), expected)

    def test_waiting_tasks_make_no_model_calls(self):
        """`await-human`, `blocked` and a `wait-until` before its time are never dispatched."""
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        calls = len(self.agents.calls)
        for _ in range(3):
            self.reconciler.poll()
            self.drain()
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(self.registry.outcome(KEY).kind, "await-human")


class FailureTests(ServiceFixture, unittest.TestCase):
    """Failures keep or suspend the stage as classified, and resume without `/concurro retry`."""

    def to_plan(self):
        """Tick until the plan stage is about to call its model."""
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())

    def test_transient_read_preserves_the_stage(self):
        """A GitHub read failure mid-tick waits for its delay, then resumes."""
        self.to_plan()
        self.backend.fail_read("git/trees/", Transient("unavailable"))
        self.drain()
        outcome = self.registry.outcome(KEY)
        self.assertEqual((outcome.kind, outcome.retry_after), ("wait-until", 60))
        stage = self.state()["stage"]
        self.assertNotIn(stage, ("blocked", "stopped"))
        self.assertEqual(self.drain(), 0, "not due before its time")
        self.clock.now += 61
        self.drain()
        self.assertEqual(self.stage(), "planned")
        self.assertNotIn("concurro:attention", self.backend.labels())

    def test_polling_keeps_the_retry_delay(self):
        """Polling unchanged input during a wait does not hasten the retry; new input does."""
        self.to_plan()
        self.backend.fail_read("git/trees/", Transient("throttled", 7200))
        self.drain()
        self.assertEqual(self.registry.outcome(KEY).retry_after, 7200)
        for _ in range(2):
            self.assertEqual(self.reconciler.poll(), [])
            self.assertEqual(self.drain(), 0)
        self.clock.now += 7201
        self.drain()
        self.assertEqual(self.stage(), "planned")
        self.backend.fail_read("git/trees/", Transient("throttled", 7200))
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        self.reconciler.poll()
        self.drain()
        self.assertEqual(self.registry.outcome(KEY).retry_after, 7200)
        self.fake.comment("A clarification arriving during the wait.")
        self.assertEqual(self.reconciler.poll(), [KEY], "new input still wakes the task")

    def test_poll_during_the_failing_tick_keeps_the_retry_delay(self):
        """A poll queued mid-tick for the tick's own input does not survive the tick's retry delay."""
        self.to_plan()
        request, polled = self.backend.request, []

        def throttled(method, path, data=None):
            if "/git/trees/" in path and not polled:
                polled.append(self.reconciler.poll())
                raise Transient("throttled", 7200)
            return request(method, path, data)

        self.backend.request = throttled
        self.drain()
        self.assertEqual(polled, [[KEY]], "the poll saw the running tick's unacknowledged input")
        self.assertEqual(self.registry.outcome(KEY).retry_after, 7200)
        self.assertEqual(self.registry.due(self.clock()), [])
        self.assertEqual(self.reconciler.poll(), [])
        self.clock.now += 7201
        self.drain()
        self.assertEqual(self.stage(), "planned")

    def test_uncertain_model_call_preserves_the_stage(self):
        """An uncertain call waits, and the next tick makes the one permitted retry."""
        self.agents = FlakyAgents("plan", UncertainOutcome("unknown"))
        self.to_plan()
        self.drain()
        self.assertEqual(self.registry.outcome(KEY).kind, "wait-until")
        self.assertEqual(self.state()["operation"]["status"], "started")
        self.clock.now += 61
        self.drain()
        self.assertEqual(self.stage(), "planned")

    def test_blocked_suspends_and_acknowledges(self):
        """A policy failure suspends the task; only new input wakes it."""
        self.agents = FlakyAgents("plan", Blocked("plan refused by policy"))
        self.to_plan()
        self.drain()
        state = self.state()
        self.assertEqual((state["stage"], state["reason"]), ("blocked", "plan refused by policy"))
        self.assertEqual(self.registry.outcome(KEY).kind, "blocked")
        notices = lambda: [c["body"] for c in self.fake.comments if "### 🛑 Blocked" in c["body"]]
        self.assertEqual(len(notices()), 1, "posted when the task blocks, not on its next tick")
        self.assertIn("plan refused by policy", notices()[0])
        self.assertEqual(self.reconciler.poll(), [])
        self.fake.comment("/concurro retry")
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.drain()
        self.assertEqual(self.stage(), "planned")
        self.assertEqual(len(notices()), 1, "the retry posts no stale notice")

    def test_exhausted_pr_publication_stays_blocked(self):
        """Persistent review rejections block after the attempt limit, and nothing retries them until a writer acts."""
        for _ in range(12):
            self.backend.fail("POST", "pulls/9/reviews", status=422)
        self.service()
        self.reconciler.poll()
        self.drain()
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        for _ in range(8):
            self.reconciler.poll()
            self.drain()
            self.clock.now += 3601
        attempts = lambda: [m for m in self.registry.mutations(KEY, "failed") if m.path.endswith("/pulls/9/reviews")]
        self.assertEqual((self.state()["stage"], self.registry.outcome(KEY).kind), ("blocked", "blocked"))
        self.assertEqual(len(attempts()), reviews.MAX_ATTEMPTS)
        self.assertEqual(self.reconciler.poll(), [])
        self.assertEqual(self.drain(), 0)
        self.assertEqual(len(attempts()), reviews.MAX_ATTEMPTS, "no automatic retries past the limit")

    def test_unpinned_target_runs_the_served_engine(self):
        """A target without an `engine` pin accepts whatever engine the operator serves."""
        self.backend.files[".concurro/config.json"] = json.dumps({k: v for k, v in self.config.items() if k != "engine"})
        self.service()
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.drain()
        self.assertEqual(self.stage(), "planned")

    def test_target_mismatches_make_no_writes(self):
        """Another engine pin, App or merge setting is refused before any state is read or written."""
        self.service()
        for change in ({"engine": "f" * 40}, {"bot_login": "other[bot]"}, {"merge_enabled": True},
                       {"repository": "owner/elsewhere"}):
            with self.subTest(change=change):
                self.backend.files[".concurro/config.json"] = json.dumps({**self.config, "engine": ENGINE, **change})
                self.registry.enqueue(KEY, "test", self.clock())
                self.drain()
                self.assertEqual(self.backend.writes, [])
                self.assertEqual(self.registry.outcome(KEY).kind, "wait-until")
                self.assertIsNone(self.registry.acknowledged(KEY))
                self.clock.now += 3601


class StopTests(ServiceFixture, unittest.TestCase):
    """Stops reach the worker's own failure handling: no outcome, no acknowledgment, no further write."""

    def test_takeover_mid_tick(self):
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())
        original, after = self.gateway.mutate, []

        def mutate(key, generation, method, path, body=None):
            if after:
                after.append(path)
            elif "/artifacts" in path:
                self.clock.now += LEASE_SECONDS + 1
                self.assertIsNotNone(self.gateway.acquire(KEY, "successor"))
                after.append(None)
            return original(key, generation, method, path, body)

        self.gateway.mutate = mutate
        self.assertEqual(self.drain(), 1)
        self.assertEqual(after, [None], "no write was attempted after the stop")
        self.assertIsNone(self.registry.outcome(KEY))
        self.assertIsNone(self.registry.acknowledged(KEY))
        self.assertIsInstance(self.gateway.stop(KEY, 1), OwnershipLost)
        self.assertEqual(self.backend.writes, [])

    def test_unknown_write_mid_tick(self):
        """The worker returns `Unresolved`: no suspension or health write, and the task is not scheduled."""
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())
        self.backend.fail("POST", "issues/7/artifacts", lost=True)
        generation = self.gateway.acquire(KEY, "test")
        result = self.worker.run(KEY, generation)
        self.gateway.release(KEY, generation, None)
        self.assertIsInstance(result, Unresolved)
        self.assertEqual(self.backend.writes, [("POST", "issues/7/artifacts")])
        self.assertIsNone(self.registry.acknowledged(KEY))
        self.registry.enqueue(KEY, "poll", self.clock())
        self.assertEqual(self.drain(), 0)


class SchedulerTests(ServiceFixture, unittest.TestCase):
    """A task that cannot be acquired never holds a slot."""

    def test_unresolved_task_does_not_starve_others(self):
        ran = []
        worker = type("Worker", (), {"run": lambda _, key, generation: ran.append(key) or Outcome("await-human")})()
        scheduler = Scheduler(self.registry, self.gateway, worker, Limits(1, 1), clock=self.clock,
                              executor=Inline())
        stuck, healthy = KEY, (KEY[0], 8)
        generation = self.registry.grant(stuck, "worker", 60)
        self.registry.settle(self.registry.admit(stuck, generation, "POST", "p", "d"), "unknown")
        self.registry.end(stuck, generation, None)
        self.registry.enqueue(stuck, "old", self.clock() - 10)
        self.registry.enqueue(healthy, "new", self.clock())
        self.assertEqual(scheduler.dispatch(), [healthy])
        self.assertEqual(ran, [healthy])
        self.assertEqual([item.key for item in self.registry.due(self.clock())], [stuck])


class TwoTargets(ServiceFixture, unittest.TestCase):
    """Two targets tick concurrently, each with only its own configuration, bundle, mirror and credentials."""

    def test_isolation(self):
        bot = self.config["bot_login"]
        second = {**deepcopy(self.config), "repository": "owner/two"}
        fakes = {KEY[0]: self.fake, "owner/two": FakeGitHub(second)}
        backends = {KEY[0]: self.backend, "owner/two": RestBackend(fakes["owner/two"])}
        charter = json.loads(backends["owner/two"].files[".concurro/charter.json"])
        backends["owner/two"].files = target_files(second, json.dumps({**charter, "id": "second-charter"}))
        seen, barrier = {}, threading.Barrier(2, timeout=30)

        def agents(config, mirror, credentials, bundle):
            repository = config["repository"]
            credential = credentials.resolve("codex", "intake", None)
            seen[repository] = {"config": config["repository"], "bundle": bundle.identity, "mirror": mirror.path,
                                "credentials": credentials.material(credential)}
            barrier.wait()
            return FakeAgents()

        targets = (Target(KEY[0], self.credentials("one.json")), Target("owner/two", self.credentials("two.json")))
        settings = type("Settings", (), {"controller_id": "service:primary", "engine": ENGINE, "targets": targets,
                                         "limits": Limits(2, 1), "state_dir": Path(self.directory.name)})
        gateway = MutationGateway(self.registry, backends.__getitem__, backends.__getitem__)
        reconciler = Reconciler(self.registry, backends.__getitem__, list(fakes), {bot}, clock=self.clock)
        worker = Worker(settings, gateway, self.registry, reconciler, backends.__getitem__, bot, agents=agents,
                        workspace_factory=FakeWorkspace)
        with ThreadPoolExecutor(2) as pool:
            scheduler = Scheduler(self.registry, gateway, worker, settings.limits, clock=self.clock, executor=pool)
            self.assertEqual(sorted(reconciler.poll()), [(KEY[0], 7), ("owner/two", 7)])
            self.assertEqual(len(scheduler.dispatch()), 2)
            self.assertTrue(scheduler.wait(30))
        self.assertEqual(seen[KEY[0]]["credentials"], {"CODEX_API_KEY": "fixture-one.json"})
        self.assertEqual(seen["owner/two"]["credentials"], {"CODEX_API_KEY": "fixture-two.json"})
        self.assertNotEqual(seen[KEY[0]]["bundle"], seen["owner/two"]["bundle"])
        for repository in fakes:
            self.assertEqual(seen[repository]["config"], repository)
            self.assertEqual(seen[repository]["mirror"],
                             Path(self.directory.name) / "mirrors" / (repository.replace("/", "__") + ".git"))


class FairnessTests(unittest.TestCase):
    """Round-robin across repositories, oldest first within each; limits are never exceeded."""

    def items(self, *keys):
        return [WorkItem(key, ("test",), index) for index, key in enumerate(keys)]

    def test_round_robin(self):
        due = self.items(("a/a", 1), ("a/a", 2), ("a/a", 3), ("b/b", 1), ("c/c", 1), ("b/b", 2))
        self.assertEqual(fair(due, {}, Limits(10, 10)),
                         [("a/a", 1), ("b/b", 1), ("c/c", 1), ("a/a", 2), ("b/b", 2), ("a/a", 3)])

    def test_limits(self):
        due = self.items(("a/a", 1), ("a/a", 2), ("a/a", 3), ("b/b", 1), ("b/b", 2))
        self.assertEqual(fair(due, {}, Limits(3, 2)), [("a/a", 1), ("b/b", 1), ("a/a", 2)])
        self.assertEqual(fair(due, {("a/a", 9): 1}, Limits(3, 2)), [("a/a", 1), ("b/b", 1)])
        self.assertEqual(fair(due, {("a/a", 1): 1, ("b/b", 9): 1, ("c/c", 9): 1}, Limits(3, 2)), [])
        self.assertNotIn(("a/a", 1), fair(due, {("a/a", 1): 1}, Limits(9, 9)), "one tick per task")


class LeaseTests(ServiceFixture, unittest.TestCase):
    """The worker renews its lease while a tick runs."""

    def test_renewal_during_a_tick(self):
        self.service()
        self.worker.renew_seconds = 0.01
        renewed = threading.Event()
        original = self.gateway.renew

        def renew(key, generation, *args):
            renewed.set()
            return original(key, generation, *args)

        self.gateway.renew = renew
        ask = self.agents.ask

        def slow(provider, stage, context):
            renewed.wait(5)
            return ask(provider, stage, context)

        self.agents.ask = slow
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        self.assertTrue(renewed.is_set())
        self.assertEqual(self.registry.outcome(KEY).kind, "await-human")


if __name__ == "__main__":
    unittest.main()
