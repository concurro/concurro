"""Rejected planner output stays visible and gets bounded, restartable correction."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import CONFIG, EXECUTION, setUpModule, tearDownModule
from checkpoints import Checkpoints, PLAN_NAME, LIMIT
from controller import Controller, PLAN_OUTPUT_REPAIRS
from policy import Blocked, validate_plan
import checks
from test_pipeline import PLAN, FakeAgents, FakeGitHub


class DirectoryScopeTests(unittest.TestCase):
    """Directory scopes mean recursive descendants without broadening path policy."""

    def test_directories_match_descendants_and_keep_legacy_files_exact(self):
        """The pilot's directory notation is accepted and does not match a sibling prefix."""
        plan = {**deepcopy(PLAN), "scope": {"paths": ["docs/guide.md", "changelog.d/"],
                                          "max_files": 2}}
        validate_plan(plan, CONFIG, EXECUTION)
        self.assertEqual(checks.scope_of(plan)["paths"], ["docs/guide.md", "changelog.d/**"])
        self.assertTrue(checks.in_scope("changelog.d/nested/change.md", plan))
        self.assertFalse(checks.in_scope("changelog.d-other/change.md", plan))
        self.assertFalse(checks.in_scope("docs/other.md", PLAN))
        plan["scope"]["paths"] = ["docs/"]
        validate_plan(plan, CONFIG, EXECUTION)
        self.assertTrue(checks.in_scope("docs/guide.md", plan))

    def test_unsafe_directories_and_invalid_types_are_still_rejected(self):
        """Slash normalization must not permit absolute, traversing or empty components."""
        for value in ("/", "/docs/", "../docs/", "docs/../", "docs//", "./docs/", {}):
            with self.subTest(value=value):
                plan = {**deepcopy(PLAN), "scope": {"paths": [value], "max_files": 2}}
                with self.assertRaises(Blocked):
                    validate_plan(plan, CONFIG, EXECUTION)


class PlanRepairTests(unittest.TestCase):
    """Use the real controller and artifact recovery with deterministic models."""

    def setUp(self):
        """Stop after intake, immediately before the first planner dispatch."""
        self.config = deepcopy(CONFIG)
        self.config['autonomous'] = False
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        controller = self.tick()
        self.assertEqual(controller.state['stage'], 'plan')

    def tick(self):
        """Run a fresh controller process against the stored issue state."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.advance(7)
        return controller

    def artifact(self):
        """Locate the managed plan and its immutable version history."""
        return next(v for v in self.gh.artifacts[7].values() if v['artifact']['name'] == PLAN_NAME)

    def invalid(self):
        """Supply an otherwise valid plan with a correctable file budget."""
        return {**deepcopy(PLAN), 'scope': {'paths': ['docs/'], 'max_files': 0}}

    def test_rejected_plan_is_visible_then_corrected_without_human_retry(self):
        """The rejected artifact includes the reason; the next dispatch receives it and continues."""
        original = self.agents.ask
        contexts = []

        def answer(provider, stage, context):
            """Return one malformed plan, then let the normal planner correct it."""
            contexts.append(deepcopy(context))
            return self.invalid() if len(contexts) == 1 else original(provider, stage, context)

        with patch.object(self.agents, 'ask', side_effect=answer):
            rejected = self.tick()
            self.assertEqual(rejected.state['stage'], 'plan')
            self.assertEqual(rejected.outcome(True).kind, 'continue')
            self.assertNotIn('plan', rejected.state)
            self.assertNotIn('approval', rejected.state)
            body = self.artifact()['artifact']['body']
            self.assertIn('Rejected draft', body)
            self.assertIn('max_files must', body)
            self.assertIn('Clarify the guide.', body)
            self.assertIn('Implementation plan', self.gh.summary()['body'])
            accepted = self.tick()
        self.assertEqual(accepted.state['stage'], 'plan_qc')
        self.assertNotIn('plan_draft', accepted.state)
        self.assertNotIn('reason', accepted.state)
        self.assertEqual(accepted.state['plan_revisions'], 0)
        self.assertIn('max_files must', contexts[1]['plan_validation_feedback']['reason'])
        self.assertNotIn('Rejected draft', self.artifact()['artifact']['body'])
        self.assertTrue(any('Rejected draft' in v['body'] for v in self.artifact()['versions']))

    def test_repeated_bad_output_escalates_and_writer_can_retry(self):
        """Restarts cannot reset the correction allowance; explicit retry grants a new one."""
        with patch.object(self.agents, 'ask', return_value=self.invalid()) as ask:
            for _ in range(PLAN_OUTPUT_REPAIRS + 1):
                controller = self.tick()
            self.assertEqual(controller.state['stage'], 'escalated')
            self.assertEqual(controller.current_step(), 1)
            self.assertEqual(self.tick().state['stage'], 'escalated')
            self.assertEqual(ask.call_count, 3)
        self.gh.comment('/concurro retry')
        self.assertEqual(self.tick().state['stage'], 'plan_qc')

    def test_malformed_and_oversized_output_still_publish_a_rejection(self):
        """Bad shapes and large text become planner feedback rather than lost output."""
        for report in ([], {**deepcopy(PLAN), 'responses': [{'id': [], 'response': 'x'}]},
                       {**deepcopy(PLAN), 'summary': '```\n<details>\n' * 10000},
                       {**deepcopy(PLAN), 'summary': '`' * 30000}):
            with self.subTest(report_type=type(report)):
                self.setUp()
                with patch.object(self.agents, 'ask', return_value=report):
                    controller = self.tick()
                self.assertEqual(controller.state['stage'], 'plan')
                body = self.artifact()['artifact']['body']
                self.assertIn('Rejected draft', body)
                self.assertLessEqual(len(controller.checkpoints.audit_body(controller.state).encode()), LIMIT)
                if len(json.dumps(report)) > 20000:
                    self.assertTrue(controller.state['plan_draft']['truncated'])
                    self.assertIn('Output excerpted', body)

    def test_completed_result_recovers_without_duplicate_dispatch_or_attempt(self):
        """Crash before saving the rejection reuses the retained model response once."""
        original = Checkpoints.save
        interrupted = []

        class Crash(BaseException):
            """Simulate process death without the normal failure handler."""

        def save(store, state):
            """Crash between response retention and the rejected draft's first checkpoint."""
            if state.get('plan_draft') and not interrupted:
                interrupted.append(True)
                raise Crash()
            return original(store, state)

        with patch.object(self.agents, 'ask', return_value=self.invalid()) as ask:
            with patch.object(Checkpoints, 'save', save), self.assertRaises(Crash):
                self.tick()
            recovered = self.tick()
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(recovered.state['plan_draft']['attempt'], 1)
        self.assertEqual(recovered.state['operation']['status'], 'consumed')
        self.assertIn('Rejected draft', self.artifact()['artifact']['body'])

    def test_lost_plan_artifact_response_is_reconstructed_before_correction(self):
        """An applied artifact creation with a lost response never produces a second plan artifact."""
        original = self.gh.repo
        lost = []

        def repo(path, method='GET', data=None):
            """Lose only the first plan-artifact create response after GitHub stores it."""
            result = original(path, method, data)
            if method == 'POST' and (data or {}).get('type') == 'plan' and not lost:
                lost.append(True)
                raise RuntimeError('lost response')
            return result

        with patch.object(self.agents, 'ask', return_value=self.invalid()):
            with patch.object(self.gh, 'repo', side_effect=repo), self.assertRaises(Exception):
                self.tick()
        self.assertTrue(lost)
        accepted = self.tick()
        self.assertEqual(accepted.state['stage'], 'plan_qc')
        plans = [v for v in self.gh.artifacts[7].values() if v['artifact']['name'] == PLAN_NAME]
        self.assertEqual(len(plans), 1)
        self.assertTrue(any('Rejected draft' in v['body'] for v in plans[0]['versions']))

    def test_transport_errors_do_not_become_planner_corrections(self):
        """Only response validation is recoverable through planner feedback."""
        with patch.object(self.agents, 'ask', side_effect=RuntimeError('transport failed')):
            with self.assertRaisesRegex(RuntimeError, 'transport failed'):
                self.tick()
        self.assertFalse(any(v['artifact']['name'] == PLAN_NAME for v in self.gh.artifacts[7].values()))
