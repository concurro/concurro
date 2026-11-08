"""Plans and candidates remain bounded without a predicted line quota."""

from copy import deepcopy
import json
import unittest

from support import CONFIG, ROOT, setUpModule, tearDownModule
import candidates
import checks
from controller import Controller, changes
from policy import Blocked, OutsideScope, digest, validate_edits, validate_plan
from test_pipeline import PLAN, FakeAgents, FakeGitHub


class LineBudgetRemovalTests(unittest.TestCase):
    """Line count cannot authorize paths or reject an otherwise supported change."""

    def setUp(self):
        """Use the current planner contract without a line allowance."""
        self.plan = {**deepcopy(PLAN), 'scope': {'paths': ['docs/**'], 'max_files': 3}}

    def test_current_schema_and_validator_do_not_require_a_line_budget(self):
        """The provider contract and controller agree on the new scope shape."""
        scope = json.loads((ROOT / 'bundle/schemas/plan.json').read_text())['properties']['scope']
        self.assertEqual(set(scope['required']), {'paths', 'max_files'})
        self.assertEqual(set(scope['properties']), {'paths', 'max_files'})
        self.assertFalse(scope['additionalProperties'])
        validate_plan(self.plan, CONFIG)

    def test_large_cumulative_change_ignores_legacy_limit_without_rewriting_plan(self):
        """Both new and stored plans admit many short lines, with informational diff counts."""
        before = {'docs/a.md': ''.join(f'old {i}\n' for i in range(2000)), 'docs/b.md': None}
        after = {'docs/a.md': ''.join(f'new {i}\n' for i in range(2000)),
                 'docs/b.md': ''.join(f'test {i}\n' for i in range(2000))}
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                plan = deepcopy(self.plan)
                if legacy:
                    plan['scope']['max_changed_lines'] = 1
                original = deepcopy(plan)
                identifier = digest(plan)
                validate_plan(plan, CONFIG)
                self.assertEqual(candidates.validate(before, after, plan, CONFIG), sorted(after))
                self.assertNotIn('max_changed_lines', checks.scope_of(plan))
                self.assertEqual(plan, original)
                self.assertEqual(digest(plan), identifier)
        stats = changes(before, after)
        self.assertEqual(sum(v['added'] + v['removed'] for v in stats), 6000)

    def test_path_and_file_count_limits_still_apply(self):
        """Dropping size quotas does not broaden path or file-count authorization."""
        for path in ('outside.md', '.github/workflows/ci.yml', 'docs/../escape.md'):
            with self.subTest(path=path), self.assertRaises(OutsideScope):
                candidates.validate({}, {path: 'x'}, self.plan, CONFIG)
        with self.assertRaisesRegex(Blocked, '4 files'):
            candidates.validate({}, {f'docs/{i}.md': 'x' for i in range(4)}, self.plan, CONFIG)
        with self.assertRaisesRegex(Blocked, 'invalid text replacement'):
            validate_edits({'summary': 'binary', 'edits': [{'path': 'docs/a.md', 'content': 'x\x00'}]},
                           self.plan, {}, CONFIG)
        with self.assertRaisesRegex(Blocked, 'changes nothing'):
            candidates.validate({'docs/a.md': 'x'}, {'docs/a.md': 'x'}, self.plan, CONFIG)

    def test_large_change_reaches_publication_reviews_and_ready(self):
        """A real controller loop accepts over 3,000 lines with new and legacy scope records."""
        content = ''.join(f'Example {i}\n' for i in range(3100))
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                config = {**deepcopy(CONFIG), 'autonomous': True}
                gh = FakeGitHub(config)
                class LargeChange(FakeAgents):
                    """Return a supported large text change through normal plan and develop turns."""

                    def ask(self, provider, stage, context):
                        """Keep all ordinary reviews and change only the proposed scope/content."""
                        report = super().ask(provider, stage, context)
                        if stage == 'plan':
                            report['scope'] = {'paths': ['docs/**'], 'max_files': 1}
                            if legacy:
                                report['scope']['max_changed_lines'] = 1
                        elif stage == 'develop':
                            report['edits'] = [{'path': 'docs/guide.md', 'content': content}]
                        return report
                agents = LargeChange()
                for _ in range(20):
                    controller = Controller(gh, agents, config)
                    controller.advance(7)
                    if controller.state and controller.state.get('handoff_ready'):
                        break
                self.assertTrue(controller.state.get('handoff_ready'), controller.state.get('reason'))
                published = gh.trees[gh.commits[controller.state['head']]['tree']['sha']]
                self.assertEqual(published['docs/guide.md'], content)
                contexts = [c for _, stage, c in agents.calls if stage == 'review']
                self.assertEqual(len(contexts), 2)
                for context in contexts:
                    self.assertEqual(context['changes'][0]['added'], 3100)
                    self.assertEqual(context['changes'][0]['path'], 'docs/guide.md')
