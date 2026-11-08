"""Regressions from the independent MVP acceptance review."""

from copy import deepcopy
import subprocess
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule
from controller import Controller, changes
from github import Transient
from policy import Blocked
from readiness import ci_reasons
from relay import ExchangeError
from test_pipeline import FakeAgents, FakeGitHub
import controller
import runner
import processes


class RunnerCleanupTests(unittest.TestCase):
    """Successful cleanup and a lost cleanup response are both idempotent."""

    def test_serve_then_always_cleanup_and_lost_end_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grant = {"token": "runner", "key": "session", "binding": {"upstream": "o/u", "fork": "o/f"}}
            runner.save_grant(root, grant)
            client = Mock()
            args = ['runner.py', 'serve', '--session', 's', '--gateway', 'https://g', '--root', directory]
            with patch.object(sys, 'argv', args), patch.object(runner, 'Runner', return_value=client), \
                    patch.object(runner, 'Mirror'), patch.object(runner, 'Loop'):
                runner.main()
                self.assertFalse((root / 'grant.json').exists())
                args[1] = 'end'
                runner.main()
            self.assertEqual(client.end.call_count, 1)
            runner.save_grant(root, grant)
            client.end.side_effect = [ExchangeError(0, 'lost'), ExchangeError(410, 'ended')]
            with self.assertRaises(ExchangeError):
                runner.end_session(root, client)
            self.assertTrue((root / 'grant.json').exists())
            runner.end_session(root, client)
            self.assertFalse((root / 'grant.json').exists())

    def test_first_heartbeat_retries_without_stopping_setup_helper(self):
        client = Mock()
        client.next.side_effect = [ExchangeError(0, 'network'), []]
        with patch.object(runner.time, 'sleep'):
            runner.renew_before_serve(client)
        self.assertEqual(client.next.call_count, 2)

    def test_detached_child_reaping_preserves_existing_helpers(self):
        with patch.object(processes, 'children', side_effect=[{11, 22}, {11, 33}, {11}]), \
                patch.object(processes.os, 'kill') as kill, patch.object(processes.os, 'waitpid'), \
                patch.object(processes.time, 'sleep'):
            processes.finish({11})
        self.assertEqual([call.args[0] for call in kill.call_args_list], [22, 33])


class EvidenceTests(unittest.TestCase):
    """CI identity and controller work are bounded independently of provider honesty."""

    def test_same_named_jobs_in_different_suites_do_not_hide_failure(self):
        failed = {"id": 100, "app": {"id": 1}, "check_suite": {"id": 10}, "name": "test",
                  "head_sha": "a", "status": "completed", "conclusion": "failure"}
        passed = {**failed, "id": 101, "check_suite": {"id": 11}, "conclusion": "success"}
        self.assertIn('CI unsuccessful: test', ci_reasons([('a', [failed, passed], [])]))
        retry = {**failed, 'id': 102, 'conclusion': 'success'}
        self.assertEqual(ci_reasons([('a', [failed, retry, passed], [])]), [])

    def test_workflow_approval_and_empty_queued_suite_beat_cla_success(self):
        cla = [{"id": 1, "context": "CLA", "state": "success"}]
        pending = {"id": 11, "workflow_id": 1, "head_sha": "a", "name": "CI", "status": "waiting",
                   "conclusion": None}
        self.assertTrue(ci_reasons([('a', [], cla, [], [pending])]))
        self.assertTrue(ci_reasons([('a', [], cla, [{"id": 12, "head_sha": "a", "status": "queued"}], [])]))
        self.assertTrue(ci_reasons([('a', [], cla, [], [{**pending, 'status': 'completed', 'conclusion': 'action_required'}])]))
        self.assertEqual(ci_reasons([('a', [], cla, [], [{**pending, 'status': 'completed', 'conclusion': 'success'}])]), [])

    def test_replacement_workflow_run_supersedes_only_its_own_suite(self):
        old = {"id": 100, "workflow_id": 1, "check_suite_id": 10, "head_sha": "a", "status": "completed", "conclusion": "failure"}
        new = {**old, "id": 101, "check_suite_id": 11, "conclusion": "success"}
        checks = [{"id": 1, "check_suite": {"id": 10}, "name": "test", "head_sha": "a", "status": "completed", "conclusion": "failure"},
                  {"id": 2, "check_suite": {"id": 11}, "name": "test", "head_sha": "a", "status": "completed", "conclusion": "success"}]
        suites = [{"id": 10, "head_sha": "a", "status": "completed", "conclusion": "failure"},
                  {"id": 11, "head_sha": "a", "status": "completed", "conclusion": "success"}]
        self.assertEqual(ci_reasons([('a', checks, [], suites, [old, new])]), [])
        self.assertTrue(ci_reasons([('a', checks, [], suites, [old, {**new, "workflow_id": 2}])]))

    def test_repetitive_diff_is_bounded_before_matcher_runs(self):
        before = 'start\n' + 'a\nb\n' * 20_000 + 'end\n'
        after = 'other\n' + 'b\na\n' * 20_000 + 'last\n'
        with patch.object(controller.difflib, 'SequenceMatcher', side_effect=AssertionError('unbounded matcher')):
            [item] = changes({'x.rs': before}, {'x.rs': after})
        self.assertTrue(item['truncated'])
        self.assertIn('repository', item['diff'])


class PublicationTests(unittest.TestCase):
    """Foreign PRs are refused, but GitHub's asynchronous views do not suspend a valid publication."""

    def pending(self):
        self.config = {**deepcopy(CONFIG), 'autonomous': True}
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        for _ in range(15):
            controller = Controller(self.gh, self.agents, self.config)
            controller.advance(7)
            if controller.state['stage'] == 'publish':
                return controller
        self.fail('not publishing')

    def test_pr_creation_explicitly_disables_maintainer_modification(self):
        """The real publisher must not ask the upstream installation to grant fork collaboration."""
        controller = self.pending()
        original = self.gh.repo
        requests = []

        def repo(path, method="GET", data=None):
            """Model GitHub's fork-collaboration rejection when the flag is missing or enabled."""
            if path == "pulls" and method == "POST":
                requests.append(deepcopy(data))
                if data.get("maintainer_can_modify") is not False:
                    raise RuntimeError("fork_collab cannot be granted")
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=repo):
            controller.publish()
        self.assertEqual(controller.state["stage"], "review")
        self.assertEqual(len(requests), 1)
        self.assertIs(requests[0]["maintainer_can_modify"], False)
        self.assertIs(requests[0]["draft"], True)

    def test_foreign_author_and_different_recorded_pr_are_refused(self):
        controller = self.pending()
        controller.publish()
        controller.state.update(pending={**controller.state['candidate'], 'initial': False}, stage='publish')
        # Restore the saved pending intent using the candidate's matching parent/head/plan.
        self.gh.pr['user']['login'] = 'outsider'
        with self.assertRaisesRegex(Blocked, 'author'):
            controller.publish()
        self.gh.pr['user']['login'] = self.config['bot_login']
        controller.state['pr'] = 10
        with self.assertRaisesRegex(Blocked, 'author'):
            controller.publish()

    def test_stale_files_retry_without_duplicate_pr_or_new_model_turn(self):
        controller = self.pending()
        original = self.gh.pages
        def pages(path, *args):
            return [] if path == 'pulls/9/files' else original(path, *args)
        with patch.object(self.gh, 'pages', side_effect=pages), self.assertRaises(Transient):
            controller.publish()
        self.assertEqual(controller.state['stage'], 'publish')
        self.assertIn('pending', controller.state)
        calls = len(self.agents.calls)
        self.gh.pr['head']['sha'] = controller.state['pending']['parent']
        with self.assertRaises(Transient):
            controller.publish()
        self.gh.pr['head']['sha'] = controller.state['pending']['head']
        controller.publish()
        self.assertEqual(controller.state['stage'], 'review')
        self.assertEqual(len(self.agents.calls), calls)

    def test_original_file_refusal_becomes_bounded_feedback(self):
        controller = self.pending()
        controller.state['stage'] = 'implement'
        parent = controller.state['candidate']['parent']
        original, refused = controller.files, []
        def files(*args, **kwargs):
            if kwargs.get("inspection") and not refused:
                refused.append(True)
                raise Blocked('not a regular file')
            return original(*args, **kwargs)
        with patch.object(controller, 'files', side_effect=files):
            self.assertIsNotNone(controller.develop(parent))
        feedback = [c['implementation_feedback'] for _, stage, c in self.agents.calls if stage == 'develop'][-1]
        self.assertIn('Nothing was applied', feedback)


@unittest.skipUnless(sys.platform == 'linux', 'Linux runner orphan adoption')
class NativeDescendantTests(unittest.TestCase):
    """A real setsid daemon is reaped before another turn can use the workspace."""

    def test_detached_grandchild_dies_with_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'pid'
            script = "import os,time,pathlib; pid=os.fork(); (exit(0) if pid else None); os.setsid(); pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); time.sleep(60)"
            baseline = processes.begin()
            child = subprocess.Popen([sys.executable, '-c', script])
            try:
                child.wait(timeout=5)
                import time
                deadline = time.monotonic() + 5
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pidfile.exists())
                pid = int(pidfile.read_text())
                processes.finish(baseline)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                processes.finish(baseline)
