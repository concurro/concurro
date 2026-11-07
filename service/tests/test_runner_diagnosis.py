"""GitHub conclusions are diagnostics for the exact OIDC-bound implementation run."""

import unittest
from fixtures import ENGINE  # initializes the service and engine test paths
from support import setUpModule, tearDownModule
from service.worker import runner_termination

SESSION = '12345678-1234-1234-1234-123456789abc'
OBSERVATION = {'runner': {'session': SESSION, 'run_id': '77/1'}}
RUN = {'id': 77, 'run_attempt': 1, 'head_branch': 'concurro/runs/' + SESSION,
       'head_sha': 'a' * 40, 'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'failure'}


class DiagnosisTests(unittest.TestCase):
    """Do not guess timeout, trust other runs or expose raw GitHub failure text."""

    def diagnose(self, run=None, jobs=None, observation=None, error=None):
        """Supply bounded fake GitHub responses and capture the requested endpoints."""
        calls = []
        class Reader:
            def request(self, method, path):
                """Serve the exact attempt or its jobs."""
                calls.append((method, path))
                if error:
                    raise error
                return {'jobs': jobs or []} if '/jobs?' in path else (run or RUN)
        result = runner_termination(Reader(), 'owner/fork', 'a' * 40, observation or OBSERVATION)
        return result, calls

    def test_timeout_comes_from_job_not_the_generic_run_failure(self):
        result, calls = self.diagnose(jobs=[{'conclusion': 'timed_out', 'started_at': '2026-11-07T00:00:00Z',
                                           'completed_at': '2026-11-07T06:00:00Z'}])
        self.assertIn('timed out after 360m 0s', result)
        self.assertIn('https://github.com/owner/fork/actions/runs/77', result)
        self.assertEqual(calls[0][1], 'repos/owner/fork/actions/runs/77/attempts/1')
        self.assertEqual(calls[1][1], 'repos/owner/fork/actions/runs/77/attempts/1/jobs?per_page=100')

    def test_cancellation_failure_and_missing_result_have_distinct_messages(self):
        for conclusion, text in (('cancelled', 'was cancelled'), ('failure', 'failed'),
                                 ('success', 'finished without delivering')):
            result, _ = self.diagnose(run={**RUN, 'conclusion': conclusion})
            self.assertIn(text, result)
            self.assertNotIn('timed out', result)

    def test_mismatched_or_unfinished_run_is_not_diagnosed(self):
        for changes in ({'head_branch': 'main'}, {'head_sha': 'b' * 40}, {'run_attempt': 2},
                        {'id': 78}, {'event': 'push'}, {'status': 'in_progress'}):
            result, calls = self.diagnose(run={**RUN, **changes})
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)

    def test_unavailable_diagnostics_stay_unknown(self):
        result, _ = self.diagnose(error=RuntimeError('private diagnostic'))
        self.assertIsNone(result)
        for observation in ({'runner': {}}, {'runner': {'session': SESSION, 'run_id': '../77/1'}},
                            {'runner': {'session': SESSION, 'run_id': '77/2'}}):
            result, calls = self.diagnose(observation=observation)
            self.assertIsNone(result)
            self.assertEqual(calls, [])
