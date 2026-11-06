"""Commit publication tolerates tree visibility lag without replaying uncertain writes."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from fixtures import ENGINE, KEY, ServiceFixture
from support import setUpModule, tearDownModule
from github import Transient
from service.errors import OwnershipLost, Unresolved
from service.gateway import Rejected, TREE_RETRY_DELAYS, WRITE_INTERVAL, settle
from service.transport import Response


class CommitPublicationTests(ServiceFixture, unittest.TestCase):
    """Retry only an explicit missing-tree rejection, under the original ownership."""

    def setUp(self):
        """Create a valid tree through the real gateway before asking for a commit."""
        super().setUp()
        self.registry.bind(KEY, KEY[0], KEY[0], ENGINE)
        self.generation = self.gateway.acquire(KEY, 'worker')
        self.gh = self.facade(self.generation)
        tree = self.gh.repo('git/trees', 'POST', {
            'base_tree': self.fake.commits[self.fake.base]['tree']['sha'],
            'tree': [{'path': 'docs/guide.md', 'mode': '100644', 'type': 'blob', 'content': 'Changed\n'}]})
        self.body = {'tree': tree['sha'], 'parents': [self.fake.base], 'message': 'Implement the plan'}
        self.write_clock.sleep(WRITE_INTERVAL)

    def commit(self):
        """Publish the same immutable tree and parent through the service facade."""
        return self.gh.repo('git/commits', 'POST', self.body)

    def test_missing_tree_retries_same_request_then_publishes_once(self):
        faults = [self.backend.fail('POST', 'git/commits', status=422, message='Tree SHA does not exist')
                  for _ in TREE_RETRY_DELAYS]
        before = deepcopy(self.body)
        started = self.write_clock()
        with patch.object(self.gateway, 'write_sleep', wraps=self.write_clock.sleep) as sleep:
            commit = self.commit()
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(TREE_RETRY_DELAYS))
        self.assertEqual(self.write_clock() - started, sum(TREE_RETRY_DELAYS))
        self.assertEqual([f['seen'] for f in faults], [before] * len(faults))
        self.assertEqual(self.body, before)
        self.assertEqual(self.fake.commits[commit['sha']]['tree']['sha'], before['tree'])
        self.assertEqual(self.fake.trees[before['tree']]['docs/guide.md'], 'Changed\n')
        self.assertEqual(self.backend.writes, [('POST', 'git/trees'), ('POST', 'git/commits')])
        self.assertEqual(len(self.registry.mutations(KEY, 'failed')), 2)
        self.assertEqual(len(self.registry.mutations(KEY, 'done')), 2)
        self.assertEqual(self.registry.unresolved(KEY), [])

    def test_structured_error_and_normalized_text_recover(self):
        """Missing-tree codes survive prose changes; legacy case and spacing are insignificant."""
        payloads = ({'message': 'Validation Failed', 'errors': [{'field': 'tree', 'code': 'missing'}]},
                    {'message': '  TREE  sha does not exist. \n'})
        send = self.backend.send
        for payload in payloads:
            with self.subTest(payload=payload):
                replies = [Response(True, 422, {}, json.dumps(payload).encode())]

                def respond(*args):
                    """Return the validation failure once, then publish through the normal backend."""
                    return replies.pop() if replies else send(*args)

                self.write_clock.sleep(WRITE_INTERVAL)
                with patch.object(self.backend, 'send', side_effect=respond) as sent:
                    self.assertIn('sha', self.commit())
                    self.assertEqual(sent.call_count, 2)

    def test_structured_unrelated_or_mixed_errors_remain_rejections(self):
        """Structured validation takes precedence over a misleading top-level message."""
        missing = {'field': 'tree', 'code': 'missing'}
        cases = ([{'field': 'parents', 'code': 'missing'}], [{'field': 'tree', 'code': 'invalid'}],
                 [{'field': 'tree', 'code': 'missing_field'}], [missing, {'field': 'message', 'code': 'invalid'}],
                 ['missing'], {'field': 'tree', 'code': 'missing'})
        for errors in cases:
            with self.subTest(errors=errors):
                response = Response(True, 422, {}, json.dumps({
                    'message': 'Tree SHA does not exist', 'errors': errors}).encode())
                status, _, error = settle(f'repos/{KEY[0]}/git/commits', response, 0, self.body)
                self.assertEqual(status, 'failed')
                self.assertIs(type(error), Rejected)

    def test_other_rejections_and_lost_responses_are_not_retried(self):
        for fault, expected in (({'status': 422, 'message': 'Invalid parent'}, Rejected),
                                ({'status': 500, 'message': 'Tree SHA does not exist'}, Unresolved),
                                ({'lost': True}, Unresolved), ({'truncated': True}, Transient)):
            with self.subTest(fault=fault):
                self.write_clock.sleep(WRITE_INTERVAL)
                # Each case has fresh ownership; unknown outcomes need explicit operator resolution.
                self.backend.fail('POST', 'git/commits', **fault)
                with patch.object(self.gateway, 'write_sleep', wraps=self.write_clock.sleep) as sleep, \
                        patch.object(self.backend, 'send', wraps=self.backend.send) as send:
                    with self.assertRaises(expected):
                        self.commit()
                    sleep.assert_not_called()
                    self.assertEqual(send.call_count, 1)
                self.gateway.release(KEY, self.generation, None)
                for record in self.registry.unresolved(KEY):
                    self.registry.resolve(record.intent, 'applied' if fault.get('lost') else 'not-applied',
                                          'fixture operator reconciliation', 'operator')
                self.generation = self.gateway.acquire(KEY, 'worker')
                self.gh = self.facade(self.generation)

    def test_missing_tree_message_on_other_endpoint_is_not_retried(self):
        self.backend.fail('POST', 'issues/7/comments', status=422, message='Tree SHA does not exist')
        with patch.object(self.gateway, 'write_sleep', wraps=self.write_clock.sleep) as sleep:
            with self.assertRaises(Rejected):
                self.gh.repo('issues/7/comments', 'POST', {'body': 'hello'})
            sleep.assert_not_called()

    def test_ownership_change_during_delay_prevents_another_write(self):
        self.backend.fail('POST', 'git/commits', status=422, message='Tree SHA does not exist')

        def takeover(delay):
            """Replace the generation while the first worker waits."""
            self.write_clock.sleep(delay)
            self.gateway.release(KEY, self.generation, None)
            self.assertEqual(self.gateway.acquire(KEY, 'replacement'), self.generation + 1)

        with patch.object(self.gateway, 'write_sleep', side_effect=takeover), \
                patch.object(self.backend, 'send', wraps=self.backend.send) as send:
            with self.assertRaises(OwnershipLost):
                self.commit()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.registry.mutations(KEY, 'failed')), 1)


class PublicationLifecycleTests(ServiceFixture, unittest.TestCase):
    """A temporary lag recovers within the tick; a persistent rejection blocks durably."""

    def publish_with_lag(self, failures):
        """Run the real worker through planning and implementation with commit rejections."""
        self.service()
        faults = [self.backend.fail('POST', 'git/commits', status=422,
                                   message='Tree SHA does not exist') for _ in range(failures)]
        with patch.object(self.gateway, 'write_sleep', wraps=self.write_clock.sleep):
            self.reconciler.poll()
            self.drain()
            self.fake.comment('/concurro approve ' + self.state()['plan_id'])
            self.reconciler.poll()
            self.drain()
        self.assertEqual(len([c for c in self.agents.calls if c[1] == 'develop']), 1)
        self.assertTrue(all('seen' in f for f in faults))
        commits = [r for status in ('done', 'failed') for r in self.registry.mutations(KEY, status)
                   if r.path.endswith('/git/commits')]
        self.assertEqual(len(commits), min(failures + 1, len(TREE_RETRY_DELAYS) + 1))
        self.assertEqual(self.backend.writes.count(('POST', 'git/trees')), 1)

    def test_tree_lag_recovers_without_another_implementation_turn(self):
        self.publish_with_lag(1)
        self.assertEqual(self.stage(), 'gates')
        self.assertIsNotNone(self.fake.pr)

    def test_persistent_rejection_stays_blocked_after_three_attempts(self):
        self.publish_with_lag(3)
        self.assertEqual(self.stage(), 'blocked')
        self.assertEqual(self.registry.outcome(KEY).kind, 'blocked')
        self.assertIsNone(self.fake.pr)
        writes = len(self.backend.writes)
        for _ in range(3):
            self.clock.now += 3600
            self.assertEqual(self.reconciler.poll(), [])
            self.assertEqual(self.drain(), 0)
        self.assertEqual(len(self.backend.writes), writes)
