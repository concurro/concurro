"""Bundle upgrades preserve observers and permit only proven implementation-only retries."""

from copy import deepcopy
from pathlib import Path
import tempfile
import json
import subprocess
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule
from pipeline_fixtures import copy_roots
from fakes import FakeAgents, FakeGitHub
from controller import BUNDLE_REASON, LEGACY_BUNDLE_REASON, Controller
from policy import Blocked, approval_valid
from github import Transient
from conversation import PublicationError
import sessions
import implementation_bundle


class BundleLifecycleTests(unittest.TestCase):
    """Drive real checkpoints and commands across an engine instruction change."""

    def setUp(self):
        """Use isolated bundles and deterministic transports, with no external calls."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        copy_roots(self.root)

    def controller(self):
        """Reconstruct the controller against whichever bundle is currently installed."""
        return Controller(self.gh, self.agents, self.config, self.root, self.root)

    def loaded(self):
        """Load without advancing, using this fixture's custom bundle roots."""
        controller = self.controller()
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def tick(self):
        """Advance once using this fixture's custom bundle roots."""
        controller = self.controller()
        controller.advance(7)
        return controller

    def reach(self, stage):
        """Advance the fixture to a named stage, supplying manual approval when needed."""
        for _ in range(35):
            controller = self.tick()
            if controller.state['stage'] == stage:
                return controller
            if controller.state['stage'] == 'planned':
                self.gh.comment('/concurro approve ' + controller.state['plan_id'])
        self.fail('did not reach ' + stage)

    def upgrade(self):
        """Change a trusted instruction without modifying the recorded checkpoint."""
        path = self.root / 'bundle/instructions.md'
        path.write_text(path.read_text() + '\nUpdated operator guidance.\n')

    def test_closed_issue_stops_without_blocking(self):
        self.reach('implement')
        self.upgrade()
        calls = len(self.agents.calls)
        self.gh.issue['state'] = 'closed'
        controller = self.tick()
        self.assertEqual(controller.state['stage'], 'stopped')
        self.assertEqual(len(self.agents.calls), calls)
        self.assertFalse(any('### 🛑 Blocked' in c['body'] for c in self.gh.comments))
        self.assertEqual(self.tick().state['stage'], 'stopped')

    def test_merged_and_closed_prs_complete_after_upgrade(self):
        for merged in (True, False):
            with self.subTest(merged=merged):
                self.setUp()
                self.reach('gates')
                self.upgrade()
                calls = len(self.agents.calls)
                self.gh.pr.update(state='closed', merged=merged, merge_commit_sha='f' * 40)
                stage = 'observed_merged' if merged else 'closed'
                controller = self.tick()
                self.assertEqual(controller.state['stage'], stage)
                self.assertEqual(self.tick().state['stage'], stage)
                self.assertEqual(len(self.agents.calls), calls)
                self.assertFalse(any('### 🛑 Blocked' in c['body'] for c in self.gh.comments))

    def test_changed_merge_head_is_still_refused(self):
        self.reach('gates')
        self.upgrade()
        self.gh.pr.update(merged=True, merge_commit_sha='f' * 40)
        self.gh.pr['head']['sha'] = 'b' * 40
        with self.assertRaisesRegex(Blocked, 'published controller head'):
            self.tick()

    def test_ready_observation_keeps_old_approval_and_checks_ci(self):
        self.reach('gates')
        prior = deepcopy(self.tick().state)
        self.upgrade()
        calls, comments = len(self.agents.calls), len(self.gh.comments)
        state = self.tick().state
        self.assertEqual(state['stage'], 'gates')
        self.assertTrue(state['handoff_ready'])
        self.assertEqual(state['bundle_id'], prior['bundle_id'])
        self.assertEqual(state['approval'], prior['approval'])
        self.assertEqual(len(self.gh.comments), comments)
        self.gh.checks_green = False
        self.assertFalse(self.tick().state['handoff_ready'])
        self.gh.checks_green = True
        self.assertTrue(self.tick().state['handoff_ready'])
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(self.gh.ready_calls, 1)

    def test_old_bundle_blocked_watcher_recovers_but_other_blocks_do_not(self):
        self.reach('gates')
        self.tick()
        self.upgrade()
        for reason, expected in ((LEGACY_BUNDLE_REASON, 'gates'), ('A different failure.', 'blocked')):
            controller = self.loaded()
            controller.state.update(stage='blocked', retry_stage='gates', reason=reason)
            controller.save()
            comments, calls = len(self.gh.comments), len(self.agents.calls)
            self.assertEqual(self.tick().state['stage'], expected)
            self.assertEqual(len(self.gh.comments), comments)
            self.assertEqual(len(self.agents.calls), calls)

    def test_each_model_stage_still_blocks_before_new_work(self):
        for stage in ('intake', 'plan', 'plan_qc', 'implement', 'review'):
            with self.subTest(stage=stage):
                self.setUp()
                controller = self.reach('plan' if stage == 'intake' else stage)
                if stage == 'intake':
                    controller.state['stage'] = stage
                    controller.save()
                self.upgrade()
                calls = len(self.agents.calls)
                with self.assertRaisesRegex(Blocked, '/concurro invalidate'):
                    self.tick()
                self.assertEqual(len(self.agents.calls), calls)

    def test_retained_model_result_cannot_bypass_bundle_guard(self):
        self.reach('plan')
        self.upgrade()
        controller = self.loaded()
        with patch.object(controller, 'recover', return_value={'old': 'response'}) as recover:
            with self.assertRaisesRegex(Blocked, '/concurro invalidate'):
                controller.ask(self.config['planner'], 'plan', {})
        recover.assert_not_called()

    def test_retry_does_not_adopt_bundle_or_discard_approval(self):
        controller = self.reach('implement')
        prior = deepcopy(controller.state)
        self.upgrade()
        controller.suspend('blocked', BUNDLE_REASON, outboxes=False)
        self.assertIn('/concurro invalidate', controller.action_line())
        self.assertNotIn('a fix and a retry', controller.action_line())
        self.assertTrue(any('Otherwise restore the previous bundle' in c['body'] for c in self.gh.comments))
        self.gh.comment('/concurro retry')
        calls = len(self.agents.calls)
        with self.assertRaisesRegex(Blocked, '/concurro invalidate'):
            self.tick()
        state = self.loaded().state
        self.assertEqual(state['bundle_id'], prior['bundle_id'])
        self.assertEqual(state['approval'], prior['approval'])
        self.assertEqual(state['sessions'], prior['sessions'])
        self.assertEqual(len(self.agents.calls), calls)
        self.assertNotIn('invalidations', state)

    def test_invalidate_replans_existing_pr_with_fresh_authorization(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                self.setUp()
                self.config['autonomous'] = not manual
                if manual:
                    self.gh.comment('/concurro start')
                prior = deepcopy(self.reach('gates').state)
                self.upgrade()
                self.gh.comment('/concurro invalidate')
                command = self.gh.comments[-1]['id']
                calls = len(self.agents.calls)
                controller = self.tick()
                state = controller.state
                self.assertEqual(state['stage'], 'intake')
                self.assertEqual(len(self.agents.calls), calls)
                for key in ('base', 'pr', 'head', 'branch', 'calls', 'round', 'plan_revisions', 'design_reopens'):
                    self.assertEqual(state[key], prior[key], key)
                for key in ('approval', 'plan', 'plan_id', 'operation', 'candidate'):
                    self.assertNotIn(key, state)
                for key in ('sessions', 'design_votes', 'reviews', 'qc'):
                    self.assertEqual(state[key], {})
                self.assertEqual(state['bundle_id'], controller.bundle.identity)
                self.assertFalse(approval_valid(state, self.config))
                audit = state['invalidations'][0]
                self.assertEqual(audit['actor'], 'writer')
                self.assertEqual(audit['comment'], command)
                self.assertEqual(audit['old_bundle'], prior['bundle_id'])
                self.assertEqual(audit['new_bundle'], controller.bundle.identity)
                self.assertEqual(self.tick().state['stage'], 'plan')
                replanned = self.tick().state
                self.assertEqual(replanned['stage'], 'plan_qc')
                self.assertEqual(replanned['approved_accumulated_files'], prior['changed_files'])
                self.assertEqual(replanned['design_parent'], prior['head'])
                self.assertEqual(len(replanned['invalidations']), 1)
                next_stage = 'planned' if manual else 'implement'
                assessed = self.reach(next_stage).state
                self.assertEqual(assessed['head'], prior['head'])
                if manual:
                    self.assertNotIn('approval', assessed)
                else:
                    self.assertTrue(approval_valid(assessed, self.config))
                    self.assertNotEqual(assessed['approval']['binding'], prior['approval']['binding'])

    def test_invalidate_requires_writer_and_rotates_same_bundle_sessions(self):
        prior = deepcopy(self.reach('plan_qc').state)
        self.gh.comment('/concurro invalidate', login='reader')
        state = self.tick().state
        self.assertNotIn('invalidations', state)
        self.gh.comment('/concurro invalidate')
        state = self.tick().state
        old = sessions.prepare(deepcopy(prior), self.config, self.config['planner'], 'plan', {})
        new = sessions.prepare(deepcopy(state), self.config, self.config['planner'], 'plan', {})
        self.assertNotEqual(old['scope'], new['scope'])
        self.assertEqual(state['bundle_id'], prior['bundle_id'])
        self.assertEqual(new['turn'], 0)

    def test_pending_publication_is_finished_before_invalidation(self):
        prior = deepcopy(self.reach('publish').state)
        self.upgrade()
        self.gh.comment('/concurro invalidate')
        failed = self.controller()
        with self.assertRaisesRegex(Blocked, 'Publication is pending') as error:
            failed.advance(7)
        failed.suspend('blocked', str(error.exception), outboxes=False)
        self.assertEqual(failed.state['pending'], prior['pending'])
        self.assertNotIn('invalidations', failed.state)
        calls = len(self.agents.calls)
        self.gh.comment('/concurro retry')
        published = self.tick().state
        self.assertEqual(published['stage'], 'review')
        self.assertEqual(published['head'], prior['pending']['head'])
        self.assertEqual(published['bundle_id'], prior['bundle_id'])
        self.assertEqual(len(self.agents.calls), calls)
        self.gh.comment('/concurro invalidate')
        state = self.tick().state
        self.assertEqual(state['stage'], 'intake')
        self.assertEqual(state['pr'], published['pr'])

    def test_invalidation_does_not_adopt_config_drift_or_foreign_heads(self):
        self.reach('gates')
        self.gh.comment('/concurro invalidate')
        self.config['max_model_calls'] += 1
        with self.assertRaisesRegex(Blocked, 'configuration changed'):
            self.tick()
        self.config['max_model_calls'] -= 1
        self.gh.pr['head']['sha'] = 'b' * 40
        with self.assertRaisesRegex(Blocked, 'outside the controller'):
            self.tick()
        self.assertNotIn('invalidations', self.loaded().state)

    def test_inconsistent_recorded_charter_is_not_adopted(self):
        controller = self.reach('plan')
        controller.state['charter']['id'] = 'tampered'
        controller.save()
        self.gh.comment('/concurro invalidate')
        with self.assertRaisesRegex(Blocked, 'inconsistent'):
            self.tick()

    def test_invalidation_save_recovery_consumes_command_once(self):
        self.reach('implement')
        self.upgrade()
        self.gh.comment('/concurro invalidate')
        failed = self.controller()
        save = failed.save

        def lost_response(*args, **kwargs):
            """Lose the reply after the new generation has reached its checkpoint."""
            save(*args, **kwargs)
            raise RuntimeError('lost checkpoint response')

        with patch.object(failed, 'save', side_effect=lost_response):
            with self.assertRaisesRegex(RuntimeError, 'lost checkpoint response'):
                failed.advance(7)
        saved = deepcopy(self.loaded().state)
        state = self.tick().state
        self.assertEqual(state['stage'], 'plan')
        self.assertEqual(state['invalidations'], saved['invalidations'])
        self.assertEqual(state['session_epoch'], saved['session_epoch'])
        self.assertEqual(state['design_generation'], saved['design_generation'])
        self.assertEqual(state['calls'], saved['calls'] + 1)

    def test_charter_upgrade_requires_explicit_adoption(self):
        prior = deepcopy(self.reach('implement').state)
        path = self.root / '.concurro/charter.json'
        charter = json.loads(path.read_text())
        charter['id'] += '-new'
        path.write_text(json.dumps(charter))
        with self.assertRaisesRegex(Blocked, '/concurro invalidate'):
            self.tick()
        self.gh.comment('/concurro invalidate')
        state = self.tick().state
        self.assertEqual(state['charter'], charter)
        self.assertEqual(state['invalidations'][0]['old_charter'], prior['charter_id'])
        self.assertEqual(state['invalidations'][0]['new_charter'], state['charter_id'])
        self.assertNotEqual(state['charter_id'], prior['charter_id'])
        self.assertEqual(self.tick().state['stage'], 'plan')

    def test_invalidation_withdraws_findings_without_erasing_them(self):
        self.agents.fix = True
        self.reach('review')
        reviewed = self.tick()
        prior = deepcopy(reviewed.state['findings'])
        self.assertTrue(prior)
        self.assertEqual(prior[0]['status'], 'open')
        self.upgrade()
        self.gh.comment('/concurro invalidate')
        state = self.tick().state
        self.assertEqual([f['id'] for f in state['findings']], [f['id'] for f in prior])
        self.assertEqual(state['findings'][0]['status'], 'withdrawn')
        self.assertTrue(state['review_history'])
        self.assertFalse(state['pr_outbox'])

    def test_invalidation_supersedes_an_open_input_request(self):
        controller = self.reach('plan')
        controller.state['qc']['intake'].update(decision='clarify', questions=['Which behavior?'])
        controller.state['stage'] = 'clarify'
        controller.save()
        self.assertIn('answer the questions', controller.action_line())
        self.gh.comment('/concurro invalidate')
        controller = self.tick()
        request = controller.state['requests'][0]
        self.assertEqual(request['state'], 'superseded')
        self.assertEqual(request['edit']['state'], 'done')
        self.assertNotIn('answer the questions', controller.action_line())
        self.assertTrue(any('a writer requested a new assessment' in c['body'] for c in self.gh.comments))

    def test_refused_invalidation_does_not_replay_or_block_later_retry(self):
        for cause in ('config', 'head', 'files', 'rename'):
            with self.subTest(cause=cause):
                self.setUp()
                prior = deepcopy(self.reach('gates').state)
                original_pages = self.gh.pages

                def pages(path, key=None):
                    """Return the problematic PR file evidence only during refusal."""
                    if path == 'pulls/9/files':
                        return [] if cause == 'files' else [{'filename': 'docs/new.md', 'status': 'renamed'}]
                    return original_pages(path, key)

                if cause == 'config':
                    self.config['max_model_calls'] += 1
                elif cause == 'head':
                    self.gh.pr['head']['sha'] = 'b' * 40
                else:
                    self.gh.pages = pages
                self.gh.comment('/concurro invalidate')
                command = self.gh.comments[-1]['id']
                failed = self.controller()
                with self.assertRaises(Blocked) as error:
                    failed.advance(7)
                failed.suspend('blocked', str(error.exception), outboxes=False)
                self.assertEqual(self.loaded().state['command_id'], command)
                self.assertNotIn('invalidations', self.loaded().state)
                self.gh.comment('/concurro retry')
                retry = self.gh.comments[-1]['id']
                controller = self.controller()
                with patch.object(controller, 'invalidate_task', wraps=controller.invalidate_task) as invalidate:
                    try:
                        controller.advance(7)
                    except Blocked as error:
                        controller.suspend('blocked', str(error), outboxes=False)
                    invalidate.assert_not_called()
                self.assertEqual(self.loaded().state['command_id'], retry)
                self.config['max_model_calls'] = CONFIG['max_model_calls']
                self.gh.pr['head']['sha'] = prior['head']
                self.gh.pages = original_pages
                self.gh.comment('/concurro retry')
                state = self.tick().state
                self.assertEqual(state['stage'], 'gates')
                self.assertNotIn('invalidations', state)
                self.assertEqual(state['approval'], prior['approval'])

    def test_transient_read_failure_leaves_invalidation_pending(self):
        self.reach('gates')
        self.gh.comment('/concurro invalidate')
        command = self.gh.comments[-1]['id']
        failed = self.loaded()
        with patch.object(self.gh, 'pages', side_effect=Transient('unavailable')):
            with self.assertRaises(Transient):
                failed.invalidate_task(self.gh.comments[-1], self.gh.comments)
        self.assertLess(self.loaded().state['command_id'], command)
        self.assertEqual(self.tick().state['stage'], 'intake')

    def test_reassessment_notice_recovers_a_lost_post_response_once(self):
        self.reach('gates')
        self.tick()
        self.assertFalse(self.gh.pr['draft'])
        self.gh.comment('/concurro invalidate')
        repo = self.gh.repo

        def lost_response(path, method='GET', data=None):
            """Post the reassessment notice, then lose its acknowledgement."""
            result = repo(path, method, data)
            if path == 'issues/7/comments' and method == 'POST' and 'Reassessment started' in data['body']:
                raise RuntimeError('lost comment response')
            return result

        with patch.object(self.gh, 'repo', side_effect=lost_response):
            with self.assertRaises(PublicationError):
                self.tick()
        self.assertEqual(self.tick().state['stage'], 'plan')
        notices = [c for c in self.gh.comments if 'Reassessment started' in c['body']]
        self.assertEqual(len(notices), 1)
        self.assertIn('Please do not merge', notices[0]['body'])
        self.assertIn('/pull/9', notices[0]['body'])
        self.assertFalse(self.gh.pr['draft'])
        self.assertEqual(len(self.loaded().state['invalidations']), 1)

    def implementation_upgrade(self):
        """Install an implementer-only skill revision."""
        path = self.root / implementation_bundle.SKILL
        path.write_text(path.read_text() + '\nKeep validation focused on the approved plan.\n')

    def blocked_implementation(self):
        """Save an interrupted implementation with a retained role session."""
        controller = self.reach('implement')
        session = sessions.prepare(controller.state, self.config, self.config['implementer'], 'develop', {})
        controller.state['sessions'][session['role']]['turn'] = 2
        controller.state['calls'] += 1
        controller.state['operation'] = {'status': 'failed', 'runner_termination': 'old termination'}
        controller.suspend('blocked', 'Implementation runner stopped.', outboxes=False)
        return deepcopy(controller.state), session['role']

    def retry_commands(self):
        """Apply the writer command without starting a model, for exact state assertions."""
        controller = self.loaded()
        controller.commands(self.gh.comments)
        return controller

    def test_implementation_only_retry_preserves_approval_and_completes(self):
        prior, role = self.blocked_implementation()
        self.implementation_upgrade()
        self.assertEqual(self.tick().state['stage'], 'blocked')
        self.gh.comment('/concurro retry', login='outsider')
        self.assertEqual(self.tick().state['stage'], 'blocked')
        self.gh.comment('/concurro retry')
        controller = self.retry_commands()
        state = controller.state
        for key in ('bundle_id', 'charter_id', 'plan', 'plan_id', 'approval', 'design_votes',
                    'calls', 'round', 'plan_revisions', 'requirements', 'config_digest'):
            self.assertEqual(state[key], prior[key], key)
        self.assertTrue(approval_valid(state, self.config))
        self.assertEqual(state['stage'], 'implement')
        self.assertNotIn('operation', state)
        self.assertNotEqual(state['sessions'][role]['scope'], prior['sessions'][role]['scope'])
        self.assertEqual(state['sessions'][role]['turn'], 0)
        for key, session in prior['sessions'].items():
            if key != role:
                self.assertEqual(state['sessions'][key], session)
        receipt = state['implementation_bundle']
        self.assertEqual(receipt['actor'], 'writer')
        self.assertEqual(receipt['comment'], state['command_id'])
        self.assertEqual(receipt['new_bundle'], controller.bundle.identity)
        self.assertEqual(controller.design_authorization()['implementation_bundle'], receipt)
        start = len(self.agents.calls)
        self.reach('gates')
        self.assertTrue(self.tick().state['handoff_ready'])
        turns = self.agents.calls[start:]
        self.assertEqual([stage for _, stage, _ in turns], ['develop', 'review', 'review'])
        self.assertTrue(all(ctx['bundle_id'] == receipt['new_bundle'] for _, _, ctx in turns))

    def test_adoption_requires_exact_implementation_only_difference(self):
        for resource in ('bundle/instructions.md', 'bundle/roles.json', 'bundle/schemas/develop.json',
                         'bundle/skills/concurro-design/SKILL.md', 'bundle/skills/concurro-review-pr/SKILL.md',
                         '.concurro/charter.json', '.concurro/instructions/implementer.md'):
            with self.subTest(resource=resource):
                self.setUp()
                prior, _ = self.blocked_implementation()
                self.implementation_upgrade()
                path = self.root / resource
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((path.read_text() if path.exists() else '') + '\n ')
                self.gh.comment('/concurro retry')
                with self.assertRaisesRegex(Blocked, '/concurro invalidate'):
                    self.retry_commands()
                state = self.loaded().state
                self.assertNotIn('implementation_bundle', state)
                self.assertEqual(state['approval'], prior['approval'])
                self.assertEqual(state['sessions'], prior['sessions'])
                self.assertEqual(state['command_id'], self.gh.comments[-1]['id'])

    def test_adoption_refusals_consume_command_without_mutating_authority(self):
        for cause in ('pending', 'config', 'requirements', 'approval', 'hash', 'missing_history'):
            with self.subTest(cause=cause):
                self.setUp()
                self.blocked_implementation()
                controller = self.loaded()
                if cause == 'pending':
                    controller.state['pending'] = {'head': 'a' * 40}
                elif cause == 'config':
                    self.config['max_model_calls'] += 1
                elif cause == 'requirements':
                    self.gh.issue['body'] += '\nA changed requirement.'
                elif cause == 'approval':
                    controller.state.pop('approval')
                elif cause == 'hash':
                    controller.state['implementation_skill'] = '0' * 64
                else:
                    controller.state.pop('implementation_skill')
                controller.save()
                prior = deepcopy(controller.state)
                self.implementation_upgrade()
                self.gh.comment('/concurro retry')
                with self.assertRaises(Blocked):
                    self.retry_commands()
                state = self.loaded().state
                self.assertNotIn('implementation_bundle', state)
                self.assertEqual(state['sessions'], prior['sessions'])
                self.assertEqual(state['calls'], prior['calls'])
                self.assertEqual(state['command_id'], self.gh.comments[-1]['id'])

    def test_retry_in_other_stages_cannot_adopt_implementation_change(self):
        for stage in ('plan', 'plan_qc', 'review'):
            with self.subTest(stage=stage):
                self.setUp()
                controller = self.reach(stage)
                controller.suspend('blocked', 'Stopped.', outboxes=False)
                self.implementation_upgrade()
                self.gh.comment('/concurro retry')
                with self.assertRaisesRegex(Blocked, '/concurro invalidate'):
                    self.tick()
                self.assertNotIn('implementation_bundle', self.loaded().state)

    def test_existing_pr_head_must_match_before_adoption(self):
        controller = self.reach('review')
        controller.state['stage'] = 'implement'
        controller.suspend('blocked', 'Stopped.', outboxes=False)
        self.implementation_upgrade()
        self.gh.pr['head']['sha'] = 'b' * 40
        self.gh.comment('/concurro retry')
        with self.assertRaisesRegex(Blocked, 'outside the controller'):
            self.retry_commands()
        self.assertNotIn('implementation_bundle', self.loaded().state)

    def test_adoption_checkpoint_lost_response_does_not_rotate_or_charge_twice(self):
        self.blocked_implementation()
        self.implementation_upgrade()
        self.gh.comment('/concurro retry')
        controller = self.loaded()
        save = controller.save

        def lost_response(*args, **kwargs):
            """Persist the atomic retry transition, then lose its response."""
            save(*args, **kwargs)
            raise RuntimeError('lost checkpoint response')

        with patch.object(controller, 'save', side_effect=lost_response):
            with self.assertRaisesRegex(RuntimeError, 'lost checkpoint response'):
                controller.commands(self.gh.comments)
        prior = deepcopy(self.loaded().state)
        state = self.retry_commands().state
        for key in ('implementation_bundle', 'sessions', 'command_id', 'calls'):
            self.assertEqual(state[key], prior[key])
        self.assertEqual(sum('### Implementation instructions updated' in c['body'] for c in self.gh.comments), 1)

    def test_later_upgrade_requires_another_writer_retry_and_invalidation_resets_adoption(self):
        self.blocked_implementation()
        self.implementation_upgrade()
        self.gh.comment('/concurro retry')
        controller = self.retry_commands()
        first = deepcopy(controller.state['implementation_bundle'])
        self.implementation_upgrade()
        with self.assertRaisesRegex(Blocked, '/concurro retry'):
            self.loaded().require_current_bundle()
        controller.suspend('blocked', BUNDLE_REASON, outboxes=False)
        self.gh.comment('/concurro retry')
        controller = self.retry_commands()
        self.assertEqual(controller.state['implementation_bundle']['old_bundle'], first['new_bundle'])
        self.assertEqual(controller.state['implementation_bundle']['old_skill'], first['new_skill'])
        self.upgrade()
        with self.assertRaisesRegex(Blocked, '/concurro invalidate'):
            self.loaded().require_current_bundle()
        self.gh.comment('/concurro invalidate')
        state = self.retry_commands().state
        self.assertNotIn('implementation_bundle', state)
        self.assertEqual(state['bundle_id'], self.controller().bundle.identity)
        self.assertEqual(state['implementation_skill'], self.controller().bundle.hashes[implementation_bundle.SKILL])
        self.assertNotIn('approval', state)

    def test_legacy_digest_is_proven_from_local_history(self):
        def git(*args):
            """Build a local fixture history without credentials or a remote."""
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)

        git('init')
        git('add', 'bundle')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'approved bundle')
        self.blocked_implementation()
        controller = self.loaded()
        controller.state.pop('implementation_skill')
        controller.save()
        prior = deepcopy(controller.state)
        self.implementation_upgrade()
        self.assertIsNotNone(implementation_bundle.original_skill(prior, self.controller().bundle))
        self.upgrade()
        self.assertIsNone(implementation_bundle.original_skill(prior, self.controller().bundle))
        path = self.root / 'bundle/instructions.md'
        path.write_text(path.read_text().replace('\nUpdated operator guidance.\n', ''))
        self.gh.comment('/concurro retry')
        state = self.retry_commands().state
        self.assertTrue(approval_valid(state, self.config))
        self.assertEqual(state['bundle_id'], prior['bundle_id'])
        self.assertIn('implementation_skill', state)

    def test_execution_rollback_also_requires_writer_retry(self):
        prior, role = self.blocked_implementation()
        path = self.root / implementation_bundle.SKILL
        original = path.read_text()
        self.implementation_upgrade()
        self.gh.comment('/concurro retry')
        controller = self.retry_commands()
        restarts = controller.state['sessions'][role]['restarts']
        path.write_text(original)
        with self.assertRaisesRegex(Blocked, '/concurro retry'):
            self.loaded().require_current_bundle()
        controller.suspend('blocked', implementation_bundle.RETRY_REASON, outboxes=False)
        self.gh.comment('/concurro retry')
        state = self.retry_commands().state
        self.loaded().require_current_bundle()
        self.assertEqual(state['implementation_bundle']['new_bundle'], prior['bundle_id'])
        self.assertEqual(state['sessions'][role]['restarts'], restarts + 1)
        self.assertEqual(state['approval'], prior['approval'])

    def test_changed_skill_blocks_active_implementation_with_retry_guidance(self):
        self.reach('implement')
        self.implementation_upgrade()
        calls = len(self.agents.calls)
        with self.assertRaisesRegex(Blocked, '/concurro retry'):
            self.tick()
        self.assertEqual(len(self.agents.calls), calls)
