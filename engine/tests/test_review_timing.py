"""Observed execution timing must not change reports, budgets or recovery decisions."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
from agents import Agents, RunnerExecutor
from policy import RunnerTerminated, SessionFailure, approval_binding, digest
from controller import Controller
from fakes import CONFIG
from runner_fixtures import FakeExecutor, FakeOperator, GATEWAY, TASK, TURN, MODELS
from pipeline_fixtures import ConsensusFixture
import review_timing
from relay import ExchangeError

TURN_ID = '12345678-1234-1234-1234-123456789abc'
OBSERVED = {'turn': TURN_ID, 'status': 'done', 'created': 100, 'started': 110, 'finished': 150}


class TimingTests(unittest.TestCase):
    """Only fixed receipt fields reach state, never credentials or model text."""

    def test_optional_receipts_and_unknown_intervals(self):
        self.assertEqual(review_timing.normalize({**OBSERVED, 'token': 'secret'}), OBSERVED)
        for value in (None, [], {}, {'turn': 'gateway-secret', 'status': 'done'}, {**OBSERVED, 'status': []}):
            self.assertIsNone(review_timing.normalize(value))
        for invalid in (True, '100', [], float('nan'), float('inf'), -1, 10 ** 400):
            with self.subTest(value=type(invalid).__name__):
                receipt = review_timing.normalize({**OBSERVED, 'started': invalid})
                self.assertIsNone(receipt['started'])
                self.assertIsNone(review_timing.durations(receipt)['execution_seconds'])
        self.assertEqual(review_timing.durations(OBSERVED),
                         {'admission_to_claim_seconds': 10, 'execution_seconds': 40})
        for status in ('lost', 'cancelled', 'running', 'queued'):
            self.assertIsNone(review_timing.durations({**OBSERVED, 'status': status})['execution_seconds'])
        self.assertIsNone(review_timing.durations({**OBSERVED, 'finished': 105})['execution_seconds'])

    def test_live_submit_replay_has_timing_but_same_result_and_request(self):
        result = {'outcome': 'ok', 'response': {'ok': True}}
        operator = FakeOperator([ExchangeError(502, ''), {**OBSERVED, 'result': result}])
        runner = RunnerExecutor(operator, GATEWAY, {}, ['claude-*'], 100, lambda _: None,
                                task=TASK, sleep=lambda _: None)
        original = deepcopy(TURN)
        with patch('agents.uuid.uuid4', return_value=TURN_ID):
            self.assertEqual(runner.run(TURN, 'review'), result)
        self.assertEqual(runner.timing, OBSERVED)
        submissions = [args for name, args in operator.calls if name == 'submit']
        self.assertEqual(submissions[0], submissions[1])
        self.assertEqual(TURN, original)
        operator.replies = [{'status': 'done', 'result': result}]
        runner.run(TURN, 'legacy exchange')
        self.assertIsNone(runner.timing, 'a previous turn must not leak into a legacy reply')

    def test_agents_pass_timing_out_of_band_for_success_and_failure(self):
        executor = FakeExecutor()
        executor.timing = OBSERVED
        agents = Agents({**CONFIG, 'models': MODELS}, executor)
        context = {'bundle_id': agents.bundle.identity,
                   'repository': {'refs': {'main': 'a' * 40}, 'head': 'main'}}
        self.assertEqual(agents.ask('codex', 'review', context), {'ok': True})
        self.assertEqual(agents.timing, OBSERVED)
        self.assertNotIn('timing', json.dumps(executor.turns))
        executor.result = {'outcome': 'lost', 'response': None}
        executor.timing = {**OBSERVED, 'status': 'lost'}
        with self.assertRaises(RunnerTerminated):
            agents.ask('codex', 'review', context)
        self.assertEqual(agents.timing['status'], 'lost')

    def test_receipt_pressure_drops_metadata_not_reports_or_pending_bodies(self):
        state = {'reports': [{'timing': OBSERVED, 'artifact': 2}],
                 'conversation': {'pending': [{'index': {'timing': OBSERVED}, 'body': 'immutable'}]},
                 'operation': {'timing': OBSERVED, 'response': {'ok': True}}}
        core = deepcopy(state)
        core['reports'][0].pop('timing')
        core['conversation']['pending'][0]['index'].pop('timing')
        core['operation'].pop('timing')
        review_timing.make_room(state, len(json.dumps(core)), lambda s: len(json.dumps(s)))
        self.assertEqual(state, core)


class ControllerTimingTests(ConsensusFixture, unittest.TestCase):
    """Real checkpoints/outboxes preserve receipts without changing consensus or replay."""

    def timed_agents(self):
        """Instrument the fake transport through the same out-of-band field as real Agents."""
        original = self.agents.ask
        def ask(provider, stage, context):
            self.agents.timing = deepcopy(OBSERVED)
            return original(provider, stage, context)
        return patch.object(self.agents, 'ask', side_effect=ask)

    def test_saved_result_replay_preserves_receipt_without_another_call(self):
        self.reach('plan_qc')
        with self.timed_agents(), patch.object(Controller, 'validate_design_report', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.tick()
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        operation = deepcopy(controller.state['operation'])
        calls = controller.state['calls']
        self.assertEqual(operation['timing'], OBSERVED)
        with patch.object(self.agents, 'ask', side_effect=AssertionError('re-dispatched')):
            state = self.tick()
        receipt = state['reports'][-1]['timing']
        self.assertEqual(receipt, {'operation': operation['id'], 'attempt': operation['attempt'], **OBSERVED})
        self.assertEqual(state['calls'], calls)
        self.assertNotIn('timing', state['qc']['plan_qc'])
        self.assertTrue(next(iter(state['design_votes'].values()))['first_pass'])
        controller.load(self.gh.comments)
        self.assertIn('Observed turn timing', controller.diagnostics_table())

    def test_observations_do_not_change_context_or_approval_binding(self):
        """Adding or dropping receipts cannot alter the work identity or consensus inputs."""
        with self.timed_agents():
            state = self.reach('implement')
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        context = controller.context()
        binding = approval_binding(controller.state, self.config)
        request = controller.state['operation']['request']
        for report in controller.state['reports']:
            report.pop('timing', None)
        controller.state['operation'].pop('timing', None)
        self.assertEqual(controller.context(), context)
        self.assertEqual(approval_binding(controller.state, self.config), binding)
        self.assertEqual(controller.state['operation']['request'], request)
        self.assertEqual(digest(controller.state['qc']['plan_qc']), digest(state['qc']['plan_qc']))

    def test_corrected_reports_keep_their_own_attempt_timing(self):
        self.reach('plan_qc')
        original = self.agents.ask
        invocation = 0
        def ask(provider, stage, context):
            nonlocal invocation
            invocation += 1
            report = original(provider, stage, context)
            self.agents.timing = {**OBSERVED, 'turn': f'{invocation:08x}-1234-1234-1234-123456789abc'}
            if invocation == 1:
                report['scope']['goal_ids'] = ['invalid']
            return report
        with patch.object(self.agents, 'ask', side_effect=ask):
            state = self.tick()
            self.assertEqual(state['reports'][-1]['outcome'], 'invalid')
            state = self.tick()
        rejected, accepted = state['reports'][-2:]
        self.assertNotEqual(rejected['timing']['attempt'], accepted['timing']['attempt'])
        self.assertNotEqual(rejected['timing']['turn'], accepted['timing']['turn'])
        self.assertEqual(accepted['outcome'], 'approve')
        self.assertTrue(next(iter(state['design_votes'].values()))['first_pass'])

    def test_failed_attempt_observation_is_replaced_not_carried_forward(self):
        self.reach('plan_qc')
        original = self.agents.ask
        seen = []
        def ask(provider, stage, context):
            if not seen:
                seen.append(True)
                self.agents.timing = {**OBSERVED, 'status': 'lost'}
                raise SessionFailure('failed', uncertain=True)
            self.agents.timing = None
            return original(provider, stage, context)
        with patch.object(self.agents, 'ask', side_effect=ask):
            state = self.tick()
        self.assertNotIn('timing', state['operation'])
        self.assertNotIn('timing', state['reports'][-1])
        self.assertFalse(next(iter(state['design_votes'].values()))['first_pass'])
