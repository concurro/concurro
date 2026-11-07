"""Independent assessments survive corrections but never inherit negotiated approval."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule
import test_consensus
from test_model_profiles import named_config
from test_pipeline import Controller
from policy import SessionFailure, approval_valid, digest
import design_eligibility as eligibility
import feedback


class ReceiptTests(unittest.TestCase):
    """Exercise receipt transitions independently of model-generated verdicts."""

    def setUp(self):
        """Create one clean bound assessment and an unsubmitted operation."""
        self.state = {}
        self.context = {'review_phase': 'cooperative', 'design_binding': 'design-1',
                        'reviewer_identity': {'id': 'reviewer-1'}, 'plan': {'responses': []},
                        'issue': {'body': 'Requirement'}, 'own_objections': []}
        self.session = {'role': 'plan_qc:one', 'scope': 'session-1', 'turn': 0, 'restarts': 0}
        self.operation = {'status': 'new'}

    def admit(self):
        """Apply the dispatch-time transition and read the resulting authority."""
        eligibility.prepare(self.state, self.context, self.session, self.operation)
        return eligibility.eligible(self.state, self.context, self.session)

    def test_validation_feedback_is_the_only_substantive_input_excluded(self):
        """Corrections preserve receipts; new writer inputs cannot silently change the baseline."""
        self.assertTrue(self.admit())
        original = deepcopy(self.state)
        self.session['turn'] = 2
        self.context['review_validation_feedback'] = {'detail': 'x' * 10000}
        self.context = feedback.prepare(self.context)
        self.assertTrue(self.admit())
        self.assertEqual(original, self.state)
        self.context['writer_feedback'] = 'Please change behavior'
        self.assertFalse(self.admit())
        self.context.pop('writer_feedback')
        self.assertFalse(self.admit())

    def test_negotiation_disqualifies_even_a_new_session(self):
        """Resolved objections, planner replies and peer input still affect independence."""
        for field, value in (('own_objections', [{'status': 'resolved'}]),
                             ('plan', {'responses': [{'response': 'Rebuttal'}]}),
                             ('peer_judgments', ['approve']), ('writer_feedback', 'More work')):
            with self.subTest(field=field):
                self.setUp()
                self.context[field] = value
                self.assertFalse(self.admit())

    def test_binding_change_requires_clean_provenance_not_just_new_identity(self):
        """Ordinary revisions retain transcripts; requirement/reopen rotations may be clean."""
        self.assertTrue(self.admit())
        self.session['turn'] = 1
        self.context['design_binding'] = 'revision'
        self.assertFalse(self.admit())
        self.context['design_binding'] = 'reopened'
        self.session.update(scope='reopen-session', turn=0)
        self.assertTrue(self.admit())
        self.context['own_objections'] = [{'status': 'resolved'}]
        self.assertFalse(self.admit())

    def test_binding_change_during_uncertain_first_turn_does_not_reuse_clean_status(self):
        """An admitted turn can have a transcript even before the controller records completion."""
        self.assertTrue(self.admit())
        self.context['design_binding'] = 'revision-before-result'
        self.assertFalse(self.admit())

    def test_missing_legacy_and_replayed_operation_never_initialize_authority(self):
        """Turn zero on a started/completed call does not prove it was newly admitted here."""
        for status in ('started', 'completed', 'uncertain'):
            with self.subTest(status=status):
                self.setUp()
                self.operation['status'] = status
                self.assertFalse(self.admit())
        self.setUp()
        self.session['turn'] = 1
        self.assertFalse(self.admit())

    def test_restart_is_sticky_and_receipts_are_bound_to_reviewer_and_session(self):
        """Restart cannot restore eligibility; another reviewer's receipt is not authority."""
        self.assertTrue(self.admit())
        eligibility.restart(self.state, self.session)
        self.session.update(scope='replacement', turn=0, restarts=1)
        self.assertFalse(self.admit())
        self.assertEqual(self.state['design_eligibility']['reviewer-1']['reason'], 'session_restart')
        self.session['restarts'] = 0
        self.assertFalse(self.admit())
        self.context['reviewer_identity']['id'] = 'another'
        self.assertFalse(eligibility.eligible(self.state, self.context, self.session))


class LifecycleTests(unittest.TestCase):
    """Reconstruct controllers and real checkpoints across corrections and recovery."""

    setUp = test_consensus.ConsensusTests.setUp
    tick = test_consensus.ConsensusTests.tick
    reach = test_consensus.ConsensusTests.reach
    persist = test_consensus.ConsensusTests.persist

    def load(self):
        """Read persisted state without dispatching another turn."""
        active = Controller(self.gh, self.agents, self.config)
        active.issue = self.gh.issue
        active.load(self.gh.comments)
        return active

    def invalid(self, original):
        """Build a correction requiring evidence, not a forced approve decision."""
        def ask(provider, stage, context):
            """Remove assessments so the production validator requests correction."""
            report = original(provider, stage, context)
            if stage == 'plan_qc':
                report['assessments'] = []
            return report
        return ask

    def test_exhaustion_retry_keeps_receipt_outside_operation_and_budget(self):
        """Writer retry renews only the correction allowance, including after checkpoint reload."""
        self.reach('plan_qc')
        with patch.object(self.agents, 'ask', side_effect=self.invalid(self.agents.ask)):
            state = self.reach('escalated')
        receipts, spent = deepcopy(state['design_eligibility']), state['calls']
        self.assertTrue(next(iter(receipts.values()))['eligible'])
        self.gh.comment('/concurro retry')
        active = self.load()
        active.commands(self.gh.comments)
        reloaded = self.load().state
        self.assertNotIn('operation', reloaded)
        self.assertEqual(reloaded['design_eligibility'], receipts)
        self.assertEqual(reloaded['calls'], spent)
        result = self.reach('implement')
        self.assertEqual(result['design_phase'], 'cooperative')
        self.assertEqual(result['calls'], spent + 2)
        for _, stage, context in self.agents.calls:
            if stage == 'plan_qc' and 'review_validation_feedback' in context:
                message = context['review_validation_feedback']
                self.assertIn('approval is never required', message['instruction'])
                self.assertTrue(message['detail'])

    def test_receipt_is_saved_before_dispatch_and_false_survives_writer_retry(self):
        """The runner cannot start before provenance is durable; retries cannot reset a refusal."""
        self.reach('plan_qc')
        original = self.agents.ask

        def ask(provider, stage, context):
            """Inspect the actual checkpoint from inside the fake provider dispatch."""
            saved = self.load().state
            receipt = saved['design_eligibility'][context['reviewer_identity']['id']]
            self.assertEqual(saved['operation']['status'], 'started')
            self.assertTrue(receipt['eligible'])
            return self.invalid(original)(provider, stage, context)

        with patch.object(self.agents, 'ask', side_effect=ask):
            self.tick()
        active = self.load()
        receipt = next(iter(active.state['design_eligibility'].values()))
        receipt.update(eligible=False, reason='context_changed')
        active.state.update(stage='escalated', retry_stage='plan_qc')
        active.save()
        self.gh.comment('/concurro retry')
        result = self.reach('implement')
        self.assertEqual(result['design_phase'], 'fresh')
        self.assertIn('context_changed', [r['reason'] for r in result['design_eligibility'].values()])

    def test_completed_result_replays_without_reinitializing_receipt(self):
        """Crash after durable completion but before validation does not spend another call."""
        self.reach('plan_qc')
        with patch.object(Controller, 'validate_design_report', side_effect=RuntimeError('crash')):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                self.tick()
        before = self.load().state
        self.assertEqual(before['operation']['status'], 'completed')
        result = self.tick()
        self.assertEqual(result['calls'], before['calls'])
        self.assertEqual(result['design_eligibility'], before['design_eligibility'])
        self.assertTrue(next(iter(result['design_votes'].values()))['first_pass'])
        self.assertTrue(approval_valid(self.reach('implement'), self.config))

    def test_missing_receipt_on_retained_result_requires_fresh_review(self):
        """A deployment cannot retroactively label an existing response independent."""
        self.reach('plan_qc')
        with patch.object(Controller, 'validate_design_report', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.tick()
        active = self.load()
        active.state.pop('design_eligibility')
        active.save()
        result = self.reach('implement')
        self.assertEqual(result['design_phase'], 'fresh')
        self.assertTrue(approval_valid(result, self.config))

    def test_failed_session_requires_fresh_review(self):
        """A replacement session cannot erase the uncertain continuity of its predecessor."""
        self.reach('plan_qc')
        original = self.agents.ask
        failures = []

        def ask(provider, stage, context):
            """Fail one provider session, then allow the normal production recovery."""
            if not failures:
                failures.append(True)
                raise SessionFailure('fixture failure')
            return original(provider, stage, context)

        with patch.object(self.agents, 'ask', side_effect=ask):
            result = self.reach('implement')
        self.assertEqual(result['design_phase'], 'fresh')
        self.assertIn('session_restart', [r['reason'] for r in result['design_eligibility'].values()])

    def test_requirement_and_reopen_rotations_can_start_clean_assessments(self):
        """Clean new scopes retain today's shortcut; old objections still disqualify them."""
        for change in ('requirements', 'reopen'):
            with self.subTest(change=change):
                self.setUp()
                self.reach('implement')
                active = self.load()
                active.invalidate_design('Fixture new design')
                if change == 'requirements':
                    self.gh.issue['body'] += ' Additional requirement.'
                    active.state['requirements'] = active.requirements(self.gh.comments)
                    active.state['issue_digest'] = digest([self.gh.issue['title'], self.gh.issue['body']])
                else:
                    active.state['design_reopens'] += 1
                active.state['stage'] = 'plan_qc'
                active.save()
                result = self.reach('implement')
                self.assertEqual(result['design_phase'], 'cooperative')
                self.assertTrue(approval_valid(result, self.config))

    def test_named_profiles_have_separate_receipts_after_corrections(self):
        """Two models on the same harness keep independent identities and correction histories."""
        self.config = named_config()
        self.gh.config = self.config
        self.reach('plan_qc')
        original = self.agents.ask
        corrected = set()

        def ask(provider, stage, context):
            """Require one correction independently for each named reviewer."""
            report = original(provider, stage, context)
            if stage == 'plan_qc' and provider not in corrected:
                corrected.add(provider)
                report['assessments'] = []
            return report

        with patch.object(self.agents, 'ask', side_effect=ask):
            result = self.reach('implement')
        self.assertEqual(corrected, {'sol-high', 'astra-high'})
        self.assertEqual(len(result['design_eligibility']), 2)
        self.assertTrue(all(r['eligible'] for r in result['design_eligibility'].values()))
        self.assertEqual(result['design_phase'], 'cooperative')
        self.assertTrue(approval_valid(result, self.config))

    def test_explicit_invalidation_drops_old_receipts(self):
        """A writer-authorized reset starts a new run rather than restoring old eligibility."""
        self.reach('implement')
        active = self.load()
        self.assertTrue(active.state['design_eligibility'])
        self.gh.comment('/concurro invalidate')
        active.commands(self.gh.comments)
        self.assertNotIn('design_eligibility', self.load().state)
        self.assertEqual(self.reach('implement')['design_phase'], 'cooperative')
