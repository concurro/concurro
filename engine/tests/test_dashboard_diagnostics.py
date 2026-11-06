"""Review corrections remain inspectable without obscuring live task progress."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule
from checkpoints import LIMIT
from controller import Controller
from conversation import PublicationError
from policy import approval_valid, digest
from report_store import Store
from test_pipeline import CONFIG, FakeAgents, FakeGitHub


class DiagnosticsTests(unittest.TestCase):
    """Exercise dashboard history and rejected reports through durable controller restarts."""

    def setUp(self):
        """Start an ordinary task with deterministic provider results."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def tick(self):
        """Run a tick with no retained controller memory."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.advance(7)
        return controller

    def load(self):
        """Read the saved task without advancing it."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def reach_review(self):
        """Stop immediately before the first design-review call."""
        for _ in range(5):
            controller = self.tick()
            if controller.state['stage'] == 'plan_qc':
                return controller
        self.fail('design review not reached')

    def reject_review(self, large=False):
        """Return approval contradicted by an outstanding concern."""
        original = self.agents.ask
        captured = []

        def invalid(provider, stage, context):
            report = original(provider, stage, context)
            report['concerns'] = ['An unresolved correctness problem.']
            if large:
                report['rationale'] = 'FULL-REJECTED-REPORT ' + 'x' * 85000
            captured.append(deepcopy(report))
            return report

        with patch.object(self.agents, 'ask', side_effect=invalid):
            controller = self.tick()
        return controller, captured[0]

    def test_specific_reason_archive_and_recovery_are_visible_below_live_status(self):
        """A correction is evidence, never a vote; a successful replacement marks it recovered."""
        self.reach_review()
        controller, report = self.reject_review()
        reason = 'QC acceptance contradicts outstanding questions or concerns'
        self.assertEqual(controller.state['review_repair']['feedback']['detail'], reason)
        self.assertFalse(controller.state['design_votes'])
        self.assertFalse(approval_valid(controller.state, self.config))
        self.assertNotIn('review_draft', controller.state)
        diagnostic = next(r for r in controller.state['reports'] if r.get('diagnostic'))
        archive = self.gh.artifacts[7][diagnostic['artifact']]['artifact']['body']
        self.assertIn(reason, archive)
        self.assertIn('not an accepted review', archive)
        self.assertEqual(controller.checkpoints.reports.get(digest(report)), report)
        summary = self.gh.summary()['body']
        self.assertLess(summary.index('| Design review |'), summary.index('<summary>Diagnostics</summary>'))
        self.assertIn('Correction requested', summary)
        self.assertIn('to correct its cooperative design review — correction 1 of', summary)
        self.assertIn(reason, summary)
        self.assertNotIn(report['concerns'][0], summary)
        controller = self.tick()
        self.assertTrue(controller.state['design_votes'])
        self.assertNotIn('review_repair', controller.state)
        self.assertIn('Recovered', self.gh.summary()['body'])
        last = next(context for _, stage, context in reversed(self.agents.calls) if stage == 'plan_qc')
        self.assertEqual(last['review_validation_feedback']['detail'], reason)

    def test_large_rejected_output_is_stored_once_and_reloadable(self):
        """Full rejection evidence does not consume the audit's inline capacity."""
        self.reach_review()
        controller, report = self.reject_review(large=True)
        self.assertLess(len(controller.checkpoints.audit['body'].encode()), LIMIT)
        store = Store(self.gh, 7, self.config['bot_login'])
        self.assertEqual(store.get(digest(report)), report)
        artifacts = [a['artifact']['body'] for a in self.gh.artifacts[7].values()]
        self.assertEqual(sum(body.count('FULL-REJECTED-REPORT') for body in artifacts), 1)
        self.assertTrue(self.load().state['reports'][-1]['diagnostic'])

    def test_lost_rejection_archive_response_recovers_without_duplicate_or_extra_review(self):
        """Pending evidence is recovered before a replacement model result can overwrite it."""
        self.reach_review()
        original, lost = self.gh.repo, []

        def repo(path, method='GET', data=None):
            result = original(path, method, data)
            if (method == 'POST' and path.endswith('/artifacts') and not lost
                    and '### Design review output rejected' in (data or {}).get('body', '')):
                lost.append(True)
                raise RuntimeError('lost response')
            return result

        with patch.object(self.gh, 'repo', side_effect=repo):
            with self.assertRaises(PublicationError):
                self.reject_review(large=True)
        before = self.load().state['calls']
        self.assertTrue(lost)
        controller = self.tick()
        self.assertEqual(controller.state['calls'], before + 1)
        diagnostics = [r for r in controller.state['reports'] if r.get('diagnostic')]
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]['diagnostic']['status'], 'Recovered')
        archives = [a['artifact']['body'] for a in self.gh.artifacts[7].values()]
        self.assertEqual(sum('### Design review output rejected' in a for a in archives), 1)

    def test_exhaustion_is_visible_and_old_checkpoints_do_not_invent_evidence(self):
        """A zero correction allowance escalates with the rejected output linked."""
        self.config['max_review_repairs'] = 0
        self.reach_review()
        controller, _ = self.reject_review()
        self.assertEqual(controller.state['stage'], 'escalated')
        self.assertIn('Escalated · 0/0 corrections', controller.diagnostics_table())
        controller.state['reports'] = [r for r in controller.state['reports'] if not r.get('diagnostic')]
        self.assertIn('Not retained by the earlier engine', controller.diagnostics_table())

    def test_malformed_review_can_be_archived_without_a_valid_report_shape(self):
        """Even a non-object result remains inspectable without granting approval."""
        self.reach_review()
        with patch.object(self.agents, 'ask', return_value=['not a review']):
            controller = self.tick()
        self.assertFalse(controller.state['design_votes'])
        self.assertEqual(controller.checkpoints.reports.get(digest(['not a review'])), ['not a review'])

    def test_current_and_historical_rounds_live_under_their_stage(self):
        """Stage rows and their report links remain together, without a separate rounds table."""
        controller = self.reach_review()
        controller.state['reports'] = [
            {'step': 'Design review', 'round': '1 cooperative', 'provider': 'claude', 'outcome': 'approve', 'artifact': 21},
            {'step': 'Design review', 'round': '1 fresh', 'provider': 'codex', 'outcome': 'revise', 'artifact': 22},
            {'step': 'Code review', 'round': '1', 'provider': 'claude', 'outcome': 'approve', 'artifact': 23}]
        summary = controller.summary_body()
        self.assertNotIn('<summary>Rounds', summary)
        self.assertLess(summary.index('| Design review |'), summary.index('↳ Round 1 cooperative'))
        self.assertLess(summary.index('↳ Round 1 fresh'), summary.index('| Implement |'))
        self.assertIn('codex: 🛠️ Revise', summary)
        self.assertLess(summary.index('| Code review |'), summary.index('/artifacts/23'))
        self.assertNotIn('↳ Round', controller.summary_lines(compact=True))

    def test_current_round_identifies_reviewing_and_waiting_agents(self):
        """A round with no submitted votes still shows its assigned reviewers."""
        controller = self.reach_review()
        from policy import design_roster
        roster = design_roster(self.config)
        controller.state['operation'] = {'provider': roster[0]['provider'], 'status': 'started'}
        summary = controller.summary_body()
        self.assertIn(roster[0]['provider'] + ': Reviewing', summary)
        self.assertIn(roster[1]['provider'] + ': Waiting', summary)
        controller.state.update(stage='escalated', retry_stage='plan_qc')
        self.assertNotIn(': Reviewing', controller.summary_body())
        controller.state.update(stage='review', round=0, pr=9, reviews={})
        controller.state.pop('operation')
        self.assertIn('| ↳ Round 1 | Current |', controller.summary_body())

    def test_now_line_distinguishes_paused_ci_and_human_review(self):
        """Old in-flight operation records cannot make a paused task look active."""
        controller = self.reach_review()
        controller.state.update(stage='escalated', retry_stage='plan_qc')
        self.assertIn('**Now:** Paused', controller.activity_line())
        controller.state.update(stage='gates', reason='CI pending', handoff_ready=False)
        self.assertIn('waiting on: CI pending', controller.activity_line())
        controller.state['handoff_ready'] = True
        self.assertIn('Ready for your review', controller.activity_line())
        self.assertNotIn('CI pending', controller.activity_line())
        controller.state['stage'] = 'observed_merged'
        self.assertIn('has been merged', controller.activity_line())

    def test_rejection_history_stays_bounded_through_the_call_budget(self):
        """Repeated invalid reviews retain links rather than copying raw output into the audit."""
        controller = self.reach_review()
        from policy import design_roster
        from design import ReviewError
        member = design_roster(self.config)[0]
        for i in range(48):
            controller.state['calls'] = i + 1
            controller.repair_review({'invalid': 'x' * 3000, 'attempt': i}, ReviewError('review_shape'), member)
            self.assertLess(len(controller.checkpoints.audit['body'].encode()), LIMIT)
            self.assertNotIn('review_draft', controller.state)
        self.assertLessEqual(len(controller.state['reports']), 40)
        self.assertLess(len(controller.summary_body().encode()), 60000)
        self.assertEqual(self.load().state['calls'], 48)

    def test_team_uses_each_roles_actual_model_and_effort(self):
        """Distinct plan/PR reviewer assignments and effort settings are printed faithfully."""
        from test_model_profiles import named_config
        self.config = named_config()
        self.config['agents']['plan_reviewers'] = ['opus-high', 'sol-medium']
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        self.tick()
        body = next(c['body'] for c in self.gh.comments if "### I'm on it" in c['body'])
        for row in ('| Intake | `gpt-6.1-sol` | medium |',
                    '| Planning | `claude-opus-5-5` | high |',
                    '| Design review | `gpt-6.1-sol` | medium |',
                    '| PR review | `gpt-6.1-sol` | high |',
                    '| PR review | `gpt-6-astra` | high |'):
            self.assertIn(row, body)

    def test_welcome_is_sent_once_and_recovers_lost_response(self):
        """The team introduction is recoverable and cannot trigger duplicate intake calls."""
        original, lost = self.gh.repo, []

        def repo(path, method='GET', data=None):
            result = original(path, method, data)
            if method == 'POST' and path.endswith('/comments') and "### I'm on it" in (data or {}).get('body', '') and not lost:
                lost.append(True)
                raise RuntimeError('lost response')
            return result

        with patch.object(self.gh, 'repo', side_effect=repo), self.assertRaises(PublicationError):
            self.tick()
        self.assertFalse(self.agents.calls)
        self.tick()
        self.tick()
        notices = [c['body'] for c in self.gh.comments if "### I'm on it" in c['body']]
        self.assertEqual(len(notices), 1)
        self.assertIn("I've picked up your issue", notices[0])
        self.assertIn('### Meet the team', notices[0])
        self.assertIn('| Role | Model | Effort |', notices[0])
        self.assertIn('| PR review |', notices[0])
