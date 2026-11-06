"""All tasks share one GitHub write slot; reads and ownership remain independent."""

import threading
import unittest
from unittest.mock import patch

from fixtures import KEY, ServiceFixture
from support import setUpModule, tearDownModule
from service.errors import OwnershipLost
from service.gateway import Rejected
from service.transport import Response


class WritePacingTests(ServiceFixture, unittest.TestCase):
    """Exercise the real gateway with a separate monotonic test clock."""

    def setUp(self):
        """Start one task without spending time on real pacing sleeps."""
        super().setUp()
        self.generation = self.gateway.acquire(KEY, 'worker')
        self.gh = self.facade(self.generation)

    def comment(self):
        """Send an admitted issue comment through the facade."""
        return self.gh.repo('issues/7/comments', 'POST', {'body': 'hello'})

    def test_failed_writes_are_spaced_but_reads_and_idle_time_add_no_delay(self):
        """Failures consume a slot; reads bypass pacing and elapsed idle time is credited."""
        starts = []
        send = self.backend.send

        def record(*args):
            """Record request start time and emulate a two-second response."""
            starts.append(self.write_clock())
            self.write_clock.sleep(2)
            return send(*args)

        self.backend.fail('POST', 'issues/7/comments', status=422)
        with patch.object(self.backend, 'send', side_effect=record), \
                patch.object(self.gateway, 'write_sleep', wraps=self.write_clock.sleep) as sleep:
            with self.assertRaises(Rejected):
                self.comment()
            self.assertEqual(self.gh.repo('issues/7')['number'], 7)
            sleep.assert_not_called()
            self.comment()
            self.assertEqual(starts[1] - starts[0], 3)
            self.assertEqual([call.args for call in sleep.call_args_list], [(1.0,)])
            self.write_clock.sleep(5)
            self.comment()
            self.assertEqual(sleep.call_count, 1)
        self.assertEqual(len(self.registry.mutations(KEY, 'failed')), 1)
        self.assertEqual(len(self.registry.mutations(KEY, 'done')), 2)

    def test_concurrent_repositories_share_one_write_slot(self):
        """A different repository cannot send until the first response plus the interval."""
        other = ('owner/other', 8)
        second_generation = self.gateway.acquire(other, 'second')
        first_sending, second_waiting, second_sending, finish = (threading.Event() for _ in range(4))
        starts, errors = [], []

        def send(method, path, body):
            """Hold the first HTTP request while the other task tries to send."""
            starts.append((path, self.write_clock()))
            if len(starts) == 1:
                first_sending.set()
                if not finish.wait(5):
                    raise TimeoutError('test did not release the first write')
                self.write_clock.sleep(2)
            else:
                second_sending.set()
            return Response(True, 201, {}, b'{}')

        def write(key, generation):
            """Capture background failures for assertions in the test thread."""
            try:
                if key == other:
                    second_waiting.set()
                self.gateway.mutate(key, generation, 'POST', f'repos/{key[0]}/labels', {'name': 'test'})
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=write, args=(KEY, self.generation))
        second = threading.Thread(target=write, args=(other, second_generation))
        with patch.object(self.backend, 'send', side_effect=send):
            try:
                first.start()
                self.assertTrue(first_sending.wait(2))
                second.start()
                self.assertTrue(second_waiting.wait(2))
                self.assertFalse(second_sending.wait(0.1))
                self.assertEqual(self.registry.in_flight(other), [])
                self.assertTrue(self.gateway.renew(other, second_generation, 60))
                self.assertEqual(self.gh.repo('issues/7')['number'], 7)
            finally:
                finish.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1][1] - starts[0][1], 3)
        self.assertEqual(len(self.registry.mutations(other, 'done')), 1)

    def test_generation_changed_while_pacing_is_rechecked_before_admission(self):
        """A sleeping request leaves no mutation record and cannot send after takeover."""
        self.comment()
        writes = len(self.backend.writes)

        def takeover(delay):
            """Grant the task to a new owner while the old request is delayed."""
            self.assertEqual(self.registry.in_flight(KEY), [])
            self.gateway.release(KEY, self.generation, None)
            self.assertEqual(self.gateway.acquire(KEY, 'replacement'), self.generation + 1)
            self.write_clock.sleep(delay)

        with patch.object(self.gateway, 'write_sleep', side_effect=takeover):
            with self.assertRaises(OwnershipLost):
                self.comment()
        self.assertEqual(len(self.backend.writes), writes)
        self.assertEqual(len(self.registry.mutations(KEY, 'done')), 1)
        self.assertEqual(self.registry.mutations(KEY, 'failed'), [])
        self.assertEqual(self.registry.in_flight(KEY), [])
