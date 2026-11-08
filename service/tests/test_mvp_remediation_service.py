"""MVP review regressions at the service/registry boundary."""

from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import KEY, ServiceFixture
from controller import Outcome
from github import Transient
from service.gateway import Disallowed
from service.ingress import Ingress


class ReviewRegressions(ServiceFixture, unittest.TestCase):
    """CI cannot authorize work, and unrelated issues cannot pin task configuration."""

    def test_fork_check_events_map_by_sha_and_cannot_wake_model_backoff(self):
        self.registry.link(KEY, 9)
        snapshot = {'issue': {'state': 'open'}, 'pull': {'head': 'a' * 40, 'checks': []}, '_stage': 'gates'}
        self.registry.acknowledge(KEY, snapshot)
        ingress = Ingress(b's', self.registry, {KEY[0]: 1}, set())
        for event in ('check_run', 'check_suite'):
            payload = {'sender': {'login': 'github-actions[bot]'}, event: {'head_sha': 'a' * 40, 'pull_requests': []}}
            self.assertEqual(ingress.keys(KEY[0], event, payload), [KEY])
        self.registry.enqueue_ci(KEY)
        self.assertEqual(len(self.registry.due(self.clock())), 1)
        # A CI event during a tick is also removed if that tick then defers.
        generation = self.registry.grant(KEY, 'worker', 60)
        self.registry.enqueue_ci(KEY)
        self.registry.defer(KEY, snapshot)
        self.registry.end(KEY, generation, Outcome('wait-until', retry_after=7200))
        newer = deepcopy(snapshot)
        newer['pull']['checks'] = ['different result']
        self.registry.enqueue_ci(KEY)
        self.assertFalse(self.registry.observe(KEY, newer))
        self.assertEqual(self.registry.due(self.clock()), [])
        self.registry.close()
        self.registry = self.open()
        self.registry.enqueue_ci(KEY)
        self.assertFalse(self.registry.observe(KEY, newer))
        self.assertEqual(self.registry.due(self.clock()), [])
        newer['comments'] = [[9, 'new human input']]
        self.assertTrue(self.registry.observe(KEY, newer))
        self.assertEqual(len(self.registry.due(self.clock())), 1)

    def test_ci_during_failed_model_tick_does_not_retry_it_early(self):
        self.service()
        self.registry.enqueue(KEY, 'test', self.clock())
        self.drain()
        self.fake.comment('/concurro approve ' + self.state()['plan_id'])
        self.reconciler.poll()
        self.drain()
        # The next design/model tick sees a transient failure plus unrelated CI activity.
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            self.registry.enqueue_ci(KEY)
            self.reconciler.poll()
            raise Transient('throttled', 7200)
        # Queue explicit writer input to enter a new repair after the PR reached gates.
        for _ in range(8):
            self.reconciler.poll()
            self.drain()
        self.fake.comment('Please repair the CI-reported example within scope.')
        self.fake.comment('/concurro retry')
        with patch.object(self.agents, 'ask', side_effect=fail):
            self.reconciler.poll()
            self.drain()
            self.fake.checks_green = False
            self.registry.enqueue_ci(KEY)
            self.reconciler.poll()
            self.assertEqual(self.drain(), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.registry.outcome(KEY).retry_after, 7200)

    def test_unenrolled_issue_does_not_pin_config(self):
        self.service()
        from service.worker import DeferredExecutor
        built = []
        def build():
            self.assertIsNotNone(self.registry.binding(KEY), 'pin precedes runner authorization')
            built.append(True)
            return SimpleNamespace(gateway={})
        self.worker.make_executor = lambda *args: DeferredExecutor(build)
        agents = self.agents
        def factory(config, executor, **kwargs):
            class Deferred:
                def credential(inner, *args):
                    return agents.credential(*args)
                def ask(inner, *args):
                    _ = executor.gateway
                    return agents.ask(*args)
            return Deferred()
        self.worker.agents = factory
        self.fake.issue['labels'] = []
        self.fake.comments[0]['body'] = '/concurro help'
        self.registry.enqueue(KEY, 'webhook:issue_comment', self.clock())
        self.drain()
        self.assertIsNone(self.registry.binding(KEY))
        self.assertEqual(self.agents.calls, [])
        self.assertEqual(built, [])
        self.fake.comment('/concurro start')
        self.reconciler.poll()
        self.drain()
        self.assertIsNotNone(self.registry.binding(KEY))
        self.assertTrue(built)

    def test_gateway_rejects_foreign_author_and_other_recorded_pull(self):
        from policy import task_branch
        self.registry.bind(KEY, KEY[0], KEY[0], 'a' * 40)
        self.fake.pr = {'number': 9, 'user': {'login': 'outsider'}, 'head': {
            'ref': task_branch(*KEY), 'repo': {'full_name': KEY[0]}}}
        generation = self.gateway.acquire(KEY, 'worker')
        path = f'repos/{KEY[0]}/pulls/9/reviews'
        body = {'event': 'COMMENT', 'commit_id': 'c'*40, 'body': 'Review.'}
        with self.assertRaises(Disallowed):
            self.gateway.mutate(KEY, generation, 'POST', path, body)
        self.gateway.pulls.clear()
        self.fake.pr['user']['login'] = self.config['bot_login']
        self.fake.pr['head']['repo']['full_name'] = KEY[0].upper()
        self.assertTrue(self.gateway.owned_pull(KEY, 9))
        self.registry.link(KEY, 10)
        self.assertFalse(self.gateway.owned_pull(KEY, 9), 'recorded identity checked even after a cached verification')
