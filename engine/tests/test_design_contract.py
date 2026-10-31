"""Evidence-based review, individual findings, bounded repairs and artifact publication."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule  # unittest module fixtures
import checks
import conversation
import design
from controller import Controller
from policy import Blocked, approval_valid, consensus_valid, digest
from test_autonomy import decision
from test_pipeline import CONFIG, PLAN, FakeAgents, FakeGitHub, FakeWorkspace, design_report


class DesignContractTests(unittest.TestCase):
    """Reload durable state between provider turns, with merging always disabled."""

    def setUp(self):
        """Start a new isolated issue and deterministic provider transport."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def tick(self):
        """Use the ordinary controller without replacing authorization or publication logic."""
        controller = Controller(self.gh, self.agents, self.config, FakeWorkspace)
        controller.advance(7)
        return controller

    def load(self):
        """Recover the authoritative artifact without advancing work."""
        controller = Controller(self.gh, self.agents, self.config, FakeWorkspace)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def reach(self, stage):
        """Bound setup independently of the task's model-call budget."""
        for _ in range(12):
            controller = self.tick()
            if controller.state['stage'] == stage:
                return controller
        self.fail('stage not reached: ' + stage)

    def test_review_rejects_missing_criterion_forged_source_and_unresolved_approval(self):
        """An unsupported approve response never records an approving vote."""
        for failure in ('missing', 'quote', 'line', 'uncertain', 'duplicate'):
            with self.subTest(failure=failure):
                self.setUp()
                self.reach('plan_qc')
                original = self.agents.ask

                def invalid(provider, stage, context):
                    """Alter only evidence while leaving the approve verdict intact."""
                    report = original(provider, stage, context)
                    if failure == 'missing': report['assessments'].pop()
                    elif failure == 'quote': report['sources'][0]['quote'] = 'Invented source'
                    elif failure == 'line': report['sources'][0]['line'] = 999
                    elif failure == 'uncertain': report['assessments'][0]['status'] = 'uncertain'
                    else: report['assessments'].append(deepcopy(report['assessments'][0]))
                    return report

                with patch.object(self.agents, 'ask', side_effect=invalid):
                    self.tick()
                state = self.load().state
                self.assertEqual((state['stage'], state['review_repairs']), ('plan_qc', 1))
                self.assertFalse(state['design_votes'])
                self.assertFalse(approval_valid(state, self.config))
                self.assertFalse(self.gh.refs)

    def test_every_acceptance_bullet_and_authorized_discussion_is_covered(self):
        """Criterion extraction cannot silently omit a bullet or later requirement."""
        requirement = {'title': 'Guide', 'body': 'Acceptance:\n- First rule\n  with continuation\n- Second rule',
                       'discussion': [{'author': 'writer', 'body': 'Also third rule'}]}
        items = design.criteria(requirement)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[1]['text'], '- First rule\n  with continuation')
        self.assertEqual(items[-1]['text'], 'Also third rule')

    def test_executable_source_can_support_citations_but_is_not_editable(self):
        """Reading source evidence grants no permission to edit or follow symlinks."""
        controller = self.reach('plan_qc')
        tree = controller.tree(controller.state['base'])
        tree['CONTRIBUTING.md']['mode'] = '100755'
        path = ['CONTRIBUTING.md']
        self.assertEqual(controller.files(controller.state['base'], path, inspection=True)[path[0]], 'Use docs.')
        with self.assertRaises(Blocked): controller.files(controller.state['base'], path)
        tree[path[0]]['mode'] = '120000'
        with self.assertRaises(Blocked): controller.files(controller.state['base'], path, inspection=True)

    def test_partial_resolution_keeps_one_stable_objection(self):
        """Accepting one correction does not require approving the entire plan."""
        self.agents.qc_reports = [decision('proceed'), decision('approve'),
                                 decision('revise', concerns=['Clarify enrollment.', 'Validate the full diff.'])]
        for _ in range(4): self.tick()
        old = deepcopy(self.load().state['objections'])
        first_review = '\n'.join(c['body'] for c in self.gh.comments)
        for item in old:
            self.assertIn('**' + item['id'] + '**: ' + item['text'], first_review)
            self.assertIn('**' + item['id'] + '**: open', first_review)
        self.tick()
        self.tick()
        original = self.agents.ask

        def partial(provider, stage, context):
            """Resolve the first finding and refine the second under its original ID."""
            original(provider, stage, context)
            report = design_report(decision('revise', concerns=['Validate the full diff including additions.']), context)
            for item, old_item in zip(report['objection_updates'], context['own_objections']):
                item.update(id=old_item['id'], detail=old_item['text'], status='resolved')
            report['objection_updates'][1].update(status='open', detail=report['concerns'][0])
            return report

        with patch.object(self.agents, 'ask', side_effect=partial):
            controller = self.tick()
        self.assertEqual(len(controller.state['objections']), 1)
        self.assertEqual(controller.state['objections'][0]['id'], old[1]['id'])
        self.assertEqual(controller.state['objection_sequence'], 2)
        timeline = '\n'.join(c['body'] for c in self.gh.comments)
        self.assertIn('**' + old[0]['id'] + '**: resolved', timeline)
        self.tick()
        plan_context = [c for _, stage, c in self.agents.calls if stage == 'plan'][-1]
        self.assertEqual([v['id'] for v in plan_context['open_objections']], [old[1]['id']])
        self.assertEqual([v['id'] for v in self.load().state['plan']['responses']], [old[1]['id']])

    def test_foreign_or_duplicate_objection_resolution_is_rejected(self):
        """A reviewer cannot close another identity's findings or skip its own."""
        own = [{'id': 'O1', 'text': 'Fix wording.'}]
        context = {'review_criteria': design.criteria(self.gh.issue), 'own_objections': own}
        report = design_report(decision('approve'), context)
        for identifiers in (['foreign'], ['O1', 'O1'], []):
            candidate = deepcopy(report)
            candidate['objection_updates'] = [{**report['objection_updates'][0], 'id': v} for v in identifiers]
            with self.assertRaises(Blocked):
                design.validate_evidence(candidate, self.gh.issue, own)

    def test_contract_repair_precedes_review_and_preserves_design_budget(self):
        """Invalid named checks get bounded planner feedback without spending semantic revisions."""
        self.reach('plan')
        original = self.agents.ask

        def unsupported(provider, stage, context):
            """Return an otherwise valid plan with an invented shell check."""
            result = original(provider, stage, context)
            result['validation']['checks'] = ['git diff BASE HEAD']
            return result

        with patch.object(self.agents, 'ask', side_effect=unsupported):
            state = self.tick().state
        self.assertEqual((state['stage'], state['plan_revisions'], state['validation_repairs']), ('plan', 0, 1))
        self.assertFalse(state.get('approval'))
        self.assertFalse([c for c in self.agents.calls if c[1] == 'plan_qc'])
        state = self.tick().state
        self.assertEqual(state['stage'], 'plan_qc')
        self.assertEqual(state['calls'], 3)
        context = self.agents.calls[-1][2]
        self.assertEqual(context['validation_feedback'][0]['code'], 'validation_checks')
        self.assertIn('synthetic', context['validation_capabilities']['checks'][0]['purpose'])
        self.assertEqual(state['sessions'][next(iter(state['sessions']))]['turn'], 2)

    def test_no_progress_and_global_repair_budget_stop_without_reset(self):
        """Repeated defects or changed-but-still-invalid contracts cannot loop forever."""
        for repeat in (True, False):
            with self.subTest(repeat=repeat):
                self.setUp()
                self.config['max_validation_repairs'] = 1
                self.reach('plan')
                original = self.agents.ask

                def invalid(provider, stage, context):
                    """Change the attempted identifier only in the budget-exhaustion case."""
                    result = original(provider, stage, context)
                    result['validation']['checks'] = ['bad' if repeat else 'bad-' + str(len(self.agents.calls))]
                    return result

                with patch.object(self.agents, 'ask', side_effect=invalid):
                    self.tick()
                    state = self.tick().state
                self.assertEqual((state['stage'], state['calls'], state['plan_revisions']), ('escalated', 3, 0))
                self.assertEqual(state['validation_repairs'], 1)
                self.assertFalse(self.gh.refs)

    def test_plan_summary_recovers_after_artifact_write_without_reposting_plan(self):
        """A crash between the artifact and event preserves the exact versioned intent."""
        self.reach('plan')
        original = conversation.queue

        def interrupt(state, title, body=None, **kwargs):
            """Stop at the first summary publication after the artifact is durable."""
            if kwargs.get('kind') == 'plan_summary':
                raise KeyboardInterrupt()
            return original(state, title, body, **kwargs)

        with patch.object(conversation, 'queue', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.tick()
        recovered = self.load()
        self.assertTrue(recovered.state['plan_event_intent'])
        self.assertIn(PLAN['steps'][0], recovered.checkpoints.plan['body'])
        recovered.advance(7)
        events = [c['body'] for c in self.gh.comments if '· plan updated' in c['body']]
        self.assertEqual(len(events), 1)
        self.assertIn('/issues/7/artifacts/', events[0])
        self.assertIn('artifact version', events[0])
        self.assertIn(recovered.state['plan_id'], events[0])
        self.assertNotIn(PLAN['steps'][0], events[0])
        self.assertNotIn('plan_event_intent', self.load().state)

    def test_fresh_review_omits_summary_verdicts_and_publication_hints(self):
        """A final reviewer cannot inherit a planner's announcement of prior approval."""
        state = self.reach('implement').state
        fresh = [c for _, stage, c in self.agents.calls if stage == 'plan_qc' and c['review_phase'] == 'fresh']
        for context in fresh:
            for key in ('summary', 'publication', 'responses'):
                self.assertNotIn(key, context['plan'])
            self.assertNotIn('controller_session', context)
        self.assertTrue(approval_valid(state, self.config))
        member = next(iter(state['design_votes']))
        state['design_votes'][member]['report']['assessments'][0]['status'] = 'defect'
        self.assertFalse(consensus_valid(state, self.config))
