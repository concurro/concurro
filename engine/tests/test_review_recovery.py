"""Realistic citation, review capacity and durable reviewer-repair regressions."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule  # unittest module fixtures
import design
from controller import Controller
from policy import Blocked, approval_valid, validate_qc
from repository import MAX_FILE, Repository, source_lines
from test_autonomy import decision
from test_pipeline import CONFIG, FakeAgents, FakeGitHub, design_report


def sized_report(context, width=300):
    """Build bounded but realistically wordy evidence for every supplied criterion."""
    report = design_report(decision('approve'), context)
    report['rationale'] = 'The source supports the requested design. ' * 25
    report['scope']['fit'] = 'Documentation supports operator usability. ' * 12
    for assessment in report['assessments']:
        assessment['reason'] = 'Source and plan satisfy this requirement. ' * 10
        assessment['reason'] = assessment['reason'][:400]
        assessment['counterexample'] = ('An omitted behavior would contradict this criterion. ' * 8)[:width]
    return report


class ReviewEvidenceTests(unittest.TestCase):
    """Check exact source provenance without confusing excerpts with whole source lines."""

    def test_fragments_indentation_crlf_and_multiline_quotes(self):
        """A literal fragment may start within its cited line, never on a different line."""
        lines = source_lines('header\r\n    let merge_enabled = false;\r\n    next();\r\n')
        self.assertTrue(design.citation_matches(lines, 2, 'let merge_enabled = false;'))
        self.assertTrue(design.citation_matches(lines, 2, 'merge_enabled'))
        self.assertTrue(design.citation_matches(lines, 2, 'false;\r\n    next();'))
        for line, quote in ((1, 'merge_enabled'), (3, 'merge_enabled'), (0, 'header'),
                            (4, 'next'), (2, 'false;\nnext();'), (2, 'true')):
            self.assertFalse(design.citation_matches(lines, line, quote))
        self.assertTrue(design.citation_matches(source_lines('a\u2028b\nnext'), 1, 'b'))
        self.assertFalse(design.citation_matches(source_lines('a\u2028b\nnext'), 2, 'b'))

    def test_headings_courtesy_repetition_and_substantive_short_answers(self):
        """Courtesy does not spend criteria; yes/no, constraints and substantive headings remain."""
        request = {'title': 'Guide', 'body': '# Acceptance criteria\n\n- Preserve this behavior.\n\nValidation:\n\n2. Check it.',
                   'discussion': [{'body': text} for text in ['Thanks!'] * 40 +
                                  ['Yes.', 'No.', 'Thanks, but keep merging disabled.', '# Never enable merging', 'Yes.']]}
        parts = [r['text'] for r in design.criteria(request)]
        self.assertEqual(len(parts), 7)
        self.assertIn('Yes.', parts)
        self.assertIn('No.', parts)
        self.assertIn('Thanks, but keep merging disabled.', parts)
        self.assertIn('# Never enable merging', parts)
        self.assertNotIn('Validation:', parts)
        request['discussion'] = [{'body': str(i)} for i in range(35)]
        with self.assertRaises(Blocked): design.criteria(request)

    def test_large_review_is_allowed_but_schema_and_actual_byte_bounds_apply(self):
        """Thirteen useful criteria can exceed the old fixed cap without being invalid."""
        request = {'title': 'Guide', 'body': '\n\n'.join(f'- Requirement {i}' for i in range(12))}
        context = {'review_criteria': design.criteria(request)}
        report = sized_report(context)
        self.assertGreater(len(json.dumps(report).encode()), 12000)
        validate_qc(report, 'plan_qc', criteria_count=13)
        design.validate_evidence(report, request, files={'CONTRIBUTING.md': 'Use docs.'})
        oversized = deepcopy(report)
        oversized['assessments'][0]['reason'] += 'x'
        with self.assertRaises(design.ReviewError) as caught:
            design.validate_evidence(oversized, request)
        self.assertEqual(caught.exception.field, 'assessments[0].reason')
        unicode_report = deepcopy(report)
        for assessment in unicode_report['assessments']:
            assessment['reason'] = '\U0001f600' * 400
        with self.assertRaises(Blocked): validate_qc(unicode_report, 'plan_qc', criteria_count=13)


class ReviewerRepairTests(unittest.TestCase):
    """Advance only fake GitHub issues while measuring real checkpoint payloads."""

    def setUp(self):
        """Seed a supervised fixture with independent deterministic provider roles."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def tick(self):
        """Recover state from artifacts between every provider attempt."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.advance(7)
        return controller

    def reach(self, stage, phase=None):
        """Bound fixture setup and require the requested review phase when supplied."""
        for _ in range(15):
            controller = self.tick()
            if controller.state['stage'] == stage and (phase is None or controller.state['design_phase'] == phase):
                return controller
        self.fail('stage not reached')

    def test_large_source_uses_same_inspection_lines_and_keeps_edit_limit(self):
        """A source above 128 KB is valid citation evidence, including a fragment after indentation."""
        path = 'stackslib/source.rs'
        source = 'padding\n' * 20000 + '    let merge_enabled = false;\r\n'
        self.gh.trees[self.gh.base][path] = source
        self.reach('plan_qc')
        original = self.agents.ask
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir()
            target.write_text(source)
            page = Repository(root).read_file(path, offset=20000, limit=1)
            self.assertEqual(page['lines'][0]['line'], 20001)
        def citing(provider, stage, context):
            """Cite the same pinned line that the MCP inspection tool returned."""
            report = original(provider, stage, context)
            report['sources'][0].update(path=path, line=20001, quote='let merge_enabled = false;')
            return report
        with patch.object(self.agents, 'ask', side_effect=citing):
            controller = self.tick()
        self.assertEqual(len(controller.state['design_votes']), 1)
        self.assertNotIn('review_repair', controller.state)
        with self.assertRaises(Blocked): controller.files(self.gh.base, [path])
        self.gh.trees[self.gh.base][path] = 'x' * (MAX_FILE + 1)
        controller.trees.clear()
        with self.assertRaises(Blocked): controller.files(self.gh.base, [path], inspection=True)

    def test_cooperative_repair_retains_session_and_spent_calls_without_peer_input(self):
        """An inaccurate citation is corrected by its owner before any vote is accepted."""
        self.reach('plan_qc')
        original = self.agents.ask
        def bad(provider, stage, context):
            """Return an unsupported literal on only the first attempt."""
            report = original(provider, stage, context)
            report['sources'][0]['quote'] = 'fabricated'
            return report
        with patch.object(self.agents, 'ask', side_effect=bad):
            before = self.tick().state
        self.assertFalse(before['design_votes'])
        self.assertEqual(before['review_repairs'], 1)
        after = self.tick().state
        contexts = [c for _, stage, c in self.agents.calls if stage == 'plan_qc']
        self.assertEqual(contexts[0]['controller_session']['scope'], contexts[1]['controller_session']['scope'])
        self.assertEqual(contexts[1]['controller_session']['turn'], 1)
        self.assertEqual(contexts[1]['review_validation_feedback']['code'], 'review_citation')
        self.assertEqual(contexts[1]['review_validation_feedback']['field'], 'sources[0]')
        self.assertNotIn('design_votes', contexts[1])
        self.assertEqual(after['calls'], before['calls'] + 1)
        self.assertEqual(after['plan_revisions'], 0)
        self.assertEqual(len(after['design_votes']), 1)
        self.assertNotIn('review_repair', after)
        self.tick()
        self.assertNotIn('review_validation_feedback', self.agents.calls[-1][2])

    def test_fresh_correction_is_independent_and_cannot_approve_incomplete_evidence(self):
        """Fresh corrections get only their own fixed diagnostic, never cooperative history."""
        self.agents.qc_reports = [decision('proceed'), decision('approve'),
                                 decision('revise', concerns=['Add an error-path regression.'])]
        self.reach('plan_qc', 'fresh')
        original = self.agents.ask
        def missing(provider, stage, context):
            """Omit one assessment from an otherwise approving response."""
            report = original(provider, stage, context)
            report['assessments'].pop()
            return report
        with patch.object(self.agents, 'ask', side_effect=missing):
            state = self.tick().state
        self.assertFalse(approval_valid(state, self.config))
        self.tick()
        context = self.agents.calls[-1][2]
        self.assertEqual(context['review_phase'], 'fresh')
        for key in ('controller_session', 'own_objections', 'design_feedback', 'design_votes'):
            self.assertNotIn(key, context)
        self.assertNotIn('responses', context['plan'])
        self.assertIn('review_validation_feedback', context)

    def test_new_objection_errors_get_actionable_feedback_then_record_the_dissent(self):
        """Pilot #6's invented ID and missing dispositions can be corrected without losing the concern."""
        for failure, code, field in (
                ('invented_id', 'review_objection_id', 'objection_updates[0].id'),
                ('missing_updates', 'review_concerns', 'objection_updates')):
            with self.subTest(failure=failure):
                self.setUp()
                self.reach('plan_qc')
                original = self.agents.ask
                concern = 'Specify stable slot reuse and partial-flush recovery.'

                def review(provider, stage, context):
                    """Correct the malformed objection only after receiving its specific diagnostic."""
                    original(provider, stage, context)
                    report = design_report(decision('revise', concerns=[concern]), context)
                    if 'review_validation_feedback' not in context:
                        if failure == 'invented_id':
                            report['objection_updates'][0]['id'] = 'D1'
                        else:
                            report['objection_updates'] = []
                    else:
                        feedback = context['review_validation_feedback']
                        self.assertEqual((feedback['code'], feedback['field']), (code, field))
                        self.assertIn('id ""', feedback['detail'])
                    return report

                with patch.object(self.agents, 'ask', side_effect=review):
                    rejected = self.tick().state
                    self.assertFalse(rejected['design_votes'])
                    accepted = self.tick().state
                vote = next(iter(accepted['design_votes'].values()))
                self.assertEqual(vote['report']['decision'], 'revise')
                self.assertEqual(accepted['review_repairs'], 1)
                self.assertNotIn('review_repair', accepted)
                self.assertEqual(accepted['objections'][0]['text'], concern)
                self.assertTrue(accepted['objections'][0]['id'].startswith('O1-'))
                self.assertFalse(accepted['objections'][0]['resolved'])
                self.assertFalse(approval_valid(accepted, self.config))
                self.assertEqual(self.tick().state['stage'], 'plan')

    def test_repeat_or_global_repair_budget_stops_and_retry_does_not_refill(self):
        """Both identical failures and varying invalid output have finite durable retries."""
        for repeat in (True, False):
            with self.subTest(repeat=repeat):
                self.setUp()
                self.config['max_review_repairs'] = 1
                self.reach('plan_qc')
                original = self.agents.ask
                def bad(provider, stage, context):
                    """Vary the failed source only for the budget exhaustion case."""
                    report = original(provider, stage, context)
                    report['sources'][0]['quote'] = 'wrong' if repeat else str(len(self.agents.calls))
                    return report
                with patch.object(self.agents, 'ask', side_effect=bad):
                    self.tick()
                    stopped = self.tick().state
                    self.gh.comment('/concurro retry')
                    retried = self.tick().state
                self.assertEqual(stopped['stage'], 'escalated')
                self.assertEqual(retried['stage'], 'escalated')
                self.assertEqual(retried['review_repairs'], 1)
                self.assertGreater(retried['calls'], stopped['calls'])
                self.assertFalse(retried['design_votes'])
                self.assertEqual(retried['plan_revisions'], 0)

    def test_task_call_limit_still_preempts_reviewer_repairs(self):
        """A repair allowance cannot authorize an attempt after the task call ceiling."""
        self.config['max_model_calls'] = 3
        self.reach('plan_qc')
        original = self.agents.ask
        def bad(provider, stage, context):
            """Use the last available attempt for an invalid quote."""
            report = original(provider, stage, context)
            report['sources'][0]['quote'] = 'wrong'
            return report
        with patch.object(self.agents, 'ask', side_effect=bad): self.tick()
        calls = len(self.agents.calls)
        with self.assertRaisesRegex(Blocked, 'model-call budget'): self.tick()
        self.assertEqual(len(self.agents.calls), calls)

    def test_source_transport_failure_is_not_mislabeled_as_reviewer_output(self):
        """An unavailable GitHub read needs infrastructure recovery, not another model response."""
        self.reach('plan_qc')
        original = Controller.files
        def unavailable(controller, sha, paths, inspection=False):
            """Fail only the evidence read after the provider has returned."""
            if inspection:
                raise RuntimeError('fixture transport unavailable')
            return original(controller, sha, paths, inspection)
        with patch.object(Controller, 'files', unavailable), self.assertRaises(RuntimeError):
            self.tick()
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        self.assertEqual(controller.state['calls'], 3)
        self.assertFalse(controller.state.get('review_repairs'))
        self.assertFalse(controller.state['design_votes'])

    def test_revision_and_exhaustion_transitions_fit_checkpoint_with_full_findings(self):
        """Design feedback, archived objections and terminal outcomes are included in capacity checks."""
        for allowance in (0, 2):
            with self.subTest(allowance=allowance):
                self.setUp()
                self.config['max_plan_revisions'] = allowance
                self.reach('plan_qc')
                original, repo = self.agents.ask, self.gh.repo
                sizes = []
                def objections(provider, stage, context):
                    """Both independent reviewers retain concrete findings in a long report."""
                    original(provider, stage, context)
                    concerns = [f'Finding {i}: ' + 'The plan misses required behavior. ' * 8 for i in range(4)]
                    return design_report(decision('revise', concerns=concerns), context)
                def measure(path, method='GET', data=None):
                    """Include the actual transition and pending event in every measured write."""
                    if '/artifacts' in path and method in ('POST', 'PATCH'):
                        sizes.append(len(data['body'].encode()))
                    return repo(path, method, data)
                with patch.object(self.agents, 'ask', side_effect=objections), patch.object(self.gh, 'repo', side_effect=measure):
                    self.tick()
                    state = self.tick().state
                self.assertEqual(state['stage'], 'plan' if allowance else 'escalated')
                self.assertEqual(len(state['design_feedback']), 2)
                self.assertLessEqual(max(sizes), 60000)
                self.assertFalse(state.get('review_repairs'))

    def test_realistic_and_maximum_criteria_fit_every_checkpoint_through_consensus(self):
        """Both reviewers receive a usable budget and compact wording before any oversized save."""
        for count in (13, 32):
            with self.subTest(count=count):
                self.setUp()
                self.gh.issue['body'] = 'Acceptance criteria:\n\n' + '\n\n'.join(f'- Requirement {i}' for i in range(count - 1))
                self.reach('plan_qc')
                original, repo = self.agents.ask, self.gh.repo
                sizes, seen = [], []
                def wordy(provider, stage, context):
                    """Simulate a reviewer choosing concise wording using its explicit budget."""
                    report = original(provider, stage, context)
                    if stage == 'plan_qc':
                        report = sized_report(context)
                        seen.append(len(report['assessments']))
                        while len(json.dumps(report).encode()) > context['review_output_budget']['max_json_bytes']:
                            for assessment in report['assessments']:
                                for key in ('reason', 'counterexample'):
                                    assessment[key] = assessment[key][:max(1, len(assessment[key]) // 2)]
                    return report
                def measure(path, method='GET', data=None):
                    """Measure actual transport payloads, including each pending conversation event."""
                    if '/artifacts' in path and method in ('POST', 'PATCH'):
                        sizes.append(len(data['body'].encode()))
                    return repo(path, method, data)
                with patch.object(self.agents, 'ask', side_effect=wordy), patch.object(self.gh, 'repo', side_effect=measure):
                    controller = self.reach('implement')
                self.assertEqual(seen, [count] * 2)
                self.assertLessEqual(max(sizes), 60000)
                self.assertTrue(approval_valid(controller.state, self.config))
                self.assertFalse(controller.state.get('review_repairs'))

    def test_projected_checkpoint_overflow_is_repaired_without_saving_the_report(self):
        """A complete valid response cannot crowd out the checkpoint or correction notice."""
        self.gh.issue['body'] = '\n\n'.join(f'- Requirement {i}' for i in range(12))
        controller = self.reach('plan_qc')
        controller.state['fixture_padding'] = 'x' * 28000
        controller.save()
        original = self.agents.ask
        def wordy(provider, stage, context):
            """Return a schema-valid response exceeding the remaining checkpoint space."""
            original(provider, stage, context)
            return sized_report(context)
        with patch.object(self.agents, 'ask', side_effect=wordy), patch.object(Controller, 'review_output_budget', return_value={'max_json_bytes':32000}):
            state = self.tick().state
        self.assertEqual(state['review_repair']['feedback']['code'], 'review_checkpoint')
        self.assertFalse(state['design_votes'])
        self.assertNotIn('assessments', state.get('qc', {}).get('plan_qc', {}))
        controller = self.tick()
        self.assertEqual(len(controller.state['design_votes']), 1)
        self.assertLessEqual(len(controller.checkpoints.audit_body(controller.state).encode()), 60000)
