"""Exercise candidate scope and text validation against real isolated Git repositories."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from support import setUpModule, tearDownModule  # unittest module fixtures
import time
import unittest
from unittest.mock import patch

import checks
import design_eval
import workspace_entry as workspace
from test_pipeline import CONFIG
from test_local_loop import archive_files
from local import LocalWorkspace


class WorkbenchValidationTests(unittest.TestCase):
    """Use the actual helper against disposable local metadata, never the user's checkout."""

    def setUp(self):
        """Prepare the same synthetic baseline layout used by Docker workbenches."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.source, self.state = root / 'source', root / 'state'
        self.source.mkdir()
        self.state.mkdir()
        for target, value in (('ROOT', self.source), ('STATE', self.state)):
            patched = patch.object(workspace, target, value)
            patched.start()
            self.addCleanup(patched.stop)
        (self.source / 'README.md').write_text('Baseline\n')
        workspace.git('init', '--bare', '--initial-branch=main', str(self.state / 'git'))
        workspace.git('add', '--force', '-A')
        workspace.git('commit', '-m', 'Local baseline')
        self.state.joinpath('base').write_text(workspace.git('rev-parse', 'HEAD'))
        self.payload = {'originals': {'docs/a.md': None, 'docs/b.md': None}, 'spec': {
            'checks': ['diff', 'scope', 'text'], 'files': [
                {'path': path, 'change': 'add', 'max_lines': 3, 'required_text': ['Required']}
                for path in ('docs/a.md', 'docs/b.md')]}}

    def add(self, path, content='Required\n', commit=True):
        """Create a candidate file with an optional local checkpoint."""
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if commit:
            workspace.git('add', '--force', '-A')
            workspace.git('commit', '-m', 'Candidate checkpoint')

    def test_complete_diff_requires_both_additions_across_multiple_checkpoints(self):
        """Empty changes and a last-commit-only view cannot satisfy complete candidate scope."""
        with self.assertRaises(ValueError): workspace.check_scope(self.payload)
        self.add('docs/a.md')
        with self.assertRaises(ValueError): workspace.check_scope(self.payload)
        self.add('docs/b.md')
        workspace.check_scope(self.payload)
        workspace.check_text(self.payload)
        self.add('docs/extra.md')
        with self.assertRaises(ValueError): workspace.check_scope(self.payload)

    def test_untracked_staged_and_unstaged_candidates_never_escape_validation(self):
        """An allowed path is still rejected until every byte is checkpointed."""
        self.add('docs/a.md')
        self.add('docs/b.md', commit=False)
        with self.assertRaisesRegex(ValueError, 'uncommitted'): workspace.check_scope(self.payload)
        workspace.git('add', '-A')
        with self.assertRaisesRegex(ValueError, 'uncommitted'): workspace.check_scope(self.payload)
        workspace.git('commit', '-m', 'Second checkpoint')
        self.add('docs/a.md', 'Required changed\n', commit=False)
        with self.assertRaisesRegex(ValueError, 'uncommitted'): workspace.check_text(self.payload)

    def test_modification_cannot_satisfy_addition_and_original_base_survives_repairs(self):
        """Declared change types use approved original content, not merely the current attempt's parent."""
        self.add('docs/a.md')
        self.add('docs/b.md')
        wrong = deepcopy(self.payload)
        wrong['spec']['files'][0]['change'] = 'modify'
        with self.assertRaises(ValueError): workspace.check_scope(wrong)
        self.state.joinpath('base').write_text(workspace.git('rev-parse', 'HEAD'))
        self.add('docs/a.md', 'Required repaired\n')
        workspace.check_scope(self.payload)

    def test_line_limits_and_text_assertions_enforce_results(self):
        """The declared constraints fail checks rather than merely printing counts."""
        self.add('docs/a.md', 'Required\nsecond\nthird\nfourth\n')
        self.add('docs/b.md')
        with self.assertRaisesRegex(ValueError, 'line limit'): workspace.check_text(self.payload)
        self.add('docs/a.md', 'Required\u2028one logical line\n')
        workspace.check_text(self.payload)
        self.add('docs/b.md', 'Wrong text\n')
        with self.assertRaisesRegex(ValueError, 'required text'): workspace.check_text(self.payload)

    def test_diff_whitespace_check_uses_the_same_complete_checkpoint(self):
        """A clean last checkpoint cannot hide whitespace errors introduced earlier."""
        self.add('docs/a.md', 'Required trailing space \n')
        self.add('docs/b.md')
        with patch('sys.argv', ['workspace_entry.py', 'diff']), patch('sys.stdout', new_callable=io.StringIO), \
                self.assertRaises(subprocess.CalledProcessError):
            workspace.main()


class ValidationCatalogTests(unittest.TestCase):
    """Keep advertised capabilities and evaluator scoring consistent with execution."""

    def test_catalog_matches_dispatch_and_requires_complete_candidate_checks(self):
        """Neither Rust nor docs plans can omit scope, text or whitespace validation."""
        for paths in (['docs/a.md'], ['src/lib.rs']):
            catalog = checks.catalog(paths)
            offered = {v['id'] for v in catalog['checks']}
            self.assertTrue(set(checks.required(paths)).issubset(offered))
            self.assertTrue(set(checks.CONTROL_CHECKS).issubset(catalog['mandatory']))
            self.assertTrue(offered.issubset(set(checks.COMMANDS) | set(checks.CONTROL_CHECKS)))

    def test_evaluation_labels_never_enter_review_context_and_scoring_detects_misses(self):
        """The quality benchmark measures false approvals and false alarms independently."""
        results = []
        for case in design_eval.cases():
            request = design_eval.request(case, 'claude', CONFIG)
            self.assertNotIn('expected', request)
            self.assertNotIn('controller_session', request)
            self.assertNotIn('responses', request['plan'])
            self.assertFalse(checks.contract_errors(case['plan']))
            results.append({'id': case['id'], 'decision': 'approve'})
        score = design_eval.score(results)
        self.assertEqual((score['missed_defects'], score['false_alarms']), (3, 0))
        results[0]['decision'] = 'revise'
        self.assertEqual(design_eval.score(results)['false_alarms'], 1)
        results[-1]['error'] = 'TransportFailure'
        self.assertEqual(design_eval.score(results)['invalid'], 1)
        with self.assertRaises(ValueError): design_eval.score(results[:-1])


@unittest.skipUnless(os.environ.get('CONCURRO_TEST_EXECUTION_IMAGE'), 'requires pinned execution Docker image')
class DockerValidationTests(unittest.TestCase):
    """Exercise the actual declarative-check transport in a credential-free workbench."""

    def test_complete_candidate_and_declared_text_constraints(self):
        """Two local commits pass together; later text violations fail without changing scope."""
        paths = ['docs/a.md', 'docs/b.md']
        spec = {'checks': ['diff', 'scope', 'text'], 'files': [
            {'path': path, 'change': 'add', 'max_lines': 2, 'required_text': ['Required']} for path in paths]}
        with patch.dict(os.environ, {'CONCURRO_EXECUTION_IMAGE': os.environ['CONCURRO_TEST_EXECUTION_IMAGE']}):
            with LocalWorkspace(CONFIG, archive_files({'README.md': 'Baseline\n'}), 'concurro/validation-fixture') as candidate:
                candidate.configure_validation(spec, dict.fromkeys(paths))
                for path in paths:
                    candidate.checkpoint([{'path': path, 'content': 'Required\n'}])
                reports = candidate.run_checks(spec['checks'], [], time.monotonic() + 120)
                self.assertEqual([v['name'] for v in reports], spec['checks'])
                self.assertTrue(all(v['exit_code'] == 0 for v in reports), reports)
                candidate.checkpoint([{'path': paths[0], 'content': 'Wrong\nsecond\nthird\n'}])
                reports = candidate.run_checks(spec['checks'], [], time.monotonic() + 120)
                self.assertEqual([v['exit_code'] for v in reports[:2]], [0, 0], reports)
                self.assertNotEqual(reports[-1]['exit_code'], 0)
