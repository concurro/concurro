"""Implementation job lifetime, termination, and writer-controlled recovery."""

from copy import deepcopy
import io
import json
import os
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
import agent_entry
from agents import Agents
from bundle import Bundle
from policy import Blocked, RunnerTerminated, SessionFailure, UncertainOutcome
from relay import ExchangeError
import turns
import test_turns
import test_consensus
from controller import Controller


class ExecutorTests(unittest.TestCase):
    """Long execution must not turn into a fresh automatic implementation attempt."""

    stage = "develop"
    runner = test_turns.ExecutorTests.runner

    def turn(self):
        """A bound implementation with no internal execution timer."""
        turn = deepcopy(test_turns.TURN)
        turn['request']['stage'] = self.stage
        turn['timeout'] = None
        return turn

    def test_running_implementation_survives_old_deadlines(self):
        now = [0]
        result = {'outcome': 'ok', 'response': {}}
        executor, operator, _ = self.runner([{'status': 'running'}, {'status': 'running'},
                                             {'status': 'done', 'result': result}], clock=lambda: now[0])
        handle = executor.submit(self.turn())
        now[0] = 5 * 3600
        self.assertIsNone(executor.observe(handle))
        self.assertEqual(executor.observe(handle), result)
        self.assertFalse(any(call[0] == 'cancel' for call in operator.calls))

    def test_observe_running_or_saved_result_before_applying_queue_deadline(self):
        for reply in ({'status': 'running'}, {'status': 'done', 'result': {'outcome': 'ok'}}):
            now = [0]
            executor, operator, _ = self.runner([{'status': 'queued'}, reply], clock=lambda: now[0])
            handle = executor.submit(self.turn())
            now[0] = 1801
            result = executor.observe(handle)
            self.assertEqual(result, reply.get('result'))
            self.assertFalse(any(c[0] == 'cancel' for c in operator.calls))

    def test_each_implementation_attempt_uses_a_separate_staffing_group(self):
        executor, operator, _ = self.runner([{}, {}, {}])
        turn = self.turn()
        executor.submit(turn, identifier='attempt-1')
        executor.submit(turn, identifier='attempt-2')
        executor.submit(turn, identifier='attempt-1')
        bindings = [call[1][-1] for call in operator.calls if call[0] == 'submit']
        self.assertNotEqual(bindings[0], bindings[1])
        self.assertEqual(bindings[0], bindings[2], 'lost admission reuses its original group')
        self.assertGreater(operator.calls[0][1][4], 6 * 3600 + 1800,
                           'credential expiry must not preempt the six-hour job plus setup queue')

    def test_queue_limit_still_withdraws_before_execution(self):
        now = [0]
        executor, operator, _ = self.runner([{'status': 'queued'}, {'status': 'queued'}], clock=lambda: now[0])
        handle = executor.submit(self.turn())
        now[0] = 1801
        result = executor.observe(handle)
        self.assertEqual(result['outcome'], 'queue_expired')
        self.assertEqual(operator.calls[-1][0], 'cancel')
        with self.assertRaisesRegex(RunnerTerminated, "No .* start was observed") as error:
            Agents({}, executor).decode_result(None, 'claude', self.stage, result)
        self.assertIn('race with execution', str(error.exception))
        self.assertNotIn('runner ended', str(error.exception))

    def test_long_controller_outage_recovers_result_without_cancelling_or_resubmitting(self):
        now, polls, sleeps = [0], [], []
        result = {'outcome': 'ok', 'response': {'summary': 'finished'}}
        errors = [ExchangeError(status, 'private diagnostic') for status in (0, 408, 429, 500, 503) * 5]
        executor, operator, started = self.runner([
            {'dispatch': 'bound', 'status': 'running'}, *errors, {'status': 'running'},
            ExchangeError(0, 'private diagnostic'), {'status': 'done', 'result': result}], clock=lambda: now[0])
        original = operator.poll
        def poll(*args, **kwargs):
            """Observe actual backoff gaps, without waiting in real time."""
            polls.append(now[0])
            return original(*args, **kwargs)
        def sleep(seconds):
            """Advance the controlled clock and record ownership-check intervals."""
            sleeps.append(seconds)
            now[0] += seconds
        executor.sleep = sleep
        now[0] = 2 * 3600
        with patch.object(operator, 'poll', side_effect=poll):
            self.assertEqual(executor.run(self.turn(), 'implementation'), result)
        gaps = [b - a for a, b in zip(polls, polls[1:])]
        self.assertEqual(gaps[:4], [15, 30, 60, 60])
        self.assertEqual(gaps[-2:], [0, 15], 'a successful poll resets backoff')
        self.assertLessEqual(max(gaps), 60)
        self.assertLessEqual(max(sleeps), 15, 'ownership stays checked during backoff')
        self.assertEqual(started, ['bound'])
        self.assertEqual([c[0] for c in operator.calls].count('submit'), 1)
        self.assertFalse(any(c[0] in ('cancel', 'end') for c in operator.calls))

    def test_permanent_poll_refusals_stop_without_repeating_or_exposing_response_text(self):
        for status in (400, 401, 403, 404, 409, 410, 422):
            with self.subTest(status=status):
                executor, operator, _ = self.runner([{'dispatch': 'bound', 'status': 'running'},
                                                     ExchangeError(status, 'private diagnostic')])
                with self.assertRaisesRegex(RunnerTerminated, f'HTTP {status}') as error:
                    executor.run(self.turn(), 'implementation')
                self.assertNotIn('private diagnostic', str(error.exception))
                self.assertNotIn('timed out', str(error.exception))
                self.assertEqual([c[0] for c in operator.calls].count('poll'), 1)
                self.assertIn(('end', 'bound'), operator.calls)
                self.assertFalse(getattr(error.exception, 'uncertain', False))

    def test_ownership_loss_or_stop_interrupts_backoff_and_cleans_up_only_its_runner(self):
        for reason in ('ownership lost', 'explicit stop'):
            with self.subTest(reason=reason):
                executor, operator, _ = self.runner([{'dispatch': 'bound', 'status': 'running'},
                                                     ExchangeError(503, 'private diagnostic')])
                stopped = [False]
                def owner():
                    """Model the gateway's generation/stop check."""
                    if stopped[0]:
                        raise Blocked(reason)
                executor.check_owner = owner
                executor.sleep = lambda seconds: stopped.__setitem__(0, True)
                with self.assertRaisesRegex(Blocked, reason):
                    executor.run(self.turn(), 'implementation')
                self.assertEqual([c[0] for c in operator.calls].count('poll'), 1)
                self.assertEqual([c for c in operator.calls if c[0] == 'end'], [('end', 'bound')])

    def test_other_roles_keep_their_existing_poll_failure_allowance(self):
        executor, operator, _ = self.runner([{'status': 'running'}, *[ExchangeError(503, '') for _ in range(21)]])
        self.assertEqual(executor.run(test_turns.TURN, 'review')['outcome'], 'uncertain')
        self.assertEqual([c[0] for c in operator.calls].count('poll'), 21)

    def test_unknown_admission_keeps_one_id_and_reports_no_confirmed_start(self):
        executor, operator, started = self.runner([ExchangeError(0, 'private diagnostic') for _ in range(4)])
        result = executor.run(self.turn(), 'implementation')
        self.assertEqual(result['outcome'], 'admission_unknown')
        self.assertEqual(started, [])
        submits = [c[1][0] for c in operator.calls if c[0] == 'submit']
        self.assertEqual(len(submits), 4)
        self.assertEqual(len(set(submits)), 1)
        with self.assertRaisesRegex(RunnerTerminated, 'admission outcome is unknown') as error:
            Agents({}, executor).decode_result(None, 'claude', self.stage, result)
        self.assertIn('no start was confirmed', str(error.exception))
        self.assertIn('race with execution', str(error.exception))
        self.assertNotIn('private diagnostic', str(error.exception))
        self.assertNotIn('runner ended', str(error.exception))

    def test_lost_runner_uses_diagnosis_and_never_becomes_session_failure(self):
        observation = {'status': 'lost', 'runner': {'session': 'bound', 'run_id': '77/1'}}
        executor, operator, _ = self.runner([{'dispatch': 'bound'}, observation])
        seen = []
        executor.runner_status = lambda value: seen.append(value) or 'Implementation runner timed out after 360m.'
        with self.assertRaisesRegex(RunnerTerminated, 'timed out') as error:
            executor.run(self.turn(), 'implementation')
        self.assertFalse(getattr(error.exception, 'uncertain', False), 'service must not auto-retry')
        self.assertTrue(error.exception.result_uncertain)
        self.assertEqual(seen, [observation])
        self.assertEqual([c[0] for c in operator.calls].count('submit'), 1)
        self.assertIn(('end', 'bound'), operator.calls)

    def test_unknown_runner_cause_is_not_reported_as_provider_failure_or_timeout(self):
        executor, _, _ = self.runner([{'status': 'lost'}])
        with self.assertRaisesRegex(RunnerTerminated, 'cause is unconfirmed'):
            executor.run(self.turn(), 'implementation')

    def test_unknown_or_killed_implementation_result_stops_even_with_a_transcript(self):
        for outcome in ('lost', 'uncertain', 'terminated'):
            with self.subTest(outcome=outcome), self.assertRaises(RunnerTerminated) as error:
                Agents({}, None).decode_result({'scope': 'a' * 64}, 'claude', self.stage, {'outcome': outcome})
            self.assertNotIsInstance(error.exception, SessionFailure)


class ReviewExecutorTests(ExecutorTests):
    """PR review shares lifetime, backoff, ownership and writer-recovery invariants."""

    stage = "review"


class NativeTests(test_turns.Workspace):
    """Exercise the real subprocess boundary without contacting a provider."""

    def test_implementation_wrapper_passes_no_deadline_and_still_extracts_changes(self):
        root = turns.directory(self.tmp / 'turns')
        script = test_turns.AGENT.replace('request["timeout"] == 1800', 'request["timeout"] is None')
        turn = {'request': {'stage': 'develop', 'session': None}, 'spec': self.spec,
                'retained': None, 'timeout': None}
        communicate = subprocess.Popen.communicate
        timeouts = []
        def capture(process, *args, **kwargs):
            """Record the native entry process timeout while leaving Git subprocesses intact."""
            if args and isinstance(args[0], str) and '"stage": "develop"' in args[0]:
                timeouts.append(kwargs.get('timeout'))
            return communicate(process, *args, **kwargs)
        with patch.object(turns, 'command', return_value=[test_turns.sys.executable, '-c', script, str(root)]), \
             patch.object(subprocess.Popen, 'communicate', capture):
            result = turns.execute(turn, 'sk-agw-test', self.mirror, root)
        self.assertEqual(timeouts, [None])
        self.assertEqual(result['outcome'], 'ok')
        self.assertTrue(result['workspace']['edits'])

    def test_termination_signal_returns_no_candidate_and_keeps_cleanup(self):
        root = turns.directory(self.tmp / 'turns')
        turn = {'request': {'stage': 'develop', 'session': None}, 'spec': self.spec,
                'retained': None, 'timeout': None}
        script = 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'
        with patch.object(turns, 'command', return_value=[test_turns.sys.executable, '-c', script]):
            result = turns.execute(turn, 'test', self.mirror, root)
        self.assertEqual(result['outcome'], 'terminated')
        self.assertIsNone(result['workspace'])
        self.assertIsNone(result['response'])
        self.assertEqual(self.mirror.git('for-each-ref', 'refs/concurro/snapshots/').stdout, b'')

    def test_heartbeat_loss_still_stops_unbounded_implementation(self):
        import threading
        root = turns.directory(self.tmp / 'turns')
        turn = {'request': {'stage': 'develop', 'session': None}, 'spec': self.spec,
                'retained': None, 'timeout': None}
        stopped = threading.Event()
        stopped.set()
        with patch.object(turns, 'command', return_value=[test_turns.sys.executable, '-c', 'import time; time.sleep(30)']):
            result = turns.execute(turn, 'test', self.mirror, root, stopped)
        self.assertEqual(result['outcome'], 'terminated')
        self.assertIsNone(result['workspace'])

    def test_implementation_job_exits_after_acknowledging_one_turn(self):
        import runner
        turn = {'id': 't1', 'payload': {'request': {'stage': 'develop', 'provider': 'claude'}}}
        client = test_turns.FakeRunner([[turn], [turn]])
        loop = runner.Loop(client, 'test', object(), self.tmp / 'loop')
        with patch.object(turns, 'execute', return_value={'outcome': 'ok', 'response': {}}):
            loop.serve()
        self.assertEqual(len(client.completed), 1)
        self.assertEqual(len(client.batches), 1)
        self.assertTrue(loop.stopped.is_set())

    def test_unbounded_timeout_is_rejected_for_planning(self):
        with self.assertRaisesRegex(turns.TurnError, 'timeout'):
            turns.execute({'request': {'stage': 'plan'}, 'spec': {}, 'timeout': None}, '', None, self.tmp)

    def test_provider_process_receives_none_timeout(self):
        bundle = Bundle()
        root = self.tmp
        request = {'provider': 'claude', 'model': 'test', 'stage': 'develop', 'session': None,
                   'gateway': test_turns.GATEWAY, 'charter': bundle.charter_text, 'bundle_id': bundle.identity,
                   'workspace': str(root), 'scratch': str(root), 'timeout': None,
                   'instruction_texts': bundle.instruction_texts,
                   'context': {'repository_commit': self.base, 'repository': {'refs': {}, 'head': 'main'}}}
        environment = {name: str(root / name) for name in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'COPILOT_HOME')}
        environment['CONCURRO_GATEWAY_KEY'] = 'test'
        cwd = os.getcwd()
        try:
            with patch.dict(os.environ, environment), patch('sys.stdin', io.StringIO(json.dumps(request))), \
                 patch('sys.stdout', io.StringIO()), patch.object(agent_entry.subprocess, 'run', return_value=
                     SimpleNamespace(returncode=0, stdout=json.dumps({'subtype': 'success', 'structured_output': {}}))) as run:
                agent_entry.main()
            self.assertIsNone(run.call_args.kwargs['timeout'])
        finally:
            os.chdir(cwd)

    def test_goal_tracking_enabled_only_for_implementation(self):
        with patch.dict(os.environ, {'CONCURRO_GATEWAY_KEY': 'test'}):
            for stage in ('develop', 'review'):
                request = {'provider': 'claude', 'stage': stage, 'gateway': test_turns.GATEWAY, 'model': 'test', 'schema': {}}
                env = agent_entry.provider_environment(request)
                self.assertEqual(env.get('CLAUDE_CODE_ENABLE_TODO_TOOLS'), '1' if stage == 'develop' else None)
                command = agent_entry.command_for({**request, 'provider': 'codex'}, 'schema', 'output')
                self.assertEqual('features.goals=true' in command, stage == 'develop')
        instructions = json.dumps(Bundle().instructions('develop'))
        self.assertIn('Set up a goal', instructions)
        self.assertIn('Do not invent a time or token budget', instructions)


class LifecycleTests(unittest.TestCase):
    """Persist termination without spending another call until a writer explicitly retries."""

    setUp = test_consensus.ConsensusTests.setUp
    tick = test_consensus.ConsensusTests.tick
    reach = test_consensus.ConsensusTests.reach

    def test_termination_blocks_once_and_writer_retry_keeps_spent_budget(self):
        self.assert_writer_recovery(RunnerTerminated('Implementation runner timed out'))

    def test_admission_and_queue_diagnostics_survive_restart_until_writer_retry(self):
        for outcome in ('admission_unknown', 'queue_expired'):
            with self.subTest(outcome=outcome):
                self.setUp()
                with self.assertRaises(RunnerTerminated) as error:
                    Agents({}, None).decode_result(None, 'claude', 'develop', {'outcome': outcome})
                self.assert_writer_recovery(error.exception)

    def assert_writer_recovery(self, termination, stage="implement"):
        """A saved stop preserves its diagnosis, consumed budget and writer retry requirement."""
        state = self.reach(stage)
        spent = state['calls']
        active = Controller(self.gh, self.agents, self.config)
        with patch.object(self.agents, 'ask', side_effect=termination) as ask:
            with self.assertRaises(RunnerTerminated) as error:
                active.advance(7)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(active.state['calls'], spent + 1)
        self.assertEqual(active.state['operation']['status'], 'uncertain')
        from service import failures
        self.assertEqual(failures.effect(error.exception, True), 'suspend')
        self.assertEqual(failures.classify(error.exception).kind, 'blocked')
        # Crash after the saved termination but before its Blocked notice: no second call.
        reloaded = Controller(self.gh, self.agents, self.config)
        with patch.object(self.agents, 'ask') as replay:
            with self.assertRaises(RunnerTerminated):
                reloaded.advance(7)
        replay.assert_not_called()
        self.assertEqual(reloaded.state['operation']['runner_termination'], str(termination))
        self.assertEqual(reloaded.state['calls'], spent + 1)
        self.assertEqual(reloaded.state['sessions'], active.state['sessions'])
        active.suspend('blocked', str(error.exception), outboxes=False)
        self.assertEqual(self.tick()['stage'], 'blocked')
        self.gh.comment('/concurro retry')
        retried = self.tick()
        self.assertEqual(retried['calls'], spent + 2)
        self.assertEqual(retried['stage'], 'publish' if stage == 'implement' else 'review')
        if stage == 'review':
            self.assertEqual(retried['head'], state['head'])
            self.assertEqual(retried['plan_id'], state['plan_id'])
            self.assertEqual(retried['findings'], state['findings'])


class ReviewLifecycleTests(unittest.TestCase):
    """An interrupted PR review keeps the published head and needs a writer retry."""

    setUp = LifecycleTests.setUp
    tick = LifecycleTests.tick
    reach = LifecycleTests.reach
    assert_writer_recovery = LifecycleTests.assert_writer_recovery

    def test_pr_review_termination_is_saved_without_automatic_second_call(self):
        self.assert_writer_recovery(RunnerTerminated('PR review runner timed out'), stage='review')


    def test_retry_keeps_another_reviewers_findings_and_only_repeats_failed_review(self):
        self.reach('review')
        self.agents.fix = True
        prior = self.tick()
        self.assertEqual(prior['stage'], 'review')
        self.assertTrue(prior['findings'])
        controller = Controller(self.gh, self.agents, self.config)
        with patch.object(self.agents, 'ask', side_effect=RunnerTerminated('PR review runner stopped')):
            with self.assertRaises(RunnerTerminated):
                controller.advance(7)
        controller.suspend('blocked', 'PR review runner stopped', outboxes=False)
        self.gh.comment('/concurro retry')
        count = len(self.agents.calls)
        resumed = self.tick()
        self.assertEqual(len(self.agents.calls), count + 1)
        self.assertEqual(self.agents.calls[-1][:2], ('codex', 'review'))
        self.assertEqual(resumed['head'], prior['head'])
        self.assertEqual(resumed['findings'], prior['findings'])
        self.assertEqual(resumed['calls'], prior['calls'] + 2)
        self.assertEqual(resumed['stage'], 'implement', 'the surviving finding must still require repair')


class ReviewNativeTests(test_turns.Workspace):
    """Actual review child processes retain cleanup, diagnostics and fresh-job semantics."""

    def run_review(self, script, stopped=None, timeout=None):
        """Run a tiny local stand-in for the CLI with the real snapshot and cleanup path."""
        root = turns.directory(self.tmp / 'turns')
        turn = {'request': {'stage': 'review', 'session': None}, 'spec': self.spec,
                'retained': None, 'timeout': timeout}
        with patch.object(turns, 'command', return_value=[test_turns.sys.executable, '-c', script]):
            return turns.execute(turn, 'test', self.mirror, root, stopped)

    def test_native_review_has_no_deadline_and_never_extracts_edits(self):
        script = ('import json, sys; from pathlib import Path; r=json.load(sys.stdin); '
                  'assert r["timeout"] is None; Path(r["workspace"], "scratch.txt").write_text("exploration"); '
                  'print(json.dumps({"verdict":"approve"}))')
        result = self.run_review(script)
        self.assertEqual(result['response'], {'verdict': 'approve'})
        self.assertIsNone(result['workspace'])
        self.assertIsNone(result['retained'], 'PR reviews remain fresh contexts')
        self.assertEqual(self.mirror.git('for-each-ref', 'refs/concurro/snapshots/').stdout, b'')

    def test_signal_and_stop_have_safe_distinct_diagnostics(self):
        import threading
        stopped = threading.Event()
        stopped.set()
        for script, event, cause in (
                ('import os, signal; os.kill(os.getpid(), signal.SIGTERM)', None, 'process_signal'),
                ('import time; time.sleep(30)', stopped, 'runner_stopped')):
            result = self.run_review(script, event)
            self.assertEqual(result['outcome'], 'terminated')
            self.assertEqual(result['termination_reason'], cause)
            self.assertGreaterEqual(result['elapsed_seconds'], 0)
            self.assertIsNone(result['response'])
            with self.assertRaisesRegex(RunnerTerminated, 'PR review'):
                Agents({}, None).decode_result(None, 'claude', 'review', result)

    def test_internal_timeout_survives_both_subprocess_boundaries(self):
        from policy import TIMEOUT_EXIT, UNCERTAIN_EXIT
        with patch.object(agent_entry, 'main', side_effect=subprocess.TimeoutExpired('secret command', 1200)), \
             patch('sys.stderr', io.StringIO()) as stderr:
            self.assertEqual(agent_entry.run(), TIMEOUT_EXIT)
        self.assertNotIn('secret command', stderr.getvalue())
        result = self.run_review(f'import sys; sys.exit({TIMEOUT_EXIT})', timeout=1200)
        self.assertEqual(result['outcome'], 'timed_out')
        self.assertEqual(result['termination_reason'], 'turn_timeout')
        with self.assertRaisesRegex(RunnerTerminated, 'execution time limit.*turn time'):
            Agents({}, None).decode_result(None, 'claude', 'review', result)
        self.assertEqual(turns.outcome(UNCERTAIN_EXIT, ''), ('uncertain', None), 'legacy uncertainty is not relabelled')

    def test_outer_wrapper_timeout_keeps_cause_and_cleans_up(self):
        communicate = subprocess.Popen.communicate
        def timeout(process, *args, **kwargs):
            """Expire only the agent wrapper, leaving snapshot Git processes intact."""
            if args and isinstance(args[0], str) and '"stage": "review"' in args[0]:
                raise subprocess.TimeoutExpired('private', 1260)
            return communicate(process, *args, **kwargs)
        with patch.object(subprocess.Popen, 'communicate', timeout):
            result = self.run_review('import time; time.sleep(30)', timeout=1200)
        self.assertEqual(result['termination_reason'], 'turn_timeout')
        self.assertEqual(result['outcome'], 'timed_out')
        self.assertEqual(self.mirror.git('for-each-ref', 'refs/concurro/snapshots/').stdout, b'')

    def test_review_job_acknowledges_one_turn_then_exits(self):
        import runner
        turn = {'id': 'r1', 'payload': {'request': {'stage': 'review', 'provider': 'claude'}}}
        client = test_turns.FakeRunner([[turn], [turn]])
        loop = runner.Loop(client, 'test', object(), self.tmp / 'loop')
        with patch.object(turns, 'execute', return_value={'outcome': 'ok', 'response': {}}):
            loop.serve()
        self.assertEqual(len(client.completed), 1)
        self.assertEqual(len(client.batches), 1)
        self.assertTrue(loop.stopped.is_set())

    def test_unrecognized_diagnostic_text_is_never_published(self):
        with self.assertRaises(RunnerTerminated) as caught:
            Agents({}, None).decode_result(None, 'claude', 'review', {
                'outcome': 'terminated', 'termination_reason': 'private provider diagnostic', 'elapsed_seconds': 'secret'})
        self.assertIn('cause is unconfirmed', str(caught.exception))
        self.assertNotIn('private', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))


class BoundedStageTests(unittest.TestCase):
    """Timing diagnosis must not alter the independent planning-stage retry policy."""

    def test_other_stage_timeouts_keep_uncertain_recovery_with_a_specific_reason(self):
        result = {'outcome': 'timed_out'}
        with self.assertRaisesRegex(UncertainOutcome, 'execution time limit'):
            Agents({}, None).decode_result(None, 'claude', 'intake', result)
        with self.assertRaises(SessionFailure) as caught:
            Agents({}, None).decode_result({'scope': 'a' * 64}, 'claude', 'plan_qc', result)
        self.assertTrue(caught.exception.uncertain)
