"""Audit baselines verify immutable data and never issue remote writes."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule
import test_consensus
from design_audit import Snapshot, measure
from test_pipeline import Controller


class AuditTests(unittest.TestCase):
    """Collect fixture archives offline and reproduce a stable source manifest."""

    setUp = test_consensus.ConsensusTests.setUp
    tick = test_consensus.ConsensusTests.tick
    reach = test_consensus.ConsensusTests.reach

    def snapshot(self, directory):
        """Export fake API responses, including metadata, to the same offline format."""
        path = Path(directory)
        (path / 'snapshot.json').write_text(json.dumps({'repository': self.config['repository'],
            'issue': 7, 'collection_started_at': '2026-11-07T00:00:00+00:00'}))
        for number, value in self.gh.artifacts[7].items():
            (path / f'{number}.json').write_text(json.dumps(value))
        return Snapshot(path, self.config['repository'], 7)

    def test_complete_round_is_reproducible_without_network_and_counts_no_replay(self):
        """Durable replay and duplicate index references cannot inflate review counts."""
        self.reach('plan_qc')
        with patch.object(Controller, 'validate_design_report', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.tick()
        self.reach('implement')
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.snapshot(directory)
            with patch('design_audit.subprocess.run', side_effect=AssertionError('network')):
                result = measure(snapshot, self.config['bot_login'])
                again = measure(Snapshot(directory, self.config['repository'], 7), self.config['bot_login'])
            self.assertEqual(result, again)
            self.assertEqual(result['by_round']['run 1 / 1 cooperative']['accepted'], 2)
            self.assertTrue(all(r['verification'] == 'verified' for r in result['reports']))
            self.assertTrue(all('body_sha256' in s for s in result['sources']))
            self.assertIsNone(result['engine_commit'])

    def test_missing_or_tampered_data_is_reported_not_treated_as_clean(self):
        """An unavailable report contributes no invented objection counts or verification."""
        state = self.reach('implement')
        number = next(r['artifact'] for r in state['reports'] if r['step'] == 'Design review')
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                snapshot = self.snapshot(directory)
                path = Path(directory) / f'{number}.json'
                if missing:
                    path.unlink()
                else:
                    value = json.loads(path.read_text())
                    value['artifact']['body'] += 'edited'
                    path.write_text(json.dumps(value))
                result = measure(snapshot, self.config['bot_login'])
                report = next(r for r in result['reports'] if r['artifact'] == number)
                self.assertTrue(report['verification'].startswith('unavailable:'))
                self.assertNotIn('objection_dispositions', report)

    def test_transport_refuses_mutations_and_other_paths_before_invocation(self):
        """No caller can turn the audit reader into a general GitHub client."""
        self.reach('implement')
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.snapshot(directory)
            with patch('design_audit.subprocess.run', side_effect=AssertionError('invoked')):
                for path, method in (('issues/7/artifacts/1', 'POST'), ('issues/8/artifacts/1', 'GET'),
                                     ('issues/7/comments', 'GET'), ('issues/7/artifacts/1?x=y', 'GET')):
                    with self.subTest(path=path, method=method), self.assertRaises(ValueError):
                        snapshot.repo(path, method)
