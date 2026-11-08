"""Pre-extraction contracts for serial and batch accounting and recovery boundaries."""

from contextlib import contextmanager, ExitStack
from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
from design_batch import Design
from fakes import BatchAgents, FakeGitHub, Crash, named_config
from pipeline_fixtures import ConsensusFixture
from policy import Blocked, SessionFailure, UncertainOutcome, digest
import sessions
import design_eligibility

UNCERTAIN_MESSAGE = 'model call outcome was uncertain twice; operator reconciliation required'


class Prepared(BaseException):
    """Stop after the real admission pre-check, before any attempt or charge."""


class AccountingTests(ConsensusFixture, unittest.TestCase):
    """Trace real controller saves and fake GitHub writes without external services."""

    def setUp(self):
        """Use three distinct reviewers to distinguish success and two recovery paths."""
        super().setUp()
        self.config = named_config()
        self.config['agents']['plan_reviewers'] = ['sol-high', 'astra-high', 'opus-high']
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.agents = BatchAgents()
        self.reach('plan_qc')
        self.agents.events.clear()

    def batch(self):
        """Open the real policy adapter against the saved plan."""
        controller = self.loaded()
        adapter = Design(controller)
        keys = adapter.open()
        return controller, adapter, keys

    @contextmanager
    def trace(self, controller):
        """Pin boundary order and the ledger visible at every save and transport call."""
        events, snapshots, requests = [], [], []

        def watch(name, original):
            """Wrap a real side effect without changing its result or exception handling."""
            def invoke(*args, **kwargs):
                events.append(name)
                snapshots.append((name, deepcopy(controller.state)))
                return original(*args, **kwargs)
            return invoke

        def repo(path, method='GET', data=None):
            """Capture writes in relation to controller boundaries, not volatile artifact bodies."""
            requests.append((len(events), method, path, controller.state['calls']))
            return original_repo(path, method, data)

        original_repo = self.gh.repo
        with ExitStack() as stack:
            for name in ('begin_attempt', 'save', 'checkpoint'):
                stack.enter_context(patch.object(controller, name, side_effect=watch(name, getattr(controller, name))))
            for name in ('prepare', 'submit', 'ask', 'decode'):
                stack.enter_context(patch.object(self.agents, name, side_effect=watch(name, getattr(self.agents, name))))
            stack.enter_context(patch.object(self.gh, 'repo', side_effect=repo))
            yield events, snapshots, requests

    def assert_ledger(self, state, calls, credentials):
        """Assert both global and per-route charges, including unchanged credential fields."""
        self.assertEqual(state['calls'], calls)
        self.assertEqual({key: value['calls'] for key, value in state['credentials'].items()}, credentials)
        for key, value in state['credentials'].items():
            expected = self.agents.credential(key.removeprefix('fixture:'), 'plan_qc', {}).record()
            expected.pop('id')
            self.assertEqual(value, {**expected, 'calls': credentials[key]})

    def assert_writes_after_checkpoint(self, trace):
        """No GitHub mutation can precede the checkpoint boundary that persists its charge."""
        events, snapshots, requests = trace
        writes = [(index, method, path, calls) for index, method, path, calls in requests if method != 'GET']
        self.assertTrue(writes)
        for index, _method, _path, calls in writes:
            self.assertEqual(events[index - 1], 'checkpoint')
            self.assertEqual(snapshots[index - 1][1]['calls'], calls)

    def test_serial_intake_saves_charge_before_asking_and_completion_after(self):
        """The sessionless serial path has one reservation save and one completion checkpoint."""
        controller = self.loaded()
        context = {'task': 'independent intake probe'}
        before = controller.state['calls']
        credentials = {k: v['calls'] for k, v in controller.state['credentials'].items()}
        credentials['fixture:sol-medium'] += 1
        with patch.object(controller, 'prepare_context', side_effect=lambda _p, _s, value: dict(value)), self.trace(controller) as trace:
            controller.ask('sol-medium', 'intake', context)
        events, snapshots, _ = trace
        self.assertEqual(events, ['begin_attempt', 'save', 'checkpoint', 'ask', 'checkpoint'])
        self.assertEqual(snapshots[0][1]['calls'], before)
        for _, state in snapshots[1:]:
            self.assert_ledger(state, before + 1, credentials)
        self.assertEqual(snapshots[-1][1]['operation']['status'], 'completed')
        self.assert_writes_after_checkpoint(trace)

    def test_serial_session_completion_binds_the_call_and_original_context(self):
        """Session counters advance once, only after success, before result retention."""
        controller = self.loaded()
        context = {'task': 'independent planning probe'}
        with patch.object(controller, 'prepare_context', side_effect=lambda _p, _s, value: dict(value)), self.trace(controller) as trace:
            response = controller.ask('opus-high', 'plan', context)
        events, snapshots, _ = trace
        self.assertEqual(events, ['begin_attempt', 'save', 'checkpoint', 'ask', 'checkpoint'])
        role = sessions.role_key(self.config, 'opus-high', 'plan', 'claude-opus-5-5')
        before, after = snapshots[-2][1]['sessions'][role], snapshots[-1][1]['sessions'][role]
        self.assertEqual(after['turn'], before['turn'] + 1)
        self.assertEqual(after['completed'], {'call': controller.state['calls'],
                                            'request': digest(context), 'response': digest(response)})
        self.assert_writes_after_checkpoint(trace)

    def test_batch_reservation_numbers_completion_and_recovery_order(self):
        """One successful member and two different failures retain independent accounting."""
        controller, adapter, keys = self.batch()
        before = controller.state['calls']
        providers = [adapter.batch['members'][key]['identity']['provider'] for key in keys]
        self.agents.effects[providers[1]] = [SessionFailure('journal failed')]
        self.agents.effects[providers[2]] = [UncertainOutcome('result lost')]
        decoded = {}
        original_decode = self.agents.decode

        def capture_response(turn, result):
            """Keep the original response before retention normalizes classification data."""
            response = original_decode(turn, result)
            decoded[turn['provider']] = deepcopy(response)
            return response

        with patch.object(self.agents, 'decode', side_effect=capture_response), self.trace(controller) as trace:
            prepared = adapter.reserve(keys, initial=True)
            for key in keys:
                self.agents.submit(prepared[key])
            for key in keys:
                try:
                    adapter.complete(key, prepared[key], {'ok': True}, None)
                except (SessionFailure, UncertainOutcome) as error:
                    adapter.failed(key, error)
            retry = adapter.reserve(keys[1:], initial=False)
            for key in keys[1:]:
                self.agents.submit(retry[key])
                adapter.complete(key, retry[key], {'ok': True}, None)
        events, snapshots, _ = trace
        self.assertEqual(events, [
            'prepare', 'prepare', 'prepare', 'begin_attempt', 'begin_attempt', 'begin_attempt', 'save', 'checkpoint',
            'submit', 'submit', 'submit', 'decode', 'checkpoint',
            'decode', 'save', 'checkpoint', 'decode', 'save', 'checkpoint',
            'prepare', 'prepare', 'begin_attempt', 'begin_attempt', 'save', 'checkpoint',
            'submit', 'decode', 'checkpoint', 'submit', 'decode', 'checkpoint',
        ])
        first_save = next(state for name, state in snapshots if name == 'save')
        self.assertEqual([first_save['review_batch']['members'][k]['operation']['call'] for k in keys],
                         [before + 1, before + 2, before + 3])
        members = adapter.batch['members']
        self.assertEqual([members[k]['operation']['call'] for k in keys], [before + 1, before + 4, before + 5])
        credentials = {'fixture:sol-medium': 1, 'fixture:opus-high': 3, 'fixture:sol-high': 1, 'fixture:astra-high': 2}
        self.assert_ledger(controller.state, before + 5, credentials)
        for key in keys:
            member = members[key]
            session = controller.state['sessions'][member['session']['role']]
            self.assertEqual(session['turn'], 1)
            self.assertEqual(session['completed'], {'call': member['operation']['call'],
                'request': digest(adapter.contexts[key]), 'response': digest(decoded[member['identity']['provider']])})
        self.assertEqual([controller.state['sessions'][members[k]['session']['role']]['restarts'] for k in keys], [0, 1, 0])
        self.assert_writes_after_checkpoint(trace)

    def test_crash_after_serial_reservation_recharges_without_resetting_uncertainty(self):
        """A saved started attempt survives process loss and keeps its first charge."""
        controller = self.loaded()
        context = {'task': 'crash probe'}
        before = controller.state['calls']
        original_save = controller.save

        def save_then_crash(*args, **kwargs):
            """Persist the charged attempt, then lose the process before transport dispatch."""
            original_save(*args, **kwargs)
            raise Crash()

        with patch.object(controller, 'save', side_effect=save_then_crash), patch.object(
                controller, 'prepare_context', side_effect=lambda _p, _s, value: dict(value)), self.trace(controller) as first:
            with self.assertRaises(Crash):
                controller.ask('sol-medium', 'intake', context)
        self.assertEqual(first[0], ['begin_attempt', 'save', 'checkpoint'])
        restored = self.loaded()
        self.assertEqual(restored.state['calls'], before + 1)
        self.assertEqual(restored.state['operation']['status'], 'started')
        with patch.object(restored, 'prepare_context', side_effect=lambda _p, _s, value: dict(value)), self.trace(restored) as second:
            restored.ask('sol-medium', 'intake', context)
        self.assertEqual(second[0], ['begin_attempt', 'save', 'checkpoint', 'ask', 'checkpoint'])
        self.assertEqual(restored.state['calls'], before + 2)
        self.assertEqual(restored.state['credentials']['fixture:sol-medium']['calls'], 3)
        self.assertEqual((restored.state['operation']['attempts'], restored.state['operation']['uncertain']), (2, 1))
        self.assert_writes_after_checkpoint(first)
        self.assert_writes_after_checkpoint(second)

    def test_late_uncertain_refusal_never_begins_or_charges_a_roster(self):
        """Earlier local preparation is allowed, but admission remains atomic."""
        controller, adapter, keys = self.batch()
        # Seed the exact matching logical operations; no attempts have begun.
        for key in keys:
            adapter.select(key)
            context = controller.prepare_context(adapter.batch['members'][key]['identity']['provider'],
                                                 'plan_qc', deepcopy(adapter.contexts[key]))
            controller.operation(adapter.batch['members'][key]['identity']['provider'], 'plan_qc', context)
        last = adapter.batch['members'][keys[-1]]['operation']
        last.update(status='uncertain', uncertain=1)
        before = deepcopy(controller.state)
        with self.trace(controller) as trace, self.assertRaisesRegex(Blocked, '^' + UNCERTAIN_MESSAGE + '$'):
            adapter.reserve(keys, initial=True)
        self.assertEqual(trace[0], ['prepare', 'prepare'])
        self.assertEqual(controller.state['calls'], before['calls'])
        self.assertEqual(controller.state['credentials'], before['credentials'])
        self.assertEqual([m['operation']['attempts'] for m in adapter.batch['members'].values()], [0, 0, 0])
        self.assertFalse(any(method != 'GET' for _, method, _, _ in trace[2]))

    def test_roster_budget_refusal_precedes_all_preparation(self):
        """A too-small task allowance performs no partial admission or persistence."""
        controller, adapter, keys = self.batch()
        before = deepcopy(controller.state)
        controller.config['max_model_calls'] = before['calls'] + 2
        with self.trace(controller) as trace, self.assertRaisesRegex(Blocked, '^model-call budget cannot admit the review roster$'):
            adapter.reserve(keys, initial=True)
        self.assertEqual(trace[0], [])
        self.assertEqual(controller.state, before)
        self.assertFalse(any(method != 'GET' for _, method, _, _ in trace[2]))

    def test_uncertain_count_boundaries(self):
        """Pin begin_attempt and the actual batch pre-check, including completed normalization."""
        expected = {
            'new': ([False, False, True], [False, False, True]),
            'started': ([False, True, True], [False, True, True]),
            'uncertain': ([False, True, True], [False, True, True]),
            'failed': ([False, False, True], [False, False, True]),
            'completed': ([False, False, True], [False, True, True]),
            'consumed': ([False, False, True], [False, False, True]),
        }
        for status, (serial, batch) in expected.items():
            for count in range(3):
                with self.subTest(status=status, count=count):
                    controller, adapter, keys = self.batch()
                    operation = {'id': 'fixed', 'status': status, 'uncertain': count, 'attempts': 0,
                                 'contract': {}, 'attempt': None, 'request': None}
                    original = deepcopy(operation)
                    if serial[count]:
                        with self.assertRaisesRegex(Blocked, '^' + UNCERTAIN_MESSAGE + '$'):
                            controller.begin_attempt(operation, {})
                    else:
                        controller.begin_attempt(operation, {})
                    self.assertEqual(operation['uncertain'], count + (status in ('started', 'uncertain')))
                    # Hold operation selection fixed to measure this check, not journal replacement.
                    adapter.batch['members'][keys[0]]['operation'] = original
                    with patch.object(controller, 'operation', return_value=original), patch.object(
                            self.agents, 'prepare', side_effect=Prepared), self.trace(controller) as trace:
                        if batch[count]:
                            with self.assertRaisesRegex(Blocked, '^' + UNCERTAIN_MESSAGE + '$'):
                                adapter.reserve(keys[:1], initial=False)
                        else:
                            with self.assertRaises(Prepared):
                                adapter.reserve(keys[:1], initial=False)
                    self.assertNotIn('begin_attempt', trace[0])
                    self.assertNotIn('save', trace[0])
                    self.assertEqual(original['uncertain'], count, 'pre-check never consumes uncertainty')

    def test_bookkeeping_helpers_only_change_the_in_memory_ledger(self):
        """The extracted helpers cannot save, dispatch, prepare or restart a session."""
        controller, adapter, keys = self.batch()
        adapter.reserve(keys, initial=True)
        session = adapter.batch['members'][keys[0]]['session']
        credential = self.agents.credential('sol-high', 'plan_qc', {})
        request, response = {'request': 'original'}, {'response': 'original'}
        expected = deepcopy(controller.state)
        expected['calls'] += 1
        expected['credentials'][credential.id]['calls'] += 1
        current = expected['sessions'][session['role']]
        current['turn'] += 1
        current['completed'] = {'call': expected['calls'], 'request': digest(request), 'response': digest(response)}
        controller.consuming = ('retained-operation', 'retained-attempt')
        with ExitStack() as stack:
            for target, names in (
                    (controller, ('save', 'checkpoint', 'begin_attempt', 'prepare_context')),
                    (self.agents, ('prepare', 'submit', 'ask')),
                    (sessions, ('prepare', 'restart')),
                    (design_eligibility, ('prepare', 'restart'))):
                for name in names:
                    stack.enter_context(patch.object(target, name, side_effect=AssertionError(name)))
            call = controller.charge_call(credential)
            controller.record_session_turn(session, call, request, response)
            self.assertEqual(controller.consuming, ('retained-operation', 'retained-attempt'))
            controller.release_result()
        self.assertEqual(call, expected['calls'])
        self.assertEqual(controller.state, expected)
        self.assertIsNone(controller.consuming)
        self.assertEqual(request, {'request': 'original'})
        self.assertEqual(response, {'response': 'original'})

    def test_changing_selected_member_retains_unconsumed_responses(self):
        """A checkpoint may drop only the selected operation's recovered response."""
        controller, adapter, keys = self.batch()
        prepared = adapter.reserve(keys, initial=True)
        for key in keys[:2]:
            adapter.complete(key, prepared[key], {'ok': True}, None)
        first = adapter.select(keys[0])['operation']
        controller.recover(first)
        second = adapter.select(keys[1])['operation']
        controller.checkpoint()
        self.assertIn('response', first)
        self.assertIn('response', second)
        adapter.select(keys[0])
        controller.recover(first)
        controller.checkpoint()
        self.assertEqual(first['status'], 'consumed')
        self.assertNotIn('response', first)
        self.assertEqual(second['status'], 'completed')
        self.assertIn('response', second)

    def test_exhausted_late_member_cannot_persist_partial_roster_charges(self):
        """Even unexpected exhausted states fail before any member starts or is charged."""
        baseline = deepcopy(self.loaded().state)
        for status, count in (('new', 2), ('failed', 2), ('completed', 1)):
            with self.subTest(status=status, count=count):
                controller = self.loaded()
                controller.state = deepcopy(baseline)
                adapter = Design(controller)
                keys = adapter.open()
                for key in keys:
                    member = adapter.select(key)
                    provider = member['identity']['provider']
                    context = controller.prepare_context(provider, 'plan_qc', deepcopy(adapter.contexts[key]))
                    controller.operation(provider, 'plan_qc', context)
                adapter.batch['members'][keys[-1]]['operation'].update(status=status, uncertain=count)
                before = deepcopy(controller.state)
                with self.trace(controller) as trace:
                    with self.assertRaisesRegex(Blocked, '^' + UNCERTAIN_MESSAGE + '$'):
                        adapter.reserve(keys, initial=True)
                controller.suspend('blocked', UNCERTAIN_MESSAGE, publish_events=False, outboxes=False)
                restored = self.loaded().state
                self.assertEqual(restored['stage'], 'blocked')
                self.assertEqual(restored['calls'], before['calls'])
                self.assertEqual(restored['credentials'], before['credentials'])
                self.assertEqual([restored['review_batch']['members'][k]['operation']['attempts'] for k in keys],
                                 [0, 0, 0])
                self.assertEqual(trace[0], ['prepare', 'prepare'])
                self.assertFalse(any(method != 'GET' for _, method, _, _ in trace[2]))

    def test_consumed_operation_gets_a_new_allowance_before_batch_admission(self):
        """The defensive check does not reuse the previous operation's exhausted allowance."""
        controller, adapter, keys = self.batch()
        key = keys[-1]
        member = adapter.select(key)
        provider = member['identity']['provider']
        context = controller.prepare_context(provider, 'plan_qc', deepcopy(adapter.contexts[key]))
        old = controller.operation(provider, 'plan_qc', context)
        old.update(status='consumed', uncertain=2, attempts=2)
        before = controller.state['calls']
        adapter.reserve(keys, initial=True)
        current = adapter.batch['members'][key]['operation']
        self.assertIsNot(current, old)
        self.assertEqual((current['status'], current['uncertain'], current['attempts']), ('started', 0, 1))
        self.assertEqual(controller.state['calls'], before + len(keys))
