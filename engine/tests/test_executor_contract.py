"""Literal transport contracts pinned before the result/executor refactor."""

import json
import tempfile
import unittest
from copy import deepcopy
from itertools import product
from unittest.mock import Mock, patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import runner
import runner_fixtures
import turns
from agents import Agents, RunnerExecutor
from policy import Blocked, RunnerTerminated, SessionFailure, UncertainOutcome
from relay import ExchangeError


class DecodeContractTests(unittest.TestCase):
    """Result categories, public text and transcript handling are transport contracts."""

    def test_decoding_table(self):
        stages = {
            "intake": None,
            "plan": None,
            "plan_qc": None,
            "develop": "Implementation",
            "review": "PR review",
            "future": None,
        }
        outcomes = (
            "ok",
            "failed",
            "uncertain",
            "lost",
            "timed_out",
            "terminated",
            "admission_unknown",
            "queue_expired",
            "future",
        )
        reasons = {
            "turn_timeout": "reached Concurro's execution time limit",
            "runner_stopped": "was stopped",
            "process_signal": "was terminated by a signal",
            "future": "ended; the cause is unconfirmed",
            None: "ended; the cause is unconfirmed",
        }
        agent = Agents({}, None)
        for stage, outcome, reason, retained in product(stages, outcomes, reasons, (False, True)):
            with self.subTest(stage=stage, outcome=outcome, reason=reason, retained=retained):
                session = {"scope": "a" * 64} if retained else None
                result = {"outcome": outcome, "response": "answer", "termination_reason": reason, "elapsed_seconds": 17}
                label = stages[stage]
                uncertain = outcome in ("uncertain", "lost", "timed_out")
                if outcome == "ok":
                    self.assertEqual(agent.decode_result(session, "claude", stage, result), "answer")
                    continue
                if label and outcome in ("admission_unknown", "queue_expired"):
                    prefix = {
                        "admission_unknown": f"{label} admission outcome is unknown; no start was confirmed.",
                        "queue_expired": f"No {label.lower()} start was observed within the 30-minute dispatch/setup limit.",
                    }[outcome]
                    kind, message = (
                        RunnerTerminated,
                        (
                            prefix
                            + " Withdrawal was attempted, but admission or cancellation can race with execution. "
                            "Inspect the exchange and runner before a writer retries; no automatic restart was attempted."
                        ),
                    )
                elif label and outcome in ("timed_out", "terminated", "lost", "uncertain"):
                    cause = "reached Concurro's execution time limit" if outcome == "timed_out" else reasons[reason]
                    kind, message = (
                        RunnerTerminated,
                        (
                            f"{label} {cause} after 17s of turn time. No result was saved; execution may be incomplete. "
                            "Inspect the runner before a writer retries; no automatic restart was attempted."
                        ),
                    )
                elif outcome == "timed_out":
                    kind = SessionFailure if retained else UncertainOutcome
                    message = "Concurro execution time limit reached; no result was saved."
                elif retained:
                    kind, message = SessionFailure, "provider session failed; output suppressed to protect credentials"
                elif uncertain:
                    kind, message = UncertainOutcome, "claude outcome is unknown; output suppressed"
                else:
                    kind, message = Blocked, "claude failed; output suppressed to protect credentials"
                with self.assertRaises(kind) as caught:
                    agent.decode_result(session, "claude", stage, result)
                self.assertIs(type(caught.exception), kind)
                self.assertEqual(str(caught.exception), message)
                if kind is SessionFailure:
                    self.assertEqual(caught.exception.uncertain, uncertain)

    def test_untrusted_reason_and_duration_boundaries(self):
        agent = Agents({}, None)
        for reason, seconds, duration in product(
            ([], {}, 7),
            (None, True, "17", -1, 86401, float("nan"), float("inf")),
            ("",),
        ):
            with self.subTest(reason=reason, seconds=seconds), self.assertRaises(RunnerTerminated) as caught:
                agent.decode_result(
                    None,
                    "claude",
                    "develop",
                    {"outcome": "terminated", "termination_reason": reason, "elapsed_seconds": seconds},
                )
            self.assertEqual(
                str(caught.exception),
                f"Implementation ended; the cause is unconfirmed{duration}. No result was saved; execution may be incomplete. Inspect the runner before a writer retries; no automatic restart was attempted.",
            )
        for seconds, duration in ((0, "0"), (17.6, "18"), (86400, "86400")):
            with self.subTest(seconds=seconds), self.assertRaises(RunnerTerminated) as caught:
                agent.decode_result(None, "claude", "review", {"outcome": "terminated", "elapsed_seconds": seconds})
            self.assertEqual(
                str(caught.exception),
                f"PR review ended; the cause is unconfirmed after {duration}s of turn time. No result was saved; execution may be incomplete. Inspect the runner before a writer retries; no automatic restart was attempted.",
            )

    def test_retained_archive_is_saved_before_failure(self):
        agent = Agents({}, None)
        agent.retained = Mock()
        session = {"scope": "a" * 64}
        with self.assertRaises(RunnerTerminated):
            agent.decode_result(session, "claude", "develop", {"outcome": "lost", "retained": "archive"})
        agent.retained.save.assert_called_once_with(session, "archive")


class ExecutorContractTests(runner_fixtures.ExecutorFixture, unittest.TestCase):
    """Exact messages, byte ordering and clocks at the executor boundary."""

    def turn(self, stage):
        """Build a fresh payload without changing the shared fixture."""
        turn = deepcopy(runner_fixtures.TURN)
        turn["request"]["stage"] = stage
        if stage in ("develop", "review"):
            turn["timeout"] = None
        return turn

    def test_runner_loss_messages_and_diagnosis_override(self):
        for stage, label in (("develop", "Implementation"), ("review", "PR review")):
            for status, diagnosis in product(("lost", "cancelled"), (None, "Confirmed runner diagnosis.")):
                with self.subTest(stage=stage, status=status, diagnosis=diagnosis):
                    executor, _, _ = self.runner([{"status": "running"}])
                    executor.runner_status = Mock(return_value=diagnosis)
                    handle = executor.submit(self.turn(stage))
                    reply = {"status": status}
                    with self.assertRaises(RunnerTerminated) as caught:
                        executor.receive(handle, reply)
                    self.assertEqual(
                        str(caught.exception),
                        diagnosis
                        or f"{label} runner stopped reporting or was ended; no result was saved. The cause is unconfirmed. Inspect the runner before a writer retries.",
                    )
                    executor.runner_status.assert_called_once_with(reply)

    def test_poll_refusal_messages_and_status_boundaries(self):
        for stage, label in (("develop", "Implementation"), ("review", "PR review")):
            for status in (200, 301, 400, 401, 403, 404, 409, 410, 422, 499, 600):
                with self.subTest(stage=stage, status=status):
                    executor, _, _ = self.runner([{"status": "running"}, ExchangeError(status, "private")])
                    handle = executor.submit(self.turn(stage))
                    with self.assertRaises(RunnerTerminated) as caught:
                        executor.observe(handle)
                    self.assertEqual(
                        str(caught.exception),
                        f"{label} status could not be read: exchange refused polling (HTTP {status}). Execution status is unknown. Inspect the exchange and runner before a writer retries; no automatic restart was attempted.",
                    )
            for status in (0, 408, 429, 500, 599):
                with self.subTest(stage=stage, retry_status=status):
                    executor, _, _ = self.runner([{"status": "running"}, ExchangeError(status, "private")])
                    self.assertIsNone(executor.observe(executor.submit(self.turn(stage))))

    def test_empty_result_json_bytes(self):
        self.assertEqual(
            json.dumps(RunnerExecutor.lost("uncertain")).encode(),
            b'{"outcome": "uncertain", "response": null, "retained": null, "workspace": null}',
        )
        with tempfile.TemporaryDirectory() as root:
            client = runner_fixtures.FakeRunner([])
            loop = runner.Loop(client, "test", object(), root)
            with patch.object(turns, "execute", side_effect=ValueError("private")), patch.object(runner, "log"):
                loop.run({"id": "t", "payload": self.turn("plan")})
        self.assertEqual(client.completed[0][0], "t")
        self.assertEqual(
            json.dumps(client.completed[0][1]).encode(),
            b'{"outcome": "failed", "response": null, "workspace": null, "retained": null}',
        )

    def test_backoff_and_non_job_failure_limit(self):
        for stage in ("plan", "develop", "review"):
            now = [0]
            executor, _, _ = self.runner(
                [{"status": "running"}, *[ExchangeError(503, "") for _ in range(21)]], clock=lambda: now[0]
            )
            handle = executor.submit(self.turn(stage))
            delays = []
            for attempt in range(21):
                result = executor.observe(handle)
                delays.append(handle.next_poll - now[0])
                now[0] = handle.next_poll
                if stage == "plan" and attempt == 20:
                    self.assertEqual(
                        result, {"outcome": "uncertain", "response": None, "retained": None, "workspace": None}
                    )
                else:
                    self.assertIsNone(result)
            self.assertEqual(delays, [15] * 21 if stage == "plan" else [15, 30, *[60] * 19])

    def test_lease_values_and_admission_retry_delay(self):
        for stage, default, custom in (("plan", 21600, 4000), ("develop", 23700, 6100), ("review", 23700, 6100)):
            for lease, expected in ((None, default), (4000, custom)):
                with self.subTest(stage=stage, lease=lease):
                    executor, operator, _ = self.runner(
                        [ExchangeError(503, ""), ExchangeError(503, ""), ExchangeError(503, ""), {}]
                    )
                    if lease is not None:
                        executor.lease = lease
                    executor.sleep = Mock()
                    executor.submit(self.turn(stage))
                    self.assertEqual([call[1][4] for call in operator.calls], [expected] * 4)
                    self.assertEqual([call.args for call in executor.sleep.call_args_list], [(15,)] * 3)

    def test_poll_waits_and_ownership_check_intervals(self):
        executor, operator, _ = self.runner([{}, {"status": "running"}])
        handle = executor.submit(self.turn("develop"))
        with patch.object(operator, "poll", wraps=operator.poll) as poll:
            executor.observe(handle)
        self.assertEqual(poll.call_args.kwargs, {"wait": 0})
        now, events = [0], []
        result = {"outcome": "ok", "response": "done"}
        executor, operator, _ = self.runner(
            [{"status": "running"}, *[ExchangeError(503, "") for _ in range(3)], {"status": "done", "result": result}],
            clock=lambda: now[0],
        )

        def sleep(seconds):
            """Record sleep boundaries and advance the deterministic clock."""
            events.append(("sleep", seconds))
            now[0] += seconds

        executor.sleep = sleep
        executor.check_owner = lambda: events.append(("owner", now[0]))
        with patch.object(operator, "poll", wraps=operator.poll) as poll:
            self.assertEqual(executor.run(self.turn("develop"), "test"), result)
        self.assertEqual([call.kwargs for call in poll.call_args_list], [{"wait": 45}] * 4)
        self.assertEqual([event[1] for event in events if event[0] == "sleep"], [15] * 7)
        for index, event in enumerate(events):
            if event[0] == "sleep":
                self.assertEqual(events[index - 1][0], "owner")
