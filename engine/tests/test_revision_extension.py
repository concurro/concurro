"""Writer-granted revision capacity preserves substantive findings and all spent budgets."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
from policy import Blocked, approval_valid
from pipeline_fixtures import ConsensusFixture
from fakes import decision


class RevisionExtensionTests(ConsensusFixture, unittest.TestCase):
    """Exercise real checkpoint saves, command replay and planner/reviewer transitions."""

    def exhausted(self, legacy=False):
        """Spend the configured allowance before a genuine reviewer requests another revision."""
        state = self.reach('plan_qc')
        state['plan_revisions'] = self.config['max_plan_revisions']
        self.persist(state)
        self.agents.qc_reports = [decision('approve'), decision('revise', concerns=['Rollback must discard aborted writes.'])]
        state = self.reach('escalated')
        self.assertTrue(state['objections'])
        self.assertFalse(state['objections'][0]['resolved'])
        self.assertEqual(state['retry_stage'], 'plan')
        if legacy:
            state.pop('revision_wait')
            state['retry_stage'] = 'intake'
            self.persist(state)
        return deepcopy(state)

    def command(self, text='/concurro retry --plan-revisions 2', login='writer'):
        """Apply an exact command without executing the following model stage."""
        self.gh.comment(text, login=login)
        controller = self.loaded()
        controller.commands(self.gh.comments)
        return controller.state

    def test_extension_resumes_planner_preserves_findings_and_requires_new_consensus(self):
        prior = self.exhausted()
        state = self.command()
        for key in ('calls', 'requirements', 'plan', 'objections', 'design_feedback', 'sessions', 'design_history', 'design_generation'):
            self.assertEqual(state[key], prior[key], key)
        self.assertEqual(state['stage'], 'plan')
        self.assertEqual(state['plan_revisions'], prior['plan_revisions'] + 1, 'reserve the pending revision, never reset spent work')
        self.assertEqual(state['plan_revision_extra'], 2)
        self.assertEqual(state['config_digest'], prior['config_digest'])
        self.assertFalse(approval_valid(state, self.config))
        self.assertFalse(state['design_votes'])
        self.assertNotIn('revision_wait', state)
        self.assertNotIn('operation', state)
        receipt = state['plan_revision_grant']
        self.assertEqual(receipt['actor'], 'writer')
        self.assertEqual(receipt['comment'], state['command_id'])
        self.assertEqual(receipt['limit'], self.config['max_plan_revisions'] + 2)
        self.assertTrue(any('Plan revision capacity extended' in c['body'] for c in self.gh.comments))
        before_calls = len(self.agents.calls)
        state = self.tick()
        self.assertEqual(self.agents.calls[before_calls][1], 'plan', 'do not repeat intake')
        self.assertEqual(self.agents.calls[before_calls][2]['open_objections'], prior['objections'])
        self.assertEqual(state['stage'], 'plan_qc')
        self.assertNotIn('approval', state)
        state = self.reach('implement')
        self.assertTrue(approval_valid(state, self.config))
        self.assertTrue(all(o['resolved'] for o in state['objections']))
        self.assertEqual(state['design_phase'], 'fresh')
        self.assertGreater(state['calls'], prior['calls'])

    def test_legacy_stall_resumes_the_recorded_design_revision(self):
        prior = self.exhausted(legacy=True)
        state = self.command()
        self.assertEqual(state['stage'], 'plan')
        self.assertEqual(state['objections'], prior['objections'])
        self.assertEqual(state['design_history'], prior['design_history'])
        self.assertEqual(state['calls'], prior['calls'])

    def test_refused_plain_retry_is_consumed_without_repeated_invalidation(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.setUp()
                prior = self.exhausted(legacy)
                state = self.command('/concurro retry')
                consumed = state['command_id']
                self.assertGreater(consumed, prior['command_id'])
                self.assertEqual(len(self.notices()), 1)
                self.assertIn('/concurro retry --plan-revisions 1', self.notices()[0]['body'])
                for _ in range(3):
                    state = self.tick()
                    for key in ('calls', 'plan_revisions', 'design_generation', 'design_history', 'objections'):
                        self.assertEqual(state[key], prior[key], key)
                    self.assertEqual(state['command_id'], consumed)
                    self.assertEqual(len(self.notices()), 1)
                self.command('/concurro retry')
                self.assertEqual(len(self.notices()), 2, 'one response per new command')
                self.assertEqual(self.command()['stage'], 'plan', 'a later explicit grant must not be wedged')

    def test_exhausted_terminal_judgment_retry_is_also_consumed(self):
        state = self.reach('implement')
        state.update(stage='escalated', retry_stage='intake', reason='Human decision required.',
                     plan_revisions=self.config['max_plan_revisions'])
        self.persist(state)
        prior = deepcopy(state)
        state = self.command('/concurro retry')
        self.assertGreater(state['command_id'], prior['command_id'])
        self.assertEqual(state['revision_wait'], {'stage': 'intake'})
        self.assertEqual(state['design_generation'], prior['design_generation'] + 1)
        self.assertEqual(self.tick()['design_generation'], state['design_generation'])
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.command()['stage'], 'intake')

    def notices(self):
        """Only command-refusal comments, excluding the original escalation and dashboard."""
        return [c for c in self.gh.comments if 'Plan revision command not applied' in c['body']]

    def test_non_writer_cannot_grant_or_trigger_a_notice(self):
        prior = self.exhausted()
        state = self.command(login='outsider')
        self.assertEqual(state.get('plan_revision_extra', 0), 0)
        self.assertEqual(state['command_id'], prior['command_id'])
        self.assertEqual(self.notices(), [])

    def test_inapplicable_grants_preserve_healthy_tasks_and_post_once(self):
        for stage in ('plan', 'implement', 'review'):
            for amount in (0, 1, 4):
                with self.subTest(stage=stage, amount=amount):
                    self.setUp()
                    prior = self.reach(stage)
                    calls = len(self.agents.calls)
                    state = self.command(f'/concurro retry --plan-revisions {amount}')
                    for key in ('stage', 'calls', 'plan_revisions', 'approval', 'findings', 'objections', 'operation', 'reason'):
                        self.assertEqual(state.get(key), prior.get(key), key)
                    self.assertGreater(state['command_id'], prior['command_id'])
                    self.assertNotIn('plan_revision_extra', state)
                    self.assertNotIn('revision_wait', state)
                    self.assertEqual(len(self.agents.calls), calls)
                    self.assertEqual(len(self.notices()), 1)
                    self.assertIn('No exhausted plan revision is waiting', self.notices()[0]['body'])
                    for _ in range(3):
                        loaded = self.loaded()
                        self.assertFalse(loaded.commands(self.gh.comments))
                        self.assertEqual(loaded.state['stage'], stage)
                    self.assertEqual(len(self.notices()), 1)
                    self.assertNotEqual(self.tick()['stage'], 'blocked')
                    self.assertEqual(len(self.agents.calls), calls + 1, 'normal work continues')

    def test_ignored_grant_does_not_skip_a_later_writer_command(self):
        self.reach('plan')
        self.gh.comment('/concurro retry --plan-revisions 1')
        self.gh.comment('/concurro stop')
        controller = self.loaded()
        controller.commands(self.gh.comments)
        self.assertEqual(controller.state['stage'], 'stopped')
        self.assertEqual(len(self.notices()), 1)
        self.assertNotIn('plan_revision_extra', controller.state)

    def test_start_at_revision_limit_posts_one_notice_without_reinvalidating(self):
        for autonomous in (False, True):
            with self.subTest(autonomous=autonomous):
                self.setUp()
                self.config['autonomous'] = autonomous
                if not autonomous:
                    self.gh.comment('/concurro start')
                prior = self.exhausted()
                state = self.command('/concurro start')
                self.assertGreater(state['command_id'], prior['command_id'])
                self.assertEqual(len(self.notices()), 1)
                self.assertIn('/concurro retry --plan-revisions 1', self.notices()[0]['body'])
                for _ in range(3):
                    state = self.tick()
                    for key in ('stage', 'calls', 'plan_revisions', 'design_history', 'design_generation', 'objections'):
                        self.assertEqual(state[key], prior[key], key)
                self.assertEqual(len(self.notices()), 1)

    def test_first_exhausted_manual_start_consumes_and_explains_command(self):
        self.config['autonomous'] = False
        self.gh.comment('/concurro start')
        state = self.reach('plan')
        state.update(stage='blocked', retry_stage='plan', plan_revisions=self.config['max_plan_revisions'])
        self.persist(state)
        state = self.command('/concurro start')
        self.assertEqual(state['stage'], 'escalated')
        self.assertEqual(state['revision_wait'], {'stage': 'intake'})
        self.assertEqual(len(self.notices()), 1)
        self.tick()
        self.assertEqual(len(self.notices()), 1)

    def test_refusal_notice_survives_lost_save_reply_without_duplicate(self):
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                self.setUp()
                prior = self.exhausted() if exhausted else self.reach('implement')
                self.gh.comment('/concurro retry' if exhausted else '/concurro retry --plan-revisions 1')
                controller = self.loaded()
                original = controller.save
                def save_then_crash(*args, **kwargs):
                    """Lose acknowledgement after the command and notice were saved."""
                    original(*args, **kwargs)
                    raise RuntimeError('crash after notice save')
                with patch.object(controller, 'save', side_effect=save_then_crash):
                    with self.assertRaisesRegex(RuntimeError, 'crash after notice save'):
                        controller.commands(self.gh.comments)
                restored = self.loaded()
                restored.commands(self.gh.comments)
                self.assertEqual(restored.state['stage'], prior['stage'])
                self.assertEqual(restored.state['calls'], prior['calls'])
                self.assertEqual(len(self.notices()), 1)
                self.assertNotIn('plan_revision_extra', restored.state)

    def test_invalid_amount_or_changed_authority_refuses_without_grant(self):
        for failure in ('zero', 'large', 'config', 'requirements', 'bundle', 'pending', 'head'):
            with self.subTest(failure=failure):
                self.setUp()
                prior = self.exhausted()
                controller = self.loaded()
                if failure == 'config':
                    self.config['max_model_calls'] -= 1
                elif failure == 'requirements':
                    self.gh.issue['body'] += ' Changed requirements.'
                elif failure == 'bundle':
                    controller.state['bundle_id'] = '0' * 64
                elif failure == 'pending':
                    controller.state['pending'] = {'test': True}
                elif failure == 'head':
                    controller.state.update(pr=1, head='a' * 40)
                    controller.pulls.repo = lambda path: {'head': {'sha': 'b' * 40}, 'state': 'open'}
                amount = 0 if failure == 'zero' else 4 if failure == 'large' else 2
                self.gh.comment(f'/concurro retry --plan-revisions {amount}')
                # Assert command checkpointing even where the deliberately invalid fixture cannot be rendered.
                with patch.object(controller, 'save') as saved:
                    with self.assertRaises(Blocked):
                        controller.commands(self.gh.comments)
                saved.assert_called_once()
                self.assertGreater(controller.state['command_id'], prior['command_id'])
                self.assertNotIn('plan_revision_extra', controller.state)
                self.assertEqual(controller.state['calls'], prior['calls'])
                self.assertEqual(controller.state['objections'], prior['objections'])

    def test_saved_grant_survives_lost_save_reply_and_command_replay_once(self):
        prior = self.exhausted()
        self.gh.comment('/concurro retry --plan-revisions 2')
        controller = self.loaded()
        original = controller.save
        def save_then_crash(*args, **kwargs):
            """Lose the acknowledgement after the real durable save."""
            original(*args, **kwargs)
            raise RuntimeError('crash after save')
        with patch.object(controller, 'save', side_effect=save_then_crash):
            with self.assertRaisesRegex(RuntimeError, 'crash after save'):
                controller.commands(self.gh.comments)
        restored = self.loaded()
        restored.commands(self.gh.comments)
        self.assertEqual(restored.state['plan_revision_extra'], 2)
        self.assertEqual(restored.state['plan_revisions'], prior['plan_revisions'] + 1)
        self.assertEqual(restored.state['calls'], prior['calls'])
        self.assertEqual(sum('Plan revision capacity extended' in c['body'] for c in self.gh.comments), 1)

    def test_grant_does_not_replenish_model_calls(self):
        self.exhausted()
        controller = self.loaded()
        controller.state['calls'] = self.config['max_model_calls']
        controller.save()
        self.command()
        with self.assertRaisesRegex(Blocked, 'model-call budget exhausted'):
            self.tick()
        self.assertEqual(self.loaded().state['calls'], self.config['max_model_calls'])

    def test_refused_legacy_extension_keeps_resume_target_after_block_notice(self):
        prior = self.exhausted(legacy=True)
        with self.assertRaises(Blocked) as refusal:
            self.command('/concurro retry --plan-revisions 0')
        controller = self.loaded()
        controller.suspend('blocked', str(refusal.exception), outboxes=False)
        state = self.command()
        self.assertEqual(state['stage'], 'plan')
        self.assertEqual(state['design_history'], prior['design_history'])
        self.assertEqual(state['objections'], prior['objections'])
        self.assertEqual(state['calls'], prior['calls'])

    def test_explicit_invalidation_preserves_spent_and_granted_capacity(self):
        self.exhausted()
        granted = self.command()
        state = self.command('/concurro invalidate')
        self.assertEqual(state['calls'], granted['calls'])
        self.assertEqual(state['plan_revisions'], granted['plan_revisions'])
        self.assertEqual(state['plan_revision_extra'], granted['plan_revision_extra'])
        self.assertNotIn('revision_wait', state)

    def test_dashboard_and_escalation_show_extension_instead_of_plain_retry(self):
        self.exhausted()
        self.assertIn('/concurro retry --plan-revisions 1', self.loaded().action_line())
        self.assertTrue(any('plain retry cannot increase' in c['body'] for c in self.gh.comments))
